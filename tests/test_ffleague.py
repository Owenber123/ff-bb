"""The data joins: roster -> user -> matchup, and everything derived from them."""

from __future__ import annotations

import pytest

from conftest import LEAGUE, LEAGUE_ID, FakeSession, build_routes
from ffleague import (
    NameAmbiguous,
    NameNotFound,
    close_games,
    expected_wins,
    head_to_head,
    load_view,
    optimal_lineup,
    points_from_settings,
    real_players,
    streak,
)
from sleeper import SleeperClient, SleeperError


# ---------------------------------------------------------------------------
# the join
# ---------------------------------------------------------------------------


def test_join_maps_roster_to_owner_to_display_name(view):
    assert view.managers[1].display_name == "bloqus"
    assert view.managers[1].team_name == "WaterBoi"
    assert view.managers[2].display_name == "barryyung"
    # A user with no team_name in metadata must still join cleanly.
    assert view.managers[3].display_name == "bru32"
    assert view.managers[3].team_name == ""


def test_matchup_sides_carry_the_right_managers(view):
    matchups, _, _ = view.matchups(1)
    pairs = {
        tuple(sorted(side.manager.display_name for side in m.sides)) for m in matchups
    }
    assert pairs == {("barryyung", "bloqus"), ("bloqus2", "bru32")}


def test_matchup_pairing_uses_matchup_id_not_order(view):
    # Week 3's rows are deliberately out of order: roster 1 pairs with 3, not 2.
    matchups, _, _ = view.matchups(3)
    by_id = {m.matchup_id: sorted(s.roster_id for s in m.sides) for m in matchups}
    assert by_id == {1: [1, 3], 2: [2, 4]}


def test_null_matchup_id_rows_are_dropped_not_paired(view):
    # Playoff weeks null out matchup_id for teams with no game. Pairing those
    # rows with each other would invent a matchup that never happened.
    matchups, _, _ = view.matchups(5)
    assert len(matchups) == 1
    assert sorted(s.roster_id for s in matchups[0].sides) == [1, 2]


def test_placeholder_starters_are_not_players(view):
    # Roster 3's starters are ["300", "0", "301", "0"].
    assert view.managers[3].starters == ["300", "301"]
    matchups, _, _ = view.matchups(1)
    side = next(s for m in matchups for s in m.sides if s.roster_id == 3)
    assert [pid for pid, _ in side.started()] == ["300", "301"]


def test_real_players_filters_placeholders():
    assert real_players(["1", "0", "2", "", None]) == ["1", "2"]
    assert real_players(None) == []


# ---------------------------------------------------------------------------
# the decimal trap
# ---------------------------------------------------------------------------


def test_fpts_decimal_is_hundredths_not_tenths():
    # Verified against a finished league: fpts=1802 fpts_decimal=8 sums to
    # 1802.08 across weeks 1-14, not 1802.8.
    assert points_from_settings({"fpts": 1802, "fpts_decimal": 8}) == pytest.approx(1802.08)
    assert points_from_settings({"fpts": 1593, "fpts_decimal": 80}) == pytest.approx(1593.80)
    assert points_from_settings({"fpts": 2015, "fpts_decimal": 0}) == pytest.approx(2015.0)


def test_points_from_settings_handles_missing_and_junk():
    assert points_from_settings({}) == 0.0
    assert points_from_settings({"fpts": None, "fpts_decimal": None}) == 0.0
    assert points_from_settings({"fpts": "x"}) == 0.0
    assert points_from_settings(
        {"fpts_against": 1200, "fpts_against_decimal": 80}, "fpts_against"
    ) == pytest.approx(1200.80)


def test_manager_points_carry_the_decimal(view):
    assert view.managers[1].fpts == pytest.approx(1436.05)
    assert view.managers[1].fpts_against == pytest.approx(1200.80)
    assert view.managers[3].fpts == pytest.approx(1100.50)


# ---------------------------------------------------------------------------
# standings order
# ---------------------------------------------------------------------------


def test_standings_sort_by_win_pct_then_points(view):
    order = [m.display_name for m in view.sorted_managers()]
    # 3-1, then two 2-2 split on points (1400.00 > 1300.00), then 1-3.
    assert order == ["bloqus", "barryyung", "bloqus2", "bru32"]
    assert view.rank_of(view.managers[1]) == 1
    assert view.rank_of(view.managers[3]) == 4


def test_win_pct_counts_a_tie_as_half():
    from ffleague import Manager

    assert Manager(1, "u", "x", wins=1, losses=1, ties=2).win_pct == pytest.approx(0.5)
    assert Manager(1, "u", "x").win_pct == 0.0  # no games, no division by zero


def test_record_string_hides_zero_ties():
    from ffleague import Manager

    assert Manager(1, "u", "x", wins=3, losses=1).record == "3-1"
    assert Manager(1, "u", "x", wins=3, losses=1, ties=1).record == "3-1-1"


# ---------------------------------------------------------------------------
# season results
# ---------------------------------------------------------------------------


def test_season_results_produce_two_rows_per_matchup(view):
    results = view.season_results([1, 2, 3, 4])
    assert len(results[1]) == 4
    week1 = results[1][0]
    assert week1.points == 100.0
    assert week1.opponent_id == 2
    assert week1.opponent_points == 90.0
    assert week1.outcome == "W"
    assert week1.margin == pytest.approx(10.0)


def test_outcomes_cover_win_loss_tie():
    from ffleague import WeekResult

    assert WeekResult(1, 1, 100.0, 2, 90.0).outcome == "W"
    assert WeekResult(1, 1, 90.0, 2, 100.0).outcome == "L"
    assert WeekResult(1, 1, 90.0, 2, 90.0).outcome == "T"
    assert WeekResult(1, 1, 90.0, None, 0.0).outcome == "-"


def test_streak_counts_only_the_current_run(view):
    results = view.season_results([1, 2, 3, 4])
    # Roster 1: W (100-90), W (110-105), W (130-70), L (85-140).
    assert streak(results[1]) == "L1"
    # Roster 2: L, L, W, W.
    assert streak(results[2]) == "W2"


def test_streak_of_nothing_is_a_dash():
    assert streak([]) == "-"


def test_close_games_counts_both_directions(view):
    results = view.season_results([1, 2, 3, 4])
    # Roster 2's margins: -10, -5, +1, +66. The -10 is excluded because the
    # threshold is strict, so that leaves one close win and one close loss.
    assert close_games(results[2], threshold=10.0) == (1, 1)
    # Widening it past the exact-10 loss picks that one up.
    assert close_games(results[2], threshold=10.5) == (1, 2)


# ---------------------------------------------------------------------------
# luck
# ---------------------------------------------------------------------------


def test_expected_wins_measures_you_against_the_whole_league(view):
    weeks = [1, 2, 3, 4]
    results = view.season_results(weeks)
    expected = expected_wins(results, weeks)

    # Week 1 scores: r4=120, r1=100, r2=90, r3=80. r4 beats all three -> 1.0.
    # Every week's credit is (teams beaten)/(teams-1), so the league total is
    # exactly one win per matchup per week: 4 weeks * 2 matchups = 8.
    assert sum(expected.values()) == pytest.approx(8.0)
    assert all(0.0 <= value <= 4.0 for value in expected.values())
    # r1 outscored the field in weeks 1-3 and was last in week 4.
    assert expected[1] == pytest.approx(1.0 + 1.0 + 1.0 + 0.0)


def test_expected_wins_splits_a_tie(view):
    from ffleague import WeekResult

    results = {
        1: [WeekResult(1, 1, 100.0, 2, 100.0)],
        2: [WeekResult(1, 2, 100.0, 1, 100.0)],
    }
    expected = expected_wins(results, [1])
    assert expected[1] == pytest.approx(0.5)
    assert expected[2] == pytest.approx(0.5)


def test_expected_wins_ignores_weeks_with_one_team():
    from ffleague import WeekResult

    results = {1: [WeekResult(9, 1, 100.0, None, 0.0)]}
    assert expected_wins(results, [9]) == {1: 0.0}


# ---------------------------------------------------------------------------
# head to head
# ---------------------------------------------------------------------------


def test_head_to_head_is_symmetric(view):
    results = view.season_results([1, 2, 3, 4])
    wins, losses, ties, games = head_to_head(results, 1, 2)
    assert (wins, losses, ties) == (2, 0, 0)
    assert [g.week for g in games] == [1, 2]

    back_wins, back_losses, _, back_games = head_to_head(results, 2, 1)
    assert (back_wins, back_losses) == (losses, wins)
    assert len(back_games) == len(games)


def test_head_to_head_with_someone_you_never_played(view):
    results = view.season_results([1, 2])
    assert head_to_head(results, 1, 4) == (0, 0, 0, [])


# ---------------------------------------------------------------------------
# optimal lineup
# ---------------------------------------------------------------------------


def test_optimal_lineup_promotes_the_better_bench_player():
    slots = ["QB", "RB", "WR", "FLEX"]
    scores = {"qb": 30.0, "rb1": 25.0, "rb2": 40.0, "wr1": 25.0, "wr2": 5.0}
    positions = {"qb": "QB", "rb1": "RB", "rb2": "RB", "wr1": "WR", "wr2": "WR"}
    total, lineup = optimal_lineup(slots, scores, positions)
    # QB 30 + RB 40 + WR 25 + FLEX(best remaining of RB/WR/TE) 25 = 120.
    assert total == pytest.approx(120.0)
    assert dict((slot, pid) for slot, pid, _ in lineup)["RB"] == "rb2"


def test_optimal_lineup_fills_restrictive_slots_first():
    # The greedy is optimal because eligibility is nested. If FLEX were filled
    # first it would take the QB-ineligible best scorer and strand the RB slot.
    slots = ["FLEX", "RB"]
    scores = {"rb1": 30.0, "wr1": 40.0}
    positions = {"rb1": "RB", "wr1": "WR"}
    total, lineup = optimal_lineup(slots, scores, positions)
    assert total == pytest.approx(70.0)
    assert dict((slot, pid) for slot, pid, _ in lineup) == {"RB": "rb1", "FLEX": "wr1"}


def test_superflex_can_take_a_quarterback():
    slots = ["QB", "SUPER_FLEX"]
    scores = {"qb1": 30.0, "qb2": 28.0, "rb1": 10.0}
    positions = {"qb1": "QB", "qb2": "QB", "rb1": "RB"}
    total, _ = optimal_lineup(slots, scores, positions)
    assert total == pytest.approx(58.0)


def test_optimal_lineup_ignores_placeholders_and_short_rosters():
    total, lineup = optimal_lineup(["QB", "RB"], {"0": 99.0, "qb": 20.0}, {"qb": "QB"})
    assert total == pytest.approx(20.0)
    assert len(lineup) == 1  # no RB available; the slot is simply unfilled


def test_optimal_lineup_never_beats_a_perfect_lineup(view):
    matchups, _, _ = view.matchups(1)
    side = next(s for m in matchups for s in m.sides if s.roster_id == 2)
    positions = {pid: view.client.player_meta(pid).get("position", "") for pid in side.players_points}
    total, _ = optimal_lineup(view.starting_slots, side.players_points, positions)
    # Roster 2 started its only four players, so optimal == actual.
    assert total == pytest.approx(side.points)


def test_bench_points_are_found(view):
    matchups, _, _ = view.matchups(1)
    side = next(s for m in matchups for s in m.sides if s.roster_id == 1)
    positions = {pid: view.client.player_meta(pid).get("position", "") for pid in side.players_points}
    total, _ = optimal_lineup(view.starting_slots, side.players_points, positions)
    # Started QB30 + RB25 + WR25 + TE20 = 100, with Barkley (RB, 40) benched.
    # Optimal is QB30 + RB40 + WR25 + FLEX25, so 20 points were left behind.
    assert total == pytest.approx(120.0)
    assert total - side.points == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# name resolution
# ---------------------------------------------------------------------------


def test_resolve_by_display_name_and_team_name(view):
    assert view.resolve("barryyung").roster_id == 2
    assert view.resolve("WaterBoi").roster_id == 1
    assert view.resolve("waterboi").roster_id == 1  # case insensitive
    assert view.resolve("Go Birds").roster_id == 4  # spaces normalised away


def test_resolve_prefers_an_exact_match_over_a_prefix(view):
    # "bloqus" is also a prefix of "bloqus2"; exact must win rather than 409.
    assert view.resolve("bloqus").roster_id == 1


def test_resolve_reports_ambiguity_instead_of_guessing(view):
    with pytest.raises(NameAmbiguous) as excinfo:
        view.resolve("bloq")
    assert sorted(excinfo.value.options) == ["bloqus", "bloqus2"]


def test_resolve_tolerates_a_typo(view):
    assert view.resolve("barryung").roster_id == 2  # missing a 'y'


def test_resolve_raises_for_a_stranger(view):
    with pytest.raises(NameNotFound):
        view.resolve("nobody at all")


def test_nickname_wins_over_the_sleeper_name(client):
    view = load_view(client, LEAGUE_ID, nicknames={"u2": "Autodraft"})
    assert view.managers[2].name == "Autodraft"
    assert view.managers[2].formal_name == "barryyung"  # the real name is not lost
    assert view.resolve("autodraft").roster_id == 2
    assert view.resolve("barryyung").roster_id == 2  # and the old name still works


def test_caller_resolves_from_a_handle_map(view):
    links = {"5551234567": "u2"}
    assert view.caller("+1 (555) 123-4567", links).roster_id == 2
    assert view.caller("+15551234567", links).roster_id == 2
    assert view.caller("5559999999", links) is None
    assert view.caller("", links) is None


def test_caller_accepts_a_display_name_not_just_a_user_id(view):
    # So SLEEPER_USER_MAP can be written with names people actually know.
    assert view.caller("+15551234567", {"5551234567": "barryyung"}).roster_id == 2


def test_resolve_me_needs_a_link(view):
    with pytest.raises(NameNotFound, match="!link"):
        view.resolve("me", caller_handle="+15550000000", links={})
    assert view.resolve("me", caller_handle="+15551234567", links={"5551234567": "u1"}).roster_id == 1


# ---------------------------------------------------------------------------
# season rollover
# ---------------------------------------------------------------------------


def _predraft_routes():
    predraft = dict(LEAGUE)
    predraft.update({"league_id": "777", "season": "2026", "status": "pre_draft",
                     "previous_league_id": LEAGUE_ID, "settings": {"leg": 1, "playoff_week_start": 15}})
    routes = build_routes()
    routes["/league/777"] = predraft
    routes["/league/777/rosters"] = [
        {"roster_id": i, "owner_id": f"u{i}", "players": [], "starters": ["0", "0"],
         "settings": {"wins": 0, "losses": 0, "fpts": 0}}
        for i in range(1, 5)
    ]
    routes["/league/777/users"] = []
    return routes


def test_predraft_league_falls_back_to_the_previous_season(tmp_path):
    session = FakeSession(_predraft_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    view = load_view(client, "777")

    assert view.is_previous_season is True
    assert view.league_id == LEAGUE_ID
    assert view.configured_league_id == "777"
    assert view.has_games is True
    assert view.managers[1].display_name == "bloqus"


def test_the_fallback_can_be_switched_off(tmp_path):
    session = FakeSession(_predraft_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    view = load_view(client, "777", follow_previous=False)

    assert view.is_previous_season is False
    assert view.has_games is False  # which is what makes commands say so


def test_a_league_with_no_previous_season_stays_put(tmp_path):
    routes = _predraft_routes()
    routes["/league/777"] = dict(routes["/league/777"], previous_league_id=None)
    client = SleeperClient(tmp_path / "d", session=FakeSession(routes))
    view = load_view(client, "777")
    assert view.is_previous_season is False
    assert view.league_id == "777"


def test_missing_league_id_is_a_clear_error(client):
    with pytest.raises(SleeperError, match="SLEEPER_LEAGUE_ID"):
        load_view(client, "")


def test_regular_weeks_stop_before_the_playoffs(view):
    assert view.regular_weeks() == [1, 2, 3, 4]  # leg=4, playoffs start week 15
    view.league["settings"]["leg"] = 16
    assert view.regular_weeks()[-1] == 14
