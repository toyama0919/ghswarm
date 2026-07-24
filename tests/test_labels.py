"""Tests for acquiring/releasing the lock via the status label (gh is not launched).

The label itself is the lock, so a leaked release directly causes the Issue to be skipped forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ghswarm import labels as lbl
from ghswarm import state as st
from ghswarm.config import LabelConfig
from ghswarm.github import Issue

AGENTS = ["claude", "cursor"]
HOST = "testhost"
NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
TTL = 3600
CFG = LabelConfig()


class FakeGitHub:
    """Stub that holds the current labels on the GitHub side."""

    def __init__(self, labels: list[str]):
        self.current = list(labels)

    def get_issue(self, number: int) -> Issue:
        return Issue(number=number, title="t", body="b", labels=list(self.current))

    def add_label(self, number: int, label: str) -> None:
        if label not in self.current:
            self.current.append(label)

    def remove_label(self, number: int, label: str) -> None:
        if label in self.current:
            self.current.remove(label)

    def ensure_label(self, label: str) -> None:
        pass


def _stale_snapshot() -> Issue:
    """Issue snapshot fetched at the start of process() (idle state)."""
    return Issue(number=8, title="t", body="b", labels=["pm-agent", "status: idle"])


def test_release_removes_busy_label_added_after_the_snapshot():
    # Regression guard: even after acquire adds busy, the passed issue.labels stays stale.
    # Removing based on that snapshot would leave busy behind, causing the next poll to
    # skip it forever as "locked" (this reproduced once per completed task).
    gh = FakeGitHub(["pm-agent", "status: idle"])
    issue = _stale_snapshot()
    cfg = LabelConfig()

    lbl.acquire(gh, issue, cfg, "claude", AGENTS)
    assert "status: busy-claude" in gh.current

    lbl.release(gh, issue, cfg, cfg.idle, AGENTS)

    assert "status: busy-claude" not in gh.current
    assert gh.current == ["pm-agent", "status: idle"]


def test_released_issue_is_not_locked():
    gh = FakeGitHub(["pm-agent", "status: idle"])
    issue = _stale_snapshot()
    cfg = LabelConfig()

    lbl.acquire(gh, issue, cfg, "claude", AGENTS)
    lbl.release(gh, issue, cfg, cfg.idle, AGENTS)

    assert lbl.is_locked(gh.get_issue(8), cfg, AGENTS) is None


def test_acquire_locks_and_replaces_previous_status():
    gh = FakeGitHub(["pm-agent", "status: idle"])
    cfg = LabelConfig()

    lbl.acquire(gh, _stale_snapshot(), cfg, "claude", AGENTS)

    assert gh.current == ["pm-agent", "status: busy-claude"]
    assert lbl.is_locked(gh.get_issue(8), cfg, AGENTS) == "status: busy-claude"


def test_release_to_blocked_clears_busy():
    gh = FakeGitHub(["pm-agent", "status: idle"])
    issue = _stale_snapshot()
    cfg = LabelConfig()

    lbl.acquire(gh, issue, cfg, "claude", AGENTS)
    lbl.release(gh, issue, cfg, cfg.blocked, AGENTS)

    assert gh.current == ["pm-agent", "status: blocked"]
    # blocked is itself a lock, so is_locked returns blocked
    assert lbl.is_locked(gh.get_issue(8), cfg, AGENTS) == "status: blocked"


# -- is_stale (pure function) -----------------------------------------------


def _state(*, owner: str = "", since: str = "") -> st.IssueState:
    return st.IssueState(branch_name="issue-1", busy_owner=owner, busy_since=since)


def test_is_stale_blocked_is_not_stale():
    assert lbl.is_stale("status: blocked", CFG, _state(), TTL, NOW, HOST) is False


def test_is_stale_same_host_dead_pid_is_stale(monkeypatch):
    monkeypatch.setattr(lbl, "pid_alive", lambda pid: False)
    state = _state(owner=f"{HOST}:99999", since=NOW.isoformat())
    assert lbl.is_stale("status: busy-implement", CFG, state, TTL, NOW, HOST) is True


def test_is_stale_same_host_alive_pid_within_ttl_is_not_stale(monkeypatch):
    monkeypatch.setattr(lbl, "pid_alive", lambda pid: True)
    state = _state(owner=f"{HOST}:42", since=(NOW - timedelta(seconds=30)).isoformat())
    assert lbl.is_stale("status: busy-implement", CFG, state, TTL, NOW, HOST) is False


def test_is_stale_other_host_within_ttl_is_not_stale(monkeypatch):
    monkeypatch.setattr(lbl, "pid_alive", lambda pid: pytest.fail("pid check skipped"))
    state = _state(owner="otherhost:1", since=(NOW - timedelta(seconds=30)).isoformat())
    assert lbl.is_stale("status: busy-implement", CFG, state, TTL, NOW, HOST) is False


def test_is_stale_other_host_ttl_exceeded_is_stale(monkeypatch):
    monkeypatch.setattr(lbl, "pid_alive", lambda pid: pytest.fail("pid check skipped"))
    state = _state(owner="otherhost:1", since=(NOW - timedelta(seconds=TTL + 1)).isoformat())
    assert lbl.is_stale("status: busy-implement", CFG, state, TTL, NOW, HOST) is True


def test_is_stale_missing_lease_is_stale():
    state = _state()
    assert lbl.is_stale("status: busy-review", CFG, state, TTL, NOW, HOST) is True


def test_is_stale_unparseable_busy_since_is_stale(monkeypatch):
    monkeypatch.setattr(lbl, "pid_alive", lambda pid: True)
    state = _state(owner=f"{HOST}:42", since="not-a-timestamp")
    assert lbl.is_stale("status: busy-implement", CFG, state, TTL, NOW, HOST) is True


def test_is_stale_naive_busy_since_uses_utc(monkeypatch):
    monkeypatch.setattr(lbl, "pid_alive", lambda pid: True)
    # ISO8601 without a TZ is interpreted as UTC; within TTL it is not stale
    state = _state(owner=f"{HOST}:42", since="2026-07-17T11:30:00")
    assert lbl.is_stale("status: busy-implement", CFG, state, TTL, NOW, HOST) is False


def test_is_stale_same_host_alive_pid_ttl_exceeded_is_stale(monkeypatch):
    monkeypatch.setattr(lbl, "pid_alive", lambda pid: True)
    state = _state(
        owner=f"{HOST}:42",
        since=(NOW - timedelta(seconds=TTL + 1)).isoformat(),
    )
    assert lbl.is_stale("status: busy-implement", CFG, state, TTL, NOW, HOST) is True
