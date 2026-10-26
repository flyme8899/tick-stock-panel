# Kronos 接旧实装（P6）

Kronos 是把 K 线当序列做**多路径采样预测**的模型。本仓接它，只接一件事：把既有
S3 研究包里已经算好的**分布特征**导进扩展表，当作风险标签候选摆出来。**不重训、
不跑全市场日批、不接买卖信号。**

本文写清：接的是什么、三态怎么判、schema 长什么样、门禁四前置、怎么导入、怎么卸。

上游材料（只读引用，不入仓）：

| 材料 | 位置 / sha256 |
|---|---|
| S3 features.csv | `a8e1bdb24b9092256af746bff6671e67ade19970e96712822c2198235052659c` |
| S3 predictions.csv | `1df4822f4f854f7bd9c7e5db4efd42af73616689fff2756241be6c776c0d1bc4` |
| S3 run_meta.json | `68a360341d1762f305c1a2862d529728d45302995a7b0d151bcc1851f27b7720` |
| S3 评估 BRIEF | `KRONOS_S3_EVAL_BRIEF.md`（**结论上限「仅提醒」，用途「只降档」**） |
| S2 评估审 | `REVIEW_20261021f.md`（h20 Rank IC NW **t=1.984 < 2.498 → 不通过**） |
| 权重目录 | 宿主机旁证（如 `/workspace/kronos_weights/`），**不入仓** |

## 一句话口径

**接旧骨架**：消费既有 S3 产物 → 扩展表分布特征 → 因子注册表里的模型预测因子族 →
默认关闭 + `enabled` ≠ `reachable` ≠ `gate_passed` 门闩。**过不了门禁不得进信号层。**
本拍**不**宣称 alpha，**不**说「信号可做」。

## 三态：enabled ≠ reachable ≠ gate_passed

这三件事分属三个维度，任何两个互相顶替都会出问题。

| 态 | 判据 | 默认 |
|---|---|---|
| `enabled` | `KRONOS_ENABLED` 为真 **且** 配了 `KRONOS_BASE_URL` 或 `KRONOS_BATCH_DIR` | **假** |
| `reachable` | sidecar `/health` 通 **或** 批成功标记在新鲜窗口内 | **假** |
| `gate_passed` | `data/kronos/gate.json` 存在、四项前置齐、未过期 | **假** |

三点设计取舍，都是有意的：

1. **`enabled` 默认假**，与 DSA / OpenBB 的 `enabled ≈ bool(URL)` 不同。那两个是常驻
   sidecar，配了 URL 就是配了；Kronos 默认没有部署物，用 `KRONOS_ENABLED=1` 显式打开，
   避免「从模板抄了一行就以为开了」。
2. **`reachable` 走「或」**。常驻 sidecar 看 health；离线批只看 `data/kronos/last_batch.json`
   的 `finished_at` 是否在窗口内（默认 48 小时，`KRONOS_FRESHNESS_HOURS` 可调）。
3. **`gate_passed` 只认显式记录**，不认环境变量。把门禁做成一行 env，等于谁都能开。

「URL 配了但 sidecar 没起来」= `enabled=true` + `reachable=false`，面板据此显示未连接。

## 门禁四前置

进信号层（BSS、自定义信号默认集、任何「可交易建议」口径）**必须同时**满足：

1. **费后检验口径一致** —— 与 TSP 研究规范同口径（买 6.6bp / 卖 11.6bp 等），记在
   `gate.json` 的 `cost_after_tax`（**非空字符串**，写清口径版本）。
2. **样本外 / walkforward 可复核** —— 记在 `out_of_sample`（记录位置，不是写个 `true`）。
3. **显式 `passed: true`** —— 逐字 `true`，不是 `1`、不是 `"true"`。
4. **用途不超出「风险降档」** —— `risk_downgrade_only: true`，`usage` 不得是
   `upgrade` / `add_position` / `increase` / `buy`。

抬升 S3「仅提醒」的结论上限，须**另口令 + 新评估包**。本拍在代码里没有这条路。

`gate.json` 示例（**不**随仓库提供，需人工按评估结论填写）：

```json
{
  "passed": true,
  "cost_after_tax": "TSP 研究规范 v1：买 6.6bp / 卖 11.6bp",
  "out_of_sample": "walkforward 2026H1，见 KRONOS_S3_EVAL_BRIEF.md",
  "risk_downgrade_only": true,
  "usage": "risk_downgrade_only",
  "expires_at": "2027-01-31T00:00:00+00:00"
}
```

过期、字段缺、JSON 坏、文件不在 —— 一律按 `gate_passed=false` 处理，原因写进 status
的 `gate_detail`。

## 扩展表 schema

配置 id `kronos_pred`，`mode: timeseries`，落 `data/ext_data/kronos_pred/timeseries/date=YYYY-MM-DD/`。

| 列族 | 列名 | 用途 |
|---|---|---|
| 键 | `symbol` | 按日分区，与 S3 一致 |
| 分布（主） | `agree_h5/h20`、`dir_entropy_h5/h20`、`p90_p10_h5/h20`、`std_h5/h20`、`mean_h5/h20`、`median_h5/h20` | 12 列，**用分布，不用单点当 alpha** |
| 元数据 | `n_paths`、`seed` | 可追溯，不参与信号 |
| 可选对照 | `pred_ret_h5/h20` | **仅研究对照列**，来自 predictions.csv |

字段名全 ASCII：`ext_factors` 用 `ext_<config>_<field>` 注册，DSL 标识符 ASCII-only，
中文名会被跳过注册。缺值保持 null，**缺日不填 0** —— 用 0 填分布列等于造出「完全一致 /
零分歧」的假信号。

## 导入

```bash
# 预演：只统计天数/行数/源 sha，不落盘
python scripts/kronos_import_s3.py --package /path/to/kronos_pred_s3_mini_n20_20261021 --dry-run

# 真导入（写 data/ext_data/kronos_pred/ 与 data/kronos/last_batch.json）
python scripts/kronos_import_s3.py --package /path/to/包

# 顺带校验上游 sha
python scripts/kronos_import_s3.py --package /path/to/包 \
  --expect features.csv=a8e1bdb24b9092256af746bff6671e67ade19970e96712822c2198235052659c
```

脚本三条纪律：

- **只读源**。导入前后比对源文件 sha256，变了就中止。
- **缺日不填 0**。没有的交易日不写分区，缺口打到 stderr。
- **不重训、不推理**。只搬既有产物。

导入会写批成功标记 `data/kronos/last_batch.json`，这是离线形态下 `reachable` 的来源。

## 因子注册与门闩

导入后 12 个分布列（+4 个可选/元数据列）自动注册为 `ext_kronos_pred_*`，与 Alpha158、
既有扩展因子走**同一套** IC / 分层 / 样本外门禁入口，不另开旁路。

门闩在 `backend/app/custom/kronos/gates.py`：

- `is_kronos_factor(id)` 按前缀 `ext_kronos_pred_` 认族（按前缀不按列名清单，加列不漏）；
- `assert_allowed_in_signals(ids)` 未过门禁时抛 `KronosGateError`，带明确原因；
- `filter_signal_fields(fields)` 给信号字段白名单做减法。

**本模块不 import `buy_sell_signals`，不往那里写任何东西。** BSS 接线与默认因子集本拍
零碰；门闩是「拦住别人接进来」，不是「自己接进去」。

## 主进程零 torch

主应用不 `import torch`、不 `import kronos`，主依赖表里也没有它们。G-T1 用静态 AST
扫描 + 依赖表检查 + 子进程 `sys.modules` 断言三重守住。

推理只有两条路：

- **离线批**：`scripts/kronos_import_s3.py` 导入既有产物（本拍用的就是这条）；
- **sidecar**：`docker/kronos/`，镜像自带 CPU 版 torch，默认不拉起。

sidecar 的 `/health` 只报状态，**没有推理接口** —— 推理接口属于 Opt-K1（全市场日批 /
GPU 调度），另等口令。本拍**不新跑推理**。

## 权重纪律

- 权重**不入仓**，也不打进 app 镜像；仓库里有测试守着（`.pt` / `.pth` / `.safetensors` /
  `.ckpt` / `.bin` 一个都不许有）。
- sidecar 用 `KRONOS_WEIGHTS_DIR` 只读挂宿主机目录，默认 `./data/kronos_weights`。
- 没准备权重时容器仍能起来，`/health` 如实报 `weights_ready=false`。

## 部署与卸载

默认 `docker compose up` **不**拉起 kronos（`profiles: ["kronos"]`，且没有别的服务
`depends_on` 它）。

启用：

```bash
# .env
KRONOS_ENABLED=1
KRONOS_BASE_URL=http://kronos:8200

docker compose --profile kronos up --build
```

卸载（三步任一即可，核心路径没有硬依赖）：

1. 清配置：`KRONOS_ENABLED=0` 或删 `KRONOS_BASE_URL` / `KRONOS_BATCH_DIR` → status 回到全关；
2. 去 profile：`docker compose up`（不带 `--profile kronos`）；
3. 删代码：删掉 `backend/app/custom/kronos/` 与 `docker/kronos/`，主进程照常启动。

扩展表数据留在 `data/ext_data/kronos_pred/`，删目录即清。

## 状态接口

`GET /api/kronos/status`（只读，带 `X-Research-Only: true`）：

```json
{
  "enabled": false,
  "reachable": false,
  "gate_passed": false,
  "detail": "未开启 KRONOS_ENABLED",
  "gate_detail": "未找到门禁记录（gate.json 不存在或不可解析）",
  "schema_version": "1",
  "ext_config_id": "kronos_pred",
  "source_refs": {"package": "kronos_s3_mini_n20_20261021", "features": "features.csv", "...": "..."},
  "research_only": true,
  "gate": {"record_present": false, "missing": ["cost_after_tax", "out_of_sample", "explicit_passed", "risk_downgrade_only"]}
}
```

另有 `GET /api/kronos/schema`（列清单）与 `GET /api/kronos/gates`（门禁自述）。
三个都是 GET，没有写接口、没有上游转发。

## 口径与边界

- **S2 不通过**：h20 Rank IC NW t=1.984 < 2.498。点预测列若导入，**仅作研究对照**，
  不得单独作为信号依据。
- **S3 结论上限「仅提醒」**，用途「只降档不加码」。即便门禁通过，也只代表可以当风险
  标签用，不代表可以当买入理由。抬升上限须另口令 + 新评估包。
- **不宣称 alpha**。本拍是接旧骨架，没有新收益、没有新回测、没有新结论。
- **不要**说「信号可做」「已上线进 BSS」「预测可上线」「已过门禁」。当前状态是：
  门禁未过，因子默认不进信号层。
- 红涨绿跌口径沿用面板。

---

*仅供研究，不构成投资建议；执行须人工确认。*
