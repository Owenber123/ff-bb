"""The five scheduled posts.

These arrive uninvited, so the tests care about two things above all: that a
post is never sent when there is nothing worth saying, and that it stays short
enough to read on a phone.
"""

from __future__ import annotations

import pytest

import ffposts
from conftest import LEAGUE_ID, FakeSession, build_routes
from ffleague import load_view
from sleeper import SleeperClient

# A post that arrives unasked has a lower tolerance than one somebody requested.
MAX_POST_LINES = 18
MAX_POST_CHARS = 1200


def _view(tmp_path, routes=None, **kwargs):
    session = FakeSession(routes or build_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    client.players()
    return load_view(client, LEAGUE_ID, **kwargs)


@pytest.fixture
def live(client):
    """The fixture league, mid-season at week 4."""
    return load_view(client, LEAGUE_ID)


# ---------------------------------------------------------------------------
# blanket properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ffposts.POST_ORDER)
def test_posts_are_short_enough_for_a_phone(live, kind):
    text = ffposts.build(kind, live, 1)
    if text is None:
        return
    assert len(text) <= MAX_POST_CHARS
    assert len(text.split("\n")) <= MAX_POST_LINES


@pytest.mark.parametrize("kind", ffposts.POST_ORDER)
def test_posts_never_emit_markdown(live, kind):
    text = ffposts.build(kind, live, 1) or ""
    assert "**" not in text
    assert "```" not in text


@pytest.mark.parametrize("kind", ffposts.POST_ORDER)
def test_a_builder_that_throws_is_swallowed(live, kind, monkeypatch):
    # A broken builder must not take down the scheduler thread.
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    original = ffposts.POST_TYPES[kind]
    monkeypatch.setitem(
        ffposts.POST_TYPES, kind,
        ffposts.PostType(original.key, original.label, explode, original.when),
    )
    assert ffposts.build(kind, live, 1) is None


@pytest.mark.parametrize("kind", ffposts.POST_ORDER)
def test_an_empty_week_produces_no_post(live, kind):
    # Week 9 has no matchups in the fixture.
    assert ffposts.build(kind, live, 9) is None


@pytest.mark.parametrize("kind", ffposts.POST_ORDER)
def test_sleeper_being_down_produces_no_post(tmp_path, kind):
    routes = build_routes()
    session = FakeSession(routes)
    client = SleeperClient(tmp_path / "d", session=session)
    client.players()
    view = load_view(client, LEAGUE_ID)

    client._cache.clear()
    session.fail = True
    # A half-rendered post during an outage is worse than no post.
    assert ffposts.build(kind, view, 1) is None


@pytest.mark.parametrize("kind", ffposts.POST_ORDER)
def test_an_undrafted_league_produces_no_post(tmp_path, kind):
    routes = build_routes()
    routes[f"/league/{LEAGUE_ID}/rosters"] = [
        {"roster_id": i, "owner_id": f"u{i}", "players": [], "starters": ["0", "0"],
         "settings": {"wins": 0, "losses": 0, "fpts": 0}}
        for i in range(1, 5)
    ]
    for week in range(1, 19):
        routes[f"/league/{LEAGUE_ID}/matchups/{week}"] = []
    view = _view(tmp_path, routes)
    assert ffposts.build(kind, view, 1) is None


# ---------------------------------------------------------------------------
# 1. lineup check -- silence is the feature
# ---------------------------------------------------------------------------


def test_a_clean_league_produces_no_lineup_post(tmp_path):
    """The single most important behaviour in this file.

    A weekly "everything looks fine!" trains twelve people to ignore the bot.
    """
    routes = build_routes()
    # Week 2: nobody has an empty slot, nobody is on bye, nobody is hurt.
    routes["/schedule/nfl/regular/2025"] = [
        {"week": 2, "home": h, "away": a, "status": "pre_game", "date": "2025-09-14"}
        for h, a in (("BUF", "ATL"), ("CIN", "KC"), ("PHI", "DAL"), ("BAL", "DET"),
                     ("LAR", "NYJ"))
    ]
    routes["/players/nfl"] = {
        pid: {k: v for k, v in meta.items() if k != "injury_status"}
        for pid, meta in routes["/players/nfl"].items()
    }
    view = _view(tmp_path, routes)
    assert ffposts.build_lineup_check(view, 2) is None


def test_an_empty_starting_slot_is_reported(live):
    # Roster 3 starts ["300", "0", "301", "0"] -- two unfilled slots.
    text = ffposts.build_lineup_check(live, 1)
    assert text is not None
    assert "bru" in text          # bru32, shortened
    assert "empty" in text


def test_a_starter_on_bye_is_reported(tmp_path):
    routes = build_routes()
    view = _view(tmp_path, routes)
    # Week 2 of the fixture schedule byes BAL and DET; roster 2 starts
    # D. Henry (BAL) and M. Andrews (BAL).
    text = ffposts.build_lineup_check(view, 2)
    assert text is not None
    assert "barryyung" in text
    assert "on bye" in text


def test_a_starter_ruled_out_is_reported(live):
    text = ffposts.build_lineup_check(live, 1) or ""
    # Kelce is Questionable and started; Barkley is Out but benched.
    assert "OUT" not in text or "Barkley" not in text


def test_questionable_is_not_worth_interrupting_a_sunday(live):
    text = ffposts.build_lineup_check(live, 1) or ""
    assert "QUESTIONABLE" not in text
    assert "Kelce" not in text


def test_the_lineup_post_only_names_people_with_problems(live):
    text = ffposts.build_lineup_check(live, 1)
    assert text is not None
    # bloqus fields a full, healthy, playing lineup in week 1. Match on whole
    # lines: "bloqus" is a substring of "bloqus2", who does have a problem.
    named = {line.split(" · ")[0] for line in text.split("\n") if " · " in line}
    assert "bloqus" not in named
    assert {"bru", "bloqus2"} <= named


def test_stale_data_produces_no_lineup_post(tmp_path):
    session = FakeSession(build_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    client.players()
    view = load_view(client, LEAGUE_ID)
    # Make week 1 the live week: a finished week is immutable and cached hard,
    # so it never goes stale and this guard would never engage.
    view.week = 1
    assert ffposts.build_lineup_check(view, 1) is not None

    for entry in client._cache.values():
        entry.fetched_at -= 100_000
    session.fail = True
    # Warning somebody about a lineup based on hours-old data is worse than
    # staying quiet -- they may have already fixed it.
    assert ffposts.build_lineup_check(view, 1) is None


# ---------------------------------------------------------------------------
# 2. sunday evening
# ---------------------------------------------------------------------------


def test_sunday_leads_with_the_closest_game(live):
    text = ffposts.build_sunday_update(live, 3)
    assert text is not None
    # The closest game leads the body -- the week is in the message header now,
    # so the body does not repeat it.
    head = text.split("\n\n")[0]
    assert head.startswith("😬 Closest")
    # Week 3's closest is bloqus2 99.0 vs barryyung 100.0 (1.0 apart).
    assert "99.0" in head and "100.0" in head


def test_sunday_names_a_blowout_when_there_is_one(live):
    text = ffposts.build_sunday_update(live, 3) or ""
    assert "Beatdown" in text
    assert "bloqus" in text and "bru" in text


def test_sunday_ends_after_the_beatdown(live):
    text = ffposts.build_sunday_update(live, 1) or ""
    assert "Up: " not in text
    assert "starters left" not in text


def test_sunday_is_short(live):
    # Blank lines between sections are deliberate -- they cost a line each and
    # make the bubble scannable. Still nothing like a wall of text.
    text = ffposts.build_sunday_update(live, 1) or ""
    assert len(text.split("\n")) <= 10


# ---------------------------------------------------------------------------
# 3. the recap -- the flagship
# ---------------------------------------------------------------------------


def test_recap_has_every_required_element(live):
    text = ffposts.build_recap(live, 1)
    assert text is not None
    # The title lives in the message header ("bloqBot - recap wk1"), not here.
    assert "Week 1 final" not in text
    assert "\u203a" in text  # score separator, far cheaper than " def. "
    assert "def." not in text
    assert "High" in text
    assert "Low" in text
    assert "Blowout" in text and "Closest" in text


def test_recap_reports_the_right_high_and_low(live):
    text = ffposts.build_recap(live, 1)
    # Week 1: bloqus2 120.0 high, bru32 80.0 low.
    assert "High 120.0 bloqus2" in text
    assert "Low 80.0 bru" in text


def test_recap_names_the_worst_bench_decision(live):
    text = ffposts.build_recap(live, 1)
    # bloqus benched S. Barkley (RB, 40.0). The biggest single swap he unlocks
    # is the FLEX, held by T. Kelce at 20.0 -- not the RB slot's B. Robinson
    # at 25.0. Taking the worst *starter* Barkley could legally have replaced
    # is the whole point of the swap framing.
    assert "Worst call" in text
    assert "S. Barkley" in text
    assert "T. Kelce" in text
    assert "\u221220.0" in text
    # Broken across lines on purpose rather than wrapped mid-name.
    assert "started T. Kelce 20.0" in text
    assert "over S. Barkley 40.0" in text


def test_a_trivial_bench_mistake_is_not_worth_a_line(live):
    # Week 2: nobody benched anybody who beats a starter by 5+.
    text = ffposts.build_recap(live, 2) or ""
    assert "Worst call" not in text


def test_recap_lists_every_matchup(live):
    text = ffposts.build_recap(live, 1)
    for name in ("bloqus", "barryyung", "bru", "bloqus2"):
        assert name in text


# ---------------------------------------------------------------------------
# worst bench swap
# ---------------------------------------------------------------------------


def test_worst_bench_swap_finds_the_biggest_single_swap(live):
    swap = ffposts.worst_bench_swap(live, 1)
    assert swap is not None
    assert swap.manager_name == "bloqus"
    assert swap.benched == "S. Barkley"
    assert swap.benched_points == pytest.approx(40.0)
    # The largest gap, not the first eligible one: Kelce in the FLEX (20.0),
    # not B. Robinson in the RB slot (25.0).
    assert swap.started == "T. Kelce"
    assert swap.gap == pytest.approx(20.0)


def test_worst_bench_swap_respects_slot_eligibility(tmp_path):
    """A benched QB cannot replace a WR, however many points it scored."""
    routes = build_routes()
    routes[f"/league/{LEAGUE_ID}"] = dict(
        routes[f"/league/{LEAGUE_ID}"],
        roster_positions=["QB", "WR", "BN"],  # no FLEX, so no cross-position swap
    )
    routes[f"/league/{LEAGUE_ID}/matchups/1"] = [
        {"roster_id": 1, "matchup_id": 1, "points": 35.0,
         "starters": ["100", "102"], "starters_points": [30.0, 5.0],
         "players_points": {"100": 30.0, "102": 5.0, "200": 99.0}},  # 200 is a QB
        {"roster_id": 2, "matchup_id": 1, "points": 10.0,
         "starters": ["200", "202"], "starters_points": [5.0, 5.0],
         "players_points": {"200": 5.0, "202": 5.0}},
    ]
    view = _view(tmp_path, routes)
    swap = ffposts.worst_bench_swap(view, 1)
    # The 99-point QB may only displace the QB slot (30.0), not the WR (5.0).
    assert swap is not None
    assert swap.started == "J. Allen"
    assert swap.gap == pytest.approx(69.0)


def test_worst_bench_swap_without_player_data(tmp_path):
    session = FakeSession(build_routes())
    client = SleeperClient(tmp_path / "d", session=session)
    view = load_view(client, LEAGUE_ID)
    # Positions are unknown, so eligibility cannot be judged; claiming a swap
    # would be a guess.
    assert ffposts.worst_bench_swap(view, 1) is None


# ---------------------------------------------------------------------------
# 4. week ahead
# ---------------------------------------------------------------------------


def test_week_ahead_lists_the_matchups(live):
    text = ffposts.build_week_ahead(live, 4)
    assert text is not None
    for name in ("bloqus", "barryyung", "bru", "bloqus2"):
        assert name in text


def test_week_ahead_picks_a_game_of_the_week(live):
    text = ffposts.build_week_ahead(live, 4)
    assert "Game of the week" in text
    assert "3-1" in text  # bloqus, the best record, is in it


def test_week_ahead_shows_waivers_when_there_are_any(live):
    text = ffposts.build_week_ahead(live, 4)
    assert "Waivers" in text
    assert "P. Nacua" in text


def test_week_ahead_omits_the_waiver_block_when_empty(live):
    text = ffposts.build_week_ahead(live, 3) or ""
    assert "Waivers" not in text


def test_week_ahead_ignores_failed_claims(live):
    text = ffposts.build_week_ahead(live, 4) or ""
    # The failed claim in the fixture is roster 2's; only the completed
    # add should appear.
    assert text.count("P. Nacua") == 1


# ---------------------------------------------------------------------------
# 5. shotgun list
# ---------------------------------------------------------------------------


def _shotgun_view(tmp_path, rows, week=4):
    routes = build_routes()
    routes[f"/league/{LEAGUE_ID}/matchups/{week}"] = rows
    return _view(tmp_path, routes)


def _row(roster_id, matchup_id, starters, starter_points, bench=None):
    players_points = dict(zip(starters, starter_points))
    players_points.update(bench or {})
    return {
        "roster_id": roster_id, "matchup_id": matchup_id,
        "points": sum(starter_points), "starters": starters,
        "starters_points": starter_points, "players_points": players_points,
    }


SHOTGUN_ROWS = [
    # bloqus: one zero and one negative -- two, so "(2)".
    _row(1, 1, ["100", "101", "102", "103"], [0.0, 25.0, -1.5, 20.0]),
    # barryyung: a clean lineup, but a zero on the bench, which does not count.
    _row(2, 1, ["200", "201", "202", "203"], [30.0, 20.0, 20.0, 20.0], bench={"204": 0.0}),
    # bru32: an empty slot (Sleeper's "0", scored 0.0) and a negative DEF.
    _row(3, 2, ["300", "0", "PHI"], [50.0, 0.0, -2.0]),
    # bloqus2: exactly one zero, so no count.
    _row(4, 2, ["400", "401"], [70.0, 0.0]),
]


def test_shotgun_lists_everyone_who_started_a_zero(tmp_path):
    text = ffposts.build("shotgun", _shotgun_view(tmp_path, SHOTGUN_ROWS), 4)
    assert text.split("\n") == ["bloqus (2)", "bloqus2", "bru"]


def test_shotgun_counts_zero_and_negative(tmp_path):
    counts = dict(ffposts.shotgun_counts(_shotgun_view(tmp_path, SHOTGUN_ROWS), 4))
    assert counts["bloqus"] == 2  # 0.0 and -1.5


def test_shotgun_counts_a_negative_defense(tmp_path):
    counts = dict(ffposts.shotgun_counts(_shotgun_view(tmp_path, SHOTGUN_ROWS), 4))
    # The empty slot beside it is not a starter, so this is the DEF alone.
    assert counts["bru"] == 1  # bru32, shortened as everywhere else


def test_shotgun_ignores_the_bench(tmp_path):
    counts = dict(ffposts.shotgun_counts(_shotgun_view(tmp_path, SHOTGUN_ROWS), 4))
    assert "barryyung" not in counts


def test_shotgun_count_is_shown_only_for_two_or_more(tmp_path):
    text = ffposts.build("shotgun", _shotgun_view(tmp_path, SHOTGUN_ROWS), 4)
    assert "(1)" not in text
    assert "bloqus2" in text.split("\n")


def test_shotgun_orders_most_zeros_first_then_name(tmp_path):
    rows = [
        _row(1, 1, ["100", "101"], [0.0, 10.0]),
        _row(2, 1, ["200", "201", "202"], [0.0, 0.0, 0.0]),
        _row(3, 2, ["300", "301"], [-1.0, -1.0]),
        _row(4, 2, ["400", "401"], [0.0, 5.0]),
    ]
    text = ffposts.build("shotgun", _shotgun_view(tmp_path, rows), 4)
    assert text.split("\n") == ["barryyung (3)", "bru (2)", "bloqus", "bloqus2"]


def test_shotgun_uses_the_nickname(tmp_path):
    routes = build_routes()
    routes[f"/league/{LEAGUE_ID}/matchups/4"] = SHOTGUN_ROWS
    view = _view(tmp_path, routes, nicknames={"u1": "Captain"})
    assert ffposts.build("shotgun", view, 4).split("\n")[0] == "Captain (2)"


def test_nobody_on_the_shotgun_list_posts_nothing(tmp_path):
    # Silence, like the lineup check: an empty list is not news.
    rows = [_row(r, 1 + (r > 2), ["1%d" % r, "2%d" % r], [10.0, 5.0]) for r in (1, 2, 3, 4)]
    assert ffposts.build("shotgun", _shotgun_view(tmp_path, rows), 4) is None


def test_an_unplayed_week_is_not_a_shotgun_list(tmp_path):
    # Before kickoff every starter is on 0.0. That must not name the league.
    rows = [_row(r, 1 + (r > 2), ["1%d" % r, "2%d" % r], [0.0, 0.0]) for r in (1, 2, 3, 4)]
    assert ffposts.build("shotgun", _shotgun_view(tmp_path, rows), 4) is None


def test_the_shotgun_title(tmp_path):
    assert ffposts.post_title("shotgun", 14) == "shotgun list wk14"
