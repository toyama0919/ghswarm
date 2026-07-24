"""Tests for the auto-response flow to PR review comments (human / review bot).

gh / git / CLI are not launched; only the state machine's behavior is verified with fakes and mocks.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import ghswarm.github as github
import ghswarm.orchestrator as orch_mod
from ghswarm import state as st
from ghswarm.config import AgentConfig, RepoConfig
from ghswarm.github import GitHub, Issue, PRStatus, ReviewItem
from ghswarm.orchestrator import (
    REVIEW_RESPONSE_MARKER,
    Orchestrator,
    _format_review_items,
    _is_actionable,
    _parse_ts,
)


# -- github.pr_review_items ------------------------------------------------


def test_pr_review_items_merges_three_sources(monkeypatch):
    def fake_run_gh(args, cwd=None, input_text=None, env=None):
        path = args[-1]
        if "/pulls/5/reviews" in path:
            return json.dumps(
                [
                    {
                        "user": {"login": "human"},
                        "body": "fix this here",
                        "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-07-17T10:00:00Z",
                    },
                    {
                        "user": {"login": "boss"},
                        "body": "",
                        "state": "APPROVED",
                        "submitted_at": "2026-07-17T10:05:00Z",
                    },
                ]
            )
        if "/pulls/5/comments" in path:
            return json.dumps(
                [
                    {
                        "user": {"login": "coderabbit[bot]"},
                        "body": "possible null dereference",
                        "path": "src/a.py",
                        "line": 42,
                        "created_at": "2026-07-17T10:02:00Z",
                    }
                ]
            )
        if "/issues/5/comments" in path:
            return json.dumps(
                [
                    {
                        "user": {"login": "coderabbit[bot]"},
                        "body": "## Overall\nLGTM, but one point",
                        "created_at": "2026-07-17T10:01:00Z",
                    }
                ]
            )
        raise AssertionError(f"unexpected: {args}")

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    items = GitHub("o/r").pr_review_items(5)

    # a review that is APPROVED with no body is excluded; the remaining 3 are in ascending created_at order
    assert [it.kind for it in items] == ["review", "comment", "inline"]
    assert items[0].author == "human"
    assert items[0].state == "CHANGES_REQUESTED"
    assert items[2].path == "src/a.py"
    assert items[2].line == 42


def test_pr_review_items_tolerates_non_list(monkeypatch):
    # does not crash even if the API returns an error object (dict)
    monkeypatch.setattr(github, "_run_gh", lambda *a, **k: json.dumps({"message": "Not Found"}))
    assert GitHub("o/r").pr_review_items(5) == []


def test_pr_comment_posts_to_pr_conversation(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(github, "_run_gh", lambda args, **k: calls.append(args) or "")
    GitHub("o/r").pr_comment(5, "done")
    assert calls == [["pr", "comment", "5", "--repo", "o/r", "--body-file", "-"]]


# -- helpers ---------------------------------------------------------------


def test_is_actionable_skips_bare_approval():
    assert not _is_actionable(ReviewItem(author="a", kind="review", body="", state="APPROVED"))


def test_is_actionable_change_request_without_body():
    assert _is_actionable(ReviewItem(author="a", kind="review", body="", state="CHANGES_REQUESTED"))


def test_is_actionable_empty_comment_ignored():
    assert not _is_actionable(ReviewItem(author="a", kind="comment", body=""))


def test_parse_ts_handles_z_suffix():
    assert _parse_ts("2026-07-17T10:00:00Z") is not None
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None


def test_format_review_items_shows_location_and_author():
    text = _format_review_items(
        [ReviewItem(author="bot", kind="inline", body="fix", path="a.py", line=3)]
    )
    assert "a.py:3" in text and "@bot" in text


# -- Orchestrator fakes -----------------------------------------------------


class FakeGitHub:
    def __init__(self, items: list[ReviewItem], status: PRStatus):
        self.items = items
        self.status = status
        self.issue_comments: list[str] = []
        self.pr_comments: list[str] = []
        self.bodies: list[str] = []

    def pr_review_items(self, number: int) -> list[ReviewItem]:
        return self.items

    def pr_status(self, number: int) -> PRStatus:
        return self.status

    def get_issue(self, number: int) -> Issue:
        return Issue(number=number, title="Test", body="Body", labels=[])

    def set_body(self, number: int, body: str) -> None:
        self.bodies.append(body)

    def comment(self, number: int, body: str) -> None:
        self.issue_comments.append(body)

    def pr_comment(self, number: int, body: str) -> None:
        self.pr_comments.append(body)

    def add_label(self, number: int, label: str) -> None:
        pass

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
        self.pushed: list[str] = []

    def savepoint(self, message: str) -> bool:
        self.savepoints.append(message)
        return True

    def push(self, branch: str) -> None:
        self.pushed.append(branch)


def _orch(items, status=None):
    status = status or PRStatus(
        number=9, state="OPEN", mergeable="MERGEABLE", review_decision="", checks="success"
    )
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(agents={"review": AgentConfig(name="review", commands=["noop"])})
    orch.cwd = "/tmp"
    orch.gh = FakeGitHub(items, status)
    orch.git = FakeGit()
    wt = FakeWorktreeGit()
    orch._git_for = lambda path: wt
    orch._test_wt = wt
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.test_command = ""
    orch.dry_run = False
    orch.agent_names = []
    return orch


def _state():
    return st.IssueState(branch_name="issue-7", pr_number=9, last_agent="review")


# -- _pending_review_items -------------------------------------------------


def test_pending_excludes_own_response_and_old_items():
    items = [
        ReviewItem(
            author="human", kind="comment", body="old feedback", created_at="2026-07-17T09:00:00Z"
        ),
        ReviewItem(
            author="pm",
            kind="comment",
            body=f"{REVIEW_RESPONSE_MARKER}\naddressed",
            created_at="2026-07-17T11:00:00Z",
        ),
        ReviewItem(
            author="bot",
            kind="inline",
            body="new feedback",
            path="a.py",
            line=1,
            created_at="2026-07-17T12:00:00Z",
        ),
    ]
    orch = _orch(items)
    state = _state()
    state.last_review_addressed_at = "2026-07-17T10:00:00Z"  # high-water mark
    pending = orch._pending_review_items(state)
    assert [it.body for it in pending] == ["new feedback"]  # old + own comments are excluded


def test_pending_empty_when_no_pr():
    orch = _orch([ReviewItem(author="x", kind="comment", body="hi")])
    state = _state()
    state.pr_number = 0
    assert orch._pending_review_items(state) == []


# -- _address_review -------------------------------------------------------


def test_address_review_runs_agent_pushes_and_advances_watermark(monkeypatch):
    monkeypatch.setattr(
        orch_mod,
        "execute_with_self_healing",
        lambda *a, **k: SimpleNamespace(ok=True, reason="ok", output="", attempts=1),
    )
    orch = _orch([])
    state = _state()
    pending = [
        ReviewItem(
            author="bot",
            kind="inline",
            body="fix it",
            path="a.py",
            line=2,
            created_at="2026-07-17T12:00:00Z",
        ),
    ]
    result = orch._address_review(Issue(number=7, title="Test", body="Body"), state, pending)

    assert result.action == "review_addressed"
    assert orch._test_wt.pushed == ["issue-7"]  # pushed from the worktree
    assert state.next_action == "wait_ci"  # goes back to waiting on CI
    assert state.last_review_addressed_at == "2026-07-17T12:00:00Z"  # high-water mark advances
    # the response comment on the PR carries the marker (so our own comment is not picked up next time)
    assert any(REVIEW_RESPONSE_MARKER in c for c in orch.gh.pr_comments)


def test_address_review_blocks_on_agent_failure(monkeypatch):
    monkeypatch.setattr(
        orch_mod,
        "execute_with_self_healing",
        lambda *a, **k: SimpleNamespace(ok=False, reason="max_retries", output="boom", attempts=1),
    )
    orch = _orch([])
    state = _state()
    pending = [ReviewItem(author="h", kind="comment", body="x", created_at="2026-07-17T12:00:00Z")]
    result = orch._address_review(Issue(number=7, title="Test", body="Body"), state, pending)

    assert result.action == "failed"
    assert state.phase == "blocked"
    assert orch._test_wt.pushed == []  # no push on failure
    assert state.last_review_addressed_at == ""  # high-water mark does not advance


def test_address_review_dry_run_does_nothing():
    orch = _orch([])
    orch.dry_run = True
    pending = [ReviewItem(author="h", kind="comment", body="x")]
    result = orch._address_review(Issue(number=7, title="Test", body="Body"), _state(), pending)
    assert result.action == "skipped"
    assert orch._test_wt.pushed == []


# -- _wait_ci should branch to unaddressed reviews --------------------------


def test_wait_ci_routes_to_address_review_when_pending(monkeypatch):
    orch = _orch(
        [
            ReviewItem(
                author="bot",
                kind="inline",
                body="fix it",
                path="a.py",
                line=1,
                created_at="2026-07-17T12:00:00Z",
            )
        ]
    )
    calls: list[int] = []
    orch._address_review = lambda issue, state, pending: (
        calls.append(len(pending)) or orch_mod.StepResult(7, "review_addressed", "")
    )
    result = orch._wait_ci(Issue(number=7, title="Test", body="Body"), _state())
    assert result.action == "review_addressed"
    assert calls == [1]  # entered address_review with 1 unaddressed item


def test_wait_ci_skips_review_when_disabled(monkeypatch):
    orch = _orch(
        [
            ReviewItem(
                author="bot",
                kind="inline",
                body="fix it",
                path="a.py",
                line=1,
                created_at="2026-07-17T12:00:00Z",
            )
        ]
    )
    orch.cfg.address_pr_reviews = False
    orch._address_review = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not be called")
    )
    # require_approval=True by default + review_decision="" means no merge, so skip
    result = orch._wait_ci(Issue(number=7, title="Test", body="Body"), _state())
    assert result.action == "skipped"
