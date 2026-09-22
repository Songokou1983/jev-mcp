# CLAUDE.md

## Project

**jev-mcp** — local MCP server exposing TypeSafe Jev (System One decision model) as native tools for Claude Code. Python + httpx (no SDK), FastMCP 1.x. Pinned to `jev-1.13.0`.

**Positioning**: "agent-facing Jev". For Codex / Claude / similar clients to get judgment capability in prototype / medium-low volume / human-supervised scenarios. NOT for high-volume production pipelines (those connect Jev API directly).

**Core idea**: Claude Code's own "external System 1". Main model (System 2) does creative / reasoning / planning; Jev (System 1) handles high-frequency small decisions during the main model's reasoning loop — called as native MCP tools.

## Current state (as of 2026-09-22, Phase 3 deployed)

- ✓ `PLAN.md` — design proposal (v2 with HTTP transport, 4+1+1 tools, 7 server-side features including retry)
- ✓ `docs/tools.md` — per-tool reference
- ✓ `pyproject.toml` — Phase 2 fixed (deps: mcp<2, httpx, uvicorn, tenacity; standard `src/jev_mcp/` layout; entry point `jev_mcp.server:main`)
- ✓ `.env.example`
- ✓ `src/jev_mcp/` — implementation (action_gate, bearer, cache, budget, decision_log, jev_client, state_cap, server)
- ✓ 142 tests passing (120 unit + 6 real-API integration + 16 regression: transport_security, proxy_bypass, description naming)
- ✓ **Deployed**: systemd --user unit `jev-mcp.service` enabled + `Linger=yes` on `sgkstation` (Tailscale `100.86.140.31:8765`)
- ✓ **Tool descriptions wired**: 6 tools each have POSITIVE/NEGATIVE/CONTRACT descriptions (500-1000 chars), registered via `add_tool(name=, description=)`
- ✓ **Tool names match docs**: `classify`/`check`/`rate`/`batch_judge`/`system_one_raw`/`health`
- ✓ **Bearer token enforced**: `BearerTokenVerifier` via FastMCP `token_verifier`; HTTP mode fail-fast without `JEV_MCP_TOKEN`
- ✓ **transport_security**: Tailscale IP in `allowed_hosts` (default blocks 421)
- ✓ **`trust_env=False`** in `JevClient` (bypass broken local HTTP proxy)
- ✓ `health()` does live API probe via `GET /v1/models`
- ✓ `classify` validates options count (2-255); `rate` validates levels (2-10)
- ✓ Public on GitHub: https://github.com/Songokou1983/jev-mcp (3 commits, no secrets in tree)
- ✓ User confirmed 7 design decisions (PLAN.md §拍板记录) — **thresholds use conservative 0.7/0.4**

## Architecture

**Desktop runs server, laptop runs Claude Code, connected over Tailscale.**

```
laptop (Claude Code) ──Tailscale──► desktop (jev-mcp) ──HTTPS──► api.typesafe.ai
                                    ↑
                                    port 8765, Tailscale IP only
                                    Bearer token + decision log
```

**Transport**: MCP `streamable-http`. NOT stdio (cross-machine setup).
**Bind**: Tailscale IP only. Never `0.0.0.0`.
**Auth**: Tailscale device trust + optional bearer token.
**Encryption**: Tailscale WireGuard built-in. No additional TLS.
**Same-machine dev**: server supports `--transport stdio` too.

## The 4+1+1 tools (Plan v2)

| Tool | Jev primitive | What it does |
|---|---|---|
| `classify(text, options, descriptions?)` | Choice | One-of-N with optional per-option descriptions |
| `check(text, condition)` | Noul | Yes/no with calibrated probability |
| `rate(text, levels)` | Score | Ordered rubric scoring |
| `batch_judge(items, criteria, ...)` | multi-primitive | **Server iterates each item independently** — prevents state bundling / cross-talk |
| `system_one_raw(state, questions)` | raw passthrough | Escape hatch — agent responsible for question design |
| `health()` | n/a | API connectivity + session budget + cache hit rate |

**Critical design rule**: NO raw passthrough as the default. SKILL.md explicitly says "agents aren't great at writing questions". The 4+1+1 design bakes best practices into the server.

## The 7 server-side features (what makes this better than calling API directly)

1. **Confidence gating** — response includes `action` field: `act` (>0.7) / `confirm` (0.4-0.7) / `escalate` (<0.4). Thresholds configurable via `JEV_THRESHOLD_ACT` / `JEV_THRESHOLD_CONFIRM`. **Noul** has no API confidence — server computes `effective_confidence = |noul - 0.5| * 2`, which makes Noul much more conservative than Choice/Score (by design).
2. **Cost guards** — 32k character state cap (**CJK caveat**: Chinese ~1 char/token, so 32k Chinese chars ≈ 30k tokens; Phase 2 to measure and lower if needed). $0.10/session budget, where **session = single server process lifetime** (PM2 restart resets it). Per-call cost: `input_tokens × $42e-9` (verified official pricing, output free).
3. **Cache** — sha256 LRU by hash(state+questions+model_version), 1000 entries, no TTL. Cache hit still logs to decision log. **Phase 3 caveat**: `cache_hit_rate` measures input repetition, NOT response stability — use `JEV_BYPASS_CACHE=1` + golden input set for true consistency checks.
4. **Centralized key** — server reads `TYPESAFE_API_KEY` env or `~/.jev/.env`. Clients only need bearer token.
5. **Decision log** — JSONL at `~/.local/share/jev-mcp/decisions.jsonl`, mode 0600. **Privacy default**: metadata only (no response body) to avoid leaking state PII/credentials. Set `JEV_LOG_FULL_RESPONSE=1` for debug mode.
6. **Version pin** — `model="jev-1.13.0"` hardcoded, client cannot override (even via `system_one_raw`).
7. **Retry + backoff** — tenacity-based exponential backoff for 429/529 (max 3 retries, base 0.5s, max 4s). Required because direct httpx doesn't auto-retry (unlike the SDK).

## 7 design decisions (user confirmed)

| # | Decision | Confirmed value |
|---|---|---|
| 1 | Confidence thresholds | **`act=0.7` / `confirm=0.4`** (conservative for prototype) |
| 2 | Session budget default | `$0.10` per session (session = process lifetime) |
| 3 | Cache size | 1000 LRU |
| 4 | `batch_judge` concurrency | **Sequential, cap 20 items** (~6s, avoids MCP client timeout) |
| 5 | Decision log retention | Permanent + monthly manual gzip archive |
| 6 | Tailscale ACL | v1 not configured (device auth + bearer sufficient) |
| 7 | stderr / PM2 log retention | Default (100 lines) |

## Key facts

- **`TYPESAFE_API_KEY`** lives in `~/.claude/settings.json` env section on **desktop** (107 chars, prefix `apikey_28598c95...`). Persists across sessions — no manual export. Claude Code bash uses `--noprofile --norc`, so `~/.bashrc` env does NOT propagate.
- **TYPESAFE_API_KEY on server**: lives at `~/.jev/.env` on desktop, mode 0600. Server reads it.
- **Bearer token** (`JEV_MCP_TOKEN`): shared between desktop (server) and laptop (client). Server reads it from `~/.jev/token` (mode 0600). Client: stored in shell env or set via `claude mcp add` command (see Next steps).
- **MCP version**: FastMCP 1.x. In mcp 2.x, `FastMCP` was renamed to `MCPServer` — must pin `mcp<2`.
- **No typesafe-sdk dependency**: server uses `httpx` directly to POST `/v1/systemone` (per user design). This is a deviation from the v1 skeleton — Phase 2 rewrites `server.py`.
- **Jev API request format** (verified — official docs + live calls):
  ```
  POST /v1/systemone
  Authorization: Bearer <TYPESAFE_API_KEY>
  Content-Type: application/json
  {
    "state": <string | dict>,              # state to evaluate
    "model": "jev-1.13.0",                 # REQUIRED, server pins, client cannot override
    "questions": {
        "<question_name>": {               # name is the dict KEY, not a field inside
            "type": "choice" | "noul" | "score",
            "instructions": "...",
            "criteria": {...}              # type-specific shape
        }
      }
  }
  ```
  Response: `{"model": "jev-1.13.0", "answers": {"<name>": {...}}, "usage": {"input_tokens": N, "output_tokens": N}}`
- **Jev model version**: `jev-1.13.0` (verified available). **Server hardcodes this** — clients cannot override even via `system_one_raw` escape hatch.
- **Jev pricing** (verified from official models page, **not** an estimate): **$42 per 1B input tokens, output tokens free**. Per-call cost = `input_tokens × 42e-9`. Earlier "$0.000015/call" was a ~360-token estimate; precise formula should be used in accounting. Server uses this rate by default; override via `JEV_INPUT_TOKEN_RATE_USD` (e.g., for future price changes).
- **Tailscale**: server bind to `tailscale ip -4` (100.x.x.x). User's network: Tailscale P2P 13ms. No port forwarding needed.
- **Question types** (for reference, will be encoded in server.py):
  - `Choice`: criteria is `dict[str, str|None]` (label → description)
  - `Score`: criteria is `list[str]` (ordered)
  - `Noul`: criteria is `dict[str, str|None]` with keys `"true"`/`"false"`
- **Response shapes** (reference):
  - Choice: `.choice`, `.confidence`, `.probabilities` (dict of all options)
  - Score: `.score` (float), `.confidence`, `.probabilities`, `.legend`
  - Noul: `.noul` (float 0-1, >0.5 = yes)

## User conventions (auto-applied)

- Style: 简短直接, Chinese OK
- Karpathy "Simplicity First" — no speculative features
- No `rm -rf` without explicit confirm
- Don't read other user projects (`~/hermes-agent-self-evolution/`, `~/feishu-claude-pal/`, `~/HaGoKu/`) unless explicitly asked
- Don't re-implement existing services (e.g., user's local LLM at port 8080 is for sensitive tasks only)
- When unsure → ask, don't guess
- API unknown → surface to user, don't fill defaults

## Next steps (Phase 3 — deployment, requires user action on desktop)

Phase 2 implementation + integration tests all done in this session. Phase 3 requires the user's desktop machine access (this session is laptop-side, server code ready to deploy):

1. On desktop: copy/sync the project (or git push + pull on desktop)
2. On desktop: `uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"`
3. On desktop: persist `JEV_MCP_TOKEN` to `~/.jev/token` (mode 0600) — see env section
4. On desktop: PM2 launch:
   ```bash
   pm2 start ".venv/bin/python -m jev_mcp.server --transport http --host $(tailscale ip -4) --port 8765" --name jev-mcp
   pm2 save
   ```
5. On laptop: register MCP server via `claude mcp add` (writes to `~/.claude.json`, which Claude Code CLI **and** VSCode extension read at startup):
   ```bash
   claude mcp add --transport http jev http://100.86.140.31:8765/mcp \
     --header "Authorization: Bearer $(cat ~/.jev/token)"
   ```
   **Important**: editing `~/.claude/settings.json`'s `mcpServers` field directly does **not** register MCP servers for VSCode extension sessions (bug discovered 2026-09-22: VSCode ext spawns Claude binary with `--mcp-config` flag from `~/.claude.json`, ignoring `settings.json.mcpServers`). After running `claude mcp add`, **reload VSCode window** (Command Palette → "Reload Window") for the tools to appear. Verify with `claude mcp list` before reloading.
6. New Claude Code session: verify `mcp__jev__*` tools are callable via `health()` first
7. Phase 3 measurement: run `batch_judge` on a real Chinese ticket sample (20 items), measure actual tokens, decide if 32k char cap needs lowering

## Origin context (not in any code — for handoff only)

This project came from investigating TypeSafe Jev after the user asked me to "research jev". Investigation found:
- Jev is real (typesafe.ai exists, API works with key, ~300ms p50)
- 155+ ecosystem projects (fatwang2/awesome-jev)
- Official typesafe-ai skill plugin exposes 3 primitives (Choice/Score/Noul) + 4 patterns
- `system-one-adapter` is TypeSafe's official drop-in replacement: same `TypeSafeClient` interface backed by LLM APIs — useful for dev/prod parity
- Pricing: **$42/1B input tokens, output free** (verified, not an estimate — earlier "$0.000015/call" was a ~360-input-token rough estimate)

User then corrected me twice:
1. Don't get distracted by other agents — focus on THIS Claude Code session improving
2. Don't read user's other projects without explicit ask
3. Realized deployment: server runs on desktop, client on laptop, via Tailscale → transport is HTTP, not stdio

User review round 2 surfaced: (a) pricing is published, (b) CLAUDE.md's old API format (`questions: list`) was wrong and contradicted PLAN.md/tools.md, (c) 32k char cap overestimates token capacity for Chinese, (d) "session" was undefined, (e) `cache_hit_rate` measures repetition not consistency, (f) `batch_judge` cap 50 → 20 to avoid MCP client timeout, (g) Noul `effective_confidence` asymmetry by design, (h) pyproject packaging bug, (i) README env interpolation unverified.

This project is the synthesis.

## Related memory files (user-global, NOT in this project)

- `claude-code-skill-description-leads-with-problem.md` — general lesson about skill descriptions leading with problem-space keywords
- `proxy-pollutes-external-reachability-tests.md` — when checking network reachability, env `-u *_PROXY` (relevant for PM2 server bind check)
