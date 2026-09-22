"""State cap: refuse if serialized state > N chars (CJK-aware note in PLAN.md §2).

TDD — these tests define the behavior before implementation.
"""
import pytest

from jev_mcp.state_cap import check_state_cap, serialize_state


class TestSerializeState:
    """State serialization: string passes through, dict → canonical JSON."""

    def test_string_passes_through(self):
        assert serialize_state("hello") == "hello"

    def test_dict_to_canonical_json(self):
        s = serialize_state({"b": 2, "a": 1})
        assert s == '{"a": 1, "b": 2}'

    def test_unicode_dict_preserved(self):
        # ensure_ascii=False
        s = serialize_state({"text": "你好"})
        assert s == '{"text": "你好"}'


class TestCheckStateCap:
    """32k character cap with CJK caveat documented in PLAN.md §2."""

    def test_under_cap_passes(self):
        # 100 chars
        check_state_cap("x" * 100, cap=32000)

    def test_at_cap_passes(self):
        check_state_cap("x" * 32000, cap=32000)

    def test_over_cap_raises(self):
        with pytest.raises(StateCapExceeded) as exc_info:
            check_state_cap("x" * 32001, cap=32000)
        assert exc_info.value.size == 32001
        assert exc_info.value.cap == 32000

    def test_dict_state_checked_after_serialize(self):
        # JSON-encoded dict of moderate size
        state = {"text": "x" * 100}
        check_state_cap(state, cap=32000)

    def test_custom_cap(self):
        check_state_cap("x" * 50, cap=100)
        with pytest.raises(StateCapExceeded):
            check_state_cap("x" * 101, cap=100)


class TestStateCapExceeded:
    def test_message_includes_size_and_cap(self):
        with pytest.raises(StateCapExceeded) as exc_info:
            check_state_cap("x" * 100, cap=50)
        msg = str(exc_info.value)
        assert "100" in msg
        assert "50" in msg


# Defined here to make pytest.raises usage clean in this test module
from jev_mcp.state_cap import StateCapExceeded