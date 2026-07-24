"""Detection of the clarification signal file (.agent_question.md).

The coding CLI's system prompt instructs it to "write .agent_question.md in the
working directory root and exit if the spec is unclear." After the CLI runs, we
detect "question mode" simply by checking whether this file exists, with no need to
parse stdout in real time.
"""

from __future__ import annotations

from pathlib import Path

from .logging_utils import get_logger

log = get_logger("prpilot.questions")


def question_prompt_hint(question_file: str) -> str:
    """Clarification rule appended to the prompt passed to the coding CLI."""
    return (
        "\n\n[Clarification rules]\n"
        "Before you start implementing, first try to resolve any ambiguity yourself by "
        "consulting docs/, the README, existing tests, and related code in the repository.\n"
        "If the spec still cannot be pinned down and you should not proceed on a guess, do "
        "not implement based on speculation. Instead, create "
        f"`{question_file}` in the working directory root in the following format, then exit.\n"
        "---\n"
        "[Background]: why clarification is needed\n"
        "[Options]: the candidate approaches and the pros/cons of each\n"
        "[Recommendation]: your recommended option and the reasoning behind it\n"
        "---\n"
    )


def check_question_file(cwd: str, question_file: str) -> str | None:
    """If the question file exists, return its contents and delete it. Otherwise None."""
    path = Path(cwd) / question_file
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8").strip()
    try:
        path.unlink()
    except OSError as e:
        log.warning("Failed to delete question file: %s", e)
    log.info("Detected clarification signal: %s", question_file)
    return content
