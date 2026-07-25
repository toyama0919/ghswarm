"""Tests for event recording in Orchestrator.process()."""

from __future__ import annotations

from pathlib import Path

from ghswarm import state as st
from ghswarm.config import AgentConfig, LabelConfig, RepoConfig
from ghswarm.events import EventLog
from ghswarm.github import Issue
from ghswarm.orchestrator import Orchestrator

BODY = """## Tasks

- [ ] Do the thing
"""


class FakeGitHub:
    def __init__(self, body: str, *, state: str = "open", labels: list[str] | None = None):
        self.body = body
        self.issue_state = state
        self.current = list(labels or [])
        self.comments: list[str] = []

    def get_issue(self, number: int) -> Issue:
        return Issue(
            number=number,
            title="Test",
            body=self.body,
            labels=list(self.current),
            state=self.issue_state,
        )

    def comment(self, number: int, body: str) -> None:
        self.comments.append(body)

    def add_label(self, number: int, label: str) -> None:
        if label not in self.current:
            self.current.append(label)

    def remove_label(self, number: int, label: str) -> None:
        if label in self.current:
            self.current.remove(label)

    def set_body(self, number: int, body: str) -> None:
        self.body = body

    def ensure_label(self, label: str) -> None:
        pass


class FakeGit:
    def ensure_worktree(self, branch: str, base: str, path: str) -> str:
        return path


def _orch(
    monkeypatch,
    gh: FakeGitHub,
    *,
    dry_run: bool,
    db_path: Path,
    issue_max_agent_runs: int = 10,
) -> Orchestrator:
    monkeypatch.setattr("ghswarm.labels.current_host", lambda: "test-host")

    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={
            "implement": AgentConfig(name="implement", commands=["noop"]),
            "review": AgentConfig(name="review", commands=["noop"]),
        },
        labels=LabelConfig(),
        event_db=str(db_path),
        issue_max_agent_runs=issue_max_agent_runs,
    )
    orch.repo_name = "owner/repo"
    orch.gh = gh
    orch.git = FakeGit()
    orch._git_for = lambda path: FakeGit()
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.dry_run = dry_run
    orch.agent_names = ["implement", "review"]
    orch._event_log = EventLog(str(db_path))
    orch._enter_blocked = lambda issue, state, reason, detail="": None
    return orch


def test_process_records_non_skipped_step(monkeypatch, tmp_path):
    state = st.IssueState(
        branch_name="issue-1",
        next_action="implement",
        total_agent_runs=10,
    )
    gh = FakeGitHub(st.write_state(BODY, state))
    db = tmp_path / "events.db"
    orch = _orch(monkeypatch, gh, dry_run=False, db_path=db)

    result = orch.process(1)

    assert result.action == "blocked"
    el = EventLog(str(db))
    rows = el.read()
    assert len(rows) == 1
    assert rows[0]["action"] == "blocked"
    assert rows[0]["repo"] == "owner/repo"
    assert rows[0]["issue"] == 1


def test_process_skipped_does_not_record(monkeypatch, tmp_path):
    gh = FakeGitHub(BODY, state="closed")
    db = tmp_path / "events.db"
    orch = _orch(monkeypatch, gh, dry_run=False, db_path=db)

    result = orch.process(2)

    assert result.action == "skipped"
    assert EventLog(str(db)).read() == []


def test_process_dry_run_does_not_record(monkeypatch, tmp_path):
    state = st.IssueState(
        branch_name="issue-3",
        next_action="implement",
        total_agent_runs=10,
    )
    gh = FakeGitHub(st.write_state(BODY, state))
    db = tmp_path / "events.db"
    orch = _orch(monkeypatch, gh, dry_run=True, db_path=db)

    orch.process(3)

    assert EventLog(str(db)).read() == []
