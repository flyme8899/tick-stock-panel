# Alpha158 实验组

158 个价量因子来自 vnpy 的 Alpha158（MIT，Copyright (c) vnpy contributors），vnpy 移植自微软 Qlib（MIT，Copyright (c) Microsoft Corporation）。TSP 不依赖这两个包，公式写在 `backend/app/factors/alpha158.py`。许可文本见 `THIRD_PARTY_NOTICES.md`。

分组名是 `Alpha158（实验）`，id 前缀 `a158_`，只覆盖股票。`all_factors()`、`/api/factors`、信号白名单、AI 提示词和工具目录默认都不返回这组。因子库和因子发现要打开「显示 Alpha158 实验组」。挖掘默认也不含这组。

## 方向和用法

方向不预填。当单因子用时，以实测 IC 的符号为准。同一类因子里符号可以相反：`vsump_*` 是负 IC，`vsumn_*` 是正 IC，两者互为镜像。按「值大做多」打分会把负 IC 的因子用反。

IC 高不等于扣费后能赚钱。这些因子换手不低，下单前要看分层多空和换手。

## 截面不完整

日均 IC 是每天 Rank IC 的等权平均。当天只有十几只股票时，这个相关系数是噪声，天数一多就会主导均值。

维护者本机的 `kline_daily_enriched` 有 243 个分区，只有 101 个是全市场（大约 5500 只），从 2026-05-13 才开始。更早的交易日每天 7～25 只。不要把 2025-10-09 至 2026-09-30 说成一次全市场检验。有效的全市场区间从 2026-05-13 算起。

`scripts/eval_alpha158.py` 的 `--min-symbols-per-date` 默认 200。面板仍按请求区间整段加载，滚动窗口的预热保留；IC、ICIR、换手和衰减只在截面不少于这个数的交易日上计算。启动时打印每天股票数的中位数、最小值，以及低于门槛的天数。

不要在正在提供服务的 tsp 容器里跑完整的 158 因子评估或分层回测，会把服务拖重启。另开一个容器，挂上同一份数据卷，例如 `docker run --rm --volumes-from <tsp> ...`。

## 分层多空（扣费）

IC 过了门槛还不等于扣费后能赚钱。`scripts/eval_factor_layers.py` 只读，复用因子检验的 `_add_groups` 做截面分位，换手和 `_calc_turnover` 同一口径。默认这 4 个彼此独立的因子：`a158_vsumd_60`、`a158_vstd_20`、`a158_vma_20`、`a158_std_20`。`a158_vsump_60` 和 `a158_vsumn_60` 不再放进默认列表：样本上 `vsump + vsumn = 1`，`vsumd = 2*vsump - 1`，三者是同一个信号的仿射变换，只保留 `a158_vsumd_60`。

买入价是 `close(t+1)`，持有 `--hold` 个交易日（默认同时报 1、3、5）。方向默认跟 Rank IC 的符号：负 IC 做多低分组。涨停日不买，跌停日不卖。单边成本默认 15 bp（佣金、印花税和一点滑点），整组换仓扣两倍，并同时打印 0 成本。截面门槛、ST、上市满 60 个交易日、截止到最近一个已收盘交易日，和评估脚本一致。

组均和多空用同一批调仓日。某一天有空的分位组，或当天可交易股票少于 `--min-tradable-per-date`（默认 200），整天跳过，并打印跳过天数。`--min-symbols-per-date` 只过滤加载后的面板，挡不住涨跌停过滤之后变薄的截面。

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/eval_factor_layers.py
```

表里每个年化数字后面标出这条收益序列的期数。期数少于 30 时，脚本会警告：年化收益和夏普是按 `252/持有天数` 外推的，不可靠。只有大约 100 个全市场交易日、持有 5 日时，期数大约二十来期，正落在这个警告里。本环境没有日线时脚本退出码为 2，不编造收益。

## 一次干净样本（仅供参考）

下面是维护者本机在截面门槛之后的一次跑数，只有 100 个交易日，只作参考，不能当成稳定结论。

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/eval_alpha158.py \
  --days 365 --min-symbols-per-date 200 --label-lag 1 \
  --exclude-limit --exclude-st --min-listed-days 60
```

有效样本是 100 个交易日、每天约 5523 只。158 个因子大约 3 分 33 秒。

按 |IC| 排在前面的是量能类：`vsump`、`vsumn`、`vsumd`、`vstd`、`vma`，不是 `max`、`std`、`qtlu`、`ma`。`vsump_*` 为负 IC，`vsumn_*` 为正 IC，互为镜像。`a158_vsump_60` 的 IC 是 -0.1568，ICIR 是 -0.3619。从 `close(t)` 出发的衰减：1 日 -0.1534，3 日 -0.2016，5 日 -0.1681。

60 日窗口不是普遍没用。早先看到 60 日档 IC 接近 0，是小截面交易日把等权日均带偏了，不是期限拉长后的衰减。这段样本上 60 日因子覆盖率大约只有 0.40，结论仍要等更长的全市场历史。这不是 `FACTOR_WARMUP_DAYS=120` 不够。

## 价格和成交量

enriched 里的 open、high、low、close 是前复权。amount 和 volume 是不复权，成交量单位是手。

`vwap_0` 用 `(amount / (volume * 100)) / raw_close`，分子分母都是不复权价格。vma、vstd、vsum*、corr、cord 仍直接用未复权成交量，送转除权日成交量会跳变。

`scoring.py` 里已有的 `vwap_bias` 仍用复权 `close` 做分母。这次没有改它。
