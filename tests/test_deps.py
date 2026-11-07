"""Tests for Issue dependency metadata parsing and resolution."""

from __future__ import annotations

import pytest

from ghswarm.deps import DepNode, DependsOnError, parse_depends_on, resolve


def test_parse_depends_on_missing_null_and_empty_values():
    assert parse_depends_on({}) == []
    assert parse_depends_on({"depends_on": None}) == []
    assert parse_depends_on({"depends_on": []}) == []


def test_parse_depends_on_valid_values_are_deduplicated_and_sorted():
    assert parse_depends_on({"depends_on": [12, 10, 12]}) == [10, 12]


@pytest.mark.parametrize(
    "value",
    [
        10,
        "10",
        "owner/repo#12",
        [True],
        [0],
        [-1],
        [10, "12"],
    ],
)
def test_parse_depends_on_rejects_invalid_values(value):
    with pytest.raises(DependsOnError):
        parse_depends_on({"depends_on": value})


def _resolver(nodes: dict[int, DepNode]):
    def fetch(number: int) -> DepNode:
        return nodes[number]

    return fetch


def test_resolve_is_ready_when_all_direct_dependencies_are_closed():
    verdict = resolve(
        11,
        [10, 12],
        _resolver({10: DepNode("closed", []), 12: DepNode("closed", [])}),
    )
    assert verdict.kind == "ready"
    assert verdict.waiting_on == []


def test_resolve_waits_only_on_open_direct_dependencies():
    verdict = resolve(
        11,
        [10, 12],
        _resolver({10: DepNode("open", []), 12: DepNode("closed", [])}),
    )
    assert verdict.kind == "waiting"
    assert verdict.waiting_on == [10]


def test_resolve_rejects_self_reference_without_fetching_it():
    fetched: list[int] = []

    def fetch(number: int) -> DepNode:
        fetched.append(number)
        raise AssertionError("self-reference should not be fetched")

    verdict = resolve(11, [11], fetch)
    assert verdict.kind == "invalid"
    assert fetched == []


@pytest.mark.parametrize("status", ["missing", "pull_request", "not_planned", "unparsable"])
def test_resolve_rejects_invalid_direct_dependency_statuses(status):
    verdict = resolve(11, [10], _resolver({10: DepNode(status, [])}))
    assert verdict.kind == "invalid"


def test_resolve_treats_closed_dependency_with_null_state_reason_as_completed():
    # GitHub's null state_reason is represented by the normalized "closed" status.
    verdict = resolve(11, [10], _resolver({10: DepNode("closed", [])}))
    assert verdict.kind == "ready"


def test_resolve_rejects_a_cycle_returning_to_the_starting_issue():
    nodes = {
        10: DepNode("open", [11]),
        11: DepNode("open", [42]),
        42: DepNode("open", [10]),
    }
    verdict = resolve(42, [10], _resolver(nodes))
    assert verdict.kind == "invalid"
    assert "cycle" in verdict.detail


def test_resolve_does_not_detect_a_cycle_through_a_closed_node():
    nodes = {
        10: DepNode("open", [11]),
        11: DepNode("closed", [10]),
    }
    verdict = resolve(42, [10], _resolver(nodes))
    assert verdict.kind == "waiting"
    assert verdict.waiting_on == [10]


def test_resolve_ignores_invalid_nodes_found_only_transitively():
    nodes = {
        10: DepNode("open", [11]),
        11: DepNode("missing", []),
    }
    verdict = resolve(42, [10], _resolver(nodes))
    assert verdict.kind == "waiting"
    assert verdict.waiting_on == [10]


def test_resolve_rejects_graphs_larger_than_50_nodes():
    nodes = {number: DepNode("open", [number + 1]) for number in range(1, 52)}
    nodes[52] = DepNode("closed", [])
    verdict = resolve(999, [1], _resolver(nodes))
    assert verdict.kind == "invalid"
    assert verdict.detail == "dependency graph too large (>50 nodes)"
