# Agent Guidelines

Guidance for AI agents (Claude Code and others) working in this repository.

## Language policy

**Always write GitHub Issues and Pull Requests in English.** This includes:

- Issue titles and bodies
- Pull Request titles and bodies
- Commit messages
- Code comments and docstrings

This is a public repository, so all repository-facing artifacts must be in English regardless of the language used in the working conversation. Chatting with the user in another language is fine, but anything published to GitHub is English.

## Project overview

`prpilot` is a spec-driven development PM agent that orchestrates multiple coding CLIs, using GitHub Issues as state.

- Source: `src/prpilot/`
- Tests: `tests/`
- Entry point: `prpilot = "prpilot.cli:main"`

## Development

- Python `>=3.10`, managed with `uv`.
- Install dev deps: `uv sync --extra dev`
- Run tests: `uv run pytest`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`

## Conventions

- Keep changes focused; do not commit or push unless asked.
- Do not work directly on `main`; create a branch first.
- Match the style of surrounding code.
