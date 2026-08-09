"""The line-length budget, asserted across everything the bot can say.

An iMessage bubble fits roughly 30 characters at default text size, and the
font is proportional, so nothing lines up and anything longer soft-wraps
wherever it lands. This file is the guard rail: every scheduled post and every
command reply is rendered against realistic league data and checked.

"Realistic" matters. The four-team fixture used elsewhere has short names and
two-digit scores, which fits under any budget by accident. The league here has
the awkward shapes that actually break layouts: thirteen-character display
names, a name that shortens into a collision with another, three-digit scores,
an emoji team name, and a long nickname.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import ffcommands  # noqa: F401  -- imported for its @command registrations
import ffposts
import ffstore
import sleeper as sleeper_module
from conftest import FakeSession, build_routes
from ffformat import (
    MAX_LINE,
    compose_message,
    display_width,
    enforce_width,
    short_name,
)
from ffleague import load_view
from registry import CommandContext, all_commands, dispatch
from sleeper import SleeperClient

BIG_LEAGUE = "4242"

# The real prefix. It spends 13 of the 30 columns on the header line, which is
# what makes titles short identifiers rather than sentences.
PREFIX = "\U0001F916 bloqBot - "

# The names that actually cause trouble, taken from a real 12-team league.
DISPLAY_NAMES = [
    "charliemalone", "Ryanmd211", "JubbysGiants", "LeNimmonz", "finnmaloney96",
    "nomah98", "bru32", "TKoop2", "nickelia", "barryyung", "thebigap", "bloqus",
]
TEAM_NAMES = [
    "Diane Danvers Simmons", "DayzedConfused", "Jubby and his one Giant ",
    "A Top Shagger", "Zynbabwe Pouching Club", "Team finnmaloney96", "",
    "Justice", "Go birds 🦅", "Jerkin Goff 2 ur mom", "", "WaterBoi",
]


def _big_routes():
    """A 12-team league with realistic names and three-digit scores."""
    routes = build_routes()
    users = [
        {
            "user_id": f"u{i}",
            "display_name": DISPLAY_NAMES[i],
            "metadata": {"team_name": TEAM_NAMES[i]} if TEAM_NAMES[i] else {},
        }
        for i in range(12)
    ]
    rosters = []
    for i in range(12):
        rosters.append({
            "roster_id": i + 1,
            "owner_id": f"u{i}",
            "players": ["100", "101", "102", "103", "104", "105"],
            "starters": ["100", "101", "102", "103"],
            "settings": {
                "wins": 12 - i, "losses": i, "ties": 0,
                # Three-digit-and-change season totals, like a real season.
                "fpts": 1400 + i * 37, "fpts_decimal": (i * 7) % 100,
                "fpts_against": 1500 - i * 11, "fpts_against_decimal": 50,
                "ppts": 1900 + i * 20, "ppts_decimal": 0,
                "waiver_budget_used": 100, "total_moves": i,
            },
        })

    matchups = {}
    for week in range(1, 5):
        rows = []
        for i in range(12):
            # Scores that straddle 100, so both 2- and 3-digit widths appear.
            points = 78.4 + ((i * 13 + week * 7) % 75)
            rows.append({
                "roster_id": i + 1,
                "matchup_id": (i // 2) + 1,
                "points": round(points, 2),
                "starters": ["100", "101", "102", "103"],
                "starters_points": [30.5, 25.25, 12.0, 10.65],
                "players_points": {"100": 30.5, "101": 25.25, "102": 12.0,
                                   "103": 10.65, "104": 44.8, "105": 3.2},
            })
        matchups[week] = rows

    league = dict(routes["/league/999"], league_id=BIG_LEAGUE, total_rosters=12,
                  previous_league_id=None)
    routes[f"/league/{BIG_LEAGUE}"] = league
    routes[f"/league/{BIG_LEAGUE}/users"] = users
    routes[f"/league/{BIG_LEAGUE}/rosters"] = rosters
    routes[f"/league/{BIG_LEAGUE}/winners_bracket"] = []
    for week in range(1, 19):
        routes[f"/league/{BIG_LEAGUE}/matchups/{week}"] = matchups.get(week, [])
        routes[f"/league/{BIG_LEAGUE}/transactions/{week}"] = routes.get(
            "/league/999/transactions/4" if week == 4 else f"/league/999/transactions/{week}", []
        )
    return routes


@pytest.fixture
def big(tmp_path):
    session = FakeSession(_big_routes())
    client = SleeperClient(tmp_path / "data", session=session)
    client.players()
    sleeper_module.set_client(client)
    store = ffstore.Store(tmp_path / "bot.db")
    ffstore.set_store(store)

    config = SimpleNamespace(
        command_prefix="!",
        sleeper_league_id=BIG_LEAGUE,
        sleeper_user_map={"5551234567": "u11"},
        sleeper_follow_previous=False,
        sleeper_data_dir=str(tmp_path / "data"),
        sleeper_db_path=str(tmp_path / "bot.db"),
        sleeper_posts_enabled=False,
        sleeper_post_chat="",
        sleeper_post_types=ffposts.POST_ORDER,
        sleeper_timezone="America/New_York",
    )
    yield SimpleNamespace(client=client, session=session, store=store, config=config,
                          view=load_view(client, BIG_LEAGUE))
    sleeper_module.set_client(None)
    ffstore.set_store(None)


def offenders(text: str, budget: int = MAX_LINE) -> list[tuple[int, str]]:
    """Lines over budget, ignoring ones that are a single unbreakable token.

    A chat GUID or a URL longer than the budget is left whole on purpose --
    breaking it would make it uncopyable, which defeats printing it at all.
    """
    bad = []
    for line in text.split("\n"):
        if display_width(line) <= budget:
            continue
        if len(line.split()) == 1:
            continue
        bad.append((display_width(line), line))
    return bad


def assert_fits(text: str, label: str) -> None:
    bad = offenders(text)
    assert not bad, (
        f"{label} has {len(bad)} line(s) over {MAX_LINE} columns:\n"
        + "\n".join(f"  {w}: {line}" for w, line in bad)
    )


# ---------------------------------------------------------------------------
# the scheduled posts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ffposts.POST_ORDER)
@pytest.mark.parametrize("week", [1, 2, 3, 4])
def test_every_post_fits_the_budget(big, kind, week):
    text = ffposts.build(kind, big.view, week)
    if text is None:
        return
    assert_fits(text, f"post {kind} wk{week}")


@pytest.mark.parametrize("kind", ffposts.POST_ORDER)
def test_posts_fit_once_the_header_is_composed(big, kind):
    """The header line carries the prefix, so it has its own budget to meet."""
    body = ffposts.build(kind, big.view, 1)
    if body is None:
        return
    wire = enforce_width(compose_message(PREFIX, ffposts.post_title(kind, 1), body))
    assert_fits(wire, f"post {kind} on the wire")


@pytest.mark.parametrize("kind", ffposts.POST_ORDER)
def test_post_headers_are_one_line_then_a_blank(big, kind):
    body = ffposts.build(kind, big.view, 1)
    if body is None:
        return
    wire = compose_message(PREFIX, ffposts.post_title(kind, 1), body)
    lines = wire.split("\n")
    assert lines[0].startswith("\U0001F916 bloqBot - ")
    assert lines[1] == ""
    assert lines[2].strip()  # body starts immediately after the blank
    # The body must not repeat what the header already said.
    assert "bloqBot" not in "\n".join(lines[2:])


@pytest.mark.parametrize("kind", ffposts.POST_ORDER)
def test_posts_survive_a_long_nickname(big, kind):
    # Somebody will do this on day one.
    big.store.set_nickname("u0", "The Undisputed Champion Of Everything")
    view = load_view(big.client, BIG_LEAGUE, nicknames=big.store.nicknames())
    text = ffposts.build(kind, view, 1)
    if text is None:
        return
    assert_fits(text, f"post {kind} with a long nickname")


# ---------------------------------------------------------------------------
# every command
# ---------------------------------------------------------------------------

COMMAND_ARGS = {
    "help": ["", "all", "roast", "fantasy"],
    "roster": ["charliemalone"],
    "record": ["Ryanmd211"],
    "roast": ["", "charliemalone"],
    "cope": ["charliemalone"],
    "curse": ["charliemalone"],
    "nickname": ["charliemalone Autodrafting Legend"],
    "fine": ["charliemalone 12.50 for the whole vibe honestly"],
    "odds": ["charliemalone finishes last again this season"],
    "8ball": ["should I start my kicker over my quarterback"],
    "poll": ["who is the worst commissioner in this league | charliemalone | Ryanmd211"],
    "vote": ["1"],
    "preview": ["", "recap", "recap --wide", "lineups", "sunday", "ahead"],
    "link": ["charliemalone"],
    "matchups": ["", "2"],
    "bye": ["", "2"],
    "punishment": ["", "Waffle House for twenty four consecutive hours"],
    "pot": ["", "50"],
    "weather": [],   # network
    "recap": [],     # needs a BlueBubbles client
    "summary": [],
}


def _ctx(big, sender="+15551234567"):
    return CommandContext(
        chat_guid="iMessage;+;chat1", sender_name="Owen", sender_address=sender,
        text="", is_group=True, command="", config=big.config,
    )


def _invocations():
    for cmd in all_commands(include_hidden=True):
        for args in COMMAND_ARGS.get(cmd.name, [""]):
            yield cmd.name, args


@pytest.mark.parametrize("name,args", list(_invocations()))
def test_every_command_reply_fits_the_budget(big, name, args):
    reply = dispatch(name, args, _ctx(big))
    if not reply:
        return
    assert_fits(reply, f"!{name} {args}".strip())


@pytest.mark.parametrize("name,args", list(_invocations()))
def test_every_command_fits_once_the_header_is_composed(big, name, args):
    ctx = _ctx(big)
    reply = dispatch(name, args, ctx)
    if not reply:
        return
    wire = enforce_width(compose_message(PREFIX, ctx.title or ctx.command, reply))
    assert_fits(wire, f"!{name} on the wire")


@pytest.mark.parametrize("name,args", list(_invocations()))
def test_every_command_header_fits_on_one_line(big, name, args):
    """A title that wraps turns a two-line header into three."""
    ctx = _ctx(big)
    reply = dispatch(name, args, ctx)
    if not reply:
        return
    header = compose_message(PREFIX, ctx.title or ctx.command, reply).split("\n")[0]
    assert display_width(header) <= MAX_LINE, header


def test_the_widest_realistic_reply_is_reported_for_eyeballing(big, capsys):
    """Not an assertion so much as a record of where the headroom actually is."""
    worst = []
    for name, args in _invocations():
        reply = dispatch(name, args, _ctx(big)) or ""
        for line in reply.split("\n"):
            if len(line.split()) > 1:
                worst.append((display_width(line), f"!{name}", line))
    worst.sort(reverse=True)
    assert worst[0][0] <= MAX_LINE, f"widest: {worst[:5]}"


# ---------------------------------------------------------------------------
# the helpers themselves
# ---------------------------------------------------------------------------


def test_display_width_counts_emoji_as_two():
    assert display_width("abc") == 3
    assert display_width("🏈") == 2
    assert display_width("🥇 abc") == 6  # 2 + 1 + 3
    # A variation selector is not a column of its own.
    assert display_width("⚠️") == 2
    assert display_width("") == 0


def test_short_name_examples_from_the_brief():
    assert short_name("Ryanmd211") == "Ryanmd"
    assert display_width(short_name("charliemalone")) <= 9
    assert short_name("JubbysGiants") == "Jubbys"
    assert short_name("Diane Danvers Simmons") == "Diane"
    assert short_name("bru32") == "bru"


def test_short_name_never_exceeds_the_name_budget():
    for name in DISPLAY_NAMES + TEAM_NAMES + ["", "x", "🦅🦅🦅🦅🦅🦅🦅🦅🦅🦅"]:
        assert display_width(short_name(name)) <= 9, name


def test_short_name_is_applied_to_nicknames_too(big):
    big.store.set_nickname("u0", "The Undisputed Champion")
    view = load_view(big.client, BIG_LEAGUE, nicknames=big.store.nicknames())
    assert display_width(view.managers[1].short) <= 9


def test_colliding_short_names_are_disambiguated(client):
    """bloqus and bloqus2 must not both render as "bloqus"."""
    from conftest import LEAGUE_ID

    view = load_view(client, LEAGUE_ID)
    shorts = [m.short for m in view.managers.values()]
    assert len(shorts) == len(set(shorts)), shorts


def test_enforce_width_wraps_prose():
    long_line = "This is a sentence that is comfortably longer than thirty columns."
    out = enforce_width(long_line)
    assert len(out.split("\n")) > 1
    assert all(display_width(line) <= MAX_LINE for line in out.split("\n"))


def test_enforce_width_leaves_an_unbreakable_token_alone():
    guid = "iMessage;+;chat483920184756192837"
    assert enforce_width(guid) == guid  # a broken GUID cannot be copied


def test_enforce_width_is_idempotent():
    text = "some words that will certainly need wrapping at thirty columns wide"
    once = enforce_width(text)
    assert enforce_width(once) == once


def test_score_line_never_exceeds_the_budget():
    from ffformat import score_line

    for left in DISPLAY_NAMES:
        for right in DISPLAY_NAMES:
            line = score_line(left, 136.65, right, 124.35)
            assert display_width(line) <= MAX_LINE, line
            assert "›" in line


def test_score_line_keeps_both_names_legible():
    from ffformat import score_line

    line = score_line("charliemalone", 136.6, "finnmaloney96", 124.3)
    assert display_width(line) <= MAX_LINE
    # Neither side is trimmed to nothing to save the other.
    left, right = line.split(" › ")
    assert len(left.split()[0]) >= 4
    assert len(right.split()[0]) >= 4


def test_the_width_report_flags_an_over_budget_line():
    report = ffposts.width_report("ok\n" + "x " * 40)
    assert "OVER BUDGET" in report
    assert re.search(r"longest \d+/30", report)


def test_the_width_report_confirms_a_clean_post():
    assert "within budget" in ffposts.width_report("🏈 Week 1 final\nTKoop 100.0 › bru 90.0")
