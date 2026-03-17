"""
SQLite-backed sync history for forksync.

Records every sync run and its individual events (per repo).
The database file (sync-history.db) is committed to the repo
so history is visible without any external infrastructure.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from forksync import SyncResult, SyncState

logger = logging.getLogger(__name__)

CREATE_SYNC_RUNS = """
CREATE TABLE IF NOT EXISTS sync_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_at      TEXT NOT NULL,
  duration_s  REAL,
  mode        TEXT NOT NULL,
  total       INTEGER,
  synced      INTEGER,
  skipped     INTEGER,
  conflicts   INTEGER,
  api_calls   INTEGER,
  status      TEXT
);
"""

CREATE_SYNC_EVENTS = """
CREATE TABLE IF NOT EXISTS sync_events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id         INTEGER REFERENCES sync_runs(id),
  repo_name      TEXT NOT NULL,
  action         TEXT NOT NULL,
  commits_merged INTEGER DEFAULT 0,
  issue_url      TEXT,
  error          TEXT,
  duration_s     REAL
);
"""


class SyncHistory:
    """
    SQLite sync history database.

    Records every run and per-repo events.
    Database is at sync-history.db in the repo root.
    """

    def __init__(self, db_path: str = "sync-history.db"):
        self.db_path = str(Path(db_path))
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a SQLite connection with row_factory set."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        with self._get_connection() as conn:
            conn.execute(CREATE_SYNC_RUNS)
            conn.execute(CREATE_SYNC_EVENTS)
            conn.commit()
        logger.debug("SQLite history database initialized at %s", self.db_path)

    def record_run(
        self,
        mode: str,
        results: List[SyncResult],
        duration: float,
        api_calls: int,
    ) -> int:
        """
        Record a complete sync run.

        Args:
            mode: "scheduled", "manual", or "dry_run"
            results: List of SyncResult from the engine.
            duration: Total run duration in seconds.
            api_calls: Total API calls made.

        Returns:
            run_id (INTEGER) for the new run record.
        """
        run_at = datetime.now(timezone.utc).isoformat()
        total = len(results)
        synced = sum(1 for r in results if r.action_taken == "synced")
        skipped = sum(
            1 for r in results if r.action_taken in ("skipped", "dry_run")
        )
        conflicts = sum(1 for r in results if r.action_taken == "conflict_reported")
        errors = sum(1 for r in results if r.action_taken == "error")

        overall_status = "success"
        if errors > 0:
            overall_status = "partial" if (synced + conflicts) > 0 else "failed"

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO sync_runs
                  (run_at, duration_s, mode, total, synced, skipped, conflicts, api_calls, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_at, duration, mode, total, synced, skipped, conflicts, api_calls, overall_status),
            )
            run_id = cursor.lastrowid

            for result in results:
                conn.execute(
                    """
                    INSERT INTO sync_events
                      (run_id, repo_name, action, commits_merged, issue_url, error, duration_s)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        result.repo_name,
                        result.action_taken,
                        result.commits_merged,
                        result.issue_url,
                        result.error,
                        result.duration_seconds,
                    ),
                )
            conn.commit()

        logger.info(
            "Recorded sync run #%d: %d synced, %d skipped, %d conflicts, %d errors",
            run_id,
            synced,
            skipped,
            conflicts,
            errors,
        )
        return run_id

    def get_recent_runs(self, limit: int = 10) -> List[Dict]:
        """
        Get the most recent sync runs.

        Returns a list of dicts with run metadata.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, run_at, duration_s, mode, total, synced, skipped,
                       conflicts, api_calls, status
                FROM sync_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [dict(row) for row in rows]

    def get_run_events(self, run_id: int) -> List[Dict]:
        """
        Get all events for a specific run.

        Returns a list of dicts with per-repo event data.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, repo_name, action, commits_merged,
                       issue_url, error, duration_s
                FROM sync_events
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def get_last_run_id(self) -> Optional[int]:
        """Get the ID of the most recent run."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["id"] if row else None
