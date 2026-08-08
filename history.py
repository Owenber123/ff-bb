"""Per-chat rolling message history, keyed by chat GUID.

Nothing in the current command set needs this. It exists as the seam for a
future AI-backed handler: a handler can call ``ctx.history.recent(ctx.chat_guid)``
and get the last N turns in a chat without any change to the dispatch plumbing.

In-memory and bounded on purpose -- no persistence, no unbounded growth. Swap
the implementation for a SQLite-backed one later and the handler API is unchanged.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Deque

MAX_TRACKED_CHATS = 200


@dataclass
class Turn:
    role: str  # "user" or "bot"
    sender: str
    text: str
    ts: float = field(default_factory=time.time)


class ChatHistory:
    def __init__(self, max_turns: int = 50):
        self.max_turns = max_turns
        self._lock = threading.Lock()
        self._chats: OrderedDict[str, Deque[Turn]] = OrderedDict()

    def _bucket(self, chat_guid: str) -> Deque[Turn]:
        bucket = self._chats.get(chat_guid)
        if bucket is None:
            bucket = deque(maxlen=self.max_turns)
            self._chats[chat_guid] = bucket
            while len(self._chats) > MAX_TRACKED_CHATS:
                self._chats.popitem(last=False)
        self._chats.move_to_end(chat_guid)
        return bucket

    def record(self, chat_guid: str, role: str, sender: str, text: str) -> None:
        if not chat_guid or not text:
            return
        with self._lock:
            self._bucket(chat_guid).append(Turn(role=role, sender=sender, text=text))

    def recent(self, chat_guid: str, limit: int | None = None) -> list[Turn]:
        with self._lock:
            bucket = self._chats.get(chat_guid)
            if not bucket:
                return []
            turns = list(bucket)
        return turns[-limit:] if limit else turns

    def chat_count(self) -> int:
        with self._lock:
            return len(self._chats)
