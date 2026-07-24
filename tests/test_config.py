"""Tests for config loading (focused on base_branch resolution)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import ghswarm.orchestrator as orchestrator
from ghswarm.config import (
    AppConfig,
    ConfigError,
    LabelConfig,
    SandboxConfig,
    TargetFilter,
    _DEFAULT_TRANSIENT_ERROR_PATTERNS,
    _deep_merge,
    load_app_config,
    load_config,
    resolve_worktree_dir,
)

_AGENTS = """
agents:
  implement:
    command: "claude -p {prompt} --model opus"
  review:
    command: "claude -p {prompt} --model sonnet"
"""


def _write_config(
    tmp_path,
    *,
    repo_extra: str = "",
    defaults_extra: str = "",
    top_extra: str = "",
    alias: str = "test",
    repo: str = "owner/repo",
    path: str | None = None,
) -> str:
    repo_path = path or str(tmp_path / "repo")
    (tmp_path / "repo").mkdir(exist_ok=True)
    config_path = tmp_path / "ghswarm.yaml"
    body = f"""\
max_parallel_repos: 3
{top_extra}defaults:
{textwrap.indent(textwrap.dedent(_AGENTS) + textwrap.dedent(defaults_extra), "  ")}
repositories:
  {alias}:
    repo: {repo}
    path: {repo_path}
{textwrap.indent(textwrap.dedent(repo_extra), "    ")}
"""
    config_path.write_text(body, encoding="utf-8")
    return str(config_path)


def _write_raw(tmp_path, body: str) -> str:
    path = tmp_path / "ghswarm.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


def _repo(app: AppConfig, alias: str = "test"):
    return app.repositories[alias]


def test_base_branch_explicit(tmp_path):
    app = load_config(_write_config(tmp_path, repo_extra='base_branch: "develop"\n'))
    assert _repo(app).base_branch == "develop"


def test_base_branch_empty_when_unset(tmp_path):
    app = load_config(_write_config(tmp_path))
    assert _repo(app).base_branch == ""


def test_post_merge_ci_defaults(tmp_path):
    app = load_config(_write_config(tmp_path))
    cfg = _repo(app)
    assert cfg.post_merge_ci is True
    assert cfg.post_merge_ci_grace == 180


# -- worktree_dir -----------------------------------------------------------


def test_worktree_dir_empty_when_unset(tmp_path):
    app = load_config(_write_config(tmp_path))
    cfg = _repo(app)
    assert cfg.worktree_dir == ""
    assert cfg.worktree_setup == ""


def test_worktree_dir_explicit(tmp_path):
    app = load_config(_write_config(tmp_path, repo_extra='worktree_dir: "../custom-worktrees"\n'))
    assert _repo(app).worktree_dir == "../custom-worktrees"


def test_resolve_worktree_dir_default_uses_repo_root_sibling():
    repo_root = Path("/Users/x/github/ghswarm")
    resolved = resolve_worktree_dir("", repo_root)
    assert resolved == Path("/Users/x/github/ghswarm-worktrees")


def test_resolve_worktree_dir_relative_is_based_on_repo_root():
    repo_root = Path("/Users/x/github/ghswarm")
    resolved = resolve_worktree_dir("../custom-worktrees", repo_root)
    assert resolved == Path("/Users/x/github/custom-worktrees")


def test_orchestrator_auto_detects_base_branch_when_unset(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    app = load_config(_write_config(tmp_path, path=str(repo_path)))
    cfg = _repo(app)
    monkeypatch.setattr(orchestrator, "detect_default_branch", lambda root, env=None: "main")
    monkeypatch.setattr(orchestrator, "GitHub", lambda repo, env=None: object())
    monkeypatch.setattr(orchestrator, "Git", lambda root: object())
    orch = orchestrator.Orchestrator(cfg)
    assert orch.base_branch == "main"
    assert orch.repo_name == "owner/repo"
    assert orch.repo_root == str(repo_path.resolve())


# -- agents (per-phase schema) -------------------------------------------


def test_agent_for_resolves_each_phase_command(tmp_path):
    app = load_config(_write_config(tmp_path))
    cfg = _repo(app)
    assert cfg.agent_for("implement").commands == ["claude -p {prompt} --model opus"]
    assert cfg.agent_for("review").commands == ["claude -p {prompt} --model sonnet"]


def test_command_string_normalizes_to_single_element_list(tmp_path):
    app = load_config(_write_config(tmp_path))
    assert len(_repo(app).agent_for("implement").commands) == 1


def test_command_list_loads_fallback_chain(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
            agents:
              implement:
                command:
                  - "cursor-agent -p {prompt}"
                  - "claude -p {prompt} --model opus"
              review:
                command: "claude -p {prompt} --model sonnet"
        """,
    )
    cfg = _repo(load_config(path))
    assert cfg.agent_for("implement").commands == [
        "cursor-agent -p {prompt}",
        "claude -p {prompt} --model opus",
    ]


def test_command_list_excludes_empty_strings(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
            agents:
              implement:
                command:
                  - "claude -p {prompt}"
                  - ""
                  - "   "
              review:
                command: "claude -p {prompt}"
        """,
    )
    cfg = _repo(load_config(path))
    assert cfg.agent_for("implement").commands == ["claude -p {prompt}"]


def test_command_empty_string_raises_config_error(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
            agents:
              implement:
                command: ""
              review:
                command: "claude -p {prompt}"
        """,
    )
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "implement" in str(e.value)


def test_command_empty_list_raises_config_error(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
            agents:
              implement:
                command: []
              review:
                command: "claude -p {prompt}"
        """,
    )
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "implement" in str(e.value)


def test_command_list_with_only_empty_strings_raises_config_error(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
            agents:
              implement:
                command:
                  - ""
                  - "   "
              review:
                command: "claude -p {prompt}"
        """,
    )
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "implement" in str(e.value)


def test_command_non_string_element_raises_config_error(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
            agents:
              implement:
                command:
                  - "claude -p {prompt}"
                  - 42
              review:
                command: "claude -p {prompt}"
        """,
    )
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "implement" in str(e.value) and "int" in str(e.value)


def test_single_string_command_backward_compatible(tmp_path):
    app = load_config(_write_config(tmp_path))
    impl = _repo(app).agent_for("implement")
    assert impl.commands == ["claude -p {prompt} --model opus"]
    assert len(impl.commands) == 1


def test_missing_implement_raises_config_error(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        defaults:
          agents:
            review:
              command: "claude -p {prompt}"
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
        """,
    )
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "implement" in str(e.value) and "review" in str(e.value)


def test_value_without_command_raises_config_error(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
            agents:
              implement:
                model: opus
              review:
                command: "claude -p {prompt}"
        """,
    )
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "implement" in str(e.value) and "review" in str(e.value)


def test_list_value_raises_config_error(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
            agents:
              implement:
                - command: "claude -p {prompt}"
              review:
                command: "claude -p {prompt}"
        """,
    )
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "implement" in str(e.value) and "review" in str(e.value)


def test_empty_agents_raises_config_error(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        defaults:
          agents: {}
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
        """,
    )
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "implement" in str(e.value) and "review" in str(e.value)


def test_missing_agents_raises_config_error(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
            base_branch: main
        """,
    )
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "implement" in str(e.value) and "review" in str(e.value)


def test_extra_agent_keys_are_ignored(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            path: /tmp/repo
            agents:
              implement:
                command: "claude -p {prompt}"
              review:
                command: "claude -p {prompt}"
              codex:
                command: "codex exec {prompt}"
        """,
    )
    cfg = _repo(load_config(path))
    assert cfg.agent_names() == ["implement", "review"]


def test_orchestrator_prefers_explicit_base_branch(tmp_path, monkeypatch):
    app = load_config(_write_config(tmp_path, repo_extra='base_branch: "develop"\n'))
    cfg = _repo(app)
    monkeypatch.setattr(
        orchestrator,
        "detect_default_branch",
        lambda root, env=None: (_ for _ in ()).throw(
            AssertionError("auto-detection must not be called")
        ),
    )
    monkeypatch.setattr(orchestrator, "GitHub", lambda repo, env=None: object())
    monkeypatch.setattr(orchestrator, "Git", lambda root: object())
    orch = orchestrator.Orchestrator(cfg)
    assert orch.base_branch == "develop"


# -- target filter ---------------------------------------------------------


def test_target_filter_defaults_when_block_missing(tmp_path):
    app = load_config(_write_config(tmp_path))
    assert _repo(app).target == TargetFilter()


def test_target_filter_empty_block(tmp_path):
    app = load_config(_write_config(tmp_path, defaults_extra="target: {}\n"))
    assert _repo(app).target == TargetFilter()


def test_target_filter_loads_all_fields(tmp_path):
    extra = """
    target:
      labels:
        - pm-agent
        - "priority: high"
      assignee: my-bot
      milestone: "Sprint 42"
    """
    app = load_config(_write_config(tmp_path, defaults_extra=extra))
    t = _repo(app).target
    assert t.labels == ["pm-agent", "priority: high"]
    assert t.assignee == "my-bot"
    assert t.milestone == "Sprint 42"


def test_target_filter_normalizes_scalar_labels_and_int_milestone(tmp_path):
    extra = """
    target:
      labels: pm-agent
      milestone: 42
    """
    app = load_config(_write_config(tmp_path, defaults_extra=extra))
    t = _repo(app).target
    assert t.labels == ["pm-agent"]
    assert t.milestone == "42"


# -- transient error config ------------------------------------------------


def test_transient_error_patterns_defaults(tmp_path):
    app = load_config(_write_config(tmp_path))
    cfg = _repo(app)
    assert cfg.transient_error_patterns == _DEFAULT_TRANSIENT_ERROR_PATTERNS
    assert cfg.transient_max_retries == 5


def test_transient_error_patterns_scalar_normalized(tmp_path):
    app = load_config(
        _write_config(tmp_path, defaults_extra='transient_error_patterns: "custom error"\n')
    )
    assert _repo(app).transient_error_patterns == ["custom error"]


def test_conflict_resolve_defaults(tmp_path):
    app = load_config(_write_config(tmp_path))
    cfg = _repo(app)
    assert cfg.auto_resolve_conflicts is True
    assert cfg.conflict_max_retries == 3


def test_conflict_resolve_yaml_override(tmp_path):
    extra = """
    auto_resolve_conflicts: false
    conflict_max_retries: 7
    """
    app = load_config(_write_config(tmp_path, defaults_extra=extra))
    cfg = _repo(app)
    assert cfg.auto_resolve_conflicts is False
    assert cfg.conflict_max_retries == 7


def test_ci_fix_defaults(tmp_path):
    app = load_config(_write_config(tmp_path))
    cfg = _repo(app)
    assert cfg.auto_fix_ci is True
    assert cfg.ci_fix_max_retries == 3


def test_ci_fix_yaml_override(tmp_path):
    extra = """
    auto_fix_ci: false
    ci_fix_max_retries: 5
    """
    app = load_config(_write_config(tmp_path, defaults_extra=extra))
    cfg = _repo(app)
    assert cfg.auto_fix_ci is False
    assert cfg.ci_fix_max_retries == 5


def test_transient_error_patterns_list_normalized(tmp_path):
    extra = """
    transient_error_patterns:
      - "pattern one"
      - "pattern two"
    transient_max_retries: 10
    """
    app = load_config(_write_config(tmp_path, defaults_extra=extra))
    cfg = _repo(app)
    assert cfg.transient_error_patterns == ["pattern one", "pattern two"]
    assert cfg.transient_max_retries == 10


def test_issue_max_agent_runs_defaults(tmp_path):
    app = load_config(_write_config(tmp_path))
    assert _repo(app).issue_max_agent_runs == 10


def test_issue_max_agent_runs_yaml_override(tmp_path):
    extra = """
    issue_max_agent_runs: 0
    """
    app = load_config(_write_config(tmp_path, defaults_extra=extra))
    assert _repo(app).issue_max_agent_runs == 0


# -- deep merge -------------------------------------------------------------


def test_deep_merge_nested_mapping():
    base = {"labels": {"idle": "a", "blocked": "b"}, "poll_interval": 60}
    override = {"labels": {"idle": "x"}, "merge_method": "merge"}
    merged = _deep_merge(base, override)
    assert merged == {
        "labels": {"idle": "x", "blocked": "b"},
        "poll_interval": 60,
        "merge_method": "merge",
    }


def test_deep_merge_replaces_scalar_and_list():
    base = {"target": {"labels": ["a"], "assignee": "@me"}}
    override = {"target": {"labels": ["b", "c"]}}
    merged = _deep_merge(base, override)
    assert merged["target"]["labels"] == ["b", "c"]
    assert merged["target"]["assignee"] == "@me"


# -- LabelConfig.is_status_label -------------------------------------------


def test_is_status_label_default_config():
    labels = LabelConfig()
    assert labels.is_status_label("status: idle") is True
    assert labels.is_status_label("status: blocked") is True
    assert labels.is_status_label("status: completed") is True
    assert labels.is_status_label("status: busy-implement") is True
    assert labels.is_status_label("pm-agent") is False
    assert labels.is_status_label("status: unknown") is False


def test_is_status_label_custom_config():
    labels = LabelConfig(
        idle="state: idle",
        blocked="state: blocked",
        completed="state: completed",
        busy_prefix="state: busy-",
    )
    assert labels.is_status_label("state: idle") is True
    assert labels.is_status_label("state: busy-review") is True
    assert labels.is_status_label("status: idle") is False
    assert labels.is_status_label("priority: high") is False


def test_defaults_and_repo_deep_merge(tmp_path):
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra='merge_method: squash\nlabels:\n  idle: "status: idle"\n',
            repo_extra='merge_method: merge\nlabels:\n  idle: "state: idle"\n',
        )
    )
    cfg = _repo(app)
    assert cfg.merge_method == "merge"
    assert cfg.labels.idle == "state: idle"
    assert cfg.labels.blocked == "status: blocked"


# -- config discovery / validation ------------------------------------------


def test_load_config_uses_home_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(home))

    config = _write_config(home, alias="main")
    # home helper writes to home/ghswarm.yaml but _write_config uses tmp_path
    # rewrite explicitly
    home_config = home / ".ghswarm.yaml"
    home_config.write_text(Path(config).read_text(), encoding="utf-8")

    (repo / ".ghswarm.yml").write_text("repositories: {}\n", encoding="utf-8")

    app = load_config()
    assert app.source_path == home_config
    assert "main" in app.repositories or "test" in app.repositories


def test_load_config_prefers_yaml_over_yml_in_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    yml = home / ".ghswarm.yml"
    yaml = home / ".ghswarm.yaml"
    yml.write_text(
        textwrap.dedent(
            f"""
            repositories:
              a:
                repo: owner/a
                path: {home}/a
                agents:
                  implement:
                    command: "from-yml"
                  review:
                    command: "from-yml"
            """
        ),
        encoding="utf-8",
    )
    yaml.write_text(
        textwrap.dedent(
            f"""
            repositories:
              b:
                repo: owner/b
                path: {home}/b
                agents:
                  implement:
                    command: "from-yaml"
                  review:
                    command: "from-yaml"
            """
        ),
        encoding="utf-8",
    )

    app = load_config()
    assert app.source_path == yaml
    assert "b" in app.repositories


def test_load_config_explicit_path(tmp_path):
    path = _write_config(tmp_path, repo_extra='base_branch: "feature"\n')
    app = load_app_config(path)
    assert app.source_path == Path(path)
    assert _repo(app).base_branch == "feature"


def test_repositories_empty_raises(tmp_path):
    path = _write_raw(tmp_path, "repositories: {}\n")
    with pytest.raises(ConfigError, match="repositories"):
        load_config(path)


def test_missing_repo_field_raises(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            path: /tmp/repo
            agents:
              implement:
                command: "x"
              review:
                command: "y"
        """,
    )
    with pytest.raises(ConfigError, match=r"repositories\.test\.repo"):
        load_config(path)


def test_missing_path_field_raises(tmp_path):
    path = _write_raw(
        tmp_path,
        """
        repositories:
          test:
            repo: owner/repo
            agents:
              implement:
                command: "x"
              review:
                command: "y"
        """,
    )
    with pytest.raises(ConfigError, match=r"repositories\.test\.path"):
        load_config(path)


def test_invalid_max_parallel_repos_raises(tmp_path):
    path = _write_config(tmp_path, top_extra="max_parallel_repos: 0\n")
    with pytest.raises(ConfigError, match="max_parallel_repos"):
        load_config(path)


def test_repo_config_has_name_alias(tmp_path):
    app = load_config(_write_config(tmp_path, alias="myalias"))
    cfg = app.repositories["myalias"]
    assert cfg.name == "myalias"
    assert cfg.repo == "owner/repo"


def test_daemon_paths_default(tmp_path):
    app = load_config(_write_config(tmp_path))
    home = Path.home()
    assert app.daemon_log == str(home / ".ghswarm" / "ghswarm.log")
    assert app.daemon_pid == str(home / ".ghswarm" / "ghswarm.pid")


def test_daemon_paths_override(tmp_path):
    app = load_config(
        _write_config(
            tmp_path,
            top_extra=('daemon_log: "/var/log/ghswarm.log"\ndaemon_pid: "/var/run/ghswarm.pid"\n'),
        )
    )
    assert app.daemon_log == "/var/log/ghswarm.log"
    assert app.daemon_pid == "/var/run/ghswarm.pid"


def test_daemon_paths_expanduser(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = load_config(
        _write_config(
            tmp_path,
            top_extra=('daemon_log: "~/logs/ghswarm.log"\ndaemon_pid: "~/run/ghswarm.pid"\n'),
        )
    )
    assert app.daemon_log == str(tmp_path / "logs" / "ghswarm.log")
    assert app.daemon_pid == str(tmp_path / "run" / "ghswarm.pid")


# -- event_db ---------------------------------------------------------------


def test_event_db_default(tmp_path):
    app = load_config(_write_config(tmp_path))
    assert _repo(app).event_db == "~/.ghswarm/events.db"


def test_event_db_from_defaults(tmp_path):
    app = load_config(_write_config(tmp_path, defaults_extra='event_db: "/tmp/shared-events.db"\n'))
    assert _repo(app).event_db == "/tmp/shared-events.db"


def test_event_db_repo_override(tmp_path):
    app = load_config(_write_config(tmp_path, repo_extra='event_db: "/tmp/repo-events.db"\n'))
    assert _repo(app).event_db == "/tmp/repo-events.db"


def test_event_db_empty_disables(tmp_path):
    app = load_config(_write_config(tmp_path, repo_extra='event_db: ""\n'))
    assert _repo(app).event_db == ""


# -- env var expansion ------------------------------------------------------


def test_expand_env_slack_webhook_url(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/abc")
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra='notify:\n  slack_webhook_url: "${SLACK_WEBHOOK_URL}"\n',
        )
    )
    assert _repo(app).notify.slack_webhook_url == "https://hooks.slack.com/services/abc"


def test_expand_env_default_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_TOKEN", raising=False)
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra='test_command: "${MY_TOKEN:-npm test}"\n',
        )
    )
    assert _repo(app).test_command == "npm test"


def test_expand_env_uses_env_over_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "pnpm test")
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra='test_command: "${MY_TOKEN:-npm test}"\n',
        )
    )
    assert _repo(app).test_command == "pnpm test"


def test_expand_env_empty_default_disables_notify(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra='notify:\n  slack_webhook_url: "${SLACK_WEBHOOK_URL:-}"\n',
        )
    )
    assert _repo(app).notify.slack_webhook_url is None


def test_load_notify_on_completed_defaults_true(tmp_path):
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra='notify:\n  slack_webhook_url: "https://hooks.slack.com/x"\n',
        )
    )
    assert _repo(app).notify.on_completed is True


def test_load_notify_on_completed_false(tmp_path):
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra=(
                'notify:\n  slack_webhook_url: "https://hooks.slack.com/x"\n  on_completed: false\n'
            ),
        )
    )
    assert _repo(app).notify.on_completed is False


def test_expand_env_missing_var_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    path = _write_config(
        tmp_path,
        defaults_extra='notify:\n  slack_webhook_url: "${SLACK_WEBHOOK_URL}"\n',
    )
    with pytest.raises(ConfigError, match="SLACK_WEBHOOK_URL"):
        load_config(path)


def test_expand_env_multiple_placeholders_and_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFIX", "pre")
    monkeypatch.setenv("SUFFIX", "suf")
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra='test_command: "${PREFIX}-middle-${SUFFIX}"\n',
        )
    )
    assert _repo(app).test_command == "pre-middle-suf"


def test_expand_env_no_double_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTER", "resolved-${INNER}")
    monkeypatch.setenv("INNER", "should-not-appear")
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra='test_command: "${OUTER}"\n',
        )
    )
    assert _repo(app).test_command == "resolved-${INNER}"


def test_expand_env_preserves_literals_without_placeholders(tmp_path):
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra='test_command: "npm test"\n',
            repo_extra='base_branch: "develop"\n',
        )
    )
    cfg = _repo(app)
    assert cfg.test_command == "npm test"
    assert cfg.base_branch == "develop"
    assert cfg.agent_for("implement").commands == ["claude -p {prompt} --model opus"]


# -- env (per-repo gh environment) -----------------------------------------


def test_env_defaults_to_empty_dict(tmp_path):
    app = load_config(_write_config(tmp_path))
    assert _repo(app).env == {}


def test_env_tilde_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = load_config(
        _write_config(
            tmp_path,
            repo_extra="env:\n  GH_CONFIG_DIR: ~/.config/gh-work\n",
        )
    )
    assert _repo(app).env == {"GH_CONFIG_DIR": str(tmp_path / ".config" / "gh-work")}


def test_env_var_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_CONFIG_BASE", str(tmp_path / "gh-configs"))
    app = load_config(
        _write_config(
            tmp_path,
            repo_extra='env:\n  GH_CONFIG_DIR: "${GH_CONFIG_BASE}/work"\n',
        )
    )
    assert _repo(app).env == {"GH_CONFIG_DIR": str(tmp_path / "gh-configs" / "work")}


def test_env_defaults_and_repo_merge(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = load_config(
        _write_config(
            tmp_path,
            defaults_extra='env:\n  GH_CONFIG_DIR: ~/.config/gh-default\n  SHARED: "from-defaults"\n',
            repo_extra='env:\n  GH_CONFIG_DIR: ~/.config/gh-repo\n  REPO_ONLY: "repo"\n',
        )
    )
    assert _repo(app).env == {
        "GH_CONFIG_DIR": str(tmp_path / ".config" / "gh-repo"),
        "SHARED": "from-defaults",
        "REPO_ONLY": "repo",
    }


def test_env_invalid_mapping_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="'env' must be a mapping"):
        load_config(_write_config(tmp_path, repo_extra="env: not-a-mapping\n"))


def test_env_value_must_be_string(tmp_path):
    with pytest.raises(ConfigError, match="'env.GH_CONFIG_DIR' must be a string"):
        load_config(
            _write_config(
                tmp_path,
                repo_extra="env:\n  GH_CONFIG_DIR: 123\n",
            )
        )


# -- sandbox ---------------------------------------------------------------


def test_sandbox_defaults_when_block_missing(tmp_path):
    app = load_config(_write_config(tmp_path))
    assert _repo(app).sandbox == SandboxConfig()


def test_sandbox_loads_all_fields(tmp_path):
    extra = """
    sandbox:
      driver: docker
      image: node:22-bookworm
      network: none
      user: "1000:1000"
      env:
        npm_config_cache: /cache/npm
      env_passthrough:
        - GH_TOKEN
        - GITHUB_TOKEN
      volumes:
        - ghswarm-cache:/cache
      isolate_dirs:
        - .venv
        - ./node_modules/
    """
    app = load_config(_write_config(tmp_path, repo_extra=extra))
    sb = _repo(app).sandbox
    assert sb.driver == "docker"
    assert sb.image == "node:22-bookworm"
    assert sb.network == "none"
    assert sb.user == "1000:1000"
    assert sb.env == {"npm_config_cache": "/cache/npm"}
    assert sb.env_passthrough == ["GH_TOKEN", "GITHUB_TOKEN"]
    assert sb.volumes == ["ghswarm-cache:/cache"]
    assert sb.isolate_dirs == [".venv", "node_modules"]


def test_sandbox_isolate_dirs_scalar_coerced(tmp_path):
    app = load_config(
        _write_config(
            tmp_path,
            repo_extra="sandbox:\n  driver: docker\n  image: alpine\n  isolate_dirs: target\n",
        )
    )
    assert _repo(app).sandbox.isolate_dirs == ["target"]


def test_sandbox_isolate_dirs_ignores_empty_elements(tmp_path):
    app = load_config(
        _write_config(
            tmp_path,
            repo_extra=(
                "sandbox:\n"
                "  driver: docker\n"
                "  image: alpine\n"
                "  isolate_dirs:\n"
                "    - .venv\n"
                "    - ''\n"
                "    - '  '\n"
            ),
        )
    )
    assert _repo(app).sandbox.isolate_dirs == [".venv"]


def test_sandbox_isolate_dirs_allows_dotdot_in_name(tmp_path):
    app = load_config(
        _write_config(
            tmp_path,
            repo_extra=(
                "sandbox:\n"
                "  driver: docker\n"
                "  image: alpine\n"
                "  isolate_dirs:\n"
                "    - ..cache\n"
                "    - foo..bar\n"
            ),
        )
    )
    assert _repo(app).sandbox.isolate_dirs == ["..cache", "foo..bar"]


def test_sandbox_isolate_dirs_rejects_absolute_path(tmp_path):
    with pytest.raises(ConfigError, match="absolute path"):
        load_config(
            _write_config(
                tmp_path,
                repo_extra=(
                    "sandbox:\n"
                    "  driver: docker\n"
                    "  image: alpine\n"
                    "  isolate_dirs:\n"
                    "    - /tmp/venv\n"
                ),
            )
        )


def test_sandbox_isolate_dirs_rejects_parent_component(tmp_path):
    with pytest.raises(ConfigError, match=r"\.\."):
        load_config(
            _write_config(
                tmp_path,
                repo_extra=(
                    "sandbox:\n"
                    "  driver: docker\n"
                    "  image: alpine\n"
                    "  isolate_dirs:\n"
                    "    - ../outside\n"
                ),
            )
        )


def test_sandbox_docker_without_image_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="sandbox.image"):
        load_config(_write_config(tmp_path, repo_extra="sandbox:\n  driver: docker\n"))


def test_sandbox_invalid_network_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="sandbox.network"):
        load_config(
            _write_config(
                tmp_path,
                repo_extra="sandbox:\n  driver: docker\n  image: alpine\n  network: host\n",
            )
        )


def test_sandbox_defaults_inherited_via_deep_merge(tmp_path):
    defaults = """
    sandbox:
      driver: docker
      image: python:3.12
      network: default
    """
    app = load_config(_write_config(tmp_path, defaults_extra=defaults))
    assert _repo(app).sandbox.driver == "docker"
    assert _repo(app).sandbox.image == "python:3.12"
