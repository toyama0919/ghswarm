"""Mutual exclusion (locking) via status labels.

A lock that limits concurrent coding CLIs against the same repository to one. The
GitHub label itself is used as the lock state, so the state is shared across
processes and servers.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone

from .config import LabelConfig
from .github import GitHub, Issue
from .logging_utils import get_logger
from .state import IssueState

log = get_logger("prpilot.labels")


def current_host() -> str:
    """Return the current hostname."""
    return socket.gethostname()


def make_owner(host: str | None = None, pid: int | None = None) -> str:
    """Build the "host:pid" string for a busy lock owner."""
    h = host if host is not None else current_host()
    p = pid if pid is not None else os.getpid()
    return f"{h}:{p}"


def parse_owner(owner: str) -> tuple[str, int | None]:
    """Split "host:pid". Returns (original string, None) if the format is invalid."""
    if not owner or ":" not in owner:
        return owner, None
    host, pid_str = owner.split(":", 1)
    try:
        return host, int(pid_str)
    except ValueError:
        return host, None


def pid_alive(pid: int) -> bool:
    """Whether the process pid is alive. Determined via os.kill(pid, 0)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Existence is confirmed even for another user's process
        return True


def parse_ts(s: str) -> datetime | None:
    """Parse an ISO8601 string into a datetime. Also accepts GitHub's 'Z' suffix."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # busy_since is assumed to be UTC; a missing TZ is treated as UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def is_stale(
    lock: str,
    labels: LabelConfig,
    state: IssueState,
    ttl: int,
    now: datetime,
    host: str,
) -> bool:
    """Determine, without side effects, whether the busy lock is stale (leftover)."""
    if not lock.startswith(labels.busy_prefix):
        return False
    owner_host, owner_pid = parse_owner(state.busy_owner)
    if owner_host == host and owner_pid and not pid_alive(owner_pid):
        return True
    since = parse_ts(state.busy_since)
    if since is None:
        return True
    return (now - since).total_seconds() > ttl


def is_locked(issue: Issue, labels: LabelConfig, agent_names: list[str]) -> str | None:
    """If locked, return the label name representing the lock. None if free."""
    busy = {labels.busy(a) for a in agent_names}
    for l in issue.labels:
        if l in busy or l == labels.blocked:
            return l
    return None


def clear_status_labels(
    gh: GitHub, issue: Issue, labels: LabelConfig, agent_names: list[str]
) -> None:
    """Remove all currently attached status labels.

    The current state on GitHub is treated as authoritative. The issue argument is a
    snapshot taken at the start of process() and does not reflect the busy label added
    by acquire. Relying on it and failing to remove that label leaves busy attached
    while idle is added, after which the Issue is permanently treated as locked and
    skipped (this recurs after every completed task).
    """
    current = set(gh.get_issue(issue.number).labels)
    for l in labels.all_status(agent_names):
        if l in current:
            gh.remove_label(issue.number, l)


def acquire(
    gh: GitHub, issue: Issue, labels: LabelConfig, agent: str, agent_names: list[str]
) -> None:
    """Acquire the lock by setting the busy-{agent} label."""
    clear_status_labels(gh, issue, labels, agent_names)
    gh.add_label(issue.number, labels.busy(agent))
    log.info("Locked Issue #%s: %s", issue.number, labels.busy(agent))


def release(
    gh: GitHub, issue: Issue, labels: LabelConfig, status: str, agent_names: list[str]
) -> None:
    """Release the lock and transition to the given status (idle/blocked/completed)."""
    clear_status_labels(gh, issue, labels, agent_names)
    gh.add_label(issue.number, status)
    log.info("Released Issue #%s: %s", issue.number, status)


def reclaim(gh: GitHub, issue: Issue, labels: LabelConfig, agent_names: list[str]) -> None:
    """Reclaim a stale busy lock and reset it to idle."""
    release(gh, issue, labels, labels.idle, agent_names)
