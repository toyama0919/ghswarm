"""Tests for the optional simplify phase and review prompt updates."""

from __future__ import annotations

from pathlib import Path

from ghswarm import state as st
from ghswarm.config import AgentConfig, RepoConfig
from ghswarm.github import Issue
from ghswarm.orchestrator import Orchestrator, StepResult

VERIFY_BLOCK = f"""{st.VERIFY_START}
verify: []
{st.VERIFY_END}"""


def _body_with_verify(base: str = "") -> str:
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
    def ensure_worktree(self, branch: str, base: str, path: str) -> str:
        return path


class FakeWorktreeGit:
    def __init__(self, cwd: str = "/tmp/worktree"):
        self.cwd = cwd
        self.savepoints: list[str] = []

    def savepoint(self, message: str) -> bool:
        self.savepoints.append(message)
        return True

    def push(self, branch: str) -> None:
        pass


class Result:
    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason
        self.output = "out"
        self.attempts = 1


def _agents(*, with_simplify: bool = False) -> dict[str, AgentConfig]:
    agents = {
        "implement": AgentConfig(name="implement", commands=["noop"]),
        "review": AgentConfig(name="review", commands=["noop"]),
    }
    if with_simplify:
        agents["simplify"] = AgentConfig(name="simplify", commands=["noop"])
    return agents


def _orchestrator(
    monkeypatch,
    body: str,
    tmp_path: Path,
    *,
    with_simplify: bool = False,
    ok: bool = True,
):
    calls: list[tuple[str, str]] = []

    def fake_execute(cfg, agent, git, verify_steps, prompt, on_question):
        calls.append((agent.name, prompt))
        return Result(ok, "" if ok else "cli_failed")

    monkeypatch.setattr("ghswarm.orchestrator.execute_with_self_healing", fake_execute)

    agents = _agents(with_simplify=with_simplify)
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(agents=agents)
    orch.gh = FakeGitHub(body)
    orch.git = FakeGit()
    wt = FakeWorktreeGit(str(tmp_path))
    orch._git_for = lambda path: wt
    orch._test_wt = wt
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.dry_run = False
    orch.agent_names = list(agents.keys())
    orch._persist = lambda issue, state: None
    orch._release_idle = lambda issue, state: None
    orch._record_busy_lease = lambda issue, state: None
    return orch, calls


def _state(**kwargs) -> st.IssueState:
    return st.IssueState(branch_name="issue-1", **kwargs)


def test_implement_completion_without_simplify_goes_to_ai_review(monkeypatch, tmp_path):
    body = _body_with_verify("- [ ] Task A\n")
    orch, _ = _orchestrator(monkeypatch, body, tmp_path)
    state = _state()
    monkeypatch.setattr(
        orch,
        "_create_pr",
        lambda issue, s: StepResult(issue.number, "pr_created", ""),
    )

    orch._implement(Issue(number=1, title="Test", body=body), state, resume=False)

    assert state.next_action == "ai_review"


def test_implement_completion_with_simplify_goes_to_simplify(monkeypatch, tmp_path):
    body = _body_with_verify("- [ ] Task A\n")
    orch, _ = _orchestrator(monkeypatch, body, tmp_path, with_simplify=True)
    state = _state()

    orch._implement(Issue(number=1, title="Test", body=body), state, resume=False)

    assert state.next_action == "simplify"


def test_no_tasks_early_path_transitions_to_simplify_when_enabled(monkeypatch, tmp_path):
    body = _body_with_verify("- [x] Done A\n")
    orch, calls = _orchestrator(monkeypatch, body, tmp_path, with_simplify=True)
    orch.dry_run = True

    result = orch._implement(Issue(number=1, title="Test", body=body), _state(), resume=False)

    assert calls == []
    assert "simplify" in result.detail


def test_simplify_disabled_next_action_falls_back_to_review(monkeypatch, tmp_path):
    body = _body_with_verify()
    state = _state(next_action="simplify")
    body_with_state = st.write_state(st.strip_state(body), state)
    orch, calls = _orchestrator(monkeypatch, body_with_state, tmp_path)
    monkeypatch.setattr(
        orch,
        "_create_pr",
        lambda issue, s: StepResult(issue.number, "pr_created", ""),
    )

    orch._step(1)

    assert any(name == "review" for name, _ in calls)


def test_simplify_success_chains_to_review(monkeypatch, tmp_path):
    body = _body_with_verify()
    orch, calls = _orchestrator(monkeypatch, body, tmp_path, with_simplify=True)
    state = _state(next_action="simplify")
    monkeypatch.setattr(
        orch,
        "_create_pr",
        lambda issue, s: StepResult(issue.number, "pr_created", ""),
    )

    result = orch._simplify(Issue(number=1, title="Test", body=body), state)

    assert state.next_action == "create_pr"
    assert [name for name, _ in calls] == ["simplify", "review"]
    assert result.action == "pr_created"


def test_review_prompt_includes_perspectives(monkeypatch, tmp_path):
    body = _body_with_verify()
    orch, calls = _orchestrator(monkeypatch, body, tmp_path)
    state = _state(next_action="ai_review")
    monkeypatch.setattr(
        orch,
        "_create_pr",
        lambda issue, s: StepResult(issue.number, "pr_created", ""),
    )

    orch._review(Issue(number=1, title="Test", body=body), state)

    prompt = calls[0][1]
    assert "Spec consistency and correctness" in prompt
    assert "Logic and control flow" in prompt
    assert "Edge cases, error handling, and test adequacy" in prompt
    assert "Fit with existing conventions" in prompt


def test_review_prompt_includes_delegation_when_simplify_enabled(monkeypatch, tmp_path):
    body = _body_with_verify()
    orch, calls = _orchestrator(monkeypatch, body, tmp_path, with_simplify=True)
    state = _state(next_action="ai_review")
    monkeypatch.setattr(
        orch,
        "_create_pr",
        lambda issue, s: StepResult(issue.number, "pr_created", ""),
    )

    orch._review(Issue(number=1, title="Test", body=body), state)

    prompt = calls[0][1]
    assert "simplify phase" in prompt
    assert "do not redo them" in prompt


def test_review_prompt_omits_delegation_when_simplify_disabled(monkeypatch, tmp_path):
    body = _body_with_verify()
    orch, calls = _orchestrator(monkeypatch, body, tmp_path)
    state = _state(next_action="ai_review")
    monkeypatch.setattr(
        orch,
        "_create_pr",
        lambda issue, s: StepResult(issue.number, "pr_created", ""),
    )

    orch._review(Issue(number=1, title="Test", body=body), state)

    prompt = calls[0][1]
    assert "simplify phase" not in prompt
