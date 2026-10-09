# Alpha158 实验组

158 个价量因子来自 vnpy 的 Alpha158（MIT，Copyright (c) vnpy contributors），vnpy 移植自微软 Qlib（MIT，Copyright (c) Microsoft Corporation）。TSP 不依赖这两个包，公式写在 `backend/app/factors/alpha158.py`。许可文本见 `THIRD_PARTY_NOTICES.md`。

分组名是 `Alpha158（实验）`，id 前缀 `a158_`，只覆盖股票。`all_factors()`、`/api/factors`、信号白名单、AI 提示词和工具目录默认都不返回这组。因子库和因子发现要打开「显示 Alpha158 实验组」。挖掘默认也不含这组。

## 方向和用法

方向不预填。当单因子用时，以实测 IC 的符号为准。强因子经常是负 IC：因子值越大，接下来的收益往往越低。按「值大做多」打分会把信号用反。

IC 高不等于扣费后能赚钱。这些因子换手不低，下单前要看分层多空和换手。

## 截面不完整

日均 IC 是每天 Rank IC 的等权平均。当天只有十几只股票时，这个相关系数是噪声，天数一多就会主导均值。

维护者本机的 `kline_daily_enriched` 有 243 个分区，只有 101 个是全市场（大约 5500 只），从 2026-05-13 才开始。更早的交易日每天 7～25 只。不要把 2025-10-09 至 2026-09-30 说成一次全市场检验。有效的全市场区间从 2026-05-13 算起。

`scripts/eval_alpha158.py` 的 `--min-symbols-per-date` 默认 200。面板仍按请求区间整段加载，滚动窗口的预热保留；IC、ICIR、换手和衰减只在截面不少于这个数的交易日上计算。启动时打印每天股票数的中位数、最小值，以及低于门槛的天数。

60 日因子在这段干净样本上覆盖率大约 0.40，全市场交易日大约 100 天，结论先不下，等更长的全市场历史再评。这不是 `FACTOR_WARMUP_DAYS=120` 不够。

不要在正在提供服务的 tsp 容器里跑完整的 158 因子评估，会把服务拖重启。另开一个容器，挂上同一份数据卷，例如 `docker run --rm --volumes-from <tsp> ...`。

## 价格和成交量

enriched 里的 open、high、low、close 是前复权。amount 和 volume 是不复权，成交量单位是手。

`vwap_0` 用 `(amount / (volume * 100)) / raw_close`，分子分母都是不复权价格。vma、vstd、vsum*、corr、cord 仍直接用未复权成交量，送转除权日成交量会跳变。

`scoring.py` 里已有的 `vwap_bias` 仍用复权 `close` 做分母。这次没有改它。
