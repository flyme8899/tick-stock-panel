# OpenBB sidecar 集成（P4）

本文固定 OpenBB sidecar 的版本、接口、状态语义与使用边界。本拍只提供 `research_only` 候选能力，状态为 `candidate/pending`；不代表生产部署或既有数据路由已经切换。

## 运行方式与依赖边界

- 主进程通过 `OPENBB_BASE_URL` 配置 sidecar 地址，自身**不安装任何 OpenBB Python 包**。
- 本地按需构建并启动：`docker compose --profile openbb up --build`。
- sidecar 固定使用 `openbb-core==2.0.1`、`openbb-platform-api==2.0.1`、`openbb-fred==2.0.0`，对应 OpenBB Platform **5.0.0**；管护仓库为 `openbq-org/OpenBB`。
- 禁 4.x，不安装全集 `openbb` 元包，不使用 AGPL 许可的 `openbb-yfinance`。
- 生产环境默认不拉起该 profile，本拍不做生产部署。

## 固定接口与上游映射

面板仅开放以下 GET 接口，标的与序列限定在白名单内：

| 面板接口 | 固定范围 | sidecar 上游 |
|---|---|---|
| `GET /api/openbb/status` | 配置、连通与能力状态诊断 | `GET /api/v1/tsp/health` |
| `GET /api/openbb/hk_daily` | 港股 `0700.HK`、`9988.HK` | `GET /api/v1/tsp_yahoo/equity/historical` |
| `GET /api/openbb/us_daily` | 美股 `AAPL`、`MSFT` | `GET /api/v1/tsp_yahoo/equity/historical` |
| `GET /api/openbb/macro` | FRED `DGS10`、`CPIAUCSL` | `GET /api/v1/fred/economy/fred_series` |

上述接口与参数白名单是固定契约，不扩展为任意市场、任意标的或任意 FRED 序列的通用代理。

## 状态与能力矩阵

- `enabled` 只表示已配置并启用，**不等于** `reachable`；是否可达必须由健康检查单独判定。
- 未配置、未连通、上游异常或数据无法判定时统一保持 `pending`，按 **fail-closed** 处理，不返回伪成功，不降级混入其他数据源。
- 能力矩阵仅登记 `research_only`，成熟度为 `candidate/pending`。
- OpenBB 即使健康也只进入研究能力 `candidates`，不注册进通用 provider 路由表；不能写成 A 股 `daily` / `financial` 偏好，也不改变现有主数据源及其回退顺序。

## PIT、数据用途与 schema

- 数据只供独立 OpenBB API 与「设置」页诊断使用。
- PIT 红线：OpenBB 当前值或修订后数据均不接入回测、enrichment 或因子落盘，不进入盘后管道、策略计算和交易流程。
- 宏观数据使用独立 schema，不复用股票日线 schema。
- 上游没有提供的字段（例如 `amount`）必须返回 `null`，不得填 `0`；缺失值不得被解释为真实零值。

## YAML、条款与卸载

YAML 声明源仅是备选接入方式，不能替代上述接口、标的和序列白名单，也不能绕过本文件的能力边界。底层数据源服务条款（ToS）、许可、访问授权及使用限制由使用者自担，并须自行核查。

卸载时须同时：

1. 清除 `OPENBB_BASE_URL` 配置；
2. 停止 `openbb` profile；
3. 删除相关 plugin/custom 包。

---

*仅供研究，不构成投资建议；执行须人工确认。*
