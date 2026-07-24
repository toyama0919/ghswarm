---
name: ghswarm-release
description: Release ghswarm to PyPI. Bumps the version in pyproject.toml, commits it to main, and pushes a matching git tag (vX.Y.Z) that triggers the "Publish to PyPI" GitHub Actions workflow (Trusted Publisher / OIDC, no token). The tag is always derived from pyproject.toml so the two never drift. Use for cutting a release, releasing, publishing to PyPI, bumping the version and tagging.
---

# ghswarm-release (cut a PyPI release)

Bump version → commit → push tag → GitHub Actions publishes to PyPI.

**The git tag is always derived from `pyproject.toml`'s `version` as `v<version>`**, so the
tag and the packaged version never drift. `pyproject.toml` is the single source of truth.

## Arguments

- (none) or `patch` → bump the patch part (`0.1.0` → `0.1.1`)
- `minor` → `0.1.0` → `0.2.0`
- `major` → `0.1.0` → `1.0.0`
- an explicit `X.Y.Z` → set that exact version

## Prerequisites

- `gh` authenticated with the **personal** account. The remote is `toyama0919`, so export
  `GH_CONFIG_DIR="$HOME/.config/gh-toyama0919"` for every `gh` command in this skill.
- Publishing is via PyPI Trusted Publisher (OIDC); no API token is needed. It relies on the
  pending/trusted publisher registered for `ghswarm` and the GitHub `pypi` Environment.

## Steps

0. **Guard the working state.** All must hold, else stop and report:
   - On `main`: `git branch --show-current` = `main`.
   - Clean tree: `git status --porcelain` is empty.
   - Up to date: `git fetch origin && git rev-parse @` = `git rev-parse @{u}`.

1. **Read the current version** from `pyproject.toml` (`^version = "X.Y.Z"`).

2. **Compute the new version** from the argument (patch/minor/major/explicit).

3. **Guard against collisions** — stop if any is true:
   - Tag exists locally: `git tag -l "v<new>"` non-empty.
   - Tag exists on origin: `git ls-remote --tags origin "v<new>"` non-empty.
   - Already on PyPI: `curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/ghswarm/<new>/json` is `200`.

4. **Edit** the `version = "..."` line in `pyproject.toml` to `<new>`.

5. **Sanity gate.** Run `uv sync --extra dev` if needed, then `uv run pytest -q` and
   `uv run ruff check .`. If either fails, revert the edit (`git checkout pyproject.toml`) and stop.

6. **Commit to main and push.** `git add pyproject.toml && git commit -m "Release v<new>"`
   (English, per AGENTS.md), then `git push origin main`.
   Release version bumps commit directly to `main` — this is the intentional exception to the
   "no direct work on main" rule.

7. **Tag and push the tag.** `git tag v<new> && git push origin v<new>`. This push is what
   triggers the publish workflow.

8. **Watch the publish.** Find the run (`gh run list --workflow=publish.yml --limit 1`) and
   `gh run watch <id> --exit-status`. On success, report the PyPI URL
   `https://pypi.org/project/ghswarm/<new>/`. On failure, surface the failing step's log.

## Notes

- If publish fails with `File already exists`, that version is already on PyPI — bump again and re-run.
- The workflow builds from the tagged commit, so the version bump commit (step 6) must land on
  `main` **before** the tag is pushed (step 7). Keep that order.
- Confirm the new version with the user before running when the bump level is ambiguous.
