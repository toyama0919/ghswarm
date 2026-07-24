"""YAML frontmatter parsing for spec files (.specs/*.md).

A spec may begin with optional YAML frontmatter (delimited by ``---``): machine-readable
metadata placed before the human-readable body. Currently supported keys:

  verify: The local verification command for this task. A string or a list of strings.
          If a list, each element is wrapped in a subshell and joined with " && " into a
          single shell command. Not tied to any particular language or tool (the real
          tests run in CI). If unspecified, falls back to config's test_command; if that
          is also empty, verification is skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---[ \t]*\n?", re.DOTALL)


@dataclass
class Spec:
    meta: dict = field(default_factory=dict)
    body: str = ""  # body with frontmatter removed (the human-written spec)

    @property
    def verify_command(self) -> str:
        """Normalize the frontmatter's verify into a single shell command."""
        v = self.meta.get("verify")
        if not v:
            return ""
        if isinstance(v, (list, tuple)):
            parts = [str(x).strip() for x in v if str(x).strip()]
            return " && ".join(f"({p})" for p in parts)
        return str(v).strip()


def parse_spec(content: str) -> Spec:
    """Split a spec's text into frontmatter (meta) and body.

    If the frontmatter is missing or malformed, meta is left empty and the entire
    text becomes the body.
    """
    text = content or ""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return Spec(meta={}, body=text)
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return Spec(meta=meta, body=text[m.end() :])
