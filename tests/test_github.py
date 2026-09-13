"""Tests for building Issue/PR objects from gh JSON output (gh is not launched)."""

from __future__ import annotations

import json
import subprocess

import pytest

import ghswarm.github as github
from ghswarm.github import GitHub, GitHubError, PRStatus, _run_gh, is_transient_error


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


# -- PRStatus.ready_to_merge / human approvals ----------------------------


@pytest.mark.parametrize(
    ("mode", "review_decision", "expected"),
    [
        ("none", "", True),
        ("none", "CHANGES_REQUESTED", True),
        ("any", "APPROVED", True),
        ("any", "", False),
        ("human", "APPROVED", True),
        ("human", "", False),
    ],
)
def test_ready_to_merge_uses_approval_mode(mode, review_decision, expected):
    status = PRStatus(
        number=1,
        state="OPEN",
        mergeable="MERGEABLE",
        review_decision=review_decision,
        checks="success",
    )
    assert status.ready_to_merge(mode) is expected


def test_ready_to_merge_rejects_non_open_conflicting_or_pending_statuses():
    base = dict(number=1, review_decision="APPROVED")
    assert not PRStatus(
        state="CLOSED", mergeable="MERGEABLE", checks="success", **base
    ).ready_to_merge("none")
    assert not PRStatus(
        state="OPEN", mergeable="CONFLICTING", checks="success", **base
    ).ready_to_merge("none")
    assert not PRStatus(
        state="OPEN", mergeable="MERGEABLE", checks="pending", **base
    ).ready_to_merge("none")


@pytest.mark.parametrize(
    "reviews",
    [
        [{"user": {"login": "coderabbit[bot]", "type": "Bot"}, "state": "APPROVED"}],
        [{"user": {"login": "ci[bot]", "type": "User"}, "state": "APPROVED"}],
    ],
)
def test_has_human_approval_rejects_bots(monkeypatch, reviews):
    monkeypatch.setattr(github.GitHub, "_api_json", lambda self, path: reviews)
    assert not GitHub("owner/repo").has_human_approval(7)


def test_has_human_approval_accepts_human_approval(monkeypatch):
    reviews = [{"user": {"login": "alice", "type": "User"}, "state": "APPROVED"}]
    monkeypatch.setattr(github.GitHub, "_api_json", lambda self, path: reviews)
    assert GitHub("owner/repo").has_human_approval(7)


def test_has_human_approval_keeps_latest_non_commented_review_per_user(monkeypatch):
    reviews = [
        {"user": {"login": "alice", "type": "User"}, "state": "APPROVED"},
        {"user": {"login": "alice", "type": "User"}, "state": "COMMENTED"},
        {"user": {"login": "bob", "type": "User"}, "state": "COMMENTED"},
        {"user": {"login": "bob", "type": "User"}, "state": "APPROVED"},
    ]
    monkeypatch.setattr(github.GitHub, "_api_json", lambda self, path: reviews)
    assert GitHub("owner/repo").has_human_approval(7)


@pytest.mark.parametrize("state", ["DISMISSED", "PENDING"])
def test_has_human_approval_dismissed_or_pending_latest_does_not_count(monkeypatch, state):
    reviews = [
        {"user": {"login": "alice", "type": "User"}, "state": "APPROVED"},
        {"user": {"login": "alice", "type": "User"}, "state": state},
    ]
    monkeypatch.setattr(github.GitHub, "_api_json", lambda self, path: reviews)
    assert not GitHub("owner/repo").has_human_approval(7)


def test_has_human_approval_uses_single_non_paginated_reviews_request(monkeypatch):
    calls: list[str] = []

    def fake_api(self, path):
        calls.append(path)
        return []

    monkeypatch.setattr(github.GitHub, "_api_json", fake_api)
    assert not GitHub("owner/repo").has_human_approval(7)
    assert calls == ["repos/owner/repo/pulls/7/reviews?per_page=100"]


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


# -- transient retry --------------------------------------------------------

# The reset seen in the field: gh exits 1 with only this on stderr, and without a retry
# a single hiccup aborted the whole cycle mid label transition.
_RESET = (
    'Post "https://api.github.com/graphql": read tcp '
    "172.16.0.2:65392->20.27.177.116:443: read: connection reset by peer"
)


def _responses(monkeypatch, results, sleeps=None):
    """Make subprocess.run return the given (returncode, stderr) pairs in order."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        code, stderr = results[min(len(calls), len(results) - 1)]
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, code, stdout="ok", stderr=stderr)

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    recorded = sleeps if sleeps is not None else []
    monkeypatch.setattr(github, "_sleep", recorded.append)
    return calls


def test_is_transient_error_matches_network_and_5xx():
    assert is_transient_error(_RESET)
    assert is_transient_error("dial tcp: i/o timeout")
    assert is_transient_error("net/http: TLS handshake timeout")
    assert is_transient_error("HTTP 502: Bad gateway")
    assert is_transient_error("Service Unavailable")


def test_is_transient_error_ignores_ordinary_failures():
    assert not is_transient_error("")
    assert not is_transient_error("could not add label: 'status: idle' not found")
    assert not is_transient_error("GraphQL: Resource not accessible by integration")
    assert not is_transient_error("API rate limit exceeded")


def test_run_gh_retries_transient_error_then_succeeds(monkeypatch):
    calls = _responses(monkeypatch, [(1, _RESET), (0, "")])
    assert _run_gh(["issue", "edit", "827", "--add-label", "status: idle"]) == "ok"
    assert len(calls) == 2


def test_run_gh_gives_up_after_the_retry_budget_with_backoff(monkeypatch):
    sleeps: list[float] = []
    calls = _responses(monkeypatch, [(1, _RESET)], sleeps)
    with pytest.raises(GitHubError):
        _run_gh(["issue", "edit", "827"])
    assert len(calls) == 3  # first attempt + 2 retries
    assert sleeps == [2.0, 4.0]  # exponential backoff


def test_run_gh_does_not_retry_ordinary_failures(monkeypatch):
    calls = _responses(monkeypatch, [(1, "GraphQL: Could not resolve to an Issue")])
    with pytest.raises(GitHubError):
        _run_gh(["issue", "view", "1"])
    assert len(calls) == 1


def test_run_gh_honours_retries_zero_for_non_idempotent_commands(monkeypatch):
    # Retrying a comment that actually reached GitHub would post it twice.
    calls = _responses(monkeypatch, [(1, _RESET)])
    with pytest.raises(GitHubError):
        _run_gh(["issue", "comment", "1"], retries=0)
    assert len(calls) == 1


def test_comment_and_merge_do_not_retry(monkeypatch):
    calls = _responses(monkeypatch, [(1, _RESET)])
    gh = GitHub("owner/repo")
    with pytest.raises(GitHubError):
        gh.comment(1, "body")
    with pytest.raises(GitHubError):
        gh.pr_comment(1, "body")
    with pytest.raises(GitHubError):
        gh.merge_pr(1)
    assert len(calls) == 3


def test_add_label_retries_transient_error(monkeypatch):
    calls = _responses(monkeypatch, [(1, _RESET), (0, "")])
    gh = GitHub("owner/repo")
    gh._known_labels = {"status: idle"}
    gh.add_label(827, "status: idle")
    assert len(calls) == 2
