"""Fantasy football commands.

Same contract as commands.py: one decorator, one function, nothing in
registry.py changes. Return a string to reply, None to stay silent, and raising
is survivable -- though almost nothing here raises, because a traceback in a
group chat is worse than a bot that admits it cannot reach Sleeper.

Every handler that touches league data is wrapped in :func:`league_command`,
which:

  * builds the joined :class:`LeagueView` once (2-4 cached API calls)
  * turns every :class:`SleeperError` into plain words
  * turns a name that matched nobody, or several people, into a useful hint
  * short-circuits a league that has not drafted yet

so the handler bodies below are just the interesting part.
"""

from __future__ import annotations

import functools
import hashlib
import random
import time
from typing import Callable

import ffposts
import ffroast
from ffformat import (
    DOT,
    MAX_LINE,
    fit,
    money,
    normalize_handle,
    ordinal,
    parse_money,
    plural,
    pts,
    rank_marker,
    score_line,
    short_name,
    signed,
    strip_mention,
    truncate,
    with_freshness,
)
from ffleague import (
    LeagueView,
    Manager,
    NameAmbiguous,
    NameNotFound,
    expected_wins,
    head_to_head,
    injury_rank,
    load_view,
    optimal_lineup,
    streak,
)
from ffstore import get_store
from logging_setup import get_logger
from registry import CommandContext, command
from sleeper import (
    LeagueNotFound,
    SleeperError,
    SleeperNotConfigured,
    describe_age,
    get_client,
)

log = get_logger(__name__)

# !help groups by these. Four buckets rather than one "fantasy" pile, because
# thirty commands on a single line is a paragraph, not a menu.
LEAGUE = "ff-league"   # what Sleeper says happened
STATS = "ff-stats"     # what we derived from it
TALK = "ff-talk"       # trash
ADMIN = "ff-admin"     # the pot, polls, setup

MAX_TRANSACTION_WEEKS = 3  # how far back !waivers / !trades will walk


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def _client(ctx: CommandContext):
    return get_client(getattr(ctx.config, "sleeper_data_dir", None))


def _store(ctx: CommandContext):
    return get_store(getattr(ctx.config, "sleeper_db_path", None))


def _links(ctx: CommandContext) -> dict[str, str]:
    """Chat handle -> Sleeper user_id.

    The environment map is the base; anything set with !link overrides it, so
    somebody can fix their own mapping without a redeploy.
    """
    merged = dict(getattr(ctx.config, "sleeper_user_map", {}) or {})
    try:
        merged.update(_store(ctx).links())
    except Exception:  # a broken DB must not take out every command
        log.exception("could not read handle links from the store")
    return merged


def _build_view(ctx: CommandContext) -> LeagueView:
    cfg = ctx.config
    store = _store(ctx)
    try:
        nicknames = store.nicknames()
    except Exception:
        log.exception("could not read nicknames; continuing without them")
        nicknames = {}
    return load_view(
        _client(ctx),
        getattr(cfg, "sleeper_league_id", ""),
        nicknames=nicknames,
        follow_previous=getattr(cfg, "sleeper_follow_previous", True),
    )


def league_command(*, needs_games: bool = True) -> Callable:
    """Wrap a handler so it receives a ready LeagueView and cannot leak errors."""

    def decorator(fn: Callable[[str, CommandContext, LeagueView], str | None]):
        @functools.wraps(fn)
        def wrapper(args: str, ctx: CommandContext) -> str | None:
            try:
                view = _build_view(ctx)
            except SleeperNotConfigured as exc:
                return str(exc)
            except LeagueNotFound as exc:
                return f"{exc}. Check SLEEPER_LEAGUE_ID in .env."
            except SleeperError as exc:
                # Everything left really is an outage, so say that and nothing
                # more -- a config problem reported as "Sleeper is down" sends
                # somebody debugging the wrong thing.
                return f"Sleeper is not responding right now. ({exc})"

            if needs_games and not view.has_games:
                return "Not drafted yet — nothing to show."

            try:
                return fn(args, ctx, view)
            except NameAmbiguous as exc:
                options = ", ".join(exc.options[:6])
                return f"{exc.query!r} matches {options}. Be more specific."
            except NameNotFound as exc:
                return str(exc)
            except SleeperError as exc:
                return f"Sleeper is not responding right now. ({exc})"

        return wrapper

    return decorator


def _head(ctx: CommandContext, view: LeagueView, title: str) -> list[str]:
    """Set the message header title, and return any body preamble.

    The title goes in the ``🤖 bloqBot - <title>`` header line that the send
    path composes, so it must not be repeated in the body. Titles are short
    identifiers, not sentences -- the prefix eats about half the header line.

    Returns a list so a caller can splice it in: empty most of the time, one
    caveat line when the data is last season's.
    """
    ctx.title = title
    if view.is_previous_season:
        return [f"({view.season} data)", ""]
    return []


def _me(ctx: CommandContext, view: LeagueView) -> Manager | None:
    return view.caller(ctx.sender_address or ctx.sender_name, _links(ctx))


def _resolve(args: str, ctx: CommandContext, view: LeagueView) -> Manager:
    return view.resolve(
        strip_mention(args),
        caller_handle=ctx.sender_address or ctx.sender_name,
        links=_links(ctx),
    )


def _scoring_week(view: LeagueView) -> tuple[int, list, float, bool]:
    """The most recent week that actually has points on the board.

    On a Tuesday, or in the preseason, the "current" week is all zeroes and a
    superlative over it is meaningless. Step back at most one week -- that is
    one extra cached call, not a scan.
    """
    week = view.week
    matchups, age, stale = view.matchups(week)
    if any(side.points for m in matchups for side in m.sides):
        return week, matchups, age, stale
    if week > 1:
        previous = week - 1
        older, age2, stale2 = view.matchups(previous)
        if any(side.points for m in older for side in m.sides):
            return previous, older, age2, stale2
    return week, matchups, age, stale


def _seeded(text: str) -> random.Random:
    """Deterministic per-question randomness.

    The oracle does not change its mind because you asked again.
    """
    digest = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


# ---------------------------------------------------------------------------
# league data
# ---------------------------------------------------------------------------


@command(
    "standings",
    help="Record, points for, and current streak.",
    aliases=("table", "st"),
    category=LEAGUE,
)
@league_command()
def cmd_standings(args: str, ctx: CommandContext, view: LeagueView) -> str:
    results = view.season_results()
    lines = _head(ctx, view, f"standings wk{view.week}")
    for i, manager in enumerate(view.sorted_managers()):
        run = streak(results.get(manager.roster_id, []))
        lines.append(
            f"{rank_marker(i)} {manager.short} {manager.record}"
            f" {pts(manager.fpts)} {run}"
        )
    return with_freshness(fit(lines), view.age, view.stale)


@command("toilet", help="Standings, from the bottom.", aliases=("bottom",), category=LEAGUE)
@league_command()
def cmd_toilet(args: str, ctx: CommandContext, view: LeagueView) -> str:
    ranked = view.sorted_managers()
    total = len(ranked)
    lines = _head(ctx, view, "toilet") + ["🚽 Bottom up"]
    for offset, manager in enumerate(reversed(ranked)):
        place = total - offset
        marker = "💩" if offset == 0 else f"{ordinal(place)}"
        lines.append(
            f"{marker} {manager.short} {manager.record} {pts(manager.fpts)}"
        )
    return with_freshness(fit(lines), view.age, view.stale)


@command("matchups", help="This week's pairings.", aliases=("mu",), category=LEAGUE)
@league_command()
def cmd_matchups(args: str, ctx: CommandContext, view: LeagueView) -> str:
    week = view.week
    if args.strip().isdigit():
        week = max(1, min(int(args.strip()), 18))
    matchups, age, stale = view.matchups(week)
    if not matchups:
        return f"No matchups posted for week {week} yet."

    lines = _head(ctx, view, f"matchups wk{week}")
    for matchup in matchups:
        if not matchup.complete:
            only = matchup.sides[0]
            lines.append(f"{only.short}{DOT}no opponent")
            continue
        a, b = matchup.sides
        lines.append(f"{a.short} v {b.short}")
    return with_freshness(fit(lines), age, stale)


@command("scores", help="Live scoring for the current week.", aliases=("score", "live"), category=LEAGUE)
@league_command()
def cmd_scores(args: str, ctx: CommandContext, view: LeagueView) -> str:
    week, matchups, age, stale = _scoring_week(view)
    if not matchups:
        return f"Nothing scored in week {week} yet."

    lines = _head(ctx, view, f"scores wk{week}")
    for matchup in sorted(matchups, key=lambda m: -m.total):
        if not matchup.complete:
            only = matchup.sides[0]
            lines.append(f"{only.short} {pts(only.points)}")
            continue
        high, low = matchup.ordered()
        lines.append(score_line(high.short, high.points, low.short, low.points))

    # The caveat is the point: say the scores are minutes old rather than
    # implying they are live.
    if stale:
        lines.append(f"⚠️ Sleeper is not responding — scores as of {describe_age(age)}")
    elif not view.is_week_final(week):
        lines.append(f"Scores as of {describe_age(age)}")
    return fit(lines)


@command("roster", help="Someone's current starters.", usage="roster <name>", category=LEAGUE)
@league_command()
def cmd_roster(args: str, ctx: CommandContext, view: LeagueView) -> str:
    manager = _resolve(args, ctx, view)
    client = view.client

    # Prefer the lineup as submitted for the scoring week; fall back to the
    # roster object when there are no matchups (preseason, or a fresh week).
    starters: list[tuple[str, float | None]] = []
    week = view.week
    try:
        week, matchups, _, _ = _scoring_week(view)
        for matchup in matchups:
            for side in matchup.sides:
                if side.roster_id == manager.roster_id:
                    starters = [(pid, score) for pid, score in side.started()]
    except SleeperError:
        pass
    if not starters:
        starters = [(pid, None) for pid in manager.starters]

    if not starters:
        return f"{manager.name} has no lineup set."

    slots = view.starting_slots
    lines = _head(ctx, view, f"{manager.short} wk{week}")

    # The cached player file holds *today's* team and injury status. Painting
    # those onto a finished season's lineup produces confident nonsense
    # ("M. Evans (SF) Questionable" for a game played months ago), so they are
    # only shown when the lineup and the player data describe the same season.
    annotate = not view.is_previous_season

    for i, (pid, score) in enumerate(starters):
        slot = slots[i] if i < len(slots) else "FLEX"
        suffix = f" {pts(score)}" if score is not None else ""
        tail = ""
        if annotate:
            meta = client.player_meta(pid)
            team = meta.get("team") or ""
            injury = meta.get("injury_status") or ""
            if team:
                tail = f" ({team})"
            if injury:
                tail += f" {injury}"
        lines.append(f"{slot:<4} {truncate(client.player_name(pid, short=True), 18)}{tail}{suffix}")

    total = sum(s for _, s in starters if s is not None)
    if total:
        lines.append(f"Total {pts(total)}")
    return fit(lines)


@command("waivers", help="Recent adds and drops.", aliases=("adds",), category=LEAGUE)
@league_command()
def cmd_waivers(args: str, ctx: CommandContext, view: LeagueView) -> str:
    client = view.client
    moves: list[str] = []
    weeks_seen: list[int] = []

    for offset in range(MAX_TRANSACTION_WEEKS):
        week = view.week - offset
        if week < 1:
            break
        weeks_seen.append(week)
        entry = client.transactions(view.league_id, week)
        rows = entry.value if isinstance(entry.value, list) else []
        for row in sorted(rows, key=lambda r: r.get("status_updated") or 0, reverse=True):
            if row.get("type") not in ("waiver", "free_agent"):
                continue
            if row.get("status") != "complete":
                continue  # failed waiver claims are not news
            roster_ids = row.get("roster_ids") or []
            manager = view.managers.get(int(roster_ids[0])) if roster_ids else None
            who = manager.name if manager else "someone"
            adds = list((row.get("adds") or {}).keys())
            drops = list((row.get("drops") or {}).keys())
            bid = (row.get("settings") or {}).get("waiver_bid")

            parts = []
            if adds:
                parts.append("+" + ", ".join(client.player_name(p, short=True) for p in adds[:2]))
            if drops:
                parts.append("−" + ", ".join(client.player_name(p, short=True) for p in drops[:2]))
            if not parts:
                continue
            cost = f" (${bid})" if bid else ""
            moves.append(f"{short_name(who)}{DOT}{' '.join(parts)}{cost}")
        if len(moves) >= 8:
            break

    if not moves:
        span = f"week {weeks_seen[0]}" if len(weeks_seen) == 1 else f"weeks {weeks_seen[-1]}-{weeks_seen[0]}"
        return f"No completed adds or drops in {span}."

    lines = _head(ctx, view, "waivers") + moves[:8]
    return fit(lines)


@command("trades", help="Recent and pending trades.", aliases=("trade",), category=LEAGUE)
@league_command()
def cmd_trades(args: str, ctx: CommandContext, view: LeagueView) -> str:
    client = view.client
    entries: list[str] = []

    for offset in range(MAX_TRANSACTION_WEEKS):
        week = view.week - offset
        if week < 1:
            break
        entry = client.transactions(view.league_id, week)
        rows = entry.value if isinstance(entry.value, list) else []
        for row in sorted(rows, key=lambda r: r.get("status_updated") or 0, reverse=True):
            if row.get("type") != "trade":
                continue
            status = str(row.get("status") or "")
            if status not in ("complete", "pending"):
                continue

            adds = row.get("adds") or {}
            by_roster: dict[int, list[str]] = {}
            for player_id, roster_id in adds.items():
                by_roster.setdefault(int(roster_id), []).append(str(player_id))

            picks = row.get("draft_picks") or []
            for pick in picks:
                owner = pick.get("owner_id")
                if owner is None:
                    continue
                label = f"{pick.get('season')} R{pick.get('round')}"
                by_roster.setdefault(int(owner), []).append(f"pick:{label}")

            if not by_roster:
                continue

            # One side per line. As a single "A gets X | B gets Y" sentence
            # this wrapped across three lines and split a player's name in
            # half, which is exactly what the budget exists to prevent.
            flag = " ⏳" if status == "pending" else ""
            entries.append(f"wk{week}{flag}")
            for roster_id, items in by_roster.items():
                manager = view.managers.get(roster_id)
                got = ", ".join(
                    item.split("pick:")[1] if item.startswith("pick:") else client.player_name(item, short=True)
                    for item in items[:2]
                )
                label = manager.short if manager else f"R{roster_id}"
                entries.append(f"{label} +{got}")
            entries.append("")
        if len(entries) >= 9:
            break

    if not entries:
        return f"No trades in {MAX_TRANSACTION_WEEKS} weeks. Cowards."
    return fit(_head(ctx, view, "trades") + entries)


@command("injuries", help="Injury report, rostered players only.", aliases=("inj", "ir"), category=LEAGUE)
@league_command()
def cmd_injuries(args: str, ctx: CommandContext, view: LeagueView) -> str:
    client = view.client
    if not client.players(allow_fetch=False):
        return "Player data is still loading. Try again in a minute."

    # Injury status is always current. Against last season's final rosters that
    # is a curiosity, not a report, and saying so beats implying otherwise.
    if view.is_previous_season:
        return (
            f"It is the off-season — {view.name} last played in {view.season}, "
            f"so there is no meaningful injury report yet."
        )

    rows: list[tuple[int, str]] = []
    for manager in view.sorted_managers():
        hurt: list[tuple[int, str]] = []
        starting = set(manager.starters)
        for player_id in manager.players:
            meta = client.player_meta(player_id)
            status = str(meta.get("injury_status") or "")
            if not status:
                continue
            rank = injury_rank(status)
            if rank > 6:
                continue
            marker = "▶" if player_id in starting else " "
            hurt.append((rank, f"{marker}{client.player_name(player_id, short=True)} {status}"))
        if hurt:
            hurt.sort()
            for _, entry in hurt[:3]:
                rows.append((len(hurt), f"{manager.short}{DOT}{entry}"))

    if not rows:
        return "Nobody rostered is on the injury report. Suspicious."
    lines = _head(ctx, view, "injuries") + ["(▶ = starting)", ""]
    lines.extend(text for _, text in rows)
    return fit(lines, note="more not shown")


@command("bye", help="Who is on bye this week.", aliases=("byes",), category=LEAGUE)
@league_command()
def cmd_bye(args: str, ctx: CommandContext, view: LeagueView) -> str:
    week = view.week
    if args.strip().isdigit():
        week = max(1, min(int(args.strip()), 18))

    client = view.client
    on_bye = client.teams_on_bye(view.season, week)
    if not on_bye:
        return f"No teams on bye in week {week} of {view.season}."

    lines = _head(ctx, view, f"byes wk{week}") + ["Teams: " + ", ".join(sorted(on_bye)), ""]

    # Per-manager counts need player -> NFL team, which is current-day data.
    # Against a finished season's rosters it would be wrong often enough to be
    # useless, so the team list stands alone out of season.
    if view.is_previous_season:
        return fit(lines)

    hits: list[tuple[int, str]] = []
    for manager in view.managers.values():
        affected = [
            p for p in manager.players if str(client.player_meta(p).get("team") or "") in on_bye
        ]
        starting = [p for p in manager.starters if p in affected]
        if not affected:
            continue
        note = f"{len(affected)} out"
        if starting:
            note += f", {len(starting)} STARTING ⚠️"
        hits.append((len(starting) * 100 + len(affected), f"{manager.short}{DOT}{note}"))

    if hits:
        hits.sort(reverse=True)
        lines.extend(text for _, text in hits[:8])
    return fit(lines)


@command("playoffs", help="Playoff picture, or the bracket once it exists.", aliases=("bracket",), category=LEAGUE)
@league_command()
def cmd_playoffs(args: str, ctx: CommandContext, view: LeagueView) -> str:
    spots = view.playoff_teams
    ranked = view.sorted_managers()

    if view.in_playoffs or view.status == "complete":
        try:
            bracket = view.client.winners_bracket(view.league_id).value or []
        except SleeperError:
            bracket = []
        if bracket:
            lines = _head(ctx, view, "bracket")
            for game in sorted(bracket, key=lambda g: (g.get("r") or 0, g.get("m") or 0)):
                one = _bracket_team(view, game.get("t1"), game.get("t1_from"))
                two = _bracket_team(view, game.get("t2"), game.get("t2_from"))
                winner = view.managers.get(game.get("w")) if game.get("w") else None
                place = game.get("p")
                label = f"R{game.get('r')}"
                if place:
                    label += f" ({ordinal(int(place))} place)"
                result = f" → {winner.short}" if winner else ""
                lines.append(f"{label}{DOT}{one} vs {two}{result}")
            return fit(lines)

    lines = _head(ctx, view, "playoffs") + [f"Top {spots} make it", ""]
    for i, manager in enumerate(ranked):
        if i == spots:
            lines.append("— cut line —")
        marker = "✅" if i < spots else "❌"
        gap = ""
        if i >= spots and spots <= len(ranked):
            bubble = ranked[spots - 1]
            behind = bubble.wins - manager.wins
            if behind > 0:
                gap = f"{DOT}{behind} back"
        lines.append(f"{marker} {manager.short} {manager.record}{gap}")
    lines.append(f"Playoffs start week {view.playoff_week_start}")
    return with_freshness(fit(lines), view.age, view.stale)


def _bracket_team(view: LeagueView, value, source) -> str:
    """Bracket slots hold a roster_id, or a {'w': m} / {'l': m} placeholder."""
    if isinstance(value, int):
        manager = view.managers.get(value)
        return manager.short if manager else f"R{value}"
    if isinstance(source, dict):
        if "w" in source:
            return f"W of m{source['w']}"
        if "l" in source:
            return f"L of m{source['l']}"
    return "TBD"


@command("record", help="Head-to-head history with someone.", usage="record <name>", aliases=("h2h",), category=LEAGUE)
@league_command()
def cmd_record(args: str, ctx: CommandContext, view: LeagueView) -> str:
    them = _resolve(args, ctx, view)
    me = _me(ctx, view)
    if me is None:
        return "I do not know your Sleeper account.\nUse !link <your name>."
    if me.roster_id == them.roster_id:
        return f"{me.name} is undefeated against {me.name}."

    results = view.season_results()
    wins, losses, ties, games = head_to_head(results, me.roster_id, them.roster_id)
    if not games:
        return f"{me.name} and {them.name} have not played this season."

    lines = [
        *_head(ctx, view, "record"),
        f"{me.short} v {them.short}",
        f"{me.name} leads {wins}-{losses}" if wins > losses else
        f"{them.name} leads {losses}-{wins}" if losses > wins else
        f"Dead even at {wins}-{losses}",
    ]
    for game in games:
        lines.append(
            f"wk{game.week}{DOT}{pts(game.points)} - {pts(game.opponent_points)}{DOT}{game.outcome}"
        )
    total_mine = sum(g.points for g in games)
    total_theirs = sum(g.opponent_points for g in games)
    lines.append(f"Points: {pts(total_mine)} - {pts(total_theirs)}")
    return fit(lines)


@command("week", help="What week it is, and what the bot is showing.", hidden=True, category=LEAGUE)
def cmd_week(args: str, ctx: CommandContext) -> str:
    client = _client(ctx)
    try:
        state = client.state().value or {}
    except SleeperError as exc:
        return f"Sleeper is not responding right now. ({exc})"

    lines = [
        f"NFL: {state.get('season')} {state.get('season_type')}, week {state.get('week')}",
    ]
    try:
        view = _build_view(ctx)
    except SleeperError as exc:
        lines.append(f"League: unavailable ({exc})")
        return "\n".join(lines)

    lines.append(f"League: {view.name} ({view.season}, {view.status})")
    lines.append(f"Showing week {view.week}")
    if view.is_previous_season:
        lines.append(f"Configured league {view.configured_league_id} has not drafted; using {view.league_id}")
    players = client.players(allow_fetch=False)
    lines.append(f"Player file: {len(players)} players" if players else "Player file: not loaded yet")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# derived stats
# ---------------------------------------------------------------------------


@command("bench", help="Points left on the bench this week.", aliases=("benched",), category=STATS)
@league_command()
def cmd_bench(args: str, ctx: CommandContext, view: LeagueView) -> str:
    week, matchups, age, stale = _scoring_week(view)
    client = view.client
    slots = view.starting_slots

    # Without positions every slot is ineligible, the optimal lineup comes out
    # as zero, and the honest answer is "I cannot work this out yet" rather
    # than "nobody benched anything".
    if not client.players(allow_fetch=False):
        return "Player data is still loading. Try again in a minute."

    rows: list[tuple[float, str]] = []
    for matchup in matchups:
        for side in matchup.sides:
            if not side.players_points:
                continue
            positions = {
                pid: str(client.player_meta(pid).get("position") or "") for pid in side.players_points
            }
            best, lineup = optimal_lineup(slots, side.players_points, positions)
            left = best - side.points
            if left <= 0:
                continue
            started = {pid for pid, _ in side.started()}
            missed = [
                (score, pid) for _, pid, score in lineup if pid not in started and score > 0
            ]
            missed.sort(reverse=True)
            # The benched player's own score is dropped: naming who should
            # have played is the interesting half, and keeping both put this
            # line eight columns over budget.
            worst = f"{DOT}{client.player_name(missed[0][1], short=True)}" if missed else ""
            rows.append((left, f"{side.short} {pts(left)}{worst}"))

    if not rows:
        return f"Nobody left anything on the bench in week {week}. Suspicious."

    rows.sort(reverse=True)
    lines = _head(ctx, view, f"bench wk{week}")
    lines.extend(text for _, text in rows[:10])
    return with_freshness(fit(lines), age, stale)


def _luck_table(view: LeagueView) -> list[tuple[float, Manager, float]]:
    """(wins above expectation, manager, expected wins), luckiest first."""
    weeks = view.regular_weeks()
    results = view.season_results(weeks)
    expected = expected_wins(results, weeks)
    table = [
        (manager.wins - expected.get(rid, 0.0), manager, expected.get(rid, 0.0))
        for rid, manager in view.managers.items()
    ]
    table.sort(key=lambda row: row[0], reverse=True)
    return table


@command("luckiest", help="Best record relative to points scored.", aliases=("lucky",), category=STATS)
@league_command()
def cmd_luckiest(args: str, ctx: CommandContext, view: LeagueView) -> str:
    table = _luck_table(view)
    if not table:
        return "Not enough games played to judge anybody's luck."
    lines = _head(ctx, view, "luckiest") + ["actual W vs earned W", ""]
    for i, (delta, manager, expected) in enumerate(table[:5]):
        lines.append(
            f"{rank_marker(i)} {manager.short} {manager.wins}W"
            f" vs {expected:.1f} {signed(delta)}"
        )
    return fit(lines)


@command("unluckiest", help="High scores, bad record.", aliases=("unlucky",), category=STATS)
@league_command()
def cmd_unluckiest(args: str, ctx: CommandContext, view: LeagueView) -> str:
    table = _luck_table(view)
    if not table:
        return "Not enough games played to judge anybody's luck."
    lines = _head(ctx, view, "unluckiest") + ["earned W vs actual W", ""]
    for i, (delta, manager, expected) in enumerate(reversed(table[-5:])):
        lines.append(
            f"{rank_marker(i)} {manager.short} {expected:.1f}"
            f" vs {manager.wins}W {signed(delta)}"
        )
    return fit(lines)


@command("closest", help="Narrowest margin this week.", category=STATS)
@league_command()
def cmd_closest(args: str, ctx: CommandContext, view: LeagueView) -> str:
    return _margin_reply(ctx, view, closest=True)


@command("blowout", help="Widest margin this week.", aliases=("beatdown",), category=STATS)
@league_command()
def cmd_blowout(args: str, ctx: CommandContext, view: LeagueView) -> str:
    return _margin_reply(ctx, view, closest=False)


def _margin_reply(ctx: CommandContext, view: LeagueView, *, closest: bool) -> str:
    week, matchups, age, stale = _scoring_week(view)
    playable = [m for m in matchups if m.complete and m.total > 0]
    if not playable:
        return f"No completed matchups in week {week} yet."

    playable.sort(key=lambda m: m.margin, reverse=not closest)
    pick = playable[0]
    high, low = pick.ordered()
    icon = "😬" if closest else "💀"

    lines = [
        *_head(ctx, view, ("closest" if closest else "blowout") + f" wk{week}"),
        icon + " " + score_line(high.short, high.points, low.short, low.points, budget=MAX_LINE - 2),
        f"Margin {pts(pick.margin)}",
    ]
    if len(playable) > 1:
        runner = playable[1]
        a, b = runner.ordered()
        lines.append(f"Next {a.short} / {b.short} by {pts(runner.margin)}")
    return with_freshness(fit(lines), age, stale)


@command("shame", help="Lowest score of the week.", aliases=("worst",), category=STATS)
@league_command()
def cmd_shame(args: str, ctx: CommandContext, view: LeagueView) -> str:
    week, matchups, age, stale = _scoring_week(view)
    sides = [s for m in matchups for s in m.sides if s.points > 0]
    if not sides:
        return f"Nothing scored in week {week} yet."

    sides.sort(key=lambda s: s.points)
    worst = sides[0]
    lines = [
        *_head(ctx, view, f"shame wk{week}"),
        f"🗑️ {worst.name}{DOT}{pts(worst.points)}",
    ]
    for other in sides[1:3]:
        lines.append(f"{other.short}{DOT}{pts(other.points)}")

    started = worst.started()
    if started:
        low = min(started, key=lambda pair: pair[1])
        lines.append(f"Architect: {view.client.player_name(low[0], short=True)} {pts(low[1])}")
    return with_freshness(fit(lines), age, stale)


@command("top", help="Highest-scoring starters in the league this week.", aliases=("studs",), category=STATS)
@league_command()
def cmd_top(args: str, ctx: CommandContext, view: LeagueView) -> str:
    week, matchups, age, stale = _scoring_week(view)
    client = view.client
    entries: list[tuple[float, str, str]] = []
    for matchup in matchups:
        for side in matchup.sides:
            for pid, score in side.started():
                if score > 0:
                    entries.append((score, pid, side.name))
    if not entries:
        return f"Nothing scored in week {week} yet."

    entries.sort(reverse=True)
    lines = _head(ctx, view, f"top wk{week}")
    for i, (score, pid, owner) in enumerate(entries[:8]):
        lines.append(
            f"{rank_marker(i)} {truncate(client.player_name(pid, short=True), 18)}"
            f" {pts(score)} {short_name(owner)}"
        )
    return with_freshness(fit(lines), age, stale)


# ---------------------------------------------------------------------------
# trash talk
# ---------------------------------------------------------------------------


@command("roast", help="An insult built from that person's real season.", usage="roast <name>", category=TALK)
@league_command()
def cmd_roast(args: str, ctx: CommandContext, view: LeagueView) -> str:
    if args.strip():
        manager = _resolve(args, ctx, view)
    else:
        # !roast is one of three commands the bot advertises, so a bare
        # invocation has to do something. Roast the caller if we know who they
        # are; otherwise pick somebody at random rather than answering a
        # first-time user with a setup instruction.
        manager = _me(ctx, view) or random.choice(list(view.managers.values()))
    results = view.season_results()
    return "🔥 " + ffroast.roast(view, manager, results)


@command("cope", help="Excuse generator.", usage="cope [name]", aliases=("excuse",), category=TALK)
@league_command(needs_games=False)
def cmd_cope(args: str, ctx: CommandContext, view: LeagueView) -> str:
    manager = None
    if view.has_games:
        try:
            manager = _resolve(args, ctx, view)
        except (NameNotFound, NameAmbiguous):
            manager = None
    results = view.season_results() if manager else {}
    return "😤 " + ffroast.cope(view, manager, results)


@command("curse", help="Random hex on one of their starters.", usage="curse <name>", aliases=("hex",), category=TALK)
@league_command()
def cmd_curse(args: str, ctx: CommandContext, view: LeagueView) -> str:
    manager = _resolve(args, ctx, view)
    pool = manager.starters or manager.players
    if not pool:
        return f"{manager.name} has nobody to curse. That is its own punishment."
    target = random.choice(pool)
    return ffroast.curse(view.client.player_name(target))


@command(
    "nickname",
    help="Give someone a nickname the bot will use from now on.",
    usage="nickname <name> <nickname>",
    aliases=("nick",),
    category=TALK,
)
@league_command(needs_games=False)
def cmd_nickname(args: str, ctx: CommandContext, view: LeagueView) -> str:
    parts = strip_mention(args).split()
    if len(parts) < 2:
        return "Usage: !nickname <name> <nickname>   (!nickname <name> clear to remove)"

    # The target may be several words ("Go Birds"), so widen the prefix until
    # one resolves and something is still left over for the nickname.
    manager = None
    nickname = ""
    for take in range(1, min(4, len(parts))):
        try:
            candidate = view.resolve(
                " ".join(parts[:take]),
                caller_handle=ctx.sender_address or ctx.sender_name,
                links=_links(ctx),
            )
        except (NameNotFound, NameAmbiguous):
            continue
        manager, nickname = candidate, " ".join(parts[take:]).strip()
        break

    if manager is None:
        raise NameNotFound(f"Nobody in {view.name} matches {parts[0]!r}")
    if not nickname:
        return "Usage: !nickname <name> <nickname>"

    store = _store(ctx)
    if nickname.lower() in ("clear", "reset", "none", "remove"):
        old = manager.name
        if store.clear_nickname(manager.user_id):
            return f"Dropped the nickname. {old} is {manager.formal_name} again."
        return f"{manager.formal_name} did not have a nickname."

    if len(nickname) > 32:
        return "Keep it under 32 characters."

    previous = manager.nickname
    store.set_nickname(manager.user_id, nickname, set_by=ctx.sender_name or ctx.sender_address)
    if previous:
        return f"✏️ {previous} is now {nickname}."
    return f"✏️ {manager.formal_name} is now {nickname}. It sticks."


@command("nicknames", help="Every nickname in force.", hidden=True, category=TALK)
@league_command(needs_games=False)
def cmd_nicknames(args: str, ctx: CommandContext, view: LeagueView) -> str:
    active = [(m.formal_name, m.nickname) for m in view.managers.values() if m.nickname]
    if not active:
        return "No nicknames set. !nickname <name> <nickname>"
    lines = ["Nicknames:"]
    lines.extend(f"  {formal} → {nick}" for formal, nick in sorted(active))
    return fit(lines)


_ODDS_FLAVOUR = (
    "Book is open.",
    "Line moves fast, act accordingly.",
    "Sharp money is already on this.",
    "Limits are low for a reason.",
    "This one is off the board in an hour.",
)


@command("odds", help="An invented betting line on anything.", usage="odds <proposition>", aliases=("line",), category=TALK)
def cmd_odds(args: str, ctx: CommandContext) -> str:
    proposition = args.strip()
    if not proposition:
        return "Usage: !odds <anything>   e.g. !odds barry benches his QB again"

    # Seeded on the proposition, so nobody can reroll a line they dislike.
    rng = _seeded(proposition)
    probability = rng.randint(3, 97)
    if probability >= 50:
        line = f"-{int(round(probability / (100 - probability) * 100))}"
    else:
        line = f"+{int(round((100 - probability) / probability * 100))}"
    return (
        f"📊 {truncate(proposition, 120)}\n"
        f"{line}{DOT}{probability}% implied{DOT}{rng.choice(_ODDS_FLAVOUR)}"
    )


_8BALL = (
    "Start him.",
    "Sit him. You know why.",
    "Start him and do not look at the box score until Tuesday.",
    "Bench. Absolutely not. No.",
    "Signs point to a touchdown and eleven yards.",
    "Ask again after the injury report.",
    "You are overthinking a 4-point decision.",
    "Yes, and you will still lose.",
    "No, and you will regret it either way.",
    "The matchup says yes. The player says no. Trust the player.",
    "Flip a coin, then do the opposite.",
    "It does not matter. Look at your record.",
)


@command("8ball", help="Start/sit oracle.", usage="8ball <question>", aliases=("oracle", "startsit"), category=TALK)
def cmd_8ball(args: str, ctx: CommandContext) -> str:
    question = args.strip()
    if not question:
        return "Ask it something. !8ball start Kupp or Nacua?"
    rng = _seeded(question)
    return f"🎱 {rng.choice(_8BALL)}"


# ---------------------------------------------------------------------------
# league management
# ---------------------------------------------------------------------------


@command("fine", help="Add to someone's tab.", usage="fine <name> <amount> <reason>", category=ADMIN)
@league_command(needs_games=False)
def cmd_fine(args: str, ctx: CommandContext, view: LeagueView) -> str:
    parts = strip_mention(args).split()
    if len(parts) < 2:
        return "Usage: !fine <name> <amount> <reason>   e.g. !fine barry 5 autodraft"

    # Find the amount first: it is the anchor between a possibly multi-word
    # name and a definitely multi-word reason.
    amount_index = None
    for i, part in enumerate(parts[1:], start=1):
        if parse_money(part) is not None:
            amount_index = i
            break
    if amount_index is None:
        return "I need an amount. !fine <name> <amount> <reason>"

    cents = parse_money(parts[amount_index])
    if cents is None or cents <= 0:
        return "The amount has to be a positive number of dollars."
    if cents > 100_000_00:
        return "Be serious."

    target = " ".join(parts[:amount_index])
    reason = " ".join(parts[amount_index + 1 :]).strip() or "no reason given"
    manager = view.resolve(target, caller_handle=ctx.sender_address or ctx.sender_name, links=_links(ctx))

    store = _store(ctx)
    store.add_fine(manager.user_id, cents, truncate(reason, 120), added_by=ctx.sender_name or ctx.sender_address)
    total = store.fine_totals().get(manager.user_id, 0)
    return f"💸 {manager.name} +{money(cents)} — {truncate(reason, 80)}\nTab: {money(total)}"


@command("fines", help="Who owes the pot.", aliases=("tab",), category=ADMIN)
@league_command(needs_games=False)
def cmd_fines(args: str, ctx: CommandContext, view: LeagueView) -> str:
    store = _store(ctx)
    totals = store.fine_totals()
    if not totals:
        return "Nobody owes anything. Yet."

    rows = []
    for user_id, cents in totals.items():
        manager = view.by_user(user_id)
        rows.append((cents, manager.name if manager else user_id))
    rows.sort(reverse=True)

    lines = _head(ctx, view, "fines")
    lines.extend(f"{short_name(name)}{DOT}{money(cents)}" for cents, name in rows)
    lines.append(f"Total: {money(sum(totals.values()))} across {store.fine_count()} fines")

    recent = store.recent_fines(limit=3)
    if recent:
        lines.append("Latest:")
        for fine in recent:
            manager = view.by_user(fine.user_id)
            who = manager.name if manager else fine.user_id
            lines.append(f"{short_name(who)} {money(fine.cents)}")
            lines.append(f"  {truncate(fine.reason, 26)}")
    return fit(lines)


@command("pot", help="Total prize money.", category=ADMIN)
@league_command(needs_games=False)
def cmd_pot(args: str, ctx: CommandContext, view: LeagueView) -> str:
    store = _store(ctx)

    if args.strip():
        cents = parse_money(args.strip().split()[0])
        if cents is None or cents < 0:
            return "Usage: !pot            (show)\n       !pot <buy-in>   (set the per-team buy-in)"
        store.set_meta("buyin_cents", str(cents))
        return f"Buy-in set to {money(cents)} per team. !pot to see the total."

    try:
        buyin = int(store.get_meta("buyin_cents", "0"))
    except ValueError:
        buyin = 0
    fines_total = sum(store.fine_totals().values())
    teams = len(view.managers)

    lines = _head(ctx, view, "pot")
    if buyin:
        lines.append(f"Buy-ins: {teams} × {money(buyin)} = {money(buyin * teams)}")
    else:
        lines.append("Buy-in not set. !pot <amount> to set it.")
    lines.append(f"Fines: {money(fines_total)}")
    lines.append(f"💰 Total: {money(buyin * teams + fines_total)}")
    return fit(lines)


DEFAULT_PUNISHMENT = (
    "Last place has to do whatever the league voted on. "
    "Set it with !punishment <text> so there is no arguing later."
)


@command("punishment", help="What last place has to do.", usage="punishment [text]", aliases=("shameful",), category=ADMIN)
@league_command(needs_games=False)
def cmd_punishment(args: str, ctx: CommandContext, view: LeagueView) -> str:
    store = _store(ctx)
    text = args.strip()
    if text:
        if len(text) > 500:
            return "Keep it under 500 characters."
        store.set_meta("punishment", text)
        return f"📜 Punishment recorded:\n{text}"

    current = store.get_meta("punishment", "")
    lines = ["📜 " + (current or DEFAULT_PUNISHMENT)]
    if view.has_games:
        ranked = view.sorted_managers()
        if ranked:
            last = ranked[-1]
            verb = "is" if view.status != "complete" else "was"
            lines.append(f"Currently {verb} {last.name} ({last.record}).")
    return "\n".join(lines)


@command("poll", help="Start a vote. Options separated by |", usage="poll <question> | a | b", category=ADMIN)
def cmd_poll(args: str, ctx: CommandContext) -> str:
    raw = args.strip()
    if not raw:
        return "Usage: !poll <question> | option | option    then everyone !vote <n>"

    parts = [p.strip() for p in raw.split("|")]
    question = parts[0]
    options = [p for p in parts[1:] if p] or ["Yes", "No"]
    if len(options) > 6:
        return "Six options maximum."
    if not question:
        return "The poll needs a question."

    store = _store(ctx)
    poll_id = store.create_poll(
        ctx.chat_guid, truncate(question, 200), [truncate(o, 60) for o in options],
        created_by=ctx.sender_name or ctx.sender_address,
    )
    lines = [f"🗳️ {question}"]
    lines.extend(f"  {i}. {option}" for i, option in enumerate(options, start=1))
    lines.append(f"!vote <number> to vote{DOT}!results to close it")
    log.info("poll opened", extra={"fields": {"poll_id": poll_id, "chat_guid": ctx.chat_guid}})
    return "\n".join(lines)


@command("vote", help="Vote in the open poll.", usage="vote <number>", category=ADMIN)
def cmd_vote(args: str, ctx: CommandContext) -> str:
    store = _store(ctx)
    poll = store.open_poll(ctx.chat_guid)
    if poll is None:
        return "No poll is open here. !poll <question> | a | b"

    choice = args.strip().split()[0] if args.strip() else ""
    if not choice.isdigit():
        return f"!vote <number>, 1 to {len(poll.options)}."
    index = int(choice)
    if not (1 <= index <= len(poll.options)):
        return f"There are {len(poll.options)} options."

    voter = normalize_handle(ctx.sender_address or ctx.sender_name)
    if not voter:
        return "I cannot tell who you are, so I cannot count your vote."
    store.vote(poll.id, voter, index)

    tally = store.poll_tally(poll.id)
    total = sum(tally.values())
    # One line, not a re-render of the whole poll: this fires on every vote.
    return f"✅ {poll.options[index - 1]}{DOT}{total} {plural(total, 'vote')} cast"


@command("results", help="Show and close the current poll.", aliases=("tally",), category=ADMIN)
def cmd_results(args: str, ctx: CommandContext) -> str:
    store = _store(ctx)
    poll = store.open_poll(ctx.chat_guid) or store.latest_poll(ctx.chat_guid)
    if poll is None:
        return "No poll has been run here."

    tally = store.poll_tally(poll.id)
    total = sum(tally.values())
    lines = [f"🗳️ {poll.question}"]
    ranked = sorted(
        enumerate(poll.options, start=1), key=lambda pair: tally.get(pair[0], 0), reverse=True
    )
    for index, option in ranked:
        count = tally.get(index, 0)
        bar = "█" * count if count <= 12 else "█" * 12 + "+"
        lines.append(f"  {option}{DOT}{count} {bar}".rstrip())
    lines.append(f"{total} {plural(total, 'vote')}" + ("" if poll.closed else " — poll closed"))
    if not poll.closed:
        store.close_poll(poll.id)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


@command(
    "link",
    help="Tell the bot which Sleeper account is yours.",
    usage="link <sleeper name>",
    category=ADMIN,
)
@league_command(needs_games=False)
def cmd_link(args: str, ctx: CommandContext, view: LeagueView) -> str:
    handle = normalize_handle(ctx.sender_address or ctx.sender_name)
    if not handle:
        return "I cannot see your handle, so I cannot link you."

    if not args.strip():
        existing = _links(ctx).get(handle)
        manager = view.by_user(existing) if existing else None
        if manager:
            return f"You are linked to {manager.formal_name}."
        return "You are not linked. !link <your sleeper name>"

    manager = view.resolve(strip_mention(args), caller_handle=handle, links=_links(ctx))
    _store(ctx).set_link(handle, manager.user_id)
    return f"🔗 Linked to {manager.formal_name}.\n!roast me now works."


@command(
    "preview",
    help="Render a scheduled post now, without waiting for its trigger.",
    usage="preview <lineups|sunday|recap|shotgun|ahead>",
    category=ADMIN,
)
@league_command()
def cmd_preview(args: str, ctx: CommandContext, view: LeagueView) -> str:
    """Testing a Monday-night post by waiting until Monday night is not workable."""
    tokens = args.strip().lower().split()
    wide = any(t in ("--wide", "-w", "wide", "--width") for t in tokens)
    tokens = [t for t in tokens if not t.startswith("-") and t not in ("wide", "width")]
    kind = tokens[0] if tokens else ""

    if kind not in ffposts.POST_TYPES:
        ctx.title = "preview"
        lines = [f"!preview {key}" for key in ffposts.POST_ORDER]
        lines += ["", "Add --wide for line widths."]
        return "\n".join(lines)

    week = view.week
    if len(tokens) > 1 and tokens[1].isdigit():
        week = max(1, min(int(tokens[1]), 18))

    text = ffposts.build(kind, view, week)
    if not text:
        # Not a failure. The silence rule is the feature being demonstrated.
        return (
            f"{ffposts.POST_TYPES[kind].label} wk {week}:\n"
            f"nothing would post.\nThat is deliberate."
        )

    if wide:
        ctx.title = f"widths {kind}"
        return ffposts.width_report(text)

    # No preview marker at all: the reply's own header is composed from this
    # title, so what comes back is byte-identical to what the scheduler would
    # post. Anything else and you are previewing a slightly different message
    # than the one that goes out.
    ctx.title = ffposts.post_title(kind, week)
    if view.is_previous_season and kind == "lineups":
        # Bye and injury data are current-day; against a finished season they
        # are a formatting preview, not a real warning.
        text += "\n\n(byes/injuries are current)"
    return text


@command("posts", help="Scheduled post status.", hidden=True, category=ADMIN)
@league_command(needs_games=False)
def cmd_posts(args: str, ctx: CommandContext, view: LeagueView) -> str:
    cfg = ctx.config
    enabled = getattr(cfg, "sleeper_posts_enabled", False)
    chat = getattr(cfg, "sleeper_post_chat", "")
    active = getattr(cfg, "sleeper_post_types", ()) or ()

    lines = [
        f"Scheduled posts: {'on' if enabled and chat else 'OFF'}",
        f"Chat: {chat or '(not set)'}",
        f"Timezone: {getattr(cfg, 'sleeper_timezone', '?')}",
        "Types: " + (", ".join(active) if active else "(none)"),
    ]
    recent = _store(ctx).recent_posts(limit=5)
    if recent:
        lines.append("Already posted:")
        for season, week, kind, when in recent:
            ago = describe_age(time.time() - when)
            lines.append(f"  {season} wk{week} {kind}{DOT}{ago}")
    return "\n".join(lines)


@command("league", help="Which league the bot is pointed at.", hidden=True, category=ADMIN)
@league_command(needs_games=False)
def cmd_league(args: str, ctx: CommandContext, view: LeagueView) -> str:
    client = view.client
    links = _links(ctx)
    linked = sum(1 for m in view.managers.values() if m.user_id in set(links.values()))
    lines = [
        f"{view.name} ({view.season}, {view.status})",
        f"League ID: {view.league_id}",
        f"{len(view.managers)} teams{DOT}{linked} linked to chat handles",
        f"Playoffs: {view.playoff_teams} teams from week {view.playoff_week_start}",
        f"Lineup: {', '.join(view.starting_slots)}",
        f"Sleeper calls this session: {client.request_count}",
    ]
    if view.is_previous_season:
        lines.insert(1, f"(configured {view.configured_league_id} has not drafted)")
    return "\n".join(lines)
