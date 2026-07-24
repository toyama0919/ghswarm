"""State persistence into the Issue body and checkbox (task list) parsing.

State is embedded as JSON in an HTML comment at the end of the Issue body. It is
invisible to human readers and only the program reads it via a regex. Even if the
server restarts, reading the Issue fully restores how far work has progressed
(a Git-driven / Label-driven design).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

STATE_START = "<!-- PRPILOT_STATE_START"
STATE_END = "PRPILOT_STATE_END -->"
_STATE_RE = re.compile(
    re.escape(STATE_START) + r"\s*(.*?)\s*" + re.escape(STATE_END),
    re.DOTALL,
)

# Checkbox: "- [ ] task" / "- [x] task" (indentation allowed)
_TASK_RE = re.compile(r"^(?P<indent>\s*)-\s*\[(?P<mark>[ xX])\]\s*(?P<text>.+?)\s*$", re.MULTILINE)


@dataclass
class IssueState:
    phase: str = "initial"
    branch_name: str = ""
    last_agent: str | None = None
    # start / implement / ai_review / create_pr / wait_ci / verify_merge / done /
    # wait_for_clarification
    next_action: str = "start"
    iteration: int = 0
    pending_questions: list[str] = field(default_factory=list)
    spec_path: str = ""  # .specs/YYYY-MM-DD-issue-N.md (relative to the repo root)
    pr_url: str = ""
    pr_number: int = 0
    merge_commit_sha: str = ""  # sha of the merge commit whose CI is checked post-merge
    merged_at: str = ""  # time the merge was performed (ISO8601 UTC)
    # High-water mark of addressed PR review comments (ISO8601 UTC). Only reviews/
    # comments newer than this are treated as unaddressed (prevents an infinite
    # loop over the same feedback).
    last_review_addressed_at: str = ""
    # Cumulative transient-error retries across loops. Reset to 0 on CLI success.
    transient_retries: int = 0
    # Cumulative merge-conflict-resolution retries across loops. Reset to 0 on success.
    conflict_retries: int = 0
    # Cumulative CI-failure-fix retries across loops. Reset to 0 on a successful fix.
    ci_fix_retries: int = 0
    # Cumulative agent runs per Issue. Not reset even on success.
    total_agent_runs: int = 0
    # busy lock lease: owner "host:pid" and acquisition time (ISO8601 UTC)
    busy_owner: str = ""
    busy_since: str = ""
    # For deduplicating blocked notifications: the reason code most recently notified.
    # Cleared to None on release.
    last_notified_reason: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "IssueState":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)


def parse_state(body: str, issue_number: int, branch_prefix: str = "issue-") -> IssueState:
    m = _STATE_RE.search(body or "")
    if m:
        try:
            return IssueState.from_dict(json.loads(m.group(1)))
        except (json.JSONDecodeError, TypeError):
            pass
    return IssueState(branch_name=f"{branch_prefix}{issue_number}")


def write_state(body: str, state: IssueState) -> str:
    """Return the body with any existing state block removed and the latest state appended."""
    clean = _STATE_RE.sub("", body or "").rstrip()
    block = f"{STATE_START}\n{state.to_json()}\n{STATE_END}"
    return f"{clean}\n\n{block}\n"


def strip_state(body: str) -> str:
    """Return the "human-written body" portion with the state block stripped out."""
    return _STATE_RE.sub("", body or "").rstrip()


# -- Checkboxes --------------------------------------------------------------


@dataclass
class Task:
    text: str
    done: bool
    span: tuple[int, int]  # span of the whole "[ ]"/"[x]" marker line in the body
    mark_span: tuple[int, int]  # span of the "[ ]" portion


def parse_tasks(body: str) -> list[Task]:
    body = strip_state(body)
    tasks: list[Task] = []
    for m in _TASK_RE.finditer(body):
        mark = m.group("mark")
        # Compute the position of "["
        line_start = m.start()
        bracket = body.index("[", line_start)
        tasks.append(
            Task(
                text=m.group("text").strip(),
                done=mark.lower() == "x",
                span=(m.start(), m.end()),
                mark_span=(bracket, bracket + 3),
            )
        )
    return tasks


def next_unchecked(body: str) -> Task | None:
    for t in parse_tasks(body):
        if not t.done:
            return t
    return None


def unchecked(body: str) -> list[Task]:
    """Return all incomplete tasks in body order."""
    return [t for t in parse_tasks(body) if not t.done]


def check_tasks(body: str, tasks: list[Task]) -> str:
    """Return the body with multiple tasks marked "[x]" at once.

    Since "[ ]" and "[x]" have the same width, rewriting does not shift the spans of
    other tasks. body must be the same string the tasks were parsed from.
    """
    for t in tasks:
        body = check_task(body, t)
    return body


def check_task(body: str, task: Task) -> str:
    """Return the body with the given task's "[ ]" rewritten to "[x]".

    Note: body must be the same string the task was parsed from (spans must be valid).
    """
    start, end = task.mark_span
    return body[:start] + "[x]" + body[end:]


def progress(body: str) -> tuple[int, int]:
    tasks = parse_tasks(body)
    done = sum(1 for t in tasks if t.done)
    return done, len(tasks)
