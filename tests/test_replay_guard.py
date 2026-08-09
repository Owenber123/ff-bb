"""The replay guard: age, not identity, is what survives a restart.

The numbers here are from the real incident on 2026-08-08. The BlueBubbles
server dropped its Messages socket at 23:34, and at 00:57:57 it re-announced
every message since 21:13 as a new-message event. The bot had restarted at
23:28, so its in-memory GUID cache was empty and it answered 22 stale commands
in 40 seconds.
"""

from app import is_stale

WINDOW = 300.0

# 2026-08-08 00:57:57 EDT (04:57:57 UTC), when the server flushed its replay.
REPLAY_AT = 1786165077.0


def ms_before(seconds: float) -> int:
    """Epoch milliseconds for a message sent ``seconds`` before the replay."""
    return int((REPLAY_AT - seconds) * 1000)


def test_live_command_is_not_stale():
    assert not is_stale(ms_before(2), WINDOW, now=REPLAY_AT)


def test_replayed_command_is_stale():
    # "!help" from 22:58, replayed at 00:58 -- the case that caused the flood.
    assert is_stale(ms_before(7191), WINDOW, now=REPLAY_AT)


def test_boundary_is_inclusive_of_the_window():
    assert not is_stale(ms_before(WINDOW), WINDOW, now=REPLAY_AT)
    assert is_stale(ms_before(WINDOW + 1), WINDOW, now=REPLAY_AT)


def test_missing_timestamp_fails_open():
    # Ignoring a real command is worse than answering a stale one.
    assert not is_stale(None, WINDOW, now=REPLAY_AT)
    assert not is_stale(0, WINDOW, now=REPLAY_AT)


def test_zero_window_disables_the_guard():
    assert not is_stale(ms_before(86_400), 0, now=REPLAY_AT)


def test_clock_skew_into_the_future_is_not_stale():
    assert not is_stale(ms_before(-3600), WINDOW, now=REPLAY_AT)
