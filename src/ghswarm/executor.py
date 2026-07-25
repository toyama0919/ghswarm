"""Non-interactive (headless) execution of coding CLIs with a self-healing loop.

Run the CLI -> verify with tests -> on failure, feed the test log into the next
prompt and retry (up to N times). Even on failure the work is never rolled back:
it is left in place and reported on the Issue so a human can pick up where it left
off.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AgentConfig, RepoConfig
from .git_ops import Git
from .logging_utils import get_logger
from .sandbox import ShellRunner

log = get_logger("ghswarm.exec")

# Header used for a path-less (legacy) verify step's output.
ROOT_STEP_LABEL = "(root)"


def is_transient(output: str, patterns: list[str]) -> bool:
    """Return whether the output matches any transient-error pattern (case-insensitive)."""
    for pattern in patterns:
        if re.search(pattern, output, re.IGNORECASE):
            return True
    return False


@dataclass
class ExecResult:
    ok: bool
    output: str
    attempts: int
    reason: str = ""  # failure reason (cli_failed / tests_failed / max_retries)


@dataclass
class ConflictResolveResult:
    ok: bool
    clean_merge: bool = False
    reason: str = ""
    output: str = ""
    attempts: int = 0


_CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


def _has_conflict_markers(paths: list[str], cwd: str) -> bool:
    root = Path(cwd)
    for rel in paths:
        path = root / rel
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.lstrip()
            if any(stripped.startswith(marker) for marker in _CONFLICT_MARKERS):
                return True
    return False


def _build_command(template: str, prompt: str) -> str:
    quoted = shlex.quote(prompt)
    if "{prompt}" in template:
        return template.replace("{prompt}", quoted)
    return f"{template} {quoted}"


def _run_single_cli(
    cmd_template: str, agent_name: str, prompt: str, cwd: str, timeout: int
) -> tuple[int, str]:
    cmd = _build_command(cmd_template, prompt)
    log.info("[%s] running: %s", agent_name, cmd[:200])
    try:
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return res.returncode, (res.stdout or "") + (res.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"ERROR: {agent_name} timed out ({timeout}s)"


def run_cli(
    agent: AgentConfig,
    prompt: str,
    cwd: str,
    timeout: int = 1800,
    question_file: str = ".agent_question.md",
) -> tuple[int, str]:
    outputs: list[str] = []
    last_code = 1

    for i, cmd_template in enumerate(agent.commands):
        if i > 0:
            qpath = Path(cwd) / question_file
            if qpath.is_file():
                qpath.unlink()
                log.info("removing stale question_file before fallback: %s", qpath)
            log.warning(
                "[%s] command %d/%d failed with exit %d. Trying fallback %d/%d: %s",
                agent.name,
                i,
                len(agent.commands),
                last_code,
                i + 1,
                len(agent.commands),
                cmd_template[:100],
            )

        last_code, output = _run_single_cli(cmd_template, agent.name, prompt, cwd, timeout)
        outputs.append(output)

        if last_code == 0:
            log.info(
                "[%s] command %d/%d succeeded: %s",
                agent.name,
                i + 1,
                len(agent.commands),
                cmd_template[:100],
            )
            return 0, output

    return last_code, "".join(outputs)


@dataclass
class ResolvedStep:
    """A verify step with its execution environment already resolved.

    path is the step's worktree-relative subdirectory (None for a legacy, path-less
    step); runner determines local vs. Docker execution.
    """

    path: str | None
    command: str
    runner: ShellRunner


def run_verify_steps(
    steps: list[ResolvedStep],
    worktree: str,
    timeout: int = 1800,
) -> tuple[int, str]:
    """Run resolved verify steps in order, stopping at the first failure.

    Zero steps is treated as success (mirrors the old "empty verify command skips
    verification" behavior). Each step's output is concatenated with a header
    identifying its path (a path-less step uses "(root)"). On failure, the combined
    log includes steps up to and including the failing one, not the ones after it.
    """
    if not steps:
        return 0, "(no verify steps configured; skipping verification)"

    combined = ""
    for step in steps:
        label = step.path or ROOT_STEP_LABEL
        log.info("running verify step [%s]: %s", label, step.command)
        try:
            code, output = step.runner.run(step.command, worktree, step.path, timeout)
        except subprocess.TimeoutExpired:
            code, output = 124, f"ERROR: verify step [{label}] timed out ({timeout}s)"
        combined += f"\n--- verify step [{label}] ---\n{output}\n"
        if code != 0:
            return code, combined

    return 0, combined


def execute_with_self_healing(
    cfg: RepoConfig,
    agent: AgentConfig,
    git: Git,
    verify_steps: list[ResolvedStep],
    base_prompt: str,
    on_question,
) -> ExecResult:
    """Run the CLI and, if the tests fail, retry with the failure log attached.

    on_question: callback invoked after each run. If a clarification signal
    (.agent_question.md) is present it returns True, and execution stops
    immediately as "blocked".
    """
    prompt = base_prompt
    combined = ""
    for attempt in range(1, cfg.max_retries + 1):
        log.info("--- attempt %d/%d (%s) ---", attempt, cfg.max_retries, agent.name)
        code, output = run_cli(agent, prompt, git.cwd, question_file=cfg.question_file)
        combined += f"\n=== attempt {attempt} ({agent.name}) ===\n{output}\n"

        # If a clarification signal was raised, stop before verifying with tests.
        if on_question():
            return ExecResult(False, combined, attempt, reason="blocked")

        if code != 0:
            if is_transient(output, cfg.transient_error_patterns):
                log.warning(
                    "CLI run failed with a transient error (exit %s). Retrying via loop.", code
                )
                return ExecResult(False, combined, attempt, reason="transient")
            log.warning(
                "CLI run failed (exit %s). Backing out but leaving the work in place.", code
            )
            return ExecResult(False, combined, attempt, reason="cli_failed")

        test_code, test_output = run_verify_steps(verify_steps, git.cwd)
        if test_code == 0:
            return ExecResult(True, combined, attempt)

        if is_transient(test_output, cfg.transient_error_patterns):
            log.warning(
                "Test run failed with a transient error (exit %s). Retrying via loop.", test_code
            )
            return ExecResult(False, combined, attempt, reason="transient")

        # Tests failed -> ask the agent to self-correct with the failure log attached.
        log.warning("Tests failed. Instructing the agent to self-correct and retrying.")
        combined += f"\n--- tests failed (attempt {attempt}, exit {test_code}) ---\n{test_output}\n"
        prompt = (
            f"{base_prompt}\n\n"
            f"The tests below failed after your last change. Identify the root cause and fix it.\n"
            f"--- test output (tail) ---\n{test_output[-2000:]}\n"
        )

    log.error(
        "Reached the maximum retry count (%d). Leaving the work in place and reporting on the Issue.",
        cfg.max_retries,
    )
    return ExecResult(False, combined, cfg.max_retries, reason="max_retries")


def resolve_conflict_with_agent(
    cfg: RepoConfig,
    agent: AgentConfig,
    git: Git,
    verify_steps: list[ResolvedStep],
    base: str,
    branch: str,
    prompt_header: str,
) -> ConflictResolveResult:
    """Merge base into the worktree, and only on conflict have the agent resolve it.

    The caller handles commit/push. On failure the merge is aborted to return to a
    clean state.
    """
    if not git.sync_branch_to_remote(branch):
        return ConflictResolveResult(False, reason="sync_failed")

    merge_result = git.merge_base(base)
    if merge_result == "clean":
        return ConflictResolveResult(True, clean_merge=True)

    conflict_files = merge_result
    if not conflict_files:
        git.abort_merge()
        return ConflictResolveResult(False, reason="merge_failed")
    prompt = (
        f"{prompt_header}\n"
        f"The files below have merge conflicts. Integrate them while preserving the "
        f"intent of both sides of the change, and leave verify passing. Do not leave any "
        f"conflict markers (<<<<<<< / ======= / >>>>>>>). Do not commit "
        f"(ghswarm will do that).\n"
        f"--- conflicting files ---\n"
        + "\n".join(conflict_files)
        + "\n--- /conflicting files ---\n"
    )

    code, output = run_cli(agent, prompt, git.cwd, question_file=cfg.question_file)
    combined = output
    if code != 0:
        git.abort_merge()
        return ConflictResolveResult(False, reason="cli_failed", output=combined, attempts=1)

    if _has_conflict_markers(conflict_files, git.cwd):
        git.abort_merge()
        return ConflictResolveResult(False, reason="markers_remain", output=combined, attempts=1)

    test_code, test_output = run_verify_steps(verify_steps, git.cwd)
    combined += f"\n--- verify ---\n{test_output}\n"
    if test_code != 0:
        git.abort_merge()
        return ConflictResolveResult(False, reason="verify_failed", output=combined, attempts=1)

    return ConflictResolveResult(True, clean_merge=False, output=combined, attempts=1)


def fix_ci_with_agent(
    cfg: RepoConfig,
    agent: AgentConfig,
    git: Git,
    verify_steps: list[ResolvedStep],
    branch: str,
    prompt_header: str,
    ci_logs: str,
) -> ExecResult:
    """Bring the worktree up to date and have the implement agent fix the CI failure using its logs.

    The caller handles commit/push. verify is used as a gate, and transient errors
    are returned as "transient".
    """
    if not git.sync_branch_to_remote(branch):
        return ExecResult(False, "", 0, reason="sync_failed")

    logs_block = ci_logs.strip() or "(could not retrieve logs)"
    prompt = (
        f"{prompt_header}\n"
        f"Below is the failing CI (GitHub Actions) log for the PR. Identify the root cause, "
        f"fix it, and leave verify passing. Do not commit (ghswarm will do that).\n"
        f"--- CI failure log ---\n{logs_block}\n--- /CI failure log ---\n"
    )

    code, output = run_cli(agent, prompt, git.cwd, question_file=cfg.question_file)
    combined = output
    if code != 0:
        if is_transient(output, cfg.transient_error_patterns):
            return ExecResult(False, combined, 1, reason="transient")
        return ExecResult(False, combined, 1, reason="cli_failed")

    test_code, test_output = run_verify_steps(verify_steps, git.cwd)
    combined += f"\n--- verify ---\n{test_output}\n"
    if test_code != 0:
        if is_transient(test_output, cfg.transient_error_patterns):
            return ExecResult(False, combined, 1, reason="transient")
        return ExecResult(False, combined, 1, reason="verify_failed")

    return ExecResult(True, combined, 1)
