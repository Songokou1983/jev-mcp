# jev-mcp

本地 MCP server，把 TypeSafe Jev（System One 决策模型）暴露成 Claude Code 的原生 tool。

**定位**：agent-facing Jev——给 Codex / Claude 这类客户端提供判断能力。**不是**替换高量生产流水线那种直连 API。

**架构**：server 跑在家里 desktop，Claude Code 跑在 laptop，Tailscale 连接。

## 状态

**Pre-implementation**（Plan v2 已定，调研发现 9 个问题已并入方案，代码未改）。`src/server.py` 是 v1 骨架（7 工具 + typesafe-sdk），Phase 2 会按 PLAN.md v2 改写（4+1+1 工具 + httpx 直连 + 7 server-side 特性，含重试 + 隐私默认 decision log）。

## 部署架构

```
laptop (Claude Code) ──Tailscale──► desktop (jev-mcp) ──HTTPS──► api.typesafe.ai
                                    port 8765, Tailscale IP only
                                    Bearer token + decision log
```

| 端 | 配置 |
|---|---|
| **Desktop** (server) | PM2 跑，绑 `tailscale ip -4`（如 `100.x.y.z:8765`），`--transport http` |
| **Laptop** (client) | `claude mcp add --transport http jev http://<desktop-ip>:8765/mcp --header "Authorization: Bearer <token>"`（写 `~/.claude.json`，**不**改 `~/.claude/settings.json`）|
| **同机** (开发) | server 支持 `--transport stdio`，laptop 用 `command:` 启动 |

## 快速开始（Phase 2 完成 + 你拍板后）

### Desktop

```bash
cd ~/jev-mcp
uv venv .venv
uv pip install --python .venv/bin/python -e .

# 配 key
echo "TYPESAFE_API_KEY=apikey_..." > ~/.jev/.env
chmod 600 ~/.jev/.env

# 配 token（laptop 调 server 用）
export JEV_MCP_TOKEN=$(openssl rand -hex 32)

# PM2 跑
pm2 start ".venv/bin/python src/server.py --transport http --host $(tailscale ip -4) --port 8765" \
  --name jev-mcp
pm2 save
pm2 logs jev-mcp
```

### Laptop

> ⚠️ **2026-09-22 update**: VSCode extension **does not read** `~/.claude/settings.json.mcpServers`. Use `claude mcp add` (writes to `~/.claude.json`, which both CLI and VSCode ext read).

```bash
# 同步 token 从 server: cat ~/.jev/token, 然后 paste
claude mcp add --transport http jev http://<desktop-tailscale-ip>:8765/mcp \
  --header "Authorization: Bearer <paste-token-here>"

# 验证
claude mcp list   # 应看到 jev

# 重启 VSCode 窗口（Command Palette → "Reload Window"）才能让 mcp__jev__* 出现
```

### 同机开发（laptop=desktop）

```bash
claude mcp add --transport stdio jev-local \
  --command /home/son_goku/jev-mcp/.venv/bin/python \
  --args -m --args jev_mcp.server --args --transport --args stdio
```
```

## 6+1 工具一览

| 工具 | 用在什么场景 |
|---|---|
| `classify(text, options, descriptions?)` | one-of-N 分类（带立场——不允许 agent 写 question） |
| `check(text, condition)` | yes/no + 置信度 |
| `rate(text, levels)` | 有序维度打分（0-3） |
| `batch_judge(items, criteria, ...)` | **server 逐条独立调用**（cap 20，≈6s 串行），防串扰 |
| `system_one_raw(state, questions)` | 透传逃生舱（agent 自负风险） |
| `health()` | API 连通性 + budget + 缓存命中率 |
| ~~`check_command_safety`~~ | **v2 移除**——bash safety 不属于 Jev 职责，agent 该自己用 `classify` + 规则判断 |

详细参数见 [docs/tools.md](docs/tools.md)。

## 7 个 server-side 特性

| # | 特性 | 默认 | 配置项 |
|---|---|---|---|
| 1 | 置信度门控 | act>0.7 / confirm>0.4 / escalate（prototype 偏保守） | `JEV_THRESHOLD_ACT`, `JEV_THRESHOLD_CONFIRM` |
| 2 | 成本护栏 | 32k 字符 state cap（**CJK 警示**：中文 32k 字符 ≈ 30k tokens），$0.10/session（session = 单次 server 进程），精确 cost = `input_tokens × $42e-9` | `JEV_SESSION_BUDGET_USD`, `JEV_INPUT_TOKEN_RATE_USD` |
| 3 | 缓存 | sha256 LRU 1000 条，无 TTL（cache hit 仍落 log） | （hardcode，Phase 4 可改 disk） |
| 4 | Key 管理 | `~/.jev/.env` 或 env | （hardcode） |
| 5 | 决策日志 | `~/.local/share/jev-mcp/decisions.jsonl` mode 0600，**默认 metadata only** | `JEV_LOG_FULL_RESPONSE=1` 启用 response body 落盘 |
| 6 | 模型版本 | `jev-1.13.0`（`system_one_raw` 也强制覆盖） | （hardcode，client 不可改） |
| 7 | 重试 | 429/529 自动指数退避（max 3，base 0.5s，max 4s） | （hardcode via tenacity） |

## 依赖

- `mcp < 2` — FastMCP 1.x（mcp 2.x 改名 `MCPServer`，破 API）
- `httpx` — 直接打 `/v1/systemone`（**不用 typesafe-sdk**——避免 SDK 默认重试和我们想要的可控重试混在一起）
- `uvicorn` — ASGI server for HTTP transport
- `tenacity` — 429/529 指数退避重试

## 环境

- `TYPESAFE_API_KEY` — desktop 上 `~/.jev/.env`，mode 0600
- `JEV_MCP_TOKEN` — desktop 端在 `~/.jev/token` (mode 0600)；laptop 端通过 `claude mcp add --header` 一次性传入（不再依赖 env 插值）
- `JEV_LOG_FULL_RESPONSE` — 默认不设（privacy 模式，只 metadata）；设 `1` 启用 response 全量落盘
- `JEV_BATCH_CONCURRENCY` — `batch_judge` worker 数，默认 `1`（串行）
- `JEV_THRESHOLD_ACT` / `JEV_THRESHOLD_CONFIRM` — action gate 阈值，默认 **0.7 / 0.4**（prototype 偏保守）
- `JEV_SESSION_BUDGET_USD` — per-session USD 上限，默认 0.10
- `JEV_INPUT_TOKEN_RATE_USD` — 精确 cost = `input_tokens × rate`；默认 **42e-9**（TypeSafe 官方价，输出 token 免费）
- `JEV_BYPASS_CACHE=1` — Phase 3 一致性监控用，重放金标输入不读 cache

## 安全清单（启动前）

- [ ] Server 绑 Tailscale IP（`tailscale ip -4`），**不**绑 `0.0.0.0`
- [ ] 不在路由器/防火墙开放 8765
- [ ] 可选加固：desktop 加 firewall 规则拒 8765 来自非 Tailscale interface（`iptables`/`ufw`，防 Tailscale down 时暴露）
- [ ] `~/.jev/.env` 权限 `0600`
- [ ] 决策日志 `~/.local/share/jev-mcp/decisions.jsonl` 权限 `0600`
- [ ] 启动后 `ss -tlnp | grep 8765` 确认监听地址正确
- [ ] 启动后 `curl http://<tailscale-ip>:8765/health` 验证 token 工作

## 设计原则

- **Simplicity First**：YAGNI，200 行解决不写 2000 行
- **带立场的工具**：server 固化最佳实践，agent 犯错空间小
- **本地优先**：本项目是给用户自己用，不发布
- **可降级**：单点失败不应影响 Claude Code 主循环（错误返回给 agent 自己处理）

## 已知约束

- Jev 是新产品（2026-09 推出），稳定性和 SLA 未独立验证
- jev-1.13 有 documented capability jaggedness（[model-jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md)）——cache 假设"同输入稳定"需 Phase 3 验证
- HTTP transport 需要 PM2 持久化（不能 nohup 跑丢）
- 所有工具调用都是真实 API（开发期 + 运行期都烧 key），用 mock 测试隔离成本；integration 测试 cap 在 $0.001/次
- Tailscale P2P 13ms（实测），HTTP over Tailscale 延迟可忽略

## 相关文档

- [PLAN.md](PLAN.md) — 设计方案 v2 + 7 个待讨论决策
- [CLAUDE.md](CLAUDE.md) — 项目级指引（新 session 自动加载）
- [docs/tools.md](docs/tools.md) — 工具详细参考
- [TypeSafe 官方文档](https://docs.typesafe.ai/llms.txt)
