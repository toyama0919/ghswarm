"""Click wiring tests for the public command-line interface."""

from __future__ import annotations

from click.testing import CliRunner

import ghswarm.cli as cli


def test_every_command_help_exits_successfully():
    commands = [
        ["init"],
        ["run"],
        ["loop"],
        ["status"],
        ["history"],
        ["config"],
        ["skills"],
        ["skills", "install"],
    ]
    runner = CliRunner()

    for command in commands:
        result = runner.invoke(cli.app, [*command, "--help"])
        assert result.exit_code == 0, result.output


def test_main_accepts_short_help():
    assert cli.main(["-h"]) == 0


def test_run_help_documents_step_completion_repo_and_cwd():
    result = CliRunner().invoke(cli.app, ["run", "--help"])

    assert result.exit_code == 0
    assert "--step" in result.output
    assert "completion" in result.output
    assert "--repo" in result.output
    assert "cwd" in result.output


def test_loop_daemon_and_once_is_processing_error(monkeypatch):
    monkeypatch.setattr(cli, "_load", lambda _config_path: cli.AppConfig())

    result = CliRunner().invoke(cli.app, ["loop", "--daemon", "--once"])

    assert result.exit_code == 1


def test_skills_install_global_and_project_is_usage_error():
    result = CliRunner().invoke(cli.app, ["skills", "install", "--global", "--project"])

    assert result.exit_code == 2


def test_run_non_numeric_issue_is_usage_error():
    result = CliRunner().invoke(cli.app, ["run", "abc"])

    assert result.exit_code == 2


def test_unknown_command_is_usage_error():
    result = CliRunner().invoke(cli.app, ["unknown"])

    assert result.exit_code == 2
    assert "No such command" in result.output
