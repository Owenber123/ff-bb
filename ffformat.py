"""Text helpers for iMessage.

iMessage renders plain text in a **proportional** font. Three consequences
drive everything here:

  1. No markdown. Asterisks, backticks and pipes show up literally, so there
     are no bold, no code spans and no tables.
  2. Space padding does not align anything, and never will. ``f"{name:<14}"``
     produces ragged columns on a phone, not a grid.
  3. **A bubble fits roughly 30 characters at default text size.** Anything
     longer soft-wraps wherever it happens to land, which is what makes a
     "table" built from spaces look broken.

So the layout rule is a hard line-length budget, not padding. :data:`MAX_LINE`
is that budget, :func:`display_width` measures against it (emoji count double,
because they render double), and :func:`enforce_width` is the backstop that
re-wraps anything over it. bbclient applies that backstop to every outgoing
message *after* stamping the bot prefix, so the prefix counts against the first
line's budget rather than silently blowing it.

Emoji are the only visual structure available and they do render, so medals and
section markers carry the layout. Blank lines between sections cost a line each
and are worth it: scanning a phone bubble is much easier with them.

One message beats three: a send is ~800ms plus a minimum interval, so
everything here is built to produce a single body, and :func:`fit` trims it to
the client's cap rather than letting bbclient truncate mid-word.
"""

from __future__ import annotations

import re
import unicodedata

# bbclient truncates at 4000; stay under it so we choose where the cut lands.
MAX_BODY = 3500

# The line budget. An iMessage bubble fits ~30 characters at default text size.
MAX_LINE = 30

# Manager names are truncated to this before rendering. Two 9-character names
# plus two scores and a separator is exactly MAX_LINE, which is why the pair
# helpers below trim further when they have to.
NAME_WIDTH = 9

DOT = " · "  # middle dot -- reads cleanly and is not markdown
SCORE_SEP = " › "  # single-glyph "beat", far cheaper than " def. "
MEDALS = ("\U0001f947", "\U0001f948", "\U0001f949")  # 1st/2nd/3rd place medals


# ---------------------------------------------------------------------------
# width
# ---------------------------------------------------------------------------

# Ranges that render as full-width emoji without being flagged East-Asian-Wide.
_EMOJI_RANGES = (
    (0x2190, 0x21FF), (0x2300, 0x23FF), (0x2460, 0x24FF), (0x25A0, 0x27BF),
    (0x2B00, 0x2BFF), (0x1F000, 0x1FAFF),
)
_ZERO_WIDTH = {"️", "︎", "‍", "​", "⁠"}


def _char_width(char: str) -> int:
    if char in _ZERO_WIDTH or unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    code = ord(char)
    for low, high in _EMOJI_RANGES:
        if low <= code <= high:
            return 2
    return 1


def display_width(text: str) -> int:
    """Roughly how wide ``text`` renders, in average-character units.

    Emoji and CJK count double because they are drawn double. Variation
    selectors, ZWJ and combining marks count zero. This is an approximation --
    the font is proportional, so an 'i' and a 'W' differ -- but it is a far
    better predictor of wrapping than ``len()``, which counts 🏈 as one.
    """
    return sum(_char_width(ch) for ch in text or "")


def too_wide(text: str, budget: int = MAX_LINE) -> bool:
    return display_width(text) > budget


def clip(text: str, budget: int) -> str:
    """Cut to ``budget`` display columns. No ellipsis -- it costs a column."""
    if display_width(text) <= budget:
        return text
    out, used = [], 0
    for char in text:
        width = _char_width(char)
        if used + width > budget:
            break
        out.append(char)
        used += width
    return "".join(out).rstrip()


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


_TRAILING_DIGITS = re.compile(r"\d+$")
_CAMEL_HEAD = re.compile(r"^[A-Za-z][a-z]+")


def short_name(name: str, limit: int = NAME_WIDTH) -> str:
    """Shorten a manager name to fit the line budget. One helper, used everywhere.

    Sleeper display names are long and mostly padding -- a first name plus a
    surname plus the year somebody signed up. Four passes, cheapest first:

        Diane Danvers Simmons -> Diane        first word of a multi-word name
        Ryanmd211             -> Ryanmd       trailing signup digits
        JubbysGiants          -> Jubbys       camelCase head
        finnmaloney96         -> finnmalon    plain clip, last resort

    A single lowercase run like ``charliemalone`` has no boundary to find, so
    it clips to ``charliema``. ``!nickname charliemalone charlie`` is the fix,
    and nicknames come through this same function, so a long nickname is
    shortened identically rather than blowing the budget.
    """
    text = (name or "").strip()
    if not text:
        return ""

    # 1. Trailing signup digits carry no information, so they go unconditionally
    #    -- "Ryanmd211" reads better as "Ryanmd" even though it already fits.
    stripped = _TRAILING_DIGITS.sub("", text).rstrip()
    if len(stripped) >= 3:
        text = stripped

    # 2. Multi-word: keep the fewest leading words that make a usable name, so
    #    "A Top Shagger" becomes "A Top" rather than being cut mid-word.
    if display_width(text) > limit and " " in text:
        words = text.split()
        for count in range(1, len(words)):
            candidate = " ".join(words[:count])
            if len(candidate) >= 3:
                text = candidate
                break

    # 3. camelCase head, if it is substantial enough to stand alone.
    if display_width(text) > limit:
        match = _CAMEL_HEAD.match(text)
        if match and len(match.group(0)) >= 4:
            text = match.group(0)

    return clip(text, limit)


def score_line(
    left: str, left_points, right: str, right_points, budget: int = MAX_LINE
) -> str:
    """``TKoop 136.6 › charlie 124.3``, guaranteed to fit.

    Two 9-character names, two 5-character scores and the separator come to
    exactly 30, so a long pair has no slack at all. Rather than let that one
    line wrap and ruin the block, the names are trimmed further -- the longer
    one first, so a short name is never punished for its opponent.
    """
    left_score, right_score = pts(left_points), pts(right_points)
    fixed = display_width(SCORE_SEP) + display_width(left_score) + display_width(right_score) + 2
    room = max(8, budget - fixed)

    a, b = short_name(left), short_name(right)
    while display_width(a) + display_width(b) > room and (len(a) > 4 or len(b) > 4):
        if display_width(a) >= display_width(b) and len(a) > 4:
            a = clip(a, display_width(a) - 1)
        elif len(b) > 4:
            b = clip(b, display_width(b) - 1)
        else:
            break
    return f"{a} {left_score}{SCORE_SEP}{b} {right_score}"


def wrap_line(text: str, budget: int = MAX_LINE, indent: str = "  ") -> list[str]:
    """Break one overlong line at word boundaries.

    A word longer than the budget on its own -- a chat GUID, a URL -- is left
    intact rather than cut, because a GUID broken across lines cannot be
    copied, which defeats the point of printing it.
    """
    if display_width(text) <= budget:
        return [text]

    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        prefix = indent if lines else ""
        if display_width(prefix + candidate) <= budget or not current:
            current = candidate
        else:
            lines.append((indent if lines else "") + current)
            current = word
    if current:
        lines.append((indent if lines else "") + current)
    return lines


def title_budget(prefix: str, budget: int = MAX_LINE) -> int:
    """Columns left for the title after the prefix on the header line."""
    return max(6, budget - display_width(prefix))


def compose_message(prefix: str, title: str, body: str, budget: int = MAX_LINE) -> str:
    """The house style for every outgoing message::

        🤖 bloqBot - scores
        <blank>
        TKoop 136.6 › charliema 124.3
        ...

    The header is its own line rather than being glued to the front of the
    first content line. That costs two lines and buys three things: the marker
    is unmissable, the body starts at column zero so it lines up with itself,
    and the title says what the message *is* without the body having to repeat
    it.

    Titles are short by necessity -- the prefix eats about half the line -- so
    they are identifiers ("scores", "recap wk14"), not sentences.
    """
    body = (body or "").strip()
    title = clip((title or "").strip(), title_budget(prefix, budget))
    header = f"{prefix}{title}".rstrip() if title else prefix.rstrip()
    if not body:
        return header
    return f"{header}\n\n{body}"


def enforce_width(text: str, budget: int = MAX_LINE) -> str:
    """The backstop. Re-wrap any line over budget.

    Applied by bbclient to every outgoing message after the bot prefix is
    stamped, so nothing -- a command, a scheduled post, the weekly summary, or
    anything added later -- can put a wall of text on somebody's phone.
    """
    if not text:
        return text
    out: list[str] = []
    for line in text.split("\n"):
        out.extend(wrap_line(line, budget))
    return "\n".join(out)


def fit(lines: list[str], limit: int = MAX_BODY, note: str = "") -> str:
    """Join lines into one body, dropping whole lines rather than cutting words.

    Also applies the per-line width budget, so every caller gets it for free.
    """
    # Split embedded newlines first. A caller may pass a two-line block as one
    # entry (_header does, for the season caveat); measuring that as a single
    # line makes it look over budget and wraps a line that already fitted.
    lines = [
        wrapped
        for entry in lines
        if entry is not None
        for line in str(entry).split("\n")
        for wrapped in wrap_line(line)
    ]
    body = "\n".join(lines)
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

    Returns "" when the data is fresh enough not to be worth a line. Kept
    inside the line budget: "Sleeper down · 4 min ago" beats a full sentence
    that wraps onto three lines of an already-long reply.
    """
    from sleeper import describe_age

    if stale:
        return f"⚠️ Sleeper down · {describe_age(age_seconds)}"
    if age_seconds >= 300:
        return f"(as of {describe_age(age_seconds)})"
    return ""


def with_freshness(body: str, age_seconds: float, stale: bool = False) -> str:
    note = freshness(age_seconds, stale)
    return enforce_width(f"{body}\n{note}") if note else body


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
