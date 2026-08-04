"""Tests for whether _implement batches unfinished tasks into a single CLI run (gh/git/CLI are not launched).

Since the CLI is a headless one-shot run that loses context every time, invoking it per task
means re-reading the same codebase each time. Batching into one run is the requirement.
"""

from __future__ import annotations

import re
from pathlib import Path

from ghswarm import state as st
from ghswarm.config import AgentConfig, RepoConfig
from ghswarm.github import Issue
from ghswarm.orchestrator import Orchestrator, StepResult

BODY = """## Task breakdown

- [x] Done task
- [ ] Task A
- [ ] Task B
- [ ] Task C
"""

VERIFY_BLOCK = f"""{st.VERIFY_START}
verify: []
{st.VERIFY_END}"""


def _body_with_verify(base: str = BODY) -> str:
    return f"{base}\n\n{VERIFY_BLOCK}"


class FakeGitHub:
    def __init__(self, body: str):
        self.body = body
        self.comments: list[str] = []

    def get_issue(self, number: int) -> Issue:
        return Issue(number=number, title="Test", body=self.body, labels=[])

    def set_body(self, number: int, body: str) -> None:
        self.body = body

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
    remove_worktree, without savepoint / push. If the orchestrator mistakenly calls
    savepoint / push on the main side, it raises AttributeError, detecting commits
    leaking into the human's tree.
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


class Result:
    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason
        self.output = "out"
        self.attempts = 1


def _orchestrator(
    monkeypatch,
    body: str,
    tmp_path: Path,
    *,
    ok: bool = True,
):
    calls: list[str] = []

    def fake_execute(cfg, agent, git, verify_steps, prompt, on_question):
        calls.append(prompt)
        return Result(ok, "" if ok else "cli_failed")

    monkeypatch.setattr("ghswarm.orchestrator.execute_with_self_healing", fake_execute)

    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={
            "implement": AgentConfig(name="implement", commands=["noop"]),
            "review": AgentConfig(name="review", commands=["noop"]),
        }
    )
    orch.cwd = "/tmp"
    orch.gh = FakeGitHub(body)
    orch.git = FakeGit()
    wt = FakeWorktreeGit(str(tmp_path))
    orch._git_for = lambda path: wt
    orch._test_wt = wt  # for verification from the test
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.dry_run = False
    orch.agent_names = ["implement", "review"]
    orch._persist = lambda issue, state: None
    orch._release_idle = lambda issue, state: None
    orch._record_busy_lease = lambda issue, state: None
    return orch, calls


def _state() -> st.IssueState:
    return st.IssueState(branch_name="issue-1")


def test_all_unchecked_tasks_go_to_a_single_cli_invocation(monkeypatch, tmp_path):
    body = _body_with_verify()
    orch, calls = _orchestrator(monkeypatch, body, tmp_path)

    orch._implement(Issue(number=1, title="Test", body=body), _state(), resume=False)

    assert len(calls) == 1  # not invoked per task
    prompt = calls[0]
    assert "1. Task A" in prompt
    assert "2. Task B" in prompt
    assert "3. Task C" in prompt
    # completed tasks are not included in the numbered list of items to implement
    # (they still appear in the prompt as part of the Issue body, so we only look at the list side)
    assert re.search(r"^\s*\d+\.\s*Done task", prompt, re.M) is None


def test_success_checks_every_batched_task(monkeypatch, tmp_path):
    body = _body_with_verify()
    orch, _ = _orchestrator(monkeypatch, body, tmp_path)
    state = _state()

    result = orch._implement(Issue(number=1, title="Test", body=body), state, resume=False)

    assert st.unchecked(orch.gh.body) == []
    done, total = st.progress(orch.gh.body)
    assert (done, total) == (4, 4)
    assert state.next_action == "ai_review"  # no remaining tasks, so move to review
    assert result.action == "implemented"
    # savepoint is recorded only on the worktree Git, not on the main Git
    assert orch._test_wt.savepoints  # committed on the worktree side
    assert orch.git.ensure_worktree_calls  # the main Git only has ensure_worktree called


def test_implement_success_pushes_branch_to_origin(monkeypatch, tmp_path):
    body = _body_with_verify()
    orch, _ = _orchestrator(monkeypatch, body, tmp_path)
    state = st.IssueState(branch_name="issue-1")

    orch._implement(Issue(number=1, title="Test", body=body), state, resume=False)

    assert orch._test_wt.pushed == ["issue-1"]


def test_failure_leaves_tasks_unchecked(monkeypatch, tmp_path):
    body = _body_with_verify()
    orch, _ = _orchestrator(monkeypatch, body, tmp_path, ok=False)
    state = _state()

    result = orch._implement(Issue(number=1, title="Test", body=body), state, resume=False)

    assert result.action == "failed"
    assert len(st.unchecked(orch.gh.body)) == 3  # none are checked off
    assert state.phase == "blocked"


def test_no_unchecked_tasks_skips_cli_and_goes_to_review(monkeypatch, tmp_path):
    body = _body_with_verify("- [x] Done A\n- [x] Done B\n")
    orch, calls = _orchestrator(monkeypatch, body, tmp_path)
    orch.dry_run = True

    result = orch._implement(Issue(number=1, title="Test", body=body), _state(), resume=False)

    assert calls == []
    assert "ai_review" in result.detail


def test_missing_verify_block_blocks_implement_without_agent(monkeypatch, tmp_path):
    orch, calls = _orchestrator(monkeypatch, BODY, tmp_path)
    state = _state()

    result = orch._implement(Issue(number=1, title="Test", body=BODY), state, resume=False)

    assert calls == []
    assert result.action == "blocked"
    assert result.detail == "spec_missing"
    assert state.phase == "blocked"
    assert orch.gh.comments
    assert "no spec is set" in orch.gh.comments[0]


def test_no_tasks_with_missing_verify_blocks_review_agent(monkeypatch, tmp_path):
    body = "- [x] Done A\n- [x] Done B\n"
    orch, calls = _orchestrator(monkeypatch, body, tmp_path)
    state = _state()

    result = orch._implement(Issue(number=1, title="Test", body=body), state, resume=False)

    assert calls == []
    assert result.action == "blocked"
    assert result.detail == "spec_missing"
    assert state.phase == "blocked"


def test_review_blocks_without_verify_meta(monkeypatch, tmp_path):
    orch, calls = _orchestrator(monkeypatch, BODY, tmp_path)
    state = st.IssueState(branch_name="issue-1", next_action="ai_review")

    result = orch._review(Issue(number=1, title="Test", body=BODY), state)

    assert calls == []
    assert result.action == "blocked"
    assert result.detail == "spec_missing"


def test_review_dry_run_with_missing_verify_does_not_block(monkeypatch, tmp_path):
    orch, calls = _orchestrator(monkeypatch, BODY, tmp_path)
    orch.dry_run = True
    state = st.IssueState(branch_name="issue-1", next_action="ai_review")

    result = orch._review(Issue(number=1, title="Test", body=BODY), state)

    assert calls == []
    assert result.action == "skipped"
    assert "spec missing" in result.detail
    assert state.phase != "blocked"
    assert orch.gh.comments == []


def test_verify_invalid_blocks_implement(monkeypatch, tmp_path):
    bad_verify = f"{st.VERIFY_START}\nverify: [unclosed\n{st.VERIFY_END}"
    body = f"{BODY}\n\n{bad_verify}"
    orch, calls = _orchestrator(monkeypatch, body, tmp_path)
    state = _state()

    result = orch._implement(Issue(number=1, title="Test", body=body), state, resume=False)

    assert calls == []
    assert result.action == "blocked"
    assert result.detail == "verify_invalid"
    assert state.phase == "blocked"
    assert any("invalid verify" in c for c in orch.gh.comments)


def test_verify_invalid_blocks_review(monkeypatch, tmp_path):
    bad_verify = f"{st.VERIFY_START}\nverify: [unclosed\n{st.VERIFY_END}"
    body = f"{bad_verify}"
    orch, calls = _orchestrator(monkeypatch, body, tmp_path)
    state = st.IssueState(branch_name="issue-1", next_action="ai_review")

    result = orch._review(Issue(number=1, title="Test", body=body), state)

    assert calls == []
    assert result.action == "blocked"
    assert result.detail == "verify_invalid"


def test_no_tasks_with_verify_present_goes_to_review(monkeypatch, tmp_path):
    body = _body_with_verify("- [x] Done A\n- [x] Done B\n")
    orch, calls = _orchestrator(monkeypatch, body, tmp_path)
    state = _state()
    monkeypatch.setattr(
        orch,
        "_create_pr",
        lambda issue, s: StepResult(issue.number, "pr_created", ""),
    )

    result = orch._implement(Issue(number=1, title="Test", body=body), state, resume=False)

    assert len(calls) == 1
    assert "Spec consistency and correctness" in calls[0]
    assert result.action != "blocked"
