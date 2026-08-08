"""Text helpers for iMessage.

iMessage renders plain text in a **proportional** font. Two consequences drive
everything here:

  1. No markdown. Asterisks, backticks and pipes show up literally, so there
     are no bold, no code spans and no tables.
  2. Space padding does not align anything. ``f"{name:<14}"`` produces ragged
     columns on a phone, not a grid. So we separate fields with a middle dot
     and keep every line short enough not to wrap, rather than trying to build
     columns out of whitespace.

Emoji are the only visual structure available, and they do render, so rank
medals and section markers carry the layout.

One message beats three: a send is ~800ms plus a minimum interval, so
everything here is built to produce a single body, and :func:`fit` trims it to
the client's cap rather than letting bbclient truncate mid-word.
"""

from __future__ import annotations

import re
import unicodedata

# bbclient truncates at 4000; stay under it so we choose where the cut lands.
MAX_BODY = 3500

DOT = " · "  # middle dot -- reads cleanly and is not markdown
MEDALS = ("\U0001f947", "\U0001f948", "\U0001f949")  # 1st/2nd/3rd place medals


def pts(value: float | int | None, places: int = 1) -> str:
    """Points, at a phone-readable precision. 1436.0 not 1436.00."""
    if value is None:
        return "-"
    return f"{float(value):.{places}f}"


def signed(value: float | int | None, places: int = 1) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.{places}f}"


def money(cents: int) -> str:
    """Whole dollars stay whole: $20, not $20.00."""
    dollars = cents / 100.0
    if abs(cents) % 100 == 0:
        return f"${int(dollars):,}"
    return f"${dollars:,.2f}"


def parse_money(text: str) -> int | None:
    """'$12.50' / '12.5' / '5' -> cents. None if it is not an amount."""
    cleaned = (text or "").strip().lstrip("$").replace(",", "").replace("_", "")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return int(round(value * 100))


def record(wins: int, losses: int, ties: int = 0) -> str:
    return f"{wins}-{losses}" + (f"-{ties}" if ties else "")


def ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many if many is not None else one + "s")


def rank_marker(index: int) -> str:
    """Medal for the top three, plain number after that."""
    if index < 3:
        return MEDALS[index]
    return f"{index + 1}."


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def fit(lines: list[str], limit: int = MAX_BODY, note: str = "") -> str:
    """Join lines into one body, dropping whole lines rather than cutting words."""
    body = "\n".join(line for line in lines if line is not None)
    if len(body) <= limit:
        return body

    kept: list[str] = []
    used = 0
    tail = f"\n… ({note})" if note else "\n…"
    budget = limit - len(tail)
    for line in lines:
        if line is None:
            continue
        cost = len(line) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept) + tail


def freshness(age_seconds: float, stale: bool = False) -> str:
    """The caveat line. Say the data is old; never quietly serve it as live.

    Returns "" when the data is fresh enough not to be worth a line.
    """
    from sleeper import describe_age

    if stale:
        return f"⚠️ Sleeper is not responding — showing data from {describe_age(age_seconds)}"
    if age_seconds >= 300:
        return f"(as of {describe_age(age_seconds)})"
    return ""


def with_freshness(body: str, age_seconds: float, stale: bool = False) -> str:
    note = freshness(age_seconds, stale)
    return f"{body}\n{note}" if note else body


# ---------------------------------------------------------------------------
# name matching
# ---------------------------------------------------------------------------

_PUNCT = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Casefold, strip accents and punctuation. 'Go birds 🦅' -> 'gobirds'."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _PUNCT.sub("", ascii_only.lower())


def normalize_handle(handle: str) -> str:
    """Phone numbers and emails to a comparable form.

    '+1 (555) 123-4567' and '5551234567' both become '5551234567', because the
    handle in a webhook payload and the one somebody types into .env are rarely
    formatted the same way.
    """
    text = (handle or "").strip().lower()
    if "@" in text:
        return text
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or text


def strip_mention(text: str) -> str:
    """Drop a leading @ so '!roast @barry' and '!roast barry' behave the same."""
    return (text or "").strip().lstrip("@").strip()
