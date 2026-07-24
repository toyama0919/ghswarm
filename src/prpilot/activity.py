"""Activity file I/O while the daemon is running."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import resolve_activity_dir
from .logging_utils import get_logger

log = get_logger("prpilot.activity")

__all__ = [
    "ActivityEntry",
    "clear_activity",
    "clear_activity_dir",
    "read_activities",
    "resolve_activity_dir",
    "sanitize_repo_name",
    "write_activity",
]


@dataclass(frozen=True)
class ActivityEntry:
    repo: str
    issue: int
    phase: str
    since: str
    pid: int


def sanitize_repo_name(repo: str) -> str:
    """Sanitize a repo name for use in a filename."""
    return repo.replace("/", "_")


def clear_activity_dir(activity_dir: str) -> None:
    """Delete all files in the activity directory."""
    if not activity_dir:
        return
    path = Path(activity_dir)
    if not path.is_dir():
        return
    for child in path.iterdir():
        try:
            if child.is_file():
                child.unlink(missing_ok=True)
        except OSError:
            log.warning("Failed to delete activity file: %s", child, exc_info=True)


def write_activity(
    activity_dir: str,
    repo: str,
    issue: int,
    phase: str,
    pid: int,
) -> None:
    """Atomically write the activity file for a repo."""
    if not activity_dir:
        return
    try:
        path = Path(activity_dir)
        path.mkdir(parents=True, exist_ok=True)
        filename = f"{sanitize_repo_name(repo)}.json"
        payload = {
            "repo": repo,
            "issue": issue,
            "phase": phase,
            "since": datetime.now(timezone.utc).isoformat(),
            "pid": pid,
        }
        fd, tmp_path = tempfile.mkstemp(dir=path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp_path, path / filename)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        log.warning("Failed to write activity file: %s", exc)


def clear_activity(activity_dir: str, repo: str) -> None:
    """Delete the activity file for a repo."""
    if not activity_dir:
        return
    try:
        path = Path(activity_dir) / f"{sanitize_repo_name(repo)}.json"
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Failed to delete activity file: %s", exc)


def read_activities(
    activity_dir: str,
    *,
    daemon_pid: int | None = None,
    is_alive_fn: Callable[[int], bool] | None = None,
) -> list[ActivityEntry]:
    """Read entries from the activity directory."""
    if not activity_dir:
        return []
    path = Path(activity_dir)
    if not path.is_dir():
        return []

    alive = is_alive_fn or (lambda _pid: True)
    entries: list[ActivityEntry] = []
    for child in sorted(path.glob("*.json")):
        try:
            data = json.loads(child.read_text(encoding="utf-8"))
            entry = ActivityEntry(
                repo=str(data["repo"]),
                issue=int(data["issue"]),
                phase=str(data["phase"]),
                since=str(data.get("since", "")),
                pid=int(data["pid"]),
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if daemon_pid is not None and entry.pid != daemon_pid:
            continue
        if daemon_pid is not None and not alive(entry.pid):
            continue
        entries.append(entry)
    return entries
