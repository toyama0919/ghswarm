"""Tests for whether the create_pr phase reuses the spec PR (gh/git are not launched).

Line 1 (prpilot-spec) creates a draft spec PR first, so running `gh pr create` on
implementation completion would fail with "PR already exists". This guarantees the existing PR can be reused.
"""

from __future__ import annotations

from pathlib import Path

from prpilot import state as st
from prpilot.config import AgentConfig, RepoConfig
from prpilot.github import Issue
from prpilot.orchestrator import Orchestrator


class FakeGitHub:
    def __init__(self, existing_pr: dict | None):
        self.existing_pr = existing_pr
        self.created: list[dict] = []
        self.ready_calls: list[int] = []
        self.pr_bodies: dict[int, str] = {}
        self.comments: list[str] = []

    def pr_for_branch(self, head: str):
        return self.existing_pr

    def pr_number_for_branch(self, head: str):
        return self.existing_pr["number"] if self.existing_pr else 99

    def create_pr(self, head: str, base: str, title: str, body: str) -> str:
        self.created.append({"head": head, "base": base, "title": title, "body": body})
        return "https://example.com/pr/99"

    def mark_pr_ready(self, number: int) -> None:
        self.ready_calls.append(number)

    def set_pr_body(self, number: int, body: str) -> None:
        self.pr_bodies[number] = body

    def get_issue(self, number: int) -> Issue:
        return Issue(number=number, title="Test", body="Body", labels=[])

    def set_body(self, number: int, body: str) -> None:
        pass

    def comment(self, number: int, body: str) -> None:
        self.comments.append(body)

    def add_label(self, number: int, label: str) -> None:
        pass

    def remove_label(self, number: int, label: str) -> None:
        pass

    def ensure_label(self, label: str) -> None:
        pass


class FakeGit:
    """Fake Git for the main repository. Only implements ensure_worktree /
    remove_worktree. By not providing savepoint / push, calling them on the main
    side by mistake is detected via AttributeError.
    """

    def __init__(self):
        self.ensure_worktree_calls: list[tuple[str, str, str]] = []

    def ensure_worktree(self, branch: str, base: str, path: str) -> str:
        self.ensure_worktree_calls.append((branch, base, path))
        return path


class FakeWorktreeGit:
    """Fake Git for the worktree side (returned by _git_for)."""

    def __init__(self, cwd: str = "/tmp/worktree"):
        self.cwd = cwd
        self.savepoints: list[str] = []
        self.pushed: list[str] = []

    def savepoint(self, message: str) -> bool:
        self.savepoints.append(message)
        return True

    def push(self, branch: str) -> None:
        self.pushed.append(branch)


def _orchestrator(existing_pr: dict | None) -> tuple[Orchestrator, FakeGitHub]:
    orch = Orchestrator.__new__(Orchestrator)  # bypass __init__ since it touches gh/git
    orch.cfg = RepoConfig(agents={"claude": AgentConfig(name="claude", commands=["noop"])})
    orch.cwd = "/tmp"
    orch.gh = FakeGitHub(existing_pr)
    orch.git = FakeGit()
    wt = FakeWorktreeGit()
    orch._git_for = lambda path: wt
    orch._test_wt = wt  # for verification from the test
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.dry_run = False
    orch.agent_names = []
    return orch, orch.gh


def _state() -> st.IssueState:
    return st.IssueState(branch_name="issue-7", spec_path="", last_agent="claude")


def test_reuses_draft_spec_pr_and_marks_it_ready():
    orch, gh = _orchestrator({"number": 12, "url": "https://example.com/pr/12", "isDraft": True})
    result = orch._create_pr(Issue(number=7, title="Test", body="Body"), _state())

    assert gh.created == []  # does not create a new one
    assert gh.ready_calls == [12]  # clears the draft flag
    assert "Refs #7" in gh.pr_bodies[12]
    assert result.action == "pr_updated"
    assert result.detail == "https://example.com/pr/12"
    # savepoint / push are recorded only on the worktree Git, not on the main Git
    assert orch._test_wt.savepoints
    assert orch._test_wt.pushed == ["issue-7"]
    assert orch.git.ensure_worktree_calls  # the main Git only has ensure_worktree called


def test_reused_pr_that_is_already_ready_is_not_readied_again():
    orch, gh = _orchestrator({"number": 12, "url": "https://example.com/pr/12", "isDraft": False})
    orch._create_pr(Issue(number=7, title="Test", body="Body"), _state())
    assert gh.ready_calls == []


def test_creates_new_pr_when_no_spec_pr_exists():
    # e.g. when the spec PR was merged first. Falls back to creating a new one as before.
    orch, gh = _orchestrator(None)
    result = orch._create_pr(Issue(number=7, title="Test", body="Body"), _state())

    assert len(gh.created) == 1
    assert gh.created[0]["head"] == "issue-7"
    assert gh.created[0]["base"] == "main"
    assert result.action == "pr_created"


def test_state_points_at_the_reused_pr():
    orch, _ = _orchestrator({"number": 12, "url": "https://example.com/pr/12", "isDraft": True})
    state = _state()
    orch._create_pr(Issue(number=7, title="Test", body="Body"), state)

    assert state.pr_number == 12
    assert state.pr_url == "https://example.com/pr/12"
    assert state.next_action == "wait_ci"  # next, wait for CI/approve
