"""SQLite: which messages were processed (duplicate protection) and their results (audit).

Row status:
    PROCESSING  being processed (if TradePilot stopped mid-way, it is retried on next start)
    REPLYING    the reply is being sent; never retried, so a message is never answered twice
    PASS / FAIL / REVIEW_REQUIRED   finished, reply sent (or reply failed: see `error`)
    IGNORED     no PDF attachments, nothing to do
    FAILED      Outlook error before replying; retried on later polls (max 3 attempts)

One extra row with message_id '__start__' remembers when TradePilot first ran, so only
emails received after that moment are processed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

DONE = ("PASS", "FAIL", "REVIEW_REQUIRED", "IGNORED", "REPLYING", "START")
MAX_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        with self._db() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS processed_messages (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id   TEXT NOT NULL UNIQUE,
                    status       TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    result_json  TEXT,
                    error        TEXT,
                    attempts     INTEGER NOT NULL DEFAULT 0
                )"""
            )

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)  # autocommit
        return closing(db)

    def start_time(self) -> str:
        """When TradePilot first ran (UTC). Recorded on the very first call."""
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, status, processed_at) VALUES ('__start__', 'START', ?)",
                (_now(),),
            )
            return db.execute("SELECT processed_at FROM processed_messages WHERE message_id = '__start__'").fetchone()[0]

    def should_process(self, message_id: str) -> bool:
        with self._db() as db:
            row = db.execute(
                "SELECT status, attempts FROM processed_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        if row is None:
            return True
        status, attempts = row
        return status not in DONE and attempts < MAX_ATTEMPTS

    def save(self, message_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
        """Insert or update the message's row. Each PROCESSING save counts as one attempt."""
        result_json = json.dumps(result) if result is not None else None
        attempt = 1 if status == "PROCESSING" else 0
        with self._db() as db:
            db.execute(
                """INSERT INTO processed_messages (message_id, status, processed_at, result_json, error, attempts)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(message_id) DO UPDATE SET
                       status = excluded.status,
                       processed_at = excluded.processed_at,
                       result_json = COALESCE(excluded.result_json, result_json),
                       error = excluded.error,
                       attempts = attempts + excluded.attempts""",
                (message_id, status, _now(), result_json, error, attempt),
            )

    def get(self, message_id: str) -> dict | None:
        with self._db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM processed_messages WHERE message_id = ?", (message_id,)).fetchone()
        return dict(row) if row else None
