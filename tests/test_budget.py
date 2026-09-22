"""Session budget tracker: cost accrual + threshold check.

TDD — these tests define the behavior before implementation.
"""
import pytest

from jev_mcp.budget import BudgetExceeded, SessionBudget


class TestSessionBudget:
    """Session = single server process lifetime (PLAN.md §13)."""

    def test_initial_state(self):
        b = SessionBudget(limit_usd=0.10)
        assert b.spent_usd == 0.0
        assert b.calls == 0
        assert b.remaining_usd == pytest.approx(0.10)

    def test_accrue_increments(self):
        b = SessionBudget(limit_usd=0.10)
        b.accrue(cost_usd=0.001, input_tokens=24)
        b.accrue(cost_usd=0.002, input_tokens=48)
        assert b.spent_usd == pytest.approx(0.003)
        assert b.calls == 2
        assert b.tokens_used == 72
        assert b.remaining_usd == pytest.approx(0.097)

    def test_exceed_raises(self):
        b = SessionBudget(limit_usd=0.01)
        b.accrue(cost_usd=0.005, input_tokens=100)
        with pytest.raises(BudgetExceeded) as exc_info:
            b.accrue(cost_usd=0.006, input_tokens=100)
        assert "0.011" in str(exc_info.value)
        # Budget not modified after exceed
        assert b.spent_usd == pytest.approx(0.005)

    def test_exact_limit_passes(self):
        b = SessionBudget(limit_usd=0.01)
        b.accrue(cost_usd=0.005, input_tokens=100)
        b.accrue(cost_usd=0.005, input_tokens=100)
        assert b.spent_usd == pytest.approx(0.01)
        assert b.remaining_usd == pytest.approx(0.0)