"""Loop guards: stopping the bot from answering itself.

Bot replies come from the same Apple ID as the operator, so every outgoing
message comes back as a webhook with ``isFromMe: true``. We still want to accept
commands typed from that Apple ID, so ``isFromMe`` alone cannot be the
discriminator. Three layers, checked in this order by ``app.process_event``:

  1. **Bot prefix, regardless of sender.** Every outgoing message is stamped
     with ``BB_BOT_PREFIX`` (default ``🤖 ``) at the send choke point, so
     anything arriving with that prefix is machine-generated and is dropped.
     This also makes automated messages visible to everyone in the chat, and it
     pushes the command prefix into mid-string, where ``parse_command`` will not
     match it.

  2. **isFromMe without a command prefix.** Our own Apple ID chattering is not
     addressed to the bot. Our own Apple ID starting a message with the command
     prefix is the operator issuing a command, and is processed.

  3. **Sent-text tracker** (``SentRegistry``, below). Remembers what we recently
     sent and rejects an ``isFromMe`` message that matches.

Layer 3 earns its place because layer 1 depends on a *visible, typeable* string:
a person can type the prefix by hand -- and with an emoji prefix that is not
even far-fetched -- and a relay, quote, or a second bot
instance can alter or drop the prefix in transit. So layer 3 deliberately
compares with the bot prefix normalised **away** — it must not share layer 1's
assumption that the prefix arrives intact.

Layer 3 matches on message GUID when one is available (exact, no false
positives) and otherwise on normalised text within a short TTL. The TTL is short
on purpose: a real echo loop fires within a second or two, whereas matching on
text forever would eventually swallow a genuine command that happened to repeat
something the bot said.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

# Long enough to cover an echo (which arrives in ~1-2s), short enough that a
# genuine repeat of the same text is unlikely to be swallowed.
DEFAULT_TTL_SECONDS = 120.0
DEFAULT_MAX_ENTRIES = 200

# Zero-width characters are stripped for comparison only. They are not used as
# a guard, but they are trivial for a copy/paste to introduce.
_INVISIBLES = ("​", "﻿", "‎", "‏")


def apply_bot_prefix(text: str, bot_prefix: str) -> str:
    """Stamp an outgoing message with the bot prefix, once, at the top.

    Idempotent: text that already carries the prefix is returned unchanged, so
    a retry or a handler that stamped its own output cannot double it. A prefix
    with no trailing whitespace gets one space added, so ``BB_BOT_PREFIX=[bot]``
    renders as ``[bot] pong`` rather than ``[bot]pong``.
    """
    if not text or not bot_prefix:
        return text
    if has_bot_prefix(text, bot_prefix):
        return text
    separator = "" if bot_prefix[-1:].isspace() else " "
    return f"{bot_prefix}{separator}{text}"


def has_bot_prefix(text: str, bot_prefix: str) -> bool:
    """True when ``text`` looks machine-generated.

    Compares against the whitespace-stripped prefix and case-insensitively, so
    ``🤖pong``, ``🤖 pong`` and (for a text prefix) ``BOT: pong`` all
    match. An
    empty ``bot_prefix`` disables the layer entirely -- without this guard every
    string would "start with" the empty prefix and nothing would ever be
    processed.
    """
    if not text or not bot_prefix:
        return False
    needle = bot_prefix.strip().casefold()
    if not needle:
        return False
    return _strip_invisibles(text).lstrip().casefold().startswith(needle)


def strip_bot_prefix(text: str, bot_prefix: str) -> str:
    """Remove a leading bot prefix, if present."""
    if not has_bot_prefix(text, bot_prefix):
        return text
    body = _strip_invisibles(text).lstrip()
    return body[len(bot_prefix.strip()) :].lstrip()


def _strip_invisibles(text: str) -> str:
    for char in _INVISIBLES:
        text = text.replace(char, "")
    return text


def normalize(text: str, bot_prefix: str = "") -> str:
    """Compare-form used by SentRegistry.

    The bot prefix is stripped here on purpose: layer 3 must still recognise our
    own text if the prefix was altered or removed somewhere in the iMessage
    path. That independence is what makes it a real second opinion rather than a
    restatement of layer 1.
    """
    if not text:
        return ""
    return _strip_invisibles(strip_bot_prefix(text, bot_prefix)).strip().casefold()


class SentRegistry:
    """Recently-sent messages, for rejecting our own echoes."""

    def __init__(
        self,
        bot_prefix: str = "",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ):
        self.bot_prefix = bot_prefix
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._by_text: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._by_guid: OrderedDict[str, float] = OrderedDict()

    def _prune(self, now: float) -> None:
        for store in (self._by_text, self._by_guid):
            while store:
                key, stamp = next(iter(store.items()))
                if now - stamp > self.ttl:
                    store.pop(key)
                else:
                    break
            while len(store) > self.max_entries:
                store.popitem(last=False)

    def record(self, chat_guid: str, text: str, message_guid: str | None = None) -> None:
        now = time.time()
        with self._lock:
            key = (chat_guid, normalize(text, self.bot_prefix))
            self._by_text.pop(key, None)
            self._by_text[key] = now
            if message_guid:
                self._by_guid.pop(message_guid, None)
                self._by_guid[message_guid] = now
            self._prune(now)

    def matches(self, chat_guid: str, text: str, message_guid: str | None = None) -> bool:
        now = time.time()
        with self._lock:
            self._prune(now)
            if message_guid and message_guid in self._by_guid:
                return True
            stamp = self._by_text.get((chat_guid, normalize(text, self.bot_prefix)))
            return stamp is not None and (now - stamp) <= self.ttl

    def size(self) -> int:
        with self._lock:
            return len(self._by_text)
