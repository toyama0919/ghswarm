"""Core state machine: advance a single Issue by one step based on its current state.

  check lock -> restore state -> select CLI -> prepare branch -> run CLI (self-healing)
  -> clarification / test verification -> persist state (body metadata + checkboxes)
  -> release label -> comment

All state is persisted on GitHub (labels + Issue body), so a run can resume from
where it left off.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import labels as lbl
from . import questions as q
from . import state as st
from .config import ConfigError, RepoConfig, resolve_worktree_dir
from .events import EventLog, resolve_event_db_path
from .executor import (
    ResolvedStep,
    execute_with_self_healing,
    fix_ci_with_agent,
    resolve_conflict_with_agent,
)
from .git_ops import Git, GitError
from .github import GitHub, GitHubError, Issue, PRStatus, ReviewItem, detect_default_branch
from .logging_utils import get_logger
from .notify import Notifier
from .sandbox import make_runner
from .spec import Spec

log = get_logger("ghswarm.orch")

# Marker for the "review addressed" comment that ghswarm posts on a PR itself.
# Used so ghswarm does not pick up its own comment as an unaddressed review.
REVIEW_RESPONSE_MARKER = "<!-- ghswarm:review-response -->"


@dataclass
class StepResult:
    issue_number: int
    # skipped / implemented / simplified / reviewed / review_addressed / pr_created / pr_updated /
    # blocked / failed / completed / merged / retry_pending
    action: str
    detail: str = ""


class Orchestrator:
    def __init__(self, cfg: RepoConfig, *, dry_run: bool = False):
        self.cfg = cfg
        self.repo_name = cfg.repo
        self.gh = GitHub(self.repo_name, env=cfg.env)
        # For the main repository. Reserved for ensure_worktree() / remove_worktree()
        # only; not used for any other git operations (checkout/add/commit/reset/clean).
        self.repo_root = str(Path(cfg.path).expanduser().resolve())
        self.git = Git(self.repo_root)
        self.base_branch = cfg.base_branch or detect_default_branch(self.repo_root, env=cfg.env)
        self.worktree_dir = resolve_worktree_dir(cfg.worktree_dir, Path(self.repo_root))
        self.worktree_setup = cfg.worktree_setup
        self.dry_run = dry_run
        self.agent_names = cfg.agent_names()
        self._notifier: Notifier | None = None
        self._event_log = EventLog(resolve_event_db_path(cfg.event_db))

    # -- worktree ------------------------------------------------------------
    def _worktree_path(self, number: int) -> str:
        return str(self.worktree_dir / f"{self.cfg.branch_prefix}{number}")

    def _git_for(self, path: str) -> Git:
        """All worktree-side `Git` instances are created through this (a test seam)."""
        return Git(path)

    def _worktree_ready(self, path: str, branch: str) -> bool:
        """Whether path is already a worktree with branch checked out."""
        if not Path(path).is_dir():
            return False
        return self._git_for(path).current_branch() == branch

    def _ensure_worktree_git(self, number: int, branch: str) -> Git:
        """Prepare the worktree for the issue and return the `Git` used for subsequent git operations.

        worktree_setup runs only when the worktree is newly created (it is not run
        when an existing worktree is reused).
        """
        path = self._worktree_path(number)
        is_new = not self._worktree_ready(path, branch)
        resolved = self.git.ensure_worktree(branch, self.base_branch, path)
        if is_new:
            self._run_worktree_setup(resolved)
        return self._git_for(resolved)

    def _run_worktree_setup(self, path: str) -> None:
        cmd = self.worktree_setup.strip()
        if not cmd:
            return
        log.info("running worktree setup: %s", cmd)
        runner = make_runner(self.cfg.verify.sandbox_for(None))
        try:
            code, out = runner.run(cmd, path, None, timeout=600)
        except subprocess.TimeoutExpired:
            log.warning("worktree setup timed out (600s): %s", cmd)
            return
        if code == 0:
            log.info("worktree setup complete:\n%s", out[-2000:])
        else:
            log.warning("worktree setup failed (exit %s, continuing):\n%s", code, out[-2000:])

    # -- entry point -------------------------------------------------------
    def process(self, number: int, *, force: bool = False, resume: bool = False) -> StepResult:
        try:
            result = self._step(number, force=force, resume=resume)
        except GitError as e:
            issue = self.gh.get_issue(number)
            state = st.parse_state(issue.body, number, self.cfg.branch_prefix)
            self._enter_blocked(issue, state, "git_error", str(e))
            result = StepResult(number, "blocked", str(e))
        event_log = getattr(self, "_event_log", None)
        if event_log and event_log.enabled and not getattr(self, "dry_run", False):
            event_log.record(self.repo_name, result, now=datetime.now(timezone.utc))
        return result

    def _step(self, number: int, *, force: bool = False, resume: bool = False) -> StepResult:
        issue = self.gh.get_issue(number)
        if issue.state != "open":
            return StepResult(number, "skipped", "Issue is already closed")
        if self.cfg.labels.completed in issue.labels:
            return StepResult(number, "skipped", "completed label")

        lock = lbl.is_locked(issue, self.cfg.labels, self.agent_names)
        state = st.parse_state(issue.body, number, self.cfg.branch_prefix)

        if (
            lock
            and not force
            and lbl.is_stale(
                lock,
                self.cfg.labels,
                state,
                self.cfg.lock_ttl,
                datetime.now(timezone.utc),
                lbl.current_host(),
            )
        ):
            log.info(
                "Issue #%s: reclaiming stale busy lock (owner=%s, lock=%s)",
                number,
                state.busy_owner or "?",
                lock,
            )
            self.gh.comment(
                number,
                f"🔓 **Reclaimed a stale busy lock**\n\n"
                f"- Lock: `{lock}`\n"
                f"- Owner: `{state.busy_owner or 'unknown'}`\n"
                f"- Acquired at: `{state.busy_since or 'unknown'}`\n"
                f"- Reclaimed at: `{datetime.now(timezone.utc).isoformat()}`",
            )
            lbl.reclaim(self.gh, issue, self.cfg.labels, self.agent_names)
            lock = None

        # Resume check for clarification waits (after stale reclaim; rescues a busy
        # lock left behind by a crash).
        if state.next_action == "wait_for_clarification" and not (force or resume):
            return StepResult(
                number, "skipped", "waiting for a clarification answer (use --resume)"
            )

        if lock and not force:
            # Skip unless we are resuming a blocked Issue.
            if not (resume and lock == self.cfg.labels.blocked):
                return StepResult(number, "skipped", f"locked: {lock}")

        if resume:
            state.last_notified_reason = None

        max_runs = self.cfg.issue_max_agent_runs
        if max_runs > 0 and state.total_agent_runs >= max_runs:
            if self.dry_run:
                return StepResult(number, "skipped", "[dry-run] budget_exhausted")
            self.gh.comment(
                number,
                f"🚫 **Cumulative agent-run cap reached** "
                f"({state.total_agent_runs}/{max_runs})\n\n"
                f"The total number of agent runs on this Issue has hit the cap, so "
                f"automatic processing has stopped (`status: blocked`).\n\n"
                f"After reviewing, either raise `issue_max_agent_runs` or reset "
                f"`total_agent_runs` in the Issue body, then resume with `--resume`.",
            )
            self._enter_blocked(
                issue,
                state,
                "budget_exhausted",
                detail=f"{state.total_agent_runs}/{max_runs}",
            )
            return StepResult(number, "blocked", "budget_exhausted")

        # Normalize the next action.
        action = state.next_action
        if action in ("start", "wait_for_clarification"):
            action = "implement"

        if action == "implement":
            return self._implement(issue, state, resume=resume)
        if action == "simplify":
            if not self.cfg.simplify_enabled:
                return self._review(issue, state)
            return self._simplify(issue, state)
        if action == "ai_review":
            return self._review(issue, state)
        if action == "create_pr":
            return self._create_pr(issue, state)
        if action == "wait_ci":
            return self._wait_ci(issue, state)
        if action == "verify_merge":
            return self._verify_merge(issue, state)
        if action == "done":
            return StepResult(number, "skipped", "already completed")
        return StepResult(number, "skipped", f"unknown next_action: {action}")

    # -- implement phase ---------------------------------------------------
    def _implement(self, issue: Issue, state: st.IssueState, *, resume: bool) -> StepResult:
        stripped = st.strip_state(issue.body)
        tasks = st.unchecked(stripped)

        if not st.has_verify_meta(issue.body):
            if self.dry_run:
                return StepResult(issue.number, "skipped", "[dry-run] cannot start: spec missing")
            return self._block_for_missing_spec(issue, state)

        if not tasks:
            # No pending tasks -> move on to simplify (if enabled) or AI review.
            next_after = "simplify" if self.cfg.simplify_enabled else "ai_review"
            log.info(
                "Issue #%s: no unfinished tasks. Transitioning to %s.",
                issue.number,
                next_after,
            )
            state.next_action = next_after
            if self.dry_run:
                return StepResult(
                    issue.number,
                    "skipped",
                    f"[dry-run] will transition to {next_after}",
                )
            self._persist(issue, state)
            fresh = self.gh.get_issue(issue.number)
            if next_after == "simplify":
                return self._simplify(fresh, state)
            return self._review(fresh, state)

        # Pass all unfinished tasks in a single CLI run. Because each CLI invocation is
        # a headless one-shot that loses its context, launching one per task would make
        # it re-read the same codebase over and over, and later tasks could not carry
        # over the intent of earlier ones.
        task_list = "\n".join(f"  {i}. {t.text}" for i, t in enumerate(tasks, 1))

        agent = self.cfg.agent_for("implement")
        agent_name = agent.name

        if self.dry_run:
            return StepResult(
                issue.number,
                "skipped",
                f"[dry-run] implement: {len(tasks)} task(s) -> {agent_name}",
            )

        self._record_busy_lease(issue, state)
        lbl.acquire(self.gh, issue, self.cfg.labels, agent_name, self.agent_names)
        wt = self._ensure_worktree_git(issue.number, state.branch_name)

        resume_ctx = ""
        if resume:
            resume_ctx = self._resume_context(issue.number)

        prompt = (
            f"You are an excellent software engineer. "
            f'You are working on GitHub Issue #{issue.number} "{issue.title}".\n'
            f"This task has an approved spec. You MUST implement it according to the spec.\n"
            f"{self._spec_block(issue.number, issue.body)}"
            f"On the current working branch, implement and test all of the following "
            f"unfinished tasks autonomously, in order from top to bottom:\n\n"
            f"{task_list}\n\n"
            f"Make one commit per task. "
            f"When you are done, leave the repository in a state where the spec's verify "
            f"command passes.\n"
            f"{resume_ctx}"
            f"{q.question_prompt_hint(self.cfg.question_file)}"
        )

        question_holder: dict[str, str | None] = {"content": None}

        def on_question() -> bool:
            content = q.check_question_file(wt.cwd, self.cfg.question_file)
            if content:
                question_holder["content"] = content
                return True
            return False

        verify = self._resolve_verify_steps(issue, state, issue.body)
        if isinstance(verify, StepResult):
            return verify

        result = execute_with_self_healing(
            self.cfg,
            agent,
            wt,
            verify,
            prompt,
            on_question,
        )

        state.iteration += 1
        state.total_agent_runs += result.attempts
        state.last_agent = agent_name

        # Interrupted by a clarification request.
        if result.reason == "blocked" and question_holder["content"]:
            wt.savepoint(f"WIP: awaiting clarification (issue #{issue.number})")
            state.next_action = "wait_for_clarification"
            state.pending_questions = [question_holder["content"]]
            self.gh.comment(
                issue.number,
                f"🤖 **Clarification from {agent_name}**\n\n{question_holder['content']}\n\n"
                f"Pausing this task until an answer is provided (`status: blocked`).",
            )
            self._enter_blocked(issue, state, "clarification", question_holder["content"][:500])
            return StepResult(issue.number, "blocked", "awaiting clarification")

        if result.reason == "transient":
            return self._handle_transient(issue, state, result, wt, agent_name)

        # Implementation failed -> leave the work as WIP and escalate to a human.
        if not result.ok:
            committed = wt.savepoint(f"WIP: implementation failed (issue #{issue.number})")
            left = (
                f"The partial changes are left on branch `{state.branch_name}` as a WIP commit."
                if committed
                else "No changes were produced, so there is no commit."
            )
            self.gh.comment(
                issue.number,
                f"⚠️ **Task processing failed** ({agent_name}, reason={result.reason})\n\n"
                f"Automatic processing of {len(tasks)} unfinished task(s) failed. "
                f"Human intervention is needed.\n\n"
                f"{left}\n\n"
                f"```\n{result.output[-1500:]}\n```",
            )
            self._enter_blocked(issue, state, "implement_failed", result.reason)
            return StepResult(issue.number, "failed", result.reason)

        # Success -> mark all of the tasks passed in this batch as [x]
        # (verify passing implies the spec's acceptance criteria are met).
        wt.savepoint(f"issue #{issue.number}: implemented {len(tasks)} task(s)")
        wt.push(state.branch_name)
        # Refresh the body before replacing it (optimistic locking).
        fresh = self.gh.get_issue(issue.number)
        fresh_stripped = st.strip_state(fresh.body)
        fresh_stripped = st.check_tasks(fresh_stripped, st.unchecked(fresh_stripped))

        done, total = st.progress(fresh_stripped)
        remaining = st.next_unchecked(fresh_stripped) is not None
        state.transient_retries = 0
        state.phase = "implementing" if remaining else "implemented"
        state.next_action = (
            "implement" if remaining else ("simplify" if self.cfg.simplify_enabled else "ai_review")
        )

        new_body = st.write_state(fresh_stripped, state)
        self.gh.set_body(issue.number, new_body)
        self._release_idle(issue, state)
        self.gh.comment(
            issue.number,
            f"✅ **{agent_name}**: completed {len(tasks)} task(s). ({done}/{total})\n\n"
            + "\n".join(f"- {t.text}" for t in tasks),
        )
        return StepResult(issue.number, "implemented", f"{len(tasks)} done ({done}/{total})")

    # -- simplify phase ----------------------------------------------------
    def _simplify(self, issue: Issue, state: st.IssueState) -> StepResult:
        agent = self.cfg.agent_for("simplify")
        agent_name = agent.name

        if not st.has_verify_meta(issue.body):
            if self.dry_run:
                return StepResult(issue.number, "skipped", "[dry-run] cannot start: spec missing")
            return self._block_for_missing_spec(issue, state)

        if self.dry_run:
            return StepResult(issue.number, "skipped", f"[dry-run] simplify -> {agent_name}")

        self._record_busy_lease(issue, state)
        lbl.acquire(self.gh, issue, self.cfg.labels, agent_name, self.agent_names)
        wt = self._ensure_worktree_git(issue.number, state.branch_name)

        prompt = (
            f'The implementation of GitHub Issue #{issue.number} "{issue.title}" is '
            f"complete but may still be untidy.\n"
            f"{self._spec_block(issue.number, issue.body)}"
            f"On the current working branch, tidy the code without changing behavior:\n"
            f"- Reuse existing patterns and deduplicate where possible.\n"
            f"- Remove redundancy and simplify over/under-abstraction (wrong altitude).\n"
            f"- Improve efficiency where it does not alter behavior.\n"
            f"Do not change public behavior, output, or API. Do not hunt for bugs "
            f"(that is the review phase's job). Leave the repository in a state where "
            f"verify still passes.\n"
            f"{q.question_prompt_hint(self.cfg.question_file)}"
        )

        def on_question() -> bool:
            return q.check_question_file(wt.cwd, self.cfg.question_file) is not None

        verify = self._resolve_verify_steps(issue, state, issue.body)
        if isinstance(verify, StepResult):
            return verify

        result = execute_with_self_healing(self.cfg, agent, wt, verify, prompt, on_question)
        state.iteration += 1
        state.total_agent_runs += result.attempts
        state.last_agent = agent_name

        if result.reason == "transient":
            return self._handle_transient(issue, state, result, wt, agent_name)

        if not result.ok:
            wt.savepoint(f"WIP: simplify failed (issue #{issue.number})")
            self.gh.comment(
                issue.number,
                f"⚠️ **Simplify failed** ({agent_name}, reason={result.reason}). "
                f"Human intervention is needed.\n\n"
                f"The partial changes are left on branch `{state.branch_name}` as a WIP commit.\n\n"
                f"```\n{result.output[-1500:]}\n```",
            )
            self._enter_blocked(issue, state, "simplify_failed", result.reason)
            return StepResult(issue.number, "failed", result.reason)

        wt.savepoint(f"issue #{issue.number}: simplified")
        state.transient_retries = 0
        state.phase = "simplified"
        state.next_action = "ai_review"
        self._persist(issue, state)
        self._release_idle(issue, state)
        self.gh.comment(
            issue.number,
            f"✨ **{agent_name}**: code tidy-up complete. Proceeding to review.",
        )
        return self._review(self.gh.get_issue(issue.number), state)

    # -- review phase ------------------------------------------------------
    def _review(self, issue: Issue, state: st.IssueState) -> StepResult:
        agent = self.cfg.agent_for("review")
        agent_name = agent.name

        if not st.has_verify_meta(issue.body):
            if self.dry_run:
                return StepResult(issue.number, "skipped", "[dry-run] cannot start: spec missing")
            return self._block_for_missing_spec(issue, state)

        if self.dry_run:
            return StepResult(issue.number, "skipped", f"[dry-run] ai_review -> {agent_name}")

        self._record_busy_lease(issue, state)
        lbl.acquire(self.gh, issue, self.cfg.labels, agent_name, self.agent_names)
        wt = self._ensure_worktree_git(issue.number, state.branch_name)

        prompt = (
            f'The implementation of GitHub Issue #{issue.number} "{issue.title}" is '
            f"broadly complete.\n"
            f"{self._spec_block(issue.number, issue.body)}"
            f"Review from these perspectives:\n"
            f"- Spec consistency and correctness\n"
            f"- Logic and control flow\n"
            f"- Edge cases, error handling, and test adequacy\n"
            f"- Fit with existing conventions in this repository\n"
        )
        if self.cfg.simplify_enabled:
            prompt += (
                "Style/structure cleanups are already handled by the simplify phase, so review "
                "should not redo them and should focus on the above.\n"
            )
        prompt += (
            f"Run the tests against the changes on the current working branch. "
            f"Fix any problems you find and leave all tests passing.\n"
            f"{q.question_prompt_hint(self.cfg.question_file)}"
        )

        def on_question() -> bool:
            return q.check_question_file(wt.cwd, self.cfg.question_file) is not None

        verify = self._resolve_verify_steps(issue, state, issue.body)
        if isinstance(verify, StepResult):
            return verify

        result = execute_with_self_healing(self.cfg, agent, wt, verify, prompt, on_question)
        state.iteration += 1
        state.total_agent_runs += result.attempts
        state.last_agent = agent_name

        if result.reason == "transient":
            return self._handle_transient(issue, state, result, wt, agent_name)

        if not result.ok:
            wt.savepoint(f"WIP: review failed (issue #{issue.number})")
            self.gh.comment(
                issue.number,
                f"⚠️ **Review failed** ({agent_name}, reason={result.reason}). "
                f"Human intervention is needed.\n\n"
                f"The partial changes are left on branch `{state.branch_name}` as a WIP commit.\n\n"
                f"```\n{result.output[-1500:]}\n```",
            )
            self._enter_blocked(issue, state, "review_failed", result.reason)
            return StepResult(issue.number, "failed", result.reason)

        wt.savepoint(f"issue #{issue.number}: review fixes")
        state.transient_retries = 0
        state.phase = "reviewed"
        state.next_action = "create_pr"
        self._persist(issue, state)
        self._release_idle(issue, state)
        self.gh.comment(
            issue.number,
            f"🔍 **{agent_name}**: review and tests complete. Creating the PR.",
        )
        return self._create_pr(self.gh.get_issue(issue.number), state)

    # -- PR creation phase -------------------------------------------------
    def _create_pr(self, issue: Issue, state: st.IssueState) -> StepResult:
        if self.dry_run:
            return StepResult(issue.number, "skipped", "[dry-run] create_pr")

        wt = self._ensure_worktree_git(issue.number, state.branch_name)
        wt.savepoint(f"issue #{issue.number}: final")
        wt.push(state.branch_name)

        pr_body = f'Refs #{issue.number}\n\nAutomated implementation for Issue "{issue.title}".\n'

        # Reuse an existing PR for the branch (for example, one a human opened manually).
        existing = self.gh.pr_for_branch(state.branch_name)
        if existing:
            pr_url = existing["url"]
            pr_number = existing["number"]
            if existing.get("isDraft"):
                self.gh.mark_pr_ready(pr_number)
            self.gh.set_pr_body(pr_number, pr_body)
            action, verb = "pr_updated", "updated the existing PR with the implementation"
            log.info("Issue #%s: reusing existing PR #%s", issue.number, pr_number)
        else:
            try:
                pr_url = self.gh.create_pr(
                    head=state.branch_name,
                    base=self.base_branch,
                    title=f"{issue.title} (#{issue.number})",
                    body=pr_body,
                )
            except Exception as e:
                log.error("failed to create PR: %s", e)
                self.gh.comment(issue.number, f"⚠️ Failed to create the PR: {e}")
                self._enter_blocked(issue, state, "pr_create_failed", str(e))
                return StepResult(issue.number, "failed", "pr_create_failed")
            pr_number = self.gh.pr_number_for_branch(state.branch_name) or 0
            action, verb = "pr_created", "created the PR"

        state.phase = "pr_open"
        state.next_action = "wait_ci"
        state.pr_url = pr_url
        state.pr_number = pr_number or 0
        self._persist(issue, state)
        # Release the lock but do not mark completed (still waiting on CI/approval).
        self._release_idle(issue, state)
        gate = "CI success + review approval" if self.cfg.require_approval else "CI success"
        self.gh.comment(
            issue.number,
            f"🚀 {verb}: {pr_url}\n"
            f"Once {gate} is met, it will be {self.cfg.merge_method}-merged automatically.",
        )
        return StepResult(issue.number, action, pr_url)

    # -- wait for CI/approval -> auto merge --------------------------------
    def _wait_ci(self, issue: Issue, state: st.IssueState) -> StepResult:
        if not state.pr_number:
            num = self.gh.pr_number_for_branch(state.branch_name)
            if not num:
                return StepResult(issue.number, "skipped", "no matching PR found")
            state.pr_number = num

        status = self.gh.pr_status(state.pr_number)

        if self.dry_run:
            return StepResult(
                issue.number,
                "skipped",
                f"[dry-run] wait_ci: checks={status.checks} "
                f"review={status.review_decision or '-'} mergeable={status.mergeable}",
            )

        if status.state == "MERGED":
            return self._enter_verify_merge(issue, state)

        if status.checks == "failure":
            if self.cfg.auto_fix_ci:
                return self._fix_ci(issue, state, status)
            self.gh.comment(
                issue.number,
                f"⚠️ **CI failed** ({status.url}). Human intervention is needed.",
            )
            self._enter_blocked(issue, state, "ci_failed", status.url)
            return StepResult(issue.number, "failed", "ci_failed")

        if self.cfg.auto_resolve_conflicts and status.mergeable == "CONFLICTING":
            return self._resolve_conflict(issue, state)

        # Merging on CI success alone would miss feedback that humans / review bots
        # left on the PR. Pick up and address unaddressed review comments before the
        # merge decision.
        if self.cfg.address_pr_reviews:
            pending = self._pending_review_items(state)
            if pending:
                return self._address_review(issue, state, pending)

        if not status.ready_to_merge(self.cfg.require_approval):
            detail = (
                f"checks={status.checks} review={status.review_decision or '-'} "
                f"mergeable={status.mergeable}"
            )
            log.info("Issue #%s: merge conditions not met (%s)", issue.number, detail)
            return StepResult(issue.number, "skipped", f"waiting on CI/approval ({detail})")

        # Conditions met -> squash merge.
        self.gh.merge_pr(
            state.pr_number,
            method=self.cfg.merge_method,
            delete_branch=self.cfg.delete_branch_on_merge,
        )
        return self._enter_verify_merge(issue, state)

    # -- addressing PR review comments -------------------------------------
    def _pending_review_items(self, state: st.IssueState) -> list[ReviewItem]:
        """Filter unaddressed PR review comments (human / bot) using a high-water mark.

        - Exclude ghswarm's own "addressed" comments (prevents an infinite loop).
        - Treat only comments after last_review_addressed_at as unaddressed.
        - Approval-only or chit-chat-only comments are not actionable (_is_actionable).
        """
        if not state.pr_number:
            return []
        items = self.gh.pr_review_items(state.pr_number)
        mark = _parse_ts(state.last_review_addressed_at)
        pending: list[ReviewItem] = []
        for it in items:
            if REVIEW_RESPONSE_MARKER in it.body:
                continue
            if not _is_actionable(it):
                continue
            ts = _parse_ts(it.created_at)
            if mark and ts and ts <= mark:
                continue
            pending.append(it)
        return pending

    def _address_review(
        self, issue: Issue, state: st.IssueState, pending: list[ReviewItem]
    ) -> StepResult:
        agent = self.cfg.agent_for("review")
        agent_name = agent.name

        if self.dry_run:
            return StepResult(
                issue.number,
                "skipped",
                f"[dry-run] address_review: {len(pending)} item(s) -> {agent_name}",
            )

        self._record_busy_lease(issue, state)
        lbl.acquire(self.gh, issue, self.cfg.labels, agent_name, self.agent_names)
        wt = self._ensure_worktree_git(issue.number, state.branch_name)

        review_block = _format_review_items(pending)
        prompt = (
            f"The PR (#{state.pr_number}) for GitHub Issue #{issue.number} "
            f'"{issue.title}" has received review comments. Address the feedback below.\n'
            f"This includes not only human reviewers but also review bots "
            f"(CodeRabbit / Copilot, etc.).\n"
            f"{self._spec_block(issue.number, issue.body)}"
            f"--- review feedback ---\n{review_block}\n--- /review feedback ---\n\n"
            f"On the current working branch, fix the valid points and leave all tests "
            f"passing.\n"
            f"For any point you decide not to act on, briefly summarize why.\n"
            f"{q.question_prompt_hint(self.cfg.question_file)}"
        )

        def on_question() -> bool:
            return q.check_question_file(wt.cwd, self.cfg.question_file) is not None

        verify = self._resolve_verify_steps(issue, state, issue.body)
        if isinstance(verify, StepResult):
            return verify

        result = execute_with_self_healing(self.cfg, agent, wt, verify, prompt, on_question)
        state.iteration += 1
        state.total_agent_runs += result.attempts
        state.last_agent = agent_name

        if result.reason == "transient":
            return self._handle_transient(issue, state, result, wt, agent_name)

        if not result.ok:
            wt.savepoint(f"WIP: review response failed (issue #{issue.number})")
            self.gh.comment(
                issue.number,
                f"⚠️ **PR review response failed** ({agent_name}, reason={result.reason}). "
                f"Human intervention is needed.\n\n"
                f"The partial changes are left on branch `{state.branch_name}` as a WIP commit.\n\n"
                f"```\n{result.output[-1500:]}\n```",
            )
            self._enter_blocked(issue, state, "review_failed", result.reason)
            return StepResult(issue.number, "failed", result.reason)

        wt.savepoint(f"issue #{issue.number}: addressed review feedback")
        wt.push(state.branch_name)

        # Advance the high-water mark to the newest comment time in this batch. Only
        # comments after this will count as unaddressed on subsequent runs.
        latest = max((it.created_at for it in pending if it.created_at), default="")
        if latest:
            state.last_review_addressed_at = latest
            # Mark the addressed review threads as resolved so they don't linger as
            # "unresolved" conversations on the PR (best-effort; a failure here must
            # not block the flow that already pushed the fix).
            if self.cfg.resolve_review_threads:
                try:
                    n = self.gh.resolve_review_threads(state.pr_number, latest)
                    if n:
                        log.info("Issue #%s: resolved %s review thread(s)", issue.number, n)
                except (GitHubError, ValueError) as e:
                    log.warning("Issue #%s: resolving review threads failed: %s", issue.number, e)
        state.transient_retries = 0
        state.phase = "review_addressed"
        state.next_action = "wait_ci"
        self._persist(issue, state)
        self._release_idle(issue, state)

        summary = f"🔧 **{agent_name}**: addressed {len(pending)} PR review comment(s) and pushed."
        self.gh.comment(issue.number, summary)
        # Also leave a response on the PR (with the marker so ghswarm can identify its own comment).
        self.gh.pr_comment(state.pr_number, f"{REVIEW_RESPONSE_MARKER}\n{summary}")
        return StepResult(issue.number, "review_addressed", f"{len(pending)} addressed")

    def _resolve_conflict(self, issue: Issue, state: st.IssueState) -> StepResult:
        agent = self.cfg.agent_for("implement")
        agent_name = agent.name

        if self.dry_run:
            return StepResult(
                issue.number,
                "skipped",
                f"[dry-run] resolve_conflict: retries={state.conflict_retries}",
            )

        self._record_busy_lease(issue, state)
        lbl.acquire(self.gh, issue, self.cfg.labels, agent_name, self.agent_names)
        wt = self._ensure_worktree_git(issue.number, state.branch_name)

        prompt_header = (
            f"The PR (#{state.pr_number}) for GitHub Issue #{issue.number} "
            f'"{issue.title}" has merge conflicts with `{self.base_branch}`.'
            f"{self._spec_block(issue.number, issue.body)}"
        )
        verify = self._resolve_verify_steps(issue, state, issue.body)
        if isinstance(verify, StepResult):
            return verify

        result = resolve_conflict_with_agent(
            self.cfg,
            agent,
            wt,
            verify,
            self.base_branch,
            state.branch_name,
            prompt_header,
        )
        state.total_agent_runs += result.attempts

        if not result.ok:
            state.conflict_retries += 1
            if state.conflict_retries > self.cfg.conflict_max_retries:
                self.gh.comment(
                    issue.number,
                    f"⚠️ **Automatic merge-conflict resolution failed** "
                    f"({agent_name}, reason={result.reason}, "
                    f"{state.conflict_retries}/{self.cfg.conflict_max_retries})\n\n"
                    f"A manual merge/resolution is needed.\n\n"
                    f"```\n{result.output[-1500:]}\n```",
                )
                self._enter_blocked(issue, state, "conflict", result.reason)
                return StepResult(issue.number, "blocked", result.reason)

            log.info(
                "Issue #%s: conflict resolution failed (retry %d/%d, reason=%s). "
                "Retrying on the next poll.",
                issue.number,
                state.conflict_retries,
                self.cfg.conflict_max_retries,
                result.reason,
            )
            self._persist(issue, state)
            self._release_idle(issue, state)
            return StepResult(issue.number, "retry_pending", result.reason)

        if not result.clean_merge:
            if not wt.finalize_merge_commit():
                state.conflict_retries += 1
                wt.abort_merge()
                if state.conflict_retries > self.cfg.conflict_max_retries:
                    self.gh.comment(
                        issue.number,
                        "⚠️ **Failed to finalize the merge commit**. A manual merge/resolution is needed.",
                    )
                    self._enter_blocked(issue, state, "conflict", "commit_failed")
                    return StepResult(issue.number, "blocked", "commit_failed")
                self._persist(issue, state)
                self._release_idle(issue, state)
                return StepResult(issue.number, "retry_pending", "commit_failed")

        if not wt.try_push(state.branch_name):
            state.conflict_retries += 1
            if state.conflict_retries > self.cfg.conflict_max_retries:
                self.gh.comment(
                    issue.number,
                    f"⚠️ **Push after conflict resolution failed** "
                    f"({state.conflict_retries}/{self.cfg.conflict_max_retries}). "
                    f"A manual merge/resolution is needed.",
                )
                self._enter_blocked(issue, state, "conflict", "push_failed")
                return StepResult(issue.number, "blocked", "push_failed")

            log.info(
                "Issue #%s: push failed (retry %d/%d). Retrying on the next poll.",
                issue.number,
                state.conflict_retries,
                self.cfg.conflict_max_retries,
            )
            self._persist(issue, state)
            self._release_idle(issue, state)
            return StepResult(issue.number, "retry_pending", "push_failed")

        state.conflict_retries = 0
        state.transient_retries = 0
        state.last_agent = agent_name
        state.phase = "conflict_resolved"
        state.next_action = "wait_ci"
        self._persist(issue, state)
        self._release_idle(issue, state)
        summary = (
            f"🔀 **{agent_name}**: resolved the merge conflict with `{self.base_branch}` "
            f"and pushed. Re-running CI."
        )
        self.gh.comment(issue.number, summary)
        return StepResult(issue.number, "conflict_resolved", state.branch_name)

    def _fix_ci(self, issue: Issue, state: st.IssueState, status: PRStatus) -> StepResult:
        agent = self.cfg.agent_for("implement")
        agent_name = agent.name

        if self.dry_run:
            return StepResult(
                issue.number,
                "skipped",
                f"[dry-run] fix_ci: retries={state.ci_fix_retries}",
            )

        ci_logs = self.gh.failed_gha_ci_logs(state.pr_number)
        if ci_logs is None:
            self.gh.comment(
                issue.number,
                f"⚠️ **CI failed** ({status.url}). "
                f"This is a non-GHA CI, so automatic fixing is out of scope. "
                f"Human intervention is needed.",
            )
            self._enter_blocked(issue, state, "ci_failed", status.url)
            return StepResult(issue.number, "failed", "ci_failed")

        self._record_busy_lease(issue, state)
        lbl.acquire(self.gh, issue, self.cfg.labels, agent_name, self.agent_names)
        wt = self._ensure_worktree_git(issue.number, state.branch_name)

        prompt_header = (
            f"CI (GitHub Actions) failed on the PR (#{state.pr_number}) for GitHub "
            f'Issue #{issue.number} "{issue.title}".'
            f"{self._spec_block(issue.number, issue.body)}"
        )
        verify = self._resolve_verify_steps(issue, state, issue.body)
        if isinstance(verify, StepResult):
            return verify

        result = fix_ci_with_agent(
            self.cfg,
            agent,
            wt,
            verify,
            state.branch_name,
            prompt_header,
            ci_logs,
        )
        state.total_agent_runs += result.attempts

        if result.reason == "transient":
            return self._handle_transient(issue, state, result, wt, agent_name)

        if not result.ok:
            state.ci_fix_retries += 1
            if state.ci_fix_retries > self.cfg.ci_fix_max_retries:
                self.gh.comment(
                    issue.number,
                    f"⚠️ **Automatic CI-failure fix failed** "
                    f"({agent_name}, reason={result.reason}, "
                    f"{state.ci_fix_retries}/{self.cfg.ci_fix_max_retries})\n\n"
                    f"A manual fix is needed.\n\n"
                    f"```\n{result.output[-1500:]}\n```",
                )
                self._enter_blocked(issue, state, "ci_failed", result.reason)
                return StepResult(issue.number, "blocked", result.reason)

            log.info(
                "Issue #%s: CI fix failed (retry %d/%d, reason=%s). Retrying on the next poll.",
                issue.number,
                state.ci_fix_retries,
                self.cfg.ci_fix_max_retries,
                result.reason,
            )
            self._persist(issue, state)
            self._release_idle(issue, state)
            return StepResult(issue.number, "retry_pending", result.reason)

        if not wt.savepoint(f"issue #{issue.number}: fix CI failure"):
            state.ci_fix_retries += 1
            if state.ci_fix_retries > self.cfg.ci_fix_max_retries:
                self.gh.comment(
                    issue.number,
                    f"⚠️ **Automatic CI-failure fix failed** "
                    f"({agent_name}, no changes, "
                    f"{state.ci_fix_retries}/{self.cfg.ci_fix_max_retries})\n\n"
                    f"A manual fix is needed.",
                )
                self._enter_blocked(issue, state, "ci_failed", "no_changes")
                return StepResult(issue.number, "blocked", "no_changes")

            log.info(
                "Issue #%s: CI fix produced no changes (retry %d/%d). Retrying on the next poll.",
                issue.number,
                state.ci_fix_retries,
                self.cfg.ci_fix_max_retries,
            )
            self._persist(issue, state)
            self._release_idle(issue, state)
            return StepResult(issue.number, "retry_pending", "no_changes")

        if not wt.try_push(state.branch_name):
            state.ci_fix_retries += 1
            if state.ci_fix_retries > self.cfg.ci_fix_max_retries:
                self.gh.comment(
                    issue.number,
                    f"⚠️ **Push after the CI fix failed** "
                    f"({state.ci_fix_retries}/{self.cfg.ci_fix_max_retries}). "
                    f"A manual fix is needed.",
                )
                self._enter_blocked(issue, state, "ci_failed", "push_failed")
                return StepResult(issue.number, "blocked", "push_failed")

            log.info(
                "Issue #%s: push failed (retry %d/%d). Retrying on the next poll.",
                issue.number,
                state.ci_fix_retries,
                self.cfg.ci_fix_max_retries,
            )
            self._persist(issue, state)
            self._release_idle(issue, state)
            return StepResult(issue.number, "retry_pending", "push_failed")

        state.ci_fix_retries = 0
        state.transient_retries = 0
        state.last_agent = agent_name
        state.phase = "ci_fixed"
        state.next_action = "wait_ci"
        self._persist(issue, state)
        self._release_idle(issue, state)
        summary = f"🔧 **{agent_name}**: fixed the CI failure and pushed. Re-running CI."
        self.gh.comment(issue.number, summary)
        return StepResult(issue.number, "ci_fixed", state.branch_name)

    def _enter_verify_merge(self, issue: Issue, state: st.IssueState) -> StepResult:
        """Transition right after a merge completes. Branches on the post-merge CI gate.

        With `post_merge_ci: false`, the Issue is closed right after the merge as before.
        When enabled, the Issue is not closed; it transitions to the `verify_merge`
        phase and the merge commit's CI is evaluated on subsequent loops.
        """
        # Record the merge commit's sha (used to look up CI on the target branch).
        state.merge_commit_sha = self.gh.merge_commit_sha(state.pr_number)
        state.merged_at = datetime.now(timezone.utc).isoformat()

        if not self.cfg.post_merge_ci:
            return self._finalize_merged(issue, state)

        state.phase = "merged"
        state.next_action = "verify_merge"
        self._persist(issue, state)
        self._release_idle(issue, state)
        self.gh.comment(
            issue.number,
            f"🔀 Merged PR #{state.pr_number}. "
            f"Will close after confirming CI on the target branch (`{self.base_branch}`).",
        )
        return StepResult(issue.number, "merged", state.pr_url)

    # -- post-merge CI gate ------------------------------------------------
    def _verify_merge(self, issue: Issue, state: st.IssueState) -> StepResult:
        sha = state.merge_commit_sha or self.gh.merge_commit_sha(state.pr_number)

        if self.dry_run:
            return StepResult(issue.number, "skipped", f"[dry-run] verify_merge: sha={sha or '-'}")

        if not sha:
            return StepResult(issue.number, "skipped", "cannot obtain the merge commit sha")

        checks = self.gh.commit_checks(sha)

        if checks == "success":
            return self._finalize_merged(issue, state)

        if checks == "failure":
            self.gh.comment(
                issue.number,
                f"⚠️ **Post-merge CI failed** (target `{self.base_branch}`, "
                f"commit `{sha[:7]}`). This may be a regression. The Issue stays open. "
                f"Human intervention is needed.",
            )
            self._enter_blocked(issue, state, "merge_ci_failed", sha[:7])
            return StepResult(issue.number, "failed", "post_merge_ci_failed")

        if checks == "none":
            # Right after a merge, no workflow may be registered yet and there can be
            # zero checks. Within the grace period, treat it as pending and wait; past
            # that, assume "no CI" and close.
            if self._within_grace(state):
                return StepResult(
                    issue.number, "skipped", "waiting for post-merge CI (checks=none, within grace)"
                )
            log.info(
                "Issue #%s: post-merge CI still has zero checks past the grace period. "
                "Assuming no CI.",
                issue.number,
            )
            return self._finalize_merged(issue, state)

        # pending
        return StepResult(issue.number, "skipped", "waiting for post-merge CI (checks=pending)")

    def _within_grace(self, state: st.IssueState) -> bool:
        """Whether time since merge is within the grace seconds. If merged_at is unset, treat as within grace."""
        if not state.merged_at:
            return True
        try:
            merged = datetime.fromisoformat(state.merged_at)
        except ValueError:
            return False
        elapsed = (datetime.now(timezone.utc) - merged).total_seconds()
        return elapsed < self.cfg.post_merge_ci_grace

    def _finalize_merged(self, issue: Issue, state: st.IssueState) -> StepResult:
        state.phase = "completed"
        state.next_action = "done"
        state.last_notified_reason = None
        self._persist(issue, state)
        self.git.remove_worktree(self._worktree_path(issue.number), state.branch_name)
        lbl.release(self.gh, issue, self.cfg.labels, self.cfg.labels.completed, self.agent_names)
        self.gh.close_issue(issue.number)
        self.gh.comment(issue.number, f"✅ Merged PR #{state.pr_number}. Done.")
        notifier = self._notifier_for()
        if notifier.enabled and self.cfg.notify.on_completed:
            repo_name = getattr(self, "repo_name", "unknown/unknown")
            notifier.notify_completed(issue, state.pr_number, repo_name)
        return StepResult(issue.number, "merged", state.pr_url)

    # -- helpers -----------------------------------------------------------
    def _handle_transient(
        self,
        issue: Issue,
        state: st.IssueState,
        result,
        wt: Git,
        agent_name: str,
    ) -> StepResult:
        """On a transient error, return to idle, or escalate to blocked once the cap is hit."""
        wt.savepoint(f"WIP: transient error (issue #{issue.number})")
        state.transient_retries += 1

        if state.transient_retries > self.cfg.transient_max_retries:
            self.gh.comment(
                issue.number,
                f"⚠️ **Transient-error retry cap reached** ({agent_name}, "
                f"{state.transient_retries}/{self.cfg.transient_max_retries})\n\n"
                f"The same phase was retried automatically, but transient errors persist. "
                f"Human intervention is needed.\n\n"
                f"```\n{result.output[-1500:]}\n```",
            )
            self._enter_blocked(issue, state, "transient_exhausted", result.reason)
            return StepResult(issue.number, "blocked", "transient_max_retries")

        log.info(
            "Issue #%s: transient error (retry %d/%d). Retrying on the next poll.",
            issue.number,
            state.transient_retries,
            self.cfg.transient_max_retries,
        )
        self._persist(issue, state)
        self._release_idle(issue, state)
        return StepResult(issue.number, "retry_pending", result.reason)

    def _spec_block(self, issue_number: int, body: str) -> str:
        text = st.prose(body).strip()
        if not text:
            return ""
        return f"\n--- spec (issue #{issue_number}) ---\n{text[:4000]}\n--- /spec ---\n"

    def _block_for_missing_spec(self, issue: Issue, state: st.IssueState) -> StepResult:
        """Cannot start due to a missing GHSWARM_VERIFY block."""
        comment = (
            "🚫 **Cannot start**: no spec is set\n\n"
            "The Issue body has no `GHSWARM_VERIFY` block. "
            "File a spec with ghswarm-spec (or append the block manually), then resume."
        )
        self.gh.comment(issue.number, comment)
        self._enter_blocked(issue, state, "spec_missing", "")
        return StepResult(issue.number, "blocked", "spec_missing")

    def _verify_steps_for(self, body: str) -> list[ResolvedStep]:
        """Resolve the Issue body's verify metadata into steps with cwd/runner attached.

        Each step's execution environment comes from config's VerifyConfig.sandbox_for,
        matched by the step's path (None for a legacy, path-less step). If verify is
        absent or empty, this returns an empty list (= skip verification).

        Raises ConfigError when the GHSWARM_VERIFY block is malformed or verify
        normalization fails.
        """
        meta = st.parse_verify_meta(body)
        steps = Spec(meta=meta).verify_steps
        return [
            ResolvedStep(
                path=step.path,
                command=step.command,
                runner=make_runner(self.cfg.verify.sandbox_for(step.path)),
            )
            for step in steps
        ]

    def _resolve_verify_steps(
        self, issue: Issue, state: st.IssueState, body: str
    ) -> list[ResolvedStep] | StepResult:
        """Resolve verify steps from the Issue body, or block with verify_invalid."""
        try:
            return self._verify_steps_for(body)
        except ConfigError as exc:
            return self._block_for_invalid_verify(issue, state, exc)

    def _block_for_invalid_verify(
        self, issue: Issue, state: st.IssueState, exc: ConfigError
    ) -> StepResult:
        """Malformed GHSWARM_VERIFY metadata. Comments, transitions to blocked, and notifies."""
        comment = (
            "🚫 **Cannot start**: invalid verify configuration\n\n"
            f"The Issue's `GHSWARM_VERIFY` block could not be parsed:\n\n"
            f"```\n{exc}\n```\n\n"
            f"Fix the `GHSWARM_VERIFY` block in the Issue body, then resume."
        )
        self.gh.comment(issue.number, comment)
        self._enter_blocked(issue, state, "verify_invalid", str(exc))
        return StepResult(issue.number, "blocked", "verify_invalid")

    def _persist(self, issue: Issue, state: st.IssueState) -> None:
        fresh = self.gh.get_issue(issue.number)
        new_body = st.write_state(st.strip_state(fresh.body), state)
        self.gh.set_body(issue.number, new_body)

    def _notifier_for(self) -> Notifier:
        notifier = getattr(self, "_notifier", None)
        if notifier is None:
            notifier = Notifier.from_config(self.cfg.notify)
            self._notifier = notifier
        return notifier

    def _enter_blocked(
        self, issue: Issue, state: st.IssueState, reason_code: str, detail: str = ""
    ) -> None:
        """Commit to blocked, applying the label and sending a notification (with dedup) in one step."""
        state.phase = "blocked"
        self._persist(issue, state)
        lbl.release(self.gh, issue, self.cfg.labels, self.cfg.labels.blocked, self.agent_names)
        if state.last_notified_reason == reason_code:
            return
        notifier = self._notifier_for()
        if not notifier.enabled:
            return
        repo_name = getattr(self, "repo_name", "unknown/unknown")
        notifier.notify_blocked(issue, reason_code, detail, repo_name)
        state.last_notified_reason = reason_code
        self._persist(issue, state)

    def _release_idle(self, issue: Issue, state: st.IssueState) -> None:
        """Return to idle. When leaving blocked, also clear last_notified_reason."""
        state.last_notified_reason = None
        lbl.release(self.gh, issue, self.cfg.labels, self.cfg.labels.idle, self.agent_names)

    def _record_busy_lease(self, issue: Issue, state: st.IssueState) -> None:
        """Write the lease into the body before acquiring the busy label (record -> label order)."""
        state.busy_owner = lbl.make_owner()
        state.busy_since = datetime.now(timezone.utc).isoformat()
        self._persist(issue, state)

    def _resume_context(self, number: int) -> str:
        comment = self.gh.latest_comment(number)
        if not comment:
            return ""
        return (
            "\n[Answer to the clarification (latest comment)]\n"
            f"{comment.get('body', '')[:2000]}\n"
            "Proceed with the implementation according to this answer.\n"
        )


def _parse_ts(s: str) -> datetime | None:
    """Parse an ISO8601 string into a datetime. Also accepts GitHub's 'Z' suffix."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_actionable(item: ReviewItem) -> bool:
    """Whether this review-derived comment is feedback worth considering.

    Approval-only reviews are excluded. Everything else (change requests / overall
    reviews / inline feedback / conversation comments) with a body is in scope.
    Whether to actually act on it is the agent's call.
    """
    if item.kind == "review":
        if item.state == "APPROVED":
            return False
        return item.state == "CHANGES_REQUESTED" or bool(item.body)
    return bool(item.body)


def _format_review_items(items: list[ReviewItem]) -> str:
    lines: list[str] = []
    for it in items:
        who = it.author or "unknown"
        if it.kind == "inline":
            loc = it.path + (f":{it.line}" if it.line else "")
            lines.append(f"- [inline {loc}] @{who}: {it.body}")
        elif it.kind == "review":
            tag = it.state or "REVIEW"
            lines.append(f"- [review {tag}] @{who}: {it.body}")
        else:
            lines.append(f"- [comment] @{who}: {it.body}")
    return "\n".join(lines)
