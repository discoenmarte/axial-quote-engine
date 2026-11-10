"""Persistence. Plain sqlite3 from the standard library - no ORM, nothing to install.

The data model (see README for the picture):

    conversations  1 --- n  messages              (a chat and what was said in it)
    quotes         1 --- n  quote_line_items      (a quote and its SKUs/quantities)
    quotes         n --- n  conversations         via quote_conversations  <- the key table
    quotes         1 --- n  quote_events          (who changed what, caused by which message)
    ingestions     one row per processed webhook  (idempotency: same message_id => no-op)
    review_items   messages a human must decide on

The many-to-many table is what lets two chats feed one quote (event 2) AND one chat
feed two quotes (event 3) without either side "owning" the other.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from ..catalog import Sku, to_sku
from ..models import LineItem, Message, Quote, QuoteEvent, QuoteEventType, ReviewItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL,
    customer_name   TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    customer_id     TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    timestamp       TEXT NOT NULL,
    text            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    quote_id    TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('draft', 'cancelled')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quote_line_items (
    quote_id TEXT NOT NULL REFERENCES quotes(quote_id),
    sku      TEXT NOT NULL,
    qty      INTEGER NOT NULL CHECK (qty > 0),
    PRIMARY KEY (quote_id, sku)
);
CREATE TABLE IF NOT EXISTS quote_conversations (
    quote_id             TEXT NOT NULL REFERENCES quotes(quote_id),
    conversation_id      TEXT NOT NULL REFERENCES conversations(conversation_id),
    linked_by_message_id TEXT NOT NULL,
    linked_at            TEXT NOT NULL,
    PRIMARY KEY (quote_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS quote_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id   TEXT NOT NULL REFERENCES quotes(quote_id),
    message_id TEXT,
    type       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingestions (
    ingestion_id TEXT PRIMARY KEY,
    received_at  TEXT NOT NULL,
    result       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_items (
    review_id           TEXT PRIMARY KEY,
    message_id          TEXT NOT NULL,
    conversation_id     TEXT NOT NULL,
    customer_id         TEXT NOT NULL,
    text                TEXT NOT NULL,
    reason              TEXT NOT NULL,
    candidate_quote_ids TEXT NOT NULL,
    interpretation      TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('pending', 'resolved', 'dismissed')),
    created_at          TEXT NOT NULL,
    resolved_at         TEXT,
    resolution          TEXT
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All changes caused by one message land together, or not at all."""
        self.db.execute("BEGIN")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # --- idempotency -----------------------------------------------------------------

    def get_ingestion(self, ingestion_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT result FROM ingestions WHERE ingestion_id = ?", (ingestion_id,)).fetchone()
        return json.loads(row["result"]) if row else None

    def save_ingestion(self, ingestion_id: str, result: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO ingestions VALUES (?, ?, ?)", (ingestion_id, now_iso(), json.dumps(result)))

    def update_ingestion(self, ingestion_id: str, result: dict[str, Any]) -> None:
        self.db.execute("UPDATE ingestions SET result = ? WHERE ingestion_id = ?", (json.dumps(result), ingestion_id))

    # --- conversations & messages ------------------------------------------------------

    def upsert_conversation(self, conversation_id: str, customer_id: str, customer_name: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET customer_name = COALESCE(excluded.customer_name, customer_name)",
            (conversation_id, customer_id, customer_name, now_iso()),
        )

    def save_message(self, m: Message) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?)",
            (m.message_id, m.conversation_id, m.customer_id, m.direction, m.timestamp, m.text),
        )

    def get_message(self, message_id: str) -> Message | None:
        row = self.db.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
        return Message(**dict(row)) if row else None

    def recent_messages(self, conversation_id: str, limit: int = 8) -> list[Message]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY rowid DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [Message(**dict(r)) for r in reversed(rows)]

    # --- quotes --------------------------------------------------------------------------

    def next_quote_id(self) -> str:
        self.db.execute(
            "INSERT INTO counters VALUES ('quote', 1) ON CONFLICT(name) DO UPDATE SET value = value + 1"
        )
        n = self.db.execute("SELECT value FROM counters WHERE name = 'quote'").fetchone()["value"]
        return f"Q-{n:03d}"

    def create_quote(self, customer_id: str, items: list[LineItem]) -> Quote:
        quote_id = self.next_quote_id()
        ts = now_iso()
        self.db.execute("INSERT INTO quotes VALUES (?, ?, 'draft', ?, ?)", (quote_id, customer_id, ts, ts))
        self.add_line_items(quote_id, items)
        return self.get_quote(quote_id)  # type: ignore[return-value]

    def add_line_items(self, quote_id: str, items: list[LineItem]) -> None:
        for item in items:
            sku: Sku = to_sku(item.sku)  # the last line of defence: only catalog SKUs reach the DB
            self.db.execute(
                "INSERT INTO quote_line_items VALUES (?, ?, ?) "
                "ON CONFLICT(quote_id, sku) DO UPDATE SET qty = qty + excluded.qty",
                (quote_id, sku, item.qty),
            )
        self._touch(quote_id)

    def set_line_qty(self, quote_id: str, sku: str, qty: int) -> None:
        self.db.execute("UPDATE quote_line_items SET qty = ? WHERE quote_id = ? AND sku = ?", (qty, quote_id, to_sku(sku)))
        self._touch(quote_id)

    def set_status(self, quote_id: str, status: str) -> None:
        self.db.execute("UPDATE quotes SET status = ? WHERE quote_id = ?", (status, quote_id))
        self._touch(quote_id)

    def link_conversation(self, quote_id: str, conversation_id: str, message_id: str) -> bool:
        """Returns True if the link is new, False if it already existed."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO quote_conversations VALUES (?, ?, ?, ?)",
            (quote_id, conversation_id, message_id, now_iso()),
        )
        return cur.rowcount == 1

    def get_quote(self, quote_id: str) -> Quote | None:
        row = self.db.execute("SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)).fetchone()
        return self._hydrate(row) if row else None

    def list_quotes(self, customer_id: str | None = None, status: str | None = None) -> list[Quote]:
        sql, args = "SELECT * FROM quotes WHERE 1=1", []
        if customer_id:
            sql, args = sql + " AND customer_id = ?", args + [customer_id]
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        return [self._hydrate(r) for r in self.db.execute(sql + " ORDER BY rowid", args).fetchall()]

    def _hydrate(self, row: sqlite3.Row) -> Quote:
        lines = self.db.execute(
            "SELECT sku, qty FROM quote_line_items WHERE quote_id = ? ORDER BY rowid", (row["quote_id"],)
        ).fetchall()
        convs = self.db.execute(
            "SELECT conversation_id FROM quote_conversations WHERE quote_id = ? ORDER BY rowid", (row["quote_id"],)
        ).fetchall()
        return Quote(
            quote_id=row["quote_id"], customer_id=row["customer_id"], status=row["status"],
            conversations=[c["conversation_id"] for c in convs],
            line_items=[LineItem(sku=to_sku(l["sku"]), qty=l["qty"]) for l in lines],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def _touch(self, quote_id: str) -> None:
        self.db.execute("UPDATE quotes SET updated_at = ? WHERE quote_id = ?", (now_iso(), quote_id))

    # --- history -----------------------------------------------------------------------

    def add_event(self, quote_id: str, message_id: str | None, type: QuoteEventType, detail: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO quote_events (quote_id, message_id, type, detail, at) VALUES (?, ?, ?, ?, ?)",
            (quote_id, message_id, type, json.dumps(detail), now_iso()),
        )

    def get_events(self, quote_id: str) -> list[QuoteEvent]:
        rows = self.db.execute("SELECT * FROM quote_events WHERE quote_id = ? ORDER BY event_id", (quote_id,)).fetchall()
        return [QuoteEvent(**{**dict(r), "detail": json.loads(r["detail"])}) for r in rows]

    # --- human review ----------------------------------------------------------------

    def create_review(self, m: Message, reason: str, candidates: list[str], interpretation: dict[str, Any]) -> ReviewItem:
        review_id = f"R-{uuid.uuid4().hex[:8]}"
        self.db.execute(
            "INSERT INTO review_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)",
            (review_id, m.message_id, m.conversation_id, m.customer_id, m.text, reason,
             json.dumps(candidates), json.dumps(interpretation), now_iso()),
        )
        return self.get_review(review_id)  # type: ignore[return-value]

    def get_review(self, review_id: str) -> ReviewItem | None:
        row = self.db.execute("SELECT * FROM review_items WHERE review_id = ?", (review_id,)).fetchone()
        return self._review(row) if row else None

    def get_review_interpretation(self, review_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT interpretation FROM review_items WHERE review_id = ?", (review_id,)).fetchone()
        return json.loads(row["interpretation"])

    def list_reviews(self, status: str | None = "pending") -> list[ReviewItem]:
        sql, args = "SELECT * FROM review_items", []
        if status:
            sql, args = sql + " WHERE status = ?", [status]
        return [self._review(r) for r in self.db.execute(sql + " ORDER BY rowid", args).fetchall()]

    def close_review(self, review_id: str, status: str, resolution: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE review_items SET status = ?, resolved_at = ?, resolution = ? WHERE review_id = ?",
            (status, now_iso(), json.dumps(resolution), review_id),
        )

    def _review(self, row: sqlite3.Row) -> ReviewItem:
        d = dict(row)
        d.pop("interpretation")
        d["candidate_quote_ids"] = json.loads(d["candidate_quote_ids"])
        d["resolution"] = json.loads(d["resolution"]) if d["resolution"] else None
        return ReviewItem(**d)
