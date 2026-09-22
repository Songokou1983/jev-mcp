"""Session budget tracker.

Session = single server process lifetime (PLAN.md §13). PM2 restart resets it.
Cost = input_tokens × JEV_INPUT_TOKEN_RATE_USD (default 42e-9, output free).
"""
from __future__ import annotations


class BudgetExceeded(Exception):
    """Raised when a call would push session cost past the limit."""

    def __init__(self, spent_usd: float, limit_usd: float):
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        super().__init__(
            f"session budget exceeded: spent ${spent_usd:.6f}, limit ${limit_usd:.2f}"
        )


class SessionBudget:
    """Per-process USD budget with monotonic cost accrual."""

    def __init__(self, limit_usd: float):
        if limit_usd <= 0:
            raise ValueError("limit_usd must be > 0")
        self.limit_usd = limit_usd
        self.spent_usd = 0.0
        self.tokens_used = 0
        self.calls = 0

    def accrue(self, cost_usd: float, input_tokens: int) -> None:
        """Record a call's cost. Raises BudgetExceeded if it would exceed the limit.

        If raise, state is rolled back (cost not added) so the limit holds even if
        the caller catches and retries.
        """
        new_spent = self.spent_usd + cost_usd
        if new_spent > self.limit_usd:
            raise BudgetExceeded(spent_usd=new_spent, limit_usd=self.limit_usd)
        self.spent_usd = new_spent
        self.tokens_used += input_tokens
        self.calls += 1

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)