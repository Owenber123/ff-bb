"""Trash talk, built from real data.

Specificity is the whole trick. "0-4 in one-score games" lands; "your team is
bad" does not. So :func:`gather_facts` mines the league for things that are
actually true about one person's season, each carrying a weight, and
:func:`roast` picks from the best of them. Generic lines exist only as the
floor for somebody with a boring season.

No LLM here on purpose: everything is deterministic and testable. The seam for
a model-backed version later is :func:`gather_facts` -- it returns structured
:class:`Fact` objects with their raw numbers attached, which is exactly the
prompt context an LLM version would want. Swapping the renderer does not
require re-deriving any of the data work.

One correctness note. Player -> NFL team comes from the cached player file,
which holds *current* team, not the team they were on in week 8 of last
season. So the "started a player on his bye" fact is only claimed when the
player also scored exactly 0.0 that week -- corroborating evidence, since a
confidently wrong burn is worse than no burn.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Sequence

from ffformat import pts
from ffleague import (
    LeagueView,
    Manager,
    WeekResult,
    close_games,
    expected_wins,
    optimal_lineup,
    streak,
)
from logging_setup import get_logger
from sleeper import SleeperError

log = get_logger(__name__)


@dataclass
class Fact:
    """Something true, with the numbers kept for a future LLM renderer."""

    kind: str
    weight: int
    text: str
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# fact mining
# ---------------------------------------------------------------------------


def gather_facts(
    view: LeagueView,
    manager: Manager,
    results: dict[int, list[WeekResult]],
) -> list[Fact]:
    """Everything unflattering and true about one manager's season."""
    facts: list[Fact] = []
    mine = sorted(results.get(manager.roster_id, []), key=lambda r: r.week)
    played = [r for r in mine if r.opponent_id is not None]
    rank = view.rank_of(manager)
    teams = len(view.managers)

    # --- record and placement ------------------------------------------------
    if manager.games:
        if rank >= max(2, teams - 1):
            facts.append(
                Fact(
                    "rank",
                    7,
                    f"is {rank} of {teams} at {manager.record}",
                    {"rank": rank, "teams": teams, "record": manager.record},
                )
            )
        if manager.wins == 0 and manager.losses >= 3:
            facts.append(
                Fact("winless", 10, f"is {manager.record}. Winless. On purpose, apparently.", {})
            )
        if manager.losses >= manager.wins * 2 and manager.losses >= 6:
            facts.append(
                Fact("lopsided", 5, f"has lost {manager.losses} of {manager.games}", {})
            )

    # --- one-score games -----------------------------------------------------
    close_w, close_l = close_games(played, threshold=10.0)
    if close_l >= 2 and close_w == 0:
        facts.append(
            Fact(
                "close",
                9,
                f"is 0-{close_l} in one-score games",
                {"wins": close_w, "losses": close_l},
            )
        )
    elif close_l >= 3 and close_l > close_w:
        facts.append(
            Fact(
                "close",
                6,
                f"is {close_w}-{close_l} in games decided by under 10",
                {"wins": close_w, "losses": close_l},
            )
        )

    # --- worst single week ---------------------------------------------------
    if played:
        worst = min(played, key=lambda r: r.points)
        league_low = _league_low(results)
        if league_low and abs(worst.points - league_low[1]) < 0.01:
            facts.append(
                Fact(
                    "league_low",
                    10,
                    f"owns the lowest score of the season: {pts(worst.points)} in week {worst.week}",
                    {"week": worst.week, "points": worst.points},
                )
            )
        elif worst.points < 80:
            facts.append(
                Fact(
                    "low_week",
                    6,
                    f"put up {pts(worst.points)} in week {worst.week}",
                    {"week": worst.week, "points": worst.points},
                )
            )

    # --- biggest beating -----------------------------------------------------
    losses = [r for r in played if r.outcome == "L"]
    if losses:
        worst_loss = min(losses, key=lambda r: r.margin)
        if abs(worst_loss.margin) >= 40:
            opponent = view.managers.get(worst_loss.opponent_id or 0)
            who = opponent.name if opponent else "somebody"
            facts.append(
                Fact(
                    "blowout",
                    8,
                    f"lost to {who} by {pts(abs(worst_loss.margin))} in week {worst_loss.week}",
                    {"week": worst_loss.week, "margin": worst_loss.margin, "opponent": who},
                )
            )

    # --- losing streak -------------------------------------------------------
    current = streak(played)
    if current.startswith("L") and current[1:].isdigit() and int(current[1:]) >= 3:
        facts.append(Fact("streak", 7, f"has lost {current[1:]} straight", {"streak": current}))

    # --- luck, in both directions -------------------------------------------
    weeks = sorted({r.week for r in played})
    if len(weeks) >= 4:
        expected = expected_wins(results, weeks)
        mine_expected = expected.get(manager.roster_id, 0.0)
        delta = manager.wins - mine_expected
        if delta >= 1.5:
            facts.append(
                Fact(
                    "lucky",
                    8,
                    f"has {manager.wins} wins and deserved {mine_expected:.1f}",
                    {"actual": manager.wins, "expected": mine_expected},
                )
            )
        elif delta <= -1.5:
            facts.append(
                Fact(
                    "unlucky",
                    4,
                    f"deserved {mine_expected:.1f} wins and got {manager.wins}, which is "
                    f"the only sympathy available here",
                    {"actual": manager.wins, "expected": mine_expected},
                )
            )

    # --- points against ------------------------------------------------------
    if manager.games and manager.fpts_against > manager.fpts:
        gap = manager.fpts_against - manager.fpts
        if gap > 100:
            facts.append(
                Fact(
                    "outscored",
                    4,
                    f"has been outscored by {pts(gap)} on the season",
                    {"gap": gap},
                )
            )

    # --- coaching: bench and lineup ------------------------------------------
    facts.extend(_lineup_facts(view, manager, mine))

    # --- FAAB ----------------------------------------------------------------
    budget = view.settings.get("waiver_budget")
    if budget and manager.waiver_budget_used >= int(budget) * 0.95 and rank > len(view.managers) / 2:
        facts.append(
            Fact(
                "faab",
                6,
                f"spent essentially all ${budget} of FAAB to finish {rank} of {teams}",
                {"budget": budget, "rank": rank},
            )
        )

    facts.sort(key=lambda f: f.weight, reverse=True)
    return facts


def _league_low(results: dict[int, list[WeekResult]]) -> tuple[int, float] | None:
    lowest: tuple[int, float] | None = None
    for roster_id, week_results in results.items():
        for result in week_results:
            if result.opponent_id is None:
                continue
            if lowest is None or result.points < lowest[1]:
                lowest = (roster_id, result.points)
    return lowest


def _lineup_facts(view: LeagueView, manager: Manager, mine: Sequence[WeekResult]) -> list[Fact]:
    """Bench points, zeroes started, and players started on their bye.

    Costs one matchup fetch per week, all of which are already cached from the
    season scan the caller has just done.
    """
    facts: list[Fact] = []
    if not mine:
        return facts

    client = view.client
    slots = view.starting_slots
    worst_bench: tuple[int, float] | None = None
    total_left = 0.0
    zeroes: list[tuple[int, str]] = []
    bye_starts: list[tuple[int, str]] = []

    # Bye claims need the current season's schedule; a player's cached team is
    # today's team, which may not be who they played for last year.
    check_byes = not view.is_previous_season

    for result in mine:
        try:
            matchups, _, _ = view.matchups(result.week)
        except SleeperError:
            continue
        side = None
        for matchup in matchups:
            for candidate in matchup.sides:
                if candidate.roster_id == manager.roster_id:
                    side = candidate
        if side is None or not side.players_points:
            continue

        positions = {
            pid: str(client.player_meta(pid).get("position") or "")
            for pid in side.players_points
        }
        best, _ = optimal_lineup(slots, side.players_points, positions)
        left = best - side.points
        if left > 0:
            total_left += left
            if worst_bench is None or left > worst_bench[1]:
                worst_bench = (result.week, left)

        on_bye = client.teams_on_bye(view.season, result.week) if check_byes else set()
        for pid, score in side.started():
            if score > 0.0:
                continue
            name = client.player_name(pid)
            if name == pid:
                continue  # player file not loaded; do not invent a burn
            meta = client.player_meta(pid)
            if on_bye and str(meta.get("team") or "") in on_bye:
                bye_starts.append((result.week, name))
            else:
                zeroes.append((result.week, name))

    if bye_starts:
        week, name = bye_starts[0]
        facts.append(
            Fact(
                "bye_start",
                10,
                f"started {name} in week {week}. {name} was on bye.",
                {"week": week, "player": name},
            )
        )
    if zeroes:
        week, name = zeroes[0]
        facts.append(
            Fact(
                "zero_start",
                7,
                f"started {name} for a clean 0.0 in week {week}",
                {"week": week, "player": name},
            )
        )
    if worst_bench and worst_bench[1] >= 20:
        facts.append(
            Fact(
                "bench",
                8,
                f"left {pts(worst_bench[1])} on the bench in week {worst_bench[0]}",
                {"week": worst_bench[0], "points": worst_bench[1]},
            )
        )
    if total_left >= 100:
        facts.append(
            Fact(
                "bench_season",
                6,
                f"has left {pts(total_left)} on the bench this season",
                {"points": total_left},
            )
        )
    return facts


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

_OPENERS = (
    "{name} {fact}.",
    "Reminder that {name} {fact}.",
    "{name} {fact}. That is a choice somebody made.",
    "Nothing to add here: {name} {fact}.",
    "{name} {fact}, and still talks.",
)

_TAGS = (
    "Wonderful stuff.",
    "Genuinely impressive.",
    "Keep it up.",
    "No notes.",
    "Historic.",
    "Built different, downward.",
)

_GENERIC = (
    "{name} is playing fantasy football the way other people fill in a tax form.",
    "{name}'s draft board was clearly a screenshot of somebody else's.",
    "{name} has a lineup and a prayer, and the lineup is the weaker of the two.",
    "The most competitive thing about {name} is the group chat.",
    "{name}'s season is going about as well as the team name suggests.",
)


def roast(
    view: LeagueView,
    manager: Manager,
    results: dict[int, list[WeekResult]],
    rng: random.Random | None = None,
) -> str:
    """One line, built from that person's worst real number.

    Picks randomly among the strongest facts so repeat invocations vary, but
    never drops to a generic line while a real one is available.
    """
    rng = rng or random
    facts = gather_facts(view, manager, results)

    if not facts:
        return rng.choice(_GENERIC).format(name=manager.name)

    top = facts[0].weight
    best = [f for f in facts if f.weight >= top - 2][:4]
    chosen = rng.choice(best)
    line = rng.choice(_OPENERS).format(name=manager.name, fact=chosen.text)

    # A second, unrelated fact roughly half the time -- twisting the knife
    # reads better than one clause, but three is a lecture.
    others = [f for f in facts if f.kind != chosen.kind]
    if others and rng.random() < 0.5:
        line += f" Also {manager.name} {rng.choice(others[:4]).text}."
    else:
        line += f" {rng.choice(_TAGS)}"
    return line


_COPE = (
    "It was a bad matchup week.",
    "Everyone I played had their season high against me.",
    "I had the second most points. Again.",
    "The projections had me winning by 14.",
    "My guys were all in blowouts by the third quarter.",
    "Injuries. Just an unbelievable amount of injuries.",
    "Waiver order has been rigged since week 1.",
    "I would be 8-2 in literally any other schedule.",
    "That call in the fourth quarter cost me nine points.",
    "The kicker missed two chip shots, which is not a coaching decision.",
    "I was without my first-round pick for six weeks and nobody mentions it.",
    "Points against leader. Look it up.",
)


def cope(
    view: LeagueView,
    manager: Manager | None,
    results: dict[int, list[WeekResult]],
    rng: random.Random | None = None,
) -> str:
    """An excuse, seeded with a real grievance when the data supplies one."""
    rng = rng or random
    lines: list[str] = []

    if manager is not None:
        mine = [r for r in results.get(manager.roster_id, []) if r.opponent_id is not None]
        if mine:
            unlucky = [r for r in mine if r.outcome == "L" and r.points >= 110]
            if unlucky:
                worst = max(unlucky, key=lambda r: r.points)
                lines.append(
                    f"I scored {pts(worst.points)} in week {worst.week} and lost. "
                    f"Explain that one to me."
                )
            near = [r for r in mine if r.outcome == "L" and abs(r.margin) < 5]
            if near:
                lines.append(
                    f"Week {near[0].week} was decided by {pts(abs(near[0].margin))}. "
                    f"That is not a loss, that is a rounding error."
                )
            if manager.fpts_against > manager.fpts:
                lines.append(
                    f"I have faced {pts(manager.fpts_against - manager.fpts)} more points "
                    f"than I have scored. There is no defence in this game."
                )
            if manager.ppts > manager.fpts + 150:
                lines.append(
                    f"My roster was good for {pts(manager.ppts)} and I have {pts(manager.fpts)}. "
                    f"The talent is there."
                )

    lines.extend(rng.sample(_COPE, k=min(2, len(_COPE))))
    return rng.choice(lines[:4] if lines else _COPE)


_HEXES = (
    "fumbles inside the five",
    "gets vultured on all three goal-line carries",
    "is limited in Friday practice and nobody says why",
    "catches nine passes for thirty yards",
    "gets ejected in the first quarter",
    "is out-targeted by a fullback",
    "has two touchdowns called back for holding",
    "is benched for a rookie at halftime",
    "gets the ball on 3rd and 22 and nowhere else",
    "is questionable, then active, then inactive at 12:58",
    "runs for 140 yards in a game you left him on the bench",
    "gets traded on Tuesday to a team on bye",
)


def curse(player_name: str, rng: random.Random | None = None) -> str:
    rng = rng or random
    return f"🕯️ Hex placed on {player_name}: {rng.choice(_HEXES)}."
