"""The command set.

Adding a command is one decorator and one function in this file. The dispatch
code in registry.py never changes.

    @command("roll", help="Roll dice", usage="roll 2d6")
    def roll(args: str, ctx: CommandContext) -> str | None:
        return "you rolled a 4"

Return a string to reply, or None to stay silent. Raising is safe -- dispatch
catches it, logs the traceback, and keeps the service up.
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timezone

import requests

from logging_setup import get_logger
from registry import CommandContext, all_commands, command, get_command
from summary import build_summary

log = get_logger(__name__)

START_TIME = time.monotonic()
WTTR_TIMEOUT = 12.0


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# --------------------------------------------------------------------------
# ping
# --------------------------------------------------------------------------


@command("ping", help="Check that the bot is alive.")
def cmd_ping(args: str, ctx: CommandContext) -> str:
    return "pong"


# --------------------------------------------------------------------------
# help
# --------------------------------------------------------------------------


# Order matters: this is the order sections appear in !help. Anything with a
# category not listed here falls into "Other".
HELP_SECTIONS: tuple[tuple[str, str], ...] = (
    ("", "General"),
    ("ff-league", "League"),
    ("ff-stats", "Stats"),
    ("ff-talk", "Talk"),
    ("ff-admin", "Admin"),
)

# !help fantasy expands all four fantasy sections at once.
HELP_GROUPS: dict[str, tuple[str, ...]] = {
    "fantasy": ("ff-league", "ff-stats", "ff-talk", "ff-admin"),
    "ff": ("ff-league", "ff-stats", "ff-talk", "ff-admin"),
}

# What a new person sees. Three commands, not thirty-five.
#
# A menu of thirty-five is a menu nobody reads: people try three things and
# never type "!" again. So the default advertises the two that are useful
# during games and the one that makes somebody screenshot it for a friend,
# and mentions that the rest exist. Everything still works; only the shop
# window changed.
# Each entry has to fit one bubble line (~30 columns, less the bot prefix on
# the first), so the blurbs are three words, not a sentence. The emoji is the
# anchor the eye lands on.
HEADLINE_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("🏈", "scores", "live scores"),
    ("📊", "standings", "the table"),
    ("🔥", "roast <name>", ""),
)


@command("help", help="List commands, or explain one.", usage="help [command]", aliases=("commands",))
def cmd_help(args: str, ctx: CommandContext) -> str:
    """Three commands by default; everything behind ``!help all``.

    ``!help <command>`` explains one, and ``!help <section>`` lists a category.
    """
    prefix = getattr(ctx.config, "command_prefix", "!")

    if not args:
        lines = [
            f"{icon} {prefix}{usage}:  {blurb}" if blurb else f"{icon} {prefix}{usage}"
            for icon, usage, blurb in HEADLINE_COMMANDS
        ]
        hidden = len(all_commands()) - len(HEADLINE_COMMANDS)
        # Kept deliberately: without it nothing points at the other commands,
        # and the short menu becomes a dead end rather than a front door.
        lines += ["", f"{prefix}help all for {hidden} more"]
        return "\n".join(lines)

    wanted = args.split()[0].lstrip(prefix).lower()

    if wanted in ("all", "everything", "more"):
        return _full_listing(prefix)

    # !help league / !help fantasy list whole sections with descriptions.
    wanted_categories = HELP_GROUPS.get(wanted)
    if wanted_categories is None:
        for category, title in HELP_SECTIONS:
            if category and wanted in (category, title.lower()):
                wanted_categories = (category,)
                break
    if wanted_categories:
        return _section_listing(prefix, wanted_categories)

    cmd = get_command(wanted)
    if cmd is None:
        return f"No command called {wanted!r}. Try {prefix}help all."
    lines = [f"{prefix}{cmd.display_usage}"]
    if cmd.help:
        lines.append(cmd.help)
    if cmd.aliases:
        lines.append("Aliases: " + ", ".join(prefix + a for a in cmd.aliases))
    return "\n".join(lines)


def _full_listing(prefix: str) -> str:
    """Every command, grouped by section. What !help used to be."""
    grouped: dict[str, list[str]] = {}
    for cmd in all_commands():
        grouped.setdefault(cmd.category, []).append(cmd.name)

    lines = []
    seen = set()
    for category, title in HELP_SECTIONS:
        names = grouped.get(category)
        if not names:
            continue
        seen.add(category)
        lines.append(f"{title}: " + " ".join(prefix + name for name in names))

    leftovers = [name for cat, names in grouped.items() if cat not in seen for name in names]
    if leftovers:
        lines.append("Other: " + " ".join(prefix + name for name in sorted(leftovers)))

    lines.append(f"{prefix}help <command> for detail.")
    return "\n".join(lines)


def _section_listing(prefix: str, categories: tuple[str, ...]) -> str:
    titles = dict(HELP_SECTIONS)
    lines: list[str] = []
    for category in categories:
        section = [c for c in all_commands() if c.category == category]
        if not section:
            continue
        lines.append(f"{titles.get(category, category)}:")
        for cmd in section:
            lines.append(f"  {prefix}{cmd.display_usage}" + (f" - {cmd.help}" if cmd.help else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# weather -- wttr.in needs no API key
# --------------------------------------------------------------------------


@command(
    "weather",
    help="Current conditions. Defaults to BB_DEFAULT_WEATHER_LOCATION.",
    usage="weather [location]",
    aliases=("wttr",),
)
def cmd_weather(args: str, ctx: CommandContext) -> str:
    location = args.strip() or getattr(ctx.config, "default_weather_location", "") or ""
    if not location:
        return "Give me a location, e.g. !weather brooklyn (or set BB_DEFAULT_WEATHER_LOCATION)."

    units = (os.environ.get("BB_WEATHER_UNITS") or "u").strip().lower()
    fmt = "%l: %C, %t (feels %f), humidity %h, wind %w"
    params = {"format": fmt}
    if units in ("u", "m"):
        params[units] = ""

    try:
        resp = requests.get(
            f"https://wttr.in/{requests.utils.quote(location)}",
            params=params,
            timeout=WTTR_TIMEOUT,
            headers={"User-Agent": "curl/8"},  # wttr.in serves plain text to curl UAs
        )
    except requests.RequestException as exc:
        log.warning("wttr.in request failed", extra={"fields": {"location": location, "error": str(exc)}})
        return "Couldn't reach wttr.in just now."

    body = resp.text.strip()
    if resp.status_code != 200 or not body or "Unknown location" in body:
        return f"wttr.in didn't recognise {location!r}."
    # wttr.in returns its own error prose on bad input; keep replies short.
    return body.splitlines()[0][:400]


# --------------------------------------------------------------------------
# status -- the Mac is the fragile dependency, so make it easy to check
# --------------------------------------------------------------------------


@command("status", help="Bot uptime and BlueBubbles server reachability.", aliases=("uptime",))
def cmd_status(args: str, ctx: CommandContext) -> str:
    lines = [f"Bot up {_fmt_duration(time.monotonic() - START_TIME)}"]

    client = ctx.client
    if client is not None:
        started = time.monotonic()
        try:
            client.ping()
            elapsed = round((time.monotonic() - started) * 1000)
            lines.append(f"BlueBubbles: reachable ({elapsed}ms)")
        except Exception as exc:  # surfaced to the chat on purpose
            lines.append(f"BlueBubbles: UNREACHABLE - {exc}")

    history = ctx.history
    if history is not None:
        lines.append(f"Tracking {history.chat_count()} chat(s) in memory")

    lines.append(datetime.now(timezone.utc).astimezone().strftime("Local time %Y-%m-%d %H:%M:%S %Z"))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# chatinfo -- the fastest way to fill in BB_ALLOWED_CHATS
# --------------------------------------------------------------------------


@command(
    "chatinfo",
    help="Show this chat's GUID, for the allowlist.",
    aliases=("guid", "whereami"),
)
def cmd_chatinfo(args: str, ctx: CommandContext) -> str:
    kind = "group" if ctx.is_group else "DM"
    sender = ctx.sender_name or ctx.sender_address or "unknown"
    allowed = "yes"
    cfg = ctx.config
    if cfg is not None and hasattr(cfg, "is_chat_allowed"):
        allowed = "yes" if cfg.is_chat_allowed(ctx.chat_guid) else "no"
    return "\n".join(
        [
            f"Chat GUID: {ctx.chat_guid}",
            f"Type: {kind}",
            f"You: {sender}" + (f" ({ctx.sender_address})" if ctx.sender_name else ""),
            f"Allowlisted: {allowed}",
        ]
    )


# --------------------------------------------------------------------------
# recap -- the weekly summary, on demand, for this chat only
# --------------------------------------------------------------------------


@command(
    "recap",
    help="Summarise this chat's last N days (default 7, max 30).",
    usage="recap [days]",
    aliases=("summary",),
)
def cmd_recap(args: str, ctx: CommandContext) -> str:
    days = 7
    if args.strip():
        try:
            days = int(args.split()[0])
        except ValueError:
            return "Usage: !recap [days]"
        days = max(1, min(days, 30))

    cfg = ctx.config
    return build_summary(
        ctx.client,
        days=days,
        chat_guids=[ctx.chat_guid],
        top_words=getattr(cfg, "summary_top_words", 10),
        max_messages=getattr(cfg, "summary_max_messages", 5000),
        command_prefix=getattr(cfg, "command_prefix", "!"),
        title=f"Recap: last {days} day{'s' if days != 1 else ''} here",
    )


# --------------------------------------------------------------------------
# roll -- small, and a harmless smoke test for argument parsing
# --------------------------------------------------------------------------


@command("roll", help="Roll dice, e.g. 2d6. Defaults to 1d20.", usage="roll [NdM]")
def cmd_roll(args: str, ctx: CommandContext) -> str:
    spec = (args.strip() or "1d20").lower().replace(" ", "")
    count, _, sides = spec.partition("d")
    try:
        count = int(count or 1)
        sides = int(sides or 20)
    except ValueError:
        return "Usage: !roll 2d6"
    if not (1 <= count <= 20) or not (2 <= sides <= 1000):
        return "Keep it to 1-20 dice with 2-1000 sides."

    rolls = [random.randint(1, sides) for _ in range(count)]
    if count == 1:
        return f"{rolls[0]} (d{sides})"
    return f"{' + '.join(map(str, rolls))} = {sum(rolls)}"
