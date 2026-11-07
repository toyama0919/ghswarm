"""Tests for the orchestrator's dependency start gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghswarm import state as st
from ghswarm.config import AgentConfig, LabelConfig, RepoConfig
from ghswarm.events import EventLog
from ghswarm.github import GitHubError, Issue, IssueRef
from ghswarm.orchestrator import Orchestrator, StepResult


VERIFY_START = st.VERIFY_START
VERIFY_END = st.VERIFY_END


class FakeGitHub:
    def __init__(
        self,
        body: str,
        *,
        labels: list[str] | None = None,
        refs: dict[int, IssueRef] | None = None,
        status_error: Exception | None = None,
    ):
        self.body = body
        self.current = list(labels or [])
        self.refs = refs or {}
        self.status_error = status_error
        self.comments: list[str] = []
        self.bodies: list[str] = []
        self.status_calls: list[int] = []

    def get_issue(self, number: int) -> Issue:
        return Issue(
            number=number,
            title="Test issue",
            body=self.body,
            labels=list(self.current),
            state="open",
            url="https://github.com/owner/repo/issues/11",
        )

    def issue_status(self, number: int) -> IssueRef:
        self.status_calls.append(number)
        if self.status_error:
            raise self.status_error
        return self.refs[number]

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


class FakeNotifier:
    enabled = True

    def __init__(self) -> None:
        self.blocked_calls: list[tuple[str, str]] = []

    def notify_blocked(self, issue, reason_code: str, detail: str, repo_name: str) -> None:
        self.blocked_calls.append((reason_code, detail))


def _body(
    state: st.IssueState,
    *,
    verify: str = "verify: []",
    with_task: bool = False,
) -> str:
    prose = "# Task\n\n- [ ] implement this" if with_task else "# Task"
    block = f"{VERIFY_START}\n{verify}\n{VERIFY_END}"
    return st.write_state(f"{prose}\n\n{block}", state)


def _state(**kwargs) -> st.IssueState:
    values = {"branch_name": "issue-11", "next_action": "start"}
    values.update(kwargs)
    return st.IssueState(**values)


def _orch(
    monkeypatch, gh: FakeGitHub, *, dry_run: bool = False
) -> tuple[Orchestrator, FakeNotifier]:
    monkeypatch.setattr(
        "ghswarm.orchestrator.execute_with_self_healing",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("agent should not run")),
    )
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={
            "implement": AgentConfig(name="implement", commands=["noop"]),
            "review": AgentConfig(name="review", commands=["noop"]),
        },
        labels=LabelConfig(),
        issue_max_agent_runs=10,
    )
    orch.repo_name = "owner/repo"
    orch.gh = gh
    orch.git = None
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.dry_run = dry_run
    orch.agent_names = ["implement", "review"]
    orch._event_log = EventLog("")
    notifier = FakeNotifier()
    orch._notifier = notifier
    return orch, notifier


def test_open_dependency_is_skipped_without_label_or_notification(monkeypatch):
    gh = FakeGitHub(
        _body(_state(), verify="depends_on: [10]\nverify: []"),
        labels=["status: idle"],
        refs={10: IssueRef("open", None, False, "")},
    )
    orch, notifier = _orch(monkeypatch, gh)

    result = orch.process(11)

    assert result == StepResult(11, "skipped", "waiting for #10")
    assert gh.current == ["status: idle"]
    assert gh.comments == []
    assert notifier.blocked_calls == []


def test_invalid_dependency_blocks_comments_and_notifies(monkeypatch):
    gh = FakeGitHub(
        _body(_state(), verify="depends_on: [10]\nverify: []"),
        labels=["status: idle"],
        refs={10: IssueRef("missing", None, False, "")},
    )
    orch, notifier = _orch(monkeypatch, gh)

    result = orch.process(11)

    assert result.action == "blocked"
    assert result.detail == "dependency_invalid"
    assert orch.cfg.labels.blocked in gh.current
    assert any("invalid dependency" in comment for comment in gh.comments)
    assert notifier.blocked_calls[0][0] == "dependency_invalid"


def test_broken_verify_yaml_still_uses_verify_invalid(monkeypatch):
    gh = FakeGitHub(
        _body(_state(), verify="depends_on: [10\nverify: []", with_task=True),
        labels=["status: idle"],
    )
    orch, notifier = _orch(monkeypatch, gh)
    orch._ensure_worktree_git = lambda number, branch: object()

    result = orch.process(11)

    assert result.action == "blocked"
    assert result.detail == "verify_invalid"
    assert notifier.blocked_calls[0][0] == "verify_invalid"


def test_force_bypasses_dependency_gate(monkeypatch):
    gh = FakeGitHub(
        _body(_state(), verify="depends_on: [10]\nverify: []"),
        refs={10: IssueRef("missing", None, False, "")},
    )
    orch, _notifier = _orch(monkeypatch, gh)
    called: list[int] = []

    def implement(issue, state, *, resume):
        called.append(issue.number)
        return StepResult(issue.number, "implemented", "bypassed")

    orch._implement = implement
    result = orch.process(11, force=True)

    assert result.action == "implemented"
    assert called == [11]
    assert gh.status_calls == []


def test_resume_waiting_returns_blocked_issue_to_idle(monkeypatch):
    state = _state(last_notified_reason="dependency_invalid", phase="blocked")
    gh = FakeGitHub(
        _body(state, verify="depends_on: [10]\nverify: []"),
        labels=["status: blocked"],
        refs={10: IssueRef("open", None, False, "")},
    )
    orch, notifier = _orch(monkeypatch, gh)

    result = orch.process(11, resume=True)

    assert result.action == "skipped"
    assert result.detail == "waiting for #10"
    assert gh.current == ["status: idle"]
    assert st.parse_state(gh.body, 11).last_notified_reason is None
    assert notifier.blocked_calls == []


def test_dependency_github_error_is_skipped(monkeypatch):
    gh = FakeGitHub(
        _body(_state(), verify="depends_on: [10]\nverify: []"),
        status_error=GitHubError("temporary failure"),
    )
    orch, _notifier = _orch(monkeypatch, gh)

    result = orch.process(11)

    assert result.action == "skipped"
    assert result.detail == "dependency check failed: temporary failure"
    assert gh.current == []
    assert gh.comments == []


@pytest.mark.parametrize("next_action", ["implement", "ai_review", "wait_ci"])
def test_only_start_is_gated(monkeypatch, next_action):
    gh = FakeGitHub(
        _body(_state(next_action=next_action), verify="depends_on: [10]\nverify: []"),
        refs={10: IssueRef("missing", None, False, "")},
    )
    orch, _notifier = _orch(monkeypatch, gh)
    called: list[int] = []
    method = {
        "implement": "_implement",
        "ai_review": "_review",
        "wait_ci": "_wait_ci",
    }[next_action]
    setattr(
        orch,
        method,
        lambda issue, state, **kwargs: called.append(issue.number) or StepResult(11, "passed"),
    )

    result = orch.process(11)

    assert result.action == "passed"
    assert called == [11]
    assert gh.status_calls == []


def test_no_dependency_behaves_as_before(monkeypatch):
    gh = FakeGitHub(_body(_state(), verify="verify: []"))
    orch, _notifier = _orch(monkeypatch, gh)
    orch._implement = lambda issue, state, *, resume: StepResult(11, "implemented")

    result = orch.process(11)

    assert result.action == "implemented"
    assert gh.status_calls == []


def test_dry_run_invalid_dependency_has_no_side_effects(monkeypatch):
    gh = FakeGitHub(
        _body(_state(), verify="depends_on: [10]\nverify: []"),
        labels=["status: idle"],
        refs={10: IssueRef("missing", None, False, "")},
    )
    orch, notifier = _orch(monkeypatch, gh, dry_run=True)

    result = orch.process(11)

    assert result.action == "skipped"
    assert result.detail.startswith("[dry-run] dependency_invalid:")
    assert gh.current == ["status: idle"]
    assert gh.comments == []
    assert gh.bodies == []
    assert notifier.blocked_calls == []
