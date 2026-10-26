# DSA 钉 commit 逐版升级 SOP

本文是 `vendor/daily_stock_analysis/`（下称 DSA）**逐版升级**的逐步流程。总览与运行方式见 [dsa-integration.md](./dsa-integration.md)。

**本仓不做跟随滚动更新。** 不跟踪上游 HEAD，不接 Dependabot 式自动 vendor 滚，不存在无人工选定的自动升级 bot。每一次升级都必须由人工选定一个 commit，并走 PR 审阅。升级完成前，对外只能说「DSA 升级 SOP」，不能说「已自动跟随上游」。

当前钉：`ce364e457aab288863a5707e7b3df79786ad07f2`（见下方「单一真源」）。

## 单一真源：四处钉

同一个 40 位 commit SHA 必须同时出现在下面四个位置，缺一处或数值不一致即视为升级失败：

| # | 位置 | 字段 |
|---|---|---|
| 1 | `backend/app/custom/dsa/catalog.py` | `UPSTREAM_COMMIT`（权威常量，`catalog_payload()["upstream_commit"]` 取其值） |
| 2 | `vendor/daily_stock_analysis/VENDOR.md` | `Upstream commit: \`…\`` |
| 3 | `docs/dsa-integration.md` 文首 | 第一段里的「提交 \`…\`」 |
| 4 | `THIRD_PARTY_NOTICES.md` | `Upstream commit: \`…\`` |

规则：

- 四处不一致 → `backend/tests/test_dsa_upgrade_pin.py` 变红，按失败处理，不得合入。
- 升级 PR 必须**四处同改**，不允许只改 catalog 就宣称升级完成。
- 任何文档、脚本、测试都**禁止**把 `latest`、`main`、`HEAD` 当作钉值；钉值只能是 40 位十六进制。
- 本仓可以「只交 SOP 不换上游版」，此时四处钉保持原值，一致性测继续锁住它。

## 逐步流程（七步）

### 1. 选定目标 commit（人工）

人工在上游仓库挑一个**已发布、已知稳定**的 commit，记录：

- 40 位 SHA；
- 上游提交日期；
- 一句话变更摘要（修了什么 / 加了什么）。

不要选「最新的那个」，也不要写脚本去取 HEAD。选定的 SHA 直接写进 PR 描述，评审据此核对。

### 2. 同步 vendor 快照

按 `vendor/daily_stock_analysis/VENDOR.md` 的 omit 树规则重新取快照，**不要**带入以下内容：

- `apps/dsa-web/`
- `apps/dsa-desktop/`
- 上游 `tests/`
- evaluation / 评测夹具
- `docs/assets/` 配图

保留的是 Python 服务本体：`main.py`、`api/`、`src/`、`data_provider/`、`bot/`、`strategies/`，以及 `LICENSE`。

**禁止改 vendor 里的上游文件来实现 TSP 的产品行为。** TSP 的集成代码始终在 `backend/app/custom/dsa/` 与 `frontend/src/custom/dsa/`，升级只换快照、不动集成语义。

### 3. 更新四处钉

把第 1 步选定的 SHA 同时写进上面「单一真源」表的四个位置。四处必须字面完全一致（同为小写 40 位十六进制）。

### 4. 重建 sidecar

Docker：

```bash
docker compose --profile dsa up --build
```

本地脚本等价：

```bash
./scripts/dsa.sh
```

```powershell
.\scripts\dsa.ps1
```

> 盘后复盘窗口（北京时间 15:30–16:00，含 `SCHEDULE_TIME`）不要重建 dsa 容器，会打断正在生成的复盘。

重建后确认容器起来了、`/api/v1/health` 通，再进下一步。

### 5. 跑钩子验收矩阵（H1–H5）

sidecar 由 `backend/app/custom/dsa/dsa_bootstrap.py` 启动，在 `main.py` 之前装五个钩子。每个钩子只有两种合法结果：**装载成功**，或**显式跳过并打日志**。任何「没装上、日志里也看不出来」都算失败，不得写成升级成功。

| ID | 钩子 | 装载点 | 成功信号 | 显式跳过信号 |
|---|---|---|---|---|
| H1 | `quant_evidence` | `install()`，env `TSP_QUANT_EVIDENCE_FILE` | 日志「已安装量化证据钩子：…」 | 未配 / `off` → 不追加，不影响启动 |
| H2 | `news_bridge` | `_install_news_bridge()` | `news_bridge.py` 在 → `install()` | 日志「未找到 news_bridge.py，DSA 不拉取 TSP 资讯」 |
| H3 | `etf_job` | `_install_etf_job()` | `etf_job.py` 在 → `install_import_hook()` | 日志「未找到 etf_job.py，DSA 不提供 ETF 轮动入口」 |
| H4 | `fund_flow_bridge` | `_install_fund_flow_bridge()` | `fund_flow_bridge.py` 在 → `install()` | 日志「未找到 fund_flow_bridge.py，DSA 不读取资金进出」 |
| H5 | `analysis_bridge` | `_install_analysis_bridge()` | `analysis_bridge.py` 在 → `install()` | 日志「未找到 analysis_bridge.py，DSA 不注入个股上下文」 |

钩子抛异常时 `main()` 会 `logger.exception(...)` 后继续启动 DSA——这是「不挡住面板」，**不等于**钩子成功。看日志确认每个钩子落在「已安装」还是「未找到」，两者都要能在日志里找到，不能只凭容器起来了就判定通过。

**Docker 挂载**：compose 的 dsa 服务把 `dsa_bootstrap.py`、`etf_job.py`、`news_bridge.py`、`analysis_bridge.py`、`fund_flow_bridge.py`、`quant_evidence.py`、`quant_evidence.yaml` 逐个只读挂进 `/opt/tsp/`。新增钩子文件时必须同步补一行挂载，否则容器内走「未找到」分支——那是显式跳过，不是装载成功。

### 6. 跑推送 / health 验收

`GET /api/dsa/status` 返回 `enabled` 与 `reachable`，两者语义不同，**不可互相顶替**：

- `enabled` 只看 `DSA_BASE_URL` 是否非空（`bool(base_url())`，默认值 `http://127.0.0.1:8000`）；
- `reachable` 必须来自真实 health（sidecar `/api/v1/health`）。

所以「URL 配了但 sidecar 没起来」是 `enabled=true` + `reachable=false`，面板据此显示未连接。health 失败不会抛崩主进程，只把 detail 带回去。

定时推送（`schedule`）这一步只验收「配置可读写、探活可用」：

- 不真发钉钉 / 飞书 / 企微；
- 不改生产 `.env` 里的 `BUY_SELL_SIGNALS_PERSIST` / `PUSH` / `DINGTALK`；
- 验收结论只能写「链路可配置、可探活」，**不能**写「推送已生产验证」。

### 7. changelog 一行

在 PR 描述（或本节下方的「变更记录」）写一行：`新钉 <sha>（上游 <日期>）：<摘要>`。同时说明本次是否有钩子面变化（新增 / 移除 / 改名）、是否补了 compose 挂载。

## 升级后自检

一次性 dry-run，不改工作树：

```bash
python3 scripts/dsa_upgrade_check.py --check
```

它会输出四处钉是否一致、钉格式是否为 40 位十六进制、五个钩子文件是否齐全，以及 compose 是否把五个钩子都只读挂到了 `/opt/tsp/`。退出码 `0` 表示一致，`1` 表示有不一致项——不一致就不要开 PR。加 `--json` 可拿到机器可读结果。

自动化测（CI 也会跑）：

```bash
cd backend && python -m pytest tests/test_dsa_upgrade_pin.py tests/test_dsa_upgrade_hooks.py \
  tests/test_dsa_upgrade_docs.py tests/test_dsa_upgrade_status.py tests/test_dsa_upgrade_compose.py -q
```

## 口径与边界

- DSA 是**旁路**服务，不嵌进 TSP 核心运行时；去掉 `--profile dsa`、清空 `DSA_BASE_URL` 即可卸载，升级流程不影响主面板。
- DSA 的结论**仅供研究**，不与 TSP 自有买卖信号混排、也不共享同一可信度；面板上的信号仍以自有链路为准。
- 密钥与 LLM/行情渠道优先用自有或官方渠道，本仓不提供中转商默认密钥，也不在升级流程里新开密钥托管路径。
- 升级只换快照与钉值，**不**借机把新市场、新策略设成生产默认，也**不**扩大 MCP / skill 的调用面。
- 红涨绿跌：升级验收若涉及价格涨跌展示，沿用面板的红涨绿跌口径。

## 变更记录

- 2026-10-26：建立本 SOP（P5）。钉保持 `ce364e457aab288863a5707e7b3df79786ad07f2`，未换上游版；补 dsa 服务 `fund_flow_bridge.py` 挂载，H4 在 Docker 下可装载。

---

*仅供研究，不构成投资建议；执行须人工确认。*
