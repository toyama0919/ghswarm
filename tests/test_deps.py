"""Tests for Issue dependency metadata parsing and resolution."""

from __future__ import annotations

import pytest

from ghswarm.deps import DependsOnError, parse_depends_on


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
