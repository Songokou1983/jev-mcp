# jev-mcp

本地 MCP server，把 TypeSafe Jev（System One 决策模型）暴露成 Claude Code / Codex 等 MCP 客户端的原生 tool。

**定位**：agent-facing Jev — 推理链上的高频小决策（路由 / 分类 / 评分 / 校验），按调用付费。**不**替换高量生产流水线那种直连 API。

**状态** (2026-09-22): Phase 2 + 真机反馈 + Phase 3 部署完成。142/142 tests passing，server 跑在家里 `sgkstation` (Tailscale `100.86.140.31:8765`)，Claude Code + Codex client 均已联调。

## 架构

```
[Claude Code / Codex / 其他 MCP client]
        │ Tailscale (100.x)
        │ HTTP POST /mcp + Authorization: Bearer JEV_MCP_TOKEN
        ▼
[server: sgkstation] ──HTTPS──► api.typesafe.ai /v1/systemone
       systemd --user service       Authorization: Bearer TYPESAFE_API_KEY
       port 8765, Tailscale IP only
```

| 端 | 角色 | 配置 |
|---|---|---|
| **Server** | jev-mcp daemon | systemd `--user` unit `jev-mcp.service`，`enabled` + `Linger=yes`，bind `tailscale ip -4` |
| **Claude Code** | MCP client | `claude mcp add --transport http jev <URL> --header "Authorization: Bearer <token>"`（写 `~/.claude.json`，**不**是 `~/.claude/settings.json`） |
| **Codex** | MCP client | (TBD；本机已测可连) |
| **Same-machine dev** | stdio mode | `claude mcp add --transport stdio jev-local ...` |

## 4+1+1 工具

| 工具 | 用途 | 关键约束 |
|---|---|---|
| `classify(text, options, descriptions?)` | one-of-N 分类 | 2-255 options |
| `check(text, condition)` | yes/no + 置信度 | Noul 无 API confidence → server 算 `effective_confidence = \|noul-0.5\|\*2` |
| `rate(text, levels)` | 有序 rubric 打分 | 2-10 levels |
| `batch_judge(items, criteria)` | **多 item 独立**调用（防概率串扰） | cap 20 (≈6s 串行，避 MCP client timeout) |
| `system_one_raw(state, questions)` | 透传逃生舱 | model 也强制 pin 到 `jev-1.13.0` |
| `health()` | 真 `GET /v1/models` probe + session stats | |

每个工具有 POSITIVE/NEGATIVE/CONTRACT 三段描述（agent 自查 + 拒绝确定性逻辑）。详见 [docs/tools.md](docs/tools.md)。

## 7 个 server-side 特性

| # | 特性 | 默认 | 配置 |
|---|---|---|---|
| 1 | 置信度门控 | `act > 0.7` / `confirm > 0.4` / `escalate < 0.4`（prototype 偏保守） | `JEV_THRESHOLD_ACT` / `_CONFIRM` |
| 2 | 成本护栏 | 32k 字符 state cap（中文 ≈ 30k tokens，注意）+ `$0.10/session` | `JEV_STATE_CAP_CHARS` / `JEV_SESSION_BUDGET_USD` |
| 3 | 缓存 | sha256 LRU 1000，无 TTL；cache hit 仍落 log | hardcode |
| 4 | Key 管理 | `TYPESAFE_API_KEY` from `~/.jev/.env` 或 env | hardcode |
| 5 | 决策日志 | `~/.local/share/jev-mcp/decisions.jsonl` mode 0600，**隐私默认**（metadata only） | `JEV_LOG_FULL_RESPONSE=1` opt-in 落 response body |
| 6 | 模型版本 | `jev-1.13.0` hardcoded，client 不可覆盖 | hardcode |
| 7 | 重试 | 429/529 **+ transport error** 指数退避（tenacity，max 3，base 0.5s，max 4s） | hardcode |

**`httpx` 用 `trust_env=False`**，即**不认** `HTTP_PROXY`/`HTTPS_PROXY` 等 env 变量（修复 local HTTP proxy 间歇 TLS 失败的 bug，见 "已知坑"）。需要走代理时用**显式**的 `JEV_API_PROXY`（如 `http://127.0.0.1:7897`）—— 2026-10-21 起本机直连 api.typesafe.ai 会收到 451 区域封锁。

> ### ⚠️ 必须设置 `JEV_API_PROXY`
>
> 从 2026-10-21 起，**从本机直连 `api.typesafe.ai` 会返回 HTTP 451**
> `{"title":"Typesafe is not available in your region."}` —— 出口 IP 被区域封锁。
> **不设 `JEV_API_PROXY` = 直连 = 全部工具调用失败、`health()` 的 `api_reachable` 恒为 `false`。**
>
> `JEV_API_PROXY` 是 **jev-mcp 自己的变量**。标准的 `HTTPS_PROXY` / `HTTP_PROXY`
> **在这里不生效** —— `JevClient` 用 `httpx trust_env=False` 建 client（2026-09-22
> 的修复：绕开当时间歇 TLS 失败的本地代理），env proxy 永远不会被采纳。
> 代理必须显式传进去。
>
> 部署时（systemd drop-in）：
>
> ```ini
> [Service]
> Environment=JEV_API_PROXY=http://127.0.0.1:7897
> ```
>
> 前提：本地有一个可用的 HTTP 代理（本机 = Clash mixed port `127.0.0.1:7897`）。

## Server 部署

```bash
git clone https://github.com/Songokou1983/jev-mcp.git ~/jev-mcp
cd ~/jev-mcp
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# 1. TYPESAFE_API_KEY (server → api.typesafe.ai 用)
mkdir -p ~/.jev
echo "TYPESAFE_API_KEY=apikey_your_key_here" > ~/.jev/.env
chmod 600 ~/.jev/.env

# 2. JEV_MCP_TOKEN (client → server Bearer 鉴权用)
openssl rand -hex 32 > ~/.jev/token
chmod 600 ~/.jev/token

# 3. systemd --user service (替代 PM2，无需 sudo)
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/jev-mcp.service <<'EOF'
[Unit]
Description=Jev MCP server
After=network-online.target

[Service]
Type=exec
WorkingDirectory=/home/son_goku/jev-mcp
EnvironmentFile=%h/.jev/.env
ExecStart=/home/son_goku/jev-mcp/.venv/bin/python -m jev_mcp.server --transport http --host $(tailscale ip -4) --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now jev-mcp

# 3b. 代理（必需 —— 直连会被区域封锁 451；见上面「必须设置 JEV_API_PROXY」）
mkdir -p ~/.config/systemd/user/jev-mcp.service.d
cat > ~/.config/systemd/user/jev-mcp.service.d/proxy.conf <<'EOF'
[Service]
Environment=JEV_API_PROXY=http://127.0.0.1:7897
Environment=HTTPS_PROXY=http://127.0.0.1:7897
Environment=HTTP_PROXY=http://127.0.0.1:7897
Environment=NO_PROXY=127.0.0.1,localhost,::1,100.64.0.0/10,*.tail4e6642.ts.net
EOF
systemctl --user daemon-reload
systemctl --user restart jev-mcp

# 4. boot 持久化（要 sudo）
sudo loginctl enable-linger $USER

# 5. 验证
ss -tlnp | grep 8765
# 期望: 100.x.x.x:8765 (Tailscale IP only, NOT 0.0.0.0)

curl -s -o /dev/null -w "%{http_code}\n" http://$(tailscale ip -4):8765/mcp
# 期望: 401 (无 token)
```

## 客户端配置

### Claude Code (任意 Tailscale 内的 host)

```bash
# 从 server 端 ~/.jev/token 复制
claude mcp add --transport http jev http://<server-tailscale-ip>:8765/mcp \
  --header "Authorization: Bearer <paste-token-here>"

claude mcp list   # 应看到 jev ... ✔ Connected
```

**VSCode 用户**：调 `mcp__jev__*` 前必须 `Developer: Reload Window`（sub-agent 不继承 MCP）。

### 同机 stdio 开发

```bash
claude mcp add --transport stdio jev-local \
  --command /home/son_goku/jev-mcp/.venv/bin/python \
  --args -m --args jev_mcp.server --args --transport --args stdio
```

## 测试

```bash
.venv/bin/python -m pytest                  # 全套 142 个 (≈12s)
.venv/bin/python -m pytest tests/test_integration.py -v   # 真实 API (env-gated)
```

- **120 unit** (mocked via `respx`)
- **6 real-API integration**（需 `TYPESAFE_API_KEY` env，≈ $0.00006/次）
- **16 regression**：`transport_security`（FastMCP 421 bug）、`proxy_bypass`（httpx trust_env bug）、description naming（[POSITIVE]/[NEGATIVE]/[EXAMPLE]/[CONTRACT] 锁住）

成本：`$42 / 1B input tokens`（TypeSafe 官方公开价，输出免费）。

## 环境变量

| 变量 | 默认 | 用途 |
|---|---|---|
| `TYPESAFE_API_KEY` | (env required) | api.typesafe.ai Bearer（server 从 `~/.jev/.env` 或 env 读） |
| `JEV_MCP_TOKEN` | (env required) | 客户端 Bearer（server 从 `~/.jev/token` 读） |
| `JEV_THRESHOLD_ACT` | `0.7` | act 阈值 |
| `JEV_THRESHOLD_CONFIRM` | `0.4` | confirm 阈值 |
| `JEV_STATE_CAP_CHARS` | `32000` | state 字符上限（CJK 1 char ≈ 1 token） |
| `JEV_SESSION_BUDGET_USD` | `0.10` | session USD 上限（session = 单次 server 进程） |
| `JEV_INPUT_TOKEN_RATE_USD` | `42e-9` | per-token 精确成本 |
| `JEV_LOG_FULL_RESPONSE` | (unset) | 设 `1` 启用 response body 落盘 |
| `JEV_BATCH_CONCURRENCY` | `1` | batch_judge 并发 worker 数（1=串行） |
| `JEV_BYPASS_CACHE` | (unset) | `1` = 跳过缓存（Phase 3 一致性监控用） |
| `JEV_API_PROXY` | (unset) | **本项目自定义变量**（不是标准 `HTTPS_PROXY`）。显式代理，如 `http://127.0.0.1:7897`。**不设 = 直连；直连已被区域封锁（451），本机必须设** |

## 安全清单

- [x] Server bind Tailscale IP（`tailscale ip -4`），**不** `0.0.0.0`
- [x] HTTP bearer auth（无 token 401）
- [x] FastMCP `transport_security` 白名单 Tailscale IP（修"Invalid Host header"→ 421）
- [x] `httpx trust_env=False`（不认 env proxy）；要走代理用显式 `JEV_API_PROXY`
- [x] systemd `Linger=yes`（boot 持久化）
- [x] `~/.jev/.env` mode 0600，`decisions.jsonl` mode 0600
- [x] Decision log 默认 metadata only（`JEV_LOG_FULL_RESPONSE=1` opt-in）
- [x] HTTP mode startup fail-fast：无 `JEV_MCP_TOKEN` → `SystemExit`
- [ ] 可选加固：firewall 拒 8765 来自非 Tailscale interface

## 设计原则

- **Simplicity First**：YAGNI；200 行解决不写 2000 行
- **带立场的工具**：server 固化最佳实践（batch 隔离、action gate、cache），agent 犯错空间小
- **POSITIVE/NEGATIVE/CONTRACT 三段描述**：每个 tool description 明确 when-to-use + when-NOT + 返回契约
- **Public repo**：TYPESAFE_API_KEY / JEV_MCP_TOKEN 不入库（`.gitignore` 挡 `.env` `.jev/` `decisions.jsonl`）

## 已知坑（已修复，列出来避免 regress）

| 坑 | 修法 |
|---|---|
| FastMCP 默认 `transport_security` 只允许 `127.0.0.1`/`localhost`，Tailscale IP 被拒 → 421 | `create_mcp()` 默认白名单 `127.0.0.1` + `localhost` + 当前 server 的 Tailscale IP |
| VSCode extension 不读 `~/.claude/settings.json.mcpServers`（用 `--mcp-config` flag 从 `~/.claude.json`） | 用 `claude mcp add`，**不**改 settings.json |
| `httpx trust_env=True` 默认 → local proxy (`127.0.0.1:7897`) 间歇 TLS 失败 → `ConnectError`，`str()==''` 看着像 server bug | `JevClient.send/probe` 都用 `trust_env=False` |
| 2026-10-21：直连 api.typesafe.ai 返回 **451** "Typesafe is not available in your region"（区域封锁，出口 IP 变了） | 显式 `JEV_API_PROXY=http://127.0.0.1:7897`；同时 `send/probe` 对 transport error 做重试（该代理路径约 2% 概率瞬时 `ConnectError`） |
| `batch_judge` cap 50 → 20：50×300ms ≈ 15s+ 触发 MCP client timeout | cap 20（≈6s 串行），`JEV_BATCH_CONCURRENCY` 可调 |
| Noul 无 API confidence | server 用 `effective_confidence = \|noul-0.5\|\*2`（Noul 比 Choice 偏保守 by design） |
| `cache_hit_rate` 测错东西（衡量输入重复度，不是一致性） | Phase 3 一致性监控：`JEV_BYPASS_CACHE=1` + 金标输入集重放 |

## 相关文档

- [PLAN.md](PLAN.md) — 设计方案 + 7 个拍板决策 + 16 项调研/反馈
- [CLAUDE.md](CLAUDE.md) — 项目指引（每个新 session 自动加载）
- [docs/tools.md](docs/tools.md) — 工具详细参考
- [TypeSafe 官方文档](https://docs.typesafe.ai/llms.txt)