"""Fixtures: a small fake league, served through a fake requests.Session.

Stubbing at the session level rather than mocking SleeperClient means the tests
exercise the real caching, the real stale-serving path and the real joins.
Nothing here touches the network.

The league is four teams, which is enough to have a bye-week edge case, an
ambiguous name, a nickname, and a matchup with a null matchup_id, while still
being small enough to assert on by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ffstore  # noqa: E402
import sleeper as sleeper_module  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Routes URLs to canned payloads and counts the calls.

    ``fail`` makes every subsequent request raise, which is how the
    stale-serving tests simulate Sleeper going away.
    """

    def __init__(self, routes: dict[str, object]):
        self.routes = dict(routes)
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}
        self.fail = False

    def get(self, url: str, timeout=None):
        self.calls.append(url)
        if self.fail:
            import requests

            raise requests.RequestException("simulated outage")
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                if isinstance(payload, int):
                    return FakeResponse(None, status_code=payload)
                return FakeResponse(payload)
        return FakeResponse(None, status_code=404)

    def count(self, suffix: str) -> int:
        return sum(1 for url in self.calls if url.endswith(suffix))


LEAGUE_ID = "999"
PREVIOUS_LEAGUE_ID = "888"

# fpts_decimal is HUNDREDTHS: 1436 + 5/100 = 1436.05. The whole point of the
# decimal tests below.
ROSTERS = [
    {
        "roster_id": 1,
        "owner_id": "u1",
        "players": ["100", "101", "102", "103", "104", "105"],
        "starters": ["100", "101", "102", "103"],
        "settings": {
            "wins": 3, "losses": 1, "ties": 0,
            "fpts": 1436, "fpts_decimal": 5,
            "fpts_against": 1200, "fpts_against_decimal": 80,
            "ppts": 1600, "ppts_decimal": 0,
            "waiver_budget_used": 40, "total_moves": 3,
        },
    },
    {
        "roster_id": 2,
        "owner_id": "u2",
        "players": ["200", "201", "202", "203"],
        "starters": ["200", "201", "202", "203"],
        "settings": {
            "wins": 2, "losses": 2, "ties": 0,
            "fpts": 1400, "fpts_decimal": 0,
            "fpts_against": 1350, "fpts_against_decimal": 25,
        },
    },
    {
        "roster_id": 3,
        "owner_id": "u3",
        # "0" is Sleeper's placeholder for an unfilled slot, not a player.
        "players": ["300", "301"],
        "starters": ["300", "0", "301", "0"],
        "settings": {
            "wins": 1, "losses": 3, "ties": 0,
            "fpts": 1100, "fpts_decimal": 50,
            "fpts_against": 1500, "fpts_against_decimal": 0,
        },
    },
    {
        "roster_id": 4,
        "owner_id": "u4",
        "players": ["400", "401"],
        "starters": ["400", "401"],
        "settings": {
            "wins": 2, "losses": 2, "ties": 0,
            "fpts": 1300, "fpts_decimal": 0,
            "fpts_against": 1290, "fpts_against_decimal": 0,
        },
    },
]

USERS = [
    {"user_id": "u1", "display_name": "bloqus", "metadata": {"team_name": "WaterBoi"}},
    {"user_id": "u2", "display_name": "barryyung", "metadata": {"team_name": "Jerkin Goff"}},
    # No team_name at all -- common, and the join must not fall over.
    {"user_id": "u3", "display_name": "bru32", "metadata": {}},
    # Shares a prefix with bloqus, for the ambiguity test.
    {"user_id": "u4", "display_name": "bloqus2", "metadata": {"team_name": "Go Birds"}},
]

LEAGUE = {
    "league_id": LEAGUE_ID,
    "name": "Test League",
    "season": "2025",
    "season_type": "regular",
    "status": "in_season",
    "total_rosters": 4,
    "previous_league_id": PREVIOUS_LEAGUE_ID,
    "roster_positions": ["QB", "RB", "WR", "FLEX", "BN", "BN"],
    "settings": {"playoff_week_start": 15, "playoff_teams": 2, "leg": 4, "waiver_budget": 100},
}

PLAYERS = {
    "100": {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "first_name": "Josh", "last_name": "Allen"},
    "101": {"full_name": "Bijan Robinson", "position": "RB", "team": "ATL", "first_name": "Bijan", "last_name": "Robinson"},
    "102": {"full_name": "Ja'Marr Chase", "position": "WR", "team": "CIN", "first_name": "Ja'Marr", "last_name": "Chase"},
    "103": {"full_name": "Travis Kelce", "position": "TE", "team": "KC", "first_name": "Travis", "last_name": "Kelce",
            "injury_status": "Questionable"},
    "104": {"full_name": "Saquon Barkley", "position": "RB", "team": "PHI", "first_name": "Saquon", "last_name": "Barkley",
            "injury_status": "Out"},
    "105": {"full_name": "Puka Nacua", "position": "WR", "team": "LAR", "first_name": "Puka", "last_name": "Nacua"},
    "200": {"full_name": "Jalen Hurts", "position": "QB", "team": "PHI", "first_name": "Jalen", "last_name": "Hurts"},
    "201": {"full_name": "Derrick Henry", "position": "RB", "team": "BAL", "first_name": "Derrick", "last_name": "Henry"},
    "202": {"full_name": "CeeDee Lamb", "position": "WR", "team": "DAL", "first_name": "CeeDee", "last_name": "Lamb"},
    "203": {"full_name": "Mark Andrews", "position": "TE", "team": "BAL", "first_name": "Mark", "last_name": "Andrews"},
    "300": {"full_name": "Patrick Mahomes", "position": "QB", "team": "KC", "first_name": "Patrick", "last_name": "Mahomes"},
    "301": {"full_name": "Jahmyr Gibbs", "position": "RB", "team": "DET", "first_name": "Jahmyr", "last_name": "Gibbs"},
    "400": {"full_name": "Lamar Jackson", "position": "QB", "team": "BAL", "first_name": "Lamar", "last_name": "Jackson"},
    "401": {"full_name": "Breece Hall", "position": "RB", "team": "NYJ", "first_name": "Breece", "last_name": "Hall"},
    # Defenses have no full_name and their ID is the team abbreviation.
    "PHI": {"position": "DEF", "team": "PHI", "first_name": "Philadelphia", "last_name": "Eagles"},
}


def _matchup(roster_id, matchup_id, points, starters, starter_points, extra=None):
    players_points = dict(zip(starters, starter_points))
    players_points.update(extra or {})
    return {
        "roster_id": roster_id,
        "matchup_id": matchup_id,
        "points": points,
        "starters": starters,
        "starters_points": starter_points,
        "players_points": players_points,
    }


MATCHUPS = {
    1: [
        _matchup(1, 1, 100.0, ["100", "101", "102", "103"], [30.0, 25.0, 25.0, 20.0],
                 extra={"104": 40.0, "105": 5.0}),  # 40 on the bench, a better RB than 101
        _matchup(2, 1, 90.0, ["200", "201", "202", "203"], [30.0, 20.0, 20.0, 20.0]),
        _matchup(3, 2, 80.0, ["300", "0", "301", "0"], [50.0, 0.0, 30.0, 0.0]),
        _matchup(4, 2, 120.0, ["400", "401"], [70.0, 50.0]),
    ],
    2: [
        _matchup(1, 1, 110.0, ["100", "101", "102", "103"], [40.0, 30.0, 20.0, 20.0]),
        _matchup(2, 1, 105.0, ["200", "201", "202", "203"], [35.0, 30.0, 20.0, 20.0]),
        _matchup(3, 2, 95.0, ["300", "301"], [55.0, 40.0]),
        _matchup(4, 2, 60.0, ["400", "401"], [40.0, 20.0]),
    ],
    3: [
        _matchup(1, 1, 130.0, ["100", "101", "102", "103"], [40.0, 40.0, 30.0, 20.0]),
        _matchup(3, 1, 70.0, ["300", "301"], [40.0, 30.0]),
        _matchup(2, 2, 100.0, ["200", "201", "202", "203"], [30.0, 30.0, 20.0, 20.0]),
        _matchup(4, 2, 99.0, ["400", "401"], [50.0, 49.0]),
    ],
    4: [
        _matchup(1, 1, 85.0, ["100", "101", "102", "103"], [20.0, 20.0, 25.0, 20.0]),
        _matchup(4, 1, 140.0, ["400", "401"], [80.0, 60.0]),
        _matchup(2, 2, 111.0, ["200", "201", "202", "203"], [31.0, 30.0, 30.0, 20.0]),
        _matchup(3, 2, 45.0, ["300", "301"], [25.0, 20.0]),
    ],
    # Playoff-shaped week: a null matchup_id means "no game", not "pair these up".
    5: [
        _matchup(1, 1, 100.0, ["100", "101"], [50.0, 50.0]),
        _matchup(2, 1, 90.0, ["200", "201"], [50.0, 40.0]),
        {"roster_id": 3, "matchup_id": None, "points": 0.0, "starters": [], "starters_points": [], "players_points": {}},
        {"roster_id": 4, "matchup_id": None, "points": 0.0, "starters": [], "starters_points": [], "players_points": {}},
    ],
}

SCHEDULE_2025 = (
    # Week 1: everyone plays except LAR and NYJ.
    [{"week": 1, "home": h, "away": a, "status": "complete", "date": "2025-09-07"}
     for h, a in (("BUF", "ATL"), ("CIN", "KC"), ("PHI", "DAL"), ("BAL", "DET"))]
    # Week 2: BAL and DET are on bye.
    + [{"week": 2, "home": h, "away": a, "status": "complete", "date": "2025-09-14"}
       for h, a in (("BUF", "ATL"), ("CIN", "KC"), ("PHI", "DAL"), ("LAR", "NYJ"))]
    # A cancelled game must not count as "played", or the teams look non-bye.
    + [{"week": 3, "home": "BAL", "away": "DET", "status": "canceled", "date": "2025-09-21"},
       {"week": 3, "home": "BUF", "away": "ATL", "status": "complete", "date": "2025-09-21"},
       {"week": 3, "home": "CIN", "away": "KC", "status": "complete", "date": "2025-09-21"},
       {"week": 3, "home": "PHI", "away": "DAL", "status": "complete", "date": "2025-09-21"},
       {"week": 3, "home": "LAR", "away": "NYJ", "status": "complete", "date": "2025-09-21"}]
)

TRANSACTIONS_4 = [
    {"type": "waiver", "status": "complete", "status_updated": 300, "roster_ids": [1],
     "adds": {"105": 1}, "drops": {"104": 1}, "settings": {"waiver_bid": 17}, "draft_picks": []},
    # A failed claim is not news and must not be reported as a move.
    {"type": "waiver", "status": "failed", "status_updated": 200, "roster_ids": [2],
     "adds": {"105": 2}, "drops": None, "settings": {"waiver_bid": 5}, "draft_picks": []},
    # drops: null is common and must not blow up the formatter.
    {"type": "free_agent", "status": "complete", "status_updated": 100, "roster_ids": [3],
     "adds": {"301": 3}, "drops": None, "settings": {}, "draft_picks": []},
    {"type": "trade", "status": "complete", "status_updated": 400, "roster_ids": [1, 2],
     "adds": {"201": 1, "101": 2}, "drops": {"101": 1, "201": 2},
     "draft_picks": [{"season": "2026", "round": 1, "owner_id": 1, "previous_owner_id": 2}],
     "settings": {}},
]

STATE = {"week": 4, "season": "2025", "season_type": "regular", "display_week": 4}


def build_routes(**overrides) -> dict[str, object]:
    routes: dict[str, object] = {
        "/state/nfl": STATE,
        f"/league/{LEAGUE_ID}": LEAGUE,
        f"/league/{LEAGUE_ID}/users": USERS,
        f"/league/{LEAGUE_ID}/rosters": ROSTERS,
        f"/league/{LEAGUE_ID}/winners_bracket": [],
        "/players/nfl": PLAYERS,
        "/schedule/nfl/regular/2025": SCHEDULE_2025,
        f"/league/{LEAGUE_ID}/transactions/4": TRANSACTIONS_4,
    }
    for week, rows in MATCHUPS.items():
        routes[f"/league/{LEAGUE_ID}/matchups/{week}"] = rows
    for week in range(1, 19):
        routes.setdefault(f"/league/{LEAGUE_ID}/matchups/{week}", [])
        routes.setdefault(f"/league/{LEAGUE_ID}/transactions/{week}", [])
    routes.update(overrides)
    return routes


@pytest.fixture
def session() -> FakeSession:
    return FakeSession(build_routes())


@pytest.fixture
def client(tmp_path, session) -> sleeper_module.SleeperClient:
    client = sleeper_module.SleeperClient(tmp_path / "data", session=session)
    client.players()  # warm the trimmed player map; no network involved
    return client


@pytest.fixture
def view(client):
    from ffleague import load_view

    return load_view(client, LEAGUE_ID)


@pytest.fixture
def store(tmp_path):
    store = ffstore.Store(tmp_path / "bot.db")
    ffstore.set_store(store)
    yield store
    ffstore.set_store(None)
