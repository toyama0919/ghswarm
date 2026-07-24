"""Local structured event log (SQLite)."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .logging_utils import get_logger

if TYPE_CHECKING:
    from .orchestrator import StepResult

log = get_logger("prpilot.events")

_DEFAULT_EVENT_DB = "~/.prpilot/events.db"
_DETAIL_MAX = 500


def resolve_event_db_path(raw: str) -> str:
    """Resolve the event_db config value to an absolute path. An empty string stays disabled."""
    if raw == "":
        return ""
    path = raw or _DEFAULT_EVENT_DB
    return os.path.expanduser(path)


def _truncate_detail(detail: str) -> str:
    if len(detail) <= _DETAIL_MAX:
        return detail
    return detail[:_DETAIL_MAX] + "…"


class EventLog:
    """Observability log that records each step result to SQLite."""

    def __init__(self, db_path: str | None) -> None:
        self._db_path = db_path or ""
        self._conn: sqlite3.Connection | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._db_path)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema(conn)
        self._conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                repo TEXT NOT NULL,
                issue INTEGER NOT NULL,
                action TEXT NOT NULL,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_events_repo_issue ON events(repo, issue);
            CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
            """
        )
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            conn.execute("PRAGMA user_version = 1")

    def record(self, repo: str, result: StepResult, *, now: datetime) -> None:
        if not self.enabled:
            return
        if result.action == "skipped":
            return
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO events(ts, repo, issue, action, detail) VALUES(?,?,?,?,?)",
                (
                    now.isoformat(),
                    repo,
                    result.issue_number,
                    result.action,
                    _truncate_detail(result.detail),
                ),
            )
            conn.commit()
        except Exception as e:
            log.warning("Failed to record event (%s): %s", self._db_path, e)

    def read(
        self,
        *,
        repo: str | None = None,
        issue: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        if not self.enabled:
            return []
        if not Path(self._db_path).is_file():
            return []
        try:
            conn = self._connect()
            clauses: list[str] = []
            params: list[object] = []
            if repo is not None:
                clauses.append("repo = ?")
                params.append(repo)
            if issue is not None:
                clauses.append("issue = ?")
                params.append(issue)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            sql = f"SELECT ts, repo, issue, action, detail FROM events{where} ORDER BY ts DESC"
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "ts": r[0],
                    "repo": r[1],
                    "issue": r[2],
                    "action": r[3],
                    "detail": r[4] or "",
                }
                for r in rows
            ]
        except Exception as e:
            log.warning("Failed to read events (%s): %s", self._db_path, e)
            return []
