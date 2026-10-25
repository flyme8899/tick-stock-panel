# TSP 官方 Agent Skills · 技能清单（MANIFEST）

> **仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。**
>
> 本文件是**机读清单**：`backend/tests/skills/test_skills_boundary.py` 直接解析它做边界断言。
> 改本文件等于改 CI 门禁的输入，请连带跑一次 `python -m pytest tests/skills -q`。

**版本口径**：`v1 · 只读优先`。默认只读；唯一的写工具 `write_ext_rows` 挂**逐次人工确认**门。
**权限权威在面板 Token scope**，skill 只是分发面 —— 本清单不授予任何权限，也不另造交易能力。

---

## 1. 技能面

| 技能名 | 目录 | 场景 | 默认 scopes | 写操作 | 确认门标记 |
|---|---|---|---|---|---|
| `tsp-scanner` | `skills/tsp-scanner/SKILL.md` | 选股 / 标的检索 / 行情概览 | `read:market` `read:analysis` | 无 | — |
| `tsp-review` | `skills/tsp-review/SKILL.md` | 复盘 / 策略清单与详情 / 回测触发说明 | `read:analysis` | 无 | — |
| `tsp-monitor` | `skills/tsp-monitor/SKILL.md` | 监控告警 / 扩展表读 / 受控写 | `read:analysis` `read:ext` | `write_ext_rows` | **逐次人工确认** |

---

## 2. 映射表：MCP 12 工具逐条归属

> 工具名以 `mcp-server/src/tsp_mcp_server/server.py` 为权威（`@mcp.tool()` 装饰的函数名）。
> **12 条全部有归属，无「本拍不映射」项**。新增工具必须先在此登记，否则边界测会红（见 §4 T3）。

| # | MCP 工具 | 技能面 | scope | Open API 路径 | 读/写 | 确认门 |
|---|---|---|---|---|---|---|
| 1 | `search_instrument` | `tsp-scanner` | `read:market` | `GET /api/kline/instruments/search` | 读 | — |
| 2 | `get_kline` | `tsp-scanner` | `read:market` | `GET /api/kline/daily` | 读 | — |
| 3 | `get_index_kline` | `tsp-scanner` | `read:market` | `GET /api/index/daily` | 读 | — |
| 4 | `get_market_overview` | `tsp-scanner` | `read:market` | `GET /api/overview/market` | 读 | — |
| 5 | `get_regime_state` | `tsp-scanner` · `tsp-review` | `read:analysis` | `GET /api/regime/latest` | 读 | — |
| 6 | `list_strategies` | `tsp-review` | `read:analysis` | `GET /api/strategies` | 读 | — |
| 7 | `get_strategy_detail` | `tsp-review` | `read:analysis` | `GET /api/strategies/{id}` | 读 | — |
| 8 | `get_recent_alerts` | `tsp-monitor` | `read:analysis` | `GET /api/alerts` | 读 | — |
| 9 | `list_ext_tables` | `tsp-monitor` | `read:ext` | `GET /api/ext-data` | 读 | — |
| 10 | `query_ext_table` | `tsp-monitor` | `read:ext` | `GET /api/ext-data/{table}/rows` | 读 | — |
| 11 | `write_ext_rows` | `tsp-monitor` | `write:ext` | `POST /api/ext-data/{table}/ingest` | **写** | **逐次人工确认** |
| 12 | `run_backtest` | `tsp-review` | `run:backtest` | `POST /api/backtest/strategy/run` | 受控（**非下单**） | — |

### 2.1 `run_backtest` 的前置约束

对外宣称「该策略已过前视偏差审计 / 已无前视 / 信号可做」之前，**必须**有仓内 lookahead
夹具的通过记录。门禁在**仓内 CI**，不在 skill 热路径；本清单**不**重跑夹具、**不**改夹具代码。
详见 `docs/agent-skills-brief.md` §4 与 `docs/lookahead-fixture.md`。

### 2.2 `write_ext_rows` 的确认门

写入会**进入策略/回测数据面**。规则：未得到用户对**该次**写入的明确确认（复述表名 + 行数 +
关键字段后获同意），**不得调用**。本清单在「确认门」列与 `tsp-monitor` frontmatter 的
`metadata.write_requires_confirm: true` 两处**同时**标注。

---

## 3. 默认安装 scopes（只读优先）

```bash
# 只读（推荐默认；三面里两面的全部工具都够用）
export TSP_SCOPES="read:market,read:analysis,read:ext"

# 需要跑回测时再加（受控 scope，非下单）
export TSP_SCOPES="read:market,read:analysis,read:ext,run:backtest"

# 需要写扩展表时再加（会进入策略数据面，请自行确认必要性）
export TSP_SCOPES="read:market,read:analysis,read:ext,write:ext"
```

> **不存在**交易 scope。面板 Token 侧也不应授予 —— 本 skill 包不申请、不使用、不描述任何
> 委托/下单能力。

---

## 4. 机读约定（CI 依赖，改动须同步测试）

| 约定 | 值 | 谁在用 |
|---|---|---|
| 清单路径 | `skills/MANIFEST.md` | T1 / T3 / T5 / T6 |
| 技能目录 | `skills/tsp-*/SKILL.md` | T1 / T2 |
| frontmatter 最小字段 | `name` · `description` | T2 |
| 写工具必须带 | `metadata.write_requires_confirm: true` **且** frontmatter `metadata.write_tools` 列出它 | T5 / T6 |
| 交易名黑名单 | 见 `docs/agent-skills-brief.md` §5 | T4 |
| 负向语境豁免 | 正文里的禁令句（「禁止下单」等）不算命中 | 黑名单扫描实现 |
| 缺清单 / 空树 | **fail-closed 抛错**，不当作通过 | T7 |

---

## 5. 并列引用：DSA 个股分析面

`vendor/daily_stock_analysis/SKILL.md` 是另一条**并列**的 skill 面（个股深度分析），本拍只做
**形态引用**，不复制、不改其行为、不接线。见 `docs/agent-skills-brief.md` §6。

---

*本清单由 `backend/tests/skills/test_skills_boundary.py` 守护。任何绕过它的「临时放行」都会让 CI 红。*
*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。*
