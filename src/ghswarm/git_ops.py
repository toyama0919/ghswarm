"""Local Git operations and self-healing (rollback / rebase).

All operations run against the specified working directory (a local clone of the repository).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Literal

from .logging_utils import get_logger

log = get_logger("ghswarm.git")


class GitError(Exception):
    pass


def detect_repo_root(cwd: str) -> str:
    """Detect the root of the repository that `cwd` belongs to.

    `Path.cwd()` is not necessarily the root, since ghswarm may be launched from a
    subdirectory. This is used as the base for resolving relative worktree_dir paths
    and for the cwd of the `Git` instance for the main repository.
    """
    res = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    out = res.stdout.strip()
    if res.returncode != 0 or not out:
        raise GitError(f"Failed to detect repository root:\n{res.stderr.strip()}")
    return out


class Git:
    def __init__(self, cwd: str):
        self.cwd = cwd

    def run(self, *args: str, check: bool = False) -> tuple[int, str]:
        res = subprocess.run(
            ["git", *args],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        out = (res.stdout + res.stderr).strip()
        log.debug("git %s -> %s", " ".join(args), res.returncode)
        if check and res.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed:\n{out}")
        return res.returncode, out

    def current_branch(self) -> str:
        _, out = self.run("rev-parse", "--abbrev-ref", "HEAD")
        return out.strip()

    def has_changes(self) -> bool:
        _, out = self.run("status", "--porcelain")
        return bool(out.strip())

    def _worktree_entries(self) -> list[tuple[str, str | None]]:
        """Parse `git worktree list --porcelain` into [(path, branch_ref_or_None), ...]."""
        _, out = self.run("worktree", "list", "--porcelain")
        entries: list[tuple[str, str | None]] = []
        path: str | None = None
        branch: str | None = None
        for line in [*out.splitlines(), ""]:
            if line.startswith("worktree "):
                path = line[len("worktree ") :].strip()
            elif line.startswith("branch "):
                branch = line[len("branch ") :].strip()
            elif line == "":
                if path is not None:
                    entries.append((path, branch))
                path, branch = None, None
        return entries

    def ensure_worktree(self, branch: str, base: str, path: str) -> str:
        """Prepare the worktree for an issue and return its absolute path.

        Priority order: reuse a registered worktree > clean up an obstructing path >
        add a worktree from a local branch > restore from a remote-tracking branch >
        create anew from base. Do not reorder these steps: doing so can cause a branch
        leak (only the branch left behind when `-b` fails).
        """
        target = str(Path(path).resolve())
        branch_ref = f"refs/heads/{branch}"

        match: tuple[str, str | None] | None = None
        for wt_path, wt_branch in self._worktree_entries():
            if str(Path(wt_path).resolve()) == target:
                match = (wt_path, wt_branch)
                break

        if match is not None and match[1] == branch_ref:
            log.info("Reusing worktree: %s (%s)", target, branch)
            code, out = self.run("-C", target, "pull", "--ff-only", "origin", branch)
            if code != 0:
                log.warning("worktree pull --ff-only failed (continuing): %s", out)
            return target

        if match is not None:
            code, out = self.run("worktree", "remove", "--force", target)
            if code != 0:
                log.warning("git worktree remove --force failed: %s", out)
            self.run("worktree", "prune")
        elif Path(target).exists():
            shutil.rmtree(target, ignore_errors=True)

        code, _ = self.run("rev-parse", "--verify", "--quiet", branch_ref)
        if code == 0:
            # Since we omit -b, this branch was not created by the current call (it may
            # contain WIP commits from past runs, etc.). Even on failure, it must not be
            # deleted with `branch -D` (see the spec).
            code, out = self.run("worktree", "add", target, branch)
            if code != 0:
                raise GitError(f"git worktree add {target} {branch} failed:\n{out}")
            log.info("worktree: %s (%s, existing local branch)", target, branch)
            return target

        self.run("fetch", "origin", branch)
        code, _ = self.run("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
        if code == 0:
            code, out = self.run(
                "worktree", "add", target, "--track", "-b", branch, f"origin/{branch}"
            )
            if code != 0:
                self.run("branch", "-D", branch)
                raise GitError(f"git worktree add {target} (from origin/{branch}) failed:\n{out}")
            log.info("worktree: %s (%s, restored from origin)", target, branch)
            return target

        self.run("fetch", "origin", base)
        code, out = self.run(
            "worktree", "add", target, "-b", branch, "--no-track", f"origin/{base}"
        )
        if code != 0:
            self.run("branch", "-D", branch)
            raise GitError(f"git worktree add {target} (new from {base}) failed:\n{out}")
        log.info("worktree: %s (%s, newly created from %s)", target, branch, base)
        return target

    def remove_worktree(self, path: str, branch: str) -> None:
        """Discard the worktree and local branch after a merge completes. Failures only warn."""
        code, out = self.run("worktree", "remove", "--force", path)
        if code != 0:
            log.warning("git worktree remove --force failed: %s", out)
        code, out = self.run("worktree", "prune")
        if code != 0:
            log.warning("git worktree prune failed: %s", out)
        code, out = self.run("branch", "-D", branch)
        if code != 0:
            log.warning("Failed to delete local branch %s: %s", branch, out)

    def savepoint(self, message: str) -> bool:
        """Stash uncommitted changes as a WIP commit. Returns True if there were changes."""
        if not self.has_changes():
            return False
        self.run("add", "-A", check=True)
        code, out = self.run("commit", "-m", message)
        if code != 0:
            log.warning("savepoint commit failed: %s", out)
            return False
        log.info("savepoint: %s", message)
        return True

    def rollback(self) -> None:
        """Discard all uncommitted changes and reset to the latest commit (self-healing)."""
        log.warning("Rolling back the working copy")
        self.run("reset", "--hard", "HEAD")
        self.run("clean", "-fd")

    def rebase_onto_base(self, branch: str, base: str) -> bool:
        """Incorporate base. On conflict, abort and reset to base's state.

        In a worktree the same branch may be checked out concurrently by another
        worktree (the human's), so checkout may not be possible; therefore we only do
        fetch + rebase origin/<base>.
        """
        self.run("fetch", "origin", base)
        code, out = self.run("rebase", f"origin/{base}")
        if code != 0:
            log.error("Rebase conflict. Aborting and resetting to base.\n%s", out)
            self.run("rebase", "--abort")
            self.run("reset", "--hard", f"origin/{base}")
            return False
        return True

    def push(self, branch: str) -> None:
        self.run("push", "-u", "origin", branch, check=True)

    def sync_branch_to_remote(self, branch: str) -> bool:
        """After fetching, align the local branch with origin/<branch>."""
        self.run("fetch", "origin", branch)
        code, out = self.run("reset", "--hard", f"origin/{branch}")
        if code != 0:
            log.warning("Could not sync branch %s to origin: %s", branch, out)
            return False
        return True

    def merge_base(self, base: str) -> Literal["clean"] | list[str]:
        """Merge origin/<base>; return "clean" on success, or the list of conflicting files."""
        self.run("fetch", "origin", base)
        code, out = self.run("merge", "--no-edit", f"origin/{base}")
        if code == 0:
            return "clean"
        _, files_out = self.run("diff", "--name-only", "--diff-filter=U")
        files = [line.strip() for line in files_out.splitlines() if line.strip()]
        if not files:
            log.warning("Merge failed but no conflicting files could be detected: %s", out)
        return files

    def abort_merge(self) -> None:
        """Abort the in-progress merge and restore the working tree to its pre-merge state."""
        self.run("merge", "--abort")

    def try_push(self, branch: str) -> bool:
        """Return whether the push succeeded (does not raise GitError)."""
        code, out = self.run("push", "-u", "origin", branch)
        if code != 0:
            log.warning("push failed: %s", out)
            return False
        return True

    def finalize_merge_commit(self) -> bool:
        """Finalize the merge commit after the agent resolves conflicts."""
        self.run("add", "-A", check=True)
        code, out = self.run("commit", "--no-edit")
        if code != 0:
            log.warning("Failed to finalize merge commit: %s", out)
            return False
        return True
