# 研究数据快照

研究（WorkBuddy、Harness 或本地脚本）只读"数据快照"，不连 TSP 生产库，不在生产容器里跑研究。
快照由 `backend/scripts/export_research_snapshot.py` 一条命令生成，放在 `research/_snapshots/YYYYMMDD/`（已加入 .gitignore）。

## 导出

先 dry-run 看行数、预估大小和磁盘余量，再真正导出：

```bash
cd ~/tick-stock-panel && mkdir -p research/_snapshots
nice -n 19 ionice -c3 docker compose run --rm --no-deps -T \
  -v "$PWD/backend/scripts:/app/scripts:ro" \
  -v "$PWD/research/_snapshots:/snapshots" \
  -e SNAPSHOT_GIT_COMMIT="$(git rev-parse HEAD)" \
  app uv run --no-sync python -m scripts.export_research_snapshot --out /snapshots --dry-run
# 确认后去掉 --dry-run
```

- 镜像里没有 `scripts/`，所以要用 `-v` 挂进去；快照目录挂到 `/snapshots`。
- `docker compose run` 起的是一次性容器，不占线上端口，跑完即删。
- 脚本启动时自己降到 `nice 19` + `ionice idle`，外层 `nice/ionice` 只是双保险；`--no-nice` 可关闭。

## 参数

| 参数 | 说明 |
|---|---|
| `--start/--end` | 日期范围，默认截至今天的近 3 年；只作用于按日分区的表和资讯表 |
| `--tables a,b` | 只导出部分表；`--list-tables` 列出全部 |
| `--tag` | 快照目录名，默认今天（北京时间） |
| `--dry-run` | 只统计行数、日期范围和预估大小，不写文件 |
| `--force` | 同名快照已存在时覆盖（默认拒绝） |
| `--min-free-gb` | 导出后剩余空间低于该值（默认 5G）则放弃 |

## 安全与资源

- 只读：Parquet 只读打开；SQLite 用 `mode=ro` + `query_only` 打开，在一个读事务内分批读取（WAL 下不阻塞线上写入）。
- 内存：按分区/按批（默认 20 万行）流式写出，内存只占一个批次。
- 原子：先写到 `.tmp-*` 临时目录，全部成功才改名；失败自动清理，不留半成品。
- 磁盘：先按源文件大小估算，导出后剩余不足门槛就不写。

## 产出

每个快照目录包含每张表一个 `<表名>.parquet`（zstd 压缩）以及：

- `MANIFEST.json`：每张表的行数、日期范围、来源、列类型、文件大小、sha256；代码版本（git commit）；生成时间（Asia/Shanghai）；参数。
- `README.md`：表格摘要和口径提醒（K 线不复权、列表与行业为导出当天状态等）。

## 包含的表

| 表 | 来源 | 日期截取 |
|---|---|---|
| kline_daily | data/kline_daily（不复权日 K） | 是 |
| adj_factor | data/adj_factor/all.parquet | 否（全量） |
| kline_index_daily | data/kline_index_daily | 是 |
| kline_etf_daily | data/kline_etf_daily | 是 |
| fund_flow_stock | data/fund_flow/stock | 是 |
| etf_shares | data/fund_flow/etf_shares | 是 |
| etf_flow_news | news.sqlite 中 source=etf_flow（ETF领航者申赎） | 是 |
| news_archive | data/news_archive.sqlite（东财 7×24、财联社） | 是 |
| instruments / instruments_etf / instruments_index | 股票、ETF、指数列表 | 否（当前快照） |
| industry_ths / concept_ths | data/ext_data 同花顺行业、概念 | 否（当前快照） |

源数据不存在的表会在清单里标为 `missing` 并跳过。
