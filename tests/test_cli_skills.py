"""Tests for `ghswarm skills install` (bundled skills copied into .claude/skills)."""

from __future__ import annotations

import ghswarm.cli as cli

# Only user-facing skills are bundled in the package. Maintainer-only skills such as
# ghswarm-release live under the repo's .claude/skills and are not shipped.
_BUNDLED = {"ghswarm-spec", "ghswarm-check", "ghswarm-requirements"}


def test_skills_install_into_dir(tmp_path):
    dest = tmp_path / "skills"
    rc = cli.install_skills(dir=str(dest))
    assert rc == 0
    for name in _BUNDLED:
        assert (dest / name / "SKILL.md").is_file()
        assert (dest / name / "SKILL.md").read_text(encoding="utf-8") != ""


def test_skills_install_global_default_is_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))

    rc = cli.install_skills()
    assert rc == 0
    assert (home / ".claude" / "skills" / "ghswarm-spec" / "SKILL.md").is_file()


def test_skills_install_project_uses_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.install_skills(project=True)
    assert rc == 0
    assert (tmp_path / ".claude" / "skills" / "ghswarm-check" / "SKILL.md").is_file()


def test_skills_install_skips_existing_without_force(tmp_path):
    dest = tmp_path / "skills"
    existing = dest / "ghswarm-spec"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("custom", encoding="utf-8")

    rc = cli.install_skills(dir=str(dest), force=False)
    assert rc == 0
    # untouched because it already existed and --force was not given
    assert (existing / "SKILL.md").read_text(encoding="utf-8") == "custom"
    # the others were still installed
    assert (dest / "ghswarm-check" / "SKILL.md").is_file()


def test_skills_install_force_overwrites(tmp_path):
    dest = tmp_path / "skills"
    existing = dest / "ghswarm-spec"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("custom", encoding="utf-8")

    rc = cli.install_skills(dir=str(dest), force=True)
    assert rc == 0
    assert (existing / "SKILL.md").read_text(encoding="utf-8") != "custom"
