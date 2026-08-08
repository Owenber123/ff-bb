"""The league model: joins, name resolution, and derived stats.

The join that everything needs, done once per refresh and passed around:

    matchups.roster_id -> rosters.roster_id -> rosters.owner_id -> users.user_id

:class:`LeagueView` is the result. Build it with :func:`load_view` and hand it
to anything that needs league data; it holds the joined managers, the week, and
the cache provenance so a reply can caveat its own freshness.

Two behaviours worth knowing about before reading further:

**Season rollover.** A Sleeper league is a per-season object. In August your
configured league is ``pre_draft`` with twelve empty rosters and zero matchups,
and ``previous_league_id`` points at the season that actually has data. Rather
than have every command answer "nothing yet" for four months, :func:`load_view`
falls back to the previous league and sets :attr:`LeagueView.is_previous_season`.
Commands surface that in the reply -- the data is real, it is just last year's.

**fpts_decimal is hundredths.** ``{"fpts": 1802, "fpts_decimal": 8}`` is
1802.08, not 1802.8. Verified by summing weeks 1-14 of a finished league
against the roster totals. The naive string join is wrong by 0.72 points, and
wrong in a way nobody notices until an argument about it.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ffformat import normalize, normalize_handle
from logging_setup import get_logger
from sleeper import (
    SleeperClient,
    SleeperError,
    SleeperNotConfigured,
    is_empty_slot,
)

log = get_logger(__name__)

# Which positions may fill which lineup slot. These eligibility sets are nested
# (QB c SUPER_FLEX, RB c FLEX c SUPER_FLEX, ...), i.e. a laminar family, which
# is what makes the greedy in optimal_lineup() actually optimal.
SLOT_ELIGIBILITY: dict[str, frozenset[str]] = {
    "QB": frozenset({"QB"}),
    "RB": frozenset({"RB"}),
    "WR": frozenset({"WR"}),
    "TE": frozenset({"TE"}),
    "K": frozenset({"K"}),
    "DEF": frozenset({"DEF"}),
    "DL": frozenset({"DL", "DE", "DT"}),
    "LB": frozenset({"LB"}),
    "DB": frozenset({"DB", "CB", "S"}),
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "WRRB_FLEX": frozenset({"RB", "WR"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "IDP_FLEX": frozenset({"DL", "DE", "DT", "LB", "DB", "CB", "S"}),
}
BENCH_SLOTS = frozenset({"BN", "IR", "TAXI"})

# injury_status values worth reporting. "Questionable" is noise by Thursday and
# gospel by Sunday, so it is included but ranked last.
INJURY_ORDER = {"IR": 0, "PUP": 1, "Sus": 2, "NA": 3, "Out": 4, "Doubtful": 5, "Questionable": 6}


class NameNotFound(SleeperError):
    """The name in a command did not match anyone in the league."""


class NameAmbiguous(SleeperError):
    def __init__(self, query: str, options: Sequence[str]):
        super().__init__(f"{query!r} matches several people")
        self.query = query
        self.options = list(options)


def points_from_settings(settings: dict, key: str = "fpts") -> float:
    """Reassemble a Sleeper points pair. ``fpts_decimal`` is HUNDREDTHS."""
    whole = settings.get(key) or 0
    decimal = settings.get(f"{key}_decimal") or 0
    try:
        return float(whole) + float(decimal) / 100.0
    except (TypeError, ValueError):
        return 0.0


def real_players(ids: Iterable[str] | None) -> list[str]:
    """Drop Sleeper's "0" placeholders for unfilled slots.

    Lowercased before the check so a literal ``None`` in the list -- which
    stringifies to "None", not "none" -- is filtered too.
    """
    return [str(p) for p in (ids or []) if not is_empty_slot(p)]


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


@dataclass
class Manager:
    roster_id: int
    user_id: str
    display_name: str
    team_name: str = ""
    nickname: str = ""
    wins: int = 0
    losses: int = 0
    ties: int = 0
    fpts: float = 0.0
    fpts_against: float = 0.0
    ppts: float = 0.0  # season-long maximum possible, per Sleeper
    players: list[str] = field(default_factory=list)
    starters: list[str] = field(default_factory=list)
    waiver_budget_used: int = 0
    total_moves: int = 0

    @property
    def name(self) -> str:
        """What the bot calls this person. A nickname wins everywhere, forever."""
        return self.nickname or self.display_name or self.team_name or f"Roster {self.roster_id}"

    @property
    def formal_name(self) -> str:
        return self.display_name or self.team_name or f"Roster {self.roster_id}"

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def win_pct(self) -> float:
        return (self.wins + 0.5 * self.ties) / self.games if self.games else 0.0

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}" + (f"-{self.ties}" if self.ties else "")

    def aliases(self) -> list[str]:
        return [a for a in (self.nickname, self.display_name, self.team_name) if a]


@dataclass
class Side:
    """One row of a matchup, joined to its manager."""

    roster_id: int
    manager: Manager | None
    points: float
    starters: list[str] = field(default_factory=list)
    starters_points: list[float] = field(default_factory=list)
    players_points: dict[str, float] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.manager.name if self.manager else f"Roster {self.roster_id}"

    def started(self) -> list[tuple[str, float]]:
        """(player_id, points) for filled starter slots, in lineup order."""
        out = []
        for i, pid in enumerate(self.starters):
            if is_empty_slot(pid):
                continue
            score = self.starters_points[i] if i < len(self.starters_points) else 0.0
            out.append((str(pid), float(score or 0.0)))
        return out

    def bench(self) -> list[tuple[str, float]]:
        starting = {str(p) for p in self.starters}
        return [
            (pid, float(score or 0.0))
            for pid, score in (self.players_points or {}).items()
            if str(pid) not in starting and not is_empty_slot(pid)
        ]


@dataclass
class Matchup:
    matchup_id: int
    sides: list[Side]

    @property
    def complete(self) -> bool:
        return len(self.sides) == 2

    @property
    def margin(self) -> float:
        if not self.complete:
            return 0.0
        return abs(self.sides[0].points - self.sides[1].points)

    @property
    def total(self) -> float:
        return sum(s.points for s in self.sides)

    def ordered(self) -> list[Side]:
        return sorted(self.sides, key=lambda s: s.points, reverse=True)

    @property
    def winner(self) -> Side | None:
        if not self.complete or self.margin == 0:
            return None
        return self.ordered()[0]

    @property
    def loser(self) -> Side | None:
        if not self.complete or self.margin == 0:
            return None
        return self.ordered()[1]


@dataclass
class WeekResult:
    week: int
    roster_id: int
    points: float
    opponent_id: int | None
    opponent_points: float

    @property
    def outcome(self) -> str:
        if self.opponent_id is None:
            return "-"
        if self.points > self.opponent_points:
            return "W"
        if self.points < self.opponent_points:
            return "L"
        return "T"

    @property
    def margin(self) -> float:
        return self.points - self.opponent_points


@dataclass
class LeagueView:
    client: SleeperClient
    league_id: str
    league: dict
    managers: dict[int, Manager]  # by roster_id
    week: int
    season: str
    season_type: str
    age: float = 0.0
    stale: bool = False
    is_previous_season: bool = False
    configured_league_id: str = ""

    # -- shape ---------------------------------------------------------------

    @property
    def name(self) -> str:
        return str(self.league.get("name") or "the league")

    @property
    def status(self) -> str:
        return str(self.league.get("status") or "")

    @property
    def settings(self) -> dict:
        return self.league.get("settings") or {}

    @property
    def roster_positions(self) -> list[str]:
        return [str(p) for p in (self.league.get("roster_positions") or [])]

    @property
    def starting_slots(self) -> list[str]:
        return [p for p in self.roster_positions if p not in BENCH_SLOTS]

    @property
    def playoff_week_start(self) -> int:
        try:
            return int(self.settings.get("playoff_week_start") or 15)
        except (TypeError, ValueError):
            return 15

    @property
    def playoff_teams(self) -> int:
        try:
            return int(self.settings.get("playoff_teams") or 6)
        except (TypeError, ValueError):
            return 6

    @property
    def in_playoffs(self) -> bool:
        return self.week >= self.playoff_week_start

    @property
    def has_games(self) -> bool:
        """False for a pre-draft league, where every command has nothing to say."""
        return any(m.games for m in self.managers.values()) or any(
            m.players for m in self.managers.values()
        )

    def by_user(self, user_id: str) -> Manager | None:
        for manager in self.managers.values():
            if manager.user_id == user_id:
                return manager
        return None

    def sorted_managers(self) -> list[Manager]:
        """League standings order: win pct, then points for."""
        return sorted(
            self.managers.values(),
            key=lambda m: (m.win_pct, m.fpts),
            reverse=True,
        )

    def rank_of(self, manager: Manager) -> int:
        for i, other in enumerate(self.sorted_managers(), start=1):
            if other.roster_id == manager.roster_id:
                return i
        return 0

    # -- weeks ---------------------------------------------------------------

    def regular_weeks(self) -> list[int]:
        """Regular-season weeks with final scores. Drives records and luck."""
        last = min(self.last_scored_week(), self.playoff_week_start - 1)
        return list(range(1, max(0, last) + 1))

    def last_scored_week(self) -> int:
        settings = self.settings
        for key in ("last_scored_leg", "leg"):
            try:
                value = int(settings.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value:
                return value
        return self.week

    def is_week_final(self, week: int) -> bool:
        """A finished week never changes, so it can be cached hard."""
        if self.is_previous_season or self.status == "complete":
            return True
        return week < self.week

    # -- data ----------------------------------------------------------------

    def matchups(self, week: int | None = None) -> tuple[list[Matchup], float, bool]:
        """This week's pairings, joined to managers.

        Returns (matchups, cache age, stale). Rows sharing a ``matchup_id``
        are the pairing. In playoff weeks ``matchup_id`` is null for teams with
        no game -- those rows are dropped rather than paired with each other.
        """
        week = self.week if week is None else week
        entry = self.client.matchups(self.league_id, week, final=self.is_week_final(week))
        rows = entry.value if isinstance(entry.value, list) else []

        grouped: dict[int, list[Side]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            matchup_id = row.get("matchup_id")
            if matchup_id is None:
                continue  # bye / eliminated in a playoff week
            roster_id = row.get("roster_id")
            if roster_id is None:
                continue
            side = Side(
                roster_id=int(roster_id),
                manager=self.managers.get(int(roster_id)),
                points=float(row.get("points") or 0.0),
                starters=[str(p) for p in (row.get("starters") or [])],
                starters_points=[float(p or 0.0) for p in (row.get("starters_points") or [])],
                players_points={str(k): float(v or 0.0) for k, v in (row.get("players_points") or {}).items()},
            )
            grouped.setdefault(int(matchup_id), []).append(side)

        matchups = [Matchup(matchup_id=mid, sides=sides) for mid, sides in sorted(grouped.items())]
        return matchups, entry.age, entry.stale

    def season_results(self, weeks: Sequence[int] | None = None) -> dict[int, list[WeekResult]]:
        """Every roster's week-by-week results.

        This is the one primitive behind streaks, head-to-head, luck and the
        season-wide superlatives. Completed weeks are cached permanently and
        persisted, so the cost is a one-time warm-up (prewarmed at startup),
        not per command.
        """
        weeks = list(weeks) if weeks is not None else self.regular_weeks()
        results: dict[int, list[WeekResult]] = {rid: [] for rid in self.managers}

        for week in weeks:
            try:
                matchups, _, _ = self.matchups(week)
            except SleeperError as exc:
                log.warning(
                    "skipping a week while building season results",
                    extra={"fields": {"week": week, "error": str(exc)}},
                )
                continue
            for matchup in matchups:
                if not matchup.complete:
                    continue
                a, b = matchup.sides
                for me, them in ((a, b), (b, a)):
                    results.setdefault(me.roster_id, []).append(
                        WeekResult(
                            week=week,
                            roster_id=me.roster_id,
                            points=me.points,
                            opponent_id=them.roster_id,
                            opponent_points=them.points,
                        )
                    )
        return results

    # -- name resolution -----------------------------------------------------

    def resolve(self, query: str, *, caller_handle: str = "", links: dict[str, str] | None = None) -> Manager:
        """Turn whatever somebody typed into a Manager.

        Matches, in order: the caller themselves, nickname, Sleeper display
        name, team name. Exact, then prefix, then substring, then a close
        match. Raises :class:`NameNotFound` or :class:`NameAmbiguous`, both of
        which commands turn into a helpful one-liner rather than a traceback.
        """
        wanted = (query or "").strip()
        if not wanted or wanted.lower() in ("me", "myself", "self"):
            manager = self.caller(caller_handle, links or {})
            if manager is None:
                raise NameNotFound(
                    "I do not know which Sleeper account is yours. "
                    "Use !link <sleeper username>, or set SLEEPER_USER_MAP."
                )
            return manager

        needle = normalize(wanted)
        if not needle:
            raise NameNotFound(f"{wanted!r} is not a name I can look up")

        candidates: list[tuple[Manager, str]] = []
        for manager in self.managers.values():
            for alias in manager.aliases():
                candidates.append((manager, normalize(alias)))

        for test in (
            lambda a: a == needle,
            lambda a: a.startswith(needle),
            lambda a: needle in a,
        ):
            hits = {m.roster_id: m for m, alias in candidates if alias and test(alias)}
            if len(hits) == 1:
                return next(iter(hits.values()))
            if len(hits) > 1:
                raise NameAmbiguous(wanted, sorted(m.name for m in hits.values()))

        aliases = [alias for _, alias in candidates if alias]
        close = difflib.get_close_matches(needle, aliases, n=1, cutoff=0.75)
        if close:
            for manager, alias in candidates:
                if alias == close[0]:
                    return manager

        raise NameNotFound(f"Nobody in {self.name} matches {wanted!r}")

    def caller(self, handle: str, links: dict[str, str]) -> Manager | None:
        """Who sent the command, if their chat handle is mapped to Sleeper.

        The mapped value may be a Sleeper user_id or a display name. Accepting
        both means SLEEPER_USER_MAP can be written with the names people
        actually know, and still costs no API call -- display names are already
        in the league users response.
        """
        key = normalize_handle(handle)
        if not key:
            return None
        identifier = links.get(key)
        if not identifier:
            return None

        direct = self.by_user(identifier)
        if direct is not None:
            return direct

        needle = normalize(identifier)
        for manager in self.managers.values():
            if any(normalize(alias) == needle for alias in manager.aliases()):
                return manager
        return None


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def resolve_league_id(client: SleeperClient, league_id: str, *, follow_previous: bool = True) -> tuple[dict, str, bool]:
    """Pick the league that actually has data.

    Between seasons the configured league is ``pre_draft`` with empty rosters,
    so we walk ``previous_league_id`` once. Returns (league, league_id,
    is_previous_season).
    """
    league = client.league(league_id).value
    if not follow_previous:
        return league, league_id, False

    status = str(league.get("status") or "")
    if status not in ("pre_draft", "drafting"):
        return league, league_id, False

    previous = league.get("previous_league_id")
    if not previous or str(previous) in ("0", ""):
        return league, league_id, False

    try:
        older = client.league(str(previous)).value
    except SleeperError as exc:
        log.info(
            "could not load previous_league_id; staying on the configured league",
            extra={"fields": {"previous": previous, "error": str(exc)}},
        )
        return league, league_id, False

    log.info(
        "configured league has not drafted yet; showing the previous season",
        extra={"fields": {"configured": league_id, "using": previous, "season": older.get("season")}},
    )
    return older, str(previous), True


def load_view(
    client: SleeperClient,
    league_id: str,
    *,
    nicknames: dict[str, str] | None = None,
    follow_previous: bool = True,
) -> LeagueView:
    """Build the joined view. Two to four cached API calls."""
    if not league_id:
        raise SleeperNotConfigured(
            "No Sleeper league is configured. Set SLEEPER_LEAGUE_ID in .env — "
            "find it with: python bot.py sleeper leagues <your sleeper username>"
        )

    league, effective_id, is_previous = resolve_league_id(
        client, league_id, follow_previous=follow_previous
    )

    users_entry = client.users(effective_id)
    rosters_entry = client.rosters(effective_id)

    users = {
        str(u.get("user_id")): u
        for u in (users_entry.value or [])
        if isinstance(u, dict) and u.get("user_id")
    }
    nicknames = nicknames or {}

    managers: dict[int, Manager] = {}
    for roster in rosters_entry.value or []:
        if not isinstance(roster, dict) or roster.get("roster_id") is None:
            continue
        roster_id = int(roster["roster_id"])
        owner_id = str(roster.get("owner_id") or "")
        user = users.get(owner_id, {})
        metadata = user.get("metadata") or {}
        settings = roster.get("settings") or {}

        managers[roster_id] = Manager(
            roster_id=roster_id,
            user_id=owner_id,
            display_name=str(user.get("display_name") or ""),
            team_name=str(metadata.get("team_name") or ""),
            nickname=nicknames.get(owner_id, ""),
            wins=int(settings.get("wins") or 0),
            losses=int(settings.get("losses") or 0),
            ties=int(settings.get("ties") or 0),
            fpts=points_from_settings(settings, "fpts"),
            fpts_against=points_from_settings(settings, "fpts_against"),
            ppts=points_from_settings(settings, "ppts"),
            players=real_players(roster.get("players")),
            starters=real_players(roster.get("starters")),
            waiver_budget_used=int(settings.get("waiver_budget_used") or 0),
            total_moves=int(settings.get("total_moves") or 0),
        )

    # The week to display. A finished league shows its final week; a live one
    # follows /state/nfl, clamped into the league's own season.
    season = str(league.get("season") or "")
    season_type = "regular"
    week = 1
    try:
        state = client.state().value or {}
        season_type = str(state.get("season_type") or "regular")
        state_week = int(state.get("week") or 1)
    except SleeperError:
        state_week = 1

    settings = league.get("settings") or {}
    last_scored = 0
    for key in ("last_scored_leg", "leg"):
        try:
            last_scored = int(settings.get(key) or 0)
        except (TypeError, ValueError):
            last_scored = 0
        if last_scored:
            break

    if is_previous or str(league.get("status")) == "complete":
        week = last_scored or state_week
    elif season_type == "pre":
        week = 1
    else:
        week = max(1, state_week)

    age = max(users_entry.age, rosters_entry.age)
    stale = users_entry.stale or rosters_entry.stale

    return LeagueView(
        client=client,
        league_id=effective_id,
        league=league,
        managers=managers,
        week=week,
        season=season,
        season_type=season_type,
        age=age,
        stale=stale,
        is_previous_season=is_previous,
        configured_league_id=league_id,
    )


# ---------------------------------------------------------------------------
# derived stats
# ---------------------------------------------------------------------------


def optimal_lineup(
    slots: Sequence[str],
    scores: dict[str, float],
    positions: dict[str, str],
) -> tuple[float, list[tuple[str, str, float]]]:
    """Best legal lineup from the players available.

    ``slots`` is the league's starting positions (bench already removed),
    ``scores`` maps player_id -> points, ``positions`` maps player_id ->
    position.

    Fills the most restrictive slots first, taking the best eligible player
    each time. Fantasy slot eligibility is nested (QB c SUPER_FLEX,
    RB c FLEX c SUPER_FLEX), which makes that greedy exactly optimal rather
    than just a decent heuristic.

    Returns (total, [(slot, player_id, points)]).
    """
    available = {
        pid: float(score or 0.0)
        for pid, score in scores.items()
        if not is_empty_slot(pid)
    }

    ordered_slots = sorted(
        enumerate(slots),
        key=lambda pair: (len(SLOT_ELIGIBILITY.get(pair[1], frozenset())) or 99, pair[0]),
    )

    chosen: list[tuple[int, str, str, float]] = []
    for index, slot in ordered_slots:
        eligible_positions = SLOT_ELIGIBILITY.get(slot)
        best_pid, best_score = None, None
        for pid, score in available.items():
            position = positions.get(pid, "")
            if eligible_positions is not None and position not in eligible_positions:
                continue
            if best_score is None or score > best_score:
                best_pid, best_score = pid, score
        if best_pid is None:
            continue
        del available[best_pid]
        chosen.append((index, slot, best_pid, best_score or 0.0))

    chosen.sort(key=lambda item: item[0])
    total = sum(item[3] for item in chosen)
    return total, [(slot, pid, score) for _, slot, pid, score in chosen]


def streak(results: Sequence[WeekResult]) -> str:
    """'W3' / 'L2' / '-' from the tail of a season."""
    played = [r for r in results if r.opponent_id is not None]
    if not played:
        return "-"
    played.sort(key=lambda r: r.week)
    last = played[-1].outcome
    if last == "-":
        return "-"
    count = 0
    for result in reversed(played):
        if result.outcome != last:
            break
        count += 1
    return f"{last}{count}"


def expected_wins(results: dict[int, list[WeekResult]], weeks: Sequence[int]) -> dict[int, float]:
    """Wins a roster "should" have: how often it beat the rest of the league.

    Each week, a roster is credited with (teams it outscored) / (teams - 1).
    This is the honest luck measure -- it ignores who you were scheduled
    against, which is the entire complaint.
    """
    per_week: dict[int, list[tuple[int, float]]] = {}
    for roster_id, week_results in results.items():
        for result in week_results:
            per_week.setdefault(result.week, []).append((roster_id, result.points))

    expected: dict[int, float] = {rid: 0.0 for rid in results}
    for week in weeks:
        entries = per_week.get(week, [])
        if len(entries) < 2:
            continue
        divisor = len(entries) - 1
        for roster_id, points in entries:
            beaten = sum(1 for other_id, other in entries if other_id != roster_id and points > other)
            tied = sum(1 for other_id, other in entries if other_id != roster_id and points == other)
            expected[roster_id] = expected.get(roster_id, 0.0) + (beaten + 0.5 * tied) / divisor
    return expected


def head_to_head(
    results: dict[int, list[WeekResult]], a: int, b: int
) -> tuple[int, int, int, list[WeekResult]]:
    """(a's wins, b's wins, ties, the games) between two rosters."""
    games = [r for r in results.get(a, []) if r.opponent_id == b]
    games.sort(key=lambda r: r.week)
    wins = sum(1 for g in games if g.outcome == "W")
    losses = sum(1 for g in games if g.outcome == "L")
    ties = sum(1 for g in games if g.outcome == "T")
    return wins, losses, ties, games


def close_games(results: Sequence[WeekResult], threshold: float = 10.0) -> tuple[int, int]:
    """(wins, losses) in games decided by less than ``threshold`` points."""
    wins = sum(1 for r in results if r.outcome == "W" and abs(r.margin) < threshold)
    losses = sum(1 for r in results if r.outcome == "L" and abs(r.margin) < threshold)
    return wins, losses


def injury_rank(status: str) -> int:
    return INJURY_ORDER.get(str(status or ""), 99)
