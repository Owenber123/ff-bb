"""Caching, stale-serving, the player file, and bye derivation."""

from __future__ import annotations

import json
import time

import pytest

from conftest import LEAGUE_ID, FakeSession, build_routes
from sleeper import (
    LeagueNotFound,
    SleeperClient,
    SleeperError,
    describe_age,
    is_empty_slot,
)


# ---------------------------------------------------------------------------
# caching
# ---------------------------------------------------------------------------


def test_repeat_calls_hit_the_cache(client, session):
    before = session.count("/league/999/users")
    for _ in range(5):
        client.users(LEAGUE_ID)
    assert session.count("/league/999/users") == before + 1


def test_each_endpoint_caches_independently(client, session):
    client.state()
    client.state()
    client.league(LEAGUE_ID)
    assert session.count("/state/nfl") == 1
    assert session.count("/league/999") >= 1


def test_expired_entries_refetch(client, session):
    client.state()
    entry = client._peek("state/nfl")
    entry.fetched_at = time.time() - 100_000  # older than TTL_STATE
    client.state()
    assert session.count("/state/nfl") == 2


def test_a_full_view_costs_a_handful_of_calls(tmp_path):
    """Rate-limit guard: building the view must not fan out."""
    from ffleague import load_view

    session = FakeSession(build_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    load_view(client, LEAGUE_ID)
    first = len(session.calls)
    assert first <= 4  # league, users, rosters, state

    for _ in range(10):
        load_view(client, LEAGUE_ID)
    assert len(session.calls) == first  # everything after is cache


# ---------------------------------------------------------------------------
# stale serving -- the behaviour that keeps tracebacks out of the chat
# ---------------------------------------------------------------------------


def test_an_outage_serves_the_last_good_value(client, session):
    entry = client.users(LEAGUE_ID)
    assert entry.stale is False
    original = entry.value

    entry.fetched_at = time.time() - 100_000
    session.fail = True

    stale = client.users(LEAGUE_ID)
    assert stale.stale is True
    assert stale.value == original
    assert "simulated outage" in stale.error
    assert stale.age > 1000  # the age reported is of the DATA, not the attempt


def test_an_outage_with_no_cached_value_raises(tmp_path):
    session = FakeSession(build_routes())
    session.fail = True
    client = SleeperClient(tmp_path / "d", session=session)
    with pytest.raises(SleeperError, match="could not reach Sleeper"):
        client.state()


def test_recovery_clears_the_stale_flag(client, session):
    client.users(LEAGUE_ID)
    client._peek("league/999/users").fetched_at = time.time() - 100_000
    session.fail = True
    assert client.users(LEAGUE_ID).stale is True

    session.fail = False
    fresh = client.users(LEAGUE_ID)
    assert fresh.stale is False
    assert fresh.age < 5


def test_a_null_league_body_is_not_found(tmp_path):
    # Sleeper answers 200 with a JSON null for a bad ID rather than 404.
    routes = build_routes()
    routes["/league/999"] = None
    client = SleeperClient(tmp_path / "d", session=FakeSession(routes))
    with pytest.raises(LeagueNotFound):
        client.league(LEAGUE_ID)


def test_an_unknown_user_is_none_not_an_error(tmp_path):
    routes = build_routes()
    routes["/user/ghost"] = None
    client = SleeperClient(tmp_path / "d", session=FakeSession(routes))
    assert client.user("ghost") is None


def test_rate_limiting_is_reported_in_words(tmp_path):
    routes = build_routes()
    routes["/state/nfl"] = 429
    client = SleeperClient(tmp_path / "d", session=FakeSession(routes))
    with pytest.raises(SleeperError, match="rate limiting"):
        client.state()


# ---------------------------------------------------------------------------
# final weeks persist
# ---------------------------------------------------------------------------


def test_final_weeks_are_written_to_disk_and_survive_a_restart(tmp_path):
    session = FakeSession(build_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    client.matchups(LEAGUE_ID, 2, final=True)
    assert (tmp_path / "d" / f"matchups_{LEAGUE_ID}_2.json").is_file()
    calls = len(session.calls)

    # A "restart": new client, same directory, no network.
    session.fail = True
    restarted = SleeperClient(tmp_path / "d", session=session)
    entry = restarted.matchups(LEAGUE_ID, 2, final=True)
    assert entry.value  # served from disk
    assert len(session.calls) == calls


def test_live_weeks_are_not_persisted(tmp_path):
    session = FakeSession(build_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    client.matchups(LEAGUE_ID, 4, final=False)
    assert not (tmp_path / "d" / f"matchups_{LEAGUE_ID}_4.json").exists()


# ---------------------------------------------------------------------------
# the player file
# ---------------------------------------------------------------------------


def test_the_player_file_is_trimmed_on_ingest(client):
    players = client.players()
    assert players["100"]["full_name"] == "Josh Allen"
    # Fields we do not use are dropped -- 15MB of JSON is a lot of RAM on an LXC.
    assert set(players["100"]) <= {
        "first_name", "last_name", "full_name", "position", "team",
        "injury_status", "injury_body_part", "status", "years_exp", "number",
    }


def test_the_player_file_is_read_from_disk_on_restart(tmp_path):
    session = FakeSession(build_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    client.players()
    calls = session.count("/players/nfl")

    session.fail = True
    restarted = SleeperClient(tmp_path / "d", session=session)
    assert restarted.players()["100"]["full_name"] == "Josh Allen"
    assert session.count("/players/nfl") == calls  # no second download


def test_player_names_resolve_including_defenses(client):
    assert client.player_name("100") == "Josh Allen"
    assert client.player_name("100", short=True) == "J. Allen"
    # Defenses appear in starters[] as a team abbreviation, not a numeric ID.
    assert client.player_name("PHI") == "Eagles D/ST"


def test_an_unknown_player_id_degrades_to_the_id(client):
    assert client.player_name("999999") == "999999"


def test_player_name_of_a_placeholder(client):
    assert client.player_name("0") == "(empty)"


def test_players_returns_empty_rather_than_blocking_a_reply(tmp_path):
    # A command handler must never wait on 15MB. It gets {} and degrades to
    # raw player IDs for this one reply.
    session = FakeSession(build_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    assert client.players(allow_fetch=False) == {}


def test_a_non_blocking_miss_still_kicks_off_a_background_load(tmp_path):
    # ...but it must self-heal, or a container with no disk cache would show
    # raw IDs forever, since no caller ever blocks.
    session = FakeSession(build_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    client.players(allow_fetch=False)

    deadline = time.time() + 5
    while time.time() < deadline and not client.players(allow_fetch=False):
        time.sleep(0.02)
    assert client.player_name("100") == "Josh Allen"


def test_a_failed_background_load_does_not_wedge_the_refresh_flag(tmp_path):
    session = FakeSession(build_routes())
    session.fail = True
    client = SleeperClient(tmp_path / "d", session=session)
    client.players(allow_fetch=False)

    deadline = time.time() + 5
    while time.time() < deadline and client._players_refreshing:
        time.sleep(0.02)
    assert client._players_refreshing is False  # a retry is possible

    session.fail = False
    client.players(allow_fetch=False)
    deadline = time.time() + 5
    while time.time() < deadline and not client.players(allow_fetch=False):
        time.sleep(0.02)
    assert client.player_name("100") == "Josh Allen"


def test_an_empty_player_file_is_rejected(tmp_path):
    routes = build_routes()
    routes["/players/nfl"] = {}
    client = SleeperClient(tmp_path / "d", session=FakeSession(routes))
    with pytest.raises(SleeperError, match="empty"):
        client.players()


# ---------------------------------------------------------------------------
# byes
# ---------------------------------------------------------------------------


def test_bye_teams_are_the_ones_absent_that_week(client):
    # Week 1 of the fixture schedule has everyone except LAR and NYJ.
    assert client.teams_on_bye("2025", 1) == {"LAR", "NYJ"}
    assert client.teams_on_bye("2025", 2) == {"BAL", "DET"}


def test_a_cancelled_game_does_not_count_as_played(client):
    # BAL/DET's week 3 game is cancelled, so both are still "not playing".
    assert client.teams_on_bye("2025", 3) == {"BAL", "DET"}


def test_a_week_outside_the_schedule_reports_no_byes(client):
    assert client.teams_on_bye("2025", 17) == set()


def test_byes_degrade_to_empty_when_the_schedule_is_missing(tmp_path):
    routes = build_routes()
    del routes["/schedule/nfl/regular/2025"]
    client = SleeperClient(tmp_path / "d", session=FakeSession(routes))
    # An undocumented endpoint disappearing must not raise into a command.
    assert client.teams_on_bye("2025", 1) == set()


def test_the_schedule_is_cached_to_disk(tmp_path):
    session = FakeSession(build_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    client.teams_on_bye("2025", 1)
    assert (tmp_path / "d" / "schedule_2025.json").is_file()

    session.fail = True
    restarted = SleeperClient(tmp_path / "d", session=session)
    assert restarted.teams_on_bye("2025", 1) == {"LAR", "NYJ"}


# ---------------------------------------------------------------------------
# odds and ends
# ---------------------------------------------------------------------------


def test_describe_age_reads_like_a_human_wrote_it():
    assert describe_age(0) == "just now"
    assert describe_age(44) == "just now"
    assert describe_age(240) == "4 min ago"
    assert describe_age(3600 * 2) == "2 hr ago"
    assert describe_age(86400 * 3) == "3 d ago"
    assert describe_age(-5) == "just now"  # clock skew must not print "-1 min"


def test_is_empty_slot_catches_every_placeholder_shape():
    assert is_empty_slot("0")
    assert is_empty_slot("")
    assert is_empty_slot(None)  # stringifies to "None"
    assert not is_empty_slot("100")
    assert not is_empty_slot("PHI")


def test_a_corrupt_disk_cache_is_ignored_not_fatal(tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    (data / "players_nfl.json").write_text("{not json")
    session = FakeSession(build_routes())
    client = SleeperClient(data, session=session)
    assert client.players()["100"]["full_name"] == "Josh Allen"  # refetched


def test_disk_writes_are_atomic(tmp_path):
    client = SleeperClient(tmp_path / "d", session=FakeSession(build_routes()))
    client._write_disk("thing.json", {"a": 1})
    assert json.loads((tmp_path / "d" / "thing.json").read_text()) == {"a": 1}
    # No temp files left behind.
    assert [p.name for p in (tmp_path / "d").iterdir()] == ["thing.json"]
