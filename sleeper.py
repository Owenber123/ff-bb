"""Sleeper API client.

Read-only, unauthenticated, ~1000 requests/minute per IP. Base URL
``https://api.sleeper.app/v1``.

The whole point of this module is the cache. A single ``!scores`` during Sunday
games would otherwise hit /state/nfl, /league, /users, /rosters and /matchups
every invocation, times however many people are spamming it. So:

  * every fetch goes through :meth:`SleeperClient._cached`, keyed by URL path
  * each key has its own TTL and its own lock, so ten concurrent ``!scores``
    calls produce one HTTP request, not ten
  * **when a refresh fails we serve the last good value and mark it stale**
    rather than raising. Callers render the age ("scores as of 4 min ago")
    instead of silently lying or throwing a traceback into the group chat.
  * completed weeks never change, so their matchups are cached permanently and
    persisted to disk. Only the in-progress week has a short TTL.

Two things are not in the documented v1 API and are handled specially:

  ``/players/nfl``
      ~15MB of JSON, and Sleeper asks that you call it at most once per day.
      We trim it to the eight fields we actually use (that is 15MB -> ~1MB,
      and a great deal less resident memory on a small LXC), persist the
      trimmed copy, and refresh on a daily schedule in the background.

  ``https://api.sleeper.app/schedule/nfl/regular/<season>``  (note: no /v1)
      Undocumented, but ``/players/nfl`` carries no bye-week field, so it is
      the only way to answer ``!bye``. A team is on bye in week N if it does
      not appear as home or away in any non-cancelled week-N game. Everything
      that uses it degrades to "bye data unavailable" if it ever disappears.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from logging_setup import get_logger

log = get_logger(__name__)

API_BASE = "https://api.sleeper.app/v1"
# The schedule endpoint lives off the unversioned root. Not a typo.
ROOT_BASE = "https://api.sleeper.app"

# --- TTLs -------------------------------------------------------------------
# "Minutes, not seconds" for league metadata; ~30s for anything that moves
# while games are being played.
TTL_STATE = 3600.0  # /state/nfl -- the week ticks over once a week
TTL_LEAGUE = 600.0  # settings barely change mid-season
TTL_USERS = 600.0
TTL_ROSTERS = 300.0  # moves after waivers clear
TTL_MATCHUPS_LIVE = 30.0  # scores move
TTL_MATCHUPS_DONE = 86400.0 * 30  # a finished week is finished
TTL_TRANSACTIONS = 120.0
TTL_BRACKET = 900.0
TTL_SCHEDULE = 86400.0 * 3
# The scheduler reads game status from the same endpoint, where a stale answer
# means posting a "week recap" during the fourth quarter. ~30KB every 5 min.
TTL_SCHEDULE_LIVE = 300.0
TTL_USER_LOOKUP = 3600.0

# Game statuses on the schedule endpoint. Anything that is not pre_game has
# kicked off, which is the only distinction the scheduler actually needs -- so
# it stays correct even if Sleeper never emits "in_game".
GAME_NOT_STARTED = "pre_game"
GAME_FINISHED = frozenset({"complete", "canceled", "cancelled"})

PLAYERS_MAX_AGE = 86400.0  # Sleeper asks for at most one call per day
HTTP_TIMEOUT = 15.0
PLAYERS_TIMEOUT = 120.0  # 15MB over a home uplink

# Sleeper uses "0" as the placeholder for an unfilled starter slot. It is not a
# player ID, and a pre-draft roster is nothing but these.
EMPTY_SLOT = {"0", "", "none", "null"}


def is_empty_slot(player_id) -> bool:
    """True for a placeholder rather than a real player.

    Lowercased so a literal ``None`` in a list -- which stringifies to "None",
    not "none" -- is caught as well.
    """
    return str(player_id).lower() in EMPTY_SLOT

# The fields we keep from /players/nfl. Everything else is dropped on ingest.
PLAYER_FIELDS = (
    "first_name",
    "last_name",
    "full_name",
    "position",
    "team",
    "injury_status",
    "injury_body_part",
    "status",
    "years_exp",
    "number",
)


class SleeperError(RuntimeError):
    """Sleeper is unreachable, slow, or returned something unusable.

    Commands catch this and say so in plain words. It never reaches dispatch's
    generic handler, so the chat never sees "blew up".
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class LeagueNotFound(SleeperError):
    """The league ID does not exist.

    Sleeper signals this two different ways -- 404, or 200 with a JSON null --
    and both mean "check SLEEPER_LEAGUE_ID", not "Sleeper is down".
    """


class SleeperNotConfigured(SleeperError):
    """SLEEPER_LEAGUE_ID is unset. A setup problem, not an outage."""


# ---------------------------------------------------------------------------
# cache primitives
# ---------------------------------------------------------------------------


@dataclass
class Cached:
    """A cache entry, plus enough provenance to caveat a reply honestly."""

    value: Any
    fetched_at: float
    stale: bool = False  # last refresh attempt failed; this is the old value
    error: str = ""

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.fetched_at)


def describe_age(seconds: float) -> str:
    """'just now' / '4 min ago' / '2 hr ago' -- for the freshness caveat."""
    seconds = max(0.0, seconds)
    if seconds < 45:
        return "just now"
    if seconds < 5400:
        minutes = int(round(seconds / 60.0))
        return f"{minutes} min ago"
    hours = seconds / 3600.0
    if hours < 36:
        return f"{int(round(hours))} hr ago"
    return f"{int(round(hours / 24.0))} d ago"


class _KeyedLocks:
    """One lock per cache key, so a burst of !scores is a single fetch."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class SleeperClient:
    def __init__(
        self,
        data_dir: str | Path,
        timeout: float = HTTP_TIMEOUT,
        session: requests.Session | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", "bluebubbles-bot/1.0 (+fantasy)")

        self._cache: dict[str, Cached] = {}
        self._cache_guard = threading.Lock()
        self._locks = _KeyedLocks()

        self._players: dict[str, dict] | None = None
        self._players_loaded_at = 0.0
        self._players_lock = threading.Lock()
        self._players_refreshing = False

        self.request_count = 0

        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Not fatal: everything still works from the in-memory cache, we
            # just lose persistence across restarts.
            log.warning(
                "cannot create the Sleeper data directory; disk cache disabled",
                extra={"fields": {"dir": str(self.data_dir), "error": str(exc)}},
            )

    # -- plumbing ------------------------------------------------------------

    def _get_json(self, url: str, timeout: float | None = None) -> Any:
        self.request_count += 1
        try:
            resp = self._session.get(url, timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            raise SleeperError(f"could not reach Sleeper: {exc}") from exc

        if resp.status_code == 404:
            raise SleeperError(f"Sleeper has no data at {url.rsplit('/v1/', 1)[-1]}", status=404)
        if resp.status_code == 429:
            raise SleeperError("Sleeper is rate limiting us; try again shortly", status=429)
        if resp.status_code >= 400:
            raise SleeperError(f"Sleeper returned HTTP {resp.status_code}", status=resp.status_code)

        try:
            return resp.json()
        except ValueError as exc:
            raise SleeperError("Sleeper returned a body that is not JSON") from exc

    def _peek(self, key: str) -> Cached | None:
        with self._cache_guard:
            return self._cache.get(key)

    def _store(self, key: str, entry: Cached) -> None:
        with self._cache_guard:
            self._cache[key] = entry

    def _cached(self, key: str, ttl: float, fetch: Callable[[], Any]) -> Cached:
        """Fetch through the cache, serving stale data rather than failing.

        Returns the entry rather than the bare value so callers can render the
        freshness caveat. Raises only when there is no usable value at all.
        """
        entry = self._peek(key)
        if entry is not None and not entry.stale and entry.age < ttl:
            return entry

        lock = self._locks.get(key)
        with lock:
            # Someone else may have refreshed it while we waited on the lock.
            entry = self._peek(key)
            if entry is not None and not entry.stale and entry.age < ttl:
                return entry

            try:
                value = fetch()
            except SleeperError as exc:
                if entry is not None:
                    stale = Cached(
                        value=entry.value,
                        fetched_at=entry.fetched_at,
                        stale=True,
                        error=str(exc),
                    )
                    self._store(key, stale)
                    log.warning(
                        "serving stale Sleeper data",
                        extra={"fields": {"key": key, "age_s": round(entry.age), "error": str(exc)}},
                    )
                    return stale
                raise

            fresh = Cached(value=value, fetched_at=time.time())
            self._store(key, fresh)
            return fresh

    def _api(self, path: str, ttl: float) -> Cached:
        return self._cached(path, ttl, lambda: self._get_json(f"{API_BASE}/{path.lstrip('/')}"))

    # -- disk ----------------------------------------------------------------

    def _disk_path(self, name: str) -> Path:
        return self.data_dir / name

    def _read_disk(self, name: str) -> tuple[Any, float] | None:
        path = self._disk_path(name)
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh), path.stat().st_mtime
        except (OSError, ValueError):
            return None

    def _write_disk(self, name: str, value: Any) -> None:
        """Atomic replace: a truncated cache file on a crash is worse than none."""
        path = self._disk_path(name)
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.data_dir), prefix=f".{name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(value, fh, separators=(",", ":"))
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.warning(
                "could not persist a Sleeper cache file",
                extra={"fields": {"file": name, "error": str(exc)}},
            )

    # -- endpoints -----------------------------------------------------------

    def state(self) -> Cached:
        """GET /state/nfl. Call this first; everything else needs the week."""
        return self._api("state/nfl", TTL_STATE)

    def league(self, league_id: str) -> Cached:
        try:
            entry = self._api(f"league/{league_id}", TTL_LEAGUE)
        except SleeperError as exc:
            if exc.status == 404:
                raise LeagueNotFound(f"There is no Sleeper league with ID {league_id}") from exc
            raise
        if not entry.value:
            # The other shape: 200 with a JSON null body.
            raise LeagueNotFound(f"There is no Sleeper league with ID {league_id}")
        return entry

    def users(self, league_id: str) -> Cached:
        return self._api(f"league/{league_id}/users", TTL_USERS)

    def rosters(self, league_id: str) -> Cached:
        return self._api(f"league/{league_id}/rosters", TTL_ROSTERS)

    def matchups(self, league_id: str, week: int, *, final: bool = False) -> Cached:
        """GET /league/<id>/matchups/<week>.

        ``final=True`` marks the week as complete, which makes the entry cache
        effectively forever and persist to disk. Only ever pass it for weeks
        strictly before the current one.
        """
        key = f"league/{league_id}/matchups/{week}"
        if final:
            entry = self._peek(key)
            if entry is not None and not entry.stale:
                return entry
            disk = self._read_disk(f"matchups_{league_id}_{week}.json")
            if disk is not None:
                entry = Cached(value=disk[0], fetched_at=disk[1])
                self._store(key, entry)
                return entry

        entry = self._api(key, TTL_MATCHUPS_DONE if final else TTL_MATCHUPS_LIVE)
        if final and entry.value and not entry.stale:
            self._write_disk(f"matchups_{league_id}_{week}.json", entry.value)
        return entry

    def transactions(self, league_id: str, week: int) -> Cached:
        return self._api(f"league/{league_id}/transactions/{week}", TTL_TRANSACTIONS)

    def winners_bracket(self, league_id: str) -> Cached:
        return self._api(f"league/{league_id}/winners_bracket", TTL_BRACKET)

    def losers_bracket(self, league_id: str) -> Cached:
        return self._api(f"league/{league_id}/losers_bracket", TTL_BRACKET)

    def traded_picks(self, league_id: str) -> Cached:
        return self._api(f"league/{league_id}/traded_picks", TTL_BRACKET)

    # -- user lookup (for the setup helper) ----------------------------------

    def user(self, username: str) -> dict | None:
        """GET /user/<username>. Returns None for an unknown user.

        Sleeper answers 200 with a JSON null rather than 404, so the null check
        is the only way to detect a typo'd username.
        """
        entry = self._cached(
            f"user/{username.lower()}",
            TTL_USER_LOOKUP,
            lambda: self._get_json(f"{API_BASE}/user/{requests.utils.quote(username)}"),
        )
        return entry.value if isinstance(entry.value, dict) else None

    def user_leagues(self, user_id: str, season: str) -> list[dict]:
        entry = self._cached(
            f"user/{user_id}/leagues/nfl/{season}",
            TTL_USER_LOOKUP,
            lambda: self._get_json(f"{API_BASE}/user/{user_id}/leagues/nfl/{season}"),
        )
        return entry.value if isinstance(entry.value, list) else []

    # -- schedule / byes -----------------------------------------------------

    def schedule(self, season: str, max_age: float = TTL_SCHEDULE) -> list[dict]:
        """Full regular-season NFL schedule. Undocumented endpoint; see module docstring.

        Each game carries ``date`` (no kickoff time) and a live ``status``
        (``pre_game`` / ``in_game`` / ``complete`` / ``canceled``).

        ``max_age`` exists because the two consumers want opposite things.
        ``!bye`` reads the fixture list, which does not change, so it takes the
        3-day default and usually answers from disk. The scheduler reads
        ``status`` to decide whether the last game has finished, and a 3-day-old
        answer to that is useless -- it passes :data:`TTL_SCHEDULE_LIVE`.
        """
        name = f"schedule_{season}.json"
        key = f"schedule/{season}"

        entry = self._peek(key)
        if entry is not None and not entry.stale and entry.age < max_age:
            return entry.value or []

        disk = self._read_disk(name)
        if disk is not None and (time.time() - disk[1]) < max_age:
            self._store(key, Cached(value=disk[0], fetched_at=disk[1]))
            return disk[0] or []

        try:
            entry = self._cached(
                key,
                max_age,
                lambda: self._get_json(f"{ROOT_BASE}/schedule/nfl/regular/{season}"),
            )
        except SleeperError:
            if disk is not None:
                return disk[0] or []
            raise

        games = entry.value if isinstance(entry.value, list) else []
        if games and not entry.stale:
            self._write_disk(name, games)
        return games

    def week_games(self, season: str, week: int, max_age: float = TTL_SCHEDULE_LIVE) -> list[dict]:
        """This week's games with fresh statuses. Empty list if unavailable."""
        try:
            games = self.schedule(season, max_age=max_age)
        except SleeperError:
            return []
        return [g for g in games if isinstance(g, dict) and g.get("week") == week]

    def teams_on_bye(self, season: str, week: int) -> set[str]:
        """Teams absent from week ``week``. Empty set if the schedule is unavailable."""
        try:
            games = self.schedule(season)
        except SleeperError:
            return set()
        if not games:
            return set()

        everyone: set[str] = set()
        playing: set[str] = set()
        for game in games:
            home, away = game.get("home"), game.get("away")
            if home:
                everyone.add(home)
            if away:
                everyone.add(away)
            if game.get("week") != week:
                continue
            if str(game.get("status") or "").lower() in ("canceled", "cancelled"):
                continue
            if home:
                playing.add(home)
            if away:
                playing.add(away)

        if not playing:
            # Week outside the regular season, or a schedule we cannot read.
            return set()
        return everyone - playing

    # -- players -------------------------------------------------------------

    def _trim_players(self, raw: Any) -> dict[str, dict]:
        if not isinstance(raw, dict):
            raise SleeperError("the Sleeper player file was not a JSON object")
        trimmed: dict[str, dict] = {}
        for pid, player in raw.items():
            if not isinstance(player, dict):
                continue
            trimmed[str(pid)] = {k: player.get(k) for k in PLAYER_FIELDS if player.get(k) is not None}
        if not trimmed:
            raise SleeperError("the Sleeper player file was empty")
        return trimmed

    def _fetch_players(self) -> dict[str, dict]:
        log.info("downloading the Sleeper player file (~15MB, once a day)")
        raw = self._get_json(f"{API_BASE}/players/nfl", timeout=PLAYERS_TIMEOUT)
        trimmed = self._trim_players(raw)
        self._write_disk("players_nfl.json", trimmed)
        log.info("player file refreshed", extra={"fields": {"players": len(trimmed)}})
        return trimmed

    def players(self, *, allow_fetch: bool = True) -> dict[str, dict]:
        """The player map, keyed by Sleeper player ID.

        Disk first, so a restart is instant. If the disk copy is older than a
        day we return it immediately and refresh in the background -- a stale
        player name is nothing, a 15MB blocking download in a command handler
        is a dead chat.
        """
        with self._players_lock:
            if self._players is not None:
                if time.time() - self._players_loaded_at > PLAYERS_MAX_AGE:
                    self._refresh_players_async()
                return self._players

            disk = self._read_disk("players_nfl.json")
            if disk is not None and isinstance(disk[0], dict) and disk[0]:
                self._players = disk[0]
                self._players_loaded_at = disk[1]
                log.info(
                    "loaded the player file from disk",
                    extra={"fields": {"players": len(self._players), "age_s": round(time.time() - disk[1])}},
                )
                if time.time() - disk[1] > PLAYERS_MAX_AGE:
                    self._refresh_players_async()
                return self._players

            if not allow_fetch:
                # Nothing in memory and nothing on disk. Start the download in
                # the background so the *next* command has names, and let this
                # one degrade to raw player IDs rather than blocking a chat
                # reply on 15MB. Without this a cold container with no disk
                # cache would show IDs forever, since no caller ever blocks.
                self._refresh_players_async()
                return {}

            # Cold start with no disk copy: this one has to block.
            self._players = self._fetch_players()
            self._players_loaded_at = time.time()
            return self._players

    def _refresh_players_async(self) -> None:
        """Caller must hold ``_players_lock``."""
        if self._players_refreshing:
            return
        self._players_refreshing = True

        def worker() -> None:
            fresh = None
            try:
                fresh = self._fetch_players()
            except SleeperError as exc:
                log.warning(
                    "player refresh failed; keeping whatever we had",
                    extra={"fields": {"error": str(exc)}},
                )
            # Publish and clear the in-flight flag under one acquisition, so a
            # concurrent caller cannot see "not refreshing, not loaded" and
            # start a second download.
            with self._players_lock:
                if fresh:
                    self._players = fresh
                    self._players_loaded_at = time.time()
                self._players_refreshing = False

        threading.Thread(target=worker, name="sleeper-players", daemon=True).start()

    def refresh_players_now(self) -> int:
        """Force a refresh. Used by ``bot.py sleeper refresh-players`` from cron."""
        fresh = self._fetch_players()
        with self._players_lock:
            self._players = fresh
            self._players_loaded_at = time.time()
        return len(fresh)

    # -- convenience ---------------------------------------------------------

    def player_name(self, player_id: str, *, short: bool = False) -> str:
        """Resolve a Sleeper player ID to a display name.

        Handles the two shapes in ``starters[]``: numeric player IDs, and team
        abbreviations for defenses ("PHI"). Falls back to the raw ID rather
        than failing, so a cold player cache degrades instead of breaking.
        """
        pid = str(player_id or "")
        if is_empty_slot(pid):
            return "(empty)"
        player = self.players(allow_fetch=False).get(pid)
        if not player:
            return pid

        if player.get("position") == "DEF":
            return f"{pid} D/ST" if short else f"{player.get('last_name') or pid} D/ST"

        full = player.get("full_name")
        if not full:
            full = " ".join(x for x in (player.get("first_name"), player.get("last_name")) if x)
        if not full:
            return pid
        if short:
            parts = full.split()
            if len(parts) >= 2:
                return f"{parts[0][0]}. {' '.join(parts[1:])}"
        return full

    def player_meta(self, player_id: str) -> dict:
        return self.players(allow_fetch=False).get(str(player_id or "")) or {}

    def prewarm(self, league_id: str, weeks: Iterable[int] = ()) -> None:
        """Warm the caches in the background at service start.

        Without this the first ``!standings`` of the day pays for the player
        file and a season's worth of matchups while somebody watches a chat
        bubble do nothing.
        """

        def worker() -> None:
            try:
                self.players()
            except SleeperError as exc:
                log.warning("prewarm: player file", extra={"fields": {"error": str(exc)}})
            for week in weeks:
                try:
                    self.matchups(league_id, week, final=True)
                except SleeperError as exc:
                    log.debug("prewarm: matchups", extra={"fields": {"week": week, "error": str(exc)}})
            log.info(
                "sleeper prewarm complete",
                extra={"fields": {"requests": self.request_count, "league": league_id}},
            )

        threading.Thread(target=worker, name="sleeper-prewarm", daemon=True).start()


# ---------------------------------------------------------------------------
# module-level singleton, so every command shares one cache
# ---------------------------------------------------------------------------

_CLIENT: SleeperClient | None = None
_CLIENT_GUARD = threading.Lock()


def get_client(data_dir: str | Path | None = None) -> SleeperClient:
    global _CLIENT
    with _CLIENT_GUARD:
        if _CLIENT is None:
            if data_dir is None:
                data_dir = Path(__file__).resolve().parent / "data"
            _CLIENT = SleeperClient(data_dir)
        return _CLIENT


def set_client(client: SleeperClient | None) -> None:
    """Test seam."""
    global _CLIENT
    with _CLIENT_GUARD:
        _CLIENT = client
