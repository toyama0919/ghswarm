# prpilot

[![CI](https://github.com/toyama0919/prpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/toyama0919/prpilot/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/prpilot.svg)](https://pypi.org/project/prpilot/)
[![Python versions](https://img.shields.io/pypi/pyversions/prpilot.svg)](https://pypi.org/project/prpilot/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A development-PM agent that, on a foundation of **spec-driven development**, treats GitHub Issues as "state"
and **orchestrates multiple coding CLIs** (Claude Code / Codex / Cursor)
**with mutual exclusion enforced via labels**.

It runs no server (no Webhook/FastAPI); all state is persisted on GitHub (labels + metadata in the Issue body +
checkboxes) and in spec files. Even if the process dies, reading the Issue lets it **resume from where it left off**.

## Two-line architecture

```
Line 1 (human in the loop / skill: prpilot-spec)   ← outside the loop
  draft in tmp/spec/ → AI review → human review → create Issue → move spec into .specs/ and open draft PR

Line 2 (prpilot's resident loop / autonomous)       ← this CLI
  implement on the same PR branch → AI review → mark PR ready → wait for CI/approve → auto squash merge
```

- **Line 1** is handled by the Claude Code skill [`prpilot-spec`](.claude/skills/prpilot-spec/SKILL.md).
  The spec draft is placed in `tmp/spec/` to await human approval; after approval it moves to `.specs/YYYY-MM-DD-issue-N.md`,
  is committed on branch `issue-N`, and opens a **draft spec PR** along with an Issue carrying a task checklist.
- **Line 2** is prpilot itself. It takes over that spec PR's branch, **stacks implementation commits onto the same PR**,
  and drives it all the way to auto-merge. Because the spec and the implementation end up in a single PR, it is easy to review.

## Design highlights

- **Mutual exclusion via labels** — `status: busy-<agent>` / `idle` / `blocked` / `completed`.
  Only one CLI runs at a time within the same repository. The lock lives on GitHub, so it is shared across processes.
- **State persisted in the Issue body** — saved as JSON in an HTML comment at the end of the body
  (`<!-- PRPILOT_STATE_START ... PRPILOT_STATE_END -->`), holding `next_action` / `branch_name` / `spec_path` / `pr_number` and so on.
- **Progress via checkboxes** — `- [ ] task` items in the Issue body are worked through and updated to `- [x]`. Incomplete tasks are
  **handed to a single CLI run all at once** (because each CLI is a one-shot headless invocation that loses its context every time,
  launching one per task would force it to re-read the codebase). Once verify passes, all items are checked.
- **Spec-driven** — the spec file's body is injected into each implement/review prompt. The agent follows the spec.
- **CLI/model fixed per phase** — the CLI and model used in the `implement` / `review` phases are specified explicitly
  in the config (the `command` under `agents.implement` / `agents.review`). There is no dynamic routing by an LLM.
- **Self-healing** — CLI run → tests → on failure, retry with the log attached (up to N times). If that fails, `git reset --hard`
  rolls back, sets `status: blocked`, and escalates to a human.
- **Clarification** — if the spec is unclear during implementation, the agent writes `.agent_question.md` and exits. prpilot comments on the Issue,
  sets `status: blocked`, and once an answer is posted it resumes with `--resume`.
- **CI/approve gate → auto-merge → post-merge CI gate** — after the PR is created, it polls as `wait_ci`.
  Once all CI succeeds and the PR review is approved (`require_approval`), it squash-merges. On CI failure it blocks.
  When `mergeable=CONFLICTING`, it merges `origin/<base_branch>` in the worktree to auto-resolve, then
  re-runs CI after pushing (can be disabled with `auto_resolve_conflicts`).
  After merging it does not close the Issue but proceeds to `verify_merge`, closing the Issue only when the CI of the merge
  commit on the merge target branch (`base_branch`) is green (regression detection; can be disabled with `post_merge_ci`).
- **blocked notifications** — when transitioning to `blocked` or awaiting clarification, it pushes to a configured Slack webhook or
  macOS notification (the `notify` section, disabled by default). Consecutive re-transitions for the same reason are suppressed, and
  a re-block after leaving blocked notifies again. Send failures only produce a warning log; the main process continues.

## State machine (Line 2)

```
start ─▶ implement ─(tasks remain)▶ implement
              │(tasks done)
              ▼
          ai_review ─▶ create_pr ─▶ wait_ci ─(CI green + approve)▶ verify_merge ─(post-merge CI green)▶ done(closed)
              │                        │(CI failed)          │(post-merge CI failed)
        (spec unclear) ▼                ▼                     ▼
   wait_for_clarification            blocked (human step-in) ◀──────┘
     (blocked) ──(answer + --resume)──▶ implement
```

## Installation

```bash
cd prpilot && pip install -e .
```

Prerequisites: `gh` (already `gh auth login`ed), and each coding CLI you use being authenticated and able to run headless.

## Setup

```bash
prpilot init && $EDITOR ~/.prpilot.yaml   # edit repositories / agents, etc.
```

### Config file lookup order

Only the first file found is used (no merging).

1. `--config <path>` (explicit)
2. `~/.prpilot.yaml`
3. `~/.prpilot.yml`

Config is **centralized in `~/.prpilot.yaml` at home**. Declare multiple repositories under `repositories:` as a
mapping keyed by alias, and put shared settings under `defaults:`. Each repo entry requires
`repo` (owner/repo) and `path` (local clone).

`max_parallel_repos` limits the number of repos run concurrently during `loop` (default 3).

`daemon_log` / `daemon_pid` set the log base path and PID file for daemon residency (defaults are
`~/.prpilot/prpilot.log` / `~/.prpilot/prpilot.pid`). When started with `-d`, the start date
`-YYYY-MM-DD` is inserted just before the log base name's extension (e.g. `~/.prpilot/prpilot-2026-07-18.log`). No rotation is done.

### Event log (for observation)

The result of each step (`action` / `detail`) is, by default, also recorded in SQLite at `~/.prpilot/events.db`
(changeable via `event_db`, empty string disables it). **The labels and Issue body on GitHub are the source of truth**,
and the event DB is derived data for observation/auditing (losing it does not affect resumability). Do not lean state on the DB.

`dry_run` and `action=skipped` (passes such as lock held / waiting on CI) are not recorded. There is no fixed aggregation command
(such as `metrics`); the intent is to accumulate raw events and later have an AI skill write SQL for ad-hoc analysis.

In v1, `prpilot history` **only opens a single DB** (when `-r` is unspecified, the `event_db` of the first repo).
If you split `event_db` per repo, events from other DBs are not visible from `history`.

If you place `event_db` inside the repository, add `*.db` / `*.db-wal` / `*.db-shm` to `.gitignore`
(not needed under the default `~/.prpilot/`).

## Config reference

A list of all keys. For a template, see [`config.example.yaml`](src/prpilot/config.example.yaml) (`prpilot init` copies it
to `~/.prpilot.yaml`). Within string values, `${VAR}` / `${VAR:-default}` expand to environment variables
(`${VAR}` errors if unset, `${VAR:-default}` uses default when unset).

The shared settings under `defaults:` and each `repositories.<alias>` entry are **deep-merged key by key**
(nested mappings merge recursively; scalars/lists are replaced by the repo side). The RepoConfig items below can be written
in `defaults:` as well as in each repo entry.

### Top level

| Key | Default | Description |
| --- | --- | --- |
| `repositories` | (required) | Mapping of repo configs keyed by alias. Select with `-r <alias>` |
| `defaults` | `{}` | Fallback shared by all repos (takes the RepoConfig items below) |
| `max_parallel_repos` | `3` | Upper bound on repos run concurrently during `loop` (1 or more) |
| `daemon_log` | `~/.prpilot/prpilot.log` | Log base path for `loop -d` (start date `-YYYY-MM-DD` is appended) |
| `daemon_pid` | `~/.prpilot/prpilot.pid` | PID file for `loop -d` |

### repositories.&lt;alias&gt; (= RepoConfig; can also go in `defaults`)

| Key | Default | Description |
| --- | --- | --- |
| `repo` | (required) | GitHub `owner/repo` |
| `path` | (required) | Absolute path to the local clone (`~` allowed) |
| `base_branch` | `""` | The PR's merge target. Work branches are also cut from here. Empty means auto-detect via `gh` |
| `branch_prefix` | `"issue-"` | Prefix for work branch names (`issue-N`) |
| `spec_dir` | `".specs"` | Directory where spec files are placed |
| `test_command` | `""` | Test command run at verify. Empty skips test verification |
| `poll_interval` | `60` | Polling interval for `loop` (seconds) |
| `question_file` | `".agent_question.md"` | File the agent writes clarifications to |
| `merge_method` | `"squash"` | `squash` / `merge` / `rebase` |
| `require_approval` | `true` | Whether auto-merge requires a PR review approval |
| `address_pr_reviews` | `true` | Whether to have the review agent address PR review comments (human / bot) |
| `delete_branch_on_merge` | `true` | Whether to delete the work branch after merge |
| `post_merge_ci` | `true` | Whether to confirm the base branch's CI is green after merge before closing the Issue |
| `post_merge_ci_grace` | `180` | Grace before treating post-merge CI as "no CI" when there are 0 checks (seconds) |
| `worktree_dir` | `""` | Location for per-issue git worktrees. Empty means `../<repo-name>-worktrees` |
| `worktree_setup` | `""` | Command run once, immediately after a worktree is newly created |
| `auto_resolve_conflicts` | `true` | Whether to merge base to auto-resolve when the PR is CONFLICTING |
| `conflict_max_retries` | `3` | Retry cap for conflict resolution across loops |
| `auto_fix_ci` | `true` | Whether to fetch logs on PR CI failure and auto-fix in implement |
| `ci_fix_max_retries` | `3` | Retry cap for CI fixes across loops |
| `transient_error_patterns` | default pattern set | Regexes treated as transient errors (case-insensitive). `resource_exhausted` / `rate limit` / `\b(429\|503)\b`, etc. |
| `transient_max_retries` | `5` | Retry cap for transient errors across loops |
| `max_retries` | `3` | Self-healing retry cap within a single run |
| `issue_max_agent_runs` | `10` | Cumulative agent-run cap per Issue. `0` means unlimited |
| `lock_ttl` | `14400` | Absolute TTL of the busy lock (seconds). On the same host, pid check takes precedence |
| `event_db` | `~/.prpilot/events.db` | SQLite path for the structured event log. Empty string disables it |
| `activity_dir` | `~/.prpilot/activity` | Location for activity files while the daemon runs. Empty string disables it |
| `env` | `{}` | Environment variables injected into `gh` calls for this repo (`~` expansion applies) |
| `agents` | (required) | Phase → CLI invocation definition (below) |
| `labels` | defaults below | Status label names |
| `target` | `{}` | Filter conditions for the Issues to pick up |
| `notify` | all disabled | Push notifications for blocked, etc. |
| `sandbox` | `driver: none` | Docker execution of setup / verify (opt-in) |

### agents (required)

The two phases `implement` / `review` are required. Each phase is a mapping with a `command`.
`command` is a single string, or a fallback chain (a list, with the primary command first).
The `{prompt}` placeholder is replaced with the `shlex.quote`d prompt.

```yaml
agents:
  implement:
    command:
      - "cursor-agent -p {prompt} --model auto"
      - "claude -p {prompt} --model opus --dangerously-skip-permissions"
  review:
    command: "claude -p {prompt} --model sonnet --dangerously-skip-permissions"
```

### labels

| Key | Default |
| --- | --- |
| `idle` | `"status: idle"` |
| `blocked` | `"status: blocked"` |
| `completed` | `"status: completed"` |
| `busy_prefix` | `"status: busy-"` (busy labels are generated dynamically as `status: busy-<agent>`) |

### target (filter for open Issues to pick up)

| Key | Default | Description |
| --- | --- | --- |
| `labels` | `[]` | Required labels (string / list). Narrows targets by labels **other than** status labels |
| `assignee` | `""` | Filter by assignee |
| `milestone` | `""` | Filter by milestone |

### notify (push notifications on blocked / awaiting clarification / completion)

| Key | Default | Description |
| --- | --- | --- |
| `slack_webhook_url` | `null` | Slack Incoming Webhook URL. Unset disables Slack |
| `slack_mention` | `null` | Mention string to attach to the Slack body |
| `macos` | `false` | Whether to emit macOS notifications |
| `on_completed` | `true` | Whether to also notify on merge completion |

### sandbox (Docker execution of setup / verify, opt-in)

| Key | Default | Description |
| --- | --- | --- |
| `driver` | `"none"` | `none` / `docker` |
| `image` | `""` | Required when `driver: docker` |
| `network` | `"default"` | `default` / `none` |
| `user` | `"auto"` | `auto` (= `$(id -u):$(id -g)`) / `"1000:1000"` / `""` (omit `--user`) |
| `env` | `{}` | Environment variables inside the container (`~` expansion applies) |
| `env_passthrough` | `[]` | Names of environment variables passed through from the host (e.g. `GH_TOKEN`) |
| `volumes` | `[]` | Additional mounts (`name:path`) |
| `isolate_dirs` | `[]` | Isolate this path inside the worktree via a container-only writable mount (e.g. `.venv` / `node_modules`). Relative to the worktree root, no `..` |

## Usage

```bash
# Line 1: prepare the spec and Issue (in Claude Code)
/prpilot-spec  ...request...

# Line 2: prpilot (cwd is arbitrary; the configured path is the base for git operations)
prpilot status                     # Issue state across all repos
prpilot status -r main             # only alias main
prpilot history                    # local event log (latest 50)
prpilot history -r main --issue 42 # filter by repo / Issue
prpilot run 42 -r main --dry-run # inspect the plan for the next single step
prpilot run 42 -r main             # run Issue #42 to completion
prpilot run 42 -r main --resume    # resume from awaiting-clarification
prpilot loop                       # resident parallel polling across all repos
prpilot loop -d                    # background residency (does not occupy the terminal)
prpilot loop --stop                # stop the resident daemon
prpilot loop -r a -r b             # only aliases a, b
prpilot loop --once                # one pass over all repos (for cron)

# After starting the daemon, tail the actual log path shown in the banner
# tail -f ~/.prpilot/prpilot-2026-07-18.log

# cron example (every 5 minutes):  */5 * * * * prpilot loop --once >> ~/.prpilot/prpilot-cron.log 2>&1
```

**Choosing a residency mode**: `loop -d` (daemon residency) and `loop --once` × cron are mutually exclusive. Use only one.
Daemon logs accumulate one file per start date. Manage size at your discretion with `logrotate` or similar.

## Notes

- Within the same repository, Issues are processed serially (one active at a time). Multiple repositories can run in parallel via `loop`.
- Each coding CLI's headless auto-approval flag (`--dangerously-skip-permissions`, etc.) is set at your own risk.
- With `require_approval: true`, a PR is not merged until it gets an approval. In setups with no approver,
  set it to `false`, or gate on an approve from another agent/human.
- Place the spec on the work branch `issue-N` (= the spec PR's branch), and **keep the spec PR as a draft; do not merge it**.
  prpilot finds and takes over origin's spec PR by branch name. If you merge it first, the spec lands on default, so implementation
  itself can continue, but the implementation splits off into a separate new PR.
