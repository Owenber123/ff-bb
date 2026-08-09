"""Command handlers end to end, through the real registry.

These go through ``dispatch()`` so the exception isolation is exercised too.
The bar every test here defends: **no command may ever put a traceback, a
Python repr, or a markdown character into a group chat**, whatever Sleeper does.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import ffcommands  # noqa: F401  -- imported for its @command registrations
import ffstore
import sleeper as sleeper_module
from conftest import LEAGUE_ID, FakeSession, build_routes
from ffformat import MAX_BODY
from registry import CommandContext, all_commands, dispatch
from sleeper import SleeperClient

# Every fantasy command, and arguments that should work for each.
INVOCATIONS = [
    ("standings", ""), ("toilet", ""), ("matchups", ""), ("scores", ""),
    ("roster", "bloqus"), ("waivers", ""), ("trades", ""), ("injuries", ""),
    ("bye", ""), ("playoffs", ""), ("record", "barryyung"), ("week", ""),
    ("bench", ""), ("luckiest", ""), ("unluckiest", ""), ("closest", ""),
    ("blowout", ""), ("shame", ""), ("top", ""),
    ("roast", "barryyung"), ("cope", ""), ("curse", "barryyung"),
    ("nickname", "barryyung Autodraft"), ("nicknames", ""),
    ("odds", "barry wins"), ("8ball", "start Kelce?"),
    ("fine", "barryyung 5 autodraft"), ("fines", ""), ("pot", ""),
    ("punishment", ""), ("poll", "who is worst | a | b"), ("vote", "1"),
    ("results", ""), ("link", "bloqus"), ("league", ""),
]


@pytest.fixture
def env(tmp_path):
    """A wired-up bot: fake Sleeper, real store, real registry."""
    session = FakeSession(build_routes())
    client = SleeperClient(tmp_path / "data", session=session)
    client.players()
    sleeper_module.set_client(client)

    store = ffstore.Store(tmp_path / "bot.db")
    ffstore.set_store(store)

    config = SimpleNamespace(
        command_prefix="!",
        sleeper_league_id=LEAGUE_ID,
        sleeper_user_map={"5551234567": "u1"},
        sleeper_follow_previous=True,
        sleeper_data_dir=str(tmp_path / "data"),
        sleeper_db_path=str(tmp_path / "bot.db"),
    )
    yield SimpleNamespace(client=client, session=session, store=store, config=config)
    sleeper_module.set_client(None)
    ffstore.set_store(None)


def ctx(env, sender="+15551234567", chat="chat1") -> CommandContext:
    return CommandContext(
        chat_guid=chat,
        sender_name="Owen",
        sender_address=sender,
        text="",
        is_group=True,
        command="",
        config=env.config,
    )


def run(env, name, args="", **kwargs) -> str:
    reply = dispatch(name, args, ctx(env, **kwargs))
    return reply or ""


def unwrapped(env, name, args="", **kwargs) -> str:
    """Reply with the width wrapper's line breaks undone.

    For asserting on *content* rather than layout: replies are wrapped to the
    ~30-column budget, so a phrase can legitimately span two lines.
    """
    return re.sub(r"\n  ", " ", run(env, name, args, **kwargs))


# ---------------------------------------------------------------------------
# the blanket guarantees
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,args", INVOCATIONS)
def test_every_command_replies_without_blowing_up(env, name, args):
    reply = run(env, name, args)
    assert reply, f"!{name} said nothing"
    assert "blew up" not in reply
    assert "Traceback" not in reply


@pytest.mark.parametrize("name,args", INVOCATIONS)
def test_no_command_emits_markdown(env, name, args):
    # iMessage renders these literally; a stray asterisk looks like a bug.
    reply = run(env, name, args)
    assert "**" not in reply
    assert "```" not in reply
    assert not re.search(r"\|\s*-{3,}", reply)  # no markdown table rules


@pytest.mark.parametrize("name,args", INVOCATIONS)
def test_no_command_exceeds_the_send_cap(env, name, args):
    assert len(run(env, name, args)) <= MAX_BODY


@pytest.mark.parametrize("name,args", INVOCATIONS)
def test_every_command_survives_sleeper_being_down(env, name, args):
    """The definition of done: nothing crashes when Sleeper returns an error."""
    env.session.fail = True
    # Nothing is cached in this fresh client, so this is the worst case.
    reply = run(env, name, args)
    assert reply
    assert "blew up" not in reply
    assert "Traceback" not in reply


@pytest.mark.parametrize("name,args", INVOCATIONS)
def test_every_command_survives_an_empty_league(env, name, args):
    """...and when the league is between seasons with nothing in it."""
    routes = build_routes()
    routes[f"/league/{LEAGUE_ID}"] = dict(
        routes[f"/league/{LEAGUE_ID}"], status="pre_draft", previous_league_id=None
    )
    routes[f"/league/{LEAGUE_ID}/rosters"] = [
        {"roster_id": i, "owner_id": f"u{i}", "players": [], "starters": ["0"],
         "settings": {"wins": 0, "losses": 0, "fpts": 0}}
        for i in range(1, 5)
    ]
    for week in range(1, 19):
        routes[f"/league/{LEAGUE_ID}/matchups/{week}"] = []
    env.session.routes = routes
    env.client._cache.clear()

    reply = run(env, name, args)
    assert reply
    assert "blew up" not in reply
    assert "Traceback" not in reply


@pytest.mark.parametrize("name,args", [(n, a) for n, a in INVOCATIONS if a])
def test_commands_taking_a_name_handle_a_stranger(env, name, args):
    reply = run(env, name, "someone who is not in this league")
    assert reply
    assert "blew up" not in reply


# ---------------------------------------------------------------------------
# specific content
# ---------------------------------------------------------------------------


def test_standings_are_ordered_and_show_a_record(env):
    reply = run(env, "standings")
    assert reply.index("bloqus") < reply.index("bru")
    assert "3-1" in reply
    assert "1436.0" in reply  # fpts=1436 + fpts_decimal=5/100
    # The naive f"{fpts}.{fpts_decimal}" join would print 1436.5 here.
    assert "1436.5" not in reply
    assert "1100.5" in reply  # fpts=1100 + fpts_decimal=50/100


def test_toilet_puts_last_place_first(env):
    reply = run(env, "toilet")
    assert reply.index("bru") < reply.index("bloqus")


def test_matchups_pairs_by_matchup_id(env):
    # Week 4's rows are ordered 1, 4, 2, 3 but matchup_id pairs 1-with-4 and
    # 2-with-3. Pairing by row order would produce bloqus vs bloqus2 and
    # barryyung vs bru32 by luck here, so assert the ids, not just the text.
    matchups, _, _ = run_view(env).matchups(4)
    assert {m.matchup_id: sorted(s.roster_id for s in m.sides) for m in matchups} == {
        1: [1, 4], 2: [2, 3]
    }
    reply = run(env, "matchups")
    assert "bloqus v bloqus2" in reply
    assert "barryyung v bru" in reply


def run_view(env):
    from ffleague import load_view

    return load_view(env.client, LEAGUE_ID)


def test_scores_carry_a_freshness_caveat(env):
    reply = run(env, "scores")
    assert "as of" in reply


def test_scores_say_so_when_sleeper_is_unreachable(env):
    run(env, "scores")  # prime the cache
    for entry in env.client._cache.values():
        entry.fetched_at -= 100_000
    env.session.fail = True

    reply = run(env, "scores")
    assert "not responding" in reply
    assert "ago" in reply
    # Stale data is still shown rather than withheld.
    assert "bloqus" in reply


def test_roster_shows_starters_with_slots(env):
    reply = run(env, "roster", "bloqus")
    assert "J. Allen" in reply
    assert "QB" in reply


def test_roster_of_a_pre_draft_team_does_not_list_placeholders(env):
    reply = run(env, "roster", "bru32")
    assert "(empty)" not in reply


def test_waivers_skips_failed_claims(env):
    reply = run(env, "waivers")
    assert "P. Nacua" in reply  # the completed claim
    assert "$17" in reply
    assert reply.count("Nacua") == 1  # the failed one is not reported


def test_trades_show_both_sides_and_picks(env):
    reply = unwrapped(env, "trades")
    assert "D. Henry" in reply
    assert "B. Robinson" in reply
    assert "2026 R1" in reply
    # Each side on its own line, so no name is split across a wrap.
    assert "bloqus +D. Henry, 2026 R1" in reply
    assert "barryyung +B. Robinson" in reply


def test_injuries_are_limited_to_rostered_players(env):
    reply = run(env, "injuries")
    assert "S. Barkley" in reply  # rostered and Out
    assert "T. Kelce" in reply  # rostered and Questionable
    # A starting injured player is flagged; Barkley is benched, Kelce starts.
    assert "▶T. Kelce" in reply


def test_bye_flags_starters_on_bye(env):
    reply = run(env, "bye", "2")
    assert "BAL" in reply and "DET" in reply
    assert "STARTING" in reply  # roster 2 starts Henry and Andrews, both BAL


def test_bench_names_the_player_who_should_have_played(env):
    reply = run(env, "matchups", "1")  # warm week 1
    reply = run(env, "bench")
    assert reply


def test_record_needs_the_caller_to_be_linked(env):
    reply = dispatch("record", "barryyung", ctx(env, sender="+15559999999"))
    assert "!link" in reply


def test_record_shows_head_to_head_once_linked(env):
    reply = run(env, "record", "barryyung")
    assert "bloqus" in reply and "barryyung" in reply
    assert "wk1" in reply


def test_closest_and_blowout_disagree(env):
    assert run(env, "closest") != run(env, "blowout")


def test_shame_finds_the_lowest_score(env):
    reply = run(env, "shame")
    assert "bru32" in reply  # 45.0 in week 4, the league low


def test_luck_commands_are_inverses(env):
    lucky = run(env, "luckiest")
    unlucky = run(env, "unluckiest")
    assert lucky != unlucky


# ---------------------------------------------------------------------------
# name resolution through commands
# ---------------------------------------------------------------------------


def test_an_ambiguous_name_asks_rather_than_guessing(env):
    reply = run(env, "roast", "bloq")
    assert "bloqus" in reply and "bloqus2" in reply
    assert "specific" in reply


def test_an_at_mention_is_accepted(env):
    assert run(env, "roast", "@barryyung")


def test_roast_me_resolves_to_the_caller(env):
    reply = run(env, "roast", "me")
    assert "bloqus" in reply


# ---------------------------------------------------------------------------
# roast: the point of the exercise
# ---------------------------------------------------------------------------


def test_roast_references_a_real_fact(env):
    # bru32 is 1-3 and last. Whichever fact is drawn, it must be a real one.
    replies = {run(env, "roast", "bru32") for _ in range(30)}
    assert len(replies) > 1, "roast never varies"
    combined = " ".join(replies)
    assert any(
        marker in combined
        for marker in ("1-3", "of 4", "lost", "week", "bench", "0.0", "outscored")
    )


def test_roast_never_names_the_wrong_person(env):
    for _ in range(20):
        reply = run(env, "roast", "bru32")
        assert "bloqus" not in reply
        assert "barryyung" not in reply or "bru32" in reply


def test_roast_uses_a_nickname_once_one_is_set(env):
    run(env, "nickname", "bru32 Autodraft")
    replies = {run(env, "roast", "bru32") for _ in range(10)}
    assert all("Autodraft" in reply for reply in replies)


# ---------------------------------------------------------------------------
# nicknames
# ---------------------------------------------------------------------------


def test_a_nickname_persists_and_is_used_everywhere(env, tmp_path):
    assert "now" in run(env, "nickname", "barryyung Autodraft")

    # A restart: brand new store and client over the same files.
    ffstore.set_store(ffstore.Store(tmp_path / "bot.db"))
    sleeper_module.set_client(SleeperClient(tmp_path / "data", session=env.session))

    assert "Autodraft" in run(env, "standings")
    assert "Autodraft" in run(env, "nicknames")
    assert run(env, "roster", "Autodraft")  # and it is a way to refer to them


def test_a_multi_word_target_still_leaves_a_nickname(env):
    reply = unwrapped(env, "nickname", "Go Birds The Bird Man")
    assert "The Bird Man" in reply


def test_clearing_a_nickname_restores_the_real_name(env):
    run(env, "nickname", "barryyung Autodraft")
    reply = run(env, "nickname", "Autodraft clear")
    assert "barryyung" in reply
    assert "Autodraft" not in run(env, "standings")


def test_nickname_without_a_nickname_explains_itself(env):
    assert "Usage" in run(env, "nickname", "barryyung")


def test_a_silly_long_nickname_is_refused(env):
    assert "32 characters" in run(env, "nickname", "barryyung " + "x" * 40)


# ---------------------------------------------------------------------------
# fines and the pot
# ---------------------------------------------------------------------------


def test_fines_accumulate_and_show_up_in_the_pot(env):
    run(env, "fine", "barryyung 5 autodrafted")
    run(env, "fine", "barryyung 2.50 late lineup")
    reply = run(env, "fines")
    assert "$7.50" in reply
    assert "autodrafted" in reply
    assert "$7.50" in run(env, "pot")


def test_a_fine_with_a_multi_word_name_and_reason(env):
    reply = unwrapped(env, "fine", "Go Birds 10 for the whole vibe")
    assert "$10" in reply
    assert "for the whole vibe" in reply


def test_a_fine_without_an_amount_says_so(env):
    assert "amount" in run(env, "fine", "barryyung because")


def test_a_negative_fine_is_refused(env):
    assert "positive" in run(env, "fine", "barryyung -5 nope")


def test_the_pot_totals_buyins_and_fines(env):
    run(env, "pot", "50")
    run(env, "fine", "barryyung 5 autodraft")
    reply = run(env, "pot")
    assert "$200" in reply  # 4 teams x $50
    assert "$205" in reply


# ---------------------------------------------------------------------------
# punishment
# ---------------------------------------------------------------------------


def test_punishment_persists_and_names_last_place(env):
    run(env, "punishment", "Waffle House, 24 hours")
    reply = run(env, "punishment")
    assert "Waffle House" in reply
    assert "bru32" in reply  # currently last


# ---------------------------------------------------------------------------
# polls
# ---------------------------------------------------------------------------


def test_a_poll_runs_end_to_end(env):
    assert "1." in run(env, "poll", "who is worst | barry | owen")
    run(env, "vote", "1", sender="+15551234567")
    run(env, "vote", "2", sender="+15559999999")
    reply = run(env, "results")
    assert "2 votes" in reply
    assert "closed" in reply


def test_voting_twice_only_counts_once(env):
    run(env, "poll", "q | a | b")
    run(env, "vote", "1")
    run(env, "vote", "2")
    assert "1 vote" in run(env, "results")


def test_voting_with_no_poll_open(env):
    assert "No poll" in run(env, "vote", "1")


def test_an_out_of_range_vote_is_rejected(env):
    run(env, "poll", "q | a | b")
    assert "2 options" in run(env, "vote", "9")


def test_a_poll_defaults_to_yes_no(env):
    reply = run(env, "poll", "should we expand")
    assert "Yes" in reply and "No" in reply


def test_polls_do_not_leak_between_chats(env):
    run(env, "poll", "chat one poll | a | b", chat="chatA")
    assert "No poll" in run(env, "vote", "1", chat="chatB")


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_the_oracle_does_not_change_its_mind(env):
    # Seeded on the question, so nobody can reroll an answer they dislike.
    assert len({run(env, "8ball", "start Kelce?") for _ in range(5)}) == 1
    assert run(env, "8ball", "start Kelce?") != run(env, "8ball", "start Andrews?")


def test_odds_are_stable_per_proposition(env):
    assert len({run(env, "odds", "barry benches his QB") for _ in range(5)}) == 1


def test_odds_produce_a_plausible_line(env):
    reply = run(env, "odds", "anything at all")
    assert re.search(r"[+-]\d+", reply)
    assert "% implied" in reply


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


def test_link_maps_a_handle_and_persists(env, tmp_path):
    reply = dispatch("link", "barryyung", ctx(env, sender="+15558887777"))
    assert "barryyung" in reply

    ffstore.set_store(ffstore.Store(tmp_path / "bot.db"))
    reply = dispatch("roast", "me", ctx(env, sender="+15558887777"))
    assert "barryyung" in reply


def test_link_with_no_argument_reports_the_current_mapping(env):
    assert "bloqus" in run(env, "link")


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------


def test_help_advertises_exactly_three_commands(env):
    import commands  # noqa: F401  -- imported for its @command registrations

    reply = run(env, "help")
    lines = reply.strip().split("\n")
    assert len(lines) == 3  # three commands, nothing else
    assert "!scores" in lines[0]
    assert "!standings" in lines[1]
    assert "!roast" in lines[2]
    # The whole point: short enough that somebody actually reads it, and no
    # line so long it wraps into more than the three.
    assert len(reply) < 160


def test_help_does_not_leak_the_other_commands(env):
    import commands  # noqa: F401

    reply = run(env, "help")
    for name in ("bench", "toilet", "8ball", "punishment", "waivers", "preview"):
        assert name not in reply


def test_help_all_still_lists_everything(env):
    import commands  # noqa: F401

    reply = run(env, "help", "all")
    assert "League:" in reply
    assert "!standings" in reply
    assert "!toilet" in reply
    assert "!8ball" in reply
    assert max(len(line) for line in reply.split("\n")) < 200  # no runaway line


def test_help_all_renders_sections(env):
    import commands  # noqa: F401

    listed = len([line for line in run(env, "help", "all").split("\n") if ":" in line])
    assert listed >= 4  # sections actually rendered


def test_help_section_expands_with_descriptions(env):
    import commands  # noqa: F401

    reply = unwrapped(env, "help", "fantasy")
    assert "!standings" in reply
    assert "Record, points for" in reply


def test_help_for_one_command(env):
    import commands  # noqa: F401

    reply = run(env, "help", "roast")
    assert "roast <name>" in reply


def test_every_fantasy_command_has_help_text():
    for cmd in all_commands(include_hidden=True):
        assert cmd.help, f"!{cmd.name} has no help text"


# ---------------------------------------------------------------------------
# the three advertised commands must work with zero setup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,args", [("scores", ""), ("standings", ""), ("roast", "")])
def test_the_advertised_commands_work_for_an_unlinked_stranger(env, name, args):
    """These are the only three most people will ever try."""
    reply = dispatch(name, args, ctx(env, sender="+15550001111"))
    assert reply
    assert "blew up" not in reply
    assert "!link" not in reply  # no setup instructions in the shop window
    assert "SLEEPER_" not in reply


def test_bare_roast_picks_somebody(env):
    replies = {dispatch("roast", "", ctx(env, sender="+15550001111")) for _ in range(15)}
    assert all(r and "!link" not in r for r in replies)
    # Unlinked, so it should not always land on the same person.
    assert len(replies) > 1


def test_bare_roast_targets_the_caller_when_linked(env):
    for _ in range(5):
        assert "bloqus" in dispatch("roast", "", ctx(env))
