"""YAML frontmatter parsing for spec files (.specs/*.md).

A spec may begin with optional YAML frontmatter (delimited by ``---``): machine-readable
metadata placed before the human-readable body. Currently supported keys:

  verify: The local verification steps for this task. Two shapes are accepted:

    - Legacy form: a string, or a list of strings. If a list, each element is wrapped
      in a subshell and joined with " && " into a single shell command (the string form
      is not wrapped). Normalized into a single step with no `path` (its execution
      environment is config's `verify` sandbox if in single form, else `driver: none`).

    - New form: a list of mappings, each with both `path` and `command` (both
      required). Each becomes its own step; `path` is matched against config's
      `verify` (when in list form) to resolve its execution environment, falling back
      to `driver: none` when no entry matches. Cannot be mixed with the legacy form.

  If `verify` is unspecified (or an empty list), there are zero steps and local
  verification is skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from .config import ConfigError, validate_verify_path

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---[ \t]*\n?", re.DOTALL)

# Header used for a legacy (path-less) step's output when concatenating verify logs.
ROOT_STEP_LABEL = "(root)"


@dataclass
class VerifyStep:
    """A single verify step: a shell command, optionally scoped to a subdirectory."""

    path: str | None
    command: str


@dataclass
class Spec:
    meta: dict = field(default_factory=dict)
    body: str = ""  # body with frontmatter removed (the human-written spec)

    @property
    def verify_steps(self) -> list[VerifyStep]:
        """Normalize the frontmatter's verify into a list of VerifyStep.

        Raises ConfigError if the new form is missing 'path'/'command' on any entry,
        or if the legacy and new forms are mixed within the same verify:.
        """
        v = self.meta.get("verify")
        if not v:
            return []

        if isinstance(v, str):
            command = v.strip()
            return [VerifyStep(path=None, command=command)] if command else []

        if isinstance(v, (list, tuple)):
            if not v:
                return []
            if all(isinstance(x, str) for x in v):
                parts = [str(x).strip() for x in v if str(x).strip()]
                if not parts:
                    return []
                command = " && ".join(f"({p})" for p in parts)
                return [VerifyStep(path=None, command=command)]
            if all(isinstance(x, dict) for x in v):
                steps: list[VerifyStep] = []
                for i, item in enumerate(v):
                    if "path" not in item or "command" not in item:
                        raise ConfigError(
                            f"spec verify[{i}]: the new form requires both 'path' and "
                            f"'command' on every entry."
                        )
                    path = validate_verify_path(item["path"], f"spec verify[{i}].path")
                    command = str(item["command"]).strip()
                    if not command:
                        raise ConfigError(f"spec verify[{i}].command must not be empty.")
                    steps.append(VerifyStep(path=path, command=command))
                return steps
            raise ConfigError(
                "spec verify: cannot mix the legacy form (a string, or a list of "
                "strings) with the new form (a list of {path, command} mappings) "
                "within the same verify:."
            )

        raise ConfigError(
            "spec verify: must be a string, a list of strings (legacy form), or a "
            "list of {path, command} mappings (new form)."
        )


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
