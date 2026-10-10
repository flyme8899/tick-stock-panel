# 配置详解

所有配置从根目录 `.env` 读取(复制 `.env.example` 开始),也可在面板 **设置** 页面可视化修改。本文件解释每个配置项的作用。

部署相关配置(端口/密码/老 CPU 兼容)的实操见 [deployment.md](./deployment.md)。

---

## 数据源:TickFlow

```ini
TICKFLOW_API_KEY=              # 留空 = None 模式(历史日K免费);填 Key = 按订阅档位解锁
```

TickFlow 是内置默认数据源;同时支持插件化接入第三方数据源(YAML 声明自有接口见 [custom-data-source.md](./custom-data-source.md),插件开发见 [plugin-development.md](./plugin-development.md)),在面板 **设置 → 数据源** 切换。

- **留空(None 模式)**:通过 free-api 使用历史日 K(当日数据盘后 1-2 小时可用),**无需付费**即可体验核心选股/回测功能
- **填入 API Key**:按你的订阅档位解锁更多能力

### 实时行情按档位

| 档位     | 实时能力                                 |
| :------- | :--------------------------------------- |
| Free     | 自选页前 5 个标的实时监控(最低 6 秒刷新) |
| Starter+ | 全市场实时行情                           |
| Pro      | 分钟 K + 盘口                            |
| Expert   | WebSocket + 财务数据 + 全量分钟          |

> 完整能力矩阵见 [tickflow.org/pricing](https://tickflow.org/pricing/),高等档位含较低档全部权益。
> 在面板 **设置 → 凭据与能力** 点「重新检测」可查看当前档位标签。
>
> **档位仅适用于 TickFlow 数据源**。功能门槛的统一标准是"能力"(`kline.minute.batch`、`depth5.batch`、`financial` 等能力键):其他第三方/自定义数据源以声明的数据集能力为准,系统会按当前数据源配置自动合并判定,UI 提示一律以能力名表达,不再依赖 TickFlow 档位名。

### 全量分钟 (full_minute)

「全量分钟」是一项**独立能力**(能力键 `full_minute`,探测名 `intraday.universe`),与其他能力同样**可路由**:盘中把全市场当日 1 分钟 K 持续增量落盘到本地 `data/kline_minute/` 当日分区,分钟策略(`minute_filter`)与分时视图即可读到新鲜数据。接入方式二选一:

- **TickFlow Expert**:配置 Expert 档 Key,零配置即用(修复轮 `intraday.batch` + 稳态 `intraday.universe` 单请求增量)
- **插件/自定义源**:声明 `full_minute` 数据集并在 **设置 → 数据源 → 全量分钟** 路由到该源 — Python 插件实现 `get_intraday_batch`(必需)/`get_intraday_latest`(可选,未实现自动降级仅修复轮、节奏下限 60s);YAML 声明式源数据集配置与 `minute` 同形(仅修复轮语义)。契约细节见 [plugin-development.md](./plugin-development.md) 与 [custom-data-source.md](./custom-data-source.md)

接入步骤:

1. **设置 → 凭据与能力**(TickFlow 路径)配置 API Key(Expert 档),或在 **设置 → 数据源** 声明/安装提供 `full_minute` 的源并路由;点「重新检测」后能力列表出现「全量分钟」
2. **开启实时行情**后落盘服务自动启动;仅连续竞价时段(9:30–11:30 / 13:00–15:00)运行,午休/收盘自动暂停与恢复
3. 冷启动(如 10 点才开服务)自动触发**全天修复轮**,一次批量补齐 9:30 起的全部缺口;稳态走**增量轮**(默认 6 秒一轮,可配 3–120 秒),幂等合并滚出全天
4. 与盘后分钟同步写同一分区(`unique(symbol, datetime)` 幂等合并),互不冲突

说明:标的池为 A 股股票(CN_Equity_A),ETF 不在内(分时走批量补拉路径);覆盖滞后超阈值或连续空轮会自动再跑修复轮自愈。

### 分钟 K 列类型诊断

`scripts/check_minute_kline.py` 只读检查 `data/kline_minute/date=*/part.parquet`，对照 `kline_sync` 的落盘类型：`symbol` 为字符串，`datetime` 为无时区的 `Datetime(us)`，价格和 `volume` / `amount` 为 Float64。它不写、不删、不改任何数据文件。

开发环境与 `./dev.sh` 共用后端虚拟环境。在仓库根目录执行：

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/check_minute_kline.py
```

Docker 镜像不含 `scripts/`。compose 把宿主机 `./data` 挂到容器 `/app/data`，并强制 `DATA_DIR=/app/data`。在仓库根目录执行：

```bash
docker compose run --rm --no-deps --entrypoint uv \
  -v "$PWD/scripts/check_minute_kline.py:/tmp/check_minute_kline.py:ro" \
  app run --no-sync python /tmp/check_minute_kline.py
```

目录不对时加 `--path`，指向含有 `date=YYYY-MM-DD/part.parquet` 的那一层。ETF 分钟是 `data/kline_etf_minute`。

结论为「有问题」时，不要手改 parquet。诊断脚本本身不改数据。`kline_sync._write_minute_partition` 会读入已有 `part.parquet` 再与新数据纵向拼接；`volume` 为 Int64 时这次拼接会失败，所以坏文件还在原处时，直接调用同步通常改不了类型。

可选修复是 `scripts/repair_minute_kline.py`。默认只打印计划（日期、文件大小、行数、股票数、备份位置、将调用的重拉、限速估算），不创建备份、不改分区。真正执行要加 `--apply`，并在终端输入 `REPAIR`，或同时加 `--yes`。

它先看本机有没有正在写分钟分区的任务（`job_store` 里近期的 pending/running，以及 `GET /api/settings/minute-refresh/status` 的 `running`）。有的话拒绝执行。接口探不到且任务文件也不能证明空闲时，必须再加 `--force`。执行前应先停掉 `./dev.sh` 或容器里的分钟同步。

备份写到 `data/backup/kline_minute_repair_<时间>/`，在 `kline_minute` 外面，避免被 `**/*.parquet` 扫进去。核对 schema 和行数之后，才把坏的 `part.parquet` 移到备份下的 `displaced/`（不删除）。然后调用 `kline_sync.sync_minute_batch`，段末用 `_write_minute_partition` 原子写回。不走 `sync_and_persist_minute`，以免空时间清理和旧分区迁移碰到其他日期。

TickFlow 付费说明写的是「一年分钟级历史」，单次 `count` 最大 10000，现有同步按默认 20 个交易日切段并限速。一年窗口内的日期计划重拉；更早的日期不重拉，移走后立刻用备份做类型转换写回。重拉没有写回某一天时同样用备份做类型转换（Int64 → Float64）原子写回，不留空洞。仅转换类型补不回被整日覆盖丢掉的股票。结束时会再跑诊断并打印结论。状态在备份目录的 `status.json`，中断后再次 `--apply` 会接着做。`--rollback <备份目录>` 把备份里的原文件写回分区。

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/repair_minute_kline.py
PYTHONPATH=backend backend/.venv/bin/python scripts/repair_minute_kline.py --apply
PYTHONPATH=backend backend/.venv/bin/python scripts/repair_minute_kline.py --rollback data/backup/kline_minute_repair_<时间>
```

Docker 镜像不含 `scripts/`，要同时挂上诊断脚本（修复脚本按同目录加载它）。`docker compose run --no-deps` 时本机地址不是正在跑的 app 容器，探针用 `TSP_API_BASE=http://app:3018`（app 容器需要已经在跑；探不到就得加 `--force`）：

```bash
docker compose run --rm --no-deps --entrypoint uv \
  -v "$PWD/scripts/check_minute_kline.py:/tmp/check_minute_kline.py:ro" \
  -v "$PWD/scripts/repair_minute_kline.py:/tmp/repair_minute_kline.py:ro" \
  -e TSP_API_BASE=http://app:3018 \
  app run --no-sync python /tmp/repair_minute_kline.py
```

`--apply`、`--yes`、`--rollback` 加在脚本参数最后。先跑不加 `--apply` 的计划。

---

## AI(可选)

用于自然语言生成策略。**所有配置留空即跳过**,不影响核心功能。支持任意 OpenAI 兼容接口。

```ini
AI_PROVIDER=openai_compat              # openai_compat | ollama
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=                            # 留空 = 关闭 AI
AI_MODEL=deepseek-chat
AI_DAILY_TOKEN_BUDGET=500000           # 每日 token 预算上限
```

| 配置项 | 说明 |
| :--- | :--- |
| `AI_PROVIDER` | `openai_compat`(OpenAI 兼容,支持 DeepSeek / 通义 / OpenAI 等)或 `ollama`(本地模型) |
| `AI_BASE_URL` | 接口地址,如 DeepSeek `https://api.deepseek.com/v1` |
| `AI_API_KEY` | 留空则关闭 AI 功能 |
| `AI_MODEL` | 模型名,如 `deepseek-chat` |
| `AI_DAILY_TOKEN_BUDGET` | 每日 token 预算,超限后当日不再调用 |

接入示例见 [strategy.md](./strategy.md) 的「AI 生成策略」章节。

---

## 服务

```ini
HOST=0.0.0.0          # 开发服务监听地址 / Docker 主机绑定地址
PORT=3018             # 开发后端端口 / Docker 主机映射端口
LOG_LEVEL=INFO        # DEBUG | INFO | WARNING | ERROR
```

- `HOST`:`0.0.0.0` 监听所有网卡(容器/公网部署需要);仅本机用可设 `127.0.0.1`
- `PORT`:默认 `3018`;开发模式兼容显式的 `BACKEND_PORT` 覆盖,改端口后 SSH 转发命令也要同步改
- `LOG_LEVEL`:排查问题时改 `DEBUG`

---

## 数据

```ini
DATA_DIR=./data       # Parquet / DuckDB 数据存储目录
```

整个 `data/` 目录都不纳入 git —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据。

如需迁移数据,直接拷贝整个 `data/` 目录即可。详见 [deployment.md → 更新代码](./deployment.md#更新代码已部署用户必读)。

---

## 访问密码(公网部署)

```ini
AUTH_PASSWORD='你的密码'  # 至少 6 位;仅首次生效,已设过则不覆盖
# AUTH_USERS=             # 留空使用 data/users.json;也可填文件路径或内联哈希 JSON
```

面板首次设置访问密码时,出于安全考虑**仅允许本机或内网访问**(防公网陌生人抢先设置锁死面板)。公网服务器部署可通过此环境变量预置首个密码。
密码建议使用单引号包裹，Docker 启动时会把整个原始 `.env` 只读挂载到容器内 `/app/.env`，兼容已有的未加引号配置。容器可以读取其中的密钥但不能修改该文件，请保持主机文件权限为 `600` 并仅运行可信镜像。

登录页有用户名。`AUTH_PASSWORD` 对应旧版共享密码,用户名填 `admin` 或留空。多用户账号写在 `data/users.json`(或 `AUTH_USERS`),密码只存 Argon2id/bcrypt 哈希。添加账号:

```bash
cd backend && uv run python ../scripts/manage_users.py add alice
```

命令会生成强密码并只打印一次。各账号权限相同。

详细步骤、SSH 转发方案、重置密码方法见 [deployment.md → 访问密码设置](./deployment.md#访问密码设置公网部署必读)。

---

## 后端依赖 Extras(可选)

```ini
BACKEND_EXTRAS=             # 留空默认;legacy-cpu 兼容老 CPU
```

老 CPU 无 AVX2/FMA 支持时设为 `legacy-cpu`,会给 Polars 切到 `rtcompat` 运行时;需回测则 `legacy-cpu backtest`。Docker 构建和 `./dev.sh` / `.\dev.ps1` 都会读取此值并同步依赖。详见 [deployment.md → 老 CPU 兼容](./deployment.md#老-cpu-兼容avx2fma-缺失)。

---

## 决策工作台（可选）

侧栏「决策」调用 vendored daily_stock_analysis。变量、启动方式、上海时间定时任务和分享图见 [dsa-integration.md](./dsa-integration.md)。`DSA_BASE_URL` 为空字符串时页面保持未启用；服务没开时仍能打开页面，并显示未连接。`SCHEDULE_ENABLED`、`SCHEDULE_TIME`、`STOCK_LIST` 也可在决策页「定时推送」里保存。

## 多源资讯（可选）

热门事件和 DSA 情报桥的变量、宿主机采集见 [news-sources.md](./news-sources.md)。`NEWS_DWS_ENABLED`、`NEWS_ZSXQ_ENABLED`、`NEWS_IMA_ENABLED`、`NEWS_CLS_ENABLED`、`NEWS_WSCN_ENABLED`、`NEWS_ETF_FLOW_ENABLED` 默认留空，来源关闭。`NEWS_DSA_FEED_TOKEN` 留空时 DSA 不拉取。ima 需要 `IMA_CLIENT_ID` 与 `IMA_API_KEY`。ETF 申赎表格用 `VISION_AI_API_KEY`（可选 `VISION_AI_BASE_URL`、`VISION_AI_MODEL`，默认 `deepseek/deepseek-v4-flash-vision-exp`，备选 `glm-5.3-flash`），不复用 `AI_API_KEY`。视觉请求按图片分批，`max_tokens` 至少 8192，不关闭模型思考。

## 配置优先级

1. **面板设置页**(`设置 → ...`):UI 修改后立即生效,持久化到 `data/`
2. **`.env` 文件**:启动时读取
3. **环境变量**:Docker / 系统环境变量,优先级最高

> 多数配置可在面板设置页修改,无需手动编辑 `.env`。仅 AI Key、API Key 等敏感项建议放 `.env`(不提交到 git)。
