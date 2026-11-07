"""Dependency metadata parsing and resolution for Issue start gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


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


def resolve(number: int, direct: list[int], fetch: Callable[[int], DepNode]) -> Verdict:
    """Resolve an Issue's direct dependencies using the injected ``fetch`` callable.

    Only direct dependency failures make the Issue invalid. Open dependency nodes
    are traversed to find cycles, while closed nodes terminate the traversal. Other
    transitive failures are deliberately ignored because those Issues report their
    own invalid metadata when they are evaluated.
    """
    nodes: dict[int, DepNode] = {}
    visiting: set[int] = set()
    visited: set[int] = set()
    fetched_count = 0

    def get_node(dep_number: int) -> DepNode | None:
        nonlocal fetched_count
        if dep_number in nodes:
            return nodes[dep_number]
        fetched_count += 1
        if fetched_count > 50:
            return None
        node = fetch(dep_number)
        nodes[dep_number] = node
        return node

    def walk(dep_number: int) -> str | None:
        """Return a dependency graph error, if an open graph cannot be walked."""
        if dep_number in visiting:
            return "cycle"
        if dep_number in visited:
            return None

        node = get_node(dep_number)
        if node is None:
            return "too_large"
        if node.status != "open":
            visited.add(dep_number)
            return None

        visiting.add(dep_number)
        for child in node.depends_on:
            error = walk(child)
            if error:
                return error
        visiting.remove(dep_number)
        visited.add(dep_number)
        return None

    waiting_on: list[int] = []
    for dep_number in direct:
        if dep_number == number:
            return Verdict("invalid", [], f"Issue #{number} depends on itself")

        node = get_node(dep_number)
        if node is None:
            return Verdict("invalid", [], "dependency graph too large (>50 nodes)")
        if node.status in {"missing", "pull_request", "not_planned", "unparsable"}:
            return Verdict("invalid", [], f"dependency #{dep_number} is {node.status}")
        if node.status == "open":
            waiting_on.append(dep_number)

    for dep_number in waiting_on:
        error = walk(dep_number)
        if error == "too_large":
            return Verdict("invalid", [], "dependency graph too large (>50 nodes)")
        if error == "cycle":
            return Verdict("invalid", [], f"dependency cycle involving #{dep_number}")

    if waiting_on:
        return Verdict("waiting", waiting_on, "")
    return Verdict("ready", [], "")
