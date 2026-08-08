"""The scheduler: decides *when* the four posts fire.

Not a cron. NFL kickoff times move -- Thursday, Saturday in December, 9:30am
London games, Monday doubleheaders, flexed Sunday nights -- and a fixed
"Monday 11pm" would eventually fire a week recap during the fourth quarter.

So the triggers read game state instead of a clock. The schedule endpoint gives
each game a ``date`` and a live ``status``, and the only distinction that
matters is ``pre_game`` versus not. That yields conditions that cannot be wrong
about the NFL because they are not predicting anything:

    lineups   the main slate day has arrived, most of its games have not
              kicked off yet, and it is past a civilised hour
    sunday    the main slate is essentially done but the week is not
    recap     every game in the week is complete (plus a settle delay)
    ahead     it is Wednesday morning and the week has rolled over

Clock time is used for exactly two things: not posting at 4am, and knowing
which day it is. Everything else is derived from the data.

**Every fire is recorded in SQLite before the message is sent**, keyed by
(season, week, kind). A restart mid-week reposts nothing, and each post type
can fire at most once per week.

At most one post goes out per tick, which spaces sends naturally and gives a
future rate limiter or quiet-hours mute a single place to hook in -- see
:meth:`Scheduler.publish`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import ffposts
from ffleague import load_view
from logging_setup import get_logger
from sleeper import GAME_FINISHED, GAME_NOT_STARTED, SleeperError

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover -- Python < 3.9
    ZoneInfo = None  # type: ignore

log = get_logger(__name__)

DEFAULT_TIMEZONE = "America/New_York"
POLL_SECONDS = 300.0

# Tunables. Deliberately not configuration: they are judgement calls about the
# NFL, not preferences, and exposing them would be six more knobs nobody turns.
# Only job is "do not post at 4am". What actually stops a useless lineup check
# is the not-yet-kicked-off guard below, not the clock. 9 rather than 10
# because the 1pm ET slate starts at 10am on the west coast, and a warning
# that arrives at kickoff is not a warning.
LINEUP_EARLIEST_HOUR = 9
LINEUP_MIN_UNSTARTED = 0.5  # still worth posting if <=half the slate has begun
SUNDAY_SLATE_DONE = 0.75  # "the afternoon games are over"
RECAP_SETTLE_SECONDS = 1800.0  # let Sleeper's final scoring settle
AHEAD_EARLIEST_HOUR = 9
AHEAD_WEEKDAY = 2  # Monday=0, so 2 is Wednesday


def get_timezone(name: str):
    """Resolve a timezone name, falling back to UTC rather than failing.

    A container missing tzdata should produce slightly odd post times, not a
    dead scheduler thread.
    """
    if not name or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning(
            "unknown timezone; falling back to UTC. Install tzdata to fix.",
            extra={"fields": {"timezone": name}},
        )
        return timezone.utc


@dataclass
class Decision:
    """Why a post did or did not fire. Returned for logging and tests."""

    kind: str
    fire: bool
    reason: str


# ---------------------------------------------------------------------------
# game-state predicates
# ---------------------------------------------------------------------------


def _slate_day(games: list[dict]) -> str | None:
    """The date carrying the most games -- Sunday in every normal week."""
    if not games:
        return None
    counts: dict[str, int] = {}
    for game in games:
        day = str(game.get("date") or "")
        if day:
            counts[day] = counts.get(day, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _split(games: list[dict]) -> tuple[int, int, int]:
    """(total, not yet started, finished)."""
    total = len(games)
    unstarted = sum(1 for g in games if str(g.get("status") or "") == GAME_NOT_STARTED)
    finished = sum(1 for g in games if str(g.get("status") or "") in GAME_FINISHED)
    return total, unstarted, finished


def week_is_complete(games: list[dict]) -> bool:
    total, _, finished = _split(games)
    return total > 0 and finished == total


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    def __init__(self, cfg, bb_client, sleeper_client, store):
        self.cfg = cfg
        self.bb = bb_client
        self.sleeper = sleeper_client
        self.store = store
        self.tz = get_timezone(getattr(cfg, "sleeper_timezone", DEFAULT_TIMEZONE))
        self._stop = threading.Event()

    # -- configuration -------------------------------------------------------

    @property
    def chat(self) -> str:
        return getattr(self.cfg, "sleeper_post_chat", "") or ""

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "sleeper_posts_enabled", False)) and bool(self.chat)

    def kind_enabled(self, kind: str) -> bool:
        return kind in (getattr(self.cfg, "sleeper_post_types", ()) or ())

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # -- the send seam -------------------------------------------------------

    def publish(self, text: str, kind: str) -> bool:
        """The single place a scheduled message leaves the process.

        Rate limiting, cooldowns and a quiet-hours mute are out of scope for
        now, but they all belong here rather than sprinkled through the
        triggers -- one function to gate, one place to log what was suppressed.
        """
        if getattr(self.cfg, "dry_run", False):
            log.info(
                "DRY RUN: scheduled post not sent",
                extra={"fields": {"kind": kind, "chars": len(text), "message": text}},
            )
            return True
        try:
            self.bb.send_text(self.chat, text)
            return True
        except Exception as exc:
            log.error(
                "failed to send a scheduled post",
                extra={"fields": {"kind": kind, "error": str(exc)}},
            )
            return False

    # -- triggers ------------------------------------------------------------

    def decide(self, kind: str, view, games: list[dict], now: datetime) -> Decision:
        """Should ``kind`` fire right now? Pure, given the inputs."""
        today = now.date().isoformat()
        total, unstarted, finished = _split(games)

        if kind == "lineups":
            slate = _slate_day(games)
            if slate is None:
                return Decision(kind, False, "no games scheduled this week")
            if today != slate:
                return Decision(kind, False, f"not the slate day ({slate})")
            if now.hour < LINEUP_EARLIEST_HOUR:
                return Decision(kind, False, f"too early ({now.hour}:00)")
            slate_games = [g for g in games if str(g.get("date") or "") == slate]
            slate_total, slate_unstarted, _ = _split(slate_games)
            if not slate_total:
                return Decision(kind, False, "no games on the slate day")
            # An early London kickoff should not block the post for the 1pm
            # slate, but once most of the day is under way it is too late to
            # be useful to anyone.
            if slate_unstarted / slate_total < LINEUP_MIN_UNSTARTED:
                return Decision(kind, False, "the slate is already under way")
            return Decision(kind, True, "slate day, most games not started")

        if kind == "sunday":
            slate = _slate_day(games)
            if slate is None or today != slate:
                return Decision(kind, False, "not the slate day")
            slate_games = [g for g in games if str(g.get("date") or "") == slate]
            slate_total, _, slate_finished = _split(slate_games)
            if not slate_total:
                return Decision(kind, False, "no games on the slate day")
            if slate_finished / slate_total < SUNDAY_SLATE_DONE:
                return Decision(kind, False, "the afternoon games are not done")
            if finished >= total:
                # Nothing left to look forward to; the recap covers it.
                return Decision(kind, False, "the week is already over")
            return Decision(kind, True, "afternoon slate done, week still live")

        if kind == "recap":
            if not week_is_complete(games):
                return Decision(kind, False, f"{total - finished} games still to finish")
            # Sleeper keeps scoring for a few minutes after the whistle. Wait
            # for it to settle rather than posting a recap that is wrong by 0.4.
            marker = f"complete_seen:{view.season}:{view.week}"
            first_seen = self.store.get_meta(marker, "")
            if not first_seen:
                self.store.set_meta(marker, str(int(time.time())))
                return Decision(kind, False, "week just finished; letting scores settle")
            try:
                waited = time.time() - int(first_seen)
            except ValueError:
                waited = RECAP_SETTLE_SECONDS
            if waited < RECAP_SETTLE_SECONDS:
                return Decision(kind, False, f"settling ({int(waited)}s of {int(RECAP_SETTLE_SECONDS)}s)")
            return Decision(kind, True, "every game complete and scores settled")

        if kind == "ahead":
            if now.weekday() != AHEAD_WEEKDAY:
                return Decision(kind, False, "not Wednesday")
            if now.hour < AHEAD_EARLIEST_HOUR:
                return Decision(kind, False, f"too early ({now.hour}:00)")
            if total and finished >= total:
                # state.week has not rolled over yet, so this is still last
                # week. Posting "the week ahead" about a finished week is worse
                # than waiting a tick.
                return Decision(kind, False, "the week shown is already played")
            return Decision(kind, True, "Wednesday morning, new week loaded")

        return Decision(kind, False, "unknown post type")

    # -- one pass ------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> str | None:
        """Evaluate every post type; send at most one. Returns the kind sent.

        Never raises. A scheduler thread that dies takes the whole week's posts
        with it and nobody notices until Monday.
        """
        if not self.enabled:
            return None
        now = now or self.now()

        try:
            state = self.sleeper.state().value or {}
        except SleeperError as exc:
            log.info("scheduler: no NFL state", extra={"fields": {"error": str(exc)}})
            return None

        season_type = str(state.get("season_type") or "")
        if season_type not in ("regular", "post"):
            return None  # nothing to say in the pre- or off-season

        try:
            view = load_view(
                self.sleeper,
                getattr(self.cfg, "sleeper_league_id", ""),
                nicknames=self.store.nicknames(),
                follow_previous=False,  # never post last season's data as news
            )
        except SleeperError as exc:
            log.info("scheduler: no league", extra={"fields": {"error": str(exc)}})
            return None

        if not view.has_games:
            return None

        games = self.sleeper.week_games(view.season, view.week)

        for kind in ffposts.POST_ORDER:
            if not self.kind_enabled(kind):
                continue
            if self.store.already_posted(view.season, view.week, kind):
                continue

            decision = self.decide(kind, view, games, now)
            log.debug(
                "scheduler decision",
                extra={"fields": {"kind": kind, "fire": decision.fire, "reason": decision.reason,
                                  "week": view.week}},
            )
            if not decision.fire:
                continue

            text = ffposts.build(kind, view, view.week)
            if not text:
                # Nothing worth saying, or the data was not there. Do NOT mark
                # it posted: a clean lineup at 10am may be a broken one at
                # 11am, and an outage now should not cost the week's recap.
                log.info(
                    "scheduler: nothing to post",
                    extra={"fields": {"kind": kind, "week": view.week}},
                )
                continue

            # Recorded before sending. A crash between the two loses one post;
            # the other order risks posting the same thing every five minutes.
            self.store.mark_posted(view.season, view.week, kind)
            if self.publish(text, kind):
                log.info(
                    "scheduled post sent",
                    extra={"fields": {"kind": kind, "week": view.week, "chars": len(text)}},
                )
            return kind

        return None

    # -- the loop ------------------------------------------------------------

    def run_forever(self, poll_seconds: float = POLL_SECONDS) -> None:
        log.info(
            "scheduler started",
            extra={
                "fields": {
                    "chat": self.chat,
                    "types": sorted(getattr(self.cfg, "sleeper_post_types", ()) or ()),
                    "timezone": str(self.tz),
                    "poll_s": poll_seconds,
                }
            },
        )
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed; continuing")
            self._stop.wait(poll_seconds)

    def start(self, poll_seconds: float = POLL_SECONDS) -> threading.Thread | None:
        if not self.enabled:
            log.info(
                "scheduled posts are off",
                extra={"fields": {"enabled": getattr(self.cfg, "sleeper_posts_enabled", False),
                                  "chat": bool(self.chat)}},
            )
            return None
        thread = threading.Thread(target=self.run_forever, args=(poll_seconds,),
                                  name="ff-scheduler", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
