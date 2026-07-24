"""Tests for in-loop retries of transient errors and escalation to blocked."""

from __future__ import annotations

from pathlib import Path

from prpilot import state as st
from prpilot.config import AgentConfig, RepoConfig, LabelConfig
from prpilot.executor import ExecResult
from prpilot.github import Issue
from prpilot.orchestrator import Orchestrator

BODY = """## Task breakdown

- [ ] Task A
"""

SPEC_PATH = ".specs/test-spec.md"


def _write_spec(
    worktree_root: Path, spec_path: str = SPEC_PATH, *, content: str = "# test\n"
) -> None:
    path = worktree_root / spec_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class FakeGitHub:
    def __init__(self, body: str):
        self.body = body
        self.comments: list[str] = []
        self.released: list[str] = []

    def get_issue(self, number: int) -> Issue:
        return Issue(number=number, title="test", body=self.body, labels=[])

    def set_body(self, number: int, body: str) -> None:
        self.body = body

    def comment(self, number: int, body: str) -> None:
        self.comments.append(body)

    def add_label(self, number: int, label: str) -> None:
        if label.startswith("status:"):
            self.released.append(label)

    def remove_label(self, number: int, label: str) -> None:
        pass

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


def _orchestrator(
    monkeypatch, body: str, *, exec_result: ExecResult, tmp_path: Path | None = None
) -> Orchestrator:
    monkeypatch.setattr(
        "prpilot.orchestrator.execute_with_self_healing",
        lambda *a, **k: exec_result,
    )
    monkeypatch.setattr("prpilot.orchestrator.lbl.acquire", lambda *a, **k: None)

    if tmp_path is not None:
        _write_spec(tmp_path)
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
        transient_max_retries=3,
    )
    orch.cwd = "/tmp"
    orch.gh = FakeGitHub(body)
    orch.git = FakeGit()
    orch._git_for = lambda path: wt
    orch._test_wt = wt
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.test_command = ""
    orch.dry_run = False
    orch.agent_names = ["implement", "review"]
    return orch


def _state(**kwargs) -> st.IssueState:
    defaults = {"branch_name": "issue-1", "spec_path": SPEC_PATH, "phase": "implementing"}
    defaults.update(kwargs)
    return st.IssueState(**defaults)


def test_transient_failure_returns_idle_and_increments_counter(monkeypatch, tmp_path):
    orch = _orchestrator(
        monkeypatch,
        BODY,
        exec_result=ExecResult(False, "RetriableError", 1, reason="transient"),
        tmp_path=tmp_path,
    )
    state = _state(transient_retries=0)

    result = orch._implement(Issue(number=1, title="test", body=BODY), state, resume=False)

    assert result.action == "retry_pending"
    assert state.transient_retries == 1
    assert state.phase == "implementing"
    assert orch.cfg.labels.idle in orch.gh.released
    assert orch.cfg.labels.blocked not in orch.gh.released
    assert orch.gh.comments == []


def test_transient_max_retries_escalates_to_blocked(monkeypatch, tmp_path):
    orch = _orchestrator(
        monkeypatch,
        BODY,
        exec_result=ExecResult(False, "RetriableError", 1, reason="transient"),
        tmp_path=tmp_path,
    )
    state = _state(transient_retries=3)

    result = orch._implement(Issue(number=1, title="test", body=BODY), state, resume=False)

    assert result.action == "blocked"
    assert state.transient_retries == 4
    assert state.phase == "blocked"
    assert orch.cfg.labels.blocked in orch.gh.released
    assert any("Transient-error retry cap reached" in c for c in orch.gh.comments)


def test_success_resets_transient_retries(monkeypatch, tmp_path):
    orch = _orchestrator(
        monkeypatch,
        BODY,
        exec_result=ExecResult(True, "ok", 1),
        tmp_path=tmp_path,
    )
    state = _state(transient_retries=2)

    orch._implement(Issue(number=1, title="test", body=BODY), state, resume=False)

    assert state.transient_retries == 0


def test_permanent_cli_failed_still_blocks(monkeypatch, tmp_path):
    orch = _orchestrator(
        monkeypatch,
        BODY,
        exec_result=ExecResult(False, "fatal error", 1, reason="cli_failed"),
        tmp_path=tmp_path,
    )
    state = _state(transient_retries=0)

    result = orch._implement(Issue(number=1, title="test", body=BODY), state, resume=False)

    assert result.action == "failed"
    assert state.phase == "blocked"
    assert orch.cfg.labels.blocked in orch.gh.released
    assert state.transient_retries == 0


def test_review_transient_returns_retry_pending(monkeypatch):
    orch = _orchestrator(
        monkeypatch,
        BODY,
        exec_result=ExecResult(False, "resource_exhausted", 1, reason="transient"),
    )
    state = _state(phase="reviewing", next_action="ai_review", transient_retries=1)

    result = orch._review(Issue(number=1, title="test", body=BODY), state)

    assert result.action == "retry_pending"
    assert state.transient_retries == 2
    assert state.phase == "reviewing"
    assert orch.cfg.labels.idle in orch.gh.released
