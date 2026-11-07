"""Dependency metadata parsing and resolution for Issue start gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal


_INVALID_DEPENDENCY_STATUSES = frozenset({"missing", "pull_request", "not_planned", "unparsable"})
_INVALID_DEPENDS_ON = "depends_on must be a list of positive Issue numbers"

DependencyStatus = Literal["closed", "missing", "open", "pull_request", "not_planned", "unparsable"]
VerdictKind = Literal["invalid", "waiting", "ready"]


class DependsOnError(ValueError):
    """Raised when the ``depends_on`` metadata is not a list of Issue numbers."""


@dataclass
class DepNode:
    """The dependency metadata needed to inspect one Issue."""

    status: DependencyStatus
    depends_on: list[int]


@dataclass
class Verdict:
    """The result of resolving an Issue's direct dependencies."""

    kind: VerdictKind
    waiting_on: list[int]
    detail: str


def parse_depends_on(meta: dict) -> list[int]:
    """Return normalized dependency numbers from parsed verify metadata.

    A missing or empty value means that the Issue has no dependencies. All present
    values must be a list containing positive integers; booleans are excluded even
    though ``bool`` is an ``int`` subclass in Python.
    """
    if "depends_on" not in meta:
        return []
    value = meta["depends_on"]
    if not isinstance(value, list):
        raise DependsOnError(_INVALID_DEPENDS_ON)

    numbers: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise DependsOnError(_INVALID_DEPENDS_ON)
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

    def get_node(dep_number: int) -> DepNode:
        if dep_number in nodes:
            return nodes[dep_number]
        node = fetch(dep_number)
        nodes[dep_number] = node
        return node

    def walk(dep_number: int) -> str | None:
        """Return a dependency graph error, if an open graph cannot be walked."""
        # Use an explicit stack so a valid but deep dependency chain cannot hit
        # Python's recursion limit. There is no graph-size limit in the Issue spec.
        stack: list[tuple[int, bool]] = [(dep_number, False)]
        while stack:
            current, exiting = stack.pop()
            if exiting:
                visiting.remove(current)
                visited.add(current)
                continue
            if current in visiting:
                return "cycle"
            if current in visited:
                continue

            node = get_node(current)
            if node.status != "open":
                visited.add(current)
                continue

            visiting.add(current)
            stack.append((current, True))
            stack.extend((child, False) for child in reversed(node.depends_on))
        return None

    waiting_on: list[int] = []
    for dep_number in direct:
        if dep_number == number:
            return Verdict("invalid", [], f"Issue #{number} depends on itself")

        node = get_node(dep_number)
        if node.status in _INVALID_DEPENDENCY_STATUSES:
            return Verdict("invalid", [], f"dependency #{dep_number} is {node.status}")
        if node.status == "open":
            waiting_on.append(dep_number)

    for dep_number in waiting_on:
        error = walk(dep_number)
        if error == "cycle":
            return Verdict("invalid", [], f"dependency cycle involving #{dep_number}")

    if waiting_on:
        return Verdict("waiting", waiting_on, "")
    return Verdict("ready", [], "")
