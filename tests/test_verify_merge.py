"""Tests for the post-merge CI gate (verify_merge) and its surroundings (gh/git are not launched)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from prpilot import state as st
from prpilot.config import AgentConfig, RepoConfig
from prpilot.github import Issue
from prpilot.orchestrator import Orchestrator


class FakeGitHub:
    def __init__(self, *, commit_checks: str = "none", merge_sha: str = "mergesha"):
        self._commit_checks = commit_checks
        self._merge_sha = merge_sha
        self.merged: list[int] = []
        self.closed: list[int] = []
        self.comments: list[str] = []
        self.labels: set[str] = set()

    # read operations
    def get_issue(self, number: int) -> Issue:
        return Issue(number=number, title="Test", body="Body", labels=sorted(self.labels))

    def merge_commit_sha(self, pr_number: int) -> str:
        return self._merge_sha

    def commit_checks(self, sha: str) -> str:
        return self._commit_checks

    def pr_status(self, number: int):  # for tests that go through wait_ci
        raise AssertionError("pr_status must not be called in this test")

    # write operations
    def merge_pr(self, number: int, method: str = "squash", delete_branch: bool = True) -> None:
        self.merged.append(number)

    def close_issue(self, number: int) -> None:
        self.closed.append(number)

    def comment(self, number: int, body: str) -> None:
        self.comments.append(body)

    def set_body(self, number: int, body: str) -> None:
        pass

    def add_label(self, number: int, label: str) -> None:
        self.labels.add(label)

    def remove_label(self, number: int, label: str) -> None:
        self.labels.discard(label)

    def ensure_label(self, label: str) -> None:
        pass


class FakeGit:
    """Fake Git for the main repository. Only remove_worktree (after merge) is used."""

    def __init__(self):
        self.removed: list[tuple[str, str]] = []

    def remove_worktree(self, path: str, branch: str) -> None:
        self.removed.append((path, branch))


def _orchestrator(
    gh: FakeGitHub, *, post_merge_ci: bool = True, dry_run: bool = False
) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)  # bypass __init__ since it touches gh/git
    orch.cfg = RepoConfig(
        agents={"claude": AgentConfig(name="claude", commands=["noop"])},
        post_merge_ci=post_merge_ci,
    )
    orch.cwd = "/tmp"
    orch.gh = gh
    orch.git = FakeGit()
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.base_branch = "main"
    orch.dry_run = dry_run
    orch.agent_names = []
    return orch


def _state(**kw) -> st.IssueState:
    base = dict(branch_name="issue-7", pr_number=9, pr_url="https://example.com/pr/9")
    base.update(kw)
    return st.IssueState(**base)


def _issue() -> Issue:
    return Issue(number=7, title="Test", body="Body")


# -- the 4 branches of _verify_merge ----------------------------------------


def test_verify_merge_green_closes_issue():
    gh = FakeGitHub(commit_checks="success")
    orch = _orchestrator(gh)
    state = _state(merge_commit_sha="sha", next_action="verify_merge")
    result = orch._verify_merge(_issue(), state)

    assert gh.closed == [7]
    assert state.phase == "completed"
    assert state.next_action == "done"
    assert result.action == "merged"
    assert orch.git.removed == [(str(Path("/tmp/worktrees/issue-7")), "issue-7")]


def test_verify_merge_failure_blocks_without_closing():
    gh = FakeGitHub(commit_checks="failure")
    orch = _orchestrator(gh)
    state = _state(merge_commit_sha="sha", next_action="verify_merge")
    result = orch._verify_merge(_issue(), state)

    assert gh.closed == []  # Issue stays open
    assert state.phase == "blocked"
    assert result.action == "failed"
    assert orch.cfg.labels.blocked in gh.labels


def test_verify_merge_pending_skips():
    gh = FakeGitHub(commit_checks="pending")
    orch = _orchestrator(gh)
    state = _state(merge_commit_sha="sha", next_action="verify_merge")
    result = orch._verify_merge(_issue(), state)

    assert gh.closed == []
    assert result.action == "skipped"
    assert state.next_action == "verify_merge"  # unchanged; re-evaluated on the next loop


def test_verify_merge_none_within_grace_skips():
    gh = FakeGitHub(commit_checks="none")
    orch = _orchestrator(gh)
    now = datetime.now(timezone.utc).isoformat()
    state = _state(merge_commit_sha="sha", merged_at=now, next_action="verify_merge")
    result = orch._verify_merge(_issue(), state)

    assert gh.closed == []
    assert result.action == "skipped"


def test_verify_merge_none_past_grace_closes():
    gh = FakeGitHub(commit_checks="none")
    orch = _orchestrator(gh)
    old = (datetime.now(timezone.utc) - timedelta(seconds=10_000)).isoformat()
    state = _state(merge_commit_sha="sha", merged_at=old, next_action="verify_merge")
    result = orch._verify_merge(_issue(), state)

    assert gh.closed == [7]  # grace exceeded, treated as "no CI" and closed
    assert result.action == "merged"


# -- dry-run --------------------------------------------------------------


def test_verify_merge_dry_run_does_not_close():
    gh = FakeGitHub(commit_checks="success")
    orch = _orchestrator(gh, dry_run=True)
    state = _state(merge_commit_sha="sha", next_action="verify_merge")
    result = orch._verify_merge(_issue(), state)

    assert gh.closed == []
    assert result.action == "skipped"


# -- _enter_verify_merge / post_merge_ci -----------------------------------


def test_enter_verify_merge_transitions_without_closing():
    gh = FakeGitHub(merge_sha="deadbeef")
    orch = _orchestrator(gh, post_merge_ci=True)
    state = _state()
    result = orch._enter_verify_merge(_issue(), state)

    assert gh.closed == []  # do not close after merge
    assert state.phase == "merged"
    assert state.next_action == "verify_merge"
    assert state.merge_commit_sha == "deadbeef"
    assert state.merged_at  # ISO8601 is recorded
    assert result.action == "merged"


def test_post_merge_ci_false_closes_immediately():
    gh = FakeGitHub()
    orch = _orchestrator(gh, post_merge_ci=False)
    state = _state()
    result = orch._enter_verify_merge(_issue(), state)

    assert gh.closed == [7]  # as before, close right after merge
    assert state.phase == "completed"
    assert state.next_action == "done"
    assert result.action == "merged"
