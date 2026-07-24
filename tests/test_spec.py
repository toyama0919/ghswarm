"""Tests for spec front-matter parsing."""

from __future__ import annotations

from ghswarm.spec import Spec, parse_spec


def test_verify_as_list_is_joined_with_and():
    spec = parse_spec(
        "---\nverify:\n  - pip install -e .\n  - ghswarm --help\n---\n# Title\nBody\n"
    )
    assert spec.verify_command == "(pip install -e .) && (ghswarm --help)"
    assert spec.body.startswith("# Title")
    assert "verify" not in spec.body  # front matter is stripped from the body


def test_verify_as_string():
    spec = parse_spec("---\nverify: make test\n---\n# x\n")
    assert spec.verify_command == "make test"


def test_no_frontmatter_keeps_full_body_and_empty_verify():
    content = "# Title only\nBody only\n"
    spec = parse_spec(content)
    assert spec.meta == {}
    assert spec.verify_command == ""
    assert spec.body == content


def test_missing_verify_key_yields_empty_command():
    spec = parse_spec("---\ntitle: foo\n---\n# x\n")
    assert spec.verify_command == ""
    assert spec.meta == {"title": "foo"}


def test_empty_and_none_input():
    assert parse_spec("").verify_command == ""
    assert parse_spec(None).verify_command == ""  # type: ignore[arg-type]


def test_malformed_yaml_is_tolerated():
    # even if the YAML in the front-matter region is broken, no exception is raised and meta stays empty
    spec = parse_spec("---\nverify: [unclosed\n---\n# x\n")
    assert spec.meta == {}
    assert spec.verify_command == ""


def test_non_dict_frontmatter_is_ignored():
    # when the front matter is not a dict (e.g. a list), treat meta as empty
    spec = parse_spec("---\n- a\n- b\n---\n# x\n")
    assert spec.meta == {}


def test_list_verify_drops_blank_entries():
    spec = parse_spec("---\nverify:\n  - ''\n  - echo ok\n  - '  '\n---\n# x\n")
    assert spec.verify_command == "(echo ok)"


def test_spec_defaults():
    s = Spec()
    assert s.meta == {}
    assert s.body == ""
    assert s.verify_command == ""


def test_list_verify_wraps_each_element_in_subshell():
    spec = parse_spec(
        "---\nverify:\n  - cd packages/api && npm test\n  - cd packages/web && npm test\n---\n# x\n"
    )
    assert spec.verify_command == "(cd packages/api && npm test) && (cd packages/web && npm test)"


def test_list_verify_preserves_and_short_circuit():
    spec = parse_spec("---\nverify:\n  - exit 1\n  - echo should not run\n---\n# x\n")
    assert spec.verify_command == "(exit 1) && (echo should not run)"


def test_string_verify_is_not_wrapped_in_subshell():
    spec = parse_spec("---\nverify: cd packages/api && npm test\n---\n# x\n")
    assert spec.verify_command == "cd packages/api && npm test"
