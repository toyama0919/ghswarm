"""Tests for ensure_worktree / remove_worktree / rebase_onto_base (git is not launched).

In the spec PR approach the spec only exists on the working branch side, so if a branch
exists on origin but we re-cut from base, the spec is lost. In addition to guarding against
that regression, this verifies the branches introduced by the worktree migration in issue-15
(reuse / cleanup / local / origin / base) and the cleanup of a leaked worktree branch
(the phenomenon where `worktree add` with `-b` fails but the branch is left behind).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghswarm.git_ops import Git, GitError


class FakeGit(Git):
    """Stub that overrides `run` to declaratively supply worktree list / rev-parse results."""

    def __init__(
        self,
        *,
        worktree_list: str = "",
        local_branch: bool = False,
        remote_branch: bool = False,
        add_should_fail: bool = False,
    ):
        super().__init__(cwd="/tmp/repo")
        self.worktree_list = worktree_list
        self.local_branch = local_branch
        self.remote_branch = remote_branch
        self.add_should_fail = add_should_fail
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, check: bool = False) -> tuple[int, str]:
        self.calls.append(args)
        if args[:3] == ("worktree", "list", "--porcelain"):
            return 0, self.worktree_list
        if args[:2] == ("rev-parse", "--verify"):
            ref = args[-1]
            if ref.startswith("refs/heads/"):
                return (0 if self.local_branch else 1), ""
            if ref.startswith("refs/remotes/"):
                return (0 if self.remote_branch else 1), ""
        if args[:2] == ("worktree", "add") and self.add_should_fail:
            return 1, "fatal: failure"
        return 0, ""


def _resolved(path: str) -> str:
    return str(Path(path).resolve())


# -- ensure_worktree: reuse --------------------------------------------------


def test_reuses_worktree_already_on_target_branch():
    target = "/tmp/wt/issue-7"
    porcelain = f"worktree {target}\nHEAD abc123\nbranch refs/heads/issue-7\n"
    git = FakeGit(worktree_list=porcelain, local_branch=True, remote_branch=True)

    result = git.ensure_worktree("issue-7", "main", target)

    assert result == _resolved(target)
    assert ("-C", _resolved(target), "pull", "--ff-only", "origin", "issue-7") in git.calls
    assert not any(c[:2] == ("worktree", "add") for c in git.calls)
    assert not any(c[:2] == ("worktree", "remove") for c in git.calls)


# -- ensure_worktree: clean up then recreate ---------------------------------


def test_removes_registered_worktree_on_a_different_branch_before_readding():
    target = "/tmp/wt/issue-7"
    porcelain = f"worktree {target}\nHEAD abc123\nbranch refs/heads/old-branch\n"
    git = FakeGit(worktree_list=porcelain, local_branch=True)

    result = git.ensure_worktree("issue-7", "main", target)

    remove_idx = git.calls.index(("worktree", "remove", "--force", _resolved(target)))
    prune_idx = git.calls.index(("worktree", "prune"))
    add_idx = git.calls.index(("worktree", "add", _resolved(target), "issue-7"))
    assert remove_idx < prune_idx < add_idx  # order: remove -> prune -> add
    assert result == _resolved(target)


# -- ensure_worktree: a local branch already exists --------------------------


def test_adds_worktree_from_existing_local_branch_without_dash_b():
    target = "/tmp/wt/issue-7"
    git = FakeGit(local_branch=True, remote_branch=True)

    result = git.ensure_worktree("issue-7", "main", target)

    assert ("worktree", "add", _resolved(target), "issue-7") in git.calls
    assert not any(c[:3] == ("worktree", "add", "-b") for c in git.calls)
    assert result == _resolved(target)


# -- ensure_worktree: restore from origin ------------------------------------


def test_adds_worktree_tracking_origin_branch_when_only_remote_exists():
    target = "/tmp/wt/issue-9"
    git = FakeGit(local_branch=False, remote_branch=True)

    result = git.ensure_worktree("issue-9", "main", target)

    assert ("fetch", "origin", "issue-9") in git.calls
    assert (
        "worktree",
        "add",
        _resolved(target),
        "--track",
        "-b",
        "issue-9",
        "origin/issue-9",
    ) in git.calls
    assert result == _resolved(target)


# -- ensure_worktree: create fresh from base ---------------------------------


def test_adds_worktree_from_base_when_branch_is_unknown():
    target = "/tmp/wt/issue-11"
    git = FakeGit(local_branch=False, remote_branch=False)

    result = git.ensure_worktree("issue-11", "main", target)

    assert ("fetch", "origin", "main") in git.calls
    assert (
        "worktree",
        "add",
        _resolved(target),
        "-b",
        "issue-11",
        "--no-track",
        "origin/main",
    ) in git.calls
    assert result == _resolved(target)


# -- ensure_worktree: branch-leak handling when worktree add fails -----------


def test_worktree_add_failure_deletes_the_orphan_branch_and_raises():
    git = FakeGit(local_branch=False, remote_branch=False, add_should_fail=True)

    with pytest.raises(GitError):
        git.ensure_worktree("issue-13", "main", "/tmp/wt/issue-13")

    assert ("branch", "-D", "issue-13") in git.calls


def test_worktree_add_failure_from_existing_local_branch_does_not_delete_it():
    # On the path without -b (reusing an existing local branch), that branch was not
    # created by this call (it may contain past WIP commits, etc.), so even on failure
    # it must not be deleted with branch -D.
    git = FakeGit(local_branch=True, remote_branch=True, add_should_fail=True)

    with pytest.raises(GitError):
        git.ensure_worktree("issue-7", "main", "/tmp/wt/issue-7")

    assert ("branch", "-D", "issue-7") not in git.calls


# -- remove_worktree ----------------------------------------------------------


def test_remove_worktree_removes_prunes_and_deletes_local_branch():
    git = FakeGit()

    git.remove_worktree("/tmp/wt/issue-7", "issue-7")

    assert ("worktree", "remove", "--force", "/tmp/wt/issue-7") in git.calls
    assert ("worktree", "prune") in git.calls
    assert ("branch", "-D", "issue-7") in git.calls


def test_remove_worktree_does_not_raise_on_failure(monkeypatch):
    git = FakeGit()

    def failing_run(self, *args, check=False):
        return 1, "failure"

    monkeypatch.setattr(FakeGit, "run", failing_run)
    git.remove_worktree("/tmp/wt/issue-7", "issue-7")  # does not raise


# -- rebase_onto_base: does not use checkout ---------------------------------


def test_rebase_onto_base_does_not_checkout():
    git = FakeGit()

    ok = git.rebase_onto_base("issue-7", "main")

    assert ok is True
    assert ("fetch", "origin", "main") in git.calls
    assert ("rebase", "origin/main") in git.calls
    assert not any(c and c[0] == "checkout" for c in git.calls)


# -- merge / push helpers -----------------------------------------------------


class MergeGit(FakeGit):
    """run stub for merge_base / sync / try_push / abort_merge."""

    def __init__(
        self,
        *,
        merge_code: int = 0,
        conflict_files: str = "",
        sync_code: int = 0,
        push_code: int = 0,
        commit_code: int = 0,
    ):
        super().__init__()
        self.merge_code = merge_code
        self.conflict_files = conflict_files
        self.sync_code = sync_code
        self.push_code = push_code
        self.commit_code = commit_code

    def run(self, *args: str, check: bool = False) -> tuple[int, str]:
        self.calls.append(args)
        if args[:2] == ("merge", "--no-edit"):
            return self.merge_code, "merge output"
        if args[:3] == ("diff", "--name-only", "--diff-filter=U"):
            return 0, self.conflict_files
        if args[:2] == ("reset", "--hard"):
            return self.sync_code, ""
        if args[:2] == ("push", "-u"):
            return self.push_code, "push failed" if self.push_code else ""
        if args[:2] == ("commit", "--no-edit"):
            return self.commit_code, ""
        if args[:2] == ("merge", "--abort"):
            return 0, ""
        return super().run(*args, check=check)


def test_merge_base_returns_clean_on_success():
    git = MergeGit(merge_code=0)

    result = git.merge_base("main")

    assert result == "clean"
    assert ("fetch", "origin", "main") in git.calls
    assert ("merge", "--no-edit", "origin/main") in git.calls


def test_merge_base_returns_conflict_file_list():
    git = MergeGit(merge_code=1, conflict_files="src/a.py\nsrc/b.py\n")

    result = git.merge_base("main")

    assert result == ["src/a.py", "src/b.py"]


def test_abort_merge_runs_abort():
    git = MergeGit()

    git.abort_merge()

    assert ("merge", "--abort") in git.calls


def test_try_push_returns_true_on_success():
    git = MergeGit(push_code=0)

    assert git.try_push("issue-7") is True


def test_try_push_returns_false_without_raising():
    git = MergeGit(push_code=1)

    assert git.try_push("issue-7") is False


def test_sync_branch_to_remote_fetches_and_resets():
    git = MergeGit(sync_code=0)

    assert git.sync_branch_to_remote("issue-7") is True
    assert ("fetch", "origin", "issue-7") in git.calls
    assert ("reset", "--hard", "origin/issue-7") in git.calls


def test_finalize_merge_commit_stages_and_commits():
    git = MergeGit(commit_code=0)

    assert git.finalize_merge_commit() is True
    assert ("add", "-A") in git.calls
    assert ("commit", "--no-edit") in git.calls
