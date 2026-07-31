"""A thin wrapper that invokes the `gh` CLI via subprocess.

Using gh instead of PyGithub lets us delegate authentication (gh auth) to gh.
Provides Issue fetching, label operations, body updates, comments, and PR creation.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .logging_utils import get_logger

log = get_logger("ghswarm.github")

_GHA_RUN_URL_RE = re.compile(r"/actions/runs/(\d+)")
_FAILED_GHA_LOG_MAX = 20000


class GitHubError(Exception):
    pass


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    state: str = "open"
    url: str = ""

    @property
    def status_labels(self) -> list[str]:
        return [l for l in self.labels if l.startswith("status")]


# Classification of CI check conclusions
_PASS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_FAIL = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}


@dataclass
class PRStatus:
    number: int
    state: str  # OPEN / MERGED / CLOSED
    mergeable: str  # MERGEABLE / CONFLICTING / UNKNOWN
    review_decision: str  # APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED / ""
    checks: str  # success / failure / pending / none
    url: str = ""

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "PRStatus":
        return cls(
            number=d.get("number", 0),
            state=d.get("state", ""),
            mergeable=d.get("mergeable", "UNKNOWN"),
            review_decision=d.get("reviewDecision") or "",
            checks=cls._rollup(d.get("statusCheckRollup") or []),
            url=d.get("url", ""),
        )

    @staticmethod
    def _rollup(rollup: list[dict[str, Any]]) -> str:
        if not rollup:
            return "none"
        any_pending = False
        for c in rollup:
            if "conclusion" in c or "status" in c:  # CheckRun
                # gh pr view (GraphQL) returns "COMPLETED" while the REST API returns
                # "completed", so compare case-insensitively (same class of bug as #4).
                if (c.get("status") or "").upper() != "COMPLETED":
                    any_pending = True
                    continue
                concl = (c.get("conclusion") or "").upper()
                if concl in _FAIL:
                    return "failure"
                if concl not in _PASS:
                    any_pending = True
            else:  # StatusContext
                st = (c.get("state") or "").upper()
                if st in _FAIL:
                    return "failure"
                if st != "SUCCESS":
                    any_pending = True
        return "pending" if any_pending else "success"

    def ready_to_merge(self, require_approval: bool) -> bool:
        if self.state != "OPEN":
            return False
        if self.mergeable == "CONFLICTING":
            return False
        if self.checks not in ("success", "none"):
            return False
        if require_approval and self.review_decision != "APPROVED":
            return False
        return True


@dataclass
class ReviewItem:
    """A single review-originated comment on a PR (includes both humans and review bots).

    kind:
      review  … the PR review itself (state + summary body). Overall feedback from bots like CodeRabbit.
      inline  … an inline review comment tied to a file/line.
      comment … a general comment in the PR conversation tab (bot summary posts often land here too).
    """

    author: str
    kind: str
    body: str
    created_at: str = ""  # ISO8601 (submitted_at for reviews, created_at otherwise)
    state: str = ""  # review only: APPROVED / CHANGES_REQUESTED / COMMENTED
    path: str = ""  # inline only
    line: int | None = None  # inline only


def _subprocess_env(env: dict[str, str] | None) -> dict[str, str] | None:
    if not env:
        return None
    return {**os.environ, **env}


def _run_gh(
    args: list[str],
    cwd: str | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    cmd = ["gh", *args]
    log.debug("gh %s", " ".join(args))
    try:
        res = subprocess.run(
            cmd,
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            env=_subprocess_env(env),
        )
    except FileNotFoundError as e:  # gh not installed
        raise GitHubError("`gh` command not found. Please install the GitHub CLI.") from e
    if res.returncode != 0:
        raise GitHubError(
            f"gh {' '.join(args)} failed (exit {res.returncode}):\n{res.stderr.strip()}"
        )
    return res.stdout


def _run_gh_ignore_exit(
    args: list[str], cwd: str | None = None, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run gh and return stdout/stderr regardless of exit code (used for pr checks)."""
    cmd = ["gh", *args]
    log.debug("gh %s", " ".join(args))
    try:
        res = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=_subprocess_env(env),
        )
    except FileNotFoundError as e:
        raise GitHubError("`gh` command not found. Please install the GitHub CLI.") from e
    return res.returncode, res.stdout, res.stderr


def _extract_gha_run_id(link: str) -> str | None:
    m = _GHA_RUN_URL_RE.search(link or "")
    return m.group(1) if m else None


def _gha_run_ids_from_failed_checks(checks: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for check in checks:
        if check.get("bucket") != "fail":
            continue
        run_id = _extract_gha_run_id(check.get("link") or "")
        if run_id and run_id not in seen:
            seen.add(run_id)
            ids.append(run_id)
    return ids


def detect_current_repo(cwd: str | None = None) -> str:
    """Detect owner/repo from the current (or cwd) git repository."""
    out = _run_gh(
        ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd=cwd
    ).strip()
    if not out:
        raise GitHubError("Could not identify a GitHub repository from the current directory.")
    return out


def detect_default_branch(cwd: str | None = None, env: dict[str, str] | None = None) -> str:
    """Detect the default branch name of the current (or cwd) repository."""
    return _run_gh(
        ["repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        cwd=cwd,
        env=env,
    ).strip()


class GitHub:
    def __init__(self, repo: str, env: dict[str, str] | None = None):
        self.repo = repo
        self.env = env

    # -- Issue fetching ---------------------------------------------------
    def get_issue(self, number: int) -> Issue:
        out = _run_gh(
            [
                "issue",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "number,title,body,labels,state,url",
            ],
            env=self.env,
        )
        return self._parse_issue(json.loads(out))

    def list_open_issues(
        self,
        labels: list[str] | None = None,
        assignee: str = "",
        milestone: str = "",
        limit: int = 100,
    ) -> list[Issue]:
        args = [
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,body,labels,state,url",
        ]
        if labels:
            for lb in labels:
                args += ["--label", str(lb)]
        if assignee:
            args += ["--assignee", str(assignee)]
        if milestone:
            args += ["--milestone", str(milestone)]
        out = _run_gh(args, env=self.env)
        return [self._parse_issue(i) for i in json.loads(out)]

    @staticmethod
    def _parse_issue(data: dict[str, Any]) -> Issue:
        return Issue(
            number=data["number"],
            title=data.get("title", ""),
            body=data.get("body") or "",
            labels=[l["name"] for l in data.get("labels", [])],
            # gh returns "OPEN"/"CLOSED", so normalize to lowercase
            state=(data.get("state") or "open").lower(),
            url=data.get("url", ""),
        )

    def latest_comment(self, number: int) -> dict[str, Any] | None:
        out = _run_gh(
            ["issue", "view", str(number), "--repo", self.repo, "--json", "comments"],
            env=self.env,
        )
        comments = json.loads(out).get("comments", [])
        return comments[-1] if comments else None

    # -- Mutations --------------------------------------------------------
    def set_body(self, number: int, body: str) -> None:
        # Pass the body via stdin (to safely handle newlines and quoting)
        _run_gh(
            ["issue", "edit", str(number), "--repo", self.repo, "--body-file", "-"],
            input_text=body,
            env=self.env,
        )

    def add_label(self, number: int, label: str) -> None:
        self.ensure_label(label)
        _run_gh(
            ["issue", "edit", str(number), "--repo", self.repo, "--add-label", label],
            env=self.env,
        )

    def remove_label(self, number: int, label: str) -> None:
        try:
            _run_gh(
                ["issue", "edit", str(number), "--repo", self.repo, "--remove-label", label],
                env=self.env,
            )
        except GitHubError as e:
            # Removing a label that isn't attached is tolerated
            log.debug("Skipping label removal (%s): %s", label, e)

    def comment(self, number: int, body: str) -> None:
        _run_gh(
            ["issue", "comment", str(number), "--repo", self.repo, "--body-file", "-"],
            input_text=body,
            env=self.env,
        )

    def close_issue(self, number: int) -> None:
        _run_gh(["issue", "close", str(number), "--repo", self.repo], env=self.env)

    _known_labels: set[str] | None = None

    def ensure_label(self, label: str) -> None:
        """Create the label if it doesn't exist (auto-provisioning of lock labels)."""
        if self._known_labels is None:
            out = _run_gh(
                ["label", "list", "--repo", self.repo, "--limit", "200", "--json", "name"],
                env=self.env,
            )
            self._known_labels = {l["name"] for l in json.loads(out)}
        if label in self._known_labels:
            return
        try:
            _run_gh(
                ["label", "create", label, "--repo", self.repo, "--force"],
                env=self.env,
            )
            self._known_labels.add(label)
            log.info("Created label: %s", label)
        except GitHubError as e:
            log.debug("Failed to create label (may already exist): %s", e)
            self._known_labels.add(label)

    # -- PR ---------------------------------------------------------------
    def create_pr(self, head: str, base: str, title: str, body: str) -> str:
        out = _run_gh(
            [
                "pr",
                "create",
                "--repo",
                self.repo,
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            input_text=body,
            env=self.env,
        )
        return out.strip()

    def pr_number_for_branch(self, head: str) -> int | None:
        pr = self.pr_for_branch(head)
        return pr["number"] if pr else None

    def pr_for_branch(self, head: str) -> dict[str, Any] | None:
        """Return the open PR corresponding to the branch, if one exists."""
        out = _run_gh(
            [
                "pr",
                "list",
                "--repo",
                self.repo,
                "--head",
                head,
                "--state",
                "open",
                "--json",
                "number,url,isDraft",
            ],
            env=self.env,
        )
        data = json.loads(out)
        return data[0] if data else None

    def mark_pr_ready(self, number: int) -> None:
        """Mark a draft PR as ready for review."""
        _run_gh(["pr", "ready", str(number), "--repo", self.repo], env=self.env)

    def set_pr_body(self, number: int, body: str) -> None:
        _run_gh(
            ["pr", "edit", str(number), "--repo", self.repo, "--body-file", "-"],
            input_text=body,
            env=self.env,
        )

    def pr_status(self, number: int) -> "PRStatus":
        out = _run_gh(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "number,state,mergeable,reviewDecision,statusCheckRollup,url",
            ],
            env=self.env,
        )
        return PRStatus.from_json(json.loads(out))

    def failed_gha_ci_logs(self, number: int) -> str | None:
        """Return logs from failed GHA workflow runs.

        Returns:
          None … no GHA run among the failed checks (e.g. non-GHA CI)
          ""   … a GHA run exists but log retrieval failed, or the log was empty
          str  … the combined logs of failed steps (tail excerpt if too long)
        """
        _, stdout, _ = _run_gh_ignore_exit(
            [
                "pr",
                "checks",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "name,state,bucket,link",
            ],
            env=self.env,
        )
        if not stdout.strip():
            return None
        try:
            checks = json.loads(stdout)
        except json.JSONDecodeError:
            log.warning("Failed to parse pr checks JSON (PR #%s)", number)
            return None
        if not isinstance(checks, list):
            return None

        run_ids = _gha_run_ids_from_failed_checks(checks)
        if not run_ids:
            return None

        parts: list[str] = []
        for run_id in run_ids:
            code, log_out, log_err = _run_gh_ignore_exit(
                ["run", "view", run_id, "--repo", self.repo, "--log-failed"],
                env=self.env,
            )
            if code != 0 and not (log_out or "").strip():
                log.warning(
                    "Failed to fetch logs via gh run view %s (exit %s): %s",
                    run_id,
                    code,
                    log_err,
                )
                continue
            chunk = (log_out or "").rstrip()
            if chunk:
                parts.append(f"--- run {run_id} ---\n{chunk}\n")

        if not parts:
            return ""

        combined = "\n".join(parts)
        if len(combined) > _FAILED_GHA_LOG_MAX:
            combined = combined[-_FAILED_GHA_LOG_MAX:]
        return combined

    def pr_comment(self, number: int, body: str) -> None:
        """Post a comment on the PR conversation tab (the PR side, distinct from Issue comments)."""
        _run_gh(
            ["pr", "comment", str(number), "--repo", self.repo, "--body-file", "-"],
            input_text=body,
            env=self.env,
        )

    def pr_review_items(self, number: int) -> list["ReviewItem"]:
        """Fetch review-originated comments on a PR, from humans and bots alike.

        Hits three sources (reviews / inline comments / conversation comments) and
        returns them sorted by created_at ascending. The API defaults to 30 items, so
        we fetch with per_page=100 (sufficient for real-world PRs).
        """
        items: list[ReviewItem] = []

        reviews = self._api_json(f"repos/{self.repo}/pulls/{number}/reviews?per_page=100")
        for r in reviews if isinstance(reviews, list) else []:
            body = (r.get("body") or "").strip()
            state = (r.get("state") or "").upper()
            # Skip reviews with no body and no state change (e.g. a bare approval click)
            if not body and state not in ("CHANGES_REQUESTED",):
                continue
            items.append(
                ReviewItem(
                    author=(r.get("user") or {}).get("login", ""),
                    kind="review",
                    body=body,
                    created_at=r.get("submitted_at") or "",
                    state=state,
                )
            )

        comments = self._api_json(f"repos/{self.repo}/pulls/{number}/comments?per_page=100")
        for c in comments if isinstance(comments, list) else []:
            items.append(
                ReviewItem(
                    author=(c.get("user") or {}).get("login", ""),
                    kind="inline",
                    body=(c.get("body") or "").strip(),
                    created_at=c.get("created_at") or "",
                    path=c.get("path") or "",
                    line=c.get("line") if c.get("line") is not None else c.get("original_line"),
                )
            )

        convo = self._api_json(f"repos/{self.repo}/issues/{number}/comments?per_page=100")
        for c in convo if isinstance(convo, list) else []:
            items.append(
                ReviewItem(
                    author=(c.get("user") or {}).get("login", ""),
                    kind="comment",
                    body=(c.get("body") or "").strip(),
                    created_at=c.get("created_at") or "",
                )
            )

        items.sort(key=lambda it: it.created_at)
        return items

    def _api_json(self, path: str) -> Any:
        return json.loads(_run_gh(["api", path], env=self.env))

    def resolve_review_threads(self, number: int, up_to_ts: str = "") -> int:
        """Resolve unresolved review threads on a PR that have already been addressed.

        A thread is resolved only when it is currently unresolved and its newest
        comment was created at or before ``up_to_ts`` (the high-water mark of the
        review feedback ghswarm just addressed). Threads with newer activity — e.g. a
        human reply after the batch — are left open so an ongoing discussion is not
        silently closed. ISO8601-UTC timestamps compare correctly as strings.

        Returns the number of threads resolved. Does nothing when ``up_to_ts`` is
        empty (nothing has been addressed yet).
        """
        if not up_to_ts:
            return 0
        owner, _, name = self.repo.partition("/")
        if not owner or not name:
            return 0

        # first:100 mirrors pr_review_items' per_page=100; threads beyond 100 are
        # left untouched (acceptable for real-world PRs).
        query = (
            "query($owner:String!,$name:String!,$number:Int!){"
            "repository(owner:$owner,name:$name){pullRequest(number:$number){"
            "reviewThreads(first:100){nodes{id isResolved "
            "comments(last:1){nodes{createdAt}}}}}}}"
        )
        out = _run_gh(
            [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={number}",
            ],
            env=self.env,
        )
        # GraphQL returns repository/pullRequest as null (not absent) with exit 0 when
        # they are missing or inaccessible, so guard every level with `or {}`.
        data = json.loads(out).get("data") or {}
        repo = data.get("repository") or {}
        pr = repo.get("pullRequest") or {}
        threads = (pr.get("reviewThreads") or {}).get("nodes") or []

        resolved = 0
        for thr in threads:
            if not isinstance(thr, dict) or thr.get("isResolved"):
                continue
            comments = ((thr.get("comments") or {}).get("nodes")) or []
            newest = comments[-1].get("createdAt") if comments else ""
            if not newest or newest > up_to_ts:
                continue
            thread_id = thr.get("id")
            if not thread_id:
                continue
            self._resolve_thread(thread_id)
            resolved += 1
        return resolved

    def _resolve_thread(self, thread_id: str) -> None:
        mutation = (
            "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}"
        )
        _run_gh(
            ["api", "graphql", "-f", f"query={mutation}", "-f", f"id={thread_id}"],
            env=self.env,
        )

    def merge_commit_sha(self, pr_number: int) -> str:
        """Return the sha of a merged PR's merge commit (empty string if not merged)."""
        out = _run_gh(
            ["pr", "view", str(pr_number), "--repo", self.repo, "--json", "mergeCommit"],
            env=self.env,
        )
        mc = json.loads(out).get("mergeCommit") or {}
        return mc.get("oid", "")

    def commit_checks(self, sha: str) -> str:
        """Collapse the CI on the merge commit (sha) into success/failure/pending/none.

        Fetches both check-runs (GitHub Actions, etc.) and legacy commit statuses, and
        classifies them with the same logic as PRStatus._rollup. Since it queries by the
        merge commit's sha, the same path works regardless of which branch was merged into.
        """
        runs = json.loads(
            _run_gh(["api", f"repos/{self.repo}/commits/{sha}/check-runs"], env=self.env)
        ).get("check_runs", [])
        statuses = json.loads(
            _run_gh(["api", f"repos/{self.repo}/commits/{sha}/status"], env=self.env)
        ).get("statuses", [])
        return PRStatus._rollup([*runs, *statuses])

    def merge_pr(self, number: int, method: str = "squash", delete_branch: bool = True) -> None:
        args = ["pr", "merge", str(number), "--repo", self.repo, f"--{method}"]
        if delete_branch:
            args.append("--delete-branch")
        _run_gh(args, env=self.env)
