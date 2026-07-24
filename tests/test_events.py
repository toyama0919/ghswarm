"""Unit tests for EventLog (SQLite)."""

from __future__ import annotations

from datetime import datetime, timezone

from prpilot.events import EventLog, resolve_event_db_path, _truncate_detail
from prpilot.orchestrator import StepResult

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def _result(action: str = "implemented", detail: str = "done", issue: int = 42) -> StepResult:
    return StepResult(issue, action, detail)


def test_resolve_event_db_path_default():
    assert resolve_event_db_path("~/.prpilot/events.db").endswith(".prpilot/events.db")
    assert resolve_event_db_path("") == ""


def test_record_inserts_one_row(tmp_path):
    db = tmp_path / "events.db"
    el = EventLog(str(db))
    el.record("owner/repo", _result(), now=NOW)
    rows = el.read()
    assert len(rows) == 1
    assert rows[0]["ts"] == NOW.isoformat()
    assert rows[0]["repo"] == "owner/repo"
    assert rows[0]["issue"] == 42
    assert rows[0]["action"] == "implemented"
    assert rows[0]["detail"] == "done"


def test_record_skips_skipped_action(tmp_path):
    db = tmp_path / "events.db"
    el = EventLog(str(db))
    el.record("owner/repo", _result(action="skipped", detail="locked"), now=NOW)
    assert el.read() == []


def test_disabled_does_not_create_db(tmp_path):
    missing = tmp_path / "nope" / "events.db"
    el = EventLog("")
    el.record("owner/repo", _result(), now=NOW)
    assert not missing.exists()
    assert el.read() == []
    assert not el.enabled


def test_read_filters_and_order(tmp_path):
    db = tmp_path / "events.db"
    el = EventLog(str(db))
    t1 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 18, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    el.record("a/r", _result(issue=1), now=t1)
    el.record("a/r", _result(issue=2, action="reviewed"), now=t2)
    el.record("b/r", _result(issue=1, action="merged"), now=t3)

    all_rows = el.read()
    assert [r["issue"] for r in all_rows] == [1, 2, 1]
    assert [r["repo"] for r in all_rows] == ["b/r", "a/r", "a/r"]

    repo_rows = el.read(repo="a/r")
    assert len(repo_rows) == 2
    assert all(r["repo"] == "a/r" for r in repo_rows)

    issue_rows = el.read(issue=1)
    assert len(issue_rows) == 2

    limited = el.read(limit=2)
    assert len(limited) == 2
    assert limited[0]["ts"] == t3.isoformat()


def test_truncate_detail_boundary():
    assert len(_truncate_detail("x" * 500)) == 500
    truncated = _truncate_detail("x" * 501)
    assert len(truncated) == 501
    assert truncated.endswith("…")
    assert truncated[:500] == "x" * 500


def test_record_truncates_detail_in_db(tmp_path):
    db = tmp_path / "events.db"
    el = EventLog(str(db))
    el.record("owner/repo", _result(detail="y" * 600), now=NOW)
    rows = el.read()
    assert len(rows[0]["detail"]) == 501


def test_record_swallows_exceptions(tmp_path, caplog):
    el = EventLog(str(tmp_path))  # pass a directory as db_path to force failure
    with caplog.at_level("WARNING"):
        el.record("owner/repo", _result(), now=NOW)
    assert any("Failed to record event" in r.message for r in caplog.records)


def test_read_missing_db_returns_empty(tmp_path):
    el = EventLog(str(tmp_path / "missing.db"))
    assert el.read() == []
