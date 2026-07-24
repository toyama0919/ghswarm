"""Push notifications (Slack / macOS) on blocked / awaiting-clarification / completed transitions."""

from __future__ import annotations

import json
import platform
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import NotifyConfig
from .github import Issue
from .logging_utils import get_logger

log = get_logger("ghswarm.notify")


def _format_message(repo_name: str, issue: Issue, reason_code: str, detail: str) -> tuple[str, str]:
    """Build the notification body (for Slack and macOS)."""
    reason_part = f"{reason_code}: {detail}" if detail else reason_code
    core = f'[ghswarm] {repo_name} #{issue.number} "{issue.title}" is blocked ({reason_part})'
    url = issue.url or f"https://github.com/{repo_name}/issues/{issue.number}"
    slack_body = f"{core}\n{url}"
    macos_body = core
    return slack_body, macos_body


def _format_completed_message(repo_name: str, issue: Issue, pr_number: int) -> tuple[str, str]:
    """Build the completion notification body (for Slack and macOS)."""
    core = (
        f'[ghswarm] {repo_name} #{issue.number} "{issue.title}" '
        f"is complete (PR #{pr_number} merged)"
    )
    url = issue.url or f"https://github.com/{repo_name}/issues/{issue.number}"
    slack_body = f"{core}\n{url}"
    macos_body = core
    return slack_body, macos_body


@dataclass
class Notifier:
    """Send blocked / completed transitions to enabled notification channels. No-op if all disabled."""

    _config: NotifyConfig

    @classmethod
    def from_config(cls, config: NotifyConfig) -> Notifier:
        if not config.slack_webhook_url and not config.macos:
            return cls.disabled()
        return cls(_config=config)

    @classmethod
    def disabled(cls) -> Notifier:
        return cls(_config=NotifyConfig())

    @property
    def enabled(self) -> bool:
        return bool(self._config.slack_webhook_url or self._config.macos)

    def notify_blocked(self, issue: Issue, reason_code: str, detail: str, repo_name: str) -> None:
        if not self._config.slack_webhook_url and not self._config.macos:
            return

        slack_body, macos_body = _format_message(repo_name, issue, reason_code, detail)

        if self._config.slack_webhook_url:
            self._send_slack(slack_body)

        if self._config.macos:
            self._send_macos(macos_body)

    def notify_completed(self, issue: Issue, pr_number: int, repo_name: str) -> None:
        if not self._config.slack_webhook_url and not self._config.macos:
            return

        slack_body, macos_body = _format_completed_message(repo_name, issue, pr_number)

        if self._config.slack_webhook_url:
            self._send_slack(slack_body)

        if self._config.macos:
            self._send_macos(macos_body)

    def _send_slack(self, body: str) -> None:
        text = body
        if self._config.slack_mention:
            text = f"{self._config.slack_mention} {body}"
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            self._config.slack_webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log.warning("Failed to send Slack notification: %s", e)

    def _send_macos(self, body: str) -> None:
        if platform.system() != "Darwin":
            return
        escaped = body.replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{escaped}" with title "ghswarm"'
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("Failed to send macOS notification: %s", e)
