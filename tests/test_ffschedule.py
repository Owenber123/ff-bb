"""Trigger logic and the at-most-once guarantee.

Two failure modes matter more than the rest. Posting a week recap while a game
is still being played is the embarrassing one; reposting Monday's recap every
five minutes after a restart is the one that gets the bot muted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import ffposts
import ffschedule
from conftest import LEAGUE_ID, FakeSession, build_routes
from ffleague import load_view
from ffschedule import Scheduler, week_is_complete
from ffstore import Store
from sleeper import SleeperClient

TZ = ZoneInfo("America/New_York")

# A representative week: Thursday night, the Sunday slate, and Monday night.
SUNDAY = "2025-10-05"
THURSDAY = "2025-10-02"
MONDAY = "2025-10-06"


def games(sunday_status="pre_game", thursday="complete", monday="pre_game", sunday_count=12,
          started_early=0):
    """Build a week of games with controllable statuses."""
    out = [{"week": 5, "date": THURSDAY, "home": "BUF", "away": "KC", "status": thursday}]
    for i in range(sunday_count):
        status = sunday_status
        if started_early and i < started_early:
            status = "complete"
        out.append({"week": 5, "date": SUNDAY, "home": f"H{i}", "away": f"A{i}", "status": status})
    out.append({"week": 5, "date": MONDAY, "home": "PHI", "away": "DAL", "status": monday})
    return out


def at(day: str, hour: int, minute: int = 0) -> datetime:
    year, month, dayn = (int(p) for p in day.split("-"))
    return datetime(year, month, dayn, hour, minute, tzinfo=TZ)


class FakeBB:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, chat, message):
        self.sent.append((chat, message))
        return {"ok": True}


@pytest.fixture
def sched(tmp_path):
    session = FakeSession(build_routes())
    sleeper = SleeperClient(tmp_path / "d", session=session)
    sleeper.players()
    store = Store(tmp_path / "bot.db")
    cfg = SimpleNamespace(
        sleeper_league_id=LEAGUE_ID,
        sleeper_post_chat="iMessage;+;chatFF",
        sleeper_posts_enabled=True,
        sleeper_post_types=ffposts.POST_ORDER,
        sleeper_timezone="America/New_York",
        sleeper_follow_previous=False,
        dry_run=False,
    )
    bb = FakeBB()
    scheduler = Scheduler(cfg, bb, sleeper, store)
    view = load_view(sleeper, LEAGUE_ID)
    return SimpleNamespace(
        scheduler=scheduler, bb=bb, store=store, view=view, cfg=cfg,
        sleeper=sleeper, session=session,
    )


def decide(sched, kind, game_list, when):
    return sched.scheduler.decide(kind, sched.view, game_list, when)


# ---------------------------------------------------------------------------
# lineup check
# ---------------------------------------------------------------------------


def test_lineups_fire_on_sunday_morning(sched):
    assert decide(sched, "lineups", games(), at(SUNDAY, 11)).fire


def test_lineups_do_not_fire_at_dawn(sched):
    assert not decide(sched, "lineups", games(), at(SUNDAY, 6)).fire


def test_lineups_do_not_fire_on_a_saturday(sched):
    assert not decide(sched, "lineups", games(), at("2025-10-04", 11)).fire


def test_lineups_do_not_fire_once_the_slate_is_under_way(sched):
    # Too late to be useful: telling somebody their QB is out at 3pm is noise.
    late = games(sunday_status="complete")
    assert not decide(sched, "lineups", late, at(SUNDAY, 15)).fire


def test_an_early_london_kickoff_does_not_block_the_post(sched):
    # One 9:30am game having started must not suppress the warning for the
    # 1pm slate, which is the whole audience for this post.
    with_london = games(started_early=1)
    assert decide(sched, "lineups", with_london, at(SUNDAY, 11)).fire


def test_lineups_stop_once_most_of_the_slate_has_kicked_off(sched):
    assert not decide(sched, "lineups", games(started_early=7), at(SUNDAY, 13)).fire


def test_the_slate_day_is_derived_not_assumed(sched):
    """A week whose main slate is a Saturday still works.

    Late-season weeks move games to Saturday; nothing here hardcodes Sunday.
    """
    saturday_week = [
        {"week": 16, "date": "2025-12-20", "home": f"H{i}", "away": f"A{i}", "status": "pre_game"}
        for i in range(10)
    ] + [{"week": 16, "date": "2025-12-21", "home": "PHI", "away": "DAL", "status": "pre_game"}]
    assert decide(sched, "lineups", saturday_week, at("2025-12-20", 11)).fire
    assert not decide(sched, "lineups", saturday_week, at("2025-12-21", 11)).fire


# ---------------------------------------------------------------------------
# sunday evening
# ---------------------------------------------------------------------------


def test_sunday_fires_once_the_afternoon_games_are_done(sched):
    afternoon_done = games(sunday_status="complete", monday="pre_game")
    assert decide(sched, "sunday", afternoon_done, at(SUNDAY, 19)).fire


def test_sunday_does_not_fire_mid_afternoon(sched):
    mid = games(started_early=4)
    assert not decide(sched, "sunday", mid, at(SUNDAY, 15)).fire


def test_sunday_does_not_fire_when_the_week_is_already_over(sched):
    # No Monday game: the recap covers it, so a Sunday update is redundant.
    everything_done = games(sunday_status="complete", thursday="complete", monday="complete")
    assert not decide(sched, "sunday", everything_done, at(SUNDAY, 19)).fire


# ---------------------------------------------------------------------------
# recap -- the one that must never fire early
# ---------------------------------------------------------------------------


def test_recap_does_not_fire_while_a_game_is_being_played(sched):
    still_going = games(sunday_status="complete", monday="in_game")
    decision = decide(sched, "recap", still_going, at(MONDAY, 23))
    assert not decision.fire
    assert "still to finish" in decision.reason


def test_recap_does_not_fire_before_the_thursday_game_is_logged(sched):
    odd = games(sunday_status="complete", thursday="pre_game", monday="complete")
    assert not decide(sched, "recap", odd, at(MONDAY, 23)).fire


def test_recap_waits_for_scores_to_settle(sched):
    done = games(sunday_status="complete", monday="complete")
    # First sighting only records the time; Sleeper is still adjusting scores.
    first = decide(sched, "recap", done, at(MONDAY, 23, 30))
    assert not first.fire
    assert "settle" in first.reason

    # Still inside the settle window.
    assert not decide(sched, "recap", done, at(MONDAY, 23, 35)).fire


def test_recap_fires_after_the_settle_window(sched, monkeypatch):
    done = games(sunday_status="complete", monday="complete")
    decide(sched, "recap", done, at(MONDAY, 23, 30))  # arm the timer

    marker = f"complete_seen:{sched.view.season}:{sched.view.week}"
    old = int(sched.store.get_meta(marker)) - int(ffschedule.RECAP_SETTLE_SECONDS) - 60
    sched.store.set_meta(marker, str(old))

    assert decide(sched, "recap", done, at(MONDAY, 23, 59)).fire


def test_recap_fires_regardless_of_the_day(sched):
    """No weekday check at all -- that is the point of not using cron.

    A week that finishes on a Saturday recaps on Saturday.
    """
    done = [{"week": 5, "date": "2025-12-20", "home": "A", "away": "B", "status": "complete"}]
    decide(sched, "recap", done, at("2025-12-20", 20))
    marker = f"complete_seen:{sched.view.season}:{sched.view.week}"
    sched.store.set_meta(marker, "1")
    assert decide(sched, "recap", done, at("2025-12-20", 21)).fire


def test_a_cancelled_game_counts_as_finished(sched):
    # Otherwise a cancelled game blocks the recap for the rest of the season.
    with_cancel = games(sunday_status="complete", monday="canceled")
    assert week_is_complete(with_cancel)


def test_week_is_complete_needs_at_least_one_game(sched):
    assert not week_is_complete([])


# ---------------------------------------------------------------------------
# week ahead
# ---------------------------------------------------------------------------


def test_ahead_fires_on_wednesday_morning(sched):
    upcoming = games(sunday_status="pre_game", thursday="pre_game", monday="pre_game")
    assert decide(sched, "ahead", upcoming, at("2025-10-08", 9)).fire


def test_ahead_does_not_fire_on_tuesday(sched):
    upcoming = games(thursday="pre_game")
    assert not decide(sched, "ahead", upcoming, at("2025-10-07", 9)).fire


def test_ahead_does_not_fire_at_3am(sched):
    upcoming = games(thursday="pre_game")
    assert not decide(sched, "ahead", upcoming, at("2025-10-08", 3)).fire


def test_ahead_waits_for_the_week_to_roll_over(sched):
    # If /state/nfl still points at the finished week, "the week ahead" would
    # describe games that already happened.
    finished = games(sunday_status="complete", thursday="complete", monday="complete")
    assert not decide(sched, "ahead", finished, at("2025-10-08", 9)).fire


# ---------------------------------------------------------------------------
# tick: at most once per week, survives restarts
# ---------------------------------------------------------------------------


def _force(sched, kind):
    """Make `kind` the only enabled type and pretend its trigger is met."""
    sched.cfg.sleeper_post_types = (kind,)
    sched.scheduler.decide = lambda k, v, g, n: ffschedule.Decision(k, k == kind, "forced")


def test_tick_sends_and_records(sched):
    _force(sched, "recap")
    assert sched.scheduler.tick(at(MONDAY, 23)) == "recap"
    assert len(sched.bb.sent) == 1
    assert sched.store.already_posted(sched.view.season, sched.view.week, "recap")


def test_tick_does_not_repeat_within_the_week(sched):
    _force(sched, "recap")
    sched.scheduler.tick(at(MONDAY, 23))
    for _ in range(5):
        assert sched.scheduler.tick(at(MONDAY, 23)) is None
    assert len(sched.bb.sent) == 1


def test_a_restart_does_not_repost(sched, tmp_path):
    """The guarantee that keeps the bot from being muted."""
    _force(sched, "recap")
    sched.scheduler.tick(at(MONDAY, 23))
    assert len(sched.bb.sent) == 1

    # New process: fresh Store and Scheduler over the same database file.
    reborn_store = Store(tmp_path / "bot.db")
    reborn_bb = FakeBB()
    reborn = Scheduler(sched.cfg, reborn_bb, sched.sleeper, reborn_store)
    reborn.decide = lambda k, v, g, n: ffschedule.Decision(k, True, "forced")

    assert reborn.tick(at(MONDAY, 23)) is None
    assert reborn_bb.sent == []


def test_a_new_week_allows_the_post_again(sched):
    _force(sched, "recap")
    sched.scheduler.tick(at(MONDAY, 23))
    sched.store.mark_posted(sched.view.season, sched.view.week, "recap")

    # Week 5 is a different key, so the recap is due again.
    assert not sched.store.already_posted(sched.view.season, sched.view.week + 1, "recap")


def test_only_one_post_goes_out_per_tick(sched):
    # Spacing sends is what keeps four posts from landing at once, and is the
    # seam a rate limiter would use.
    sched.scheduler.decide = lambda k, v, g, n: ffschedule.Decision(k, True, "forced")
    first = sched.scheduler.tick(at(SUNDAY, 11))
    assert first is not None
    assert len(sched.bb.sent) == 1


def test_nothing_to_say_is_not_recorded_as_posted(sched):
    """A clean lineup at 10am may be a broken one at 11am."""
    _force(sched, "lineups")
    sched.scheduler.tick(at(SUNDAY, 11))
    # The fixture's live week (4) has no lineup problems, so nothing was sent.
    assert sched.bb.sent == []
    assert not sched.store.already_posted(sched.view.season, sched.view.week, "lineups")


def test_an_outage_does_not_burn_the_week(sched):
    """A failed build must not consume the one allowed post."""
    _force(sched, "recap")
    sched.sleeper._cache.clear()
    sched.session.fail = True

    assert sched.scheduler.tick(at(MONDAY, 23)) is None
    assert sched.bb.sent == []
    assert not sched.store.already_posted(sched.view.season, sched.view.week, "recap")

    sched.session.fail = False
    assert sched.scheduler.tick(at(MONDAY, 23)) == "recap"


def test_a_send_failure_still_counts_as_posted(sched):
    """Better one missed post than a retry loop hammering a locked Mac."""
    _force(sched, "recap")

    def boom(chat, message):
        raise RuntimeError("mac is asleep")

    sched.bb.send_text = boom
    sched.scheduler.tick(at(MONDAY, 23))
    assert sched.store.already_posted(sched.view.season, sched.view.week, "recap")


# ---------------------------------------------------------------------------
# switches
# ---------------------------------------------------------------------------


def test_the_master_switch_stops_everything(sched):
    sched.cfg.sleeper_posts_enabled = False
    assert sched.scheduler.tick(at(MONDAY, 23)) is None
    assert sched.bb.sent == []


def test_no_configured_chat_means_disabled(sched):
    sched.cfg.sleeper_post_chat = ""
    assert not sched.scheduler.enabled
    assert sched.scheduler.tick(at(MONDAY, 23)) is None


def test_a_disabled_post_type_is_skipped(sched):
    sched.cfg.sleeper_post_types = ("recap",)
    sched.scheduler.decide = lambda k, v, g, n: ffschedule.Decision(k, True, "forced")
    assert sched.scheduler.tick(at(SUNDAY, 11)) == "recap"  # not "lineups"


def test_dry_run_records_but_does_not_send(sched):
    sched.cfg.dry_run = True
    _force(sched, "recap")
    assert sched.scheduler.tick(at(MONDAY, 23)) == "recap"
    assert sched.bb.sent == []


def test_the_preseason_is_silent(sched):
    routes = build_routes()
    routes["/state/nfl"] = {"week": 1, "season": "2026", "season_type": "pre"}
    sched.session.routes = routes
    sched.sleeper._cache.clear()
    sched.scheduler.decide = lambda k, v, g, n: ffschedule.Decision(k, True, "forced")
    assert sched.scheduler.tick(at(SUNDAY, 11)) is None


def test_the_scheduler_never_posts_last_seasons_data_as_news(sched):
    """`!standings` may fall back to last season. A broadcast must not."""
    routes = build_routes()
    routes[f"/league/{LEAGUE_ID}"] = dict(
        routes[f"/league/{LEAGUE_ID}"], status="pre_draft"
    )
    routes[f"/league/{LEAGUE_ID}/rosters"] = [
        {"roster_id": i, "owner_id": f"u{i}", "players": [], "starters": ["0"],
         "settings": {"wins": 0, "losses": 0, "fpts": 0}}
        for i in range(1, 5)
    ]
    sched.session.routes = routes
    sched.sleeper._cache.clear()
    sched.scheduler.decide = lambda k, v, g, n: ffschedule.Decision(k, True, "forced")
    assert sched.scheduler.tick(at(SUNDAY, 11)) is None
    assert sched.bb.sent == []


def test_a_tick_never_raises(sched):
    """A scheduler thread that dies takes the whole week with it."""
    sched.sleeper._cache.clear()
    sched.session.fail = True
    assert sched.scheduler.tick(at(SUNDAY, 11)) is None


# ---------------------------------------------------------------------------
# timezone
# ---------------------------------------------------------------------------


def test_an_unknown_timezone_falls_back_instead_of_dying(caplog):
    tz = ffschedule.get_timezone("Mars/Olympus_Mons")
    assert tz is not None
    assert datetime.now(tz) is not None


def test_the_timezone_decides_which_day_it_is(sched):
    """10pm Tuesday on the west coast is already Wednesday in UTC.

    Deciding "is it Wednesday" in the wrong zone posts the week-ahead a day
    early for anyone west of London.
    """
    upcoming = games(thursday="pre_game")
    tuesday_night_pt = datetime(2025, 10, 7, 22, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert tuesday_night_pt.astimezone(ZoneInfo("UTC")).weekday() == 2  # Wednesday in UTC

    sched.scheduler.tz = ZoneInfo("America/Los_Angeles")
    assert not decide(sched, "ahead", upcoming, tuesday_night_pt).fire


def test_a_west_coast_league_is_warned_before_the_10am_kickoff(sched):
    """The 1pm ET slate starts at 10am Pacific."""
    pacific = datetime(2025, 10, 5, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    sched.scheduler.tz = ZoneInfo("America/Los_Angeles")
    assert decide(sched, "lineups", games(), pacific).fire


# ---------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------


def test_the_week_produces_at_most_four_posts(sched):
    """Four to six bot-initiated messages a week, not more."""
    sched.scheduler.decide = lambda k, v, g, n: ffschedule.Decision(k, True, "forced")
    sent = []
    when = at(SUNDAY, 11)
    for _ in range(50):  # a whole week of five-minute ticks, compressed
        kind = sched.scheduler.tick(when)
        if kind:
            sent.append(kind)
        when += timedelta(minutes=5)
    assert len(sent) <= len(ffposts.POST_ORDER)
    assert len(sent) == len(set(sent))  # no repeats
