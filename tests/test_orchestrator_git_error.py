"""Tests for blocked promotion, notification, and dedup suppression when a GitError occurs."""

from __future__ import annotations

from pathlib import Path

from ghswarm import state as st
from ghswarm.config import AgentConfig, LabelConfig, RepoConfig
from ghswarm.events import EventLog
from ghswarm.git_ops import GitError
from ghswarm.github import Issue
from ghswarm.orchestrator import Orchestrator

BODY = """## Task breakdown

- [ ] Task A
"""

VERIFY_BLOCK = f"""{st.VERIFY_START}
verify: []
{st.VERIFY_END}"""

BODY_WITH_VERIFY = f"{BODY}\n\n{VERIFY_BLOCK}"
GIT_ERROR_MSG = "fatal: invalid reference: nonexistent-base"


class FakeGitHub:
    def __init__(self, body: str, *, labels: list[str] | None = None):
        self.body = body
        self.current = list(labels or [])
        self.comments: list[str] = []

    def get_issue(self, number: int) -> Issue:
        return Issue(
            number=number,
            title="Test",
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


class RaisingFakeGit:
    def ensure_worktree(self, branch: str, base: str, path: str) -> str:
        raise GitError(GIT_ERROR_MSG)


class FakeNotifier:
    enabled = True

    def __init__(self) -> None:
        self.blocked_calls: list[tuple[str, str]] = []

    def notify_blocked(self, issue: Issue, reason_code: str, detail: str, repo_name: str) -> None:
        self.blocked_calls.append((reason_code, detail))


def _body_with_state(state: st.IssueState) -> str:
    return st.write_state(BODY_WITH_VERIFY, state)


def _state(**kwargs) -> st.IssueState:
    defaults = {
        "branch_name": "issue-1",
        "next_action": "implement",
    }
    defaults.update(kwargs)
    return st.IssueState(**defaults)


def _orch(monkeypatch, gh: FakeGitHub) -> tuple[Orchestrator, FakeNotifier]:
    monkeypatch.setattr(
        "ghswarm.orchestrator.execute_with_self_healing",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent should not run")),
    )

    labels = LabelConfig()
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={
            "implement": AgentConfig(name="implement", commands=["noop"]),
            "review": AgentConfig(name="review", commands=["noop"]),
        },
        labels=labels,
    )
    orch.repo_name = "owner/repo"
    orch.gh = gh
    orch.git = RaisingFakeGit()
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "nonexistent-base"
    orch.dry_run = False
    orch.agent_names = ["implement", "review"]
    orch._event_log = EventLog("")
    notifier = FakeNotifier()
    orch._notifier = notifier

    for name in (
        "process",
        "_step",
        "_implement",
        "_ensure_worktree_git",
        "_worktree_path",
        "_record_busy_lease",
        "_persist",
        "_enter_blocked",
        "_notifier_for",
    ):
        setattr(orch, name, getattr(Orchestrator, name).__get__(orch, Orchestrator))
    orch._worktree_ready = lambda path, branch: False

    return orch, notifier


def test_process_git_error_blocks_and_notifies(monkeypatch):
    state = _state()
    gh = FakeGitHub(_body_with_state(state))
    orch, notifier = _orch(monkeypatch, gh)

    result = orch.process(1)

    assert result.action == "blocked"
    assert result.detail == GIT_ERROR_MSG
    persisted = st.parse_state(gh.body, 1)
    assert persisted.phase == "blocked"
    assert orch.cfg.labels.blocked in gh.current
    assert len(notifier.blocked_calls) == 1
    assert notifier.blocked_calls[0] == ("git_error", GIT_ERROR_MSG)


def test_process_git_error_dedup_on_force_retry(monkeypatch):
    state = _state()
    gh = FakeGitHub(_body_with_state(state))
    orch, notifier = _orch(monkeypatch, gh)

    orch.process(1)
    assert len(notifier.blocked_calls) == 1

    result = orch.process(1, force=True)

    assert result.action == "blocked"
    assert len(notifier.blocked_calls) == 1


def test_process_git_error_releases_busy_label(monkeypatch):
    state = _state()
    gh = FakeGitHub(_body_with_state(state))
    orch, _notifier = _orch(monkeypatch, gh)
    labels = orch.cfg.labels

    orch.process(1)

    busy_labels = {labels.busy(a) for a in orch.agent_names}
    assert not busy_labels.intersection(gh.current)
    assert labels.blocked in gh.current
    status_labels = set(labels.all_status(orch.agent_names))
    assert set(gh.current) & status_labels == {labels.blocked}
