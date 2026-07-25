"""Tests for the sandbox runners and Docker sandbox execution."""

from __future__ import annotations

import subprocess

import pytest

from ghswarm.config import SandboxConfig
from ghswarm.sandbox import (
    DockerRunner,
    DockerUnavailableError,
    LocalRunner,
    ensure_docker_available,
    make_runner,
    reset_docker_availability_cache,
)


@pytest.fixture(autouse=True)
def _reset_docker_cache():
    reset_docker_availability_cache()
    yield
    reset_docker_availability_cache()


def test_make_runner_none_returns_local_runner():
    runner = make_runner(SandboxConfig(driver="none"))
    assert isinstance(runner, LocalRunner)


def test_make_runner_docker_returns_docker_runner(monkeypatch):
    monkeypatch.setattr("ghswarm.sandbox.ensure_docker_available", lambda: None)
    runner = make_runner(SandboxConfig(driver="docker", image="python:3.12"))
    assert isinstance(runner, DockerRunner)


def test_make_runner_docker_checks_availability(monkeypatch):
    called = {"n": 0}

    def _check():
        called["n"] += 1

    monkeypatch.setattr("ghswarm.sandbox.ensure_docker_available", _check)
    make_runner(SandboxConfig(driver="docker", image="python:3.12"))
    make_runner(SandboxConfig(driver="docker", image="python:3.12"))
    assert called["n"] == 2


def test_ensure_docker_available_memoized(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="24.0.0\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ensure_docker_available()
    ensure_docker_available()
    assert len(calls) == 1


def test_ensure_docker_unavailable_when_command_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DockerUnavailableError, match="docker command was not found"):
        ensure_docker_available()


def test_ensure_docker_unavailable_when_daemon_down(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Cannot connect to daemon")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DockerUnavailableError, match="Docker daemon"):
        ensure_docker_available()


def test_local_runner_executes_command(tmp_path):
    runner = LocalRunner()
    code, output = runner.run("echo hello-local", str(tmp_path), None, timeout=30)
    assert code == 0
    assert "hello-local" in output


def test_local_runner_uses_workdir_under_mount_root(tmp_path):
    sub = tmp_path / "backend"
    sub.mkdir()
    runner = LocalRunner()
    code, output = runner.run("pwd", str(tmp_path), "backend", timeout=30)
    assert code == 0
    assert output.strip() == str(sub.resolve())


def test_docker_runner_builds_expected_args(monkeypatch, tmp_path):
    monkeypatch.setattr("ghswarm.sandbox.os.getuid", lambda: 1000)
    monkeypatch.setattr("ghswarm.sandbox.os.getgid", lambda: 1001)
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    cwd = tmp_path / "worktree"
    cwd.mkdir()
    git_common = tmp_path / "repo.git"
    git_common.mkdir()

    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd_arg: str(git_common))

    sandbox = SandboxConfig(
        driver="docker",
        image="node:22-bookworm",
        network="default",
        user="auto",
        env={"npm_config_cache": "/cache/npm"},
        env_passthrough=["GH_TOKEN", "GITHUB_TOKEN"],
        volumes=["ghswarm-cache:/cache"],
    )
    runner = DockerRunner(sandbox)
    args, name = runner._build_run_command("npm test", str(cwd), None)

    assert args[:3] == ["docker", "run", "--rm"]
    assert "--user" in args and "1000:1001" in args
    assert f"{cwd.resolve()}:/workspace" in args
    assert "-w" in args and "/workspace" in args
    assert f"{git_common}:{git_common}" in args
    assert "--network" not in args
    assert "HOME=/tmp" in args
    assert "GIT_CONFIG_COUNT=1" in args
    assert "GIT_CONFIG_KEY_0=safe.directory" in args
    assert "GIT_CONFIG_VALUE_0=*" in args
    assert "npm_config_cache=/cache/npm" in args
    assert "GH_TOKEN" in args
    assert "GITHUB_TOKEN" not in args
    assert "ghswarm-cache:/cache" in args
    assert args[-4:] == ["node:22-bookworm", "sh", "-c", "npm test"]
    assert name.startswith("ghswarm-sbx-")


def test_docker_runner_mounts_whole_worktree_with_per_path_workdir(monkeypatch, tmp_path):
    """A step scoped to a subdirectory still mounts the whole worktree; only -w changes."""
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd_arg: None)

    runner = DockerRunner(SandboxConfig(driver="docker", image="alpine"))
    args, _ = runner._build_run_command("pytest", str(cwd), "backend")

    # The mount is still the whole worktree (not worktree/backend).
    assert f"{cwd.resolve()}:/workspace" in args
    assert f"{cwd.resolve() / 'backend'}:/workspace" not in args
    # Only the container's working directory reflects the step's path.
    w_idx = args.index("-w")
    assert args[w_idx + 1] == "/workspace/backend"


def test_docker_runner_workdir_none_uses_workspace_root(monkeypatch, tmp_path):
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd_arg: None)
    runner = DockerRunner(SandboxConfig(driver="docker", image="alpine"))
    args, _ = runner._build_run_command("true", str(tmp_path), None)
    w_idx = args.index("-w")
    assert args[w_idx + 1] == "/workspace"


def test_docker_runner_injects_git_safe_directory_env(monkeypatch, tmp_path):
    """The docker run args include GIT_CONFIG_* for git safe.directory.

    Real in-container git behavior depends on the runtime environment, so here we only verify argument construction.
    """
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd: None)
    runner = DockerRunner(SandboxConfig(driver="docker", image="alpine"))
    args, _ = runner._build_run_command("true", str(tmp_path), None)

    assert "GIT_CONFIG_COUNT=1" in args
    assert "GIT_CONFIG_KEY_0=safe.directory" in args
    assert "GIT_CONFIG_VALUE_0=*" in args
    assert "HOME=/tmp" in args


def test_docker_runner_git_safe_directory_env_overridable(monkeypatch, tmp_path):
    """GIT_CONFIG_VALUE_0 can be overridden via sandbox.env."""
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd: None)
    sandbox = SandboxConfig(
        driver="docker",
        image="alpine",
        env={"GIT_CONFIG_VALUE_0": "/workspace"},
    )
    runner = DockerRunner(sandbox)
    args, _ = runner._build_run_command("true", str(tmp_path), None)

    assert "GIT_CONFIG_VALUE_0=/workspace" in args
    assert "GIT_CONFIG_VALUE_0=*" not in args


def test_docker_runner_network_none(monkeypatch, tmp_path):
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd: None)
    runner = DockerRunner(SandboxConfig(driver="docker", image="alpine", network="none"))
    args, _ = runner._build_run_command("true", str(tmp_path), None)
    idx = args.index("--network")
    assert args[idx + 1] == "none"


def test_docker_runner_skips_user_when_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd: None)
    runner = DockerRunner(SandboxConfig(driver="docker", image="alpine", user=""))
    args, _ = runner._build_run_command("true", str(tmp_path), None)
    assert "--user" not in args


def test_docker_runner_isolate_dirs_adds_tmpfs_mount(monkeypatch, tmp_path):
    """When isolate_dirs is set, tmpfs mount args are added.

    Actual Docker isolation behavior depends on the runtime environment, so here we only verify argument construction.
    """
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd: None)
    runner = DockerRunner(SandboxConfig(driver="docker", image="alpine", isolate_dirs=[".venv"]))
    args, _ = runner._build_run_command("true", str(tmp_path), None)

    tmpfs_idx = args.index("--tmpfs")
    assert args[tmpfs_idx + 1] == "/workspace/.venv:exec,mode=1777"
    assert "exec" in args[tmpfs_idx + 1]
    assert "mode=1777" in args[tmpfs_idx + 1]


def test_docker_runner_isolate_dirs_stay_mount_root_relative_with_workdir(monkeypatch, tmp_path):
    """isolate_dirs stay relative to the mount root even when workdir scopes to a subdirectory."""
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd: None)
    runner = DockerRunner(SandboxConfig(driver="docker", image="alpine", isolate_dirs=[".venv"]))
    args, _ = runner._build_run_command("true", str(tmp_path), "backend")

    tmpfs_idx = args.index("--tmpfs")
    assert args[tmpfs_idx + 1] == "/workspace/.venv:exec,mode=1777"


def test_docker_runner_isolate_dirs_multiple_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd: None)
    runner = DockerRunner(
        SandboxConfig(
            driver="docker",
            image="alpine",
            isolate_dirs=[".venv", "node_modules", "a/b"],
        )
    )
    args, _ = runner._build_run_command("true", str(tmp_path), None)

    tmpfs_mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--tmpfs"]
    assert tmpfs_mounts == [
        "/workspace/.venv:exec,mode=1777",
        "/workspace/node_modules:exec,mode=1777",
        "/workspace/a/b:exec,mode=1777",
    ]


def test_docker_runner_isolate_dirs_empty_omits_mount(monkeypatch, tmp_path):
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd: None)
    runner = DockerRunner(SandboxConfig(driver="docker", image="alpine", isolate_dirs=[]))
    args, _ = runner._build_run_command("true", str(tmp_path), None)
    assert "--tmpfs" not in args


def test_docker_runner_timeout_kills_container(monkeypatch, tmp_path):
    monkeypatch.setattr("ghswarm.sandbox._resolve_git_common_dir", lambda cwd: None)
    killed: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd[0:2] == ["docker", "kill"]:
            killed.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = DockerRunner(SandboxConfig(driver="docker", image="alpine"))
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run("sleep 999", str(tmp_path), None, timeout=1)
    assert len(killed) == 1
    assert killed[0][0:2] == ["docker", "kill"]


def test_make_runner_docker_hard_fails_without_docker(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DockerUnavailableError):
        make_runner(SandboxConfig(driver="docker", image="alpine"))


def test_run_worktree_setup_uses_runner(monkeypatch, tmp_path):
    import ghswarm.orchestrator as orchestrator
    from ghswarm.config import AgentConfig, RepoConfig

    calls: list[tuple[str, str, str | None, int]] = []

    class _StubRunner:
        def run(
            self, command: str, mount_root: str, workdir: str | None, timeout: int
        ) -> tuple[int, str]:
            calls.append((command, mount_root, workdir, timeout))
            return 0, "setup ok"

    cfg = RepoConfig(
        agents={
            "implement": AgentConfig(name="implement", commands=["noop"]),
            "review": AgentConfig(name="review", commands=["noop"]),
        },
        base_branch="main",
        worktree_setup="echo setup",
    )
    monkeypatch.setattr(orchestrator, "make_runner", lambda sandbox: _StubRunner())
    monkeypatch.setattr(orchestrator, "GitHub", lambda repo, env=None: object())
    monkeypatch.setattr(orchestrator, "Git", lambda root: object())

    orch = orchestrator.Orchestrator(cfg)
    path = str(tmp_path)
    orch._run_worktree_setup(path)
    assert calls == [("echo setup", path, None, 600)]


def test_run_worktree_setup_skips_empty_command(monkeypatch):
    import ghswarm.orchestrator as orchestrator
    from ghswarm.config import AgentConfig, RepoConfig

    monkeypatch.setattr(
        orchestrator,
        "make_runner",
        lambda sandbox: (_ for _ in ()).throw(AssertionError("must not create a runner")),
    )
    monkeypatch.setattr(orchestrator, "GitHub", lambda repo, env=None: object())
    monkeypatch.setattr(orchestrator, "Git", lambda root: object())

    cfg = RepoConfig(
        agents={
            "implement": AgentConfig(name="implement", commands=["noop"]),
            "review": AgentConfig(name="review", commands=["noop"]),
        },
        base_branch="main",
        worktree_setup="   ",
    )
    orch = orchestrator.Orchestrator(cfg)
    orch._run_worktree_setup("/tmp/worktree")


def test_run_worktree_setup_continues_on_failure(monkeypatch, tmp_path):
    import ghswarm.orchestrator as orchestrator
    from ghswarm.config import AgentConfig, RepoConfig

    class _FailRunner:
        def run(
            self, command: str, mount_root: str, workdir: str | None, timeout: int
        ) -> tuple[int, str]:
            return 1, "setup failed"

    monkeypatch.setattr(orchestrator, "make_runner", lambda sandbox: _FailRunner())
    monkeypatch.setattr(orchestrator, "GitHub", lambda repo, env=None: object())
    monkeypatch.setattr(orchestrator, "Git", lambda root: object())

    cfg = RepoConfig(
        agents={
            "implement": AgentConfig(name="implement", commands=["noop"]),
            "review": AgentConfig(name="review", commands=["noop"]),
        },
        base_branch="main",
        worktree_setup="false",
    )
    orch = orchestrator.Orchestrator(cfg)
    orch._run_worktree_setup(str(tmp_path))
