"""Loading of the config file (YAML) and type definitions.

Search order (only the first file found is used; no merging):
  1. the path given explicitly via --config
  2. ~/.ghswarm.yaml in the home directory
  3. ~/.ghswarm.yml in the home directory

${VAR} / ${VAR:-default} in string values are expanded from environment variables.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


@dataclass
class AgentConfig:
    """Launch definition of the coding CLI assigned to a single phase.

    commands is a list of templates containing the {prompt} placeholder. The first is
    the primary command; the rest are fallbacks in order. At runtime the placeholder is
    replaced with the shlex.quote'd prompt.
    Example: ["claude -p {prompt} --dangerously-skip-permissions"]
    name holds the phase name (implement / review).
    """

    name: str
    commands: list[str]


@dataclass
class TargetFilter:
    """Filter for the open Issues that loop / status pick up."""

    labels: list[str] = field(default_factory=list)
    assignee: str = ""
    milestone: str = ""


@dataclass
class NotifyConfig:
    """Push notification settings for blocked / clarification / completed transitions.

    All channels are disabled if unset.
    """

    slack_webhook_url: str | None = None
    slack_mention: str | None = None
    macos: bool = False
    on_completed: bool = True


@dataclass
class SandboxConfig:
    """Opt-in settings to run setup / verify inside a Docker container."""

    driver: str = "none"  # none | docker
    image: str = ""
    network: str = "default"  # default | none
    user: str = "auto"  # auto | "uid:gid" | "" (skip --user)
    env: dict[str, str] = field(default_factory=dict)
    env_passthrough: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    isolate_dirs: list[str] = field(default_factory=list)


@dataclass
class VerifyConfig:
    """Path -> SandboxConfig registry resolved from RepoConfig's `verify:` block.

    Single form (a `verify:` mapping with only `sandbox`) is represented by `single`
    and applies to every path regardless of what is asked. List form (a `verify:` list
    of `{path, sandbox}` entries) is matched by exact `path` via `entries`. If `verify:`
    is unset (both empty), `sandbox_for` falls back to the default SandboxConfig
    (driver: none) for every path.
    """

    single: SandboxConfig | None = None
    entries: list[tuple[str, SandboxConfig]] = field(default_factory=list)

    def sandbox_for(self, path: str | None) -> SandboxConfig:
        if self.single is not None:
            return self.single
        if path is not None:
            for entry_path, sandbox in self.entries:
                if entry_path == path:
                    return sandbox
        return SandboxConfig()


@dataclass
class LabelConfig:
    idle: str = "status: idle"
    blocked: str = "status: blocked"
    completed: str = "status: completed"
    # busy labels are generated dynamically as "status: busy-{agent}"
    busy_prefix: str = "status: busy-"

    def busy(self, agent: str) -> str:
        return f"{self.busy_prefix}{agent}"

    def is_status_label(self, label: str) -> bool:
        """Determine whether the label is a status label defined by the labels: config."""
        if label in (self.idle, self.blocked, self.completed):
            return True
        return label.startswith(self.busy_prefix)

    def all_status(self, agent_names: list[str]) -> list[str]:
        return [self.idle, self.blocked, self.completed] + [self.busy(a) for a in agent_names]


@dataclass
class RepoConfig:
    """ghswarm settings for a single repository."""

    # phase name (implement / review) -> the agent definition used for that phase.
    agents: dict[str, AgentConfig]
    # alias under repositories (for selection and log display)
    name: str = ""
    # GitHub owner/repo
    repo: str = ""
    # absolute path of the local clone (need not have ~ expanded)
    path: str = ""
    labels: LabelConfig = field(default_factory=LabelConfig)
    target: TargetFilter = field(default_factory=TargetFilter)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    poll_interval: int = 60
    max_retries: int = 3
    question_file: str = ".agent_question.md"
    branch_prefix: str = "issue-"
    spec_dir: str = ".specs"
    # branch the PR merges into. Work branches are also cut from here. Auto-detected via gh if empty.
    base_branch: str = ""
    merge_method: str = "squash"  # squash / merge / rebase
    require_approval: bool = True  # require PR review approval for auto-merge
    # whether to automatically pick up review comments on the PR (both humans and
    # review bots) and have the review agent address them. Ensures review feedback
    # is not dropped, not just that CI passes.
    address_pr_reviews: bool = True
    delete_branch_on_merge: bool = True
    # whether to check the target branch's CI after merging before closing the Issue
    post_merge_ci: bool = True
    # grace period (seconds) to wait before treating "0 checks" post-merge as "no CI"
    post_merge_ci_grace: int = 180
    # where per-issue git worktrees are placed. If empty, uses
    # "<repo-root-dir-name>-worktrees" alongside repo_root. Relative paths are
    # resolved relative to the repository root (see resolve_worktree_dir).
    worktree_dir: str = ""
    # setup command run exactly once right after a worktree is newly created. No-op if empty.
    worktree_setup: str = ""
    # CLI output matching any of these regexes is treated as a transient error (case-insensitive).
    transient_error_patterns: list[str] = field(
        default_factory=lambda: list(_DEFAULT_TRANSIENT_ERROR_PATTERNS)
    )
    # cap on transient retries across loops. Escalates to blocked when exceeded.
    transient_max_retries: int = 5
    # whether to auto-resolve by merging base in the worktree when the PR is CONFLICTING.
    auto_resolve_conflicts: bool = True
    # cap on conflict-resolution retries across loops. Escalates to blocked when exceeded.
    conflict_max_retries: int = 3
    # whether to fetch logs on PR CI (GitHub Actions) failure and auto-fix with the implement agent.
    auto_fix_ci: bool = True
    # cap on CI-fix retries across loops. Escalates to blocked when exceeded.
    ci_fix_max_retries: int = 3
    # cap on cumulative agent runs per Issue. 0 disables it (unlimited).
    issue_max_agent_runs: int = 10
    # absolute TTL (seconds) for the busy lock. On the same host the pid check takes
    # precedence, so this is the fallback cap.
    lock_ttl: int = 14400
    # SQLite path for the structured event log. Empty string disables it.
    event_db: str = "~/.ghswarm/events.db"
    # directory for activity files while the daemon runs. Empty string disables it.
    activity_dir: str = "~/.ghswarm/activity"
    # env vars injected into this repo's gh invocations (overrides os.environ per key).
    env: dict[str, str] = field(default_factory=dict)
    # path -> sandbox registry used to run verify steps (see VerifyConfig). Never
    # carries a command; the steps to run come from the spec frontmatter.
    verify: VerifyConfig = field(default_factory=VerifyConfig)

    def agent_names(self) -> list[str]:
        """Return all phase names. Used as the targets for label cleanup and lock checks."""
        return list(self.agents.keys())

    def agent_for(self, phase: str) -> AgentConfig:
        """Return the agent assigned to the phase (implement / review).

        Assumes agents has been validated by load_config(). Constructing a RepoConfig
        directly without validation may raise KeyError; the behavior of that usage is undefined.
        """
        return self.agents[phase]


@dataclass
class AppConfig:
    """The entire home config file (multiple repositories)."""

    repositories: dict[str, RepoConfig]
    max_parallel_repos: int = 3
    daemon_log: str = os.path.expanduser("~/.ghswarm/ghswarm.log")
    daemon_pid: str = os.path.expanduser("~/.ghswarm/ghswarm.pid")
    source_path: Path | None = None


# Phase names. Only these two keys are extracted from agents and validated; the rest are ignored.
_PHASES = ("implement", "review")

_DEFAULT_TRANSIENT_ERROR_PATTERNS = [
    "resource_exhausted",
    "RetriableError",
    "Connection lost",
    "reconnecting",
    "rate limit",
    "Too Many Requests",
    "overloaded",
    "ECONNRESET",
    "temporarily unavailable",
    r"\b(429|503)\b",
]

_AGENTS_EXAMPLE = """Example fix:
agents:
  implement:
  # a single command (string) or a fallback chain (list, first is primary)
    command:
      - "cursor-agent -p {prompt} --model auto"
      - "claude -p {prompt} --model opus --dangerously-skip-permissions"
  review:
    command: "claude -p {prompt} --model sonnet --dangerously-skip-permissions"
"""

_SANDBOX_EXAMPLE = """Example fix:
verify:
  sandbox:
    driver: docker
    image: node:22-bookworm
    network: default
"""

_SANDBOX_ISOLATE_DIRS_EXAMPLE = """Example fix:
verify:
  sandbox:
    isolate_dirs:
      - .venv
"""

_VERIFY_EXAMPLE = """Example fix (single form, whole repo):
verify:
  sandbox:
    driver: none   # also the default when omitted

Example fix (list form, monorepo path -> sandbox registry):
verify:
  - path: terraform
    sandbox:
      driver: none
  - path: backend
    sandbox:
      driver: docker
      image: python:3.12
"""

_VERIFY_MIGRATION_EXAMPLE = """'test_command' / top-level 'sandbox' have been removed. Migrate to 'verify:'.
The command itself now belongs in each spec's frontmatter 'verify:', not in config.
""" + _VERIFY_EXAMPLE


def _normalize_commands(raw: Any, path: Path, phase: str) -> list[str]:
    """Normalize agents.<phase>.command into a commands list."""
    if isinstance(raw, str):
        if not raw.strip():
            raise ConfigError(
                f"{path}: 'agents.{phase}.command' cannot be empty. "
                f"Each of the implement / review agents must specify a command.\n{_AGENTS_EXAMPLE}"
            )
        return [raw]
    if isinstance(raw, list):
        commands: list[str] = []
        for i, item in enumerate(raw):
            if not isinstance(item, str):
                raise ConfigError(
                    f"{path}: each element of 'agents.{phase}.command' must be a string "
                    f"(index {i} is {type(item).__name__}).\n{_AGENTS_EXAMPLE}"
                )
            if item.strip():
                commands.append(item)
        if not commands:
            raise ConfigError(
                f"{path}: 'agents.{phase}.command' has no valid commands "
                f"(only empty strings, or an empty list).\n{_AGENTS_EXAMPLE}"
            )
        return commands
    raise ConfigError(
        f"{path}: 'agents.{phase}.command' must be a string or a list of strings "
        f"({type(raw).__name__} is not allowed).\n{_AGENTS_EXAMPLE}"
    )


def _load_agents(agents_raw: Any, path: Path) -> dict[str, AgentConfig]:
    """Extract and validate only the two keys implement / review from agents.

    All other keys are ignored and not validated. On a violation, raise a ConfigError
    that includes an example fix to guide migration.
    """
    if not isinstance(agents_raw, dict):
        raise ConfigError(
            f"{path}: 'agents' must be a mapping with implement / review.\n{_AGENTS_EXAMPLE}"
        )
    agents: dict[str, AgentConfig] = {}
    for phase in _PHASES:
        a = agents_raw.get(phase)
        if a is None:
            raise ConfigError(
                f"{path}: 'agents' is missing the required phase '{phase}'. "
                f"Specify both implement and review.\n{_AGENTS_EXAMPLE}"
            )
        if not isinstance(a, dict):
            raise ConfigError(
                f"{path}: 'agents.{phase}' must be a mapping with a command key "
                f"(a list or scalar is not allowed). Specify implement / review each as a mapping.\n"
                f"{_AGENTS_EXAMPLE}"
            )
        if "command" not in a:
            raise ConfigError(
                f"{path}: 'agents.{phase}' is missing the required 'command'. "
                f"Each of the implement / review agents must have a command.\n{_AGENTS_EXAMPLE}"
            )
        agents[phase] = AgentConfig(
            name=phase, commands=_normalize_commands(a["command"], path, phase)
        )
    return agents


def _load_notify(raw: dict[str, Any]) -> NotifyConfig:
    """Normalize the notify: block into a NotifyConfig."""
    block = raw.get("notify")
    if not isinstance(block, dict):
        return NotifyConfig()

    url_raw = block.get("slack_webhook_url")
    slack_webhook_url = None if url_raw is None or url_raw == "" else str(url_raw)

    mention_raw = block.get("slack_mention")
    slack_mention = None if mention_raw is None or mention_raw == "" else str(mention_raw)

    return NotifyConfig(
        slack_webhook_url=slack_webhook_url,
        slack_mention=slack_mention,
        macos=bool(block.get("macos", False)),
        on_completed=bool(block.get("on_completed", True)),
    )


def _load_target_filter(raw: dict[str, Any]) -> TargetFilter:
    """Normalize the target: block into a TargetFilter."""
    block = raw.get("target")
    if not isinstance(block, dict):
        return TargetFilter()

    labels_raw = block.get("labels")
    if labels_raw is None:
        labels: list[str] = []
    elif isinstance(labels_raw, str):
        labels = [str(labels_raw)]
    elif isinstance(labels_raw, list):
        labels = [str(lb) for lb in labels_raw]
    else:
        labels = [str(labels_raw)]

    assignee_raw = block.get("assignee")
    assignee = "" if assignee_raw is None or assignee_raw == "" else str(assignee_raw)

    milestone_raw = block.get("milestone")
    milestone = "" if milestone_raw is None or milestone_raw == "" else str(milestone_raw)

    return TargetFilter(labels=labels, assignee=assignee, milestone=milestone)


def _normalize_isolate_dirs(isolate_dirs_raw: Any, source: Path) -> list[str]:
    """Normalize sandbox.isolate_dirs and reject any escape outside the worktree."""
    if isolate_dirs_raw is None:
        raw_list: list[Any] = []
    elif isinstance(isolate_dirs_raw, list):
        raw_list = isolate_dirs_raw
    else:
        raw_list = [isolate_dirs_raw]

    result: list[str] = []
    for entry in raw_list:
        text = str(entry).strip()
        if not text:
            continue
        norm = posixpath.normpath(text)
        if norm.startswith("/"):
            raise ConfigError(
                f"{source}: each element of 'sandbox.isolate_dirs' must be a worktree-root-relative path "
                f"(absolute path {norm!r} is not allowed).\n{_SANDBOX_ISOLATE_DIRS_EXAMPLE}"
            )
        if ".." in norm.split("/"):
            raise ConfigError(
                f"{source}: elements of 'sandbox.isolate_dirs' must not contain '..' "
                f"(got {norm!r}).\n{_SANDBOX_ISOLATE_DIRS_EXAMPLE}"
            )
        result.append(norm)
    return result


def _load_sandbox_env(block: dict[str, Any], source: Path, prefix: str) -> dict[str, str]:
    """Normalize the sandbox.env block into a str->str mapping."""
    env_raw = block.get("env")
    if env_raw is None:
        return {}
    if not isinstance(env_raw, dict):
        raise ConfigError(f"{source}: '{prefix}.env' must be a mapping.")
    result: dict[str, str] = {}
    for key, value in env_raw.items():
        if not isinstance(key, str):
            raise ConfigError(f"{source}: keys of '{prefix}.env' must be strings.")
        if not isinstance(value, str):
            raise ConfigError(
                f"{source}: '{prefix}.env.{key}' must be a string (got {type(value).__name__})."
            )
        result[key] = os.path.expanduser(value)
    return result


def _load_sandbox_block(block: Any, source: Path, prefix: str) -> SandboxConfig:
    """Normalize a sandbox mapping (found at `prefix`) into a SandboxConfig."""
    if not isinstance(block, dict):
        return SandboxConfig()

    driver = str(block.get("driver", "none"))
    image = str(block.get("image", ""))
    network = str(block.get("network", "default"))
    user_raw = block.get("user", "auto")
    user = "" if user_raw is None else str(user_raw)

    if driver == "docker" and not image.strip():
        raise ConfigError(
            f"{source}: '{prefix}.image' is required when '{prefix}.driver: docker' "
            f"(empty string is not allowed).\n{_SANDBOX_EXAMPLE}"
        )

    if network not in ("default", "none"):
        raise ConfigError(
            f"{source}: '{prefix}.network' must be 'default' or 'none' "
            f"(got {network!r}).\n{_SANDBOX_EXAMPLE}"
        )

    env_passthrough_raw = block.get("env_passthrough")
    if env_passthrough_raw is None:
        env_passthrough: list[str] = []
    elif isinstance(env_passthrough_raw, list):
        env_passthrough = [str(v) for v in env_passthrough_raw]
    else:
        env_passthrough = [str(env_passthrough_raw)]

    volumes_raw = block.get("volumes")
    if volumes_raw is None:
        volumes: list[str] = []
    elif isinstance(volumes_raw, list):
        volumes = [str(v) for v in volumes_raw]
    else:
        volumes = [str(volumes_raw)]

    isolate_dirs = _normalize_isolate_dirs(block.get("isolate_dirs"), source)

    return SandboxConfig(
        driver=driver,
        image=image,
        network=network,
        user=user,
        env=_load_sandbox_env(block, source, prefix),
        env_passthrough=env_passthrough,
        volumes=volumes,
        isolate_dirs=isolate_dirs,
    )


def validate_verify_path(raw: str, context: str) -> str:
    """Validate and normalize a `verify` registry path (config side or spec side).

    Rejects an empty string, an absolute path, and '..' segments that would escape
    the worktree. Returns the normalized (posixpath.normpath'd) path.
    """
    text = str(raw).strip()
    if not text:
        raise ConfigError(f"{context}: 'path' must not be empty.\n{_VERIFY_EXAMPLE}")
    norm = posixpath.normpath(text)
    if norm.startswith("/"):
        raise ConfigError(
            f"{context}: 'path' must be a worktree-relative path "
            f"(absolute path {norm!r} is not allowed).\n{_VERIFY_EXAMPLE}"
        )
    if ".." in norm.split("/"):
        raise ConfigError(
            f"{context}: 'path' must not contain '..' (got {norm!r}).\n{_VERIFY_EXAMPLE}"
        )
    return norm


def _load_verify(raw: dict[str, Any], source: Path) -> VerifyConfig:
    """Normalize the verify: block into a VerifyConfig.

    Raises a migration ConfigError if the old 'test_command' / 'sandbox' keys are
    still present at the top level.
    """
    if "test_command" in raw or "sandbox" in raw:
        raise ConfigError(f"{source}: {_VERIFY_MIGRATION_EXAMPLE}")

    block = raw.get("verify")
    if block is None:
        return VerifyConfig()

    if isinstance(block, dict):
        if "command" in block or "path" in block:
            raise ConfigError(
                f"{source}: single-form 'verify:' must only have 'sandbox' "
                f"('command' / 'path' are not allowed here; commands belong in the spec's "
                f"frontmatter).\n{_VERIFY_EXAMPLE}"
            )
        sandbox = _load_sandbox_block(block.get("sandbox"), source, "verify.sandbox")
        return VerifyConfig(single=sandbox)

    if isinstance(block, list):
        entries: list[tuple[str, SandboxConfig]] = []
        seen: set[str] = set()
        for i, item in enumerate(block):
            if not isinstance(item, dict):
                raise ConfigError(
                    f"{source}: 'verify[{i}]' must be a mapping with 'path' / 'sandbox'.\n"
                    f"{_VERIFY_EXAMPLE}"
                )
            if "command" in item:
                raise ConfigError(
                    f"{source}: 'verify[{i}].command' is not allowed in config; the command "
                    f"belongs in the spec's frontmatter 'verify:'.\n{_VERIFY_EXAMPLE}"
                )
            path = validate_verify_path(item.get("path", ""), f"{source}: 'verify[{i}].path'")
            if path in seen:
                raise ConfigError(
                    f"{source}: duplicate 'verify' path {path!r} (index {i}).\n{_VERIFY_EXAMPLE}"
                )
            seen.add(path)
            sandbox = _load_sandbox_block(item.get("sandbox"), source, f"verify[{i}].sandbox")
            entries.append((path, sandbox))
        return VerifyConfig(entries=entries)

    raise ConfigError(
        f"{source}: 'verify' must be a mapping (single form) or a list (list form).\n"
        f"{_VERIFY_EXAMPLE}"
    )


def _load_env(raw: dict[str, Any], source: Path) -> dict[str, str]:
    """Normalize the env block into a str->str mapping."""
    block = raw.get("env")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ConfigError(f"{source}: 'env' must be a mapping.")
    result: dict[str, str] = {}
    for key, value in block.items():
        if not isinstance(key, str):
            raise ConfigError(f"{source}: keys of 'env' must be strings.")
        if not isinstance(value, str):
            raise ConfigError(
                f"{source}: 'env.{key}' must be a string (got {type(value).__name__})."
            )
        result[key] = os.path.expanduser(value)
    return result


def _load_transient_error_patterns(raw: dict[str, Any]) -> list[str]:
    """Normalize transient_error_patterns from either a single str or a list."""
    patterns_raw = raw.get("transient_error_patterns")
    if patterns_raw is None:
        return list(_DEFAULT_TRANSIENT_ERROR_PATTERNS)
    if isinstance(patterns_raw, str):
        return [patterns_raw]
    if isinstance(patterns_raw, list):
        return [str(p) for p in patterns_raw]
    return [str(patterns_raw)]


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_]\w*)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    """Expand ${VAR} / ${VAR:-default} in config values using environment variables."""
    if isinstance(value, str):

        def replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default = match.group(2)
            if var_name in os.environ:
                return os.environ[var_name]
            if default is None:
                raise ConfigError(f"Environment variable '{var_name}' is not set.")
            return default

        return _ENV_VAR_PATTERN.sub(replacer, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge nested mappings key by key; scalars/lists are replaced by override."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _candidate_paths(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    home = Path.home()
    return [
        home / ".ghswarm.yaml",
        home / ".ghswarm.yml",
    ]


def _build_repo_config_from_raw(
    alias: str, repo: str, path: str, raw: dict[str, Any], source: Path
) -> RepoConfig:
    """Build a RepoConfig from a merged dict."""
    agents = _load_agents(raw.get("agents") or {}, source)

    labels_raw = raw.get("labels") or {}
    labels = LabelConfig(
        idle=labels_raw.get("idle", LabelConfig.idle),
        blocked=labels_raw.get("blocked", LabelConfig.blocked),
        completed=labels_raw.get("completed", LabelConfig.completed),
        busy_prefix=labels_raw.get("busy_prefix", LabelConfig.busy_prefix),
    )

    return RepoConfig(
        agents=agents,
        name=alias,
        repo=repo,
        path=path,
        labels=labels,
        target=_load_target_filter(raw),
        notify=_load_notify(raw),
        poll_interval=int(raw.get("poll_interval", 60)),
        max_retries=int(raw.get("max_retries", 3)),
        question_file=raw.get("question_file", ".agent_question.md"),
        branch_prefix=raw.get("branch_prefix", "issue-"),
        spec_dir=raw.get("spec_dir", ".specs"),
        base_branch=raw.get("base_branch", ""),
        merge_method=raw.get("merge_method", "squash"),
        require_approval=bool(raw.get("require_approval", True)),
        address_pr_reviews=bool(raw.get("address_pr_reviews", True)),
        delete_branch_on_merge=bool(raw.get("delete_branch_on_merge", True)),
        post_merge_ci=bool(raw.get("post_merge_ci", True)),
        post_merge_ci_grace=int(raw.get("post_merge_ci_grace", 180)),
        worktree_dir=raw.get("worktree_dir", ""),
        worktree_setup=raw.get("worktree_setup", ""),
        transient_error_patterns=_load_transient_error_patterns(raw),
        transient_max_retries=int(raw.get("transient_max_retries", 5)),
        auto_resolve_conflicts=bool(raw.get("auto_resolve_conflicts", True)),
        conflict_max_retries=int(raw.get("conflict_max_retries", 3)),
        auto_fix_ci=bool(raw.get("auto_fix_ci", True)),
        ci_fix_max_retries=int(raw.get("ci_fix_max_retries", 3)),
        issue_max_agent_runs=int(raw.get("issue_max_agent_runs", 10)),
        lock_ttl=int(raw.get("lock_ttl", 14400)),
        event_db=raw.get("event_db", "~/.ghswarm/events.db"),
        activity_dir=raw.get("activity_dir", "~/.ghswarm/activity"),
        env=_load_env(raw, source),
        verify=_load_verify(raw, source),
    )


def load_app_config(explicit: str | None = None) -> AppConfig:
    path: Path | None = None
    for cand in _candidate_paths(explicit):
        if cand.is_file():
            path = cand
            break
    if path is None:
        raise ConfigError("No config file found. Run `ghswarm init` to create a template.")

    with path.open(encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    raw = _expand_env(raw)

    defaults_raw = raw.get("defaults")
    if defaults_raw is not None and not isinstance(defaults_raw, dict):
        raise ConfigError(f"{path}: 'defaults' must be a mapping.")

    repos_raw = raw.get("repositories")
    if not isinstance(repos_raw, dict) or not repos_raw:
        raise ConfigError(
            f"{path}: 'repositories' must specify at least one repository as a mapping."
        )

    max_parallel = raw.get("max_parallel_repos", 3)
    if not isinstance(max_parallel, int) or max_parallel < 1:
        raise ConfigError(
            f"{path}: 'max_parallel_repos' must be an integer >= 1 (got {max_parallel!r})."
        )

    defaults: dict[str, Any] = dict(defaults_raw or {})
    repositories: dict[str, RepoConfig] = {}
    for alias, entry in repos_raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: 'repositories.{alias}' must be a mapping.")
        repo_name = entry.get("repo")
        repo_path = entry.get("path")
        if not repo_name or not isinstance(repo_name, str):
            raise ConfigError(f"{path}: 'repositories.{alias}.repo' (owner/repo) is required.")
        if not repo_path or not isinstance(repo_path, str):
            raise ConfigError(
                f"{path}: 'repositories.{alias}.path' (local clone path) is required."
            )
        merged = _deep_merge(defaults, entry)
        repositories[str(alias)] = _build_repo_config_from_raw(
            str(alias), repo_name, repo_path, merged, path
        )

    daemon_log = os.path.expanduser(raw.get("daemon_log", "~/.ghswarm/ghswarm.log"))
    daemon_pid = os.path.expanduser(raw.get("daemon_pid", "~/.ghswarm/ghswarm.pid"))

    return AppConfig(
        repositories=repositories,
        max_parallel_repos=max_parallel,
        daemon_log=daemon_log,
        daemon_pid=daemon_pid,
        source_path=path,
    )


def load_config(explicit: str | None = None) -> AppConfig:
    """Load the home config (~/.ghswarm.yaml) and return an AppConfig."""
    return load_app_config(explicit)


def resolve_worktree_dir(worktree_dir: str, repo_root: Path) -> Path:
    """Resolve where worktrees are placed.

    If empty, use the default `../<repo_root-dir-name>-worktrees`.
    Relative paths are resolved relative to repo_root.
    """
    raw = worktree_dir or f"../{repo_root.name}-worktrees"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


_DEFAULT_ACTIVITY_DIR = "~/.ghswarm/activity"


def resolve_activity_dir(raw: str) -> str:
    """Resolve the activity_dir config value to an absolute path. An empty string stays disabled."""
    if raw == "":
        return ""
    path = raw or _DEFAULT_ACTIVITY_DIR
    return os.path.expanduser(path)
