"""GitHub Issue-driven development PM agent CLI.

Orchestrates multiple coding CLIs (claude / codex / cursor-agent) while using
GitHub Issue status labels for mutual exclusion. State is persisted in the Issue
body's metadata and checkboxes, so work can resume mid-way even if the process dies.
"""

__version__ = "0.1.0"
