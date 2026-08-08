"""Weekly recap: message counts per person, busiest day, most-used words.

Reads the last N days out of ``POST /api/v1/message/query`` using an ``after``
timestamp in epoch milliseconds, then aggregates locally. Nothing here sends;
the caller decides where the text goes.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from logging_setup import get_logger
from parsing import (
    chat_guid_of,
    is_tapback,
    message_sender,
    message_text,
    message_timestamp_ms,
)

log = get_logger(__name__)

# Small, deliberately boring English stopword list plus the chat-specific
# noise that otherwise dominates every ranking (yeah, lol, ok, ...).
STOPWORDS = {
    "a", "about", "above", "after", "again", "all", "also", "am", "an", "and", "any",
    "are", "aren", "as", "at", "back", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "can", "cant", "come", "could", "couldn",
    "did", "didn", "do", "does", "doesn", "doing", "don", "down", "during", "each",
    "even", "few", "for", "from", "further", "get", "gets", "getting", "go", "going",
    "good", "got", "had", "hadn", "has", "hasn", "have", "haven", "having", "he",
    "her", "here", "hers", "herself", "him", "himself", "his", "how", "i", "if", "im",
    "in", "into", "is", "isn", "it", "its", "itself", "just", "know", "let", "like",
    "ll", "look", "made", "make", "man", "many", "may", "me", "mean", "might", "mine",
    "more", "most", "much", "must", "my", "myself", "need", "no", "nor", "not", "now",
    "of", "off", "oh", "ok", "okay", "on", "once", "one", "only", "or", "other", "ought",
    "our", "ours", "ourselves", "out", "over", "own", "really", "re", "right", "said",
    "same", "say", "see", "shan", "she", "should", "shouldn", "so", "some", "still",
    "such", "sure", "take", "than", "that", "thats", "the", "their", "theirs", "them",
    "themselves", "then", "there", "these", "they", "thing", "things", "think", "this",
    "those", "though", "through", "time", "to", "too", "under", "until", "up", "us",
    "use", "ve", "very", "want", "was", "wasn", "way", "we", "well", "were", "weren",
    "what", "whats", "when", "where", "which", "while", "who", "whom", "why", "will",
    "with", "won", "would", "wouldn", "yeah", "yep", "yes", "yet", "you", "your",
    "yours", "yourself", "yourselves", "lol", "haha", "hahaha", "lmao", "omg", "idk",
    "gonna", "wanna", "gotta", "actually", "basically", "probably", "maybe", "thanks",
    "thank", "please", "sorry", "hey", "hi", "hello",
}

URL_RE = re.compile(r"https?://\S+|www\.\S+")
WORD_RE = re.compile(r"[a-z][a-z'’-]{2,}")
DAY_MS = 86_400_000


@dataclass
class SummaryStats:
    days: int
    start_ms: int
    end_ms: int
    total_messages: int = 0
    per_person: Counter = field(default_factory=Counter)
    per_day: Counter = field(default_factory=Counter)
    per_chat: Counter = field(default_factory=Counter)
    words: Counter = field(default_factory=Counter)
    skipped: int = 0

    @property
    def busiest_day(self) -> tuple[str, int] | None:
        if not self.per_day:
            return None
        return self.per_day.most_common(1)[0]


def _tokenize(text: str) -> Iterable[str]:
    lowered = URL_RE.sub(" ", text.lower()).replace("’", "'")
    for match in WORD_RE.finditer(lowered):
        word = match.group(0).strip("'-")
        # Reduce contractions to their stem so "i'll" and "don't" get caught by
        # the stopword list instead of ranking as distinct words.
        word = word.split("'")[0]
        if len(word) < 3 or word in STOPWORDS:
            continue
        yield word


def collect_stats(
    client,
    *,
    days: int = 7,
    chat_guids: Iterable[str] = (),
    max_messages: int = 5000,
    command_prefix: str = "!",
) -> SummaryStats:
    """Pull the window from the server and aggregate it.

    ``chat_guids`` empty means every chat on the account. When it is populated
    we both ask the server to filter and re-check client-side, since not every
    server build honours ``chatGuid`` on ``message/query``.
    """
    chat_guids = [g for g in chat_guids if g]
    now_ms = int(time.time() * 1000)
    after_ms = now_ms - days * DAY_MS

    stats = SummaryStats(days=days, start_ms=after_ms, end_ms=now_ms)
    wanted = set(chat_guids)

    # One pass per chat when filtering, otherwise a single unfiltered pass.
    sources: list[str | None] = list(chat_guids) if chat_guids else [None]
    budget = max_messages
    seen_guids: set[str] = set()

    for source in sources:
        if budget <= 0:
            break
        for msg in client.iter_messages(
            after=after_ms, chat_guid=source, max_messages=budget
        ):
            if not isinstance(msg, dict):
                continue

            guid = msg.get("guid")
            if guid:
                if guid in seen_guids:
                    continue
                seen_guids.add(guid)

            budget -= 1

            msg_chat = chat_guid_of(msg)
            if wanted and msg_chat and msg_chat not in wanted:
                stats.skipped += 1
                continue

            if is_tapback(msg):
                stats.skipped += 1
                continue

            text = message_text(msg)
            if not text:
                stats.skipped += 1
                continue

            ts = message_timestamp_ms(msg)
            if ts is not None and ts < after_ms:
                stats.skipped += 1
                continue

            stats.total_messages += 1
            stats.per_person[message_sender(msg)] += 1
            if msg_chat:
                stats.per_chat[msg_chat] += 1
            if ts is not None:
                day = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                stats.per_day[day] += 1

            if not text.startswith(command_prefix):
                stats.words.update(_tokenize(text))

    log.info(
        "collected summary stats",
        extra={
            "fields": {
                "days": days,
                "chats": len(chat_guids) or "all",
                "messages": stats.total_messages,
                "skipped": stats.skipped,
            }
        },
    )
    return stats


def render_summary(
    stats: SummaryStats,
    *,
    top_words: int = 10,
    top_people: int = 10,
    chat_names: dict[str, str] | None = None,
    title: str | None = None,
) -> str:
    """Render plain text. iMessage has no markdown, so keep it flat."""
    start = datetime.fromtimestamp(stats.start_ms / 1000).strftime("%b %-d")
    end = datetime.fromtimestamp(stats.end_ms / 1000).strftime("%b %-d")
    header = title or f"Recap: last {stats.days} day{'s' if stats.days != 1 else ''}"
    lines = [f"{header} ({start} - {end})"]

    if not stats.total_messages:
        lines.append("")
        lines.append("No messages in that window.")
        return "\n".join(lines)

    lines.append(f"{stats.total_messages} messages")

    busiest = stats.busiest_day
    if busiest:
        day, count = busiest
        pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%A %b %-d")
        lines.append(f"Busiest day: {pretty} ({count})")

    lines.append("")
    lines.append("Who talked:")
    total = stats.total_messages
    for person, count in stats.per_person.most_common(top_people):
        pct = round(100 * count / total)
        lines.append(f"  {person}: {count} ({pct}%)")
    remaining = len(stats.per_person) - top_people
    if remaining > 0:
        lines.append(f"  ...and {remaining} more")

    if len(stats.per_chat) > 1:
        lines.append("")
        lines.append("Busiest chats:")
        for guid, count in stats.per_chat.most_common(5):
            label = (chat_names or {}).get(guid) or guid
            lines.append(f"  {label}: {count}")

    top = stats.words.most_common(top_words)
    if top:
        lines.append("")
        lines.append("Most-used words:")
        lines.append("  " + ", ".join(f"{word} ({count})" for word, count in top))

    return "\n".join(lines)


def build_summary(
    client,
    *,
    days: int = 7,
    chat_guids: Iterable[str] = (),
    top_words: int = 10,
    max_messages: int = 5000,
    command_prefix: str = "!",
    chat_names: dict[str, str] | None = None,
    title: str | None = None,
) -> str:
    stats = collect_stats(
        client,
        days=days,
        chat_guids=chat_guids,
        max_messages=max_messages,
        command_prefix=command_prefix,
    )
    return render_summary(
        stats, top_words=top_words, chat_names=chat_names, title=title
    )


def chat_name_map(client, guids: Iterable[str]) -> dict[str, str]:
    """Best-effort friendly names for chat GUIDs, for the recap header."""
    wanted = {g for g in guids if g}
    names: dict[str, str] = {}
    if not wanted:
        return names
    try:
        for chat in client.iter_chats():
            guid = chat.get("guid")
            if guid not in wanted:
                continue
            names[guid] = describe_chat(chat)
            if len(names) == len(wanted):
                break
    except Exception:
        log.warning("could not resolve chat names; falling back to GUIDs", exc_info=True)
    return names


def describe_chat(chat: dict[str, Any]) -> str:
    """A human label for a chat: group name, else the participant list."""
    display = (chat.get("displayName") or "").strip()
    if display:
        return display
    participants = chat.get("participants") or []
    names = []
    for person in participants:
        if not isinstance(person, dict):
            continue
        label = (person.get("displayName") or "").strip() or (person.get("address") or "").strip()
        if label:
            names.append(label)
    if names:
        return ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")
    return str(chat.get("guid") or "unknown chat")
