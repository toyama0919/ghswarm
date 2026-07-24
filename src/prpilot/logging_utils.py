"""Logging configuration."""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_logging(verbose: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger().setLevel(logging.DEBUG if verbose else logging.INFO)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class RepoLogAdapter(logging.LoggerAdapter):
    """Prefixes log lines with the repository alias during parallel execution."""

    def process(self, msg, kwargs):
        alias = self.extra.get("repo", "")
        prefix = f"[{alias}] " if alias else ""
        return f"{prefix}{msg}", kwargs


def get_repo_logger(alias: str, name: str = "prpilot.cli") -> RepoLogAdapter:
    return RepoLogAdapter(get_logger(name), {"repo": alias})
