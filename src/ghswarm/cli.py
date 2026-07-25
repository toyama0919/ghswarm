"""Command-line interface.

  ghswarm config [-r ALIAS]          Print the current repo's resolved config as JSON
  ghswarm init                       Write a config file template (~/.ghswarm.yaml)
  ghswarm run 42 -r a                Run Issue #42 to completion (done) for a repo
  ghswarm run 42 --step              Advance Issue #42 by a single step
  ghswarm run 42 --resume            Resume an Issue awaiting clarification
  ghswarm loop                       Poll all repositories in parallel (long-running)
  ghswarm status                     List the Issue state for the target repositories

Config is centralized in the home file ~/.ghswarm.yaml. Select an alias with `-r` / `--repo`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, wait
from datetime import datetime, timezone
from importlib.resources import as_file, files
from pathlib import Path
from typing import Callable

from . import labels as lbl
from . import state as st
from . import activity
from . import daemon
from .config import AppConfig, ConfigError, RepoConfig, load_config, resolve_activity_dir
from .events import EventLog, resolve_event_db_path
from .github import GitHub, GitHubError, detect_default_branch
from .logging_utils import get_logger, get_repo_logger, setup_logging
from .orchestrator import Orchestrator

log = get_logger("ghswarm.cli")

_stop_event = threading.Event()

CONFIG_TEMPLATE = files("ghswarm") / "config.example.yaml"
SKILLS_SOURCE = files("ghswarm") / "skills"
DEFAULT_CONFIG_PATH = Path.home() / ".ghswarm.yaml"


def _load(args) -> AppConfig:
    try:
        return load_config(args.config)
    except ConfigError as e:
        log.error("%s", e)
        sys.exit(2)


def _select_repos(app: AppConfig, aliases: list[str] | None) -> list[RepoConfig]:
    """Return the list of target RepoConfigs filtered by alias. All if omitted."""
    if not aliases:
        return list(app.repositories.values())
    repos: list[RepoConfig] = []
    for alias in dict.fromkeys(aliases):
        if alias not in app.repositories:
            raise ConfigError(f"Unknown repository alias: {alias}")
        repos.append(app.repositories[alias])
    return repos


def _filter_missing_paths(repos: list[RepoConfig]) -> list[RepoConfig]:
    """Exclude repos whose local path does not exist, logging one warning each. Preserves order."""
    kept: list[RepoConfig] = []
    for cfg in repos:
        if Path(cfg.path).expanduser().is_dir():
            kept.append(cfg)
        else:
            log.warning("Repo %r's local path does not exist, skipping: %s", cfg.name, cfg.path)
    return kept


def _build_issue_create_args(cfg: RepoConfig) -> list[str]:
    """Build the flag array for gh issue create from the idle label and target."""
    args: list[str] = []
    seen_labels: set[str] = set()

    def add_label(label: str) -> None:
        if label and label not in seen_labels:
            seen_labels.add(label)
            args.extend(["--label", label])

    add_label(cfg.labels.idle)
    for label in cfg.target.labels:
        add_label(label)
    if cfg.target.assignee:
        args.extend(["--assignee", cfg.target.assignee])
    if cfg.target.milestone:
        args.extend(["--milestone", cfg.target.milestone])
    return args


def _select_single_repo_by_cwd(app: AppConfig, alias: str | None) -> RepoConfig:
    """Identify a single RepoConfig (explicit -r, or by matching cwd against path)."""
    if alias:
        if alias not in app.repositories:
            raise ConfigError(f"Unknown repository alias: {alias}")
        return app.repositories[alias]

    cwd = Path.cwd().resolve()
    best: RepoConfig | None = None
    best_len = -1
    for cfg in app.repositories.values():
        repo_path = Path(cfg.path).expanduser().resolve()
        if cwd == repo_path or repo_path in cwd.parents:
            path_len = len(repo_path.parts)
            if path_len > best_len:
                best = cfg
                best_len = path_len

    if best is None:
        raise ConfigError(
            "No repository matches the current directory. Specify a target with -r/--repo."
        )
    return best


def _select_repo_for_config(app: AppConfig, alias: str | None) -> RepoConfig:
    """Identify a single RepoConfig for config (explicit -r, or by matching cwd against path)."""
    return _select_single_repo_by_cwd(app, alias)


def _add_repo_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-r",
        "--repo",
        action="append",
        dest="repos",
        metavar="ALIAS",
        help="target repository alias (repeatable; all if omitted)",
    )


def cmd_config(args) -> int:
    app = _load(args)
    try:
        repo_aliases = list(dict.fromkeys(getattr(args, "repos", None) or []))
        if len(repo_aliases) > 1:
            raise ConfigError("config accepts only one repository. Specify exactly one -r.")
        alias = repo_aliases[0] if repo_aliases else None
        cfg = _select_repo_for_config(app, alias)
    except ConfigError as e:
        log.error("%s", e)
        return 2

    base_branch = cfg.base_branch
    if not base_branch:
        try:
            base_branch = detect_default_branch(str(Path(cfg.path).expanduser()), env=cfg.env)
        except GitHubError:
            base_branch = ""

    payload = {
        "name": cfg.name,
        "repo": cfg.repo,
        "path": cfg.path,
        "spec_dir": cfg.spec_dir,
        "branch_prefix": cfg.branch_prefix,
        "base_branch": base_branch,
        "idle_label": cfg.labels.idle,
        "blocked_label": cfg.labels.blocked,
        "target": {
            "labels": list(cfg.target.labels),
            "assignee": cfg.target.assignee,
            "milestone": cfg.target.milestone,
        },
        "issue_create_args": _build_issue_create_args(cfg),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_init(args) -> int:
    dest = Path(args.output or DEFAULT_CONFIG_PATH)
    if dest.exists() and not args.force:
        log.error("%s already exists (use --force to overwrite)", dest)
        return 1
    try:
        template = CONFIG_TEMPLATE.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError) as e:
        log.error("Config template not found (%s): %s", CONFIG_TEMPLATE, e)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(template, encoding="utf-8")
    log.info("Created config file template: %s", dest)
    print(f"Please edit it: {dest}")
    return 0


def cmd_skills_install(args) -> int:
    """Copy the bundled Claude Code skills into a .claude/skills directory.

    The skills are shipped as package data, so the installed version always matches
    the installed CLI. --global (default) targets ~/.claude/skills so they are
    available regardless of cwd; --project targets ./.claude/skills.
    """
    if args.dir:
        dest_root = Path(args.dir).expanduser()
    elif args.project:
        dest_root = Path.cwd() / ".claude" / "skills"
    else:
        dest_root = Path.home() / ".claude" / "skills"

    installed: list[str] = []
    skipped: list[str] = []
    try:
        with as_file(SKILLS_SOURCE) as src_root:
            skill_dirs = sorted(p for p in Path(src_root).iterdir() if p.is_dir())
            if not skill_dirs:
                log.error("No bundled skills found at %s", src_root)
                return 1
            dest_root.mkdir(parents=True, exist_ok=True)
            for skill_dir in skill_dirs:
                dest = dest_root / skill_dir.name
                if dest.exists() and not args.force:
                    skipped.append(skill_dir.name)
                    continue
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(skill_dir, dest)
                installed.append(skill_dir.name)
    except (OSError, FileNotFoundError) as e:
        log.error("Failed to install skills: %s", e)
        return 1

    for name in installed:
        print(f"Installed skill: {name} -> {dest_root / name}")
    for name in skipped:
        print(f"Skipped (exists, use --force to overwrite): {name}")
    if not installed and skipped:
        log.info("All skills already present. Re-run with --force to update them.")
    return 0


def _target_issues(cfg: RepoConfig, gh: GitHub):
    t = cfg.target
    issues = gh.list_open_issues(
        labels=t.labels or None,
        assignee=t.assignee,
        milestone=t.milestone,
    )
    # Issues created via the ghswarm-spec skill carry at least idle. Exclude any Issue
    # with no status label as unmanaged by ghswarm (judged from the labels: config values,
    # not dependent on the literal "status:" string).
    managed = [i for i in issues if any(cfg.labels.is_status_label(lb) for lb in i.labels)]
    # Issue numbers increase monotonically, so ascending number = oldest first. Start with the oldest.
    return sorted(managed, key=lambda i: i.number)


# Polling-oriented phases that wait for CI or a human reply. These are not counted as
# the one active development slot; their state is checked every cycle (so the next Issue
# can proceed while waiting). However, a cycle in which real work actually ran (merge,
# review handling, etc.) is detected as non-skipped in _process_cycle and counted as that slot.
# verify_merge is post-merge CI polling (_verify_merge), so it is treated as waiting.
_WAITING_ACTIONS = frozenset({"wait_ci", "wait_for_clarification", "verify_merge"})

# Cap on process() calls per Issue (guards against infinite loops).
_RUN_MAX_ITERATIONS = 50


def _run_to_completion(
    cfg: RepoConfig,
    orch,
    number: int,
    *,
    force: bool = False,
    resume: bool = False,
    sleep_fn=time.sleep,
    repo_log=None,
) -> int:
    """Iterate over the given Issue until the state machine reaches its terminal state (done).

    Stop/wait/continue is classified via a decision table over StepResult.action and
    next_action (raw value). The return value is an exit code (0=clean stop, 1=cannot
    make progress or hit the iteration cap).
    """
    rlog = repo_log or log
    resume_flag = resume
    for _ in range(_RUN_MAX_ITERATIONS):
        result = orch.process(number, force=force, resume=resume_flag)
        rlog.info(
            "Issue #%s -> %s: %s",
            result.issue_number,
            result.action,
            result.detail,
        )
        resume_flag = False

        issue = orch.gh.get_issue(number)
        next_action = st.parse_state(issue.body, issue.number, cfg.branch_prefix).next_action

        if next_action == "done":
            return 0
        if next_action == "wait_for_clarification":
            rlog.info("Awaiting clarification. Resume with --resume")
            return 0
        if result.action in ("blocked", "failed"):
            return 1
        if next_action in ("wait_ci", "verify_merge"):
            sleep_fn(cfg.poll_interval)
            continue
        if result.action == "skipped":
            rlog.info("Skipped: %s", result.detail or "(no reason)")
            return 0
        # Active progress (implemented / reviewed / retry_pending, etc.) — continue immediately
        continue

    rlog.warning(
        "Issue #%s: hit the process() cap (%d calls); aborting",
        number,
        _RUN_MAX_ITERATIONS,
    )
    return 1


def _phase_kind(cfg: RepoConfig, issue) -> str:
    """Classify the Issue's next action as active / waiting / done.

    active: an active development step such as implement/ai_review/create_pr.
    Only one Issue is active at a time, to avoid ping-ponging between Issues.
    """
    action = st.parse_state(issue.body, issue.number, cfg.branch_prefix).next_action
    if action == "done":
        return "done"
    if action in _WAITING_ACTIONS:
        return "waiting"
    return "active"


def _process_cycle(
    cfg: RepoConfig,
    orch: Orchestrator,
    *,
    repo_log=None,
    daemon_pid: int | None = None,
) -> None:
    """Make one pass over target Issues oldest-first, keeping active development to one at a time.

    Issues waiting on CI/replies (waiting) are not counted as "the one in development" and
    are checked every cycle, so while the leading Issue is waiting the next Issue can proceed
    (without ping-ponging between Issues).
    """
    rlog = repo_log or log
    activity_path = resolve_activity_dir(cfg.activity_dir)
    try:
        active_taken = False
        for issue in _target_issues(cfg, orch.gh):
            kind = _phase_kind(cfg, issue)
            if kind == "done":
                continue
            # Once the first active Issue is advanced, defer the rest. waiting (CI/reply) is checked every time.
            if kind == "active" and active_taken:
                rlog.debug(
                    "Issue #%s: deferred because another Issue is in development", issue.number
                )
                continue
            next_action = st.parse_state(issue.body, issue.number, cfg.branch_prefix).next_action
            activity.write_activity(
                activity_path,
                cfg.repo,
                issue.number,
                next_action,
                daemon_pid if daemon_pid is not None else os.getppid(),
            )
            result = orch.process(issue.number)
            # A cycle in which real work ran (non-skipped) counts as that one slot. Even
            # while waiting, if a merge or review handling (_wait_ci) runs it consumes the
            # slot and suppresses starting the next active Issue.
            if result.action != "skipped":
                active_taken = True
                rlog.info(
                    "Issue #%s -> %s: %s",
                    result.issue_number,
                    result.action,
                    result.detail,
                )
    finally:
        activity.clear_activity(activity_path, cfg.repo)


def run_process_cycle_worker(
    repo_cfg: RepoConfig,
    *,
    dry_run: bool = False,
    verbose: bool = False,
    daemon_pid: int | None = None,
) -> None:
    """Run one cycle for a single repository (a ProcessPoolExecutor worker).

    Placed at module top level because it is called from spawned child processes.
    """
    setup_logging(verbose)
    rlog = get_repo_logger(repo_cfg.name)
    try:
        orch = Orchestrator(repo_cfg, dry_run=dry_run)
        _process_cycle(repo_cfg, orch, repo_log=rlog, daemon_pid=daemon_pid)
    except Exception as e:
        rlog.error("Error in loop processing: %s", e)


def _run_parallel_cycle(
    repos: list[RepoConfig],
    *,
    max_workers: int,
    dry_run: bool,
    verbose: bool,
    daemon_pid: int | None = None,
    executor_factory: Callable[[int], ProcessPoolExecutor] | None = None,
) -> None:
    """Run _process_cycle for all target repos in parallel and wait for all futures."""
    if not repos:
        return

    def _submit(pool: ProcessPoolExecutor) -> None:
        futures = [
            pool.submit(
                run_process_cycle_worker,
                repo_cfg,
                dry_run=dry_run,
                verbose=verbose,
                daemon_pid=daemon_pid,
            )
            for repo_cfg in repos
        ]
        wait(futures)

    if executor_factory is not None:
        with executor_factory(max_workers) as pool:
            _submit(pool)
        return

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        _submit(pool)


def cmd_run(args) -> int:
    app = _load(args)
    try:
        repo_aliases = list(dict.fromkeys(getattr(args, "repos", None) or []))
        if len(repo_aliases) > 1:
            raise ConfigError("run accepts only one repository. Specify exactly one -r.")
        alias = repo_aliases[0] if repo_aliases else None
        cfg = _select_single_repo_by_cwd(app, alias)
    except ConfigError as e:
        log.error("%s", e)
        return 2

    rlog = get_repo_logger(cfg.name) if cfg.name else log
    orch = Orchestrator(cfg, dry_run=args.dry_run)
    rc = 0
    single_step = args.step or args.dry_run
    try:
        for number in args.issues:
            num = int(number)
            try:
                if single_step:
                    result = orch.process(num, force=args.force, resume=args.resume)
                    rlog.info(
                        "Issue #%s -> %s: %s",
                        result.issue_number,
                        result.action,
                        result.detail,
                    )
                else:
                    issue_rc = _run_to_completion(
                        cfg,
                        orch,
                        num,
                        force=args.force,
                        resume=args.resume,
                        repo_log=rlog,
                    )
                    if issue_rc != 0:
                        rc = 1
            except Exception as e:
                rlog.error("Error processing Issue #%s: %s", number, e)
                rc = 1
    except KeyboardInterrupt:
        rlog.info("Interrupted")
        return 130
    return rc


def _handle_sigterm(signum, frame) -> None:
    _stop_event.set()


def _resolve_activity_dir_for_stop(app: AppConfig, repo_aliases: list[str] | None) -> str:
    try:
        repos = _select_repos(app, repo_aliases)
        if repos:
            return resolve_activity_dir(repos[0].activity_dir)
    except ConfigError:
        pass
    return resolve_activity_dir("~/.ghswarm/activity")


def _format_stop_activity_line(entry: activity.ActivityEntry, dot_count: int) -> str:
    dots = "." * (dot_count % 6 + 1)
    return f"    - {entry.repo:<22} #{entry.issue:<4}  {entry.phase} running {dots}"


def _wait_for_daemon_stop(
    pid: int,
    activity_dir: str,
    *,
    sleep_fn=time.sleep,
    is_alive_fn=daemon.is_alive,
    read_activities_fn=activity.read_activities,
    poll_interval: float = 0.5,
) -> None:
    showed_progress = False

    def on_tick(tick: int) -> None:
        nonlocal showed_progress
        entries = read_activities_fn(
            activity_dir,
            daemon_pid=pid,
            is_alive_fn=is_alive_fn,
        )
        if not entries:
            return
        if not showed_progress:
            print(
                "Waiting for in-flight commands to finish... "
                "(Ctrl-C cancels only the wait; the stop continues in the background)",
                flush=True,
            )
            print("  Running:", flush=True)
            showed_progress = True
        for entry in entries:
            print(_format_stop_activity_line(entry, tick), flush=True)
        print(
            f"  ({len(entries)} repositories in progress; waiting for completion)",
            flush=True,
        )

    daemon.wait_until_gone(
        pid,
        sleep_fn=sleep_fn,
        is_alive_fn=is_alive_fn,
        on_tick=on_tick,
        poll_interval=poll_interval,
    )


def _stop_and_wait(app, args) -> str:
    pid_before = daemon.read_pid(app.daemon_pid)
    if daemon.stop_daemon(app.daemon_pid):
        log.info("Sent SIGTERM to the ghswarm daemon (pid=%d)", pid_before)
        activity_dir = _resolve_activity_dir_for_stop(app, getattr(args, "repos", None))
        try:
            _wait_for_daemon_stop(
                pid_before,
                activity_dir,
                sleep_fn=getattr(args, "_stop_sleep_fn", time.sleep),
                is_alive_fn=getattr(args, "_stop_is_alive_fn", daemon.is_alive),
                read_activities_fn=getattr(
                    args, "_stop_read_activities_fn", activity.read_activities
                ),
            )
            print(
                f"✔ ghswarm daemon stopped (pid={pid_before})",
                flush=True,
            )
            return "stopped"
        except KeyboardInterrupt:
            print(
                "The daemon continues its stop in the background",
                flush=True,
            )
            return "interrupted"
    if pid_before is not None:
        log.info("Removed a stale PID file: %s", app.daemon_pid)
        return "absent"
    log.info("No ghswarm daemon is running")
    return "absent"


def cmd_loop(args) -> int:
    _stop_event.clear()

    app = _load(args)
    daemon_mode = getattr(args, "daemon", False)
    stop = getattr(args, "stop", False)
    once = getattr(args, "once", False)
    restart = getattr(args, "restart", False)

    if restart and stop:
        log.error("--restart and --stop cannot be used together")
        return 1
    if restart and once:
        log.error("--restart and --once cannot be used together")
        return 1

    daemon_mode = daemon_mode or restart

    if daemon_mode and stop:
        log.error("--daemon and --stop cannot be used together")
        return 1
    if daemon_mode and once:
        log.error("--daemon and --once cannot be used together")
        return 1

    if stop:
        _stop_and_wait(app, args)
        return 0

    if restart:
        outcome = _stop_and_wait(app, args)
        if outcome == "interrupted":
            log.info("Restart canceled (the daemon's stop continues in the background)")
            return 130
        if outcome == "absent":
            log.info("Starting fresh")
        daemon_mode = True

    if daemon_mode:
        if not hasattr(os, "fork"):
            log.error("Daemon mode (-d) is not supported in this environment")
            return 1
        running = daemon.already_running(app.daemon_pid)
        if running is not None:
            log.error("ghswarm daemon is already running (pid=%d)", running)
            return 1
        log_path = daemon.dated_log_path(app.daemon_log, datetime.now().strftime("%Y-%m-%d"))
        daemon.daemonize(log_path, app.daemon_pid)
        signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        try:
            repos = _select_repos(app, getattr(args, "repos", None))
        except ConfigError as e:
            log.error("%s", e)
            return 2

        repos = _filter_missing_paths(repos)

        if not repos:
            log.warning("No target repositories")
            return 0

        if daemon_mode:
            activity.clear_activity_dir(resolve_activity_dir(repos[0].activity_dir))

        interval = min(r.poll_interval for r in repos)
        names = ", ".join(r.name for r in repos)
        log.info(
            "Polling started (interval=%ds, repos=%s, max_parallel=%d)",
            interval,
            names,
            app.max_parallel_repos,
        )
        while not _stop_event.is_set():
            _run_parallel_cycle(
                repos,
                max_workers=app.max_parallel_repos,
                dry_run=args.dry_run,
                verbose=getattr(args, "verbose", False),
                daemon_pid=os.getpid() if daemon_mode else None,
                executor_factory=getattr(args, "_executor_factory", None),
            )
            if once:
                return 0
            _stop_event.wait(interval)
        return 0
    finally:
        if daemon_mode:
            daemon.remove_pid(app.daemon_pid)


def _print_repo_status(cfg: RepoConfig) -> None:
    gh = GitHub(cfg.repo, env=cfg.env)
    agent_names = cfg.agent_names()
    print(f"\n== [{cfg.name}] {cfg.repo} ==")
    for issue in _target_issues(cfg, gh):
        state = st.parse_state(issue.body, issue.number, cfg.branch_prefix)
        done, total = st.progress(issue.body)
        lock = lbl.is_locked(issue, cfg.labels, agent_names)
        if lock and lbl.is_stale(
            lock,
            cfg.labels,
            state,
            cfg.lock_ttl,
            datetime.now(timezone.utc),
            lbl.current_host(),
        ):
            lock = f"{lock}(stale)"
        lock = lock or "-"
        status = ",".join(issue.status_labels) or "-"
        ci = ""
        if state.next_action == "wait_ci" and state.pr_number:
            try:
                ps = gh.pr_status(state.pr_number)
                ci = f" pr#{ps.number}(ci={ps.checks},rev={ps.review_decision or '-'})"
            except Exception:
                ci = f" pr#{state.pr_number}(?)"
        print(
            f"  #{issue.number:<4} [{status}] next={state.next_action:<20} "
            f"tasks={done}/{total} lock={lock}{ci}  {issue.title}"
        )


def cmd_status(args) -> int:
    app = _load(args)
    try:
        repos = _select_repos(app, getattr(args, "repos", None))
    except ConfigError as e:
        log.error("%s", e)
        return 2

    if not getattr(args, "repos", None):
        repos = _filter_missing_paths(repos)

    for cfg in repos:
        _print_repo_status(cfg)
    return 0


def cmd_history(args) -> int:
    app = _load(args)
    try:
        repo_aliases = list(dict.fromkeys(getattr(args, "repos", None) or []))
        if len(repo_aliases) > 1:
            raise ConfigError("history accepts only one repository. Specify exactly one -r.")
        alias = repo_aliases[0] if repo_aliases else None
        if alias:
            if alias not in app.repositories:
                raise ConfigError(f"Unknown repository alias: {alias}")
            cfg = app.repositories[alias]
            db_path = resolve_event_db_path(cfg.event_db)
            filter_repo = cfg.repo
        else:
            cfg = next(iter(app.repositories.values()))
            db_path = resolve_event_db_path(cfg.event_db)
            filter_repo = None
    except ConfigError as e:
        log.error("%s", e)
        return 2

    events = EventLog(db_path).read(
        repo=filter_repo,
        issue=getattr(args, "issue", None),
        limit=args.limit,
    )
    if not events:
        print("No events")
        return 0

    show_repo = filter_repo is None
    for ev in events:
        line = f"{ev['ts']}  {ev['action']:<16}  #{ev['issue']:<4}  {ev['detail']}"
        if show_repo:
            line = f"[{ev['repo']}] {line}"
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ghswarm", description="GitHub Issue-driven development PM agent"
    )
    p.add_argument("-c", "--config", help="path to the config file")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("init", help="write a config file template")
    pi.add_argument(
        "-o",
        "--output",
        help=f"output path (default {DEFAULT_CONFIG_PATH})",
    )
    pi.add_argument("--force", action="store_true", help="overwrite an existing file")
    pi.set_defaults(func=cmd_init)

    pr = sub.add_parser(
        "run",
        help="run the given Issue to completion (--step for a single step)",
        description="run the given Issue to completion (--step for a single step)",
    )
    pr.add_argument("issues", nargs="+", help="Issue number(s)")
    _add_repo_arg(pr)
    for action in pr._actions:
        if action.dest == "repos":
            action.help = "target repository alias (auto-detected from cwd if omitted)"
            break
    pr.add_argument("--dry-run", action="store_true", help="show the plan without executing")
    pr.add_argument("--step", action="store_true", help="advance a single step and exit")
    pr.add_argument("--force", action="store_true", help="run ignoring the lock")
    pr.add_argument(
        "--resume",
        action="store_true",
        help="resume awaiting-clarification from the latest comment",
    )
    pr.set_defaults(func=cmd_run)

    pl = sub.add_parser("loop", help="poll the target repositories' Issues in parallel")
    _add_repo_arg(pl)
    pl.add_argument("--dry-run", action="store_true")
    pl.add_argument("--once", action="store_true", help="run a single pass and exit")
    pl.add_argument(
        "-d",
        "--daemon",
        action="store_true",
        help="run as a background daemon (stdout/stderr go to a log file)",
    )
    pl.add_argument("--stop", action="store_true", help="stop the running daemon")
    pl.add_argument(
        "--restart",
        action="store_true",
        help="stop the running daemon and restart it (started as if with -d)",
    )
    pl.set_defaults(func=cmd_loop)

    ps = sub.add_parser("status", help="list the Issue state for the target repositories")
    _add_repo_arg(ps)
    ps.set_defaults(func=cmd_status)

    ph = sub.add_parser(
        "history",
        help="list the local event log chronologically",
        description=(
            "List the local SQLite event log chronologically."
            " In v1 only a single DB is consulted (if event_db is split per repo,"
            " only the first repo's DB is shown when unspecified)."
        ),
    )
    _add_repo_arg(ph)
    ph.add_argument("--issue", type=int, metavar="N", help="filter by Issue number")
    ph.add_argument(
        "--limit", type=int, default=50, metavar="N", help="number of entries to show (default 50)"
    )
    ph.set_defaults(func=cmd_history)

    pc = sub.add_parser(
        "config",
        help="print the current repo's resolved config as JSON",
        description="print the current repo's resolved config as JSON",
    )
    _add_repo_arg(pc)
    pc.set_defaults(func=cmd_config)

    pk = sub.add_parser(
        "skills",
        help="manage the bundled Claude Code skills (ghswarm-spec / -check / -requirements)",
        description="manage the bundled Claude Code skills",
    )
    ksub = pk.add_subparsers(dest="skills_command", required=True)
    ki = ksub.add_parser(
        "install",
        help="copy the bundled skills into a .claude/skills directory",
        description=(
            "Copy the bundled skills into ~/.claude/skills (default) or ./.claude/skills "
            "(--project). The skills ship with the package, so they always match this CLI version."
        ),
    )
    kg = ki.add_mutually_exclusive_group()
    kg.add_argument(
        "--global",
        dest="project",
        action="store_false",
        help="install into ~/.claude/skills (default; available regardless of cwd)",
    )
    kg.add_argument(
        "--project",
        dest="project",
        action="store_true",
        help="install into ./.claude/skills of the current directory",
    )
    ki.add_argument("--dir", help="install into an explicit directory instead")
    ki.add_argument(
        "-f", "--force", action="store_true", help="overwrite skills that already exist"
    )
    ki.set_defaults(func=cmd_skills_install, project=False)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
