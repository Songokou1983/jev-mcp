# Tools reference

6 tools exposed by the MCP server. All return structured dicts. All multi-dimension evaluations use ONE `/v1/systemone` HTTP call with multiple parallel questions (batching is free time-wise: measured 1 question ~350ms, 3 questions ~374ms).

**Server version pin**: `jev-1.13.0`. Client cannot override.

---

## Universal rule (applies to all tools)

**NEVER** use these tools for: arithmetic, counting, date/time formatting or comparison, exact string match, regex, SQL queries, deterministic lookups, or anything where a competent programmer could write the answer in <10 lines of code. Rule of thumb: if ground truth is in the data, don't pay model cost.

---

## `classify`

One-of-N classification. Single Choice primitive.

### When to use

- Pick exactly one option from a defined set (2-10 options)
- Options have clear labels and (optionally) descriptions
- Decision should be stable across repeated calls

### Arguments

| Name | Type | Required | Description |
|---|---|---|---|
| `text` | str | yes | What to classify |
| `options` | list[str] | yes | Option labels |
| `descriptions` | dict[str, str] | no | Map option → description (for nuanced boundaries) |
| `question` | str | no | Question framing (default: "Which option best matches?") |

### Returns

```python
{
    "choice": str,                       # chosen option
    "confidence": float,                 # 0-1
    "probabilities": dict[str, float],   # all options
    "action": "act" | "confirm" | "escalate",  # by threshold
    "tokens": {"input": int, "output": int},
    "cached": bool
}
```

### Tip

When 2+ options are similar (e.g., "complaint" vs "returns"), pass `descriptions` with `what` / `not_for` / `examples` per option. Costs ~50 input tokens per option but improves accuracy on borderline cases significantly.

---

## `check`

Yes/no with calibrated probability. Single Noul primitive.

### When to use

- Binary decision where you want the probability, not just a bool
- Want to know "how sure" (vs hardcoded threshold)
- e.g., "Does this contain a credential?", "Is this a destructive command?", "Should we escalate?"

### Arguments

| Name | Type | Required | Description |
|---|---|---|---|
| `text` | str | yes | State to evaluate |
| `condition` | str | yes | The yes/no question |

### Returns

```python
{
    "probability": float,                # 0-1, >0.5 = yes (raw Jev `noul` field)
    "verdict": "yes" | "no",             # derived from probability > 0.5
    "action": "act" | "confirm" | "escalate",  # see Confidence gate below
    "tokens": {"input": int, "output": int},
    "cached": bool
}
```

**Note**: Jev's Noul response does NOT include `confidence` — only `noul` (probability of yes). The `action` field is computed by server as `effective_confidence = |noul - 0.5| * 2`, then run through the same threshold gates as Choice/Score. Rationale: distance from the uncertain midpoint (0.5) measures certainty, regardless of yes/no direction.

---

## `rate`

Ordered rubric scoring. Single Score primitive.

### When to use

- Score along an ordered dimension (relevance 0-2, urgency 0-4, quality 0-3)
- Need to distinguish between levels of the same dimension
- Want the position, not just a binary decision

### Arguments

| Name | Type | Required | Description |
|---|---|---|---|
| `text` | str | yes | State to evaluate |
| `levels` | list[str] | yes | Ordered labels, index 0 = lowest (2-10 levels) |
| `question` | str | no | Score question framing |

### Returns

```python
{
    "score": float,                      # 0.0 to len(levels)-1, probability-weighted position
    "score_label": str,                  # levels[int(round(score))]
    "confidence": float,
    "probabilities": dict[int, float],   # level index → probability
    "action": "act" | "confirm" | "escalate",
    "tokens": {"input": int, "output": int},
    "cached": bool
}
```

---

## `batch_judge`

Multi-item evaluation with **server-side iteration**. Prevents state bundling / cross-talk.

### When to use

- 2+ items to evaluate (each by same or different criteria)
- Want independent scoring per item (not a single combined judgment)
- Want server-controlled state isolation

### Why this exists (not just `classify` per item)

SKILL.md warns: "agents aren't great at writing questions". If you expose only raw primitives, agent might:
- Bundle multiple items into one state → probabilities get cross-contaminated (we saw this in testing)
- Write question that's too vague
- Use wrong primitive type

`batch_judge` takes structured items, server iterates with isolation. Agent can't bundle, can't write questions — only declares criteria per item.

### Arguments

| Name | Type | Required | Description |
|---|---|---|---|
| `items` | list[str] | yes | Each item to evaluate (**max 20 per call** — see Limits) |
| `criteria` | list[dict] | yes | One criterion per item. Length must equal `items` length. |

Each criterion dict (must match the item at same index):

```python
# Choice variant
{"type": "choice", "options": ["billing", "technical", "other"],
 "question": "Which department?"}

# Score variant
{"type": "score", "levels": ["low", "medium", "high"],
 "question": "Urgency?"}

# Noul variant
{"type": "noul", "condition": "Is this a return request?"}
```

### Returns

```python
{
    "results": [
        {
            "item_index": int,
            "item": str,
            "type": "choice" | "noul" | "score",
            "choice": str,             # for choice
            "score": float,            # for score
            "probability": float,      # for noul
            "confidence": float,
            "probabilities": dict,
            "action": "act" | "confirm" | "escalate"
        },
        ...
    ],
    "items_count": int,
    "tokens": {"input": int, "output": int},
    "total_latency_ms": float
}
```

### Concurrency

Server runs items **sequentially** by default. Sequential is the safe default because:
- Jev is ~300ms/item, 20 items ≈ 6s — predictable, fits under typical MCP client timeouts
- Concurrent could blow past API rate limits
- Phase 2 ships sequential + 20 cap; `JEV_BATCH_CONCURRENCY` env (1=serial, 5=5 workers) available for v2 if needed

### Limits

**20 items per call.** Rationale: serial execution means 20 items × ~300ms = ~6s; MCP clients often have tool-call timeouts (commonly 30s but can be tighter), so 50 items × 300ms ≈ 15s+ risks triggering client-side timeout or making the agent think the call is hung. For larger batches, chunk into multiple `batch_judge` calls.

---

## `system_one_raw` (escape hatch)

Raw passthrough to `/v1/systemone`. **Agent responsible for question design.**

### When to use

- Need multi-question parallel in ONE call (server doesn't expose this for arbitrary questions)
- Special composite scoring pattern not covered by other tools
- Debugging / exploration

### When NOT to use

- Any case the 4 core tools cover
- If you find yourself using this frequently, you're probably doing something the server should bake into a new tool

### Arguments

| Name | Type | Required | Description |
|---|---|---|---|
| `state` | `dict \| str` | yes | State object (will be JSON-serialized if dict) |
| `questions` | `dict[str, dict]` | yes | Raw question definitions, **keyed by question name** |

Each question dict (value in the `questions` map):

```python
{
    "type": "choice",                    # "choice" | "noul" | "score"
    "instructions": "What is X?",        # string | object | array | null
    "criteria": {...}                    # type-specific (see below)
}
```

**Note**: Per [docs.typesafe.ai/api.md](https://docs.typesafe.ai/api.md), the **question name is the key in the `questions` dict** — there is no `name` field inside the question object. Naming example: `"questions": {"is_urgent": {"type": "noul", ...}}`.

Criteria shape per type:

| Type | `criteria` shape |
|---|---|
| `choice` | `dict[str, str\|None]` — label → description (None = undescribed); 2-255 options |
| `score` | `list[str]` — ordered labels, index 0 = lowest; 2-10 levels |
| `noul` | `dict[str, str\|None]` with keys `"true"`/`"false"` (or None) — optional |

### Returns

Raw Jev response dict. Shape per question type:

| Type | Response field |
|---|---|
| `choice` | `.choice` (str), `.confidence` (float), `.probabilities` (dict) |
| `score` | `.score` (float), `.confidence` (float), `.probabilities` (dict), `.legend` (dict) |
| `noul` | `.noul` (float 0-1) — **no separate confidence** |

### Caveats

- **No** automatic confidence gate, cost guard, or caching on this path (those are server features applied at higher level)
- 32k character state cap still enforced (server-side)
- Session budget still tracked
- **Server overrides `model` field to `jev-1.13.0` even if client passes another** — version pin is mandatory
- Server adds automatic 429/529 retry+backoff (via tenacity), but does not retry on 4xx validation errors

---

## `health`

Server diagnostics.

### When to use

- Debugging "why is this slow / failing?"
- Checking session budget remaining before a big batch
- Confirming server is reachable after laptop reconnects to Tailscale

### Returns

```python
{
    "api_reachable": bool,
    "model_version": "jev-1.13.0",
    "session_tokens_used": int,
    "session_budget_usd": float,
    "session_remaining_usd": float,
    "cache_size": int,
    "cache_hit_rate": float,             # 0-1, this session
    "decisions_logged": int,
    "uptime_seconds": int
}
```

---

## Server-side features (apply to all tools except `health`)

### Confidence → action gate

Every tool response includes `action`. For `choice` and `rate`, server uses Jev's `confidence` field directly. For `check` (Noul), server computes `effective_confidence = |noul - 0.5| * 2` (see `check` tool docs).

| effective_confidence | action | meaning |
|---|---|---|
| `> JEV_THRESHOLD_ACT` (default 0.7) | `act` | Auto-execute |
| `JEV_THRESHOLD_CONFIRM` (0.4) – `JEV_THRESHOLD_ACT` (0.7) | `confirm` | Need human confirmation |
| `< JEV_THRESHOLD_CONFIRM` (0.4) | `escalate` | Route to human / stronger model |

Override via env: `JEV_THRESHOLD_ACT`, `JEV_THRESHOLD_CONFIRM`.

### Cost guards

- **32k character state cap**: `text` / `state` JSON-serialized length > 32 000 chars → refuse with error before hitting API (avoids accidental key burn). **CJK caveat**: English ~4 chars/token, Chinese ~1 char/token, so 32k Chinese chars ≈ 30k+ tokens and may push against Jev's state+questions input limit. Phase 2 to measure real Chinese ticket tokens and lower the cap (e.g., 16k chars) if needed
- **Session = single server process lifetime**: budget resets when server restarts. Rationale: PM2 crashloop would otherwise mean unbounded budget. `health()` exposes `session_started_at` / `uptime_seconds` / `session_tokens_used` / `session_cost_usd`
- **Default session budget**: `$0.10` (≈2 380 calls at 320 input tokens). Override: `JEV_SESSION_BUDGET_USD`
- **Precise per-call cost**: TypeSafe pricing is **$42 per 1B input tokens, output tokens free** (verified from official models page). Server computes `cost_usd = input_tokens * 42e-9`. Override per-token rate via `JEV_INPUT_TOKEN_RATE_USD` env (e.g., if pricing changes)

### Retry + backoff

- 429 (rate limit) and 529 (overloaded) responses → automatic retry with exponential backoff
- Config: max 3 retries, base 0.5s, max 4s (via `tenacity`)
- All other errors (4xx validation, 5xx server) → propagate to caller

### Cache

- LRU by **sha256(canonical_json(state) + canonical_json(questions) + model_version)**
  - Canonical JSON = `json.dumps(obj, sort_keys=True, ensure_ascii=False)`
- 1000 entries, in-memory, no TTL
- Pin to model means cache stays valid across server restarts within same model version
- `cached: true` in response when hit
- **Cache hit still writes to decision log** (with `cached: true`) — Phase 3 monitors `cache_hit_rate`; < 50% over 1 week may warrant downgrading to TTL'd cache (24h)
- Caveat: jev-1.13 has documented capability jaggedness (not directly a consistency issue, but worth monitoring)

### Decision log

Every call → JSONL append at `~/.local/share/jev-mcp/decisions.jsonl` (mode 0600):

**Default (privacy mode)** — metadata only, no response body:

```json
{
  "timestamp": "2026-09-21T16:45:23Z",
  "tool": "classify",
  "args_hash": "sha256:abc...",
  "latency_ms": 287,
  "tokens": {"input": 314, "output": 47},
  "cost_usd": 0.000015,
  "action": "act",
  "cached": false
}
```

**With `JEV_LOG_FULL_RESPONSE=1`** (debug mode) — also writes full `response` field:

```json
{
  ...,
  "response": {"choice": "billing", "confidence": 0.81, ...}
}
```

Privacy default rationale: `state` may contain credentials, PII, or destructive command payloads — logging the Jev response body risks leaking these.

Use the log for: reviewing tool/action distribution, cache hit rate, cost sustainability. Args (state + questions) are NOT logged by design — only their SHA-256 hash, for traceability without exposure.

---

## Cross-reference

- Design rationale: [PLAN.md](../PLAN.md)
- Project-level notes: [CLAUDE.md](../CLAUDE.md)
- Quickstart: [README.md](../README.md)
