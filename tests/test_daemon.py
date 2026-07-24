"""Unit tests for the daemon module (no real fork is performed)."""

from __future__ import annotations

import os
import re
import signal
from unittest.mock import patch

import pytest

from ghswarm import daemon


def test_read_pid_missing(tmp_path):
    assert daemon.read_pid(str(tmp_path / "missing.pid")) is None


def test_read_pid_invalid(tmp_path):
    pid_path = tmp_path / "bad.pid"
    pid_path.write_text("not-a-pid", encoding="utf-8")
    assert daemon.read_pid(str(pid_path)) is None


def test_read_pid_valid(tmp_path):
    pid_path = tmp_path / "ok.pid"
    pid_path.write_text("  4242\n", encoding="utf-8")
    assert daemon.read_pid(str(pid_path)) == 4242


def test_is_alive_true():
    with patch("os.kill", return_value=None):
        assert daemon.is_alive(123) is True


def test_is_alive_false_on_process_lookup_error():
    with patch("os.kill", side_effect=ProcessLookupError):
        assert daemon.is_alive(123) is False


def test_is_alive_true_on_permission_error():
    with patch("os.kill", side_effect=PermissionError):
        assert daemon.is_alive(123) is True


def test_already_running_stale(tmp_path):
    pid_path = tmp_path / "stale.pid"
    pid_path.write_text("99999", encoding="utf-8")
    with patch("ghswarm.daemon.is_alive", return_value=False):
        assert daemon.already_running(str(pid_path)) is None


def test_already_running_alive(tmp_path):
    pid_path = tmp_path / "live.pid"
    pid_path.write_text("4242", encoding="utf-8")
    with patch("ghswarm.daemon.is_alive", return_value=True):
        assert daemon.already_running(str(pid_path)) == 4242


def test_stop_daemon_sends_sigterm_and_keeps_pid_file(tmp_path):
    pid_path = tmp_path / "live.pid"
    pid_path.write_text("4242", encoding="utf-8")
    with (
        patch("ghswarm.daemon.read_pid", return_value=4242),
        patch("ghswarm.daemon.is_alive", return_value=True),
        patch("os.kill") as kill,
    ):
        assert daemon.stop_daemon(str(pid_path)) is True
        kill.assert_called_once_with(4242, signal.SIGTERM)
    assert pid_path.is_file()


def test_stop_daemon_removes_stale_pid_file(tmp_path):
    pid_path = tmp_path / "stale.pid"
    pid_path.write_text("99999", encoding="utf-8")
    with (
        patch("ghswarm.daemon.read_pid", return_value=99999),
        patch("ghswarm.daemon.is_alive", return_value=False),
    ):
        assert daemon.stop_daemon(str(pid_path)) is False
    assert not pid_path.exists()


def test_stop_daemon_missing_pid_file(tmp_path):
    assert daemon.stop_daemon(str(tmp_path / "missing.pid")) is False


def test_remove_pid_idempotent(tmp_path):
    pid_path = tmp_path / "test.pid"
    daemon.remove_pid(str(pid_path))
    pid_path.write_text("1", encoding="utf-8")
    daemon.remove_pid(str(pid_path))
    assert not pid_path.exists()
    daemon.remove_pid(str(pid_path))


def test_dated_log_path_with_extension():
    assert daemon.dated_log_path("/home/user/.ghswarm/ghswarm.log", "2026-07-18") == (
        "/home/user/.ghswarm/ghswarm-2026-07-18.log"
    )


def test_dated_log_path_without_extension():
    assert daemon.dated_log_path("/home/user/.ghswarm/ghswarm", "2026-07-18") == (
        "/home/user/.ghswarm/ghswarm-2026-07-18.log"
    )


def test_daemonize_raises_without_fork(monkeypatch):
    import os

    if hasattr(os, "fork"):
        monkeypatch.delattr(os, "fork", raising=False)
    with pytest.raises(RuntimeError, match="not supported"):
        daemon.daemonize("/tmp/log", "/tmp/pid")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork not available")
def test_daemonize_prints_grandchild_pid(tmp_path):
    import subprocess
    import sys

    log_path = str(tmp_path / "daemon.log")
    pid_path = str(tmp_path / "daemon.pid")

    code = f"""
import os
from ghswarm import daemon
daemon.daemonize({log_path!r}, {pid_path!r})
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    match = re.search(
        rf"ghswarm daemon started \(pid=(\d+), log={re.escape(log_path)}\)",
        result.stdout,
    )
    assert match
    daemon_pid = int(match.group(1))
    try:
        os.kill(daemon_pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(daemon_pid, signal.SIGTERM)
