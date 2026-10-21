"""MCP server for Jev (TypeSafe System One).

4 core tools (classify/check/rate/batch_judge) + 1 escape hatch (system_one_raw) + 1 health.
7 server-side features: confidence gating, cost guards, cache, centralized key,
decision log, version pin, retry+backoff (retry lives in JevClient).

Tool orchestrators are pure async functions taking a ServerDeps. FastMCP
decorators wrap them and inject the shared deps from server state.
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from jev_mcp.action_gate import compute_action, effective_confidence
from jev_mcp.bearer import BearerTokenVerifier, load_bearer_token
from jev_mcp.budget import SessionBudget
from jev_mcp.cache import LRUCache, compute_cache_key
from jev_mcp.decision_log import DecisionLog, LogEntry
from jev_mcp.jev_client import JevClient, PINNED_MODEL_VERSION
from jev_mcp.state_cap import check_state_cap


# --- config / dependencies ---

@dataclass
class ServerDeps:
    """Bundle of stateful collaborators. Pass through to tool functions."""

    client: JevClient
    cache: LRUCache
    budget: SessionBudget
    decision_log: DecisionLog
    threshold_act: float
    threshold_confirm: float
    state_cap_chars: int
    input_token_rate_usd: float
    session_started_at: str
    model_version: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _args_hash(state: Any, questions: dict[str, Any]) -> str:
    """sha256 of canonical (state, questions) for traceability without exposure."""
    import hashlib
    import json

    payload = (
        json.dumps(state, sort_keys=True, ensure_ascii=False)
        + "|"
        + json.dumps(questions, sort_keys=True, ensure_ascii=False)
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _accrue(
    deps: ServerDeps,
    tool_name: str,
    state: Any,
    questions: dict[str, Any],
    response: dict,
    elapsed_ms: int,
    cached: bool,
) -> tuple[int, float]:
    """Log decision + accrue budget. Returns (input_tokens, cost_usd)."""
    input_tokens = response.get("usage", {}).get("input_tokens", 0)
    cost_usd = input_tokens * deps.input_token_rate_usd
    action = _action_for_response(deps, response, questions)
    response_for_log = response if deps.decision_log.log_full_response else None
    entry = LogEntry(
        timestamp=_utc_now_iso(),
        tool=tool_name,
        args_hash=_args_hash(state, questions),
        latency_ms=elapsed_ms,
        tokens=response.get("usage", {}),
        cost_usd=cost_usd,
        action=action,
        cached=cached,
        response=response_for_log,
    )
    deps.decision_log.append(entry)
    if not cached:
        deps.budget.accrue(cost_usd=cost_usd, input_tokens=input_tokens)
    return input_tokens, cost_usd


def _action_for_response(
    deps: ServerDeps,
    response: dict,
    questions: dict[str, Any],
) -> str:
    """Compute action based on the first question's confidence/noul."""
    answers = response.get("answers", {})
    if not answers:
        return "escalate"
    first_name = next(iter(answers))
    ans = answers[first_name]
    qtype = ans.get("type")
    if qtype in ("choice", "score"):
        conf = ans.get("confidence", 0.0)
        return compute_action(conf, deps.threshold_act, deps.threshold_confirm)
    if qtype == "noul":
        eff = effective_confidence(ans.get("noul", 0.5))
        return compute_action(eff, deps.threshold_act, deps.threshold_confirm)
    return "escalate"


# --- tool orchestrators (pure async, testable) ---

async def classify(
    deps: ServerDeps,
    text: str,
    options: list[str],
    descriptions: dict[str, str] | None = None,
    question: str = "Which option best matches?",
) -> dict:
    """One-of-N classification."""
    if not 2 <= len(options) <= 255:
        raise ValueError(f"options must have 2-255 entries, got {len(options)}")
    check_state_cap(text, deps.state_cap_chars)
    criteria: dict[str, str | None] = {}
    for opt in options:
        criteria[opt] = descriptions.get(opt) if descriptions else None
    questions = {"answer": {"type": "choice", "instructions": question, "criteria": criteria}}
    return await _cached_send(deps, "classify", text, questions)


async def check(deps: ServerDeps, text: str, condition: str) -> dict:
    """Yes/no with calibrated probability."""
    check_state_cap(text, deps.state_cap_chars)
    questions = {
        "answer": {
            "type": "noul",
            "instructions": condition,
        }
    }
    return await _cached_send(deps, "check", text, questions)


async def rate(
    deps: ServerDeps,
    text: str,
    levels: list[str],
    question: str = "Score according to the rubric",
) -> dict:
    """Ordered rubric scoring."""
    if not 2 <= len(levels) <= 10:
        raise ValueError(f"levels must have 2-10 entries, got {len(levels)}")
    check_state_cap(text, deps.state_cap_chars)
    questions = {"answer": {"type": "score", "instructions": question, "criteria": levels}}
    return await _cached_send(deps, "rate", text, questions, levels_for_label=levels)


async def batch_judge(
    deps: ServerDeps,
    items: list[str],
    criteria: list[dict],
) -> dict:
    """Independent per-item evaluation. Server iterates sequentially."""
    if len(items) > 20:
        raise ValueError(f"batch_judge cap is 20 items, got {len(items)}")
    if len(items) != len(criteria):
        raise ValueError(f"items ({len(items)}) and criteria ({len(criteria)}) length mismatch")
    if len(items) == 0:
        return {"results": [], "items_count": 0, "tokens": {"input": 0, "output": 0}, "total_latency_ms": 0.0}

    results: list[dict] = []
    total_input_tokens = 0
    total_latency_ms = 0.0
    started = time.monotonic()
    for i, (item, crit) in enumerate(zip(items, criteria)):
        ct = crit["type"]
        if ct == "choice":
            per_q = {
                "instructions": crit.get("question", "Which option best matches?"),
                "type": "choice",
                "criteria": {opt: None for opt in crit["options"]},
            }
        elif ct == "noul":
            per_q = {"instructions": crit["condition"], "type": "noul"}
        elif ct == "score":
            per_q = {
                "instructions": crit.get("question", "Score"),
                "type": "score",
                "criteria": crit["levels"],
            }
        else:
            raise ValueError(f"unknown criterion type: {ct}")
        questions = {f"q{i}": per_q}
        # No cache for batch items in v1 — keeps semantics simple
        t0 = time.monotonic()
        response = await deps.client.send(state=item, questions=questions)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        ans = response["answers"][f"q{i}"]
        total_input_tokens += response.get("usage", {}).get("input_tokens", 0)
        total_latency_ms += elapsed_ms
        eff = (
            effective_confidence(ans.get("noul", 0.5))
            if ans.get("type") == "noul"
            else ans.get("confidence", 0.0)
        )
        action = compute_action(eff, deps.threshold_act, deps.threshold_confirm)
        cost_usd = response.get("usage", {}).get("input_tokens", 0) * deps.input_token_rate_usd
        deps.budget.accrue(cost_usd=cost_usd, input_tokens=response.get("usage", {}).get("input_tokens", 0))
        deps.decision_log.append(LogEntry(
            timestamp=_utc_now_iso(),
            tool="batch_judge",
            args_hash=_args_hash(item, questions),
            latency_ms=elapsed_ms,
            tokens=response.get("usage", {}),
            cost_usd=cost_usd,
            action=action,
            cached=False,
            response=response if deps.decision_log.log_full_response else None,
        ))
        item_result: dict[str, Any] = {
            "item_index": i,
            "item": item,
            "type": ans.get("type"),
            "confidence": eff,
            "action": action,
        }
        if ans.get("type") == "choice":
            item_result["choice"] = ans["choice"]
            item_result["probabilities"] = ans["probabilities"]
        elif ans.get("type") == "noul":
            item_result["probability"] = ans["noul"]
            item_result["verdict"] = "yes" if ans["noul"] > 0.5 else "no"
        elif ans.get("type") == "score":
            item_result["score"] = ans["score"]
            item_result["score_label"] = crit["levels"][min(int(round(ans["score"])), len(crit["levels"]) - 1)]
            item_result["probabilities"] = ans["probabilities"]
        results.append(item_result)
    total_ms = int((time.monotonic() - started) * 1000)
    return {
        "results": results,
        "items_count": len(items),
        "tokens": {"input": total_input_tokens, "output": 0},
        "total_latency_ms": total_ms,
    }


async def system_one_raw(deps: ServerDeps, state: Any, questions: dict[str, Any]) -> dict:
    """Raw passthrough. Model is always pinned by JevClient."""
    check_state_cap(state, deps.state_cap_chars)
    t0 = time.monotonic()
    response = await deps.client.send(state=state, questions=questions)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _accrue(deps, "system_one_raw", state, questions, response, elapsed_ms, cached=False)
    return response


async def health(deps: ServerDeps) -> dict:
    """Server diagnostics. Real API probe via GET /v1/models (free, no inference cost)."""
    api_reachable = await deps.client.probe()
    return {
        "api_reachable": api_reachable,
        "model_version": deps.model_version,
        "session_tokens_used": deps.budget.tokens_used,
        "session_budget_usd": deps.budget.limit_usd,
        "session_remaining_usd": deps.budget.remaining_usd,
        "session_spent_usd": deps.budget.spent_usd,
        "session_started_at": deps.session_started_at,
        "uptime_seconds": int((datetime.now(timezone.utc) - datetime.strptime(deps.session_started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()),
        "cache_size": deps.cache.size,
        "cache_hits": deps.cache.hits,
        "cache_misses": deps.cache.misses,
        "cache_hit_rate": deps.cache.hit_rate(),
        "decisions_logged": _count_log_entries(deps),
    }


def _count_log_entries(deps: ServerDeps) -> int:
    """Count lines in the current decision log file."""
    try:
        with open(deps.decision_log.path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


# --- internal cache wrapper for single-primitive tools ---

async def _cached_send(
    deps: ServerDeps,
    tool_name: str,
    state: Any,
    questions: dict[str, Any],
    levels_for_label: list[str] | None = None,
) -> dict:
    """Cache lookup → JevClient.send on miss → log + accrue → format."""
    key = compute_cache_key(state, questions, deps.model_version)
    cached_resp = deps.cache.get(key)
    if cached_resp is not None:
        elapsed_ms = 0
        input_tokens, cost_usd = _accrue(deps, tool_name, state, questions, cached_resp, elapsed_ms, cached=True)
        result = _format_single_response(deps, cached_resp, cached=True, levels_for_label=levels_for_label)
        result["tokens"] = {"input": input_tokens, "output": 0}
        result["cost_usd"] = cost_usd
        return result

    t0 = time.monotonic()
    response = await deps.client.send(state=state, questions=questions)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    deps.cache.set(key, response)
    input_tokens, cost_usd = _accrue(deps, tool_name, state, questions, response, elapsed_ms, cached=False)
    result = _format_single_response(deps, response, cached=False, levels_for_label=levels_for_label)
    result["tokens"] = {"input": input_tokens, "output": response.get("usage", {}).get("output_tokens", 0)}
    result["cost_usd"] = cost_usd
    return result


def _format_single_response(
    deps: ServerDeps,
    response: dict,
    cached: bool,
    levels_for_label: list[str] | None = None,
) -> dict:
    """Build the user-facing dict from a Jev single-primitive response."""
    ans = next(iter(response["answers"].values()))
    qtype = ans.get("type")
    if qtype == "choice":
        action = compute_action(ans["confidence"], deps.threshold_act, deps.threshold_confirm)
        return {
            "choice": ans["choice"],
            "confidence": ans["confidence"],
            "probabilities": ans["probabilities"],
            "action": action,
            "cached": cached,
        }
    if qtype == "noul":
        eff = effective_confidence(ans["noul"])
        action = compute_action(eff, deps.threshold_act, deps.threshold_confirm)
        return {
            "probability": ans["noul"],
            "verdict": "yes" if ans["noul"] > 0.5 else "no",
            "action": action,
            "cached": cached,
        }
    if qtype == "score":
        action = compute_action(ans["confidence"], deps.threshold_act, deps.threshold_confirm)
        result = {
            "score": ans["score"],
            "confidence": ans["confidence"],
            "probabilities": ans["probabilities"],
            "action": action,
            "cached": cached,
        }
        if levels_for_label is not None:
            idx = min(int(round(ans["score"])), len(levels_for_label) - 1)
            result["score_label"] = levels_for_label[idx]
        return result
    raise ValueError(f"unknown question type: {qtype}")


# --- FastMCP glue ---

_deps: ServerDeps | None = None


def _require() -> ServerDeps:
    if _deps is None:
        raise RuntimeError("server not initialized; call init() before running")
    return _deps


# Tool descriptions — POSITIVE / NEGATIVE / EXAMPLE three-part format per PLAN.md §工具描述.
# These are passed via add_tool(description=...), not via docstrings, because FastMCP
# reads description from the Tool object, not the wrapper function.

DESC_CLASSIFY = """[POSITIVE] Use this tool to classify text into ONE of 2-255 named categories. Best when the options are mutually exclusive and well-defined.

[NEGATIVE] NEVER use this tool for: arithmetic, counting, date/time formatting or comparison, exact string match, regex, SQL queries, deterministic lookups, or anything a competent programmer could answer in <10 lines of code. Rule of thumb: if ground truth is in the data, don't pay model cost.

[EXAMPLE] classify(text="user wants refund denied", options=["billing", "returns", "other"])

[CONTRACT] Returns `choice` (string), `confidence` (0-1), `probabilities` (all options), `action` (act/confirm/escalate), and `cached` (bool). Default `question` framing is "Which option best matches?"; override with `question` only when ambiguous. Pass `descriptions` when 2+ options are similar — ~50 input tokens per option, dramatically improves borderline accuracy."""

DESC_CHECK = """[POSITIVE] Use this tool for binary yes/no decisions where you want a calibrated probability (0-1), not just a boolean. Examples: "Does this contain a credential?", "Is this a destructive command?", "Should we escalate to a human?".

[NEGATIVE] NEVER use this tool for: arithmetic, counting, date/time, exact string match, regex, SQL, or any task where a programmer could write the answer in <10 lines. If the answer is "definitely yes" or "definitely no" from data, don't pay model cost.

[EXAMPLE] check(text="rm -rf /tmp/cache", condition="Is this command destructive?")

[CONTRACT] Returns `probability` (0-1, >0.5 = yes), `verdict` ("yes"/"no"), `action` (act/confirm/escalate). By design, Noul is more conservative than Choice near the boundary — `probability=0.6` will escalate because effective_confidence = |0.6 - 0.5| × 2 = 0.2 < 0.4."""

DESC_RATE = """[POSITIVE] Use this tool to score text along an ORDERED rubric (2-10 ordered levels). Best when you need to distinguish degrees of the same dimension, e.g., urgency 0-4, quality 0-3, relevance 0-2.

[NEGATIVE] NEVER use this tool for: arithmetic, exact numeric comparisons, or when the level labels are ambiguous. For unordered category picking, use `classify` instead.

[EXAMPLE] rate(text="Customer says payouts have failed for 3 days", levels=["Calm", "Frustrated", "Very angry"])

[CONTRACT] Returns `score` (continuous float, 0 to len(levels)-1), `score_label` (rounded level name), `probabilities` (per-level), `confidence`, `action` (act/confirm/escalate). Use `score` for thresholds; use `score_label` for human-readable output."""

DESC_BATCH_JUDGE = """[POSITIVE] Use this tool when you have 2-20 items that each need to be evaluated by the same or different criteria. The server iterates each item INDEPENDENTLY (separate API call per item) — this is the whole point: preventing the agent from bundling multiple items into one state, which causes probability cross-contamination.

[NEGATIVE] NEVER use this tool for: a single item (use `classify`/`check`/`rate` instead); > 20 items (chunk and call multiple times); arithmetic, counting, date/time, exact string match, regex, SQL, or any deterministic check a programmer could write in <10 lines (rule of thumb: if the ground truth is in the data, don't pay model cost). NEVER try to bundle items yourself to "save calls" — that's exactly the bug this tool exists to prevent.

[EXAMPLE] batch_judge(items=["refund denied twice", "how do I cancel?", "just saying hi"], criteria=[
  {"type": "choice", "options": ["billing", "returns", "other"], "question": "Which department?"},
  {"type": "noul", "condition": "Is this asking how to cancel?"},
  {"type": "score", "levels": ["cold", "neutral", "warm"], "question": "Customer sentiment?"}
])

[CONTRACT] Each item's criterion dict declares type + options/levels/condition; server handles state isolation. Returns `results` (array, one per item), each with the per-item choice/score/probability, `action`, and `items_count` + `total_latency_ms`."""

DESC_SYSTEM_ONE_RAW = """[POSITIVE] Use this tool ONLY when the 4 core tools don't cover your case and you need raw access to /v1/systemone — typically multi-question parallel in one call, or a special composite scoring pattern.

[NEGATIVE] NEVER use this tool as a default. The 4 core tools (classify/check/rate/batch_judge) bake in best practices: default question framing, schema bounds, server-side state isolation, caching, budget tracking. If you find yourself reaching for this tool frequently, you're probably doing something the server should bake into a new tool.

[EXAMPLE] system_one_raw(state="customer support ticket", questions={"urgency": {"type": "score", "criteria": ["low", "high"]}, "category": {"type": "choice", "criteria": {"billing": None, "tech": None}}})

[CONTRACT] NO automatic confidence gate, cost guard, or caching on this path (those are server features applied to the 4 core tools). The `model` field is always pinned to `jev-1.13.0` server-side; client overrides are silently dropped. Returns the raw Jev response dict."""

DESC_HEALTH = """[POSITIVE] Use this tool to debug "why is this slow / failing?", check session budget before a big batch, or confirm the server is reachable after reconnecting to Tailscale.

[NEGATIVE] NEVER call this in a tight loop — it does a real GET /v1/models round-trip to verify API reachability and key validity.

[EXAMPLE] health() → {"api_reachable": true, "session_tokens_used": 1234, "session_remaining_usd": 0.087, "cache_hit_rate": 0.42, ...}

[CONTRACT] Returns `api_reachable` (bool, real probe), `model_version`, `session_tokens_used`, `session_spent_usd`, `session_remaining_usd`, `cache_size`/`cache_hits`/`cache_hit_rate`, `decisions_logged`, `uptime_seconds`. All free (no inference cost)."""


# Plain async wrappers (no @mcp.tool decorator — we register explicitly below for
# explicit names + descriptions)

async def classify_tool(
    text: str,
    options: list[str],
    descriptions: dict[str, str] | None = None,
    question: str = "Which option best matches?",
) -> dict:
    return await classify(_require(), text=text, options=options, descriptions=descriptions, question=question)


async def check_tool(text: str, condition: str) -> dict:
    return await check(_require(), text=text, condition=condition)


async def rate_tool(
    text: str,
    levels: list[str],
    question: str = "Score according to the rubric",
) -> dict:
    return await rate(_require(), text=text, levels=levels, question=question)


async def batch_judge_tool(items: list[str], criteria: list[dict]) -> dict:
    return await batch_judge(_require(), items=items, criteria=criteria)


async def system_one_raw_tool(state: Any, questions: dict[str, Any]) -> dict:
    return await system_one_raw(_require(), state=state, questions=questions)


async def health_tool() -> dict:
    return await health(_require())


def register_tools(mcp: FastMCP) -> None:
    """Register all 6 tools with explicit names + POSITIVE/NEGATIVE/EXAMPLE descriptions.

    Using add_tool(name=, description=) instead of @mcp.tool() decorator ensures
    the agent sees meaningful tool descriptions (the whole point of "tools with
    attitude" — without descriptions, agents misuse them).
    """
    mcp.add_tool(classify_tool, name="classify", description=DESC_CLASSIFY)
    mcp.add_tool(check_tool, name="check", description=DESC_CHECK)
    mcp.add_tool(rate_tool, name="rate", description=DESC_RATE)
    mcp.add_tool(batch_judge_tool, name="batch_judge", description=DESC_BATCH_JUDGE)
    mcp.add_tool(system_one_raw_tool, name="system_one_raw", description=DESC_SYSTEM_ONE_RAW)
    mcp.add_tool(health_tool, name="health", description=DESC_HEALTH)


def create_mcp(
    bearer_token: str | None = None,
    transport_security: "TransportSecuritySettings | None" = None,
) -> FastMCP:
    """Create the FastMCP instance. If bearer_token is set, enforce Bearer auth on HTTP.

    transport_security: defaults to allowing 127.0.0.1, localhost, and 100.86.140.31
    (this server's Tailscale IP). FastMCP enables DNS rebinding protection by default
    which rejects any Host header not in allowed_hosts — without this override, clients
    connecting to the Tailscale IP get 421 Misdirected Request.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    if transport_security is None:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "100.86.140.31:*"],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://100.86.140.31:*",
            ],
        )

    kwargs = {
        "name": "jev-mcp",
        "transport_security": transport_security,
    }
    if bearer_token:
        # FastMCP requires AuthSettings when token_verifier is provided. The OAuth-flavored
        # fields are required by the validator but we don't run an OAuth server — just
        # bearer middleware (set auth_server_provider=None keeps OAuth endpoints disabled).
        from mcp.server.auth.settings import AuthSettings

        kwargs["token_verifier"] = BearerTokenVerifier(bearer_token)
        kwargs["auth"] = AuthSettings(
            issuer_url="http://127.0.0.1:8765/",  # placeholder; no OAuth flow served
            resource_server_url="http://127.0.0.1:8765/mcp",
            required_scopes=[],
            validate_token_resource=False,
        )
    return FastMCP(**kwargs)


# --- initialization ---

def init(
    api_key: str,
    log_path: str,
    bearer_token: str | None = None,
    threshold_act: float | None = None,
    threshold_confirm: float | None = None,
    state_cap_chars: int | None = None,
    session_budget_usd: float | None = None,
    input_token_rate_usd: float | None = None,
    cache_capacity: int | None = None,
    log_full_response: bool | None = None,
    model_version: str | None = None,
) -> tuple[ServerDeps, FastMCP]:
    """Build ServerDeps + FastMCP instance. Called once at startup."""
    global _deps
    _deps = ServerDeps(
        client=JevClient(
            api_key=api_key,
            model_version=model_version or PINNED_MODEL_VERSION,
            proxy=os.environ.get("JEV_API_PROXY"),
        ),
        cache=LRUCache(capacity=cache_capacity or 1000),
        budget=SessionBudget(limit_usd=float(os.environ.get("JEV_SESSION_BUDGET_USD", session_budget_usd or 0.10))),
        decision_log=DecisionLog(
            path=log_path,
            log_full_response=bool(int(os.environ.get("JEV_LOG_FULL_RESPONSE", "0"))) if log_full_response is None else log_full_response,
        ),
        threshold_act=float(os.environ.get("JEV_THRESHOLD_ACT", threshold_act or 0.7)),
        threshold_confirm=float(os.environ.get("JEV_THRESHOLD_CONFIRM", threshold_confirm or 0.4)),
        state_cap_chars=int(os.environ.get("JEV_STATE_CAP_CHARS", state_cap_chars or 32000)),
        input_token_rate_usd=float(os.environ.get("JEV_INPUT_TOKEN_RATE_USD", input_token_rate_usd or 42e-9)),
        session_started_at=_utc_now_iso(),
        model_version=model_version or PINNED_MODEL_VERSION,
    )
    mcp = create_mcp(bearer_token=bearer_token)
    register_tools(mcp)
    return _deps, mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="jev-mcp")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    api_key = _load_api_key()
    if not api_key:
        raise SystemExit(
            "TYPESAFE_API_KEY not found. Set it in ~/.jev/.env (mode 0600) or in env."
        )

    bearer_token = load_bearer_token()
    if args.transport == "http" and not bearer_token:
        # HTTP mode without bearer = naked. Refuse.
        raise SystemExit(
            "JEV_MCP_TOKEN not found. For HTTP transport, set JEV_MCP_TOKEN env or "
            "write to ~/.jev/token (mode 0600). stdio transport does not require a token."
        )

    log_dir = os.environ.get("JEV_LOG_DIR") or os.path.expanduser("~/.local/share/jev-mcp")
    log_path = os.path.join(log_dir, "decisions.jsonl")

    _deps, mcp = init(api_key=api_key, log_path=log_path, bearer_token=bearer_token)

    if args.transport == "stdio":
        mcp.run()
    else:
        import uvicorn
        app = mcp.streamable_http_app()
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def _load_api_key() -> str | None:
    """Read TYPESAFE_API_KEY from env or ~/.jev/.env."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    env_path = os.path.expanduser("~/.jev/.env")
    if not os.path.exists(env_path):
        return None
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("TYPESAFE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


if __name__ == "__main__":
    main()