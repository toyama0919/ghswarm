"""Dependency metadata parsing and resolution for Issue start gates."""

from __future__ import annotations

from dataclasses import dataclass


class DependsOnError(ValueError):
    """Raised when the ``depends_on`` metadata is not a list of Issue numbers."""


@dataclass
class DepNode:
    """The dependency metadata needed to inspect one Issue."""

    status: str
    depends_on: list[int]


@dataclass
class Verdict:
    """The result of resolving an Issue's direct dependencies."""

    kind: str
    waiting_on: list[int]
    detail: str


def parse_depends_on(meta: dict) -> list[int]:
    """Return normalized dependency numbers from parsed verify metadata.

    A missing, null, or empty value means that the Issue has no dependencies.  All
    other values must be a list containing positive integers; booleans are excluded
    even though ``bool`` is an ``int`` subclass in Python.
    """
    value = meta.get("depends_on")
    if value is None:
        return []
    if not isinstance(value, list):
        raise DependsOnError("depends_on must be a list of positive Issue numbers")

    numbers: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise DependsOnError("depends_on must be a list of positive Issue numbers")
        numbers.add(item)
    return sorted(numbers)

