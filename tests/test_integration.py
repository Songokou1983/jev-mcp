"""Integration tests against real Jev API. Env-gated, ~$0.001 cap per run.

Skipped unless TYPESAFE_API_KEY is set in env. Claude Code propagates it from
~/.claude/settings.json env section.
"""
import os

import pytest

from jev_mcp.budget import SessionBudget
from jev_mcp.cache import LRUCache
from jev_mcp.decision_log import DecisionLog
from jev_mcp.jev_client import JevClient
from jev_mcp.server import ServerDeps, batch_judge, check, classify, health, rate as rate_fn, system_one_raw


API_KEY = os.environ.get("TYPESAFE_API_KEY")
SKIP_REASON = "TYPESAFE_API_KEY not set in env — integration tests skipped"

pytestmark = pytest.mark.skipif(not API_KEY, reason=SKIP_REASON)


@pytest.fixture
def deps(tmp_path):
    """Real-deps integration: real JevClient, fresh cache/budget/log per test."""
    return ServerDeps(
        client=JevClient(api_key=API_KEY),
        cache=LRUCache(capacity=100),
        budget=SessionBudget(limit_usd=0.01),  # tight cap to bound test cost
        decision_log=DecisionLog(path=str(tmp_path / "decisions.jsonl"), log_full_response=False),
        threshold_act=0.7,
        threshold_confirm=0.4,
        state_cap_chars=32000,
        input_token_rate_usd=42e-9,
        session_started_at="2026-09-21T17:00:00Z",
        model_version="jev-1.13.0",
    )


class TestClassifyReal:
    async def test_basic(self, deps):
        result = await classify(
            deps,
            text="My payout has been failing for 3 days",
            options=["billing", "technical", "sales"],
        )
        assert result["choice"] in ("billing", "technical", "sales")
        assert 0.0 <= result["confidence"] <= 1.0
        assert result["action"] in ("act", "confirm", "escalate")
        assert result["cached"] is False


class TestCheckReal:
    async def test_yes(self, deps):
        result = await check(deps, text="Help! My payouts have been failing for 3 days.", condition="Does this convey urgency?")
        assert "verdict" in result
        assert 0.0 <= result["probability"] <= 1.0
        assert result["action"] in ("act", "confirm", "escalate")


class TestRateReal:
    async def test_basic(self, deps):
        result = await rate_fn(
            deps,
            text="Help! My payouts have been failing for 3 days.",
            levels=["Calm", "Frustrated", "Very angry"],
        )
        assert 0.0 <= result["score"] <= 2.0
        assert result["score_label"] in ("Calm", "Frustrated", "Very angry")


class TestBatchJudgeReal:
    async def test_three_items(self, deps):
        result = await batch_judge(
            deps,
            items=["Refund denied twice", "How do I cancel?", "Just saying hi"],
            criteria=[
                {"type": "choice", "options": ["billing", "technical", "other"], "question": "Which team?"},
                {"type": "choice", "options": ["billing", "technical", "other"], "question": "Which team?"},
                {"type": "choice", "options": ["billing", "technical", "other"], "question": "Which team?"},
            ],
        )
        assert result["items_count"] == 3
        assert all("choice" in r for r in result["results"])


class TestSystemOneRawReal:
    async def test_passthrough(self, deps):
        result = await system_one_raw(
            deps,
            state="The package arrived broken",
            questions={"is_damage": {"type": "noul", "instructions": "Does this report damage?"}},
        )
        assert "answers" in result
        assert "is_damage" in result["answers"]
        assert "model" in result
        assert result["model"] == "jev-1.13.0"  # server pins


class TestHealthReal:
    async def test_health(self, deps):
        result = await health(deps)
        assert result["model_version"] == "jev-1.13.0"
        assert result["session_spent_usd"] >= 0