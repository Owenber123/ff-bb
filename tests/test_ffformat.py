"""The formatting helpers.

iMessage is plain text in a proportional font, so these mostly exist to keep
replies short, keep markdown characters out, and make the freshness caveat
honest.
"""

from __future__ import annotations

from ffformat import (
    DOT,
    MAX_BODY,
    fit,
    freshness,
    money,
    normalize,
    normalize_handle,
    ordinal,
    parse_money,
    plural,
    pts,
    rank_marker,
    record,
    signed,
    strip_mention,
    truncate,
    with_freshness,
)


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------


def test_points_render_at_phone_precision():
    assert pts(1436.05) == "1436.0"  # binary float sits just under the midpoint
    assert pts(1436.28) == "1436.3"
    assert pts(0) == "0.0"
    assert pts(None) == "-"
    assert pts(1436.05, places=2) == "1436.05"


def test_signed_always_shows_the_sign():
    assert signed(1.5) == "+1.5"
    assert signed(-1.5) == "-1.5"
    assert signed(0) == "+0.0"


def test_whole_dollars_stay_whole():
    assert money(2000) == "$20"
    assert money(2050) == "$20.50"
    assert money(0) == "$0"
    assert money(123456789) == "$1,234,567.89"


def test_parse_money_accepts_what_people_type():
    assert parse_money("5") == 500
    assert parse_money("$5") == 500
    assert parse_money("12.50") == 1250
    assert parse_money("$1,000") == 100000
    assert parse_money("") is None
    assert parse_money("beer") is None
    assert parse_money("nan") is None  # float("nan") parses; it must not slip through
    assert parse_money("inf") is None


def test_record_hides_a_zero_tie():
    assert record(3, 1) == "3-1"
    assert record(3, 1, 2) == "3-1-2"


def test_ordinal_handles_the_teens():
    assert [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 112)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "112th",
    ]


def test_plural():
    assert plural(1, "vote") == "vote"
    assert plural(0, "vote") == "votes"
    assert plural(2, "vote") == "votes"
    assert plural(2, "loss", "losses") == "losses"


def test_rank_marker_medals_the_podium():
    assert rank_marker(0) != rank_marker(1)
    assert rank_marker(3) == "4."
    assert rank_marker(11) == "12."


# ---------------------------------------------------------------------------
# length
# ---------------------------------------------------------------------------


def test_truncate_adds_an_ellipsis_only_when_it_cuts():
    assert truncate("short", 10) == "short"
    assert truncate("a very long team name indeed", 10) == "a very lo…"
    assert len(truncate("a very long team name indeed", 10)) == 10


def test_fit_returns_everything_when_it_is_short():
    lines = ["a", "b", "c"]
    assert fit(lines) == "a\nb\nc"


def test_fit_drops_whole_lines_rather_than_cutting_words():
    lines = [f"line {i} " + "x" * 100 for i in range(200)]
    body = fit(lines, limit=500)
    assert len(body) <= 500
    assert body.endswith("…")
    # Every retained line is intact.
    for line in body.split("\n")[:-1]:
        assert line.endswith("x" * 100)


def test_fit_stays_under_the_client_send_cap():
    body = fit([f"row {i}" for i in range(5000)])
    assert len(body) <= MAX_BODY


def test_fit_ignores_none_entries():
    assert fit(["a", None, "b"]) == "a\nb"


# ---------------------------------------------------------------------------
# freshness -- say the data is old, never imply it is live
# ---------------------------------------------------------------------------


def test_fresh_data_gets_no_caveat():
    assert freshness(10) == ""
    assert freshness(299) == ""
    assert with_freshness("body", 10) == "body"


def test_old_data_is_labelled():
    note = freshness(600)
    assert "10 min ago" in note
    assert with_freshness("body", 600).startswith("body\n")


def test_stale_data_says_sleeper_is_down():
    note = freshness(600, stale=True)
    assert "not responding" in note
    assert "10 min ago" in note
    # Even brand-new-looking stale data must be flagged.
    assert "not responding" in freshness(5, stale=True)


# ---------------------------------------------------------------------------
# name normalisation
# ---------------------------------------------------------------------------


def test_normalize_strips_case_punctuation_and_emoji():
    assert normalize("Go birds 🦅") == "gobirds"
    assert normalize("Ja'Marr Chase") == "jamarrchase"
    assert normalize("  Jerkin Goff 2 ur mom ") == "jerkingoff2urmom"
    assert normalize("") == ""
    assert normalize(None) == ""


def test_normalize_folds_accents():
    assert normalize("Amón-Ra") == normalize("Amon-Ra")


def test_normalize_handle_makes_phone_formats_comparable():
    for shape in ("+15551234567", "+1 (555) 123-4567", "555-123-4567", "5551234567"):
        assert normalize_handle(shape) == "5551234567"


def test_normalize_handle_leaves_emails_alone():
    assert normalize_handle("Someone@Example.com") == "someone@example.com"


def test_normalize_handle_of_junk_is_not_a_crash():
    assert normalize_handle("") == ""
    assert normalize_handle(None) == ""


def test_strip_mention():
    assert strip_mention("@barry") == "barry"
    assert strip_mention("  barry ") == "barry"
    assert strip_mention("") == ""


# ---------------------------------------------------------------------------
# no markdown
# ---------------------------------------------------------------------------


def test_the_separator_is_not_a_markdown_character():
    # Asterisks, backticks and pipes render literally in iMessage.
    assert not set(DOT) & set("*`_|#[]")
