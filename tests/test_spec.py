"""Tests for verify-step normalization from Issue metadata."""

from __future__ import annotations

import pytest

from ghswarm.config import ConfigError
from ghswarm.spec import Spec, VerifyStep


def test_verify_as_list_is_joined_with_and():
    spec = Spec(meta={"verify": ["pip install -e .", "ghswarm --help"]})
    assert spec.verify_steps == [
        VerifyStep(path=None, command="(pip install -e .) && (ghswarm --help)")
    ]


def test_verify_as_string():
    spec = Spec(meta={"verify": "make test"})
    assert spec.verify_steps == [VerifyStep(path=None, command="make test")]


def test_empty_meta_yields_empty_verify():
    assert Spec(meta={}).verify_steps == []
    assert Spec().verify_steps == []


def test_missing_verify_key_yields_empty_steps():
    spec = Spec(meta={"title": "foo"})
    assert spec.verify_steps == []
    assert spec.meta == {"title": "foo"}


def test_list_verify_drops_blank_entries():
    spec = Spec(meta={"verify": ["", "echo ok", "  "]})
    assert spec.verify_steps == [VerifyStep(path=None, command="(echo ok)")]


def test_spec_defaults():
    s = Spec()
    assert s.meta == {}
    assert s.verify_steps == []


def test_list_verify_wraps_each_element_in_subshell():
    spec = Spec(
        meta={
            "verify": [
                "cd packages/api && npm test",
                "cd packages/web && npm test",
            ]
        }
    )
    assert spec.verify_steps == [
        VerifyStep(
            path=None,
            command="(cd packages/api && npm test) && (cd packages/web && npm test)",
        )
    ]


def test_list_verify_preserves_and_short_circuit():
    spec = Spec(meta={"verify": ["exit 1", "echo should not run"]})
    assert spec.verify_steps == [VerifyStep(path=None, command="(exit 1) && (echo should not run)")]


def test_string_verify_is_not_wrapped_in_subshell():
    spec = Spec(meta={"verify": "cd packages/api && npm test"})
    assert spec.verify_steps == [VerifyStep(path=None, command="cd packages/api && npm test")]


# -- new form: a list of {path, command} mappings ---------------------------


def test_new_form_returns_one_step_per_entry():
    spec = Spec(
        meta={
            "verify": [
                {"path": "terraform", "command": "terraform validate"},
                {"path": "backend", "command": "pytest"},
            ]
        }
    )
    assert spec.verify_steps == [
        VerifyStep(path="terraform", command="terraform validate"),
        VerifyStep(path="backend", command="pytest"),
    ]


def test_new_form_single_entry():
    spec = Spec(meta={"verify": [{"path": "backend", "command": "pytest -q"}]})
    assert spec.verify_steps == [VerifyStep(path="backend", command="pytest -q")]


def test_new_form_normalizes_path():
    spec = Spec(meta={"verify": [{"path": "./backend/", "command": "pytest"}]})
    assert spec.verify_steps == [VerifyStep(path="backend", command="pytest")]


def test_new_form_missing_command_raises_config_error():
    spec = Spec(meta={"verify": [{"path": "backend"}]})
    with pytest.raises(ConfigError, match="path.*command|command.*path"):
        spec.verify_steps


def test_new_form_missing_path_raises_config_error():
    spec = Spec(meta={"verify": [{"command": "pytest"}]})
    with pytest.raises(ConfigError, match="path.*command|command.*path"):
        spec.verify_steps


def test_new_form_empty_command_raises_config_error():
    spec = Spec(meta={"verify": [{"path": "backend", "command": "  "}]})
    with pytest.raises(ConfigError, match="command"):
        spec.verify_steps


def test_new_form_rejects_absolute_path():
    spec = Spec(meta={"verify": [{"path": "/etc", "command": "pytest"}]})
    with pytest.raises(ConfigError, match="absolute path"):
        spec.verify_steps


def test_new_form_rejects_dotdot_path():
    spec = Spec(meta={"verify": [{"path": "../outside", "command": "pytest"}]})
    with pytest.raises(ConfigError, match=r"\.\."):
        spec.verify_steps


def test_new_form_rejects_empty_path():
    spec = Spec(meta={"verify": [{"path": "", "command": "pytest"}]})
    with pytest.raises(ConfigError, match="path"):
        spec.verify_steps


# -- mixing the legacy form and the new form is an error ---------------------


def test_mixing_legacy_and_new_forms_raises_config_error():
    spec = Spec(meta={"verify": ["echo ok", {"path": "backend", "command": "pytest"}]})
    with pytest.raises(ConfigError, match="mix"):
        spec.verify_steps
