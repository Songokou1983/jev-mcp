# jev-mcp 项目方案 (v2)

## 定位

**MCP server 是 "agent 用的 Jev"**——给 Codex / Claude 这类客户端提供判断能力。

适用场景：
- ✅ 原型期 / 中低量 / 有人盯着的场景
- ✅ agent 推理时需要做高频小决策（路由 / 分类 / 评分 / 验证）
- ❌ 高量生产流水线（那种直接代码连 Jev API，不走 MCP）

两者不冲突：MCP 是**额外接入层**，**不是**替换直连。

## 为什么用 MCP

| 方式 | LLM 怎么调 Jev |
|---|---|
| Bash 调 python 脚本 | LLM 自己构造命令、解析 JSON、用 Bash 工具绕一圈 |
| **MCP tool** | `mcp__jev__classify(text, options, ...)` 原生 tool 调用，参数结构化，agent 不用管传输 |

MCP 让 Jev 变成 **agent 推理链上的工具**，不是 HTTP client。

## 部署架构

**Server 跑在家里 desktop**，**Claude Code 跑在 laptop**，中间 Tailscale。

```
┌─────────────────┐    Tailscale     ┌──────────────────────┐
│  laptop         │ ◄──────────────► │  desktop (home)      │
│  Claude Code    │  HTTP /mcp       │  jev-mcp server      │
│  mcp client     │  port 8765       │  + TYPESAFE_API_KEY  │
│                 │  Bearer token    │  + decision log      │
└─────────────────┘  100.x.y.z       └──────────┬───────────┘
                                              │
                                              ▼
                                       TypeSafe API
                                       (api.typesafe.ai)
```

**Transport**：MCP `streamable-http`（不是 stdio）
**Bind**：Tailscale IP only（如 `100.x.y.z:8765`），**不**绑 `0.0.0.0`
**Auth**：Tailscale 设备信任 + 可选 bearer token
**Encryption**：Tailscale WireGuard 自带，HTTP over Tailscale 已是加密——不另加 TLS

### 同机开发怎么办

`server.py` 同时支持 stdio（同机用，最快）+ HTTP（远程用）：
- `jev-mcp --transport stdio` → 同机 Claude Code
- `jev-mcp --transport http --host <tailscale_ip> --port 8765` → 远程

## 工具设计（v1: 4 + 1 + 1）

### 4 个核心工具（带立场，固化最佳实践）

| 工具 | Jev primitive | 单条 vs 批量 | 防坑点 |
|---|---|---|---|
| `classify(text, options, descriptions?)` | Choice | 单条 | agent 不会把多条塞 state 串扰 |
| `check(text, condition)` | Noul | 单条 | 是/否 + 置信度 |
| `rate(text, levels)` | Score | 单条 | 有序维度打分 |
| `batch_judge(items, criteria, ...)` | 多 primitive | **server 内部逐条独立调用** | **强制防串扰**——agent 想串扰都串扰不了 |

**为什么不暴露"自由组合 questions"接口**：SKILL.md 明说"agent 不擅长写 questions"——raw passthrough 把最难的设计问题甩给 agent，还容易出现"一条 state 多 questions"导致的串扰（实测见过）。

### 1 个逃生舱

| 工具 | 用途 |
|---|---|
| `system_one_raw(state, questions)` | **透传 SDK**——高级用法，agent 自负风险。**model 也硬编码 pin `jev-1.13.0`，不接受 client 覆盖**（与 §版本 pin 一致）。`questions` 是 `dict[str, dict]`，name 是 dict key，不是 question 内字段（参 [docs/typesafe.ai/api.md](https://docs.typesafe.ai/api.md)） |

### 1 个调试工具

| 工具 | 用途 |
|---|---|
| `health()` | 返回 API 连通性、剩余 session budget、缓存命中率 |

### 工具描述统一格式（每个工具 description 必含 3 段）

参考 minimax 的 "You MUST use..." 风格：

```
[POSITIVE]  Use this tool when you need to <scenario>.
[NEGATIVE]  **NEVER** use this tool for: arithmetic, counting, date/time,
            exact string match, regex, SQL queries, or anything where a
            competent programmer could write a deterministic answer in
            <10 lines of code. Rule of thumb: if ground truth is in the
            data, don't pay model cost.
[EXAMPLE]   classify(text="user wants refund", options=["billing","returns","other"])
```

## Server-side 真正值钱的逻辑（7 个，agent helper 脚本做不了）

### 1. 置信度门控（response 多带 `action` 字段）

不只是返回 probability，还按风险分档建议动作：

| confidence | action | 含义 |
|---|---|---|
| `> THRESHOLD_ACT` | `act` | 自动执行 |
| `THRESHOLD_CONFIRM` – `THRESHOLD_ACT` | `confirm` | 需要人确认 |
| `< THRESHOLD_CONFIRM` | `escalate` | 升级到人/更强模型 |

默认 `THRESHOLD_ACT=0.7` / `THRESHOLD_CONFIRM=0.4`（用户拍板的保守值，prototype 期宁可多 confirm 不要假 act），env 可改。

**Noul 特殊处理**：Jev API 对 Noul 不返回 `confidence`，只有 `noul`（0-1，>0.5=yes）。Server 把 `effective_confidence = |noul - 0.5| * 2` 作为 Noul 的 effective confidence，再走上面三档门控。**Noul 的 action 表态语义**：effective_confidence 反映"远离模糊中点的距离"——noul=0.95 → 高确定 yes；noul=0.51 → 高不确定；noul=0.05 → 高确定 no。

**Noul vs Choice 不对称性**（调阈值前必读）：`|noul - 0.5| * 2` 在 noul 接近 0.5 时**快速衰减到 0**——noul=0.6 的 eff_confidence = 0.2，按默认阈值 (act=0.7 / confirm=0.4) 直接 escalate。而 Choice 的 confidence 0.6 是直接数字，仍然 confirm。同样阈值下 Noul 比 Choice **激进得多地往 escalate 偏**。这是想要的安全姿态（不确定就升级），但调阈值时要意识到 Noul 偏保守是 by design 不是 bug。如果业务希望 Noul 跟 Choice 对齐，需要单独为 Noul 放宽阈值（如 Noul 的 `JEV_NOUL_THRESHOLD_CONFIRM=0.3`）。v1 不分用，Phase 3 看实际分布再决定。

### 2. 成本护栏

- **32k 字符 state cap**：state 序列化后超 32 000 字符直接拒。**注意**：英文 ~4 chars/token，中文 ~1 char/token，32k 中文字符 ≈ 30k+ tokens，会逼近 Jev `state + questions` 上限。工单数据若以中文为主，**建议实测后**把 cap 调低（如 16k 字符）或改按估算 token 数卡。Phase 2 实测中文 20 条工单取 token 数再定
- **每 session USD 预算**：默认 `$0.10`（≈2 380 次 320-input-token 调用），超了返回 error 而非静默失败。配置项 `JEV_SESSION_BUDGET_USD`
- **session 定义**：**单次 server 启动进程 = 一个 session**（PM2 一重启就清零预算；这样 PM2 crashloop 不会变无限预算）。`health()` 暴露 `session_started_at`、`uptime_seconds`、`session_tokens_used`、`session_cost_usd`。如需滚动窗口（24h），后续用 cron job reset 而不是改 server 逻辑
- **精确 per-call 成本**：TypeSafe 官方公开定价 **$42 / 1B input tokens，输出 token 免费**（[models 页](https://docs.typesafe.ai/models.md)，已核实）。Server 用 `cost_usd = input_tokens * 42e-9` 精确计算。env `JEV_INPUT_TOKEN_RATE_USD` 可覆盖（如涨价时）。原 "$0.000015/call" 是 ~360 input token 的近似估算，**机制上应用精确公式**

### 3. 429 / 529 重试

- 用 `tenacity` 实现指数退避（base 0.5s，max 4s，max 3 次）
- 覆盖所有非 raw 工具的 HTTP 调用
- 4xx 验证错误不重试，5xx 服务器错按 529 处理

### 4. 缓存

- **LRU by hash(state + questions + model_version)**
  - 哈希算法：sha256，state/questions 序列化为 canonical JSON（`json.dumps(obj, sort_keys=True, ensure_ascii=False)`）
- 内存，cap 1000 条
- TTL 无限（pin 死 jev-1.13.0，TypeSafe 官方 self-consistency cookbook 验证同输入评估一致）
- 命中直接返回，**不**重新计费
- **cache hit 仍落 decision log**（`cached: true` 标记）——便于 Phase 3 验证"同输入稳定"假设
- **Phase 3 一致性验证机制**：`cache_hit_rate` 衡量的是"输入重复度"，**不是**"同输入稳定性"（LRU hash 命中必然返回存好的 response）。要验证一致性，需要：
  1. 维护一份"金标输入集"（如 50 条手工验证过 ground truth 的输入）
  2. **绕过缓存**重放这 50 条（每条 server 加 `JEV_BYPASS_CACHE=1` 临时开关）
  3. 比较响应与历史 response 的一致性
  4. Phase 3 weekly cron 跑这个，得到真一致性指标
- **已知 caveat**：jev-1.13 有 documented capability jaggedness（[model-jaggedness/jev-1.13.md](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md)）。Jaggedness 指能力维度差异，不直接否定一致性，但 Phase 3 的真一致性监控发现异常时降级为带 TTL（24h）

### 5. Key 集中管理

- server 端读 `TYPESAFE_API_KEY` env 或 `~/.jev/.env`
- **客户端零 key 配置**——只持有可选 bearer token

### 6. 决策日志

- 所有调用 JSONL 落盘 `~/.local/share/jev-mcp/decisions.jsonl`
- **隐私默认**（避免 state 含凭证/PII 时泄露）：
  - 默认记录 metadata only（timestamp, tool, args_hash, latency_ms, tokens, cost_usd, action, cached）
  - `response` body **不**默认落盘
  - 设 `JEV_LOG_FULL_RESPONSE=1` 才记录完整 response（debug 模式）
- 每条 schema（默认）：
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
- 文件权限 `0600`
- 月度归档建议（手动 `gzip` + rename，**不**自动删）
- **人工审查重点**：看 tool 分布、action 分布、cache_hit_rate——review 阈值是否合理

### 7. 版本 pin

- `model="jev-1.13.0"` hardcode
- **禁止** client 覆盖——防止模型升级后行为漂移
- `system_one_raw` 也强制覆盖（即使 client 在 raw payload 里塞 `model` 字段，server 也改写为 `jev-1.13.0`）

## 文件结构

```
jev-mcp/
├── PLAN.md              # 本文档
├── CLAUDE.md            # 项目级指引（新 session 自动加载）
├── README.md            # 简介 + 快速开始
├── pyproject.toml       # 依赖：mcp<2, httpx, uvicorn, tenacity  （NO typesafe-sdk）
├── .env.example         # 环境变量模板
├── src/
│   └── server.py        # MCP server，4+1+1 工具 + 7 server-side 特性
├── tests/
│   ├── test_primitives.py     # 4 个核心工具的 mock 单测
│   ├── test_batch_judge.py    # 防串扰验证
│   ├── test_server_features.py # gate/cache/budget/log
│   └── test_integration.py    # 真实 API 各 1 次（jail budget cap）
└── docs/
    └── tools.md         # 每个工具的详细参数 + 决策逻辑
```

## 集成方式

### Desktop 端（server）

```bash
cd ~/jev-mcp
uv venv .venv
uv pip install --python .venv/bin/python -e .

# 配 key
echo "TYPESAFE_API_KEY=apikey_..." > ~/.jev/.env
chmod 600 ~/.jev/.env

# 配 token（laptop 调 server 用）
export JEV_MCP_TOKEN=$(openssl rand -hex 32)

# PM2 跑（绑 Tailscale IP）
pm2 start ".venv/bin/python src/server.py --transport http --host $(tailscale ip -4) --port 8765" \
  --name jev-mcp
pm2 save
pm2 logs jev-mcp
```

### Laptop 端（Claude Code settings.json）

```json
{
  "mcpServers": {
    "jev": {
      "type": "http",
      "url": "http://<desktop-tailscale-ip>:8765/mcp",
      "headers": { "Authorization": "Bearer ${env:JEV_MCP_TOKEN}" }
    }
  }
}
```

`JEV_MCP_TOKEN` 也要在 laptop 的 `~/.claude/settings.json` env 段配置（同步方式：1Password / pass / 手动复制）。

### 同机开发（desktop 直接跑 Claude Code）

```json
{
  "mcpServers": {
    "jev-local": {
      "command": "/home/son_goku/jev-mcp/.venv/bin/python",
      "args": ["/home/son_goku/jev-mcp/src/server.py", "--transport", "stdio"]
    }
  }
}
```

## 安全清单（启动前必走）

- [ ] Server 绑 Tailscale IP（`tailscale ip -4`），**不**绑 `0.0.0.0`
- [ ] 不在路由器/防火墙开放 8765
- [ ] Tailscale ACL（可选加强）：`{"src":["laptop-device"],"dst":["desktop-device:8765"]}`
- [ ] Bearer token 在 env 里，laptop 端只读 env
- [ ] `~/.jev/.env` 权限 `0600`
- [ ] 决策日志 `~/.local/share/jev-mcp/decisions.jsonl` 权限 `0600`
- [ ] 启动后 `tailscale status` + `ss -tlnp | grep 8765` 确认监听地址正确

## 阶段计划

### Phase 1 — 方案确认（本阶段）
- [x] 调研 Jev 生态
- [x] 决定 MCP 定位（agent-facing）
- [x] 决定工具粒度（4+1+1 带立场）
- [x] 决定 server-side 7 特性（gate / cache / budget / key / log / version pin / retry）
- [x] 决定 transport（HTTP over Tailscale）
- [ ] **等你拍板本方案**

### Phase 2 — 实现（你拍板后）
- 改写 `src/server.py`：httpx 直打 `/v1/systemone`，4+1+1 工具 + 7 特性
- `pyproject.toml`：`mcp<2`, `httpx`, `uvicorn`（**不** typesafe-sdk）
- 跑 mock 单测 + 真实 1×6 integration（~$0.001 cap）
- Desktop 本地（stdio 或 `127.0.0.1` HTTP）验证

### Phase 3 — 部署 + 集成
- PM2 跑在 desktop，绑 Tailscale IP
- Laptop `~/.claude/settings.json` 配 HTTP MCP server
- 新 session 验证 6 个工具都能被 Claude 调到
- 跑 1 周 double-track（Jev MCP + 直连 GPT-4）验证决策质量

### Phase 4 — 优化（可选，看 Phase 3 结果）
- HTTP/SSE 升级支持多客户端（如果要给其他人/机器用）
- 缓存持久化（disk LRU）
- 决策日志可视化 dashboard

## 调研阶段发现的问题（已并入上述方案）

调研 [docs.typesafe.ai/api.md](https://docs.typesafe.ai/api.md) + [primitives/advanced.md](https://docs.typesafe.ai/primitives/advanced.md) + jev-1.13 jaggedness 文档后，发现 v2 原稿有以下问题，已并入方案：

1. **`system_one_raw` questions shape 写错**：原方案写 `list[dict]` + 内嵌 `name` 字段，实际 API 是 `dict[str, dict]`，name 是 dict key。已修正
2. **Noul 无 API 置信度，原 action gate 未定义 Noul 语义**：Jev Noul response 没有 `confidence`，只有 `noul`。Server 用 `effective_confidence = |noul - 0.5| * 2` 走三档门控。已加到 §1
3. **成本计算缺公式**（**用户反馈**：TypeSafe 官方公开定价 **$42/1B input tokens，输出免费**）：Server 用精确公式 `cost_usd = input_tokens * 42e-9`，env `JEV_INPUT_TOKEN_RATE_USD` 可覆盖（涨价时）。原 "$0.000015/call" 估算量级没错但机制错。已加到 §2
4. **Cache hit 不落 decision log → 无法验证假设**：已加 `cached: true` 标记 + Phase 3 用"金标输入集 + bypass cache 重放"做真一致性监控（不是 `cache_hit_rate`，那个测的是输入重复度）。已加到 §4
5. **直连 httpx 不带重试，官方 SDK 是默认重试的**：server 加 `tenacity`（指数退避，max 3，base 0.5s，max 4s），独立成 §3（不是塞成本里）。已加
6. **Schema 边界未在文档层 enforce**：Choice 2-255 options、Score 2-10 levels、batch_judge **20** items（**用户反馈**：50 → 20，原 50×300ms ≈ 15s+ 会触发 MCP 客户端超时）、state 32k chars 在 server 层 enforce。已加到 [docs/tools.md](docs/tools.md)
7. **decision log 默认全量落 response 可能泄 state 敏感内容**：默认只 metadata，`JEV_LOG_FULL_RESPONSE=1` 才记录 response。已加到 §6
8. **system_one_raw 的 model 字段**：客户端可能通过 raw mode 覆盖 model 破坏 pin。server 在 raw mode 也强制覆盖为 `jev-1.13.0`。已加到 §7
9. **Cache key 算法未指定**：sha256 + canonical JSON（`sort_keys=True, ensure_ascii=False`）。已加到 §4

## 用户反馈阶段发现的问题（已并入上述方案）

用户在 [docs/typesafe.ai/models.md](https://docs.typesafe.ai/models.md) 核实后指出：

10. **CLAUDE.md 的 API 请求格式写错**：`questions` 写成 list 格式且标着 "to be verified"，实测是 `dict[str, dict]`，name 是 key 且 `model` 必填。已同步修正 CLAUDE.md（auto-load，**最危险**——家里 AI 接手 Phase 2 第一份读的就是它）
11. **定价公开**：TypeSafe 官方明确 **$42/1B input tokens，输出免费**。原方案"未公开"措辞错。精确公式替代估算，两个 rate env 简化为一个（`JEV_INPUT_TOKEN_RATE_USD`，默认 42e-9）。已加到 §2
12. **32k 字符 cap 对中文失准**：英文 ~4 chars/token，中文 ~1 char/token，32k 中文字符 ≈ 30k+ tokens。工单数据若中文为主，**Phase 2 实测后调 cap**。已警示标注到 §2 和 [docs/tools.md](docs/tools.md)
13. **session 没定义**：PM2 重启清零 vs 跑一个月 = 月预算。"**单次 server 进程 = 一个 session**" 写死，`health()` 暴露 `session_started_at` / `uptime_seconds`。已加到 §2
14. **`cache_hit_rate` 监控测错东西**：命中率衡量输入重复度，不是一致性。Phase 3 用"金标输入集 + `JEV_BYPASS_CACHE=1` 重放"做真一致性监控。已加到 §4
15. **batch_judge cap 50 → 20**：50×300ms ≈ 15s+ 触发 MCP 客户端超时。已降到 20（≈6s），文档说明分块。已加到 [docs/tools.md](docs/tools.md)
16. **Noul effective_confidence 不对称**：`|noul-0.5|*2` 在 noul 接近 0.5 时衰减快，Noul 比 Choice 激进得多地往 escalate 偏。by design（安全姿态），但调阈值时要意识到。v1 不分用，Phase 3 看分布。已加到 §1

待 Phase 2 实现时处理（不在文档层）：

- **pyproject 打包 bug**：`packages.find where=["src"]` + `entry point src.server:main` 矛盾，改标准 src layout（`src/jev_mcp/server.py`）
- **README 里的 `Bearer ${env:JEV_MCP_TOKEN}` 插值语法**：Claude Code HTTP MCP header env 展开支持因版本而异，**Phase 3 前实测**；不行就 token 直接写 settings.json
- **PM2 token 持久化**：从 `export` 改成落盘 `~/.jev/token` (0600)，重启机器不丢
- **启动 fail-fast**：`TYPESAFE_API_KEY` 缺失 + 日志目录 `mkdir -p`

## 拍板记录（用户确认采纳）

1. **阈值默认值**：用户采纳保守方案 → **`act=0.7` / `confirm=0.4`**。server 启动默认按此值（`JEV_THRESHOLD_ACT=0.7`, `JEV_THRESHOLD_CONFIRM=0.4`）。prototype 期宁可多 confirm 不要假 act
2. **session budget 默认**：`$0.10/session`（配合 §13 session = 单次进程的明确定义）
3. **缓存大小**：1000 LRU
4. **batch_judge 串行 vs 并发**：**串行 + cap 20**（用户反馈：50→20 防客户端超时）。`JEV_BATCH_CONCURRENCY` env 默认 1 串行，可调 5 并发
5. **决策日志保留期**：**永久 + 月度手动 gzip 归档**
6. **Tailscale ACL**：v1 不配
7. **PM2 日志**：默认即可

## 我现在没做（等你 override）

- ✗ `src/server.py` 没改（还是上一版 7 工具骨架）
- ✗ `pyproject.toml` 没改（还列了 typesafe-sdk）
- ✗ 没装 server、没写测试、没 PM2 启动
- ✗ 没碰 laptop settings.json

**你看完上面 7 个决策 + 整体方案，回我"OK / 改 X"，我开 Phase 2。**
