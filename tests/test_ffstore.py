"""Persistence. The point of every test here is "survives a restart"."""

from __future__ import annotations

from ffstore import Store


def _restart(tmp_path) -> Store:
    """A fresh Store over the same file, i.e. what a systemd restart looks like."""
    return Store(tmp_path / "bot.db")


# ---------------------------------------------------------------------------
# nicknames
# ---------------------------------------------------------------------------


def test_nicknames_survive_a_restart(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.set_nickname("u2", "Autodraft", set_by="owen")
    assert _restart(tmp_path).nicknames() == {"u2": "Autodraft"}


def test_setting_a_nickname_twice_replaces_it(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.set_nickname("u2", "Autodraft")
    store.set_nickname("u2", "Waiver Wire Larry")
    assert store.nicknames() == {"u2": "Waiver Wire Larry"}


def test_clearing_a_nickname(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.set_nickname("u2", "Autodraft")
    assert store.clear_nickname("u2") is True
    assert store.clear_nickname("u2") is False  # already gone
    assert store.nicknames() == {}


def test_nicknames_are_keyed_by_user_id_not_display_name(tmp_path):
    # So a nickname follows someone who renames their Sleeper account.
    store = Store(tmp_path / "bot.db")
    store.set_nickname("u2", "Autodraft")
    assert "u2" in store.nicknames()


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------


def test_links_survive_a_restart_and_normalise_the_handle(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.set_link("+15551234567", "u1")
    assert _restart(tmp_path).links() == {"+15551234567": "u1"}


def test_relinking_replaces(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.set_link("h", "u1")
    store.set_link("h", "u2")
    assert store.links() == {"h": "u2"}


# ---------------------------------------------------------------------------
# fines
# ---------------------------------------------------------------------------


def test_fines_accumulate_per_person(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.add_fine("u1", 500, "autodraft")
    store.add_fine("u1", 1000, "no show")
    store.add_fine("u2", 250, "trash talk")
    assert store.fine_totals() == {"u1": 1500, "u2": 250}
    assert store.fine_count() == 3


def test_fines_survive_a_restart(tmp_path):
    Store(tmp_path / "bot.db").add_fine("u1", 500, "autodraft")
    assert _restart(tmp_path).fine_totals() == {"u1": 500}


def test_voiding_a_fine_removes_it_from_the_total(tmp_path):
    store = Store(tmp_path / "bot.db")
    fine_id = store.add_fine("u1", 500, "mistake")
    store.add_fine("u1", 100, "real")
    assert store.void_fine(fine_id) is True
    assert store.void_fine(fine_id) is False  # already void
    assert store.fine_totals() == {"u1": 100}
    assert store.fine_count() == 1


def test_recent_fines_are_newest_first(tmp_path):
    store = Store(tmp_path / "bot.db")
    for i in range(5):
        store.add_fine("u1", 100, f"reason {i}")
    recent = store.recent_fines(limit=3)
    assert [f.reason for f in recent] == ["reason 4", "reason 3", "reason 2"]


def test_no_fines_is_an_empty_dict_not_an_error(tmp_path):
    assert Store(tmp_path / "bot.db").fine_totals() == {}


# ---------------------------------------------------------------------------
# key/value
# ---------------------------------------------------------------------------


def test_meta_round_trips_and_survives(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.set_meta("punishment", "Waffle House, 24 hours")
    store.set_meta("buyin_cents", "5000")
    restarted = _restart(tmp_path)
    assert restarted.get_meta("punishment") == "Waffle House, 24 hours"
    assert restarted.get_meta("buyin_cents") == "5000"
    assert restarted.get_meta("missing", "fallback") == "fallback"


def test_schema_version_is_recorded(tmp_path):
    assert Store(tmp_path / "bot.db").get_meta("schema_version") == "1"


def test_reopening_does_not_reset_the_schema_version_row(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.set_meta("schema_version", "1")
    store.set_meta("punishment", "x")
    assert _restart(tmp_path).get_meta("punishment") == "x"


# ---------------------------------------------------------------------------
# polls
# ---------------------------------------------------------------------------


def test_a_poll_round_trips(tmp_path):
    store = Store(tmp_path / "bot.db")
    poll_id = store.create_poll("chat1", "Who is worst?", ["barry", "owen"], created_by="owen")
    poll = store.open_poll("chat1")
    assert poll.id == poll_id
    assert poll.question == "Who is worst?"
    assert poll.options == ["barry", "owen"]
    assert poll.closed is False


def test_opening_a_poll_closes_the_previous_one(tmp_path):
    # Two live polls in one chat and "!vote 2" is ambiguous to everybody.
    store = Store(tmp_path / "bot.db")
    first = store.create_poll("chat1", "one", ["a", "b"])
    second = store.create_poll("chat1", "two", ["a", "b"])
    assert store.open_poll("chat1").id == second
    assert first != second


def test_polls_are_scoped_per_chat(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.create_poll("chat1", "one", ["a"])
    store.create_poll("chat2", "two", ["b"])
    assert store.open_poll("chat1").question == "one"
    assert store.open_poll("chat2").question == "two"


def test_one_vote_per_person_and_revoting_replaces(tmp_path):
    store = Store(tmp_path / "bot.db")
    poll_id = store.create_poll("chat1", "q", ["a", "b"])
    store.vote(poll_id, "5551234567", 1)
    store.vote(poll_id, "5551234567", 2)  # changed their mind
    store.vote(poll_id, "5559999999", 2)
    assert store.poll_tally(poll_id) == {2: 2}


def test_votes_survive_a_restart(tmp_path):
    store = Store(tmp_path / "bot.db")
    poll_id = store.create_poll("chat1", "q", ["a", "b"])
    store.vote(poll_id, "someone", 1)
    assert _restart(tmp_path).poll_tally(poll_id) == {1: 1}


def test_closing_a_poll(tmp_path):
    store = Store(tmp_path / "bot.db")
    poll_id = store.create_poll("chat1", "q", ["a"])
    assert store.close_poll(poll_id) is True
    assert store.close_poll(poll_id) is False
    assert store.open_poll("chat1") is None
    assert store.latest_poll("chat1").id == poll_id  # still readable


def test_no_poll_is_none_not_an_error(tmp_path):
    store = Store(tmp_path / "bot.db")
    assert store.open_poll("nobody") is None
    assert store.latest_poll("nobody") is None
    assert store.poll_tally(999) == {}
