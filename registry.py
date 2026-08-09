"""Command registry and dispatch.

Adding a command is one decorator and one function -- nothing in this file
changes::

    @command("roll", help="Roll dice, e.g. !roll 2d6")
    def roll(args: str, ctx: CommandContext) -> str:
        return "..."

Handlers receive the raw argument string and a CommandContext. Returning a
string sends it as a reply; returning None or "" sends nothing.

Exception isolation lives here: a handler that raises is logged with a
traceback and produces an apologetic one-liner. It never propagates into the
webhook worker, and never affects any other command.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from logging_setup import get_logger

log = get_logger(__name__)

CommandHandler = Callable[[str, "CommandContext"], Optional[str]]


@dataclass
class CommandContext:
    """Everything a handler is allowed to know about the incoming message."""

    chat_guid: str
    sender_name: str
    sender_address: str
    text: str  # full original message text, including the command
    is_group: bool
    command: str  # the matched command name (canonical, not the alias used)
    message_guid: str = ""
    # Optional header title. A handler may set this to something more useful
    # than its own name ("scores wk17"); the send path falls back to the
    # command name when it is empty. Keep it short -- the prefix eats about
    # half the header line.
    title: str = ""
    client: object = None  # BlueBubblesClient
    config: object = None  # Config
    history: object = None  # ChatHistory


@dataclass
class Command:
    name: str
    handler: CommandHandler
    help: str = ""
    usage: str = ""
    aliases: tuple[str, ...] = ()
    hidden: bool = False
    # Purely cosmetic: !help groups by this. Optional, so registering a command
    # is still one decorator and one function.
    category: str = ""

    @property
    def display_usage(self) -> str:
        return self.usage or self.name


_COMMANDS: dict[str, Command] = {}
_ALIASES: dict[str, str] = {}


def command(
    name: str,
    *,
    help: str = "",
    usage: str = "",
    aliases: tuple[str, ...] | list[str] = (),
    hidden: bool = False,
    category: str = "",
) -> Callable[[CommandHandler], CommandHandler]:
    """Register a command handler under ``name`` (plus any aliases)."""

    def decorator(fn: CommandHandler) -> CommandHandler:
        key = name.lower()
        if key in _COMMANDS:
            raise ValueError(f"duplicate command registration: {name}")
        cmd = Command(
            name=key,
            handler=fn,
            help=help or (fn.__doc__ or "").strip().split("\n")[0],
            usage=usage,
            aliases=tuple(a.lower() for a in aliases),
            hidden=hidden,
            category=category,
        )
        _COMMANDS[key] = cmd
        for alias in cmd.aliases:
            if alias in _ALIASES or alias in _COMMANDS:
                raise ValueError(f"duplicate command alias: {alias}")
            _ALIASES[alias] = key
        return fn

    return decorator


def get_command(name: str) -> Command | None:
    key = name.lower()
    if key in _COMMANDS:
        return _COMMANDS[key]
    aliased = _ALIASES.get(key)
    return _COMMANDS.get(aliased) if aliased else None


def all_commands(include_hidden: bool = False) -> list[Command]:
    cmds = _COMMANDS.values()
    if not include_hidden:
        cmds = [c for c in cmds if not c.hidden]
    return sorted(cmds, key=lambda c: c.name)


def parse_command(text: str, prefix: str) -> tuple[str, str] | None:
    """Split ``!weather nyc`` into ``("weather", "nyc")``.

    Returns None when the text is not a command invocation at all.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith(prefix):
        return None
    body = stripped[len(prefix) :].strip()
    if not body:
        return None
    parts = body.split(None, 1)
    name = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return (name, args)


def dispatch(name: str, args: str, ctx: CommandContext) -> str | None:
    """Run a command, isolating any exception it throws.

    Returns the reply text, or None if there is nothing to send. An unknown
    command returns None -- silently dropped, so the bot stays quiet in chats
    where people use ``!`` for other reasons.
    """
    cmd = get_command(name)
    if cmd is None:
        log.info(
            "unknown command, dropping",
            extra={"fields": {"command": name, "chat_guid": ctx.chat_guid}},
        )
        return None

    ctx.command = cmd.name
    started = time.monotonic()
    try:
        reply = cmd.handler(args, ctx)
    except Exception:
        log.exception(
            "command handler raised",
            extra={"fields": {"command": cmd.name, "chat_guid": ctx.chat_guid, "args": args}},
        )
        return f"Sorry, `{cmd.name}` blew up. The error is in the bot log."

    log.info(
        "command handled",
        extra={
            "fields": {
                "command": cmd.name,
                "chat_guid": ctx.chat_guid,
                "sender": ctx.sender_name or ctx.sender_address,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "replied": bool(reply),
            }
        },
    )

    if reply is None:
        return None
    if not isinstance(reply, str):
        log.warning(
            "handler returned a non-string; coercing",
            extra={"fields": {"command": cmd.name, "type": type(reply).__name__}},
        )
        reply = str(reply)

    reply = reply.strip()
    if not reply:
        return None

    # The line-width budget, applied once at the command boundary so that what
    # a handler returns is what the phone actually shows -- logs, !preview and
    # tests all see the same thing. A handler is free to write prose; it does
    # not have to know that an iMessage bubble is ~30 columns wide. The send
    # path applies the same rule again after stamping the bot prefix.
    from ffformat import enforce_width

    return enforce_width(reply)
