"""Tests for the _wait_ci automatic merge-conflict resolution flow."""

from __future__ import annotations

from pathlib import Path

import prpilot.orchestrator as orch_mod
from prpilot import state as st
from prpilot.config import AgentConfig, RepoConfig
from prpilot.executor import ConflictResolveResult
from prpilot.github import Issue, PRStatus
from prpilot.orchestrator import Orchestrator


class FakeGitHub:
    def __init__(self, status: PRStatus):
        self.status = status
        self.issue_comments: list[str] = []
        self.bodies: list[str] = []
        self.labels: list[str] = []

    def pr_status(self, number: int) -> PRStatus:
        return self.status

    def pr_review_items(self, number: int) -> list:
        return []

    def get_issue(self, number: int) -> Issue:
        return Issue(number=number, title="test", body="body", labels=[])

    def set_body(self, number: int, body: str) -> None:
        self.bodies.append(body)

    def comment(self, number: int, body: str) -> None:
        self.issue_comments.append(body)

    def add_label(self, number: int, label: str) -> None:
        self.labels.append(label)

    def remove_label(self, number: int, label: str) -> None:
        pass

    def ensure_label(self, label: str) -> None:
        pass


class FakeGit:
    def ensure_worktree(self, branch: str, base: str, path: str) -> str:
        return path


class FakeWorktreeGit:
    def __init__(self, *, push_ok: bool = True, commit_ok: bool = True):
        self.cwd = "/tmp/worktree"
        self.push_ok = push_ok
        self.commit_ok = commit_ok
        self.pushed: list[str] = []
        self.finalized = False
        self.aborted = False

    def finalize_merge_commit(self) -> bool:
        self.finalized = True
        return self.commit_ok

    def try_push(self, branch: str) -> bool:
        if self.push_ok:
            self.pushed.append(branch)
            return True
        return False

    def abort_merge(self) -> None:
        self.aborted = True


def _orch(status: PRStatus, *, auto_resolve: bool = True, conflict_max_retries: int = 3):
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={"implement": AgentConfig(name="implement", commands=["noop"])},
        auto_resolve_conflicts=auto_resolve,
        conflict_max_retries=conflict_max_retries,
    )
    orch.cwd = "/tmp"
    orch.gh = FakeGitHub(status)
    orch.git = FakeGit()
    wt = FakeWorktreeGit()
    orch._git_for = lambda path: wt
    orch._test_wt = wt
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.test_command = ""
    orch.dry_run = False
    orch.agent_names = ["implement"]
    orch._persist = lambda issue, state: None
    orch._record_busy_lease = lambda issue, state: None
    orch._ensure_worktree_git = lambda number, branch: wt
    orch._test_command_for = lambda state, worktree: ""
    orch._spec_block = lambda state, worktree: ""
    return orch


def _state(conflict_retries: int = 0) -> st.IssueState:
    return st.IssueState(
        branch_name="issue-7",
        pr_number=9,
        conflict_retries=conflict_retries,
    )


def _conflicting_status() -> PRStatus:
    return PRStatus(
        number=9,
        state="OPEN",
        mergeable="CONFLICTING",
        review_decision="",
        checks="success",
    )


def test_wait_ci_resolves_conflict_and_resets_retries(monkeypatch):
    orch = _orch(_conflicting_status())
    monkeypatch.setattr(
        orch_mod,
        "resolve_conflict_with_agent",
        lambda *a, **k: ConflictResolveResult(True, clean_merge=True),
    )
    state = _state(conflict_retries=2)
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), state)

    assert result.action == "conflict_resolved"
    assert orch._test_wt.pushed == ["issue-7"]
    assert state.conflict_retries == 0


def test_wait_ci_conflict_blocked_after_max_retries(monkeypatch):
    orch = _orch(_conflicting_status(), conflict_max_retries=2)
    monkeypatch.setattr(
        orch_mod,
        "resolve_conflict_with_agent",
        lambda *a, **k: ConflictResolveResult(False, reason="verify_failed", output="boom"),
    )
    state = _state(conflict_retries=2)
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), state)

    assert result.action == "blocked"
    assert state.phase == "blocked"
    assert state.conflict_retries == 3
    assert orch._test_wt.pushed == []
    assert any("A manual merge/resolution is needed" in c for c in orch.gh.issue_comments)


def test_wait_ci_conflict_push_failure_increments_retries(monkeypatch):
    orch = _orch(_conflicting_status())
    orch._test_wt.push_ok = False
    monkeypatch.setattr(
        orch_mod,
        "resolve_conflict_with_agent",
        lambda *a, **k: ConflictResolveResult(True, clean_merge=True),
    )
    state = _state()
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), state)

    assert result.action == "retry_pending"
    assert state.conflict_retries == 1
    assert orch._test_wt.pushed == []


def test_wait_ci_skips_conflict_when_disabled():
    orch = _orch(_conflicting_status(), auto_resolve=False)
    orch._resolve_conflict = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not be called")
    )
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), _state())
    assert result.action == "skipped"


def test_wait_ci_skips_unknown_mergeable():
    status = PRStatus(
        number=9,
        state="OPEN",
        mergeable="UNKNOWN",
        review_decision="",
        checks="success",
    )
    orch = _orch(status)
    orch._resolve_conflict = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not be called")
    )
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), _state())
    assert result.action == "skipped"


def test_resolve_conflict_finalizes_merge_when_agent_was_used(monkeypatch):
    orch = _orch(_conflicting_status())
    monkeypatch.setattr(
        orch_mod,
        "resolve_conflict_with_agent",
        lambda *a, **k: ConflictResolveResult(True, clean_merge=False),
    )
    state = _state()
    result = orch._resolve_conflict(Issue(number=7, title="test", body="body"), state)

    assert result.action == "conflict_resolved"
    assert orch._test_wt.finalized is True
    assert orch._test_wt.pushed == ["issue-7"]
