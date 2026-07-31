---
name: ghswarm-requirements
description: Line 0 (pre-spec) consultation for deciding system requirements through dialogue — clarifies the problem, goals, non-goals, constraints, and success criteria; surfaces hidden assumptions; lays out design options with tradeoffs when more than one reasonable approach exists. Writes no files, creates no Issues, touches no git state — output is a plain-language recap in chat. Once the user confirms the recap, hands off to ghswarm-spec to draft the actual spec. Use for deciding what to build, requirements gathering, requirements consultation, thinking through a design before writing it down, evaluating options/tradeoffs before filing a spec.
---

# ghswarm-requirements (Line 0: requirements consultation)

A skill for **thinking through system requirements with a human before anything gets written down**. It exists because `ghswarm-spec` already assumes a reasonably settled feature request as its input (step 0: "confirm the user's request... ask if unclear") — this skill is what happens *before* that, when the request is still vague, under-specified, or has more than one reasonable shape.

- **Line 0 (this skill)**: dialogue only — clarify the problem, goals, constraints, and options. No files, no Issues, no git.
- **Line 1 ([`ghswarm-spec`](../ghswarm-spec/SKILL.md))**: drafts into `tmp/spec/` → human review → independent review → Issue with `GHSWARM_VERIFY`.
- **Line 2 (ghswarm daemon)**: cut branch → implement → review → create PR → auto-merge.

This skill produces exactly one artifact: **a plain-language recap in the conversation**, confirmed by the user. It never writes `tmp/spec/`, never creates or edits an Issue/PR, and never runs git commands. If you find yourself about to draft a spec file or file an Issue, you have moved into `ghswarm-spec`'s territory — hand off instead of continuing here.

## When to use

- The user has a feature idea, problem, or complaint that isn't yet specific enough to draft (`ghswarm-spec` would have to guess or ask the same clarifying questions anyway).
- There's a source Issue/ticket that's rough or ambiguous, and it's worth talking through what's actually needed before anyone drafts it.
- There's more than one reasonable way to solve the problem and the choice matters (architecture, scope, UX), and nobody has decided yet.

Skip this skill and go straight to `ghswarm-spec` when the request is already concrete (clear problem, clear scope, one obvious approach) — don't force a consultation step where there's nothing to consult about.

## Steps

### 0. Understand the request

Ask what problem or feature is being considered. If a source Issue, PR, doc, or chat YAML is referenced, read enough of it to have real context (`gh issue view`, relevant existing code, README) — but keep this light. The point of this skill is dialogue with the human, not research depth; deep repo investigation belongs to `ghswarm-spec`'s drafting step.

### 1. Probe the requirement

Work through the load-bearing unknowns **conversationally, a few at a time** — don't dump a questionnaire. Prioritize whichever 2-4 unknowns most change the shape of the eventual spec:

- **Problem**: what's broken or missing today, for whom, and how do we know.
- **Goal / non-goal**: what this must achieve, and what's explicitly out of scope.
- **Users / callers**: who or what invokes this, and how.
- **Constraints**: existing architecture, performance, compatibility, deadlines.
- **Success criteria**: what observable outcome means "done" — this should be concrete enough that `ghswarm-spec` could later turn it into a `verify` command or acceptance criterion.
- **Alternatives already considered and rejected**, if any.

Push back on vague requests instead of rubber-stamping them ("make it faster" → faster by what measure, measured how, what's an acceptable tradeoff). A requirement that can't eventually be verified is not yet a requirement — say so.

### 2. Surface design options when real tradeoffs exist

When more than one reasonable approach exists, don't quietly pick one — lay out 2-3 candidates with the core tradeoff each. Use the `AskUserQuestion` tool when the choice materially changes scope or architecture. Give a recommendation, but let the user decide; this mirrors how exploratory questions are handled generally — 2-3 sentences plus a recommendation, not an exhaustive survey.

### 3. Recap and confirm

Summarize the settled requirement **directly in chat, not in a file**:

- Background / purpose
- Requirements (what must be true)
- Explicit non-goals
- Chosen approach and why (if an option was picked in step 2)

Ask the user to confirm this matches what they want. Iterate until they do. This recap is deliberately the same shape as `ghswarm-spec`'s draft sections (Background/Purpose, Requirements, Design approach) so it hands off cleanly.

### 4. Hand off to ghswarm-spec

Once confirmed, ask whether to proceed to drafting now. If yes, invoke the `ghswarm-spec` skill (via the Skill tool) and pass it **the confirmed recap** as the feature-request material for its own step 0/1 — do not pass the full back-and-forth dialogue, only the settled recap (same reason `ghswarm-spec`'s independent review only gets the spec file: passing your own reasoning trail biases what should be a fresh read). Do not perform any of `ghswarm-spec`'s steps yourself (no `tmp/spec/` draft, no Issue, no PR) — that skill owns everything from drafting onward.

If the user wants to keep talking, isn't ready, or the requirement genuinely needs more time, don't push into `ghswarm-spec` — staying in consultation is a valid outcome of this skill.

## Out of scope (things not to do)

- Writing `tmp/spec/` files (that is `ghswarm-spec`'s job after handoff).
- Creating or editing GitHub Issues or PRs.
- Any git operations (branching, committing, pushing).
- Implementation of any kind.
- Deep, exhaustive repository investigation (light context-gathering only — leave the thorough investigation to `ghswarm-spec`'s drafting and independent-review steps).
