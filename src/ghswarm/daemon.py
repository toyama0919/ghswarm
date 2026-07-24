"""Primitives for daemonizing, stopping, and liveness checks."""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from pathlib import Path


def dated_log_path(base: str, date_str: str) -> str:
    """Return a log path with the date inserted just before the base path's extension."""
    root, ext = os.path.splitext(base)
    if ext:
        return f"{root}-{date_str}{ext}"
    return f"{base}-{date_str}.log"


def read_pid(pid_path: str) -> int | None:
    """Read the PID file and return an int. None if absent or malformed."""
    path = Path(pid_path)
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def is_alive(pid: int) -> bool:
    """Check whether the process is alive using os.kill(pid, 0)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def already_running(pid_path: str) -> int | None:
    """Return the daemon PID if one is alive. None if stale or absent."""
    pid = read_pid(pid_path)
    if pid is None:
        return None
    if is_alive(pid):
        return pid
    return None


def stop_daemon(pid_path: str) -> bool:
    """Send SIGTERM to a live daemon. Delete a stale PID file and return False."""
    pid = read_pid(pid_path)
    if pid is None:
        return False
    if is_alive(pid):
        os.kill(pid, signal.SIGTERM)
        return True
    remove_pid(pid_path)
    return False


def wait_until_gone(
    pid: int,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    is_alive_fn: Callable[[int], bool] | None = None,
    on_tick: Callable[[int], None] | None = None,
    poll_interval: float = 0.5,
) -> None:
    """Poll until pid is gone. on_tick is called at the start of each tick."""
    alive = is_alive_fn or is_alive
    tick = 0
    while alive(pid):
        if on_tick is not None:
            on_tick(tick)
        tick += 1
        sleep_fn(poll_interval)


def remove_pid(pid_path: str) -> None:
    """Delete the PID file (need not exist)."""
    Path(pid_path).unlink(missing_ok=True)


def daemonize(log_path: str, pid_path: str) -> None:
    """Detach from the terminal via a double fork and set up the log and PID files.

    The caller process (the first parent) prints the startup banner and exits
    immediately. All subsequent work continues only in the daemon (grandchild process).
    """
    if not hasattr(os, "fork"):
        raise RuntimeError("daemon mode is not supported on this platform")

    read_fd, write_fd = os.pipe()

    pid = os.fork()
    if pid > 0:
        os.close(write_fd)
        data = os.read(read_fd, 32)
        os.close(read_fd)
        if data:
            daemon_pid = int(data.decode().strip())
            print(f"ghswarm daemon started (pid={daemon_pid}, log={log_path})", flush=True)
        os._exit(0)

    os.close(read_fd)
    os.setsid()

    pid = os.fork()
    if pid > 0:
        os.write(write_fd, str(pid).encode())
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)

    os.chdir("/")

    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    pid_dir = os.path.dirname(pid_path)
    if pid_dir:
        os.makedirs(pid_dir, exist_ok=True)

    with open(os.devnull, "rb", buffering=0) as devnull:
        os.dup2(devnull.fileno(), 0)

    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    if log_fd > 2:
        os.close(log_fd)

    Path(pid_path).write_text(str(os.getpid()), encoding="utf-8")
