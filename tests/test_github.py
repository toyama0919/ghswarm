"""Tests for building Issue/PR objects from gh JSON output (gh is not launched)."""

from __future__ import annotations

import json
import subprocess

import ghswarm.github as github
from ghswarm.github import GitHub, PRStatus, _run_gh


def _payload(**overrides) -> dict:
    data = {
        "number": 1,
        "title": "Test Issue",
        "body": "Body",
        "labels": [{"name": "pm-agent"}],
        "state": "OPEN",
        "url": "https://example.com/1",
    }
    data.update(overrides)
    return data


def test_parse_issue_normalizes_uppercase_open_state():
    # gh issue view returns "OPEN". Without lowercasing, an open Issue would be
    # treated as closed and skipped (regression guard).
    issue = GitHub._parse_issue(_payload(state="OPEN"))
    assert issue.state == "open"


def test_parse_issue_normalizes_uppercase_closed_state():
    issue = GitHub._parse_issue(_payload(state="CLOSED"))
    assert issue.state == "closed"


def test_parse_issue_defaults_to_open_when_state_missing():
    payload = _payload()
    del payload["state"]
    assert GitHub._parse_issue(payload).state == "open"


def test_parse_issue_reads_label_names_and_fields():
    issue = GitHub._parse_issue(_payload(labels=[{"name": "pm-agent"}, {"name": "status: idle"}]))
    assert issue.labels == ["pm-agent", "status: idle"]
    assert issue.number == 1
    assert issue.title == "Test Issue"


def test_parse_issue_tolerates_null_body():
    assert GitHub._parse_issue(_payload(body=None)).body == ""


# -- _rollup ---------------------------------------------------------------


def test_rollup_none_when_empty():
    assert PRStatus._rollup([]) == "none"


def test_rollup_checkrun_lowercase_completed_success():
    # The REST API (commits/{sha}/check-runs) returns "completed"/"success" in lowercase.
    # Without case-insensitive handling it would be misclassified as pending (same kind of regression guard as #4).
    rollup = [{"status": "completed", "conclusion": "success"}]
    assert PRStatus._rollup(rollup) == "success"


def test_rollup_checkrun_lowercase_completed_failure():
    rollup = [{"status": "completed", "conclusion": "failure"}]
    assert PRStatus._rollup(rollup) == "failure"


def test_rollup_checkrun_in_progress_is_pending():
    rollup = [{"status": "in_progress", "conclusion": None}]
    assert PRStatus._rollup(rollup) == "pending"


def test_rollup_status_context_lowercase_state():
    rollup = [{"state": "success", "context": "ci"}]
    assert PRStatus._rollup(rollup) == "success"


# -- commit_checks / merge_commit_sha / close_issue ------------------------


def test_commit_checks_concatenates_check_runs_and_statuses(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_gh(args, cwd=None, input_text=None, env=None):
        calls.append(args)
        if "check-runs" in args[-1]:
            return json.dumps({"check_runs": [{"status": "completed", "conclusion": "success"}]})
        if args[-1].endswith("/status"):
            return json.dumps({"statuses": [{"state": "success", "context": "ci"}]})
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    gh = GitHub("owner/repo")
    assert gh.commit_checks("abc123") == "success"
    assert calls[0] == ["api", "repos/owner/repo/commits/abc123/check-runs"]
    assert calls[1] == ["api", "repos/owner/repo/commits/abc123/status"]


def test_commit_checks_none_when_no_checks(monkeypatch):
    def fake_run_gh(args, cwd=None, input_text=None, env=None):
        if "check-runs" in args[-1]:
            return json.dumps({"check_runs": []})
        return json.dumps({"statuses": []})

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    assert GitHub("owner/repo").commit_checks("sha") == "none"


def test_commit_checks_pending_when_in_progress(monkeypatch):
    def fake_run_gh(args, cwd=None, input_text=None, env=None):
        if "check-runs" in args[-1]:
            return json.dumps({"check_runs": [{"status": "in_progress", "conclusion": None}]})
        return json.dumps({"statuses": []})

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    assert GitHub("owner/repo").commit_checks("sha") == "pending"


def test_commit_checks_failure_wins(monkeypatch):
    def fake_run_gh(args, cwd=None, input_text=None, env=None):
        if "check-runs" in args[-1]:
            return json.dumps({"check_runs": [{"status": "completed", "conclusion": "failure"}]})
        return json.dumps({"statuses": [{"state": "success", "context": "ci"}]})

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    assert GitHub("owner/repo").commit_checks("sha") == "failure"


def test_merge_commit_sha_reads_oid(monkeypatch):
    monkeypatch.setattr(
        github, "_run_gh", lambda *a, **k: json.dumps({"mergeCommit": {"oid": "deadbeef"}})
    )
    assert GitHub("owner/repo").merge_commit_sha(9) == "deadbeef"


def test_merge_commit_sha_empty_when_not_merged(monkeypatch):
    monkeypatch.setattr(github, "_run_gh", lambda *a, **k: json.dumps({"mergeCommit": None}))
    assert GitHub("owner/repo").merge_commit_sha(9) == ""


def test_close_issue_invokes_gh_issue_close(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(github, "_run_gh", lambda args, **k: calls.append(args) or "")
    GitHub("owner/repo").close_issue(7)
    assert calls == [["issue", "close", "7", "--repo", "owner/repo"]]


# -- list_open_issues ------------------------------------------------------


def test_list_open_issues_builds_filter_flags(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_gh(args, cwd=None, input_text=None, env=None):
        calls.append(args)
        return "[]"

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    GitHub("owner/repo").list_open_issues(
        labels=["pm-agent", "priority: high"],
        assignee="my-bot",
        milestone="Sprint 42",
    )
    args = calls[0]
    assert args[:6] == ["issue", "list", "--repo", "owner/repo", "--state", "open"]
    pairs = list(zip(args, args[1:]))
    assert ("--label", "pm-agent") in pairs
    assert ("--label", "priority: high") in pairs
    assert ("--assignee", "my-bot") in pairs
    assert ("--milestone", "Sprint 42") in pairs


def test_list_open_issues_no_filters_when_empty(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_gh(args, cwd=None, input_text=None, env=None):
        calls.append(args)
        return "[]"

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    GitHub("owner/repo").list_open_issues()
    args = calls[0]
    assert "--label" not in args
    assert "--assignee" not in args
    assert "--milestone" not in args


def test_list_open_issues_milestone_coerced_to_str(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_gh(args, cwd=None, input_text=None, env=None):
        calls.append(args)
        return "[]"

    monkeypatch.setattr(github, "_run_gh", fake_run_gh)
    GitHub("owner/repo").list_open_issues(milestone="42")
    pairs = list(zip(calls[0], calls[0][1:]))
    assert ("--milestone", "42") in pairs


# -- failed_gha_ci_logs ----------------------------------------------------


def test_extract_gha_run_id_from_actions_url():
    link = "https://github.com/owner/repo/actions/runs/123456789/job/987"
    assert github._extract_gha_run_id(link) == "123456789"


def test_extract_gha_run_id_returns_none_for_non_gha():
    assert github._extract_gha_run_id("https://circleci.com/gh/owner/repo/42") is None


def test_gha_run_ids_from_failed_checks_filters_and_dedupes():
    checks = [
        {
            "name": "test",
            "bucket": "fail",
            "link": "https://github.com/o/r/actions/runs/111",
        },
        {
            "name": "lint",
            "bucket": "fail",
            "link": "https://github.com/o/r/actions/runs/111",
        },
        {"name": "circle", "bucket": "fail", "link": "https://circleci.com/gh/o/r/9"},
        {"name": "ok", "bucket": "pass", "link": "https://github.com/o/r/actions/runs/222"},
    ]
    assert github._gha_run_ids_from_failed_checks(checks) == ["111"]


def test_failed_gha_ci_logs_returns_combined_logs(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_gh_ignore_exit(args, cwd=None, env=None):
        calls.append(args)
        if args[0] == "pr":
            return (
                1,
                json.dumps(
                    [
                        {
                            "name": "ci",
                            "bucket": "fail",
                            "link": "https://github.com/owner/repo/actions/runs/42",
                        }
                    ]
                ),
                "",
            )
        if args[0] == "run":
            return 0, "FAILED step output\n", ""
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(github, "_run_gh_ignore_exit", fake_run_gh_ignore_exit)
    result = GitHub("owner/repo").failed_gha_ci_logs(7)
    assert result == "--- run 42 ---\nFAILED step output\n"
    assert calls[0][:3] == ["pr", "checks", "7"]
    assert calls[1][:4] == ["run", "view", "42", "--repo"]


def test_failed_gha_ci_logs_non_gha_only_returns_none(monkeypatch):
    def fake_run_gh_ignore_exit(args, cwd=None, env=None):
        return (
            1,
            json.dumps(
                [
                    {
                        "name": "circle",
                        "bucket": "fail",
                        "link": "https://circleci.com/gh/owner/repo/99",
                    }
                ]
            ),
            "",
        )

    monkeypatch.setattr(github, "_run_gh_ignore_exit", fake_run_gh_ignore_exit)
    assert GitHub("owner/repo").failed_gha_ci_logs(7) is None


def test_failed_gha_ci_logs_log_fetch_failure_returns_empty(monkeypatch):
    def fake_run_gh_ignore_exit(args, cwd=None, env=None):
        if args[0] == "pr":
            return (
                1,
                json.dumps(
                    [
                        {
                            "name": "ci",
                            "bucket": "fail",
                            "link": "https://github.com/owner/repo/actions/runs/55",
                        }
                    ]
                ),
                "",
            )
        return 1, "", "log unavailable"

    monkeypatch.setattr(github, "_run_gh_ignore_exit", fake_run_gh_ignore_exit)
    assert GitHub("owner/repo").failed_gha_ci_logs(7) == ""


def test_failed_gha_ci_logs_ignores_pr_checks_nonzero_exit(monkeypatch):
    def fake_run_gh_ignore_exit(args, cwd=None, env=None):
        return 1, "[]", "some checks failed"

    monkeypatch.setattr(github, "_run_gh_ignore_exit", fake_run_gh_ignore_exit)
    assert GitHub("owner/repo").failed_gha_ci_logs(7) is None


# -- _run_gh env injection -------------------------------------------------


def test_run_gh_passes_merged_env_to_subprocess(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(github.os, "environ", {"EXISTING": "keep", "OVERRIDE": "old"})
    monkeypatch.setattr(github.subprocess, "run", fake_run)

    assert _run_gh(["version"], env={"OVERRIDE": "new", "EXTRA": "added"}) == "ok"
    assert captured["env"] == {
        "EXISTING": "keep",
        "OVERRIDE": "new",
        "EXTRA": "added",
    }


def test_run_gh_inherits_env_when_unset(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    assert _run_gh(["version"]) == "ok"
    assert captured["env"] is None


def test_run_gh_inherits_env_when_empty_dict(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    assert _run_gh(["version"], env={}) == "ok"
    assert captured["env"] is None
