# K 线形态

日 K 上的经典蜡烛形态，数值沿用 TA-Lib `CDL*`：`100` 看涨，`-100` 看跌，`0` 没有出现。锤头、上吊这类单边形态在 TA-Lib 里也返回 `100`，这里按语义把看跌写成 `-100`，选股条件才能直接区分方向。

目录与 InStock `STOCK_KLINE_PATTERN_DATA` 同一组，共 61 个。进程能 `import talib` 时全部用 TA-Lib 计算。装不上时用 Polars 实现其中主体形态；`builtin=False` 的冷门形态不猜测，选股下拉里不出现。环境变量 `TSP_CANDLE_ENGINE=builtin` 可以强制走 Polars。

Docker 构建会尝试 `uv pip install TA-Lib`。C 库或轮子缺失时这条命令失败也不让镜像构建失败，运行时自动改走内置实现。

## 增量

旁路目录是 `data/kline_candle_patterns/date=*/part.parquet`。盘后管道写完股票 enriched 之后重算最近 60 个交易日的 OHLC（其中 20 根只做实体均值暖机），只保留最近 40 天的结果，更早的分区删掉。选股「近 N 日」最多回看 30 个交易日，读这些分区，不在打开页面时重扫多年历史。

戳文件是 `引擎|目录版本|最新 enriched 日期`。日期没变时，选股查询不会重算。除权后的盘后管道会直接重写这个窗口。

日 K 图上的标记按当前返回的那一只股票现算，包含实时蜡烛。失败时标记为空，日 K 接口本身不报错。

## 选股和图表

选股结果筛选：形态、方向（看涨 / 看跌；十字这类中性形态显示为「出现」）、近 N 个交易日。接口：

- `GET /api/screener/candle-patterns`
- `GET /api/screener/candle-patterns/match?pattern=&side=&within=`

`within` 取 1 到 30。方向和当前引擎对不上时返回 400。

自定义信号可以写 `cdl_engulfing == 100`。这些列只在信号引用它们、并且帧里有前序 K 线时才算。盘中单日帧没有前序 K 线，不算形态。

图表标记走现有买卖箭头：看涨用买入箭头（红），看跌用卖出箭头（绿）。同一天同一方向最多显示三个名字。中性十字用中性标记，不画成买入箭头。

形态用的是前复权 OHLC，和日 K 图同一套价格。
