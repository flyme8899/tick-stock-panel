# OpenBB 研究源插件

主进程仅通过 `client.py` 请求独立 OpenBB V5 sidecar，不安装或导入 OpenBB Python 包。

- 配置 `OPENBB_BASE_URL` 只表示 `enabled=true`；插件可用性还要求真实 health 通过。
- 仅 `hk_daily`、`us_daily`、`macro` 三类研究数据；固定符号白名单见 `client.py`。
- 不提供 `get_daily`、`get_financials` 等主链协议；清单的 `research_only` 使其即使健康也不进入通用 provider 路由表，不写入行情、财务或因子存储。
- YAML 自定义源只是其他场景的备选能力，不能替代这里的白名单与 health 门控。
- 清掉 URL 后插件进入 pending；删除本插件和 `custom/openbb` 可卸载研究面。

仅供研究，不构成投资建议；执行须人工确认。
