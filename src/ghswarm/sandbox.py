"""Shell execution runners for setup / verify (local or Docker sandbox)."""

from __future__ import annotations

import os
import posixpath
import subprocess
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from .config import SandboxConfig


class DockerUnavailableError(Exception):
    """Raised when Docker is unavailable."""


_docker_available: bool | None = None


def ensure_docker_available() -> None:
    """Check Docker CLI and daemon availability, only once per process."""
    global _docker_available
    if _docker_available is True:
        return

    try:
        res = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as e:
        raise DockerUnavailableError(
            "sandbox.driver: docker is enabled, but the docker command was not found."
            " Install Docker, or set sandbox.driver back to none."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise DockerUnavailableError(
            "sandbox.driver: docker is enabled, but the Docker daemon is unreachable."
            " Start Docker Desktop / dockerd, or set sandbox.driver back to none."
        ) from e

    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        msg = (
            "sandbox.driver: docker is enabled, but the Docker daemon is unreachable."
            " Start Docker Desktop / dockerd, or set sandbox.driver back to none."
        )
        if detail:
            msg = f"{msg}\n{detail}"
        raise DockerUnavailableError(msg)

    _docker_available = True


def reset_docker_availability_cache() -> None:
    """For tests: reset the Docker availability cache."""
    global _docker_available
    _docker_available = None


def _resolve_git_common_dir(cwd: str) -> str | None:
    """Return the worktree's git common-dir as an absolute path. None if not under git."""
    try:
        res = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None

    raw = res.stdout.strip()
    if not raw:
        return None

    path = Path(raw)
    if not path.is_absolute():
        path = (Path(cwd) / path).resolve()
    else:
        path = path.resolve()
    if not path.is_dir():
        return None
    return str(path)


class ShellRunner(ABC):
    @abstractmethod
    def run(
        self, command: str, mount_root: str, workdir: str | None, timeout: int
    ) -> tuple[int, str]:
        """Run a shell command and return (exit_code, combined_output).

        mount_root is always the whole worktree (under Docker, the whole worktree is
        mounted regardless of workdir). workdir is the step's path relative to
        mount_root (None means mount_root itself).
        """


class LocalRunner(ShellRunner):
    """Runs on the host, equivalent to subprocess.run(shell=True, cwd=...)."""

    def run(
        self, command: str, mount_root: str, workdir: str | None, timeout: int
    ) -> tuple[int, str]:
        cwd = str(Path(mount_root) / workdir) if workdir else mount_root
        res = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return res.returncode, (res.stdout or "") + (res.stderr or "")


class DockerRunner(ShellRunner):
    """Runs sh -c "<command>" inside docker run --rm.

    The whole worktree is always mounted at /workspace; only the container's working
    directory (-w) changes per step, via workdir.
    """

    def __init__(self, sandbox: SandboxConfig) -> None:
        self._sandbox = sandbox

    def _build_run_command(
        self, command: str, mount_root: str, workdir: str | None
    ) -> tuple[list[str], str]:
        mount_root_abs = str(Path(mount_root).resolve())
        args: list[str] = ["docker", "run", "--rm"]

        if self._sandbox.user == "auto":
            args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        elif self._sandbox.user:
            args.extend(["--user", self._sandbox.user])

        container_workdir = posixpath.join("/workspace", workdir) if workdir else "/workspace"
        args.extend(["-v", f"{mount_root_abs}:/workspace", "-w", container_workdir])

        git_common = _resolve_git_common_dir(mount_root_abs)
        if git_common:
            args.extend(["-v", f"{git_common}:{git_common}"])

        if self._sandbox.network == "none":
            args.extend(["--network", "none"])

        env = {
            "HOME": "/tmp",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        }
        env.update(self._sandbox.env)
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])

        for var in self._sandbox.env_passthrough:
            if var in os.environ:
                args.extend(["-e", var])

        for volume in self._sandbox.volumes:
            args.extend(["-v", volume])

        for d in self._sandbox.isolate_dirs:
            dest = posixpath.join("/workspace", d)
            args.extend(["--tmpfs", f"{dest}:exec,mode=1777"])

        container_name = f"ghswarm-sbx-{uuid.uuid4().hex}"
        args.extend(["--name", container_name])
        args.extend([self._sandbox.image, "sh", "-c", command])
        return args, container_name

    def run(
        self, command: str, mount_root: str, workdir: str | None, timeout: int
    ) -> tuple[int, str]:
        args, container_name = self._build_run_command(command, mount_root, workdir)
        try:
            res = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                text=True,
            )
            raise
        return res.returncode, (res.stdout or "") + (res.stderr or "")


def make_runner(sandbox: SandboxConfig) -> ShellRunner:
    """Return a ShellRunner according to sandbox.driver."""
    if sandbox.driver == "none":
        return LocalRunner()
    if sandbox.driver == "docker":
        ensure_docker_available()
        return DockerRunner(sandbox)
    return LocalRunner()
