"""Tests for `ghswarm init` (bundled templates and regression of the empty-file generation bug)."""

from __future__ import annotations

import argparse

import yaml

import ghswarm.cli as cli
from ghswarm.config import load_config


def _args(output, force=False):
    return argparse.Namespace(output=str(output) if output is not None else None, force=force)


def test_init_writes_template(tmp_path):
    dest = tmp_path / "ghswarm.yaml"
    rc = cli.cmd_init(_args(dest))
    assert rc == 0
    written = dest.read_text(encoding="utf-8")
    assert written != ""
    assert written == cli.CONFIG_TEMPLATE.read_text(encoding="utf-8")
    parsed = yaml.safe_load(written)
    assert isinstance(parsed, dict)
    assert "repositories" in parsed
    assert "defaults" in parsed


def test_init_missing_template_errors(tmp_path, monkeypatch):
    dest = tmp_path / "ghswarm.yaml"
    monkeypatch.setattr(cli, "CONFIG_TEMPLATE", tmp_path / "no-such.yaml")
    rc = cli.cmd_init(_args(dest))
    assert rc == 1
    assert not dest.exists()


def test_init_existing_file_protected(tmp_path):
    dest = tmp_path / "ghswarm.yaml"
    dest.write_text("existing", encoding="utf-8")
    rc = cli.cmd_init(_args(dest, force=False))
    assert rc == 1
    assert dest.read_text(encoding="utf-8") == "existing"


def test_init_default_output_is_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", home / ".ghswarm.yaml")

    rc = cli.cmd_init(_args(None, force=False))
    assert rc == 0
    dest = home / ".ghswarm.yaml"
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == cli.CONFIG_TEMPLATE.read_text(encoding="utf-8")


def test_init_default_output_is_loadable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    dest = home / ".ghswarm.yaml"
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", dest)

    rc = cli.cmd_init(_args(None, force=False))
    assert rc == 0
    app = load_config(str(dest))
    assert app.source_path == dest
    assert "main" in app.repositories
