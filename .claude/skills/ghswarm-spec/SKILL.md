---
name: ghswarm-spec
description: Line 1 of spec-driven development. Drafts a spec into tmp/spec/ from a feature request, chat YAML, or topic; goes through human review (approval) → an independent review by a subagent in a separate session triggered by that approval; then moves the spec to .specs/YYYY-MM-DD-issue-N.md, opens a spec PR, and registers a GitHub Issue with a task checklist for ghswarm. Use for creating specs, writing specs, turning things into Issues, spec-driven work, and filing ghswarm tasks.
---

# ghswarm-spec (spec-driven Line 1)

A skill that, in collaboration with a human, goes all the way from **writing a spec to filing a GitHub Issue** in a form ghswarm (Line 2's resident loop) can consume.

- Line 1 (this skill, human in the loop): **draft into tmp/spec/ → human review (approval) → AI review in a separate session triggered by that approval → create Issue → move spec to its canonical location and open a spec PR**
- Line 2 (ghswarm daemon, autonomous): implement **on that spec PR's branch** → AI review → mark PR ready → wait for CI/approve → auto-merge

This skill **does not implement**. The goal is to prepare an approved spec, an Issue with a checklist of decomposed tasks, and a spec PR that serves as the vessel for the implementation.

**Spec PR approach**: the spec is committed not on the default branch but on the work branch `issue-<N>`, and put out first as a draft PR. ghswarm takes over this branch, stacks implementation commits onto the same PR, and marks the PR ready when review is complete. Because **the spec and the implementation end up in a single PR**, reviewers can check "what was supposed to be built" and "what was actually built" in the same diff.

## Prerequisites

- `gh` (authenticated).
- **The target is always the git repository in the current directory** (same convention as ghswarm). Before starting, `cd` into the clone of the target repository. owner/repo is auto-detected by `gh`, so do not pass `--repo`.
- Respect ghswarm's config (`spec_dir`, `target`, `branch_prefix`, `base_branch`).
  **In step 0, run `ghswarm config`, obtain the resolved values, and use them in later steps** (do not hardcode).
  Extract target Issues via `target` (`labels` / `assignee` / `milestone`). **The policy is not to create a dedicated
  selection label (such as `pm-agent`)**. If you operate with `target.assignee: "@me"`, assign the Issue to yourself
  and let ghswarm pick it up. Read the config's `target` and create the Issue so it satisfies those extraction conditions
  (assign it for an assignee-based setup, apply that label for a label-based setup).
- If `ghswarm config` exits non-zero (config not created, current repo not registered, `jq` absent, etc.), use these
  fallback defaults: `spec_dir=.specs` / `branch_prefix=issue-` / `base_branch=main` /
  `idle_label=status: idle` / `blocked_label=status: blocked` /
  `issue_create_args=("--label" "status: idle")` (if target is unknown, assignee/label is idle only).

## Steps

### 0. Understand the input and fetch ghswarm config
Confirm the user's request (feature, bug, or change), or the materials handed over (inbox chat YAML, notes, topic). Ask if unclear. Confirm the working directory is a **clone of the target repository** (`gh repo view --json nameWithOwner`). If not, `cd` into the correct repository.

**When the input is an existing issue's URL/number** (e.g. you want to turn a roughly-written existing issue into ghswarm form), fetch its content with `gh issue view <N> --json number,title,body,labels,assignees` and record that number N as the **source issue**. In this case, instead of creating a new issue in step 4, you have the option to **convert that existing issue into ghswarm form** (whether to create new or convert is confirmed in the human review in step 2).

**Fetch ghswarm config** (used in later steps; when multiple repos are registered or cwd is an unregistered repo, add `-r ALIAS`):

```bash
ghswarm config          # or ghswarm config -r <ALIAS>
```

On success, extract the following from the JSON and keep them as variables (`jq` may be used):

- `spec_dir` — where spec files go (e.g. `.specs`)
- `branch_prefix` — prefix for work branches (e.g. `issue-`)
- `base_branch` — the default branch (fallback `main` if empty)
- `idle_label` / `blocked_label` — ghswarm mutual-exclusion labels (e.g. `status: idle` / `status: blocked`)
- `issue_create_args` — the array of flags to pass to `gh issue create` (already reflecting idle + target). **When creating, add `blocked_label` on top of these, and remove blocked at the very end after writing spec_path** (see steps 4-2/4-6; this avoids a race where the loop picks it up before spec_path is written).

If `ghswarm config` exits non-zero, switch to the fallback defaults above.

**Determine the canonical spec language (`spec_lang`)**: whether the spec should stay in the draft's language once it lands in `<spec_dir>/`, or be translated, is decided from **the target repository's `README.md` language** — record this as `spec_lang` (used in step 4-3). Read the top of `README.md` (title/overview) and take its dominant language as `spec_lang` (e.g. mostly Japanese → Japanese, mostly English → English). If there is no README or its language is unclear, look at the language of existing spec files under `<spec_dir>/` (the most recent one, if any); if there are none either, **keep `spec_lang` as the draft's own language (no translation)**.
**Do not hardcode OSS vs. internal**—an OSS repo's README is naturally in English so its specs end up in English, while an internal repo whose README is in Japanese keeps its specs in Japanese. If the README is multilingual and the signal is ambiguous, ask the user.

**Check the branch you start spec investigation from is up to date**: since the spec is drafted assuming the current codebase, **at minimum check the diff against remote** before starting investigation. Staying on the current branch is generally fine, but fetch and confirm the current branch is not behind, so you don't write a spec based on stale code.

```bash
git fetch
git status -sb   # check whether "behind N" appears
```

If it is behind, let the user know and confirm whether to update with `git pull --ff-only` before investigating, or proceed on the current branch (do not pull on your own). Even when the current branch is a work branch rather than default (main), first fetch to grasp the current state before starting the draft.

### 1. Draft the spec (custom lightweight version)
**First, write it out as a draft file at `tmp/spec/<slug>.md`** (`<slug>` is a short kebab-case name describing the content, e.g. `tmp/spec/github-actions-ci.md`). `tmp/` is a gitignored work area; at this point you do **not commit or turn it into an Issue**.

**Draft language**: write the `tmp/spec/` draft **in the author's native language** (Japanese for a Japanese speaker). The native language makes it easier to notice ambiguity in requirements, and the human review (step 2) is faster. **Do not translate at this stage** — whether it needs translating into `spec_lang` (determined in step 0, from the target repo's README) is decided all at once when you commit to `<spec_dir>/` in step 4 (see step 4-3; no translation is needed at all if `spec_lang` matches the draft language).

Draft the `spec` body in Markdown with the following structure. Don't make it overly large; keep the granularity such that the implementation agent won't get lost.

```markdown
---
verify:
  - <local verification command 1 for this task>
  - <verification command 2>
---
# <title>

## Background / Purpose
Why do this. The problem to solve.

## Requirements
- Behaviors that must be satisfied (bullets; add acceptance criteria if needed)

## Design approach
- The approach taken, scope of impact, libraries/existing patterns used.
- Also state what you won't do (out of scope).

## Task breakdown
- [ ] Concrete implementation task 1
- [ ] Concrete implementation task 2
- [ ] Tests: <test perspective/command>
```

- Make tasks at a granularity of **1 task = roughly 1 commit**. ghswarm hands incomplete tasks
  to a single CLI run all at once, and has the agent produce a commit per task.
- Including acceptance criteria or test commands in the tasks makes it easier for the implementation agent to verify.
- **The front-matter `verify`**: the **local verification steps** (language-agnostic) ghswarm runs after each implement/review attempt, in order, stopping at the first failure. It accepts two shapes:
  - **Legacy form** (a single whole-repo target): a string, or a list of strings; a list wraps each element in a subshell and joins them with ` && ` (the string form is not wrapped).
  - **New form** (per-directory, for monorepos — e.g. a `terraform/` directory alongside a `backend/`): a list of `{path, command}` mappings, **both required on every entry**. Each becomes its own step, run in order in `<worktree>/<path>`. Its execution environment (local vs. Docker/image) is resolved from the target repo's config `verify:` registry by matching `path` exactly — **do not** write `sandbox` here, only in config. **Do not mix the legacy form and the new form** within the same `verify:`.

    ```markdown
    ---
    verify:
      - path: terraform
        command: "terraform validate"
      - path: backend
        command: "pytest"
    ---
    ```
  - This is the gate of the implementation loop, so write commands that **can realistically succeed** for that task (e.g. a startup check `uv run ghswarm --help`, `make test`, `npm test`, etc.). The intent is that exhaustive production tests are delegated to CI. **If you omit `verify` (or leave it an empty list), local verification is skipped entirely** — config no longer carries a fallback command, only a `path -> sandbox` registry (where each step runs), so leaving `verify` unset here always means "no local verification, CI alone is the gate." Since ghswarm generates a per-Issue `verify` for every spec, in normal operation this should not happen unintentionally.
  - **Important (do not pollute the environment / be hermetic)**: each verify step runs **in a bare shell with `<worktree>/<path>` (or the worktree root) as cwd, inheriting the ghswarm process's environment** (no isolation; for a `path` whose config-side sandbox has `driver: docker`, execution is inside a container instead and this constraint is relaxed). Therefore **do not write commands in verify that have side effects rewriting the surroundings (global/ambient interpreter or shared environment)** (except in docker mode). Keep side effects confined within the worktree. Since the worktree is deleted when the loop completes, an install that pollutes the outside leaves broken references every time.
    - ❌ `pip install -e .` / `uv pip install -e .` (without a venv): editable-installs into the surrounding Python, and the `.pth` points at a deleted worktree, **breaking global executables** (a cause of `ModuleNotFoundError`).
    - ✅ For python, use `uv run ...` (e.g. `uv run ghswarm --help`, `uv run --extra dev python -m pytest -q`). `uv run` installs dependencies into the worktree's `.venv` and runs there, and it disappears with the worktree, so it does not pollute the surroundings. To specify a venv explicitly, confine it inside the worktree like `uv venv .venv && uv pip install -e '.[dev]' --python .venv/bin/python && ...`.
    - ✅ Same principle for other languages: avoid global installs (`npm i -g`, `go install`, `pip install --user`, etc.) and rewriting shared caches; use commands confined to the project/worktree.
  - Note: if you specify a bare test runner like `pytest -q` when there are no tests to verify, pytest returns exit 5 ("no tests collected") and is **treated as a failure every time**. For a task with no tests, write a command in `verify` that reliably passes, such as a startup check.

### 2. Human review (approval gate)
Present the `tmp/spec/<slug>.md` path and the full spec text, and ask the user for approval. Always show the path so the user can edit the file directly in an editor. If there are correction instructions, reflect them in the file and re-present. **Do not proceed until you get explicit approval (no creating the Issue, no moving the spec file, no committing).** This approval is the trigger for step 3 (independent review) — do not run a review automatically right after drafting; proceed in the order **human OK first**, then independent review → issue creation/idle-ization.

**When there is a source issue** (step 0), along with spec approval, always confirm "should I convert that existing issue #N into ghswarm form, or create a new issue?" If conversion is chosen, proceed with step 4 via the **conversion path (4-2 B)** (use the existing issue's number N). If new is chosen, create it normally via **4-2 A**.

### 3. Independent review (run by a subagent in a separate session, triggered by approval)
Run this after the human gives OK in step 2, against **that approved final version** (including the human's edits). This way the review hits the same final text the implementation agent reads, and there is no version drift after approval. **The person who drafted it does not review it.** Re-reading your own spec keeps the drafting assumptions in your context ("this is obvious," "this design must be agreed on"), so you cannot recognize ambiguity as ambiguity. The review must be done by **launching a separate subagent via the Agent tool**. Because the subagent starts with a cold context, it can read under the same "the spec is the only clue" conditions as the implementation agent.

```
Agent tool:
  subagent_type: general-purpose
  model: opus                 # same model as drafting is fine. What we want to separate is context, not the model
  run_in_background: false    # wait for the result before proceeding to issue creation / bounce back to step 2 on failure
```

**The only things you may pass in the prompt are the spec file path and the target repository path.** Do not pass any of the drafting history, the exchanges with the user, or supplements like "the intent here is such-and-such" (the moment you do, it reverts to a self-review). Instruct the subagent on the following review perspectives:

- Are the requirements ambiguous or missing? Have it concretely point out **whether the spec alone is enough to implement, and where guessing would be required**.
- Is the design approach consistent with the existing codebase (have the subagent investigate the repository)?
- Is the task breakdown at an appropriate granularity, in dependency order, and does it include tests?
- Is out-of-scope stated?
- Is the front-matter `verify` a command that **realistically succeeds** in that repository? And is it a hermetic command with **no side effects that pollute the surroundings (global/ambient environment)** (no editable install to global via `pip install -e .` etc.; for python, does it use `uv run`)?
- For a monorepo task touching multiple directories, does `verify` use the new `{path, command}` form (one entry per directory) rather than chaining `cd`s inside a single legacy-form string/list element? And are `path` and `command` both present on every entry, with no mixing of the legacy and new forms?

Reflect valid points into `tmp/spec/<slug>.md`. **The criterion for bouncing back is not severity but "whether a spec-level trade-off arises"** — for fixes that resolve ambiguity, correct verify or wording, or adjust task granularity, i.e. **fixes that do not change the spec decision itself, reflect them and proceed straight to step 4** (it is enough to include "what you fixed" in your report to the human). On the other hand, **points involving a spec-level judgment — a change of design approach, an increase/decrease in scope, or a redefinition of requirements bearing on acceptance criteria — bounce back to step 2**: re-present to the user and get re-approval before proceeding (redo the independent review if needed). **Do not take points at face value** — do not adopt off-target points, and include that in your report to the human. **Summarize this refinement process concisely for the human** (what was pointed out and what you fixed / what you rejected; a verbose full diff is unnecessary).

### 4. Create/convert Issue → move spec to its canonical location → create spec PR → finally make it ready to start
Once you have both human approval (step 2) and passing the independent review (step 3), run the following in order (to fix the Issue number first, keep this order).

**Important (race avoidance)**: the ghswarm loop **picks up Issues that match target and whose status is idle (not busy/blocked) on every poll**. If you create with idle before writing `spec_path` into the body, the loop can pick it up in the gap before writing and block it at the start gate for a missing spec (this actually happens). The fix is simple: **attach the blocked label at creation time, and remove it at the very end once `spec_path` is written**. While blocked is present, `is_locked` always excludes it (blocked takes precedence even if idle is also present), so the loop won't pick it up in the meantime.

1. **Get the date**: `date +%F` (e.g. 2026-07-16).
2. **Fix the Issue (with blocked)** and obtain the number N. The body is "background/purpose, requirements, design-approach summary" + the **task checklist** derived from the spec. Branch into **A (create new) / B (convert existing issue)** by whether there is a source issue.

   **A. To create a new issue** (no source, or new was chosen in step 2). Use `issue_create_args` (idle + target) as-is and just **add blocked on top**:

   ```bash
   cfg=$(ghswarm config)   # or ghswarm config -r <ALIAS>
   blocked=$(jq -r '.blocked_label' <<<"$cfg")
   args=()   # turn issue_create_args into an array (works in both bash/zsh; mapfile is bash-only, don't use it)
   while IFS= read -r a; do args+=("$a"); done < <(jq -r '.issue_create_args[]' <<<"$cfg")
   gh issue create --title "<title>" "${args[@]}" --label "$blocked" --body-file <body.md>
   ```

   (Created in the current repository. Obtain Issue number N from the end of the output URL. If `ghswarm config` is unavailable, create with `--label "status: idle" --label "status: blocked"`.)

   **B. To convert an existing issue #N into ghswarm form** (conversion chosen in step 2). Use the source's number N. **First attach blocked by itself**, then set up the title, body, and target/idle labels (going idle first would let the loop pick it up in the gap, so always attach blocked first). Unlike create, `gh issue edit` uses `--add-label` / `--add-assignee`, so translate `issue_create_args`'s `--label`→`--add-label` and `--assignee`→`--add-assignee`:

   ```bash
   cfg=$(ghswarm config)   # or ghswarm config -r <ALIAS>
   blocked=$(jq -r '.blocked_label' <<<"$cfg")
   gh issue edit <N> --add-label "$blocked"            # ← blocked first (lock before going idle)
   # translate issue_create_args into edit flags and apply
   eargs=()
   while IFS= read -r a; do
     case "$a" in
       --label) eargs+=("--add-label");;
       --assignee) eargs+=("--add-assignee");;
       *) eargs+=("$a");;
     esac
   done < <(jq -r '.issue_create_args[]' <<<"$cfg")
   gh issue edit <N> --title "<title>" --body-file <body.md> "${eargs[@]}"
   ```

   Rewrite the body **entirely** with the same "background/purpose, requirements, design-approach summary + task checklist" as A (reshape the rough original description into ghswarm form). Incorporate any information worth keeping from the original into the spec during drafting. If `ghswarm config` is unavailable, attach `--add-label "status: blocked"` first, then `--add-label "status: idle"` (+ target label/assignment per your setup). The subsequent steps (3–7) are common to A/B, using this N.
3. **Cut the work branch `<branch_prefix><N>` from `<base_branch>`, and move the spec to its canonical location, matching `spec_lang` (determined in step 0)**. Match the branch name to ghswarm's `branch_prefix`+number (ghswarm takes over this branch).
   **Compare the draft's language against `spec_lang`**:
   - **They match** (e.g. an internal repo whose README and draft are both Japanese): no translation needed — just `mv` the draft straight to `<spec_dir>/<YYYY-MM-DD>-issue-<N>.md`.
   - **They differ** (e.g. an OSS repo whose README is English but the draft is Japanese): instead of `mv`, Write the body translated into `spec_lang` to `<spec_dir>/<YYYY-MM-DD>-issue-<N>.md` and delete the original draft. Translate **without changing the meaning**, keeping `verify` commands, code blocks, checklist granularity, and functional keys (label strings, state-machine names, config keys, etc.) intact.
   ```bash
   git switch <base_branch> && git pull --ff-only
   git switch -c <branch_prefix><N>
   mkdir -p <spec_dir>
   # draft language == spec_lang: just move it
   mv tmp/spec/<slug>.md <spec_dir>/<YYYY-MM-DD>-issue-<N>.md
   # draft language != spec_lang: translate tmp/spec/<slug>.md into spec_lang, Write the
   #   translated version to <spec_dir>/<YYYY-MM-DD>-issue-<N>.md, then rm -f the original draft
   git add <spec_dir>/<YYYY-MM-DD>-issue-<N>.md
   git commit -m "spec: #<N> <title>"
   git push -u origin <branch_prefix><N>
   ```
   (When translation is involved, use Write + `rm -f` instead of `mv` — do not leave the native-language draft `tmp/spec/<slug>.md` behind. Do not place the spec in `<spec_dir>/` before cutting the branch = do not put the spec on default. Do not leave draft remnants in `tmp/spec/`.)
4. **Create the spec PR as a draft**. Because this PR **becomes the implementation PR as-is**, match the title to ghswarm's convention `<title> (#<N>)`, and put `Refs #<N>` in the body (using `Closes #<N>` would make GitHub auto-close the Issue when a human merges the draft first, skipping ghswarm's post-merge CI gate; closing is always done explicitly by ghswarm).
   ```bash
   gh pr create --draft \
     --base <base_branch> --head <branch_prefix><N> \
     --title "<title> (#<N>)" \
     --body "Refs #<N>

   spec: \`<spec_dir>/<YYYY-MM-DD>-issue-<N>.md\`
   This is a spec-only draft PR for now. ghswarm will stack the implementation onto this branch."
   ```
   Note the PR number from the output URL. **Do not merge** (implementation commits will be stacked onto the same PR).
5. **Embed ghswarm's state metadata into the Issue body**. Append the following HTML comment to the end of the body and update via `gh issue edit <N> --body-file -`. `spec_path` is a **path relative to the repository root**. Put the **created spec PR** into `pr_url` / `pr_number`.

   ```
   <!-- GHSWARM_STATE_START
   {
     "phase": "spec_approved",
     "branch_name": "<branch_prefix><N>",
     "last_agent": null,
     "next_action": "start",
     "iteration": 0,
     "pending_questions": [],
     "spec_path": "<spec_dir>/<YYYY-MM-DD>-issue-<N>.md",
     "pr_url": "<spec PR URL>",
     "pr_number": <spec PR number>
   }
   GHSWARM_STATE_END -->
   ```
   **Always put links to the spec file and the spec PR at the top of the body** (not optional; do not omit). Put it on line 1 in the following form:

   ```
   spec: [`<spec_dir>/<YYYY-MM-DD>-issue-<N>.md`](<absolute URL>) / implementation PR: #<spec PR number>
   ```

   **The link target must always be an absolute URL (a `https://github.com/...` blob URL)**. Because the Issue body is displayed on github.com, a **relative path like `.specs/...` will 404** (relative links are not resolved in Issues). Since the spec is only on the work branch at this point, use `<branch_prefix><N>` for the branch part of the blob URL. Get the repo name with `gh repo view --json nameWithOwner -q .nameWithOwner` and assemble it:

   ```bash
   repo=$(gh repo view --json nameWithOwner -q .nameWithOwner)
   spec_url="https://github.com/$repo/blob/<branch_prefix><N>/<spec_dir>/<YYYY-MM-DD>-issue-<N>.md"
   ```

   (The display text can stay the repo-relative path, but the href inside `( )` must be the absolute URL above.)
6. **Finally, remove blocked to enable starting**. Only **at this point**, after finishing writing the state metadata including `spec_path`, remove blocked. idle and target were already applied in step 4-2, so removing blocked leaves only idle and makes it ready to start:

   ```bash
   gh issue edit <N> --remove-label "$blocked"
   ```

   (If `ghswarm config` was unavailable, `gh issue edit <N> --remove-label "status: blocked"`.) **Do not do this blocked removal before writing the state metadata** — that is the cause of the race.
7. **Return to the default branch**: `git switch <base_branch>`. Squatting on the work branch would fork the next spec filing.

### 5. Completion report and continuing development
Report the created Issue URL, the spec PR URL, and the spec file path (**do not enumerate commands**).

Then handle **whether to continue into implementation (Line 2) in this same session** with the judgment below. This is an optional continuation after spec filing, meant to let you see development through to merge in the same session.

**5-1. Determine externally whether loop is running** (pgrep check, no code change):

```bash
pgrep -fl "ghswarm loop"   # if anything shows, it's running (catches both foreground and daemon)
```

- **If running, no guidance or confirmation is needed**. The idle Issue #N you unblocked will be picked up automatically by loop on the next poll. Just say "loop is running so it will be started automatically" and finish.
  (Note: if only a loop that does not include the target repo is running via `-r other`, it won't be picked up. If it appears to be running but nothing starts, treat it as not running and proceed to 5-2.)
- **If not running**, confirm with the user "shall I continue development as-is (run `ghswarm run <N>` through to merge)?" **Get explicit consent.** Without consent, finish with just the completion report.

**5-2. If consent is obtained, run `run` in the background**. `ghswarm run <N>` has built-in CI polling and runs to completion at merge, so it **can block for several to a dozen-plus minutes**. Foreground execution hits the shell timeout, so **always run it in the background** (the Bash tool's `run_in_background`).

**`-r` is required in multi-repo environments**. `run`'s repo selection is decided solely by the number of registrations and **does not reference cwd** (unlike `config`, it does not auto-identify the current repo). With a single registration it auto-selects and `-r` is unneeded, but **with multiple registrations, omitting `-r` exits 2 with `ConfigError`**. Pass the alias you used in step 0 as `-r <ALIAS>`. Do not run a running loop and run simultaneously (they would fight over the same Issue via the label lock; only run when you confirmed non-running in 5-1).

After completion (the background task's exit notification), branch on the exit state:

- **done**: merged & closed. Report completion and let them know they can keep conversing in the same session.
- **awaiting clarification (`wait_for_clarification`)**: run exits normally partway through. Present the question from `.agent_question.md` or the Issue comment to the user, and once you have an answer, run `ghswarm run <N> --resume` **in the background again**.
- **blocked / failure (rc=1)**: check the reason (`ghswarm status` / Issue comment), report it, and prompt for human intervention.

**5-3. Progress tracking is primarily via run's stdout**. Reading the background run's log (each process() step's `Issue #N -> <action>: <detail>` line = implemented / wait_ci / merged / done ...) tracks progress. If asked to "monitor," **look at this stdout first** and report milestones (PR marked ready / entering CI wait / merge / done, plus blocked and awaiting-clarification).

The following is a supplementary external check (GitHub-side source-of-truth) **only when** run's stdout is not enough. Do not use routinely:

```bash
gh pr checks <PR number>    # details on which CI check failed (stdout only goes up to wait_ci/blocked)
ghswarm status -r <ALIAS>   # tasks done/total, current label state directly from GitHub (cross-check when run seems stuck)
```

Unless explicitly asked, do not monitor at all; just wait for the completion notification.

## Notes
- Always match `branch_name` to `<branch_prefix><N>` (the `branch_prefix` obtained in step 0 + number). **ghswarm searches for and takes over origin's spec PR branch by this name**, so a mismatch would cut a spec-less branch from default.
- Set `next_action` to `start` (ghswarm starts from implement).
- Leave the spec PR **as a draft**. ghswarm marks it ready with `gh pr ready` when review is complete, and merges after waiting for CI/approve.
- If a human merges the spec PR first, the implementation does not land on the same PR but splits into a separate new PR (the spec lands on default, so implementation itself can continue). If you want them in a single PR, **do not merge; leave it as a draft.**
- Do not write to or commit into `<spec_dir>/` before approval. Always keep the draft in `tmp/spec/`.
- Write the draft in the native language, and translate to `spec_lang` (the target repo's README language, determined in step 0) only if it differs from the draft language, when committing to `<spec_dir>/` (steps 1 and 4-3). After Writing a translated version, `rm -f` the native-language draft `tmp/spec/<slug>.md` so it does not remain in `tmp/spec/`.
- This skill focuses on the spec and task design and does not write code. Implementation is left to Line 2 (ghswarm).
