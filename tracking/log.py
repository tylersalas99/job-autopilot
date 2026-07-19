"""Tracking: SQLite log of every application attempt + duplicate detection."""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    company TEXT, title TEXT, url TEXT, final_url TEXT,
    ats TEXT, track TEXT,
    status TEXT NOT NULL,          -- submitted|held|escalated|failed|rejected_by_rules|duplicate
    reason TEXT,
    resume_path TEXT, cover_letter_path TEXT,
    jd_snapshot_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_company_title ON applications (company, title);
"""


class Tracker:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def duplicate(self, company: str, title: str, url: str, within_days: int = 90):
        """Return the prior SUBMITTED record if this looks like a repeat.

        Only 'submitted' blocks: dry runs, held (never-submitted) attempts,
        and escalations must always be re-runnable.
        """
        cutoff = (datetime.now() - timedelta(days=within_days)).isoformat()
        row = self.conn.execute(
            """SELECT * FROM applications
               WHERE created_at > ? AND status = 'submitted'
                 AND (url = ? OR final_url = ?
                      OR (LOWER(company)=LOWER(?) AND LOWER(title)=LOWER(?)))
               ORDER BY created_at DESC LIMIT 1""",
            (cutoff, url, url, company or "-", title or "-"),
        ).fetchone()
        return dict(row) if row else None

    def record(self, **fields) -> int:
        fields.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.conn.execute(
            f"INSERT INTO applications ({cols}) VALUES ({marks})", list(fields.values())
        )
        self.conn.commit()
        return cur.lastrowid

    def update_status(self, app_id: int, status: str, reason: str = ""):
        self.conn.execute(
            "UPDATE applications SET status=?, reason=? WHERE id=?", (status, reason, app_id)
        )
        self.conn.commit()

    def export_csv(self, out_path: str | Path) -> Path:
        out_path = Path(out_path)
        rows = self.conn.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if rows:
                writer.writerow(rows[0].keys())
                writer.writerows([list(r) for r in rows])
        return out_path

    def summary(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) n FROM applications GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}
