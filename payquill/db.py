"""SQLite ledger: one file, no server. All money is stored in integer cents."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    email   TEXT NOT NULL,
    contact TEXT
);

-- Past, settled invoices. Used only to train the late-payment model.
CREATE TABLE IF NOT EXISTS payment_history (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    amount_cents INTEGER NOT NULL,
    due_date     TEXT NOT NULL,
    paid_date    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id            INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    number        TEXT NOT NULL,
    description   TEXT NOT NULL,
    milestone     TEXT,
    amount_cents  INTEGER NOT NULL,
    issue_date    TEXT NOT NULL,
    due_date      TEXT NOT NULL,
    status        TEXT NOT NULL,            -- draft | sent | paid | cancelled | error
    paypal_id     TEXT,
    payer_url     TEXT,
    paid_date     TEXT,
    risk_score    REAL,
    risk_band     TEXT,
    risk_reason   TEXT,
    reminders     TEXT NOT NULL DEFAULT '[]',  -- JSON list of {date, sent}
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bills (
    id               INTEGER PRIMARY KEY,
    supplier_id      INTEGER NOT NULL REFERENCES suppliers(id),
    number           TEXT,
    amount_cents     INTEGER NOT NULL,
    bill_date        TEXT NOT NULL,
    due_date         TEXT NOT NULL,
    discount_pct     REAL NOT NULL DEFAULT 0,
    discount_days    INTEGER NOT NULL DEFAULT 0,
    net_days         INTEGER NOT NULL DEFAULT 30,
    status           TEXT NOT NULL,          -- review | scheduled | processing | paid | failed
    recommendation   TEXT,                   -- JSON from paydate.recommend
    pay_date         TEXT,
    pay_amount_cents INTEGER,
    payout_batch_id  TEXT,
    source_file      TEXT,
    extraction       TEXT,                   -- JSON of what the AI read, with confidence
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL
);

-- Only used when PAYPAL_MODE=mock (no sandbox credentials).
CREATE TABLE IF NOT EXISTS mock_paypal (
    id   TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    body TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Add columns introduced after the first release to existing databases.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
            if "last_reminder" not in cols:
                conn.execute("ALTER TABLE invoices ADD COLUMN last_reminder TEXT")
            if "reminder_count" not in cols:
                conn.execute("ALTER TABLE invoices ADD COLUMN reminder_count INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- generic helpers -------------------------------------------------
    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.connect() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid

    # ---- settings ---------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # ---- demo clock -------------------------------------------------------
    # The demo can fast-forward days so judges can watch a scheduled payout
    # release without waiting a week. Real time is never changed.
    def today(self) -> date:
        return date.today() + timedelta(days=int(self.get_setting("clock_offset_days", 0)))

    def advance_clock(self, days: int) -> date:
        offset = int(self.get_setting("clock_offset_days", 0)) + days
        self.set_setting("clock_offset_days", offset)
        return self.today()

    # ---- cash balance -----------------------------------------------------
    def balance(self) -> int:
        return int(self.get_setting("cash_balance_cents", 0))

    def adjust_balance(self, delta_cents: int) -> int:
        value = self.balance() + delta_cents
        self.set_setting("cash_balance_cents", value)
        return value

    # ---- activity log -----------------------------------------------------
    def log(self, kind: str, message: str) -> None:
        self.execute(
            "INSERT INTO events(ts, kind, message) VALUES(?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), kind, message),
        )

    def reset(self) -> None:
        with self.connect() as conn:
            for table in (
                "events", "bills", "suppliers", "invoices",
                "payment_history", "customers", "settings", "mock_paypal",
            ):
                conn.execute(f"DELETE FROM {table}")


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def parse_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
