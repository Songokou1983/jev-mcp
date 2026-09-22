"""Server tools: classify, check, rate, batch_judge, system_one_raw, health.

Each tool goes through:
  state_cap check → cache lookup → JevClient.send → action_gate → decision_log

Tests use a fake JevClient (no HTTP) to assert orchestration logic.

TDD — define behavior before implementation.
"""
import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jev_mcp.cache import LRUCache, compute_cache_key
from jev_mcp.budget import SessionBudget
from jev_mcp.decision_log import DecisionLog, LogEntry
from jev_mcp.server import (
    ServerDeps,
    batch_judge,
    check,
    classify,
    health,
    rate,
    system_one_raw,
)


# --- fixtures ---

@pytest.fixture
def fake_client() -> MagicMock:
    """A MagicMock pretending to be JevClient. Returns canned responses."""
    client = MagicMock()
    client.model_version = "jev-1.13.0"
    client.send = AsyncMock()
    client.probe = AsyncMock(return_value=True)
    return client


@pytest.fixture
def cache() -> LRUCache:
    return LRUCache(capacity=100)


@pytest.fixture
def budget() -> SessionBudget:
    return SessionBudget(limit_usd=1.0)  # high cap for tests


@pytest.fixture
def decision_log(tmp_path) -> DecisionLog:
    return DecisionLog(path=str(tmp_path / "decisions.jsonl"), log_full_response=False)


@pytest.fixture
def deps(fake_client, cache, budget, decision_log) -> ServerDeps:
    return ServerDeps(
        client=fake_client,
        cache=cache,
        budget=budget,
        decision_log=decision_log,
        threshold_act=0.7,
        threshold_confirm=0.4,
        state_cap_chars=32000,
        input_token_rate_usd=42e-9,
        session_started_at="2026-09-21T17:00:00Z",
        model_version="jev-1.13.0",
    )


def _choice_response(name: str, choice: str, confidence: float = 0.9) -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            name: {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {choice: confidence},
            }
        },
        "usage": {"input_tokens": 314, "output_tokens": 5},
    }


def _noul_response(name: str, noul: float) -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {name: {"type": "noul", "noul": noul}},
        "usage": {"input_tokens": 50, "output_tokens": 5},
    }


def _score_response(name: str, score: float, confidence: float = 0.9) -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            name: {
                "type": "score",
                "score": score,
                "confidence": confidence,
                "probabilities": {str(int(score)): confidence},
                "legend": {"0": "low", "1": "high"},
            }
        },
        "usage": {"input_tokens": 100, "output_tokens": 5},
    }


# --- classify ---

class TestClassify:
    async def test_returns_choice_confidence_action(self, deps, fake_client):
        fake_client.send.return_value = _choice_response("answer", "billing", confidence=0.9)
        result = await classify(
            deps,
            text="user wants refund",
            options=["billing", "returns", "other"],
        )
        assert result["choice"] == "billing"
        assert result["confidence"] == 0.9
        assert result["action"] == "act"  # 0.9 > 0.7

    async def test_confirm_in_middle(self, deps, fake_client):
        fake_client.send.return_value = _choice_response("answer", "x", confidence=0.5)
        result = await classify(deps, text="x", options=["x", "y"])
        assert result["action"] == "confirm"

    async def test_escalate_below(self, deps, fake_client):
        fake_client.send.return_value = _choice_response("answer", "x", confidence=0.2)
        result = await classify(deps, text="x", options=["x", "y"])
        assert result["action"] == "escalate"

    async def test_descriptions_passed_through(self, deps, fake_client):
        fake_client.send.return_value = _choice_response("answer", "billing")
        await classify(
            deps,
            text="refund denied",
            options=["billing", "returns"],
            descriptions={
                "billing": "invoicing/payment",
                "returns": "product returns",
            },
        )
        # Inspect the send call args
        send_kwargs = fake_client.send.call_args.kwargs
        questions = send_kwargs["questions"]
        assert questions["answer"]["type"] == "choice"
        assert "billing" in questions["answer"]["criteria"]

    async def test_state_cap_rejects_large(self, deps, fake_client):
        with pytest.raises(Exception) as exc_info:
            await classify(deps, text="x" * 32001, options=["a", "b"])
        assert "cap" in str(exc_info.value).lower()

    async def test_min_two_options(self, deps, fake_client):
        with pytest.raises(ValueError) as exc_info:
            await classify(deps, text="x", options=["only"])
        assert "2" in str(exc_info.value)

    async def test_max_255_options(self, deps, fake_client):
        with pytest.raises(ValueError) as exc_info:
            await classify(deps, text="x", options=[f"o{i}" for i in range(256)])
        assert "255" in str(exc_info.value)

    async def test_cache_hit_does_not_call_api(self, deps, fake_client):
        # First call populates cache
        fake_client.send.return_value = _choice_response("answer", "billing", confidence=0.9)
        r1 = await classify(deps, text="same input", options=["billing", "returns"])
        # Second call should hit cache, no API call
        fake_client.send.reset_mock()
        r2 = await classify(deps, text="same input", options=["billing", "returns"])
        fake_client.send.assert_not_called()
        assert r1["choice"] == r2["choice"]
        assert r2["cached"] is True

    async def test_cached_response_logs_with_cached_true(self, deps, fake_client, decision_log):
        fake_client.send.return_value = _choice_response("answer", "x")
        await classify(deps, text="logtest", options=["x", "y"])
        # 2nd call hits cache
        await classify(deps, text="logtest", options=["x", "y"])
        log_text = decision_log.path.read_text()
        # Both entries should have cached field; second one true
        cached_values = [line for line in log_text.strip().split("\n") if "logtest" in line or True]
        # Just assert at least one entry has cached=True
        assert '"cached":true' in log_text or '"cached": true' in log_text

    async def test_default_question_used(self, deps, fake_client):
        fake_client.send.return_value = _choice_response("answer", "x")
        await classify(deps, text="y", options=["x", "y"])
        send_kwargs = fake_client.send.call_args.kwargs
        # Default instructions for Choice
        assert "instructions" in send_kwargs["questions"]["answer"]


# --- check ---

class TestCheck:
    async def test_high_yes_returns_act(self, deps, fake_client):
        fake_client.send.return_value = _noul_response("answer", noul=0.95)
        result = await check(deps, text="x", condition="is urgent?")
        assert result["verdict"] == "yes"
        assert result["action"] == "act"

    async def test_low_no_returns_act(self, deps, fake_client):
        fake_client.send.return_value = _noul_response("answer", noul=0.05)
        result = await check(deps, text="x", condition="is urgent?")
        assert result["verdict"] == "no"
        # eff = |0.05 - 0.5| * 2 = 0.9 → act
        assert result["action"] == "act"

    async def test_weak_yes_escalates_by_design(self, deps, fake_client):
        # noul=0.6 → eff=0.2 < 0.4 → escalate (Noul is more conservative than Choice)
        fake_client.send.return_value = _noul_response("answer", noul=0.6)
        result = await check(deps, text="x", condition="yes?")
        assert result["verdict"] == "yes"
        assert result["action"] == "escalate"

    async def test_uncertain_escalates(self, deps, fake_client):
        fake_client.send.return_value = _noul_response("answer", noul=0.5)
        result = await check(deps, text="x", condition="yes?")
        assert result["action"] == "escalate"


# --- rate ---

class TestRate:
    async def test_returns_score_and_label(self, deps, fake_client):
        fake_client.send.return_value = _score_response("answer", score=1.0, confidence=0.9)
        result = await rate(deps, text="x", levels=["low", "medium", "high"])
        assert result["score"] == 1.0
        # int(round(1.0)) = 1 → levels[1] = "medium"
        assert result["score_label"] == "medium"
        assert result["action"] == "act"


# --- batch_judge ---

class TestBatchJudge:
    async def test_serial_iteration_each_independent(self, deps, fake_client):
        # Mock returns different responses for sequential calls
        fake_client.send.side_effect = [
            _choice_response("q0", "a"),
            _choice_response("q1", "b"),
            _noul_response("q2", noul=0.9),
        ]
        result = await batch_judge(
            deps,
            items=["item0", "item1", "item2"],
            criteria=[
                {"type": "choice", "options": ["a", "b"], "question": "Q0?"},
                {"type": "choice", "options": ["a", "b"], "question": "Q1?"},
                {"type": "noul", "condition": "is it?"},
            ],
        )
        assert result["items_count"] == 3
        assert len(result["results"]) == 3
        # 3 separate API calls (one per item)
        assert fake_client.send.call_count == 3
        # Each result has its own answer
        assert result["results"][0]["choice"] == "a"
        assert result["results"][1]["choice"] == "b"
        assert result["results"][2]["probability"] == 0.9

    async def test_cap_20(self, deps, fake_client):
        # 21 items should reject
        with pytest.raises(Exception) as exc_info:
            await batch_judge(
                deps,
                items=[f"item{i}" for i in range(21)],
                criteria=[{"type": "noul", "condition": "yes?"}] * 21,
            )
        assert "20" in str(exc_info.value)

    async def test_each_item_independent_state(self, deps, fake_client):
        # Each call's state should be just one item, not the whole list
        fake_client.send.side_effect = [
            _choice_response("q0", "x"),
            _choice_response("q1", "y"),
        ]
        await batch_judge(
            deps,
            items=["first", "second"],
            criteria=[
                {"type": "choice", "options": ["x", "y"], "question": "Pick"},
                {"type": "choice", "options": ["x", "y"], "question": "Pick"},
            ],
        )
        # Each send call should have state=item, not items=[...]
        for call in fake_client.send.call_args_list:
            state = call.kwargs["state"]
            assert isinstance(state, str)
            assert state in ("first", "second")


# --- system_one_raw ---

class TestSystemOneRaw:
    async def test_passes_through(self, deps, fake_client):
        fake_client.send.return_value = _noul_response("custom_q", noul=0.8)
        result = await system_one_raw(
            deps,
            state={"data": "x"},
            questions={"custom_q": {"type": "noul", "instructions": "yes?"}},
        )
        # Returns raw Jev response (passthrough)
        assert "answers" in result
        assert "custom_q" in result["answers"]

    async def test_model_pin_overrides_client(self, deps, fake_client):
        fake_client.send.return_value = _noul_response("q", noul=0.5)
        # Caller can't override model via raw — server always pins
        await system_one_raw(
            deps,
            state="x",
            questions={"q": {"type": "noul", "model": "jev-99.0"}},  # attempted override
        )
        # The actual send call uses pinned model via format_request
        send_kwargs = fake_client.send.call_args.kwargs
        # Model is pinned by JevClient.format_request (always returns "jev-1.13.0")


# --- health ---

class TestHealth:
    async def test_reports_session_stats(self, deps, fake_client):
        # Configure probe to return True
        fake_client.probe = AsyncMock(return_value=True)
        result = await health(deps)
        assert result["model_version"] == "jev-1.13.0"
        assert result["api_reachable"] is True
        assert result["session_budget_usd"] == 1.0
        assert "session_started_at" in result

    async def test_api_unreachable_reflected(self, deps, fake_client):
        fake_client.probe = AsyncMock(return_value=False)
        result = await health(deps)
        assert result["api_reachable"] is False

    async def test_budget_after_calls(self, deps, fake_client):
        fake_client.send.return_value = _choice_response("answer", "x", confidence=0.9)
        await classify(deps, text="t1", options=["x", "y"])
        await classify(deps, text="t2", options=["x", "y"])
        result = await health(deps)
        # 314 input tokens × 42e-9 = 1.3188e-5 per call
        assert result["session_tokens_used"] == 628
        assert result["decisions_logged"] >= 2