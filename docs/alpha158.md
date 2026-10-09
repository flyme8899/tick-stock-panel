# Alpha158 实验组

158 个价量因子来自 vnpy 的 Alpha158（MIT，Copyright (c) vnpy contributors），vnpy 移植自微软 Qlib（MIT，Copyright (c) Microsoft Corporation）。TSP 不依赖这两个包，公式写在 `backend/app/factors/alpha158.py`。许可文本见 `THIRD_PARTY_NOTICES.md`。

分组名是 `Alpha158（实验）`，id 前缀 `a158_`，只覆盖股票。`all_factors()`、`/api/factors`、信号白名单、AI 提示词和工具目录默认都不返回这组。因子库和因子发现要打开「显示 Alpha158 实验组」。挖掘默认也不含这组。

## 方向和用法

方向不预填。当单因子用时，以实测 IC 的符号为准。全市场样本上，max、std、qtlu、ma 这几类头部因子是负 IC：因子值越大，接下来的收益往往越低。按「值大做多」打分会把信号用反。

IC 高不等于扣费后能赚钱。这些因子换手不低，下单前要看分层多空和换手。

60 日窗口在 2025-10-09 至 2026-09-30 的全市场检验里，|IC| 掉到 0 附近（`a158_max_60` 约 0.0004，`a158_max_30` 约 -0.126）。这不是预热把窗口算空。检验会在起点前再取 120 个自然日，这段里的交易日多于 60，因子覆盖率是满的。max 从 5 日到 30 日 |IC| 单调变弱，60 日接到 0 是期限拉长后的结果。`scripts/eval_alpha158.py` 会打印每个因子的覆盖率和交易日数，用来核对是不是空值。

## 价格和成交量

enriched 里的 open、high、low、close 是前复权。amount 和 volume 是不复权，成交量单位是手。

`vwap_0` 用 `(amount / (volume * 100)) / raw_close`，分子分母都是不复权价格。vma、vstd、vsum*、corr、cord 仍直接用未复权成交量，送转除权日成交量会跳变。

`scoring.py` 里已有的 `vwap_bias` 仍用复权 `close` 做分母。这次没有改它。
