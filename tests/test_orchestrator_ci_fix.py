"""Tests for the _wait_ci automatic CI-failure fix flow."""

from __future__ import annotations

from pathlib import Path

import ghswarm.orchestrator as orch_mod
from ghswarm import state as st
from ghswarm.config import AgentConfig, RepoConfig
from ghswarm.executor import ExecResult
from ghswarm.github import Issue, PRStatus
from ghswarm.orchestrator import Orchestrator


class FakeGitHub:
    def __init__(self, status: PRStatus, *, gha_logs: str | None = "log output"):
        self.status = status
        self.gha_logs = gha_logs
        self.issue_comments: list[str] = []
        self.bodies: list[str] = []
        self.labels: list[str] = []

    def pr_status(self, number: int) -> PRStatus:
        return self.status

    def failed_gha_ci_logs(self, number: int) -> str | None:
        return self.gha_logs

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
    def __init__(self, *, push_ok: bool = True, savepoint_ok: bool = True):
        self.cwd = "/tmp/worktree"
        self.push_ok = push_ok
        self.savepoint_ok = savepoint_ok
        self.pushed: list[str] = []
        self.savepoints: list[str] = []

    def savepoint(self, message: str) -> bool:
        self.savepoints.append(message)
        return self.savepoint_ok

    def try_push(self, branch: str) -> bool:
        if self.push_ok:
            self.pushed.append(branch)
            return True
        return False


def _orch(
    status: PRStatus,
    *,
    auto_fix_ci: bool = True,
    ci_fix_max_retries: int = 3,
    gha_logs: str | None = "FAILED: test",
):
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={"implement": AgentConfig(name="implement", commands=["noop"])},
        auto_fix_ci=auto_fix_ci,
        ci_fix_max_retries=ci_fix_max_retries,
    )
    orch.cwd = "/tmp"
    orch.gh = FakeGitHub(status, gha_logs=gha_logs)
    orch.git = FakeGit()
    wt = FakeWorktreeGit()
    orch._git_for = lambda path: wt
    orch._test_wt = wt
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.dry_run = False
    orch.agent_names = ["implement"]
    orch._persist = lambda issue, state: None
    orch._record_busy_lease = lambda issue, state: None
    orch._ensure_worktree_git = lambda number, branch: wt
    orch._verify_steps_for = lambda state, worktree: []
    orch._spec_block = lambda state, worktree: ""
    return orch


def _state(ci_fix_retries: int = 0) -> st.IssueState:
    return st.IssueState(
        branch_name="issue-7",
        pr_number=9,
        ci_fix_retries=ci_fix_retries,
    )


def _failed_status() -> PRStatus:
    return PRStatus(
        number=9,
        state="OPEN",
        mergeable="MERGEABLE",
        review_decision="",
        checks="failure",
        url="https://github.com/o/r/pull/9",
    )


def test_wait_ci_fix_success_resets_retries_and_returns_wait_ci(monkeypatch):
    orch = _orch(_failed_status())
    monkeypatch.setattr(
        orch_mod,
        "fix_ci_with_agent",
        lambda *a, **k: ExecResult(True, "ok", 1),
    )
    state = _state(ci_fix_retries=2)
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), state)

    assert result.action == "ci_fixed"
    assert orch._test_wt.pushed == ["issue-7"]
    assert state.ci_fix_retries == 0
    assert state.next_action == "wait_ci"


def test_wait_ci_fix_blocked_after_max_retries(monkeypatch):
    orch = _orch(_failed_status(), ci_fix_max_retries=2)
    monkeypatch.setattr(
        orch_mod,
        "fix_ci_with_agent",
        lambda *a, **k: ExecResult(False, "boom", 1, reason="verify_failed"),
    )
    state = _state(ci_fix_retries=2)
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), state)

    assert result.action == "blocked"
    assert state.phase == "blocked"
    assert state.ci_fix_retries == 3
    assert orch._test_wt.pushed == []
    assert any("A manual fix is needed" in c for c in orch.gh.issue_comments)


def test_wait_ci_fix_disabled_blocks_immediately():
    orch = _orch(_failed_status(), auto_fix_ci=False)
    orch._fix_ci = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called"))
    state = _state()
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), state)

    assert result.action == "failed"
    assert state.phase == "blocked"
    assert any("Human intervention is needed" in c for c in orch.gh.issue_comments)


def test_wait_ci_non_gha_ci_blocks_without_agent(monkeypatch):
    orch = _orch(_failed_status(), gha_logs=None)
    monkeypatch.setattr(
        orch_mod,
        "fix_ci_with_agent",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the agent must not be called")),
    )
    state = _state()
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), state)

    assert result.action == "failed"
    assert state.phase == "blocked"
    assert any("non-GHA CI" in c for c in orch.gh.issue_comments)


def test_wait_ci_fix_transient_delegates_without_incrementing_retries(monkeypatch):
    orch = _orch(_failed_status())
    monkeypatch.setattr(
        orch_mod,
        "fix_ci_with_agent",
        lambda *a, **k: ExecResult(False, "rate limit", 1, reason="transient"),
    )
    state = _state(ci_fix_retries=1)
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), state)

    assert result.action == "retry_pending"
    assert state.ci_fix_retries == 1
    assert state.transient_retries == 1


def test_wait_ci_fix_empty_savepoint_increments_retries(monkeypatch):
    orch = _orch(_failed_status())
    orch._test_wt.savepoint_ok = False
    monkeypatch.setattr(
        orch_mod,
        "fix_ci_with_agent",
        lambda *a, **k: ExecResult(True, "ok", 1),
    )
    state = _state()
    result = orch._wait_ci(Issue(number=7, title="test", body="body"), state)

    assert result.action == "retry_pending"
    assert state.ci_fix_retries == 1
    assert orch._test_wt.pushed == []
