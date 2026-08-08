"""SQLite persistence for the things the bot invents rather than reads.

Nicknames, fines, polls and the punishment text are all bot state -- Sleeper
knows nothing about them, so they have to survive a restart.

Deliberately small. One file, five tables, no ORM, no migrations framework
beyond ``CREATE TABLE IF NOT EXISTS`` plus a schema_version row. Connections
are per-call and short-lived: sqlite3 objects are not shareable across threads
and the dispatcher runs a worker pool.

The file lives under the data directory, which is inside the systemd unit's
``ReadWritePaths=``. Nothing else in the container is writable.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from logging_setup import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Nicknames are keyed by Sleeper user_id, not by display name, so they follow
-- somebody who renames their Sleeper account mid-season.
CREATE TABLE IF NOT EXISTS nicknames (
    user_id  TEXT PRIMARY KEY,
    nickname TEXT NOT NULL,
    set_by   TEXT NOT NULL DEFAULT '',
    set_at   INTEGER NOT NULL
);

-- Chat handle (phone/email) -> Sleeper user_id. Seeded from SLEEPER_USER_MAP
-- in the environment; !link writes overrides here.
CREATE TABLE IF NOT EXISTS links (
    handle   TEXT PRIMARY KEY,
    user_id  TEXT NOT NULL,
    linked_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fines (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  TEXT NOT NULL,
    cents    INTEGER NOT NULL,
    reason   TEXT NOT NULL DEFAULT '',
    added_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    voided   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fines_user ON fines(user_id, voided);

CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_guid  TEXT NOT NULL,
    question   TEXT NOT NULL,
    options    TEXT NOT NULL,           -- JSON array of strings
    created_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_polls_chat ON polls(chat_guid, closed);

-- What the scheduler has already sent. The primary key is the whole point:
-- it is what stops a restart reposting Monday's recap, and what enforces
-- "at most once per post type per week".
CREATE TABLE IF NOT EXISTS posted (
    season    TEXT NOT NULL,
    week      INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    posted_at INTEGER NOT NULL,
    PRIMARY KEY (season, week, kind)
);

-- One vote per person per poll; re-voting overwrites.
CREATE TABLE IF NOT EXISTS votes (
    poll_id INTEGER NOT NULL,
    voter   TEXT NOT NULL,
    choice  INTEGER NOT NULL,
    voted_at INTEGER NOT NULL,
    PRIMARY KEY (poll_id, voter)
);
"""


@dataclass
class Fine:
    id: int
    user_id: str
    cents: int
    reason: str
    added_by: str
    created_at: int


@dataclass
class Poll:
    id: int
    chat_guid: str
    question: str
    options: list[str]
    created_by: str
    created_at: int
    closed: bool


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # Serialised through one lock. The write volume here is a nickname
        # every few weeks; contention is not a consideration, correctness is.
        with self._lock:
            conn = sqlite3.connect(str(self.path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )

    # -- key/value -----------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- nicknames -----------------------------------------------------------

    def set_nickname(self, user_id: str, nickname: str, set_by: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO nicknames(user_id, nickname, set_by, set_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "nickname = excluded.nickname, set_by = excluded.set_by, set_at = excluded.set_at",
                (user_id, nickname, set_by, int(time.time())),
            )

    def clear_nickname(self, user_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM nicknames WHERE user_id = ?", (user_id,))
            return cur.rowcount > 0

    def nicknames(self) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT user_id, nickname FROM nicknames").fetchall()
        return {r["user_id"]: r["nickname"] for r in rows}

    # -- links ---------------------------------------------------------------

    def set_link(self, handle: str, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO links(handle, user_id, linked_at) VALUES(?, ?, ?) "
                "ON CONFLICT(handle) DO UPDATE SET "
                "user_id = excluded.user_id, linked_at = excluded.linked_at",
                (handle.strip().lower(), user_id, int(time.time())),
            )

    def links(self) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT handle, user_id FROM links").fetchall()
        return {r["handle"]: r["user_id"] for r in rows}

    # -- fines ---------------------------------------------------------------

    def add_fine(self, user_id: str, cents: int, reason: str, added_by: str = "") -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO fines(user_id, cents, reason, added_by, created_at) VALUES(?, ?, ?, ?, ?)",
                (user_id, int(cents), reason, added_by, int(time.time())),
            )
            return int(cur.lastrowid or 0)

    def void_fine(self, fine_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE fines SET voided = 1 WHERE id = ? AND voided = 0", (fine_id,))
            return cur.rowcount > 0

    def fine_totals(self) -> dict[str, int]:
        """user_id -> total cents owed."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, SUM(cents) AS total FROM fines WHERE voided = 0 GROUP BY user_id"
            ).fetchall()
        return {r["user_id"]: int(r["total"] or 0) for r in rows}

    def recent_fines(self, limit: int = 5) -> list[Fine]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM fines WHERE voided = 0 ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Fine(
                id=r["id"],
                user_id=r["user_id"],
                cents=r["cents"],
                reason=r["reason"],
                added_by=r["added_by"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def fine_count(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM fines WHERE voided = 0").fetchone()
        return int(row["n"] or 0)

    # -- scheduled posts -----------------------------------------------------

    def already_posted(self, season: str, week: int, kind: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM posted WHERE season = ? AND week = ? AND kind = ?",
                (str(season), int(week), kind),
            ).fetchone()
        return row is not None

    def mark_posted(self, season: str, week: int, kind: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO posted(season, week, kind, posted_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(season, week, kind) DO NOTHING",
                (str(season), int(week), kind, int(time.time())),
            )

    def clear_posted(self, season: str, week: int, kind: str) -> bool:
        """Let a post fire again. Used by ``bot.py post --force``."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM posted WHERE season = ? AND week = ? AND kind = ?",
                (str(season), int(week), kind),
            )
            return cur.rowcount > 0

    def recent_posts(self, limit: int = 10) -> list[tuple[str, int, str, int]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT season, week, kind, posted_at FROM posted "
                "ORDER BY posted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(r["season"], r["week"], r["kind"], r["posted_at"]) for r in rows]

    # -- polls ---------------------------------------------------------------

    def create_poll(self, chat_guid: str, question: str, options: list[str], created_by: str = "") -> int:
        with self._conn() as conn:
            # Only one poll open per chat: two live polls and "!vote 2" is
            # ambiguous to everyone including us.
            conn.execute("UPDATE polls SET closed = 1 WHERE chat_guid = ? AND closed = 0", (chat_guid,))
            cur = conn.execute(
                "INSERT INTO polls(chat_guid, question, options, created_by, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (chat_guid, question, json.dumps(options), created_by, int(time.time())),
            )
            return int(cur.lastrowid or 0)

    def open_poll(self, chat_guid: str) -> Poll | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM polls WHERE chat_guid = ? AND closed = 0 ORDER BY id DESC LIMIT 1",
                (chat_guid,),
            ).fetchone()
        return _poll_from_row(row)

    def latest_poll(self, chat_guid: str) -> Poll | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM polls WHERE chat_guid = ? ORDER BY id DESC LIMIT 1", (chat_guid,)
            ).fetchone()
        return _poll_from_row(row)

    def close_poll(self, poll_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE polls SET closed = 1 WHERE id = ? AND closed = 0", (poll_id,))
            return cur.rowcount > 0

    def vote(self, poll_id: int, voter: str, choice: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO votes(poll_id, voter, choice, voted_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(poll_id, voter) DO UPDATE SET "
                "choice = excluded.choice, voted_at = excluded.voted_at",
                (poll_id, voter.strip().lower(), int(choice), int(time.time())),
            )

    def poll_tally(self, poll_id: int) -> dict[int, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT choice, COUNT(*) AS n FROM votes WHERE poll_id = ? GROUP BY choice",
                (poll_id,),
            ).fetchall()
        return {int(r["choice"]): int(r["n"]) for r in rows}


def _poll_from_row(row: sqlite3.Row | None) -> Poll | None:
    if row is None:
        return None
    try:
        options = json.loads(row["options"])
    except ValueError:
        options = []
    return Poll(
        id=row["id"],
        chat_guid=row["chat_guid"],
        question=row["question"],
        options=[str(o) for o in options],
        created_by=row["created_by"],
        created_at=row["created_at"],
        closed=bool(row["closed"]),
    )


# ---------------------------------------------------------------------------
# singleton
# ---------------------------------------------------------------------------

_STORE: Store | None = None
_STORE_GUARD = threading.Lock()


def get_store(path: str | Path | None = None) -> Store:
    global _STORE
    with _STORE_GUARD:
        if _STORE is None:
            if path is None:
                path = Path(__file__).resolve().parent / "data" / "bot.db"
            _STORE = Store(path)
        return _STORE


def set_store(store: Store | None) -> None:
    """Test seam."""
    global _STORE
    with _STORE_GUARD:
        _STORE = store
