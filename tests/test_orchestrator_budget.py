"""Tests for the per-Issue cumulative agent-run guard (budget_exhausted)."""

from __future__ import annotations

from pathlib import Path

import ghswarm.orchestrator as orch_mod
from ghswarm import state as st
from ghswarm.config import AgentConfig, LabelConfig, RepoConfig
from ghswarm.events import EventLog
from ghswarm.executor import ConflictResolveResult, ExecResult
from ghswarm.github import Issue
from ghswarm.orchestrator import Orchestrator

BODY = """## Task breakdown

- [ ] Task A
"""

VERIFY_BLOCK = """<!-- GHSWARM_VERIFY_START
verify: []
GHSWARM_VERIFY_END -->"""


def _body_with_verify(base: str = BODY) -> str:
    return f"{base}\n\n{VERIFY_BLOCK}"


class FakeGitHub:
    def __init__(self, body: str, *, labels: list[str] | None = None):
        self.body = body
        self.current = list(labels or [])
        self.comments: list[str] = []
        self.bodies: list[str] = []

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
        self.bodies.append(body)

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


def _body_with_state(state: st.IssueState) -> str:
    return st.write_state(_body_with_verify(), state)


def _orch(
    monkeypatch,
    gh: FakeGitHub,
    *,
    issue_max_agent_runs: int = 10,
    dry_run: bool = False,
    tmp_path: Path | None = None,
    exec_result: ExecResult | None = None,
) -> Orchestrator:
    if exec_result is None:
        exec_result = ExecResult(True, "ok", 1)

    monkeypatch.setattr(
        "ghswarm.orchestrator.execute_with_self_healing",
        lambda *a, **k: exec_result,
    )
    monkeypatch.setattr("ghswarm.orchestrator.lbl.acquire", lambda *a, **k: None)

    if tmp_path is not None:
        wt = FakeWorktreeGit(str(tmp_path))
    else:
        wt = FakeWorktreeGit()

    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={
            "implement": AgentConfig(name="implement", commands=["noop"]),
            "review": AgentConfig(name="review", commands=["noop"]),
        },
        labels=LabelConfig(),
        issue_max_agent_runs=issue_max_agent_runs,
        max_retries=1,
    )
    orch.cwd = "/tmp"
    orch.gh = gh
    orch.git = FakeGit()
    orch._git_for = lambda path: wt
    orch._test_wt = wt
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.dry_run = dry_run
    orch.agent_names = ["implement", "review"]
    orch._event_log = EventLog("")
    orch._ensure_worktree_git = lambda number, branch: wt
    return orch


def _state(**kwargs) -> st.IssueState:
    defaults = {
        "branch_name": "issue-1",
        "next_action": "implement",
    }
    defaults.update(kwargs)
    return st.IssueState(**defaults)


def test_process_blocks_when_total_agent_runs_reaches_limit(monkeypatch):
    state = _state(total_agent_runs=10, next_action="implement")
    gh = FakeGitHub(_body_with_state(state))
    orch = _orch(monkeypatch, gh, issue_max_agent_runs=10)

    result = orch.process(1)

    assert result.action == "blocked"
    assert result.detail == "budget_exhausted"
    persisted = st.parse_state(gh.body, 1)
    assert persisted.phase == "blocked"
    assert any("Cumulative agent-run cap reached" in c for c in gh.comments)
    assert orch.cfg.labels.blocked in gh.current


def test_process_allows_when_below_limit(monkeypatch, tmp_path):
    state = _state(total_agent_runs=9, next_action="implement")
    gh = FakeGitHub(_body_with_state(state))
    orch = _orch(monkeypatch, gh, issue_max_agent_runs=10, tmp_path=tmp_path)

    result = orch.process(1)

    assert result.action == "implemented"
    persisted = st.parse_state(gh.body, 1)
    assert persisted.total_agent_runs == 10


def test_process_budget_disabled_when_issue_max_agent_runs_zero(monkeypatch, tmp_path):
    state = _state(total_agent_runs=100, next_action="implement")
    gh = FakeGitHub(_body_with_state(state))
    orch = _orch(monkeypatch, gh, issue_max_agent_runs=0, tmp_path=tmp_path)

    result = orch.process(1)

    assert result.action == "implemented"
    persisted = st.parse_state(gh.body, 1)
    assert persisted.total_agent_runs == 101


def test_process_dry_run_budget_skipped_without_blocked_side_effects(monkeypatch):
    state = _state(total_agent_runs=10, next_action="implement")
    gh = FakeGitHub(_body_with_state(state))
    orch = _orch(monkeypatch, gh, issue_max_agent_runs=10, dry_run=True)

    result = orch.process(1)

    assert result.action == "skipped"
    assert result.detail == "[dry-run] budget_exhausted"
    assert state.phase != "blocked"
    assert gh.comments == []
    assert orch.cfg.labels.blocked not in gh.current


def test_implement_increments_total_agent_runs(monkeypatch, tmp_path):
    body = _body_with_verify()
    gh = FakeGitHub(body)
    orch = _orch(
        monkeypatch,
        gh,
        tmp_path=tmp_path,
        exec_result=ExecResult(True, "ok", 1),
    )
    state = _state(total_agent_runs=0)

    orch._implement(Issue(number=1, title="test", body=body), state, resume=False)

    assert state.total_agent_runs == 1


def test_spec_missing_does_not_increment_total_agent_runs(monkeypatch):
    gh = FakeGitHub(BODY)
    orch = _orch(monkeypatch, gh)
    state = _state(total_agent_runs=0)

    orch._implement(Issue(number=1, title="test", body=BODY), state, resume=False)

    assert state.total_agent_runs == 0


def test_resolve_conflict_clean_merge_does_not_increment_total_agent_runs(monkeypatch):
    gh = FakeGitHub("body")
    orch = _orch(monkeypatch, gh)
    orch._persist = lambda issue, state: None
    orch._record_busy_lease = lambda issue, state: None
    orch._verify_steps_for = lambda body: []
    orch._spec_block = lambda issue_number, body: ""
    wt = FakeWorktreeGit()
    wt.try_push = lambda branch: True
    orch._ensure_worktree_git = lambda number, branch: wt
    monkeypatch.setattr(
        orch_mod,
        "resolve_conflict_with_agent",
        lambda *a, **k: ConflictResolveResult(True, clean_merge=True, attempts=0),
    )
    state = st.IssueState(branch_name="issue-7", pr_number=9, total_agent_runs=3)

    orch._resolve_conflict(Issue(number=7, title="test", body="body"), state)

    assert state.total_agent_runs == 3


def test_total_agent_runs_persists_across_parse_roundtrip():
    original = st.IssueState(branch_name="issue-7", total_agent_runs=5)
    body = st.write_state("body", original)
    restored = st.parse_state(body, 7)
    assert restored.total_agent_runs == 5
