"""Tests for whether _implement batches unfinished tasks into a single CLI run (gh/git/CLI are not launched).

Since the CLI is a headless one-shot run that loses context every time, invoking it per task
means re-reading the same codebase each time. Batching into one run is the requirement.
"""

from __future__ import annotations

import re
from pathlib import Path

from prpilot import state as st
from prpilot.config import AgentConfig, RepoConfig
from prpilot.github import Issue
from prpilot.orchestrator import Orchestrator, StepResult

BODY = """## Task breakdown

- [x] Done task
- [ ] Task A
- [ ] Task B
- [ ] Task C
"""

SPEC_PATH = ".specs/test-spec.md"


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

    def savepoint(self, message: str) -> bool:
        self.savepoints.append(message)
        return True


class Result:
    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason
        self.output = "out"
        self.attempts = 1


def _write_spec(
    worktree_root: Path, spec_path: str = SPEC_PATH, *, content: str = "# test\n"
) -> None:
    path = worktree_root / spec_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _orchestrator(
    monkeypatch,
    body: str,
    tmp_path: Path,
    *,
    ok: bool = True,
    spec_path: str = SPEC_PATH,
    create_spec: bool = True,
):
    calls: list[str] = []

    def fake_execute(cfg, agent, git, test_command, prompt, on_question):
        calls.append(prompt)
        return Result(ok, "" if ok else "cli_failed")

    monkeypatch.setattr("prpilot.orchestrator.execute_with_self_healing", fake_execute)

    if create_spec:
        _write_spec(tmp_path, spec_path)

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
    orch.test_command = ""
    orch.dry_run = False
    orch.agent_names = ["implement", "review"]
    return orch, calls


def _state(spec_path: str = SPEC_PATH) -> st.IssueState:
    return st.IssueState(branch_name="issue-1", spec_path=spec_path)


def test_all_unchecked_tasks_go_to_a_single_cli_invocation(monkeypatch, tmp_path):
    orch, calls = _orchestrator(monkeypatch, BODY, tmp_path)

    orch._implement(Issue(number=1, title="Test", body=BODY), _state(), resume=False)

    assert len(calls) == 1  # not invoked per task
    prompt = calls[0]
    assert "1. Task A" in prompt
    assert "2. Task B" in prompt
    assert "3. Task C" in prompt
    # completed tasks are not included in the numbered list of items to implement
    # (they still appear in the prompt as part of the Issue body, so we only look at the list side)
    assert re.search(r"^\s*\d+\.\s*Done task", prompt, re.M) is None


def test_success_checks_every_batched_task(monkeypatch, tmp_path):
    orch, _ = _orchestrator(monkeypatch, BODY, tmp_path)
    state = _state()

    result = orch._implement(Issue(number=1, title="Test", body=BODY), state, resume=False)

    assert st.unchecked(orch.gh.body) == []
    done, total = st.progress(orch.gh.body)
    assert (done, total) == (4, 4)
    assert state.next_action == "ai_review"  # no remaining tasks, so move to review
    assert result.action == "implemented"
    # savepoint is recorded only on the worktree Git, not on the main Git
    assert orch._test_wt.savepoints  # committed on the worktree side
    assert orch.git.ensure_worktree_calls  # the main Git only has ensure_worktree called


def test_failure_leaves_tasks_unchecked(monkeypatch, tmp_path):
    orch, _ = _orchestrator(monkeypatch, BODY, tmp_path, ok=False)
    state = _state()

    result = orch._implement(Issue(number=1, title="Test", body=BODY), state, resume=False)

    assert result.action == "failed"
    assert len(st.unchecked(orch.gh.body)) == 3  # none are checked off
    assert state.phase == "blocked"


def test_no_unchecked_tasks_skips_cli_and_goes_to_review(monkeypatch, tmp_path):
    body = "- [x] Done A\n- [x] Done B\n"
    orch, calls = _orchestrator(monkeypatch, body, tmp_path)
    orch.dry_run = True

    result = orch._implement(Issue(number=1, title="Test", body=body), _state(), resume=False)

    assert calls == []
    assert "ai_review" in result.detail


def test_empty_spec_path_blocks_without_agent(monkeypatch, tmp_path):
    orch, calls = _orchestrator(monkeypatch, BODY, tmp_path, create_spec=False)
    state = _state(spec_path="")

    result = orch._implement(Issue(number=1, title="Test", body=BODY), state, resume=False)

    assert calls == []
    assert result.action == "blocked"
    assert result.detail == "spec_missing"
    assert state.phase == "blocked"
    assert orch.gh.comments
    assert "no spec is set" in orch.gh.comments[0]


def test_missing_spec_file_in_worktree_blocks(monkeypatch, tmp_path):
    orch, calls = _orchestrator(monkeypatch, BODY, tmp_path, create_spec=False)
    state = _state()

    result = orch._implement(Issue(number=1, title="Test", body=BODY), state, resume=False)

    assert calls == []
    assert result.action == "blocked"
    assert result.detail == "spec_not_in_branch"
    assert state.phase == "blocked"
    assert orch.gh.comments
    assert "not on the branch" in orch.gh.comments[0]


def test_no_tasks_with_missing_spec_blocks_review_agent(monkeypatch, tmp_path):
    body = "- [x] Done A\n- [x] Done B\n"
    orch, calls = _orchestrator(monkeypatch, body, tmp_path, create_spec=False)
    state = _state()

    result = orch._implement(Issue(number=1, title="Test", body=body), state, resume=False)

    assert calls == []
    assert result.action == "blocked"
    assert result.detail == "spec_not_in_branch"
    assert state.phase == "blocked"


def test_no_tasks_with_spec_present_goes_to_review(monkeypatch, tmp_path):
    body = "- [x] Done A\n- [x] Done B\n"
    orch, calls = _orchestrator(monkeypatch, body, tmp_path)
    state = _state()
    monkeypatch.setattr(
        orch,
        "_create_pr",
        lambda issue, s: StepResult(issue.number, "pr_created", ""),
    )

    result = orch._implement(Issue(number=1, title="Test", body=body), state, resume=False)

    assert len(calls) == 1
    assert "review" in calls[0]
    assert result.action != "blocked"
