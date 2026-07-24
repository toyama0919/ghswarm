---
name: ghswarm-check
description: "Diagnoses Issues where ghswarm stopped its autonomous loop at status: blocked, and for those safely resolvable, confirms verify passes in a worktree, then returns them to status: idle to bring them back. Use for ghswarm has stopped, checking blocked, returning to idle, checking development status, isolating the cause of a stop, etc. Called as a later stage in the same session as ghswarm-spec."
---

# ghswarm-check (diagnose blocked Issues, return to idle)

A skill that, for Issues where ghswarm (Line 2's resident loop) stopped autonomous processing at `status: blocked`, **isolates and diagnoses the cause of the stop, and for those safely resolvable, fixes them in a worktree, confirms verify passes, and returns them to `status: idle` to bring them back into the loop**.

- Line 1 (ghswarm-spec): spec filing → Issue creation → (optional) starting development with `ghswarm run`
- **This skill (later stage)**: **diagnosis → remediation → return to idle** when run or loop stopped at blocked

## Overview

**The autonomy level is "diagnosis + safe automatic recovery"**. Isolate the cause, and for things reliably resolvable via code fixes or counter adjustments, fix them and go all the way to returning to idle. For things requiring judgment (awaiting clarification, product decisions, post-merge CI failure, etc.), stop at reporting and keep blocked.

**Non-destructive principles** (always follow):

- No force push, no history rewriting, no closing the Issue
- Do not delete the worktree's WIP savepoint commit
- The target is **only the git repository in the current directory** (other repos are out of scope)
- A single session may target multiple Issues (chosen by the user or from the blocked list)

## Prerequisites

- `gh` (authenticated) and the `ghswarm` CLI are usable.
- **The target is always the git repository in the current directory**. Before starting, `cd` into the clone of the target repository. owner/repo is auto-detected by `gh`, so do not pass `--repo`.
- Usually called from **a later stage of the same session** where you created the spec/Issue with `ghswarm-spec` and ran `ghswarm run`. The immediately preceding run's log (stderr) is the primary source for diagnosis.
- **In step 0, run `ghswarm config`, obtain the resolved values, and use them in later steps** (do not hardcode).
- If `ghswarm config` exits non-zero, use these fallback defaults: `spec_dir=.specs` / `branch_prefix=issue-` / `base_branch=main` / `idle_label=status: idle` / `blocked_label=status: blocked` / `path=<cwd>`.

## ghswarm's recovery mechanism (facts based on the implementation)

### blocked = lock

`labels.is_locked()` treats the `status: blocked` label (`ghswarm config`'s `blocked_label`) **as a lock, same as busy labels**. A blocked Issue is skipped by `ghswarm loop`'s polling and does not advance autonomously. `ghswarm run <N>` also skips it normally, but there is a path to resume blocked with `--resume` (such as resuming from awaiting clarification `clarification`).

### GHSWARM_STATE persistence

State is saved as JSON inside the `<!-- GHSWARM_STATE_START` … `GHSWARM_STATE_END -->` HTML comment at the end of the Issue body (`state.py`). Main fields:

| Field | Purpose |
|---|---|
| `phase` | Current phase. `"blocked"` when blocked |
| `branch_name` | Work branch (`<branch_prefix><N>`) |
| `next_action` | The next action to run (`implement` / `ai_review` / `wait_ci`, etc.) |
| `spec_path` | Spec file path relative to the repository root |
| `transient_retries` | Cumulative transient errors across loops (reset to 0 when the relevant phase completes successfully) |
| `conflict_retries` | Cumulative conflict resolutions across loops (reset to 0 on successful resolution) |
| `ci_fix_retries` | Cumulative CI fixes across loops (reset to 0 on successful fix) |
| `total_agent_runs` | Cumulative agent runs per Issue (**not reset even on success**) |
| `last_notified_reason` | For deduplicating blocked notifications. On manual idle recovery, clear it to `null` in the body JSON |

### Counter handling per cause (do not conflate)

| Stop reason | Counter to adjust | Note |
|---|---|---|
| `budget_exhausted` | `total_agent_runs` | Compared against config `issue_max_agent_runs`. Reset to 0 or raise the cap |
| `transient_exhausted` | `transient_retries` | Reset to 0 |
| `conflict` | `conflict_retries` | Reset to 0 |
| `ci_failed` | `ci_fix_retries` | Reset to 0 |
| **`implement_failed` / `review_failed` (`reason=max_retries`)** | **No counter needed** | Failed in the in-process self-healing loop (`max_retries` times). There is no persistent counter across loops. The correct remediation is **root-fix in the worktree → return the label** |
| `spec_missing` / `spec_not_in_branch` | No counter needed | Requires setting `spec_path` or committing the spec to the branch |
| `clarification` | No counter needed | Awaiting clarification. `--resume` after an answer |
| `merge_ci_failed` / `git_error` / `pr_create_failed` | No counter needed | Often requires human judgment |

### verify command resolution order (`spec.py` / `_test_command_for`)

The verify that runs in the implement, review, conflict-resolution, and CI-fix phases is resolved in the following priority:

1. **The spec front-matter `verify:`** (reads the file `GHSWARM_STATE.spec_path` points to, within the worktree)
2. config's `test_command` (does not appear in `ghswarm config`; refer to the repo's config YAML)
3. empty → local verification skipped (CI alone is the gate)

A list-form `verify` wraps each element in a subshell `(...)` and joins with ` && `. A string form is run as-is as a single command.

### Immediate resume methods

After returning to idle:

```bash
ghswarm run <N>              # normal resume (add -r <ALIAS> when multiple repos are registered)
ghswarm run <N> --resume     # when the blocked label remains / after budget_exhausted /
                             # wait_for_clarification (--resume required even if the label is idle)
```

`--resume` bypasses the blocked-label lock and clears `last_notified_reason` in memory. If you returned the label to idle in step 3, `ghswarm run <N>` is usually enough.

If `ghswarm loop` is running, an idle-ized Issue is picked up automatically on the next poll (confirm with `pgrep -fl "ghswarm loop"`).

## reason_code quick reference

The reason_codes that `orchestrator._enter_blocked()` / `_block_for_missing_spec()` set, and their remediation. Identify by cross-referencing the caller's `gh.comment` (if any) and `GHSWARM_STATE.last_notified_reason`.

| reason_code | Typical detail / comment traits | Auto-recovery possible? | Remediation | Counter adjustment |
|---|---|---|---|---|
| `implement_failed` | `reason=max_retries` most common (also `cli_failed`, etc.). `⚠️ **Task processing failed**` comment | **Possible if fixed until verify passes** | Reproduce verify in worktree → root-cause fix → confirm verify passes | **None** (needs root fix) |
| `review_failed` | `reason=max_retries`, etc. `⚠️ **Review failed**` or `⚠️ **PR review handling failed**` comment | Same as above | Same as above | **None** (needs root fix) |
| `ci_failed` | CI URL, or `no_changes` / `push_failed` | Fixable if GHA logs are available. Report only for non-GHA | Fix the CI failure cause in worktree, confirm with `gh pr checks` | `ci_fix_retries` → 0 |
| `conflict` | `commit_failed` / `push_failed`, etc. | Possible if manual resolution is reliable | Resolve the merge conflict in worktree → push → verify | `conflict_retries` → 0 |
| `transient_exhausted` | Transient errors exceeded `transient_max_retries` | Possible if the root cause is resolved | Confirm the transient cause (environment, network, etc.) | `transient_retries` → 0 |
| `budget_exhausted` | `total_agent_runs` / `issue_max_agent_runs` | Report only if the cap is intentional | If continuing is reasonable, lower `total_agent_runs` or raise the cap in config | `total_agent_runs` → 0 (or an appropriate value) |
| `git_error` | git-operation exception message. Often no Issue comment | Environment-dependent; usually report | Check run's stderr, daemon log, worktree / remote / auth | None |
| `clarification` | `🤖 **Clarification from …**` comment | **Report only** (keep blocked) | Present the question to the user. `--resume` after an answer | None |
| `merge_ci_failed` | Post-merge CI failed. Issue stays open | **Report only** (product decision) | A human checks/fixes the merge target's CI | None |
| `pr_create_failed` | PR creation exception | Conditional | Check the branch, permissions, existing PR | None |
| `spec_missing` | `🚫 **Cannot start**: spec is not set` | **Possible** (after setting the spec) | Set `spec_path` in GHSWARM_STATE, or re-file with ghswarm-spec | None |
| `spec_not_in_branch` | `🚫 **Cannot start**: spec is not in the branch` | **Possible** (after committing the spec) | Commit and push the spec to the work branch | None |

## Steps

### 0. Fetch config and identify the target Issue

**Fetch ghswarm config** (when multiple repos are registered or cwd is an unregistered repo, add `-r ALIAS`):

```bash
ghswarm config          # or ghswarm config -r <ALIAS>
```

On success, extract the following from the JSON and keep them as variables:

- `path` — the repository's local path (to confirm the `cd` target)
- `spec_dir` / `branch_prefix` / `base_branch`
- `idle_label` / `blocked_label`
- `target` — Issue extraction conditions

**Identify the target Issue**:

- If the user specified a number, use it (an Issue you `ghswarm run` in the same session is typical)
- If unspecified, list Issues with the blocked label:

```bash
cfg=$(ghswarm config)   # use fallback blocked_label on failure
blocked=$(jq -r '.blocked_label' <<<"$cfg")
gh issue list --label "$blocked" --state open --json number,title,labels
```

If there are several, confirm with the user or narrow by session context (the immediately preceding run target).

### 1. Diagnosis

Priority of primary sources (**since a later stage of the same session is the main use, prioritize run's log output**):

#### (a) State and stop reason

Extract `GHSWARM_STATE` from the Issue body:

```bash
gh issue view <N> --json body -q .body | python3 -c "
import re, json, sys
body = sys.stdin.read()
m = re.search(r'<!-- GHSWARM_STATE_START\s*(.*?)\s*GHSWARM_STATE_END -->', body, re.DOTALL)
print(json.dumps(json.loads(m.group(1)), indent=2, ensure_ascii=False) if m else '{}')
"
```

Items to check: `phase` (is it `blocked`), `next_action`, `spec_path`, `branch_name`, each counter (`transient_retries` / `conflict_retries` / `ci_fix_retries` / `total_agent_runs`).

**blocked-related comments** (refer to the `gh.comment` each caller posts before `_enter_blocked`, if any; `_enter_blocked` itself is Notifier-only and posts no Issue comment. This is separate from desktop/slack Notifier notifications):

```bash
gh issue view <N> --comments
# or the latest comment:
gh api repos/{owner}/{repo}/issues/<N>/comments --jq '.[-1].body'
```

Comments starting with `⚠️` / `🚫` hold clues to the reason (`reason=max_retries`, CI URL, spec path, etc.).

#### (b) `ghswarm run` log / daemon log

**Right after `ghswarm run` in the same session**, the background task's stderr (log) is the primary source. Look for these lines:

- `Issue #<N> -> <action>: <detail>` — the step just before the stop (`failed` / `blocked`, etc.; `cli.py`'s `rlog.info`)
- `running tests: <command>` — the resolved verify command (`executor.run_tests`'s log output)

Only when `ghswarm loop` is resident can you also refer to the daemon log:

```bash
ls -t ~/.ghswarm/ghswarm-*.log | head -1   # pick the most recent date file
grep -E "Issue #<N>|running tests:" ~/.ghswarm/ghswarm-YYYY-MM-DD.log | tail -20
```

If loop is not running and you did not run in the same session, rely mainly on Issue comments and reproducing verify in the worktree.

#### (c) Identify the worktree

The worktree path depends on the `worktree_dir` setting and does not appear in `ghswarm config`. **Identify the work branch's actual path with `git worktree list --porcelain`**:

```bash
cfg=$(ghswarm config)
branch_prefix=$(jq -r '.branch_prefix' <<<"$cfg")
target_branch="${branch_prefix}<N>"

git worktree list --porcelain | awk -v b="$target_branch" '
/^worktree / { wt=$2 }
/^branch / { if ($2 == "refs/heads/" b) print wt }
'
```

If not found, ghswarm has not created a worktree yet (a pre-start `spec_missing`, etc.). Check with `GHSWARM_STATE.branch_name` and the repository in the `path` setting.

#### (d) Reproduce verify

**The actual verify is the spec front-matter `verify:`** (takes precedence over config `test_command`). Read the spec at `GHSWARM_STATE.spec_path` within the worktree and reproduce the command that actually ran:

```bash
# assume the worktree path is in WT and spec_path is already obtained from STATE
spec_file="$WT/<spec_path>"   # e.g. $WT/.specs/2026-07-22-issue-86.md

# extract the verify command (a list is normalized the same as subshell joining)
verify_cmd=$(python3 -c "
import yaml, re, sys
text = open('$spec_file').read()
m = re.match(r'---\n(.*?)\n---', text, re.S)
meta = yaml.safe_load(m.group(1)) if m else {}
v = meta.get('verify', '')
if isinstance(v, list):
    parts = [str(x).strip() for x in v if str(x).strip()]
    print(' && '.join(f'({p})' for p in parts))
else:
    print(str(v).strip())
")

# run with the worktree as cwd (same conditions as ghswarm)
if [ -z "$verify_cmd" ]; then
  echo "local verification skipped (verify / test_command not set)"
else
  cd "$WT" && eval "$verify_cmd"
fi
```

If the spec has no `verify` and it falls back to config's `test_command`, refer to the relevant repo entry in `~/.ghswarm.yaml`. If both are empty, local verification was being skipped (`run_tests` exits 0).

**In repos with `sandbox.driver: docker`**, verify runs inside a Docker container (`executor.make_runner`). The manual reproduction above is a shell on the host, so **the local reproduction may diverge from the result** (presence of dependency packages, paths, permissions, etc.). In docker-mode repos, also consider the docker execution environment when isolating the failure cause.

For verify-failure types (`implement_failed` / `review_failed`), this reproduction is the **center of diagnosis**. Cross-reference it with the failure logs in run's log / comments to pin down the root cause.

### 2. Judgment and remediation (auto-recovery / report only)

Following the reason_code quick reference, isolate as follows:

**Examples where auto-recovery is fine**:

- `implement_failed` / `review_failed`: fixed the root cause in the worktree and verify passed
- `transient_exhausted` / `conflict` / `ci_failed`: the cause is resolved and retrying after a counter reset is reasonable
- `budget_exhausted`: judged continuing reasonable, and adjusted the counter
- `spec_missing` / `spec_not_in_branch`: setting spec_path or committing the spec to the branch is complete

**Report only (keep blocked)**:

- `clarification`: present the question to the user. `--resume` after an answer
- `merge_ci_failed`: post-merge regression. Requires a product decision
- `git_error` / `pr_create_failed`: environment/permission/config issues where auto-resolution is uncertain
- Unknown cause, or changes requiring judgment

Cautions during remediation:

- When fixing in a worktree, do not delete the WIP savepoint commit (stack fix commits on top)
- For verify-failure types, **always confirm verify passes before** returning to idle
- For `spec_not_in_branch`, commit and push the spec to the branch before recovering

### 3. Return to idle

**Order: adjust the body `GHSWARM_STATE` (if applicable) → change the label from `blocked` to `idle`**. Verify-failure types need no counter adjustment, but clear `last_notified_reason` for all reasons.

#### 3-1. Adjust GHSWARM_STATE

Strictly preserve the `GHSWARM_STATE_START` / `GHSWARM_STATE_END` markers and other fields, and **edit only the target fields in place**. Do not do a full replacement or delete the markers.

**Common to all reasons**: clear `last_notified_reason` to `null` (to avoid Notifier suppression when it re-blocks for the same reason). `phase` stays `"blocked"` from a label change alone, but is overwritten on the next successful `ghswarm run`.

```bash
# example: reset transient_retries to 0 (reason=transient_exhausted)
gh issue view <N> --json body -q .body > /tmp/issue-body.md

python3 <<'PY'
import re, json, sys

path = "/tmp/issue-body.md"
body = open(path).read()
m = re.search(
    r'(<!-- GHSWARM_STATE_START\n)(.*?)(\nGHSWARM_STATE_END -->)',
    body, re.DOTALL
)
if not m:
    sys.exit("GHSWARM_STATE not found")
state = json.loads(m.group(2))
state["last_notified_reason"] = None    # clear for all reasons
state["transient_retries"] = 0          # field to change depending on reason
# state["conflict_retries"] = 0         # conflict
# state["ci_fix_retries"] = 0           # ci_failed
# state["total_agent_runs"] = 0         # budget_exhausted
new_body = body[:m.start()] + m.group(1) + json.dumps(state, ensure_ascii=False, indent=2) + m.group(3) + body[m.end():]
open(path, "w").write(new_body)
PY

gh issue edit <N> --body-file /tmp/issue-body.md
```

Even for verify-failure types where counter adjustment is unnecessary, run just the `last_notified_reason` clear above. If you edit only the JSON part with `jq`, leave the marker lines intact.

#### 3-2. Return the label blocked → idle

```bash
cfg=$(ghswarm config)
idle=$(jq -r '.idle_label' <<<"$cfg")
blocked=$(jq -r '.blocked_label' <<<"$cfg")

# labels.release() removes all status labels (idle/blocked/completed/busy-*) then re-applies them.
# Manually, first check which status labels are attached and remove the ones other than idle.
gh issue view <N> --json labels -q '.labels[].name'
gh issue edit <N> --remove-label "$blocked" --add-label "$idle"
# if busy-* or completed remain, --remove-label them individually (rescue from crash remnants)
```

If `ghswarm config` is unavailable, use the fallback `status: blocked` / `status: idle`.

#### 3-3. Guidance for immediate resume

```bash
# when loop is not running (add -r <ALIAS> for multiple repos). Enough if you idle-ized in step 3-2
ghswarm run <N>

# when the blocked label remains / after budget_exhausted / clarification (wait_for_clarification)
ghswarm run <N> --resume
```

If loop is running (via `pgrep -fl "ghswarm loop"`), tell them that idle-izing alone is enough for it to be picked up on the next poll.

### 4. Report

Report the following concisely to the user (no need to enumerate commands):

- The target Issue URL and the stop reason_code
- The diagnosis result (cause of the verify failure, whether a counter cap was hit, etc.)
- The remediation performed (worktree fix content, counter adjustment, spec commit, etc.)
- Whether you returned to idle / kept blocked and why
- How to resume (loop auto-pickup / `ghswarm run` / `--resume` / additional human work)

## Out of scope (things not to do)

- Changing ghswarm's own (Python) code
- Diagnosis across other repositories
- Automatic bulk recovery of blocked
- Force push, history rewriting, closing Issues
- Deleting worktree WIP commits
