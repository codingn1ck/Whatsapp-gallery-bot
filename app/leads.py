"""
Lead capture.

This is the part that makes the gallery money. The chatbot answering questions
is a convenience; the chatbot capturing "this person asked about Threshold at
9pm on a Saturday and wants a callback" is revenue.

Deliberately SQLite: one file, zero setup, trivially exportable to CSV for the
owner. If the gallery later has a real CRM, swap the backend — the interface
is three functions.
"""

from __future__ import annotations

import csv
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.getenv("LEADS_DB", "data/leads.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_number      TEXT NOT NULL,
    wa_name        TEXT,
    reason         TEXT NOT NULL,        -- callback | visit | price | artwork
    artwork_id     TEXT,
    artwork_title  TEXT,
    message        TEXT,
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'new'   -- new | contacted | closed
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_number ON leads(wa_number);
"""


@dataclass
class Lead:
    wa_number: str
    reason: str
    wa_name: str | None = None
    artwork_id: str | None = None
    artwork_title: str | None = None
    message: str | None = None


@contextmanager
def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


def save_lead(lead: Lead) -> int:
    init_db()
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO leads
               (wa_number, wa_name, reason, artwork_id, artwork_title, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                lead.wa_number,
                lead.wa_name,
                lead.reason,
                lead.artwork_id,
                lead.artwork_title,
                lead.message,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        return cur.lastrowid or 0


def open_leads() -> list[sqlite3.Row]:
    init_db()
    with _conn() as c:
        return c.execute(
            "SELECT * FROM leads WHERE status = 'new' ORDER BY created_at DESC"
        ).fetchall()


def all_leads() -> list[sqlite3.Row]:
    init_db()
    with _conn() as c:
        return c.execute("SELECT * FROM leads ORDER BY created_at DESC").fetchall()


def mark(lead_id: int, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))


def export_csv(path: str = "data/leads_export.csv") -> str:
    """The owner wants a spreadsheet. Give him a spreadsheet."""
    rows = all_leads()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["id", "date", "name", "number", "reason", "artwork", "message", "status"]
        )
        for r in rows:
            writer.writerow([
                r["id"], r["created_at"], r["wa_name"] or "", r["wa_number"],
                r["reason"], r["artwork_title"] or "", r["message"] or "", r["status"],
            ])
    return path
