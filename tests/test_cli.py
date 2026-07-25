"""Tests for how the cli wires up target-Issue selection (gh is not launched)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ghswarm.cli as cli
from ghswarm import activity
from ghswarm.cli import (
    _build_issue_create_args,
    _filter_missing_paths,
    _phase_kind,
    _process_cycle,
    _run_parallel_cycle,
    _run_to_completion,
    _select_repo_for_config,
    _select_repos,
    _select_single_repo_by_cwd,
    _target_issues,
)
from ghswarm.config import (
    AgentConfig,
    AppConfig,
    ConfigError,
    LabelConfig,
    RepoConfig,
    TargetFilter,
)
from ghswarm.github import GitHubError, Issue
from ghswarm.orchestrator import StepResult

# A real, always-existing directory for RepoConfig.path defaults, distinct from any
# pytest tmp_path (which lives under the system temp dir) so cwd-matching tests aren't
# affected by it.
_REAL_DIR = str(Path(__file__).resolve().parent)


class FakeGitHub:
    def __init__(self, issues=None):
        self.last_kwargs: dict | None = None
        self._issues = issues or []
        self._issue_by_number: dict[int, Issue] = {}

    def list_open_issues(self, **kwargs):
        self.last_kwargs = kwargs
        return list(self._issues)

    def get_issue(self, number: int) -> Issue:
        if number in self._issue_by_number:
            return self._issue_by_number[number]
        for issue in self._issues:
            if issue.number == number:
                return issue
        raise KeyError(number)


class FakeOrch:
    """Dummy Orchestrator that just records the Issue numbers it processed."""

    def __init__(self, gh, results=None):
        self.gh = gh
        self.processed: list[int] = []
        # allow injecting a StepResult.action per number (default is "advanced")
        self._results = results or {}

    def process(self, number: int, **kwargs) -> StepResult:
        self.processed.append(number)
        action = self._results.get(number, "advanced")
        return StepResult(number, action, "")


class SequenceOrch:
    """Dummy where process advances a transition sequence and get_issue returns the current shared state."""

    def __init__(self, gh: FakeGitHub, sequences: dict[int, list[tuple[str, str]]]):
        self.gh = gh
        self._sequences = sequences
        self._indices: dict[int, int] = {}
        self.process_calls: list[dict] = []
        for number, steps in sequences.items():
            if steps:
                _, next_action = steps[0]
                gh._issue_by_number[number] = _issue(number, next_action)

    def process(self, number: int, *, force=False, resume=False) -> StepResult:
        self.process_calls.append({"number": number, "force": force, "resume": resume})
        idx = self._indices.get(number, 0)
        steps = self._sequences[number]
        action, next_action = steps[min(idx, len(steps) - 1)]
        self._indices[number] = idx + 1
        self.gh._issue_by_number[number] = _issue(number, next_action)
        return StepResult(number, action, f"detail-{action}")


def _issue(number: int, next_action: str, *, labels: list[str] | None = None) -> Issue:
    body = f'body\n\n<!-- GHSWARM_STATE_START\n{{"next_action": "{next_action}"}}\nGHSWARM_STATE_END -->'
    issue_labels = labels if labels is not None else ["status: idle"]
    return Issue(number=number, title=f"issue {number}", body=body, labels=issue_labels)


def _cfg(**target_overrides) -> AppConfig:
    repo = RepoConfig(
        name="test",
        repo="owner/repo",
        path=_REAL_DIR,
        agents={
            "implement": AgentConfig(name="implement", commands=["echo"]),
            "review": AgentConfig(name="review", commands=["echo"]),
        },
        target=TargetFilter(**target_overrides),
    )
    return AppConfig(repositories={"test": repo})


def _multi_app() -> AppConfig:
    repos = {}
    for alias in ("a", "b"):
        repos[alias] = RepoConfig(
            name=alias,
            repo=f"owner/{alias}",
            path=_REAL_DIR,
            agents={
                "implement": AgentConfig(name="implement", commands=["echo"]),
                "review": AgentConfig(name="review", commands=["echo"]),
            },
        )
    return AppConfig(repositories=repos, max_parallel_repos=2)


def test_target_issues_passes_all_filter_fields():
    gh = FakeGitHub()
    cfg = _cfg(labels=["pm-agent"], assignee="@me", milestone="v1.0").repositories["test"]
    _target_issues(cfg, gh)
    assert gh.last_kwargs == {
        "labels": ["pm-agent"],
        "assignee": "@me",
        "milestone": "v1.0",
    }


def test_target_issues_passes_none_labels_when_empty():
    gh = FakeGitHub()
    cfg = _cfg().repositories["test"]
    _target_issues(cfg, gh)
    assert gh.last_kwargs == {"labels": None, "assignee": "", "milestone": ""}


def test_target_issues_sorted_oldest_first():
    gh = FakeGitHub([_issue(5, "start"), _issue(2, "start"), _issue(9, "start")])
    numbers = [i.number for i in _target_issues(_cfg().repositories["test"], gh)]
    assert numbers == [2, 5, 9]


def test_target_issues_excludes_issues_without_status_label():
    gh = FakeGitHub(
        [
            _issue(1, "start"),
            _issue(2, "start", labels=[]),
            _issue(3, "start", labels=["pm-agent"]),
            _issue(4, "start", labels=["status: blocked"]),
        ]
    )
    numbers = [i.number for i in _target_issues(_cfg().repositories["test"], gh)]
    assert numbers == [1, 4]


def test_target_issues_respects_custom_label_config():
    cfg = _cfg().repositories["test"]
    cfg.labels = LabelConfig(
        idle="state: idle",
        blocked="state: blocked",
        completed="state: completed",
        busy_prefix="state: busy-",
    )
    gh = FakeGitHub(
        [
            _issue(1, "start", labels=["state: idle"]),
            _issue(2, "start", labels=["status: idle"]),
            _issue(3, "start", labels=["state: busy-implement"]),
            _issue(4, "start", labels=[]),
        ]
    )
    numbers = [i.number for i in _target_issues(cfg, gh)]
    assert numbers == [1, 3]


def test_phase_kind_classification():
    cfg = _cfg().repositories["test"]
    assert _phase_kind(cfg, _issue(1, "start")) == "active"
    assert _phase_kind(cfg, _issue(1, "implement")) == "active"
    assert _phase_kind(cfg, _issue(1, "ai_review")) == "active"
    assert _phase_kind(cfg, _issue(1, "create_pr")) == "active"
    # verify_merge is post-merge CI polling, so it is waiting (checked every cycle).
    assert _phase_kind(cfg, _issue(1, "verify_merge")) == "waiting"
    assert _phase_kind(cfg, _issue(1, "wait_ci")) == "waiting"
    assert _phase_kind(cfg, _issue(1, "wait_for_clarification")) == "waiting"
    assert _phase_kind(cfg, _issue(1, "done")) == "done"


def test_cycle_processes_only_one_active_issue():
    # If both #1 and #2 are in development (active), advance only the older #1 and defer #2.
    gh = FakeGitHub([_issue(2, "implement"), _issue(1, "implement")])
    orch = FakeOrch(gh)
    _process_cycle(_cfg().repositories["test"], orch)
    assert orch.processed == [1]


def test_cycle_advances_next_issue_while_oldest_waits():
    # While #1 is waiting on CI (pending=skipped), #2's development can be advanced (behavior B).
    gh = FakeGitHub([_issue(1, "wait_ci"), _issue(2, "implement")])
    orch = FakeOrch(gh, results={1: "skipped"})
    _process_cycle(_cfg().repositories["test"], orch)
    assert orch.processed == [1, 2]


def test_cycle_waiting_that_does_real_work_consumes_slot():
    # If #1's wait_ci runs real work (non-skipped), such as addressing review, count it as
    # the one slot for that cycle and do not start the following active #2 (keep concurrent development at one).
    gh = FakeGitHub([_issue(1, "wait_ci"), _issue(2, "implement")])
    orch = FakeOrch(gh, results={1: "review_addressed"})
    _process_cycle(_cfg().repositories["test"], orch)
    assert orch.processed == [1]


def test_cycle_polls_waiting_after_active_taken():
    # Even after advancing active #1 first, the following waiting #2 (CI wait) is checked every cycle.
    gh = FakeGitHub([_issue(1, "implement"), _issue(2, "verify_merge")])
    orch = FakeOrch(gh, results={2: "skipped"})
    _process_cycle(_cfg().repositories["test"], orch)
    assert orch.processed == [1, 2]


def test_cycle_skips_done_and_holds_active_slot_for_oldest():
    # Skip done. The active slot is taken by the oldest one (#3), and #5 is deferred.
    gh = FakeGitHub([_issue(1, "done"), _issue(3, "ai_review"), _issue(5, "implement")])
    orch = FakeOrch(gh)
    _process_cycle(_cfg().repositories["test"], orch)
    assert orch.processed == [3]


def test_cycle_skipped_active_does_not_consume_slot():
    # If the first active is skipped (e.g. locked), do not consume the slot and try the next active too.
    gh = FakeGitHub([_issue(1, "implement"), _issue(2, "implement")])
    orch = FakeOrch(gh, results={1: "skipped"})
    _process_cycle(_cfg().repositories["test"], orch)
    assert orch.processed == [1, 2]


def test_run_to_completion_reaches_done():
    gh = FakeGitHub()
    orch = SequenceOrch(
        gh,
        {
            42: [
                ("implemented", "ai_review"),
                ("reviewed", "create_pr"),
                ("completed", "done"),
            ],
        },
    )
    rc = _run_to_completion(_cfg().repositories["test"], orch, 42)
    assert rc == 0
    assert len(orch.process_calls) == 3


def test_run_to_completion_blocked_with_clarification_rc0():
    gh = FakeGitHub()
    orch = SequenceOrch(gh, {42: [("blocked", "wait_for_clarification")]})
    rc = _run_to_completion(_cfg().repositories["test"], orch, 42)
    assert rc == 0
    assert len(orch.process_calls) == 1


def test_run_to_completion_blocked_with_wait_ci_rc1():
    gh = FakeGitHub()
    orch = SequenceOrch(gh, {42: [("blocked", "wait_ci")]})
    rc = _run_to_completion(_cfg().repositories["test"], orch, 42)
    assert rc == 1
    assert len(orch.process_calls) == 1


def test_run_to_completion_failed_rc1():
    gh = FakeGitHub()
    orch = SequenceOrch(gh, {42: [("failed", "implement")]})
    rc = _run_to_completion(_cfg().repositories["test"], orch, 42)
    assert rc == 1
    assert len(orch.process_calls) == 1


def test_run_to_completion_verify_merge_polls_until_done():
    gh = FakeGitHub()
    orch = SequenceOrch(
        gh,
        {
            42: [
                ("skipped", "verify_merge"),
                ("merged", "done"),
            ],
        },
    )
    sleeps: list[float] = []
    rc = _run_to_completion(
        _cfg().repositories["test"], orch, 42, sleep_fn=lambda sec: sleeps.append(sec)
    )
    assert rc == 0
    assert len(orch.process_calls) == 2
    assert sleeps == [60]


def test_run_to_completion_wait_ci_polls_until_done():
    gh = FakeGitHub()
    orch = SequenceOrch(
        gh,
        {
            42: [
                ("skipped", "wait_ci"),
                ("skipped", "wait_ci"),
                ("merged", "done"),
            ],
        },
    )
    sleeps: list[float] = []
    rc = _run_to_completion(
        _cfg().repositories["test"], orch, 42, sleep_fn=lambda sec: sleeps.append(sec)
    )
    assert rc == 0
    assert len(orch.process_calls) == 3
    assert sleeps == [60, 60]


def test_run_to_completion_skipped_non_waiting_rc0():
    gh = FakeGitHub()
    orch = SequenceOrch(gh, {42: [("skipped", "implement")]})
    rc = _run_to_completion(_cfg().repositories["test"], orch, 42)
    assert rc == 0
    assert len(orch.process_calls) == 1


def test_run_to_completion_max_iterations_rc1():
    gh = FakeGitHub()
    orch = SequenceOrch(
        gh,
        {42: [("skipped", "wait_ci")]},
    )
    rc = _run_to_completion(
        _cfg().repositories["test"],
        orch,
        42,
        sleep_fn=lambda _sec: None,
    )
    assert rc == 1
    assert len(orch.process_calls) == cli._RUN_MAX_ITERATIONS


def test_run_to_completion_resume_passed_to_first_process_only():
    gh = FakeGitHub()
    orch = SequenceOrch(
        gh,
        {
            42: [
                ("implemented", "ai_review"),
                ("reviewed", "done"),
            ],
        },
    )
    _run_to_completion(_cfg().repositories["test"], orch, 42, resume=True)
    assert orch.process_calls[0]["resume"] is True
    assert orch.process_calls[1]["resume"] is False


def _run_args(**overrides):
    defaults = {
        "config": None,
        "dry_run": False,
        "force": False,
        "resume": False,
        "step": False,
        "issues": ["42"],
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_run_step_mode_single_process(monkeypatch):
    calls: list[tuple] = []

    class MockOrch:
        def process(self, number, *, force=False, resume=False):
            calls.append((number, force, resume))
            return StepResult(number, "implemented", "")

    monkeypatch.setattr(cli, "_load", lambda _args: _cfg())
    monkeypatch.setattr(cli, "Orchestrator", lambda _cfg, **kw: MockOrch())

    rc = cli.cmd_run(_run_args(step=True, repos=["test"]))
    assert rc == 0
    assert len(calls) == 1


def test_cmd_run_dry_run_single_process(monkeypatch):
    calls: list[int] = []

    class MockOrch:
        def process(self, number, *, force=False, resume=False):
            calls.append(number)
            return StepResult(number, "implemented", "")

    monkeypatch.setattr(cli, "_load", lambda _args: _cfg())
    monkeypatch.setattr(cli, "Orchestrator", lambda _cfg, **kw: MockOrch())

    rc = cli.cmd_run(_run_args(dry_run=True, repos=["test"]))
    assert rc == 0
    assert len(calls) == 1


def test_cmd_run_multiple_issues_aggregates_rc(monkeypatch):
    completed: list[int] = []

    def mock_run(_cfg, _orch, number, *, force=False, resume=False, sleep_fn=..., repo_log=None):
        completed.append(number)
        return 1 if number == 1 else 0

    monkeypatch.setattr(cli, "_load", lambda _args: _cfg())
    monkeypatch.setattr(cli, "Orchestrator", MagicMock())
    monkeypatch.setattr(cli, "_run_to_completion", mock_run)

    rc = cli.cmd_run(_run_args(issues=["1", "2"], repos=["test"]))
    assert rc == 1
    assert completed == [1, 2]


def test_cmd_run_resume_passed_to_run_to_completion(monkeypatch):
    captured: list[bool] = []

    def mock_run(_cfg, _orch, number, *, force=False, resume=False, sleep_fn=..., repo_log=None):
        captured.append(resume)
        return 0

    monkeypatch.setattr(cli, "_load", lambda _args: _cfg())
    monkeypatch.setattr(cli, "Orchestrator", MagicMock())
    monkeypatch.setattr(cli, "_run_to_completion", mock_run)

    cli.cmd_run(_run_args(resume=True, repos=["test"]))
    assert captured == [True]


def test_run_help_shows_step_and_completion():
    parser = cli.build_parser()
    run_parser = parser._subparsers._group_actions[0].choices["run"]
    help_text = run_parser.format_help()
    assert "--step" in help_text
    assert "completion" in help_text
    assert "--repo" in help_text
    assert "cwd" in help_text


def test_select_repos_unknown_alias_raises():
    with pytest.raises(ConfigError, match="Unknown repository alias"):
        _select_repos(_multi_app(), ["a", "missing"])


def test_select_repos_all_when_omitted():
    repos = _select_repos(_multi_app(), None)
    assert {r.name for r in repos} == {"a", "b"}


def test_select_repos_deduplicates_aliases():
    repos = _select_repos(_multi_app(), ["a", "a", "b"])
    assert [r.name for r in repos] == ["a", "b"]


def test_filter_missing_paths_excludes_missing_and_logs(tmp_path, caplog):
    existing = tmp_path / "exists"
    existing.mkdir()
    missing = tmp_path / "does-not-exist"

    app = _multi_app()
    app.repositories["a"].path = str(existing)
    app.repositories["b"].path = str(missing)

    with caplog.at_level("WARNING"):
        kept = _filter_missing_paths([app.repositories["a"], app.repositories["b"]])

    assert [r.name for r in kept] == ["a"]
    assert any(str(missing) in rec.message for rec in caplog.records)


def test_filter_missing_paths_preserves_order():
    app = _multi_app()
    kept = _filter_missing_paths([app.repositories["b"], app.repositories["a"]])
    assert [r.name for r in kept] == ["b", "a"]


def test_select_single_repo_by_cwd_no_match_with_multiple(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    with pytest.raises(ConfigError, match="-r/--repo"):
        _select_single_repo_by_cwd(_multi_app(), None)


def test_select_single_repo_by_cwd_no_match_even_with_one(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    with pytest.raises(ConfigError, match="-r/--repo"):
        _select_single_repo_by_cwd(_cfg(), None)


def test_cmd_run_without_r_errors_when_no_cwd_match(monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    rc = cli.cmd_run(_run_args())
    assert rc == 2


def test_cmd_run_selects_repo_by_cwd_when_r_omitted(monkeypatch, tmp_path):
    repo_path = tmp_path / "myrepo"
    repo_path.mkdir()
    monkeypatch.chdir(repo_path)
    created: list[str] = []

    class MockOrch:
        def process(self, number, *, force=False, resume=False):
            return StepResult(number, "implemented", "")

    def mock_orch(cfg, **kw):
        created.append(cfg.name)
        return MockOrch()

    app = AppConfig(
        repositories={
            "test": RepoConfig(
                name="test",
                repo="owner/repo",
                path=str(repo_path),
                agents=_cfg().repositories["test"].agents,
            )
        }
    )
    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli, "Orchestrator", mock_orch)
    monkeypatch.setattr(cli, "_run_to_completion", lambda *a, **k: 0)

    rc = cli.cmd_run(_run_args(step=True))
    assert rc == 0
    assert created == ["test"]


def test_cmd_run_no_cwd_match_errors_even_with_single_repo(monkeypatch, tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    app = AppConfig(
        repositories={
            "test": RepoConfig(
                name="test",
                repo="owner/repo",
                path=str(repo_path),
                agents=_cfg().repositories["test"].agents,
            )
        }
    )
    monkeypatch.setattr(cli, "_load", lambda _args: app)
    rc = cli.cmd_run(_run_args())
    assert rc == 2


def test_cmd_run_selects_longest_cwd_match(monkeypatch, tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    child = parent / "myrepo"
    child.mkdir()
    monkeypatch.chdir(child)
    created: list[str] = []

    class MockOrch:
        def process(self, number, *, force=False, resume=False):
            return StepResult(number, "implemented", "")

    def mock_orch(cfg, **kw):
        created.append(cfg.name)
        return MockOrch()

    agents = _cfg().repositories["test"].agents
    app = AppConfig(
        repositories={
            "parent": RepoConfig(
                name="parent",
                repo="owner/parent",
                path=str(parent),
                agents=agents,
            ),
            "child": RepoConfig(
                name="child",
                repo="owner/child",
                path=str(child),
                agents=agents,
            ),
        }
    )
    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli, "Orchestrator", mock_orch)
    monkeypatch.setattr(cli, "_run_to_completion", lambda *a, **k: 0)

    rc = cli.cmd_run(_run_args(step=True))
    assert rc == 0
    assert created == ["child"]


def test_cmd_run_with_repo_alias(monkeypatch):
    created: list[str] = []

    class MockOrch:
        def process(self, number, *, force=False, resume=False):
            return StepResult(number, "implemented", "")

    def mock_orch(cfg, **kw):
        created.append(cfg.name)
        return MockOrch()

    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    monkeypatch.setattr(cli, "Orchestrator", mock_orch)
    monkeypatch.setattr(cli, "_run_to_completion", lambda *a, **k: 0)

    rc = cli.cmd_run(_run_args(repos=["b"]))
    assert rc == 0
    assert created == ["b"]


def test_cmd_run_duplicate_repo_alias_is_deduped(monkeypatch):
    created: list[str] = []

    class MockOrch:
        def process(self, number, *, force=False, resume=False):
            return StepResult(number, "implemented", "")

    def mock_orch(cfg, **kw):
        created.append(cfg.name)
        return MockOrch()

    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    monkeypatch.setattr(cli, "Orchestrator", mock_orch)
    monkeypatch.setattr(cli, "_run_to_completion", lambda *a, **k: 0)

    rc = cli.cmd_run(_run_args(repos=["a", "a"]))
    assert rc == 0
    assert created == ["a"]


class SyncExecutor:
    """Test executor that runs synchronously in place of ProcessPoolExecutor."""

    def __init__(self, max_workers: int):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        result = fn(*args, **kwargs)
        fut: Future = Future()
        fut.set_result(result)
        return fut


def test_run_parallel_cycle_passes_daemon_pid(monkeypatch):
    captured: list[int | None] = []

    def fake_worker(repo_cfg, *, dry_run=False, verbose=False, daemon_pid=None):
        captured.append(daemon_pid)

    monkeypatch.setattr(cli, "run_process_cycle_worker", fake_worker)
    app = _multi_app()
    repos = list(app.repositories.values())

    _run_parallel_cycle(
        repos,
        max_workers=2,
        dry_run=False,
        verbose=False,
        daemon_pid=4242,
        executor_factory=lambda n: SyncExecutor(n),
    )
    assert captured == [4242, 4242]


def test_run_parallel_cycle_invokes_all_repos(monkeypatch):
    calls: list[str] = []

    def fake_worker(repo_cfg, *, dry_run=False, verbose=False, daemon_pid=None):
        calls.append(repo_cfg.name)

    monkeypatch.setattr(cli, "run_process_cycle_worker", fake_worker)
    app = _multi_app()
    repos = list(app.repositories.values())

    _run_parallel_cycle(
        repos,
        max_workers=2,
        dry_run=False,
        verbose=False,
        executor_factory=lambda n: SyncExecutor(n),
    )
    assert sorted(calls) == ["a", "b"]


def test_run_parallel_cycle_continues_after_worker_error(monkeypatch):
    calls: list[str] = []

    def boom_orch(cfg, **kw):
        calls.append(cfg.name)
        if cfg.name == "a":
            raise RuntimeError("boom")
        return MagicMock()

    monkeypatch.setattr(cli, "Orchestrator", boom_orch)
    monkeypatch.setattr(cli, "_process_cycle", lambda *a, **k: None)
    monkeypatch.setattr(cli, "setup_logging", lambda verbose=False: None)

    app = _multi_app()
    repos = list(app.repositories.values())

    _run_parallel_cycle(
        repos,
        max_workers=2,
        dry_run=False,
        verbose=False,
        executor_factory=lambda n: SyncExecutor(n),
    )
    assert sorted(calls) == ["a", "b"]


def test_cmd_loop_once_uses_parallel_cycle(monkeypatch):
    cycles: list[list[str]] = []

    def fake_parallel(repos, **kwargs):
        cycles.append([r.name for r in repos])

    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    monkeypatch.setattr(cli, "_run_parallel_cycle", fake_parallel)

    rc = cli.cmd_loop(argparse.Namespace(repos=None, once=True, dry_run=False, verbose=False))
    assert rc == 0
    assert cycles == [["a", "b"]]


def test_cmd_loop_once_skips_repo_with_missing_path(monkeypatch, tmp_path):
    cycles: list[list[str]] = []

    def fake_parallel(repos, **kwargs):
        cycles.append([r.name for r in repos])

    app = _multi_app()
    app.repositories["b"].path = str(tmp_path / "does-not-exist")

    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli, "_run_parallel_cycle", fake_parallel)

    rc = cli.cmd_loop(argparse.Namespace(repos=None, once=True, dry_run=False, verbose=False))
    assert rc == 0
    assert cycles == [["a"]]


def test_cmd_loop_once_all_missing_paths_reports_no_target_repos(monkeypatch, tmp_path, caplog):
    cycles: list[list[str]] = []

    def fake_parallel(repos, **kwargs):
        cycles.append([r.name for r in repos])

    app = _multi_app()
    app.repositories["a"].path = str(tmp_path / "missing-a")
    app.repositories["b"].path = str(tmp_path / "missing-b")

    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli, "_run_parallel_cycle", fake_parallel)

    with caplog.at_level("WARNING"):
        rc = cli.cmd_loop(argparse.Namespace(repos=None, once=True, dry_run=False, verbose=False))
    assert rc == 0
    assert cycles == []
    assert any("No target repositories" in rec.message for rec in caplog.records)


def test_cmd_loop_idle_stop_interrupts_wait(monkeypatch):
    cycles: list[int] = []
    waits: list[int] = []

    def fake_parallel(*_args, **_kwargs):
        cycles.append(1)

    def fake_wait(timeout: float) -> bool:
        waits.append(int(timeout))
        cli._stop_event.set()
        return True

    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    monkeypatch.setattr(cli, "_run_parallel_cycle", fake_parallel)
    monkeypatch.setattr(cli._stop_event, "wait", fake_wait)

    rc = cli.cmd_loop(argparse.Namespace(repos=None, once=False, dry_run=False, verbose=False))
    assert rc == 0
    assert cycles == [1]
    assert waits == [60]


def test_cmd_loop_stop_waits_for_cycle_completion(monkeypatch):
    started: list[int] = []
    finished: list[int] = []

    def fake_parallel(*_args, **_kwargs):
        started.append(1)
        cli._stop_event.set()
        finished.append(1)

    def fake_wait(_timeout: float) -> bool:
        return True

    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    monkeypatch.setattr(cli, "_run_parallel_cycle", fake_parallel)
    monkeypatch.setattr(cli._stop_event, "wait", fake_wait)

    rc = cli.cmd_loop(argparse.Namespace(repos=None, once=False, dry_run=False, verbose=False))
    assert rc == 0
    assert started == [1]
    assert finished == [1]


def test_cmd_loop_uses_min_poll_interval(monkeypatch):
    waited: list[int] = []

    def fake_wait(seconds: float) -> bool:
        waited.append(int(seconds))
        raise KeyboardInterrupt

    app = _multi_app()
    app.repositories["a"].poll_interval = 120
    app.repositories["b"].poll_interval = 30

    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli, "_run_parallel_cycle", lambda *a, **k: None)
    monkeypatch.setattr(cli._stop_event, "wait", fake_wait)

    with pytest.raises(KeyboardInterrupt):
        cli.cmd_loop(argparse.Namespace(repos=None, once=False, dry_run=False, verbose=False))
    assert waited == [30]


def test_cmd_loop_stop_ctrl_c_shows_background_message(monkeypatch, tmp_path, capsys):
    app = _multi_app()
    app.daemon_pid = str(tmp_path / "ghswarm.pid")

    def fake_wait(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli.daemon, "read_pid", lambda _path: 4242)
    monkeypatch.setattr(cli.daemon, "stop_daemon", lambda _path: True)
    monkeypatch.setattr(cli, "_wait_for_daemon_stop", fake_wait)

    rc = cli.cmd_loop(
        argparse.Namespace(repos=None, once=False, dry_run=False, verbose=False, stop=True)
    )
    assert rc == 0
    assert "in the background" in capsys.readouterr().out


def test_cmd_loop_stop_waits_until_daemon_gone(monkeypatch, tmp_path, capsys):
    pid_path = tmp_path / "ghswarm.pid"
    pid_path.write_text("4242", encoding="utf-8")
    app = _multi_app()
    app.daemon_pid = str(pid_path)

    stop_calls: list[str] = []
    sleep_calls: list[float] = []
    alive_checks = [0]

    def fake_stop(path: str) -> bool:
        stop_calls.append(path)
        return True

    def fake_is_alive(_pid: int) -> bool:
        alive_checks[0] += 1
        return alive_checks[0] < 3

    def fake_sleep(interval: float) -> None:
        sleep_calls.append(interval)

    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli.daemon, "read_pid", lambda _path: 4242)
    monkeypatch.setattr(cli.daemon, "stop_daemon", fake_stop)

    rc = cli.cmd_loop(
        argparse.Namespace(
            repos=None,
            once=False,
            dry_run=False,
            verbose=False,
            stop=True,
            _stop_sleep_fn=fake_sleep,
            _stop_is_alive_fn=fake_is_alive,
        )
    )
    assert rc == 0
    assert stop_calls == [str(pid_path)]
    assert sleep_calls == [0.5, 0.5]
    assert "✔ ghswarm daemon stopped (pid=4242)" in capsys.readouterr().out


def test_cmd_loop_stop_shows_running_repos(monkeypatch, tmp_path, capsys):
    pid_path = tmp_path / "ghswarm.pid"
    pid_path.write_text("4242", encoding="utf-8")
    app = _multi_app()
    app.daemon_pid = str(pid_path)
    app.repositories["a"].activity_dir = str(tmp_path / "activity")

    alive_checks = [0]

    def fake_is_alive(_pid: int) -> bool:
        alive_checks[0] += 1
        return alive_checks[0] < 2

    def fake_read(_activity_dir, *, daemon_pid=None, is_alive_fn=None):
        return [
            activity.ActivityEntry(
                repo="toyama0919/ghswarm",
                issue=58,
                phase="ai_review",
                since="",
                pid=4242,
            )
        ]

    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli.daemon, "read_pid", lambda _path: 4242)
    monkeypatch.setattr(cli.daemon, "stop_daemon", lambda _path: True)

    rc = cli.cmd_loop(
        argparse.Namespace(
            repos=None,
            once=False,
            dry_run=False,
            verbose=False,
            stop=True,
            _stop_sleep_fn=lambda _interval: None,
            _stop_is_alive_fn=fake_is_alive,
            _stop_read_activities_fn=fake_read,
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Waiting for in-flight commands" in out
    assert "toyama0919/ghswarm" in out
    assert "#58" in out
    assert "ai_review" in out
    assert "✔ ghswarm daemon stopped (pid=4242)" in out


def test_cmd_loop_stop_no_daemon(monkeypatch, tmp_path):
    app = _multi_app()
    app.daemon_pid = str(tmp_path / "missing.pid")
    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli.daemon, "read_pid", lambda _path: None)

    rc = cli.cmd_loop(
        argparse.Namespace(repos=None, once=False, dry_run=False, verbose=False, stop=True)
    )
    assert rc == 0


def test_cmd_loop_daemon_rejects_double_start(monkeypatch):
    app = _multi_app()
    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli.daemon, "already_running", lambda _path: 9999)

    rc = cli.cmd_loop(
        argparse.Namespace(repos=None, once=False, dry_run=False, verbose=False, daemon=True)
    )
    assert rc == 1


def test_cmd_loop_daemon_and_once_error(monkeypatch):
    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    rc = cli.cmd_loop(
        argparse.Namespace(repos=None, once=True, dry_run=False, verbose=False, daemon=True)
    )
    assert rc == 1


def test_cmd_loop_daemon_and_stop_error(monkeypatch):
    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    rc = cli.cmd_loop(
        argparse.Namespace(
            repos=None, once=False, dry_run=False, verbose=False, daemon=True, stop=True
        )
    )
    assert rc == 1


def test_cmd_loop_daemon_graceful_stop(monkeypatch, tmp_path):
    pid_path = tmp_path / "ghswarm.pid"
    app = _multi_app()
    app.daemon_pid = str(pid_path)
    remove_calls: list[str] = []

    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli.daemon, "already_running", lambda _path: None)
    monkeypatch.setattr(cli.daemon, "dated_log_path", lambda base, date: f"{base}-{date}.log")
    monkeypatch.setattr(cli.daemon, "daemonize", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_run_parallel_cycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.daemon, "remove_pid", lambda path: remove_calls.append(path))

    def fake_wait(_seconds: int) -> bool:
        cli._stop_event.set()
        return True

    monkeypatch.setattr(cli._stop_event, "wait", fake_wait)

    rc = cli.cmd_loop(
        argparse.Namespace(
            repos=None,
            once=False,
            dry_run=False,
            verbose=False,
            daemon=True,
            _executor_factory=lambda n: SyncExecutor(n),
        )
    )
    assert rc == 0
    assert remove_calls == [str(pid_path)]


def test_cmd_loop_restart_stops_then_starts_daemon(monkeypatch, tmp_path):
    pid_path = tmp_path / "ghswarm.pid"
    pid_path.write_text("4242", encoding="utf-8")
    app = _multi_app()
    app.daemon_pid = str(pid_path)

    alive_checks = [0]
    daemonize_calls: list[tuple] = []
    remove_calls: list[str] = []

    def fake_is_alive(_pid: int) -> bool:
        alive_checks[0] += 1
        return alive_checks[0] < 3

    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli.daemon, "read_pid", lambda _path: 4242)
    monkeypatch.setattr(cli.daemon, "stop_daemon", lambda _path: True)
    monkeypatch.setattr(cli.daemon, "already_running", lambda _path: None)
    monkeypatch.setattr(cli.daemon, "dated_log_path", lambda base, date: f"{base}-{date}.log")
    monkeypatch.setattr(
        cli.daemon,
        "daemonize",
        lambda *args, **kwargs: daemonize_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(cli, "_run_parallel_cycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.daemon, "remove_pid", lambda path: remove_calls.append(path))

    def fake_wait(_seconds: int) -> bool:
        cli._stop_event.set()
        return True

    monkeypatch.setattr(cli._stop_event, "wait", fake_wait)

    rc = cli.cmd_loop(
        argparse.Namespace(
            repos=None,
            once=False,
            dry_run=False,
            verbose=False,
            restart=True,
            _stop_sleep_fn=lambda _interval: None,
            _stop_is_alive_fn=fake_is_alive,
            _executor_factory=lambda n: SyncExecutor(n),
        )
    )
    assert rc == 0
    assert alive_checks[0] >= 2
    assert len(daemonize_calls) == 1
    assert remove_calls == [str(pid_path)]


def test_cmd_loop_restart_start_when_no_daemon(monkeypatch, tmp_path):
    app = _multi_app()
    app.daemon_pid = str(tmp_path / "missing.pid")
    daemonize_calls: list[int] = []

    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli.daemon, "read_pid", lambda _path: None)
    monkeypatch.setattr(cli.daemon, "already_running", lambda _path: None)
    monkeypatch.setattr(cli.daemon, "dated_log_path", lambda base, date: f"{base}-{date}.log")
    monkeypatch.setattr(cli.daemon, "daemonize", lambda *args, **kwargs: daemonize_calls.append(1))
    monkeypatch.setattr(cli, "_run_parallel_cycle", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.daemon, "remove_pid", lambda _path: None)

    def fake_wait(_seconds: int) -> bool:
        cli._stop_event.set()
        return True

    monkeypatch.setattr(cli._stop_event, "wait", fake_wait)

    rc = cli.cmd_loop(
        argparse.Namespace(
            repos=None,
            once=False,
            dry_run=False,
            verbose=False,
            restart=True,
            _executor_factory=lambda n: SyncExecutor(n),
        )
    )
    assert rc == 0
    assert daemonize_calls == [1]


def test_cmd_loop_restart_ctrl_c_returns_130(monkeypatch, tmp_path):
    app = _multi_app()
    app.daemon_pid = str(tmp_path / "ghswarm.pid")
    daemonize_calls: list[int] = []

    def fake_sleep(_interval: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli.daemon, "read_pid", lambda _path: 4242)
    monkeypatch.setattr(cli.daemon, "stop_daemon", lambda _path: True)
    monkeypatch.setattr(cli.daemon, "daemonize", lambda *args, **kwargs: daemonize_calls.append(1))

    rc = cli.cmd_loop(
        argparse.Namespace(
            repos=None,
            once=False,
            dry_run=False,
            verbose=False,
            restart=True,
            _stop_sleep_fn=fake_sleep,
            _stop_is_alive_fn=lambda _pid: True,
        )
    )
    assert rc == 130
    assert daemonize_calls == []


def test_cmd_loop_restart_and_stop_error(monkeypatch):
    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    rc = cli.cmd_loop(
        argparse.Namespace(
            repos=None,
            once=False,
            dry_run=False,
            verbose=False,
            restart=True,
            stop=True,
        )
    )
    assert rc == 1


def test_cmd_loop_restart_and_once_error(monkeypatch):
    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    rc = cli.cmd_loop(
        argparse.Namespace(
            repos=None,
            once=True,
            dry_run=False,
            verbose=False,
            restart=True,
        )
    )
    assert rc == 1


def test_build_issue_create_args_idle_only():
    cfg = _cfg().repositories["test"]
    assert _build_issue_create_args(cfg) == ["--label", "status: idle"]


def test_build_issue_create_args_with_target():
    cfg = _cfg(labels=["pm-agent"], assignee="@me", milestone="v1.0").repositories["test"]
    assert _build_issue_create_args(cfg) == [
        "--label",
        "status: idle",
        "--label",
        "pm-agent",
        "--assignee",
        "@me",
        "--milestone",
        "v1.0",
    ]


def test_build_issue_create_args_deduplicates_labels():
    cfg = _cfg(labels=["status: idle", "pm-agent"]).repositories["test"]
    args = _build_issue_create_args(cfg)
    label_values = [args[i + 1] for i in range(0, len(args), 2) if args[i] == "--label"]
    assert label_values == ["status: idle", "pm-agent"]


def test_select_repo_for_config_by_alias():
    cfg = _select_repo_for_config(_multi_app(), "b")
    assert cfg.name == "b"


def test_select_repo_for_config_unknown_alias():
    with pytest.raises(ConfigError, match="Unknown repository alias"):
        _select_repo_for_config(_multi_app(), "missing")


def test_select_repo_for_config_cwd_match(tmp_path, monkeypatch):
    repo_path = tmp_path / "myrepo"
    repo_path.mkdir()
    monkeypatch.chdir(repo_path)
    app = AppConfig(
        repositories={
            "test": RepoConfig(
                name="test",
                repo="owner/repo",
                path=str(repo_path),
                agents=_cfg().repositories["test"].agents,
            )
        }
    )
    cfg = _select_repo_for_config(app, None)
    assert cfg.name == "test"


def test_select_repo_for_config_longest_match(tmp_path, monkeypatch):
    parent = tmp_path / "projects"
    parent.mkdir()
    child = parent / "myrepo"
    child.mkdir()
    monkeypatch.chdir(child)
    agents = _cfg().repositories["test"].agents
    app = AppConfig(
        repositories={
            "parent": RepoConfig(
                name="parent",
                repo="owner/parent",
                path=str(parent),
                agents=agents,
            ),
            "child": RepoConfig(
                name="child",
                repo="owner/child",
                path=str(child),
                agents=agents,
            ),
        }
    )
    cfg = _select_repo_for_config(app, None)
    assert cfg.name == "child"


def test_select_repo_for_config_no_cwd_match(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    with pytest.raises(ConfigError, match="-r/--repo"):
        _select_repo_for_config(_cfg(), None)


def test_select_repo_for_config_single_repo_no_shortcut(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    with pytest.raises(ConfigError, match="-r/--repo"):
        _select_repo_for_config(_cfg(), None)


def test_cmd_config_json_output(monkeypatch, tmp_path, capsys):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.chdir(repo_path)
    cfg = _cfg(labels=["pm-agent"], assignee="@me").repositories["test"]
    cfg.path = str(repo_path)
    cfg.base_branch = "develop"
    cfg.spec_dir = "specs"
    cfg.branch_prefix = "feat-"
    app = AppConfig(repositories={"test": cfg})
    monkeypatch.setattr(cli, "_load", lambda _args: app)

    rc = cli.cmd_config(argparse.Namespace(repos=None, config=None))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "name": "test",
        "repo": "owner/repo",
        "path": str(repo_path),
        "spec_dir": "specs",
        "branch_prefix": "feat-",
        "base_branch": "develop",
        "idle_label": "status: idle",
        "blocked_label": "status: blocked",
        "target": {"labels": ["pm-agent"], "assignee": "@me", "milestone": ""},
        "issue_create_args": [
            "--label",
            "status: idle",
            "--label",
            "pm-agent",
            "--assignee",
            "@me",
        ],
    }


def test_cmd_config_detects_base_branch(monkeypatch, tmp_path, capsys):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.chdir(repo_path)
    cfg = _cfg().repositories["test"]
    cfg.path = str(repo_path)
    cfg.base_branch = ""
    app = AppConfig(repositories={"test": cfg})
    monkeypatch.setattr(cli, "_load", lambda _args: app)
    monkeypatch.setattr(cli, "detect_default_branch", lambda path, env=None: "main")

    rc = cli.cmd_config(argparse.Namespace(repos=None, config=None))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["base_branch"] == "main"


def test_cmd_config_base_branch_github_error_fallback(monkeypatch, tmp_path, capsys):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.chdir(repo_path)
    cfg = _cfg().repositories["test"]
    cfg.path = str(repo_path)
    cfg.base_branch = ""
    app = AppConfig(repositories={"test": cfg})
    monkeypatch.setattr(cli, "_load", lambda _args: app)

    def boom(_path, env=None):
        raise GitHubError("network")

    monkeypatch.setattr(cli, "detect_default_branch", boom)

    rc = cli.cmd_config(argparse.Namespace(repos=None, config=None))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["base_branch"] == ""


def test_cmd_config_with_repo_alias(monkeypatch, tmp_path, capsys):
    repo_path = tmp_path / "b"
    repo_path.mkdir()
    app = _multi_app()
    app.repositories["b"].path = str(repo_path)
    monkeypatch.setattr(cli, "_load", lambda _args: app)

    rc = cli.cmd_config(argparse.Namespace(repos=["b"], config=None))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "b"


def test_cmd_config_unknown_alias_exit2(monkeypatch):
    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    rc = cli.cmd_config(argparse.Namespace(repos=["missing"], config=None))
    assert rc == 2


def test_cmd_config_multiple_repos_exit2(monkeypatch):
    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    rc = cli.cmd_config(argparse.Namespace(repos=["a", "b"], config=None))
    assert rc == 2


def test_cmd_config_unregistered_cwd_exit2(monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setattr(cli, "_load", lambda _args: _cfg())
    rc = cli.cmd_config(argparse.Namespace(repos=None, config=None))
    assert rc == 2


def test_config_help_shows_repo():
    parser = cli.build_parser()
    config_parser = parser._subparsers._group_actions[0].choices["config"]
    help_text = config_parser.format_help()
    assert "--repo" in help_text
    assert "JSON" in help_text


def _seed_events(db_path, rows):
    from datetime import datetime

    from ghswarm.events import EventLog
    from ghswarm.orchestrator import StepResult

    el = EventLog(str(db_path))
    for repo, issue, action, detail, ts in rows:
        el.record(
            repo,
            StepResult(issue, action, detail),
            now=datetime.fromisoformat(ts),
        )


def test_cmd_history_output_and_filters(monkeypatch, tmp_path, capsys):
    db = tmp_path / "events.db"
    _seed_events(
        db,
        [
            ("owner/a", 1, "implemented", "first", "2026-07-18T10:00:00+00:00"),
            ("owner/a", 2, "reviewed", "second", "2026-07-18T11:00:00+00:00"),
            ("owner/b", 3, "merged", "third", "2026-07-18T12:00:00+00:00"),
        ],
    )
    app = _multi_app()
    for cfg in app.repositories.values():
        cfg.event_db = str(db)
    monkeypatch.setattr(cli, "_load", lambda _args: app)

    rc = cli.cmd_history(argparse.Namespace(repos=None, config=None, issue=None, limit=50))
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    assert out[0].startswith("[owner/b]")
    assert "merged" in out[0]
    assert out[1].startswith("[owner/a]")
    assert "reviewed" in out[1]

    capsys.readouterr()
    rc = cli.cmd_history(argparse.Namespace(repos=["a"], config=None, issue=1, limit=10))
    assert rc == 0
    filtered = capsys.readouterr().out.strip()
    assert filtered.startswith("2026-07-18T10:00:00+00:00")
    assert "[owner/" not in filtered
    assert "#1" in filtered
    assert "implemented" in filtered


def test_cmd_history_missing_db_exit0(monkeypatch, tmp_path, capsys):
    app = _cfg()
    app.repositories["test"].event_db = str(tmp_path / "missing.db")
    monkeypatch.setattr(cli, "_load", lambda _args: app)

    rc = cli.cmd_history(argparse.Namespace(repos=None, config=None, issue=None, limit=50))
    assert rc == 0
    assert "No events" in capsys.readouterr().out


def test_cmd_history_multiple_repos_exit2(monkeypatch):
    monkeypatch.setattr(cli, "_load", lambda _args: _multi_app())
    rc = cli.cmd_history(argparse.Namespace(repos=["a", "b"], config=None, issue=None, limit=50))
    assert rc == 2


def test_history_help_shows_subcommand():
    parser = cli.build_parser()
    assert "history" in parser._subparsers._group_actions[0].choices
    help_text = parser.format_help()
    assert "history" in help_text
