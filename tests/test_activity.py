"""Unit tests for the activity module."""

from __future__ import annotations

from prpilot import activity


def test_activity_write_clear_read_roundtrip(tmp_path):
    activity_dir = str(tmp_path / "activity")
    activity.write_activity(activity_dir, "owner/repo", 42, "implement", 1234)
    entries = activity.read_activities(
        activity_dir,
        daemon_pid=1234,
        is_alive_fn=lambda _pid: True,
    )
    assert len(entries) == 1
    assert entries[0].repo == "owner/repo"
    assert entries[0].issue == 42
    assert entries[0].phase == "implement"
    assert entries[0].pid == 1234

    activity.clear_activity(activity_dir, "owner/repo")
    assert activity.read_activities(activity_dir) == []


def test_activity_filters_by_daemon_pid_and_alive(tmp_path):
    activity_dir = str(tmp_path / "activity")
    activity.write_activity(activity_dir, "owner/a", 1, "implement", 100)
    activity.write_activity(activity_dir, "owner/b", 2, "ai_review", 200)

    entries = activity.read_activities(
        activity_dir,
        daemon_pid=100,
        is_alive_fn=lambda pid: pid == 100,
    )
    assert len(entries) == 1
    assert entries[0].repo == "owner/a"


def test_sanitize_repo_name():
    assert activity.sanitize_repo_name("owner/repo") == "owner_repo"
