"""The four scheduled posts.

These arrive uninvited on twelve phones, so the bar is higher than for a
command somebody asked for. Two rules follow from that:

**Lead with the interesting thing.** Twelve rows of standings is a wall of
text. The closest game and the worst bench decision are what people reply to,
so those go near the top and the table (when there is one at all) goes under.

**Silence is a valid output.** Every builder returns ``None`` rather than a
placeholder when it has nothing worth saying or cannot get the data. A weekly
"everything looks fine!" trains people to ignore the bot, and a half-rendered
post during an outage is worse than no post. The scheduler treats ``None`` as
"say nothing and try again later", so a transient failure is not a missed week.

Builders are pure: ``(LeagueView, week) -> str | None``. No sending, no
persistence, no clock. That is what makes ``!preview`` possible and what makes
them testable without waiting until Monday night.
"""

from __future__ import annotations

from dataclasses import dataclass

from ffformat import DOT, MAX_LINE, SCORE_SEP, fit, pts, score_line, short_name
from ffleague import SLOT_ELIGIBILITY, LeagueView, Side
from logging_setup import get_logger
from sleeper import GAME_FINISHED, is_empty_slot

log = get_logger(__name__)

# Injury designations worth interrupting somebody's Sunday morning for.
# Questionable is deliberately excluded: half a roster is Questionable on any
# given week, and a post that names eight people is a post nobody reads.
BENCH_WORTHY = frozenset({"Out", "Doubtful", "IR", "PUP", "Sus", "NA"})


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _side_for(view: LeagueView, week: int, roster_id: int) -> Side | None:
    matchups, _, _ = view.matchups(week)
    for matchup in matchups:
        for side in matchup.sides:
            if side.roster_id == roster_id:
                return side
    return None


def _lineup_slots(view: LeagueView, side: Side) -> list[tuple[str, str, float]]:
    """(slot, player_id, points) for filled starter slots.

    ``Side.started()`` drops empty slots, which desynchronises it from
    ``roster_positions``. This keeps the slot name attached, which is what the
    bench-swap check needs to know what may legally replace what.
    """
    slots = view.starting_slots
    out: list[tuple[str, str, float]] = []
    for i, pid in enumerate(side.starters):
        if is_empty_slot(pid):
            continue
        slot = slots[i] if i < len(slots) else "FLEX"
        score = side.starters_points[i] if i < len(side.starters_points) else 0.0
        out.append((slot, str(pid), float(score or 0.0)))
    return out


def _empty_slots(view: LeagueView, side: Side) -> list[str]:
    slots = view.starting_slots
    return [
        (slots[i] if i < len(slots) else "FLEX")
        for i, pid in enumerate(side.starters)
        if is_empty_slot(pid)
    ]


@dataclass
class BenchSwap:
    """One player who should have started over one player who did."""

    manager_name: str
    benched: str
    benched_points: float
    started: str
    started_points: float

    @property
    def gap(self) -> float:
        return self.benched_points - self.started_points


def worst_bench_swap(view: LeagueView, week: int) -> BenchSwap | None:
    """The largest single swap anybody left on the table.

    Deliberately a *swap*, not the optimal-lineup delta that ``!bench`` uses:
    "you should have started X over Y" is an argument people can have, whereas
    "your optimal lineup was 14.2 better" is a number nobody can picture.
    """
    client = view.client
    if not client.players(allow_fetch=False):
        return None

    matchups, _, _ = view.matchups(week)
    best: BenchSwap | None = None

    for matchup in matchups:
        for side in matchup.sides:
            if not side.players_points:
                continue
            lineup = _lineup_slots(view, side)
            for bench_id, bench_points in side.bench():
                position = str(client.player_meta(bench_id).get("position") or "")
                if not position:
                    continue
                for slot, started_id, started_points in lineup:
                    eligible = SLOT_ELIGIBILITY.get(slot)
                    if eligible is not None and position not in eligible:
                        continue
                    gap = bench_points - started_points
                    if gap <= 0 or (best is not None and gap <= best.gap):
                        continue
                    best = BenchSwap(
                        manager_name=side.name,
                        benched=client.player_name(bench_id, short=True),
                        benched_points=bench_points,
                        started=client.player_name(started_id, short=True),
                        started_points=started_points,
                    )
    return best


def _still_playing(view: LeagueView, week: int) -> int:
    """Starters whose NFL game has not finished. 0 if we cannot tell."""
    client = view.client
    games = client.week_games(view.season, week)
    if not games:
        return 0
    unfinished = {
        team
        for game in games
        if str(game.get("status") or "") not in GAME_FINISHED
        for team in (game.get("home"), game.get("away"))
        if team
    }
    if not unfinished:
        return 0

    matchups, _, _ = view.matchups(week)
    return sum(
        1
        for matchup in matchups
        for side in matchup.sides
        for pid, _ in side.started()
        if str(client.player_meta(pid).get("team") or "") in unfinished
    )


def _name_run(prefix: str, names: list[str], budget: int = MAX_LINE) -> str:
    """``Up: Ryanmd, TKoop +4`` -- as many names as fit, then a count.

    Taking a fixed first three overflows whenever the names are long, which is
    exactly when it matters.
    """
    from ffformat import display_width

    shown: list[str] = []
    for name in names:
        candidate = prefix + ", ".join(shown + [name])
        remaining = len(names) - len(shown) - 1
        suffix = f" +{remaining}" if remaining else ""
        if display_width(candidate + suffix) > budget:
            break
        shown.append(name)
    if not shown:
        shown = names[:1]
    left = len(names) - len(shown)
    return prefix + ", ".join(shown) + (f" +{left}" if left else "")


def _scored_matchups(view: LeagueView, week: int) -> list:
    matchups, _, _ = view.matchups(week)
    return [m for m in matchups if m.complete and m.total > 0]


# ---------------------------------------------------------------------------
# 1. Sunday morning -- lineup check
# ---------------------------------------------------------------------------


def build_lineup_check(view: LeagueView, week: int | None = None) -> str | None:
    """Name only the managers with a fixable problem. Silence if everyone is clean.

    Three problems are worth a Sunday-morning interruption: an unfilled
    starting slot, a starter on bye, and a starter ruled out. Everything else
    is an opinion, and this post is not for opinions.
    """
    week = view.week if week is None else week
    client = view.client
    try:
        matchups, _, stale = view.matchups(week)
    except Exception:
        return None
    if not matchups or stale:
        return None

    on_bye = client.teams_on_bye(view.season, week)
    have_players = bool(client.players(allow_fetch=False))

    problems: list[tuple[int, str]] = []
    for matchup in matchups:
        for side in matchup.sides:
            faults: list[str] = []

            empties = _empty_slots(view, side)
            if empties:
                faults.append("empty " + "/".join(empties[:3]))

            for pid, _ in side.started():
                if not have_players:
                    continue
                meta = client.player_meta(pid)
                name = client.player_name(pid, short=True)
                team = str(meta.get("team") or "")
                if on_bye and team in on_bye:
                    faults.append(f"{name} bye")
                    continue
                status = str(meta.get("injury_status") or "")
                if status in BENCH_WORTHY:
                    faults.append(f"{name} {status.upper()}")

            if faults:
                # Empty slots first: those are certainly wrong, whereas an
                # OUT designation occasionally gets upgraded by kickoff.
                severity = (2 if empties else 0) + len(faults)
                # One fault per line. Two on a line reads fine at 40 characters
                # and wraps into porridge at 30.
                for fault in faults[:2]:
                    problems.append((severity, f"{side.short}{DOT}{fault}"))

    if not problems:
        return None  # the whole point: no post at all

    problems.sort(reverse=True)
    lines: list[str] = []
    lines.extend(text for _, text in problems)
    lines.append("")
    # Deliberately does not claim how long is left: the schedule endpoint has
    # dates but no kickoff times, so any number here would be invented.
    lines.append("Locks at kickoff.")
    return fit(lines)


# ---------------------------------------------------------------------------
# 2. Sunday evening -- state of play
# ---------------------------------------------------------------------------


def build_sunday_update(view: LeagueView, week: int | None = None) -> str | None:
    """Short. The closest game, the worst beating, who is up, what is left."""
    week = view.week if week is None else week
    try:
        scored = _scored_matchups(view, week)
    except Exception:
        return None
    if not scored:
        return None

    closest = min(scored, key=lambda m: m.margin)
    widest = max(scored, key=lambda m: m.margin)

    lines: list[str] = []

    close_high, close_low = closest.ordered()
    lines.append("😬 Closest")
    lines.append(score_line(close_high.short, close_high.points,
                            close_low.short, close_low.points))

    if widest.margin > closest.margin:
        wide_high, wide_low = widest.ordered()
        lines.append("")
        lines.append("💀 Beatdown")
        lines.append(score_line(wide_high.short, wide_high.points,
                                wide_low.short, wide_low.points))

    leaders = [m.ordered()[0].short for m in scored if m.margin > 0]
    if leaders:
        lines.append("")
        lines.append(_name_run("Up: ", leaders))

    remaining = _still_playing(view, week)
    if remaining:
        lines.append(f"{remaining} starters left.")
    return fit(lines)


# ---------------------------------------------------------------------------
# 3. Monday night -- the week recap. The flagship.
# ---------------------------------------------------------------------------


def build_recap(view: LeagueView, week: int | None = None) -> str | None:
    """Final scores, the superlatives, and the week's worst decision."""
    week = view.week if week is None else week
    try:
        scored = _scored_matchups(view, week)
    except Exception:
        return None
    if not scored:
        return None

    sides = sorted(
        (s for m in scored for s in m.sides), key=lambda s: s.points, reverse=True
    )
    if not sides:
        return None

    high, low = sides[0], sides[-1]
    closest = min(scored, key=lambda m: m.margin)
    widest = max(scored, key=lambda m: m.margin)

    # No title line and no league name: the message header already says
    # "bloqBot - recap wk14", and everyone in the chat knows the league.
    lines: list[str] = []

    # Results, best game first -- the scoreboard is the body, not the lead.
    for matchup in sorted(scored, key=lambda m: -m.total):
        winner, loser = matchup.ordered()
        separator = " = " if matchup.margin == 0 else None
        line = score_line(winner.short, winner.points, loser.short, loser.points)
        if separator:
            line = line.replace(SCORE_SEP, separator, 1)
        lines.append(line)

    lines.append("")
    lines.append(f"🥇 High {pts(high.points)} {high.short}")
    lines.append(f"🗑️ Low {pts(low.points)} {low.short}")
    if widest.margin > 0:
        # One superlative per line: the two together were 31 characters.
        lines.append(f"💀 Blowout {pts(widest.margin)}")
        lines.append(f"😬 Closest {pts(closest.margin)}")

    swap = worst_bench_swap(view, week)
    if swap is not None and swap.gap >= 5:
        # Broken across lines on purpose. As one sentence this wraps mid-name,
        # which is exactly the thing that makes a bubble look broken.
        lines.append("")
        lines.append(f"🪑 Worst call{DOT}{short_name(swap.manager_name)}")
        lines.append(f"started {swap.started} {pts(swap.started_points)}")
        lines.append(f"over {swap.benched} {pts(swap.benched_points)}")
        lines.append(f"−{pts(swap.gap)}")
    return fit(lines)


# ---------------------------------------------------------------------------
# 4. Wednesday morning -- the week ahead
# ---------------------------------------------------------------------------


def build_week_ahead(view: LeagueView, week: int | None = None) -> str | None:
    """Overnight waiver results plus this week's matchups."""
    week = view.week if week is None else week
    client = view.client
    try:
        matchups, _, stale = view.matchups(week)
    except Exception:
        return None
    if not matchups or stale:
        return None

    pairs = [m for m in matchups if m.complete]
    if not pairs:
        return None

    lines: list[str] = []

    # Lead with the marquee game rather than an unsorted list of six.
    def quality(matchup) -> float:
        return sum(s.manager.win_pct if s.manager else 0.0 for s in matchup.sides)

    marquee = max(pairs, key=quality)
    if quality(marquee) > 0:
        a, b = marquee.sides
        record_a = a.manager.record if a.manager else "?"
        record_b = b.manager.record if b.manager else "?"
        lines.append("⭐ Game of the week")
        lines.append(f"{a.short} {record_a} v {b.short} {record_b}")
        lines.append("")

    for matchup in pairs:
        if matchup is marquee:
            continue
        a, b = matchup.sides
        lines.append(f"{a.short} v {b.short}")

    moves = _waiver_lines(view, week, client)
    if moves:
        lines.append("")
        lines.append("Waivers")
        lines.extend(moves[:5])

    return fit(lines)


def _waiver_lines(view: LeagueView, week: int, client) -> list[str]:
    """Completed adds/drops for the week. Empty list on any failure."""
    try:
        entry = client.transactions(view.league_id, week)
    except Exception:
        return []
    rows = entry.value if isinstance(entry.value, list) else []

    out: list[str] = []
    for row in sorted(rows, key=lambda r: r.get("status_updated") or 0, reverse=True):
        if row.get("type") not in ("waiver", "free_agent") or row.get("status") != "complete":
            continue
        roster_ids = row.get("roster_ids") or []
        manager = view.managers.get(int(roster_ids[0])) if roster_ids else None
        adds = list((row.get("adds") or {}).keys())
        if not adds:
            continue  # a bare drop is not news on a Wednesday
        bid = (row.get("settings") or {}).get("waiver_bid")
        cost = f" (${bid})" if bid else ""
        who = short_name(manager.name) if manager else "?"
        out.append(f"{who} +{client.player_name(adds[0], short=True)}{cost}")
    return out


# ---------------------------------------------------------------------------
# registry -- the scheduler and !preview both drive off this
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostType:
    key: str
    label: str
    builder: object
    when: str  # human description, for !preview and !help


POST_TYPES: dict[str, PostType] = {
    "lineups": PostType("lineups", "Lineup check", build_lineup_check,
                        "Sunday, before the first kickoff"),
    "sunday": PostType("sunday", "Sunday night", build_sunday_update,
                       "Sunday evening, once the afternoon games are done"),
    "recap": PostType("recap", "Week recap", build_recap,
                      "After the week's last game finishes"),
    "ahead": PostType("ahead", "Week ahead", build_week_ahead,
                      "Wednesday morning, after waivers clear"),
}

POST_ORDER: tuple[str, ...] = ("lineups", "sunday", "recap", "ahead")

# Header titles. Short by necessity: "🤖 bloqBot - " already spends about half
# the line, leaving ~17 columns.
TITLE_FORMATS: dict[str, str] = {
    "lineups": "lineups wk{week}",
    "sunday": "sunday wk{week}",
    "recap": "recap wk{week}",
    "ahead": "ahead wk{week}",
}


def post_title(kind: str, week: int | None = None) -> str:
    """The header title for a post, e.g. ``recap wk14``."""
    fmt = TITLE_FORMATS.get(kind)
    if not fmt or not week:
        return kind
    return fmt.format(week=week)


def build(kind: str, view: LeagueView, week: int | None = None) -> str | None:
    post = POST_TYPES.get(kind)
    if post is None:
        return None
    try:
        return post.builder(view, week)
    except Exception:
        # A builder must never take down the scheduler thread or a command.
        log.exception("scheduled post builder failed", extra={"fields": {"kind": kind}})
        return None


def width_report(text: str, budget: int = MAX_LINE, worst: int = 4) -> str:
    """Line widths for a rendered post, worst first.

    The point of ``!preview <kind> --wide``: checking the budget by eyeballing
    screenshots does not scale, and ``len()`` lies about emoji.
    """
    from ffformat import display_width

    lines = text.split("\n")
    widths = [(display_width(line), line) for line in lines]
    longest = max((w for w, _ in widths), default=0)
    over = [pair for pair in widths if pair[0] > budget]

    out = [
        f"longest {longest}/{budget}{DOT}{len(lines)} lines",
        "OVER BUDGET" if over else "within budget ✅",
        "",
    ]
    from ffformat import clip

    for width, line in sorted(widths, key=lambda pair: -pair[0])[:worst]:
        flag = "!" if width > budget else " "
        # Clipped, not wrapped: a sample line that wraps is useless as a sample.
        sample = clip(line, budget - 4) if line else "(blank)"
        out.append(f"{width:>2}{flag} {sample}")
    return "\n".join(out)
