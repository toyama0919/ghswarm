"""Tests for command assembly and test-verification skipping (subprocess is not launched)."""

from __future__ import annotations

import prpilot.executor as ex
from prpilot.config import AgentConfig, RepoConfig, _DEFAULT_TRANSIENT_ERROR_PATTERNS
from prpilot.executor import (
    _build_command,
    _has_conflict_markers,
    execute_with_self_healing,
    fix_ci_with_agent,
    is_transient,
    resolve_conflict_with_agent,
    run_cli,
    run_tests,
)


def test_build_command_with_placeholder():
    cmd = _build_command("claude -p {prompt} --yes", "hello world")
    assert cmd == "claude -p 'hello world' --yes"


def test_build_command_without_placeholder_appends():
    cmd = _build_command("cursor-agent -p", "do it")
    assert cmd == "cursor-agent -p 'do it'"


def test_build_command_quotes_shell_metacharacters():
    cmd = _build_command("run {prompt}", "a; rm -rf /")
    # shlex.quote single-quotes it, so nothing is injected
    assert cmd == "run 'a; rm -rf /'"


def test_run_tests_skips_when_command_blank():
    code, output = run_tests("   ", cwd=".")
    assert code == 0
    assert "skipping" in output


class _SpyGit:
    """Git stub that just records whether rollback was called."""

    cwd = "."

    def __init__(self):
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True


def _cfg() -> RepoConfig:
    return RepoConfig(agents={"a": AgentConfig(name="a", commands=["noop"])}, max_retries=2)


def _healing(
    monkeypatch,
    *,
    cli_code: int,
    test_code: int,
    git: _SpyGit,
    cli_output="cli output",
    test_output="test output",
):
    monkeypatch.setattr(ex, "run_cli", lambda *a, **k: (cli_code, cli_output))
    monkeypatch.setattr(ex, "run_tests", lambda *a, **k: (test_code, test_output))
    return execute_with_self_healing(
        _cfg(), AgentConfig(name="a", commands=["noop"]), git, "pytest", "prompt", lambda: False
    )


def test_max_retries_does_not_roll_back(monkeypatch):
    # keep the work even on failure (so a human can pick it up)
    git = _SpyGit()
    result = _healing(monkeypatch, cli_code=0, test_code=1, git=git)
    assert result.ok is False
    assert result.reason == "max_retries"
    assert git.rolled_back is False


def test_cli_failure_does_not_roll_back(monkeypatch):
    git = _SpyGit()
    result = _healing(monkeypatch, cli_code=1, test_code=0, git=git)
    assert result.ok is False
    assert result.reason == "cli_failed"
    assert git.rolled_back is False


def test_max_retries_output_includes_test_failure_log(monkeypatch):
    # the test output must be included in the result so the failure reason can be posted to the Issue comment
    result = _healing(monkeypatch, cli_code=0, test_code=1, git=_SpyGit())
    assert "test output" in result.output


# -- run_cli fallback chain ------------------------------------------------


def test_run_cli_first_success_skips_fallback(monkeypatch):
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class Res:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Res()

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    agent = AgentConfig(
        name="implement",
        commands=["cmd-primary {prompt}", "cmd-fallback {prompt}"],
    )
    code, output = run_cli(agent, "hello", cwd=".")
    assert code == 0
    assert output == "ok"
    assert len(calls) == 1
    assert "cmd-primary" in calls[0]


def test_run_cli_falls_back_on_first_failure(monkeypatch):
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class Res:
            returncode = 0 if "fallback" in cmd else 1
            stdout = "fallback ok" if "fallback" in cmd else "primary fail"
            stderr = ""

        return Res()

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    agent = AgentConfig(
        name="implement",
        commands=["cmd-primary {prompt}", "cmd-fallback {prompt}"],
    )
    code, output = run_cli(agent, "hello", cwd=".")
    assert code == 0
    assert output == "fallback ok"
    assert len(calls) == 2


def test_run_cli_all_fail_returns_last_code_and_concatenated_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        class Res:
            returncode = 2 if "second" in cmd else 1
            stdout = f"out:{cmd[:20]}"
            stderr = ""

        return Res()

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    agent = AgentConfig(
        name="implement",
        commands=["cmd-first {prompt}", "cmd-second {prompt}"],
    )
    code, output = run_cli(agent, "hello", cwd=".")
    assert code == 2
    assert "out:cmd-first" in output
    assert "out:cmd-second" in output


def test_run_cli_single_command_chain_backward_compatible(monkeypatch):
    """A single-element commands list behaves the same as the legacy single-command run (backward compat)."""
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class Res:
            returncode = 0
            stdout = "single ok"
            stderr = ""

        return Res()

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    agent = AgentConfig(name="implement", commands=["only-cmd {prompt}"])
    code, output = run_cli(agent, "hello", cwd=".")
    assert code == 0
    assert output == "single ok"
    assert len(calls) == 1


def test_run_cli_removes_stale_question_file_before_fallback(tmp_path, monkeypatch):
    qfile = tmp_path / ".agent_question.md"
    qfile.write_text("stale question", encoding="utf-8")
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            qfile.write_text("written by failed cmd", encoding="utf-8")

        class Res:
            returncode = 0 if len(calls) == 2 else 1
            stdout = "ok"
            stderr = ""

        return Res()

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    agent = AgentConfig(
        name="implement",
        commands=["cmd-primary {prompt}", "cmd-fallback {prompt}"],
    )
    code, output = run_cli(
        agent,
        "hello",
        cwd=str(tmp_path),
        question_file=".agent_question.md",
    )
    assert code == 0
    assert output == "ok"
    assert len(calls) == 2
    assert not qfile.exists()


# -- is_transient ----------------------------------------------------------


def test_is_transient_matches_default_patterns():
    patterns = list(_DEFAULT_TRANSIENT_ERROR_PATTERNS)
    assert is_transient("RetriableError: [resource_exhausted] Error", patterns)
    assert is_transient("Connection lost, reconnecting to https://example.com", patterns)
    assert is_transient("HTTP 429 Too Many Requests", patterns)
    assert is_transient("service unavailable 503", patterns)
    assert is_transient("ECONNRESET during pip install", patterns)


def test_is_transient_case_insensitive():
    patterns = list(_DEFAULT_TRANSIENT_ERROR_PATTERNS)
    assert is_transient("RESOURCE_EXHAUSTED", patterns)
    assert is_transient("rate LIMIT exceeded", patterns)


def test_is_transient_does_not_match_fatal_errors():
    patterns = list(_DEFAULT_TRANSIENT_ERROR_PATTERNS)
    assert not is_transient("AssertionError: expected 1 == 2", patterns)
    assert not is_transient("FAILED tests/test_foo.py::test_bar", patterns)


def test_is_transient_does_not_match_timeout_by_default():
    patterns = list(_DEFAULT_TRANSIENT_ERROR_PATTERNS)
    assert not is_transient("ERROR: implement timed out (1800s)", patterns)
    assert not is_transient("ERROR: tests timed out", patterns)


def test_is_transient_http_status_uses_word_boundary():
    patterns = list(_DEFAULT_TRANSIENT_ERROR_PATTERNS)
    # do not falsely match when the number is embedded in another token
    assert not is_transient("line1429 in foo.py", patterns)
    assert not is_transient("processed 5030 items", patterns)
    assert not is_transient("error code 1429", patterns)
    assert is_transient("HTTP status 429", patterns)
    assert is_transient("returned 503", patterns)


# -- execute_with_self_healing transient -----------------------------------


def test_agent_transient_failure_returns_transient_reason(monkeypatch):
    result = _healing(
        monkeypatch,
        cli_code=1,
        test_code=0,
        git=_SpyGit(),
        cli_output="RetriableError: [resource_exhausted] Error",
    )
    assert result.ok is False
    assert result.reason == "transient"
    assert result.attempts == 1


def test_verify_transient_failure_skips_self_healing(monkeypatch):
    call_count = {"n": 0}

    def fake_run_cli(*a, **k):
        call_count["n"] += 1
        return 0, "ok"

    monkeypatch.setattr(ex, "run_cli", fake_run_cli)
    monkeypatch.setattr(
        ex,
        "run_tests",
        lambda *a, **k: (1, "pip install failed: network unreachable ECONNRESET"),
    )
    result = execute_with_self_healing(
        _cfg(),
        AgentConfig(name="a", commands=["noop"]),
        _SpyGit(),
        "pytest",
        "prompt",
        lambda: False,
    )
    assert result.ok is False
    assert result.reason == "transient"
    assert call_count["n"] == 1  # no second CLI run for self-healing


def test_real_test_failure_enters_self_healing(monkeypatch):
    call_count = {"n": 0}

    def fake_run_cli(*a, **k):
        call_count["n"] += 1
        return 0, f"cli attempt {call_count['n']}"

    monkeypatch.setattr(ex, "run_cli", fake_run_cli)
    monkeypatch.setattr(
        ex, "run_tests", lambda *a, **k: (1, "FAILED tests/test_foo.py::test_bar - AssertionError")
    )
    result = execute_with_self_healing(
        _cfg(),
        AgentConfig(name="a", commands=["noop"]),
        _SpyGit(),
        "pytest",
        "prompt",
        lambda: False,
    )
    assert result.ok is False
    assert result.reason == "max_retries"
    assert call_count["n"] == 2  # self-healing retries up to max_retries=2


# -- resolve_conflict_with_agent -------------------------------------------


class _ConflictGit:
    cwd: str = "."

    def __init__(self, merge_result="clean", sync_ok=True):
        self.merge_result = merge_result
        self.sync_ok = sync_ok
        self.aborted = False
        self.synced = False

    def sync_branch_to_remote(self, branch: str) -> bool:
        self.synced = True
        return self.sync_ok

    def merge_base(self, base: str):
        return self.merge_result

    def abort_merge(self) -> None:
        self.aborted = True


def _resolve_cfg() -> RepoConfig:
    return RepoConfig(
        agents={"implement": AgentConfig(name="implement", commands=["noop"])},
        max_retries=2,
    )


def test_resolve_conflict_clean_merge_skips_agent(monkeypatch):
    git = _ConflictGit(merge_result="clean")
    calls: list[str] = []

    def fake_run_cli(*a, **k):
        calls.append("cli")
        return 0, ""

    monkeypatch.setattr(ex, "run_cli", fake_run_cli)
    monkeypatch.setattr(
        ex,
        "run_tests",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("verify must not be called")),
    )

    result = resolve_conflict_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "main",
        "issue-7",
        "header",
    )

    assert result.ok is True
    assert result.clean_merge is True
    assert result.attempts == 0
    assert calls == []
    assert git.synced is True


def test_resolve_conflict_agent_success_passes_verify(monkeypatch, tmp_path):
    conflict_file = tmp_path / "a.py"
    conflict_file.write_text("resolved", encoding="utf-8")
    git = _ConflictGit(merge_result=["a.py"])
    git.cwd = str(tmp_path)

    monkeypatch.setattr(ex, "run_cli", lambda *a, **k: (0, "agent ok"))
    monkeypatch.setattr(ex, "run_tests", lambda *a, **k: (0, "tests ok"))

    result = resolve_conflict_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "main",
        "issue-7",
        "header",
    )

    assert result.ok is True
    assert result.clean_merge is False
    assert result.attempts == 1
    assert git.aborted is False


def test_resolve_conflict_verify_failure_aborts(monkeypatch, tmp_path):
    conflict_file = tmp_path / "a.py"
    conflict_file.write_text("resolved", encoding="utf-8")
    git = _ConflictGit(merge_result=["a.py"])
    git.cwd = str(tmp_path)

    monkeypatch.setattr(ex, "run_cli", lambda *a, **k: (0, "agent ok"))
    monkeypatch.setattr(ex, "run_tests", lambda *a, **k: (1, "FAILED"))

    result = resolve_conflict_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "main",
        "issue-7",
        "header",
    )

    assert result.ok is False
    assert result.reason == "verify_failed"
    assert git.aborted is True


def test_resolve_conflict_markers_remain_aborts(monkeypatch, tmp_path):
    conflict_file = tmp_path / "a.py"
    conflict_file.write_text("<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> base\n", encoding="utf-8")
    git = _ConflictGit(merge_result=["a.py"])
    git.cwd = str(tmp_path)

    monkeypatch.setattr(ex, "run_cli", lambda *a, **k: (0, "agent ok"))
    monkeypatch.setattr(
        ex,
        "run_tests",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("verify must not be called")),
    )

    result = resolve_conflict_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "main",
        "issue-7",
        "header",
    )

    assert result.ok is False
    assert result.reason == "markers_remain"
    assert git.aborted is True


def test_has_conflict_markers_detects_marker_lines(tmp_path):
    path = tmp_path / "x.py"
    path.write_text("no markers\n", encoding="utf-8")
    assert _has_conflict_markers(["x.py"], str(tmp_path)) is False
    path.write_text("<<<<<<< ours\n", encoding="utf-8")
    assert _has_conflict_markers(["x.py"], str(tmp_path)) is True


def test_has_conflict_markers_ignores_comment_mentions(tmp_path):
    path = tmp_path / "x.py"
    path.write_text("# see <<<<<<< in docs\n", encoding="utf-8")
    assert _has_conflict_markers(["x.py"], str(tmp_path)) is False


def test_resolve_conflict_merge_failed_without_files_aborts(monkeypatch):
    git = _ConflictGit(merge_result=[])

    monkeypatch.setattr(
        ex,
        "run_cli",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent must not be called")),
    )

    result = resolve_conflict_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "main",
        "issue-7",
        "header",
    )

    assert result.ok is False
    assert result.reason == "merge_failed"
    assert result.attempts == 0
    assert git.aborted is True


def test_resolve_conflict_sync_failed_has_zero_attempts(monkeypatch):
    git = _ConflictGit(sync_ok=False)
    monkeypatch.setattr(
        ex,
        "run_cli",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent must not be called")),
    )

    result = resolve_conflict_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "main",
        "issue-7",
        "header",
    )

    assert result.ok is False
    assert result.reason == "sync_failed"
    assert result.attempts == 0


# -- fix_ci_with_agent -----------------------------------------------------


class _CIFixGit:
    cwd: str = "."

    def __init__(self, *, sync_ok: bool = True):
        self.sync_ok = sync_ok
        self.synced = False

    def sync_branch_to_remote(self, branch: str) -> bool:
        self.synced = True
        return self.sync_ok


def test_fix_ci_verify_success(monkeypatch):
    git = _CIFixGit()
    monkeypatch.setattr(ex, "run_cli", lambda *a, **k: (0, "agent ok"))
    monkeypatch.setattr(ex, "run_tests", lambda *a, **k: (0, "tests ok"))

    result = fix_ci_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "issue-7",
        "header",
        "FAILED: test_foo",
    )

    assert result.ok is True
    assert git.synced is True


def test_fix_ci_verify_failure(monkeypatch):
    git = _CIFixGit()
    monkeypatch.setattr(ex, "run_cli", lambda *a, **k: (0, "agent ok"))
    monkeypatch.setattr(ex, "run_tests", lambda *a, **k: (1, "FAILED"))

    result = fix_ci_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "issue-7",
        "header",
        "FAILED: test_foo",
    )

    assert result.ok is False
    assert result.reason == "verify_failed"


def test_fix_ci_cli_failure(monkeypatch):
    git = _CIFixGit()
    monkeypatch.setattr(ex, "run_cli", lambda *a, **k: (1, "agent crashed"))
    monkeypatch.setattr(
        ex,
        "run_tests",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("verify must not be called")),
    )

    result = fix_ci_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "issue-7",
        "header",
        "FAILED: test_foo",
    )

    assert result.ok is False
    assert result.reason == "cli_failed"


def test_fix_ci_transient_output(monkeypatch):
    git = _CIFixGit()
    monkeypatch.setattr(
        ex,
        "run_cli",
        lambda *a, **k: (1, "RetriableError: [resource_exhausted] Error"),
    )
    monkeypatch.setattr(
        ex,
        "run_tests",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("verify must not be called")),
    )

    result = fix_ci_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "issue-7",
        "header",
        "FAILED: test_foo",
    )

    assert result.ok is False
    assert result.reason == "transient"


def test_fix_ci_sync_failed_has_zero_attempts(monkeypatch):
    git = _CIFixGit(sync_ok=False)
    monkeypatch.setattr(
        ex,
        "run_cli",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent must not be called")),
    )

    result = fix_ci_with_agent(
        _resolve_cfg(),
        AgentConfig(name="implement", commands=["noop"]),
        git,
        "pytest",
        "issue-7",
        "header",
        "FAILED: test_foo",
    )

    assert result.ok is False
    assert result.reason == "sync_failed"
    assert result.attempts == 0
