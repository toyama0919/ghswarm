"""Tests for process() stale busy-lock reclaim and skip-while-alive (no gh/git/CLI launched)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from prpilot import state as st
from prpilot.config import AgentConfig, RepoConfig, LabelConfig
from prpilot.events import EventLog
from prpilot.github import Issue
from prpilot.orchestrator import Orchestrator

HOST = "orch-host"
NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
BODY = """## Tasks

- [ ] To do
"""


class FakeGitHub:
    def __init__(self, body: str, labels: list[str]):
        self.body = body
        self.current = list(labels)
        self.comments: list[str] = []

    def get_issue(self, number: int) -> Issue:
        return Issue(
            number=number,
            title="test",
            body=self.body,
            labels=list(self.current),
            state="open",
        )

    def set_body(self, number: int, body: str) -> None:
        self.body = body

    def comment(self, number: int, body: str) -> None:
        self.comments.append(body)

    def add_label(self, number: int, label: str) -> None:
        if label not in self.current:
            self.current.append(label)

    def remove_label(self, number: int, label: str) -> None:
        if label in self.current:
            self.current.remove(label)

    def ensure_label(self, label: str) -> None:
        pass


class FakeGit:
    def ensure_worktree(self, branch: str, base: str, path: str) -> str:
        return path


class FakeWorktreeGit:
    def __init__(self, cwd: str = "/tmp/worktree"):
        self.cwd = cwd
        self.savepoints: list[str] = []

    def savepoint(self, message: str) -> bool:
        self.savepoints.append(message)
        return True


class Result:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.reason = ""
        self.output = "ok"
        self.attempts = 1


def _body_with_state(state: st.IssueState) -> str:
    return st.write_state(BODY, state)


def _orchestrator(monkeypatch, gh: FakeGitHub):
    monkeypatch.setattr("prpilot.orchestrator.execute_with_self_healing", lambda *a, **k: Result())
    monkeypatch.setattr("prpilot.labels.current_host", lambda: HOST)

    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={
            "implement": AgentConfig(name="implement", commands=["noop"]),
            "review": AgentConfig(name="review", commands=["noop"]),
        },
        labels=LabelConfig(),
        lock_ttl=3600,
    )
    orch.cwd = "/tmp"
    orch.gh = gh
    orch.git = FakeGit()
    wt = FakeWorktreeGit()
    orch._git_for = lambda path: wt
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.test_command = ""
    orch.dry_run = False
    orch.agent_names = ["implement", "review"]
    orch._event_log = EventLog("")
    return orch


def test_process_reclaims_stale_busy_and_continues(monkeypatch):
    state = st.IssueState(
        branch_name="issue-5",
        next_action="implement",
        busy_owner=f"{HOST}:99999",
        busy_since=(NOW - timedelta(seconds=30)).isoformat(),
    )
    gh = FakeGitHub(_body_with_state(state), ["status: busy-implement"])
    monkeypatch.setattr("prpilot.labels.pid_alive", lambda pid: False)
    orch = _orchestrator(monkeypatch, gh)

    result = orch.process(5)

    assert result.action != "skipped"
    assert "status: busy-implement" not in gh.current
    assert any("Reclaimed a stale busy lock" in c for c in gh.comments)


def test_process_skips_alive_busy_lock(monkeypatch):
    # Base busy_since on the real clock. With a fixed NOW it would exceed lock_ttl and get stale-reclaimed.
    state = st.IssueState(
        branch_name="issue-6",
        next_action="implement",
        busy_owner=f"{HOST}:42",
        busy_since=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
    )
    gh = FakeGitHub(_body_with_state(state), ["status: busy-implement"])
    monkeypatch.setattr("prpilot.labels.pid_alive", lambda pid: True)
    orch = _orchestrator(monkeypatch, gh)

    result = orch.process(6)

    assert result.action == "skipped"
    assert "locked" in result.detail
    assert gh.comments == []
    assert "status: busy-implement" in gh.current


def test_process_reclaims_stale_busy_before_clarification_skip(monkeypatch):
    """If it crashes after _persist but before release: reclaim the busy remnant, then evaluate clarification."""
    state = st.IssueState(
        branch_name="issue-7",
        next_action="wait_for_clarification",
        busy_owner=f"{HOST}:99999",
        busy_since=(NOW - timedelta(seconds=30)).isoformat(),
    )
    gh = FakeGitHub(_body_with_state(state), ["status: busy-implement"])
    monkeypatch.setattr("prpilot.labels.pid_alive", lambda pid: False)
    orch = _orchestrator(monkeypatch, gh)

    result = orch.process(7)

    assert result.action == "skipped"
    assert "waiting for a clarification answer" in result.detail
    assert "status: busy-implement" not in gh.current
    assert any("Reclaimed a stale busy lock" in c for c in gh.comments)
