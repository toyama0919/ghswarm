---
name: ghswarm-release
description: Release ghswarm to PyPI. Bumps the version in pyproject.toml, commits it to main, and pushes a matching git tag (vX.Y.Z) that triggers the "Publish to PyPI" GitHub Actions workflow (Trusted Publisher / OIDC, no token). The tag is always derived from pyproject.toml so the two never drift. Use for cutting a release, releasing, publishing to PyPI, bumping the version and tagging.
---

# ghswarm-release (cut a PyPI release)

Bump version → commit → push tag → GitHub Actions publishes to PyPI.

The whole flow is one script — [`release.sh`](release.sh). It is deterministic, so run it
rather than doing the steps by hand:

```bash
.claude/skills/ghswarm-release/release.sh [patch|minor|major|X.Y.Z]
```

- (none) or `patch` → bump the patch part (`0.1.0` → `0.1.1`)
- `minor` → `0.2.0`
- `major` → `1.0.0`
- an explicit `X.Y.Z` → set that exact version

**The git tag is always derived from `pyproject.toml`'s `version` as `v<version>`**, so the
tag and the packaged version never drift. `pyproject.toml` is the single source of truth.

## What the script does

1. Guards the working state: on `main`, clean tree, up to date with origin.
2. Reads the current version and computes the new one from the argument.
3. Stops on collision: tag already exists (local/origin) or version already on PyPI.
4. Edits the `version` line in `pyproject.toml` and refreshes `uv.lock` (`uv lock`).
5. Sanity gate: `uv run pytest -q` and `uv run ruff check .`; reverts and stops on failure.
6. Commits both `pyproject.toml` and `uv.lock` to `main` as `Release v<new>`, tags `v<new>`
   locally, then pushes `main`.
7. Pushes the tag — this is what triggers the publish workflow.
8. Polls PyPI (~5 min) until `<new>` appears, then reports `https://pypi.org/project/ghswarm/<new>/`.

## Notes

- Releasing commits directly to `main` — the intentional exception to the "no direct work on
  main" rule. The script enforces the commit-before-tag order the publish workflow needs.
- Publishing is via PyPI Trusted Publisher (OIDC); no API token or `gh` is needed. It relies on
  the pending/trusted publisher registered for `ghswarm` and the GitHub `pypi` Environment.
- Confirm the new version with the user before running when the bump level is ambiguous.
- If publish fails with `File already exists`, that version is already on PyPI — bump again.
- Exit codes: `0` = published and confirmed; `2` = committed and tagged but PyPI not confirmed
  within the poll window (the publish is likely still running — check Actions, do **not** re-bump);
  `1` = a guard/gate failed before anything was pushed.
- If `main` is pushed but the tag push fails, the tag already exists locally — recover with
  `git push origin v<new>`; do not re-run the script (it would bump to the next version).
