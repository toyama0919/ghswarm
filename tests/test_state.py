"""Tests for persisting state into the Issue body and parsing the task list."""

from __future__ import annotations

from ghswarm import state as st


def test_parse_state_default_when_no_block():
    s = st.parse_state("body only", 42, branch_prefix="issue-")
    assert s.branch_name == "issue-42"
    assert s.next_action == "start"


def test_write_then_parse_roundtrip():
    original = st.IssueState(
        phase="implementing",
        branch_name="issue-7",
        next_action="ai_review",
        iteration=3,
    )
    body = st.write_state("# Issue body\n- [ ] task", original)
    restored = st.parse_state(body, 7)
    assert restored.phase == "implementing"
    assert restored.branch_name == "issue-7"
    assert restored.next_action == "ai_review"
    assert restored.iteration == 3


def test_from_dict_ignores_unknown_keys_like_legacy_spec_path():
    s = st.IssueState.from_dict(
        {
            "phase": "implementing",
            "branch_name": "issue-7",
            "spec_path": ".specs/2026-07-16-issue-7.md",
        }
    )
    assert s.phase == "implementing"
    assert s.branch_name == "issue-7"


def test_strip_state_removes_block_but_keeps_human_body():
    human = "# Title\n- [ ] Do the thing"
    body = st.write_state(human, st.IssueState(branch_name="issue-1"))
    assert st.STATE_START not in st.strip_state(body)
    assert st.strip_state(body) == human


def test_write_state_replaces_existing_block_without_stacking():
    body = st.write_state("body", st.IssueState(phase="a"))
    body = st.write_state(body, st.IssueState(phase="b"))
    # only one state block should remain
    assert body.count(st.STATE_START) == 1
    assert st.parse_state(body, 1).phase == "b"


def test_parse_tasks_and_progress():
    body = "- [ ] first\n- [x] second\n  - [ ] nested"
    tasks = st.parse_tasks(body)
    assert [t.text for t in tasks] == ["first", "second", "nested"]
    assert [t.done for t in tasks] == [False, True, False]
    assert st.progress(body) == (1, 3)


def test_next_unchecked_returns_first_open_task():
    body = "- [x] done\n- [ ] next\n- [ ] after next"
    task = st.next_unchecked(body)
    assert task is not None
    assert task.text == "next"


def test_next_unchecked_none_when_all_done():
    assert st.next_unchecked("- [x] a\n- [x] b") is None


def test_check_task_marks_the_targeted_box():
    body = "- [ ] alpha\n- [ ] beta"
    task = st.next_unchecked(body)
    assert task is not None
    updated = st.check_task(body, task)
    assert "- [x] alpha" in updated
    assert "- [ ] beta" in updated
    assert st.progress(updated) == (1, 2)


def test_parse_state_tolerates_broken_json():
    body = f"{st.STATE_START}\n{{ not valid json }}\n{st.STATE_END}"
    s = st.parse_state(body, 5)
    assert s.branch_name == "issue-5"  # falls back to the default


def test_conflict_retries_defaults_to_zero():
    s = st.IssueState()
    assert s.conflict_retries == 0


def test_from_dict_backward_compat_without_conflict_retries():
    s = st.IssueState.from_dict({"phase": "pr_open", "branch_name": "issue-3"})
    assert s.conflict_retries == 0


def test_conflict_retries_roundtrip():
    original = st.IssueState(branch_name="issue-7", conflict_retries=2)
    body = st.write_state("body", original)
    restored = st.parse_state(body, 7)
    assert restored.conflict_retries == 2


def test_ci_fix_retries_defaults_to_zero():
    s = st.IssueState()
    assert s.ci_fix_retries == 0


def test_from_dict_backward_compat_without_ci_fix_retries():
    s = st.IssueState.from_dict({"phase": "pr_open", "branch_name": "issue-3"})
    assert s.ci_fix_retries == 0


def test_ci_fix_retries_roundtrip():
    original = st.IssueState(branch_name="issue-7", ci_fix_retries=2)
    body = st.write_state("body", original)
    restored = st.parse_state(body, 7)
    assert restored.ci_fix_retries == 2


def test_total_agent_runs_defaults_to_zero():
    s = st.IssueState()
    assert s.total_agent_runs == 0


def test_from_dict_backward_compat_without_total_agent_runs():
    s = st.IssueState.from_dict({"phase": "pr_open", "branch_name": "issue-3"})
    assert s.total_agent_runs == 0


def test_total_agent_runs_roundtrip():
    original = st.IssueState(branch_name="issue-7", total_agent_runs=8)
    body = st.write_state("body", original)
    restored = st.parse_state(body, 7)
    assert restored.total_agent_runs == 8


# -- GHSWARM_VERIFY ----------------------------------------------------------


def _verify_block(yaml_body: str) -> str:
    return f"{st.VERIFY_START}\n{yaml_body}\n{st.VERIFY_END}"


def test_has_verify_meta_true_when_block_present():
    body = "# Issue\n" + _verify_block("verify:\n  - echo ok")
    assert st.has_verify_meta(body)


def test_has_verify_meta_false_when_block_absent():
    assert not st.has_verify_meta("# Issue only")


def test_parse_verify_meta_returns_verify_dict():
    body = _verify_block("verify:\n  - make test")
    assert st.parse_verify_meta(body) == {"verify": ["make test"]}


def test_parse_verify_meta_empty_when_no_block():
    assert st.parse_verify_meta("# no verify") == {}


def test_parse_verify_meta_raises_on_malformed_yaml():
    import pytest

    from ghswarm.config import ConfigError

    body = _verify_block("verify: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        st.parse_verify_meta(body)


def test_parse_verify_meta_raises_on_non_dict():
    import pytest

    from ghswarm.config import ConfigError

    body = _verify_block("- a\n- b")
    with pytest.raises(ConfigError, match="mapping"):
        st.parse_verify_meta(body)


def test_strip_verify_removes_block():
    human = "# Title\n- [ ] task"
    body = f"{human}\n\n{_verify_block('verify: []')}"
    assert st.strip_verify(body) == human


def test_prose_strips_state_and_verify():
    human = "# Title\n- [ ] task"
    body = st.write_state(human + "\n\n" + _verify_block("verify:\n  - echo ok"), st.IssueState())
    assert st.prose(body) == human


def test_verify_survives_repeated_write_state():
    human = "# Title\n- [ ] task"
    verify = _verify_block("verify:\n  - pytest")
    body = f"{human}\n\n{verify}"
    state = st.IssueState(branch_name="issue-1")
    for _ in range(3):
        body = st.write_state(st.strip_state(body), state)
    assert st.has_verify_meta(body)
    assert st.parse_verify_meta(body) == {"verify": ["pytest"]}
    assert st.prose(body) == human


def test_check_task_correct_with_verify_before_tasks():
    body = _verify_block("verify:\n  - echo ok") + "\n- [ ] alpha\n- [ ] beta"
    task = st.next_unchecked(body)
    assert task is not None
    updated = st.check_task(body, task)
    assert "- [x] alpha" in updated
    assert "- [ ] beta" in updated


def test_check_task_correct_with_verify_after_tasks():
    body = "- [ ] alpha\n- [ ] beta\n\n" + _verify_block("verify:\n  - echo ok")
    task = st.next_unchecked(body)
    assert task is not None
    updated = st.check_task(body, task)
    assert "- [x] alpha" in updated
    assert "- [ ] beta" in updated
