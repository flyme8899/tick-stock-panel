# Alpha158 实验组

158 个价量因子来自 vnpy 的 Alpha158（MIT，Copyright (c) vnpy contributors），vnpy 移植自微软 Qlib（MIT，Copyright (c) Microsoft Corporation）。TSP 不依赖这两个包，公式写在 `backend/app/factors/alpha158.py`。许可文本见 `THIRD_PARTY_NOTICES.md`。

分组名是 `Alpha158（实验）`，id 前缀 `a158_`，只覆盖股票。`all_factors()`、`/api/factors`、信号白名单、AI 提示词和工具目录默认都不返回这组。因子库和因子发现要打开「显示 Alpha158 实验组」。挖掘默认也不含这组。

## 方向和用法

方向不预填。当单因子用时，以实测 IC 的符号为准。剔除样本不足的交易日后（见下），头部集中在**量能类**（`vsump` / `vsumn` / `vsumd` / `vstd` / `vma`）；`vsump_*`（放量占比）是负 IC、`vsumn_*`（缩量占比）是正 IC，两者互为镜像。按「值大做多」打分会把信号用反。

IC 高不等于扣费后能赚钱。这些因子换手不低，下单前要看分层多空和换手。

## 检验样本和横截面门槛

**跑评估前先看 enriched 分区是不是齐的。** `kline_daily_enriched` 可能有相当一部分日期只有几只股票（补录不全）。这种日子的 Rank IC 是噪声——只有几只股票时几个点就能算出 ±0.5——数量一多就会主导均值，把因子强弱的排序整个带偏。

`scripts/eval_alpha158.py --min-symbols-per-date 200` 会先按每天样本数过滤，再算 IC，并打印保留下来的每天样本数中位数和最小值：

    横截面门槛: 去掉不足 200 只的交易日 142 个，剩 100 个；每天样本数 中位 5523、最少 5493。

门槛要放在算 `market_dates` 之前，否则被剔除的日子仍会通过「下一天收益」把标签带回来。默认 0（不过滤）是为了保持旧行为，但数据没补录全时一定要显式给一个值。

60 日窗口**不是**普遍无效。含有这类因子的量能组在干净样本上是头部（`a158_vsump_60` IC -0.1568），且 IC 衰减在 3 日最强（-0.2016）。之前观察到「60 日档 |IC| 掉到 0」是样本不足造成的，不是期限衰减。60 日档覆盖率只有 0.40 左右（100 个交易日里预热吃掉一半），结论仍要看覆盖率那一列。

## 价格和成交量

enriched 里的 open、high、low、close 是前复权。amount 和 volume 是不复权，成交量单位是手。

`vwap_0` 用 `(amount / (volume * 100)) / raw_close`，分子分母都是不复权价格。vma、vstd、vsum*、corr、cord 仍直接用未复权成交量，送转除权日成交量会跳变。

`scoring.py` 里已有的 `vwap_bias` 仍用复权 `close` 做分母。这次没有改它。
