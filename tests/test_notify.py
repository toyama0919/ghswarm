"""Tests for blocked notifications (Slack / dedup)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from ghswarm import state as st
from ghswarm.config import AgentConfig, RepoConfig, NotifyConfig
from ghswarm.events import EventLog
from ghswarm.executor import ExecResult
from ghswarm.github import Issue, PRStatus
from ghswarm.notify import Notifier, _format_completed_message, _format_message
from ghswarm.orchestrator import Orchestrator


class FakeGitHub:
    def __init__(self, body: str = "body"):
        self.body = body
        self.comments: list[str] = []
        self.released: list[str] = []

    def get_issue(self, number: int) -> Issue:
        return Issue(
            number=number,
            title="add blocked notification",
            body=self.body,
            labels=[],
            url="https://github.com/toyama0919/ghswarm/issues/42",
        )

    def set_body(self, number: int, body: str) -> None:
        self.body = body

    def comment(self, number: int, body: str) -> None:
        self.comments.append(body)

    def add_label(self, number: int, label: str) -> None:
        if label.startswith("status:"):
            self.released.append(label)

    def remove_label(self, number: int, label: str) -> None:
        pass

    def ensure_label(self, label: str) -> None:
        pass

    def close_issue(self, number: int) -> None:
        pass


class CiFakeGitHub(FakeGitHub):
    def __init__(self, status: PRStatus, body: str = "body"):
        super().__init__(body)
        self.status = status

    def pr_status(self, number: int) -> PRStatus:
        return self.status


class FakeGit:
    def ensure_worktree(self, branch: str, base: str, path: str) -> str:
        return path


class FakeWorktreeGit:
    def __init__(self, cwd: str = "/tmp/worktree"):
        self.cwd = cwd

    def savepoint(self, message: str) -> bool:
        return True


def _notify_config(**kwargs) -> NotifyConfig:
    defaults = {
        "slack_webhook_url": "https://hooks.slack.com/services/T/B/X",
        "slack_mention": "<!here>",
        "macos": False,
    }
    defaults.update(kwargs)
    return NotifyConfig(**defaults)


def _orch_with_notify(monkeypatch, *, notify: NotifyConfig | None = None) -> Orchestrator:
    monkeypatch.setattr("ghswarm.orchestrator.lbl.acquire", lambda *a, **k: None)
    monkeypatch.setattr(
        "ghswarm.orchestrator.execute_with_self_healing",
        lambda *a, **k: ExecResult(ok=False, output="fail", attempts=1, reason="verify_failed"),
    )

    cfg = RepoConfig(
        agents={
            "implement": AgentConfig(name="implement", commands=["noop"]),
            "review": AgentConfig(name="review", commands=["noop"]),
        },
        notify=notify or _notify_config(),
    )
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = cfg
    orch.cwd = "/tmp"
    orch.repo_name = "toyama0919/ghswarm"
    orch.gh = FakeGitHub()
    orch.git = FakeGit()
    orch._git_for = lambda path: FakeWorktreeGit()
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.test_command = ""
    orch.dry_run = False
    orch.agent_names = ["implement", "review"]
    orch._event_log = EventLog("")
    orch._notifier = Notifier.from_config(cfg.notify)
    return orch


def test_notifier_disabled_is_noop():
    notifier = Notifier.disabled()
    issue = Issue(number=1, title="t", body="", url="https://github.com/o/r/issues/1")
    with patch("ghswarm.notify.urllib.request.urlopen") as urlopen:
        notifier.notify_blocked(issue, "ci_failed", "detail", "o/r")
        urlopen.assert_not_called()


def test_format_message_includes_reason_and_url():
    issue = Issue(
        number=42,
        title="Title",
        body="",
        url="https://github.com/toyama0919/ghswarm/issues/42",
    )
    slack, macos = _format_message(
        "toyama0919/ghswarm", issue, "implement_failed", "verify failed 3 times"
    )
    assert "toyama0919/ghswarm #42" in slack
    assert '"Title"' in slack
    assert "implement_failed: verify failed 3 times" in slack
    assert "https://github.com/toyama0919/ghswarm/issues/42" in slack
    assert "implement_failed: verify failed 3 times" in macos


@patch("ghswarm.notify.urllib.request.urlopen")
def test_slack_post_includes_mention_at_start(urlopen):
    urlopen.return_value = MagicMock(read=lambda: b"ok")
    issue = Issue(number=42, title="Title", body="", url="https://github.com/o/r/issues/42")
    notifier = Notifier.from_config(_notify_config(slack_mention="<!here>"))

    notifier.notify_blocked(issue, "clarification", "question", "o/r")

    req = urlopen.call_args[0][0]
    payload = json.loads(req.data.decode("utf-8"))
    assert payload["text"].startswith("<!here> [ghswarm]")


BODY = """## Task breakdown

- [ ] Task A
"""

SPEC_PATH = ".specs/test-spec.md"


@patch("ghswarm.notify.urllib.request.urlopen")
def test_implement_failed_triggers_slack_post(urlopen, monkeypatch, tmp_path):
    urlopen.return_value = MagicMock(read=lambda: b"ok")
    spec_file = tmp_path / SPEC_PATH
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text("# test\n", encoding="utf-8")
    orch = _orch_with_notify(monkeypatch)
    orch.gh = FakeGitHub(BODY)
    wt = FakeWorktreeGit(str(tmp_path))
    orch._ensure_worktree_git = lambda number, branch: wt
    orch._record_busy_lease = lambda issue, state: None
    orch._persist = lambda issue, state: None
    orch._test_command_for = lambda state, worktree: ""
    orch._spec_block = lambda state, worktree: ""
    issue = orch.gh.get_issue(42)
    state = st.IssueState(branch_name="issue-42", next_action="implement", spec_path=SPEC_PATH)

    result = orch._implement(issue, state, resume=False)

    assert result.action == "failed"
    assert urlopen.call_count == 1
    payload = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
    assert "implement_failed" in payload["text"]


@patch("ghswarm.notify.urllib.request.urlopen")
def test_ci_failed_without_auto_fix_triggers_slack_post(urlopen):
    urlopen.return_value = MagicMock(read=lambda: b"ok")
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={"implement": AgentConfig(name="implement", commands=["noop"])},
        auto_fix_ci=False,
        notify=_notify_config(),
    )
    orch.cwd = "/tmp"
    orch.repo_name = "toyama0919/ghswarm"
    status = PRStatus(
        number=9,
        state="OPEN",
        mergeable="MERGEABLE",
        checks="failure",
        review_decision="",
        url="https://github.com/o/r/pull/9",
    )
    orch.gh = CiFakeGitHub(status)
    orch.git = FakeGit()
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.worktree_setup = ""
    orch.base_branch = "main"
    orch.test_command = ""
    orch.dry_run = False
    orch.agent_names = ["implement"]
    orch._notifier = Notifier.from_config(orch.cfg.notify)
    orch._persist = lambda issue, state: None
    state = st.IssueState(branch_name="issue-7", pr_number=9, next_action="wait_ci")
    issue = orch.gh.get_issue(7)

    result = orch._wait_ci(issue, state)

    assert result.action == "failed"
    assert urlopen.call_count == 1
    payload = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
    assert "ci_failed" in payload["text"]


@patch("ghswarm.notify.urllib.request.urlopen")
def test_same_reason_blocked_only_notifies_once(urlopen):
    urlopen.return_value = MagicMock(read=lambda: b"ok")
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={"implement": AgentConfig(name="implement", commands=["noop"])},
        notify=_notify_config(),
    )
    orch.cwd = "/tmp"
    orch.repo_name = "toyama0919/ghswarm"
    orch.gh = FakeGitHub()
    orch.agent_names = ["implement"]
    orch._notifier = Notifier.from_config(orch.cfg.notify)
    orch._persist = lambda issue, state: None

    issue = orch.gh.get_issue(42)
    state = st.IssueState(branch_name="issue-42", phase="blocked")

    orch._enter_blocked(issue, state, "ci_failed", "round 1")
    orch._enter_blocked(issue, state, "ci_failed", "round 2")

    assert urlopen.call_count == 1


@patch("ghswarm.notify.urllib.request.urlopen")
def test_resume_from_blocked_clears_dedup_and_renotifies(urlopen):
    urlopen.return_value = MagicMock(read=lambda: b"ok")
    captured: dict[str, str | None] = {}

    class BlockedFakeGitHub(FakeGitHub):
        def get_issue(self, number: int) -> Issue:
            issue = super().get_issue(number)
            return Issue(
                number=issue.number,
                title=issue.title,
                body=issue.body,
                labels=["status: blocked"],
                url=issue.url,
            )

    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={"implement": AgentConfig(name="implement", commands=["noop"])},
        notify=_notify_config(),
    )
    orch.cwd = "/tmp"
    orch.repo_name = "toyama0919/ghswarm"
    state = st.IssueState(
        branch_name="issue-7",
        pr_number=9,
        next_action="wait_ci",
        phase="blocked",
        last_notified_reason="ci_failed",
    )
    orch.gh = BlockedFakeGitHub(st.write_state("body", state))
    orch.agent_names = ["implement"]
    orch._notifier = Notifier.from_config(orch.cfg.notify)
    orch._persist = lambda issue, state: None

    def intercept_wait_ci(issue, state):
        captured["last_notified_reason"] = state.last_notified_reason
        orch._enter_blocked(issue, state, "ci_failed", "again")
        from ghswarm.orchestrator import StepResult

        return StepResult(issue.number, "failed", "ci_failed")

    orch._wait_ci = intercept_wait_ci

    orch.process(7, resume=True)

    assert captured["last_notified_reason"] is None
    assert urlopen.call_count == 1


@patch("ghswarm.notify.urllib.request.urlopen")
def test_reblocked_after_idle_notifies_again(urlopen):
    urlopen.return_value = MagicMock(read=lambda: b"ok")
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={"implement": AgentConfig(name="implement", commands=["noop"])},
        notify=_notify_config(),
    )
    orch.cwd = "/tmp"
    orch.repo_name = "toyama0919/ghswarm"
    orch.gh = FakeGitHub()
    orch.agent_names = ["implement"]
    orch._notifier = Notifier.from_config(orch.cfg.notify)
    orch._persist = lambda issue, state: None

    issue = orch.gh.get_issue(42)
    state = st.IssueState(branch_name="issue-42", phase="blocked")

    orch._enter_blocked(issue, state, "ci_failed", "first")
    orch._release_idle(issue, state)
    orch._enter_blocked(issue, state, "ci_failed", "second")

    assert urlopen.call_count == 2


@patch("ghswarm.notify.urllib.request.urlopen")
def test_slack_failure_does_not_raise(urlopen):
    import urllib.error

    urlopen.side_effect = urllib.error.URLError("network down")
    issue = Issue(number=1, title="t", body="", url="https://github.com/o/r/issues/1")
    notifier = Notifier.from_config(_notify_config())

    notifier.notify_blocked(issue, "ci_failed", "detail", "o/r")


def test_notify_config_defaults_when_unset(tmp_path):
    from ghswarm.config import load_config

    path = tmp_path / "ghswarm.yaml"
    path.write_text(
        """
repositories:
  test:
    repo: owner/repo
    path: /tmp/repo
    agents:
      implement:
        command: "noop {prompt}"
      review:
        command: "noop {prompt}"
""",
        encoding="utf-8",
    )
    app = load_config(str(path))
    assert app.repositories["test"].notify == NotifyConfig()


def test_last_notified_reason_roundtrip():
    state = st.IssueState(branch_name="issue-1", last_notified_reason="ci_failed")
    body = st.write_state("body", state)
    restored = st.parse_state(body, 1)
    assert restored.last_notified_reason == "ci_failed"


def test_last_notified_reason_missing_defaults_none():
    state = st.parse_state("body", 1)
    assert state.last_notified_reason is None


def test_format_completed_message_includes_pr_and_url():
    issue = Issue(
        number=42,
        title="Title",
        body="",
        url="https://github.com/toyama0919/ghswarm/issues/42",
    )
    slack, macos = _format_completed_message("toyama0919/ghswarm", issue, 99)
    assert "toyama0919/ghswarm #42" in slack
    assert '"Title"' in slack
    assert "is complete (PR #99 merged)" in slack
    assert "https://github.com/toyama0919/ghswarm/issues/42" in slack
    assert "is complete (PR #99 merged)" in macos
    assert "blocked" not in slack


@patch("ghswarm.notify.platform.system", return_value="Darwin")
@patch("ghswarm.notify.subprocess.run")
@patch("ghswarm.notify.urllib.request.urlopen")
def test_notify_completed_sends_to_both_channels(urlopen, subprocess_run, _darwin):
    urlopen.return_value = MagicMock(read=lambda: b"ok")
    issue = Issue(
        number=42,
        title="Title",
        body="",
        url="https://github.com/toyama0919/ghswarm/issues/42",
    )
    notifier = Notifier.from_config(_notify_config(macos=True))

    notifier.notify_completed(issue, 99, "toyama0919/ghswarm")

    assert urlopen.call_count == 1
    payload = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
    assert "is complete (PR #99 merged)" in payload["text"]
    assert "https://github.com/toyama0919/ghswarm/issues/42" in payload["text"]
    assert subprocess_run.call_count == 1
    script = subprocess_run.call_args[0][0]
    assert "is complete (PR #99 merged)" in script[2]


def test_notify_completed_disabled_channels_is_noop():
    notifier = Notifier.disabled()
    issue = Issue(number=1, title="t", body="", url="https://github.com/o/r/issues/1")
    with patch("ghswarm.notify.urllib.request.urlopen") as urlopen:
        notifier.notify_completed(issue, 9, "o/r")
        urlopen.assert_not_called()


@patch("ghswarm.orchestrator.lbl.release")
@patch("ghswarm.notify.urllib.request.urlopen")
def test_finalize_merged_sends_completion_notification(urlopen, release):
    urlopen.return_value = MagicMock(read=lambda: b"ok")
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={"implement": AgentConfig(name="implement", commands=["noop"])},
        notify=_notify_config(on_completed=True),
    )
    orch.cwd = "/tmp"
    orch.repo_name = "toyama0919/ghswarm"
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.gh = FakeGitHub()
    orch.git = MagicMock()
    orch.git.remove_worktree = MagicMock()
    orch.agent_names = ["implement"]
    orch._notifier = Notifier.from_config(orch.cfg.notify)
    orch._persist = lambda issue, state: None
    issue = orch.gh.get_issue(42)
    state = st.IssueState(
        branch_name="issue-42",
        pr_number=99,
        pr_url="https://github.com/toyama0919/ghswarm/pull/99",
    )

    result = orch._finalize_merged(issue, state)

    assert result.action == "merged"
    assert urlopen.call_count == 1
    payload = json.loads(urlopen.call_args[0][0].data.decode("utf-8"))
    assert "is complete (PR #99 merged)" in payload["text"]


@patch("ghswarm.orchestrator.lbl.release")
@patch("ghswarm.notify.urllib.request.urlopen")
def test_finalize_merged_skips_notification_when_on_completed_false(urlopen, release):
    urlopen.return_value = MagicMock(read=lambda: b"ok")
    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RepoConfig(
        agents={"implement": AgentConfig(name="implement", commands=["noop"])},
        notify=_notify_config(on_completed=False),
    )
    orch.cwd = "/tmp"
    orch.repo_name = "toyama0919/ghswarm"
    orch.worktree_dir = Path("/tmp/worktrees")
    orch.gh = FakeGitHub()
    orch.git = MagicMock()
    orch.git.remove_worktree = MagicMock()
    orch.agent_names = ["implement"]
    orch._notifier = Notifier.from_config(orch.cfg.notify)
    orch._persist = lambda issue, state: None
    issue = orch.gh.get_issue(42)
    state = st.IssueState(
        branch_name="issue-42",
        pr_number=99,
        pr_url="https://github.com/toyama0919/ghswarm/pull/99",
    )

    orch._finalize_merged(issue, state)

    urlopen.assert_not_called()
