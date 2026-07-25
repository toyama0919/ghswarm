"""Tests for spec front-matter parsing."""

from __future__ import annotations

import pytest

from ghswarm.config import ConfigError
from ghswarm.spec import Spec, VerifyStep, parse_spec


def test_verify_as_list_is_joined_with_and():
    spec = parse_spec(
        "---\nverify:\n  - pip install -e .\n  - ghswarm --help\n---\n# Title\nBody\n"
    )
    assert spec.verify_steps == [
        VerifyStep(path=None, command="(pip install -e .) && (ghswarm --help)")
    ]
    assert spec.body.startswith("# Title")
    assert "verify" not in spec.body  # front matter is stripped from the body


def test_verify_as_string():
    spec = parse_spec("---\nverify: make test\n---\n# x\n")
    assert spec.verify_steps == [VerifyStep(path=None, command="make test")]


def test_no_frontmatter_keeps_full_body_and_empty_verify():
    content = "# Title only\nBody only\n"
    spec = parse_spec(content)
    assert spec.meta == {}
    assert spec.verify_steps == []
    assert spec.body == content


def test_missing_verify_key_yields_empty_steps():
    spec = parse_spec("---\ntitle: foo\n---\n# x\n")
    assert spec.verify_steps == []
    assert spec.meta == {"title": "foo"}


def test_empty_and_none_input():
    assert parse_spec("").verify_steps == []
    assert parse_spec(None).verify_steps == []  # type: ignore[arg-type]


def test_malformed_yaml_is_tolerated():
    # even if the YAML in the front-matter region is broken, no exception is raised and meta stays empty
    spec = parse_spec("---\nverify: [unclosed\n---\n# x\n")
    assert spec.meta == {}
    assert spec.verify_steps == []


def test_non_dict_frontmatter_is_ignored():
    # when the front matter is not a dict (e.g. a list), treat meta as empty
    spec = parse_spec("---\n- a\n- b\n---\n# x\n")
    assert spec.meta == {}


def test_list_verify_drops_blank_entries():
    spec = parse_spec("---\nverify:\n  - ''\n  - echo ok\n  - '  '\n---\n# x\n")
    assert spec.verify_steps == [VerifyStep(path=None, command="(echo ok)")]


def test_spec_defaults():
    s = Spec()
    assert s.meta == {}
    assert s.body == ""
    assert s.verify_steps == []


def test_list_verify_wraps_each_element_in_subshell():
    spec = parse_spec(
        "---\nverify:\n  - cd packages/api && npm test\n  - cd packages/web && npm test\n---\n# x\n"
    )
    assert spec.verify_steps == [
        VerifyStep(
            path=None,
            command="(cd packages/api && npm test) && (cd packages/web && npm test)",
        )
    ]


def test_list_verify_preserves_and_short_circuit():
    spec = parse_spec("---\nverify:\n  - exit 1\n  - echo should not run\n---\n# x\n")
    assert spec.verify_steps == [VerifyStep(path=None, command="(exit 1) && (echo should not run)")]


def test_string_verify_is_not_wrapped_in_subshell():
    spec = parse_spec("---\nverify: cd packages/api && npm test\n---\n# x\n")
    assert spec.verify_steps == [VerifyStep(path=None, command="cd packages/api && npm test")]


# -- new form: a list of {path, command} mappings ---------------------------


def test_new_form_returns_one_step_per_entry():
    spec = parse_spec(
        "---\n"
        "verify:\n"
        "  - path: terraform\n"
        "    command: terraform validate\n"
        "  - path: backend\n"
        "    command: pytest\n"
        "---\n# x\n"
    )
    assert spec.verify_steps == [
        VerifyStep(path="terraform", command="terraform validate"),
        VerifyStep(path="backend", command="pytest"),
    ]


def test_new_form_single_entry():
    spec = parse_spec("---\nverify:\n  - path: backend\n    command: pytest -q\n---\n# x\n")
    assert spec.verify_steps == [VerifyStep(path="backend", command="pytest -q")]


def test_new_form_normalizes_path():
    spec = parse_spec("---\nverify:\n  - path: ./backend/\n    command: pytest\n---\n# x\n")
    assert spec.verify_steps == [VerifyStep(path="backend", command="pytest")]


def test_new_form_missing_command_raises_config_error():
    spec = parse_spec("---\nverify:\n  - path: backend\n---\n# x\n")
    with pytest.raises(ConfigError, match="path.*command|command.*path"):
        spec.verify_steps


def test_new_form_missing_path_raises_config_error():
    spec = parse_spec("---\nverify:\n  - command: pytest\n---\n# x\n")
    with pytest.raises(ConfigError, match="path.*command|command.*path"):
        spec.verify_steps


def test_new_form_empty_command_raises_config_error():
    spec = parse_spec("---\nverify:\n  - path: backend\n    command: '  '\n---\n# x\n")
    with pytest.raises(ConfigError, match="command"):
        spec.verify_steps


def test_new_form_rejects_absolute_path():
    spec = parse_spec("---\nverify:\n  - path: /etc\n    command: pytest\n---\n# x\n")
    with pytest.raises(ConfigError, match="absolute path"):
        spec.verify_steps


def test_new_form_rejects_dotdot_path():
    spec = parse_spec("---\nverify:\n  - path: ../outside\n    command: pytest\n---\n# x\n")
    with pytest.raises(ConfigError, match=r"\.\."):
        spec.verify_steps


def test_new_form_rejects_empty_path():
    spec = parse_spec("---\nverify:\n  - path: ''\n    command: pytest\n---\n# x\n")
    with pytest.raises(ConfigError, match="path"):
        spec.verify_steps


# -- mixing the legacy form and the new form is an error ---------------------


def test_mixing_legacy_and_new_forms_raises_config_error():
    spec = parse_spec(
        "---\nverify:\n  - echo ok\n  - path: backend\n    command: pytest\n---\n# x\n"
    )
    with pytest.raises(ConfigError, match="mix"):
        spec.verify_steps
