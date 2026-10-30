"""批次 20261030gkparquet · P-kline-parquet-guard · 分钟分区坏 parquet 读写兜底。

## 这一组测在测什么

分钟K一天一个分区(`kline_minute/date=…/part.parquet`、ETF 同构)。一个读不出来的
分区文件, 以前会让两件事同时坏掉:

- **写侧**: `_write_minute_partition` 里一句裸 `pl.read_parquet(out)`,
  `out.exists()` 为真就读, 读失败直接冒到 `sync_minute_single` / 5日10日补齐 /
  `minute_refresh` 上层 → **一次 HTTP 500**。而"补齐这一天"正是修它的手段,
  于是坏的那天再也补不上。
- **查侧**: `get_minute_range` / `get_minute_by_dates` 一次 `scan_parquet` 一把梭,
  一个坏文件 → **整段区间返回空**。调用方看到的是"这段时间没数据", 不是"某天坏了"。

**运维隔离挪文件 ≠ 本 PR 已修**: 那是并行的运维动作, 代码路径不改, 下一次还会踩。
**外层 try→空 ≠ 已按分区跳过**: 那只防 500, 好分区会陪着坏分区一起丢。

## 本组测例的边界

只测分钟分区(`kline_minute` / `kline_etf_minute`)。日K、`adj_factor` all.parquet、
DuckDB 视图的坏文件策略**不在本拍**。合入 ≠ 部署; tip ≠ prod。

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp(本拍非回测, 仅旁注)。
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import polars as pl
import pytest

from app.services.kline_sync import _write_minute_partition
from app.tickflow.repository import DataStore, KlineRepository

DAY = "2026-01-05"
DAY2 = "2026-01-06"
CORRUPT = b"NOT_A_PARQUET\x00\x01\x02 this is not a parquet file"


def _bars(spec: dict[str, list[tuple[str, float]]]) -> pl.DataFrame:
    rows = [
        {"symbol": sym, "datetime": datetime.fromisoformat(dt), "close": px}
        for sym, bars in spec.items()
        for dt, px in bars
    ]
    return pl.DataFrame(rows)


def _corrupt_partition(base, day: str) -> str:
    """在 `base/date=<day>/part.parquet` 放一个读不出来的文件, 返回其路径。"""
    out = base / f"date={day}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(CORRUPT)
    return str(out)


# ---------------------------------------------------------------- ① 写侧兜底


def test_write_minute_partition_skips_corrupt_existing_and_continues(tmp_path, caplog):
    """G-T2/G-T3: 已有 part.parquet 损坏时写路径不抛、能写进新数据, 日志含完整路径。"""
    minute_dir = tmp_path / "kline_minute"
    bad_path = _corrupt_partition(minute_dir, DAY)

    df = _bars({"000001.SZ": [(f"{DAY} 09:31:00", 10.0), (f"{DAY} 09:32:00", 10.1)]})
    with caplog.at_level(logging.WARNING, logger="app.services.kline_sync"):
        written = _write_minute_partition(df, minute_dir)

    assert written == 2
    out = minute_dir / f"date={DAY}" / "part.parquet"
    stored = pl.read_parquet(out)
    assert stored.height == 2
    assert stored["symbol"].to_list() == ["000001.SZ", "000001.SZ"]

    # 日志必须**含完整路径** —— 不含路径的"读失败"等于没说是哪个文件坏了。
    assert any(bad_path in record.getMessage() for record in caplog.records), caplog.text


def test_corrupt_partition_is_quarantined_not_left_as_scannable_parquet(tmp_path):
    """N2: 坏文件被移出后**不再是 `.parquet`**, 不会被 `**/*.parquet` 扫回来。"""
    minute_dir = tmp_path / "kline_minute"
    _corrupt_partition(minute_dir, DAY)
    _write_minute_partition(_bars({"000001.SZ": [(f"{DAY} 09:31:00", 10.0)]}), minute_dir)

    day_dir = minute_dir / f"date={DAY}"
    quarantined = list(day_dir.glob("part.parquet.corrupt*"))
    assert quarantined, "坏文件应被隔离为 .corrupt.<ts> 旁路文件"
    # 除了新写的 part.parquet, 目录里不应再有其它 *.parquet
    assert sorted(p.name for p in day_dir.glob("*.parquet")) == ["part.parquet"]
    # 新写的 part.parquet 可读(备份文件带 .corrupt 后缀, glob 扫不到)
    assert pl.read_parquet(day_dir / "part.parquet").height == 1
    # 坏文件内容仍在, 没被删也没被覆盖
    assert quarantined[0].read_bytes() == CORRUPT


def test_corrupt_etf_minute_partition_also_guarded(tmp_path):
    """覆盖 `kline_etf_minute`: 同一函数守两个目录, 不因只修 stock 而漏 ETF。"""
    minute_dir = tmp_path / "kline_etf_minute"
    bad_path = _corrupt_partition(minute_dir, DAY)
    written = _write_minute_partition(
        _bars({"510300.SH": [(f"{DAY} 09:31:00", 3.9)]}), minute_dir,
    )
    assert written == 1
    assert pl.read_parquet(minute_dir / f"date={DAY}" / "part.parquet").height == 1
    assert list((minute_dir / f"date={DAY}").glob("part.parquet.corrupt*"))
    assert bad_path == str(minute_dir / f"date={DAY}" / "part.parquet")


# ---------------------------------------------------------------- ② 查侧按分区跳过


def _seed_minute_days(repo: KlineRepository, days: list[str]) -> None:
    """写若干健康日分区, 每只一次 _write_minute_partition(与真实写路径同一条)。"""
    for day in days:
        _write_minute_partition(
            _bars({"000001.SZ": [(f"{day} 09:31:00", 10.0), (f"{day} 09:32:00", 10.1)]}),
            repo.store.data_dir / "kline_minute",
        )


def test_get_minute_range_skips_corrupt_partition_with_path_log(tmp_path, caplog):
    """G-T4: 范围查询跳过坏日、好日仍返回; warning 含坏文件路径。**禁止仅整段空。**"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_minute_days(repo, [DAY])
    bad_path = _corrupt_partition(repo.store.data_dir / "kline_minute", DAY2)

    with caplog.at_level(logging.WARNING, logger="app.tickflow.repository"):
        df = repo.get_minute_range(
            ["000001.SZ"], date(2026, 1, 5), date(2026, 1, 6), "stock",
        )

    # 好日的数据**必须还在** —— 这正是"整段空冒充已兜底"过不了的地方。
    assert df.height == 2
    assert df["datetime"].dt.date().unique().to_list() == [date(2026, 1, 5)]
    assert any(bad_path in record.getMessage() for record in caplog.records), caplog.text


def test_get_minute_by_dates_skips_corrupt_partition(tmp_path, caplog):
    """G-T4 同族: `get_minute_by_dates` 的 parts 列表里任一坏文件不该让整次返回空。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_minute_days(repo, [DAY])
    _corrupt_partition(repo.store.data_dir / "kline_minute", DAY2)

    with caplog.at_level(logging.WARNING, logger="app.tickflow.repository"):
        df = repo.get_minute_by_dates(
            ["000001.SZ"], [date(2026, 1, 5), date(2026, 1, 6)], "stock",
        )

    assert df.height == 2
    assert df["datetime"].dt.date().unique().to_list() == [date(2026, 1, 5)]
    assert any("跳过损坏分区" in record.getMessage() for record in caplog.records)


def test_get_minute_not_poisoned_by_other_day_corrupt_when_fallback(tmp_path, caplog):
    """G-T5: 当日分区健康时, 不因**他日**坏文件导致当日查询为空。

    走的是回退全树那条路(当日分区不存在时), 坏他日不该连坐当日。
    """
    repo = KlineRepository(DataStore(tmp_path))
    _seed_minute_days(repo, [DAY])
    _corrupt_partition(repo.store.data_dir / "kline_minute", DAY2)

    with caplog.at_level(logging.WARNING, logger="app.tickflow.repository"):
        # DAY 的 part 存在 → 直读; 删掉它才会走回退, 所以这里用一个"存在但当日无行"的新日
        df = repo.get_minute("000001.SZ", date(2026, 1, 5), "stock")

    assert df.height == 2

    # 再验回退路径: 临时移除当日 part, 只剩一个坏他日 + 无当日分区
    part = repo.store.data_dir / "kline_minute" / f"date={DAY}" / "part.parquet"
    part.rename(part.with_suffix(".parquet.away"))
    quiet = repo.get_minute("000001.SZ", date(2026, 1, 5), "stock")
    assert quiet.is_empty()  # 当日确实没数据了, 但**没有崩**, 也没被坏他日拖成异常


def test_get_minute_range_all_corrupt_returns_empty_without_raising(tmp_path, caplog):
    """全坏时返回空(而非抛 500), 且每个坏文件各留一条带路径的告警。"""
    repo = KlineRepository(DataStore(tmp_path))
    bad = [
        _corrupt_partition(repo.store.data_dir / "kline_minute", day)
        for day in (DAY, DAY2)
    ]

    with caplog.at_level(logging.WARNING, logger="app.tickflow.repository"):
        df = repo.get_minute_range(
            ["000001.SZ"], date(2026, 1, 5), date(2026, 1, 6), "stock",
        )

    assert df.is_empty()
    text = caplog.text
    assert all(path in text for path in bad)


# ---------------------------------------------------------------- ③ 健康不误伤


def test_healthy_partition_merge_regression_unchanged(tmp_path):
    """G-T6: 健康文件上合并语义与 `test_minute_partition_merge` 对齐。

    未触及 symbol 原样保留; 触及 symbol unique keep=last; 全局按 symbol,datetime 有序。
    """
    minute_dir = tmp_path / "kline_minute"
    first = _bars({
        "000001.SZ": [(f"{DAY} 09:31:00", 10.0), (f"{DAY} 09:32:00", 10.1)],
        "600000.SH": [(f"{DAY} 09:31:00", 8.0), (f"{DAY} 09:32:00", 8.1)],
        "300750.SZ": [(f"{DAY} 09:31:00", 20.0)],
    })
    _write_minute_partition(first, minute_dir)

    second = _bars({"000001.SZ": [(f"{DAY} 09:32:00", 10.9), (f"{DAY} 09:33:00", 10.2)]})
    written = _write_minute_partition(second, minute_dir)

    stored = pl.read_parquet(minute_dir / f"date={DAY}" / "part.parquet")
    # `written` 是合并后分区的总行数(既有语义, 见 test_merge_preserves_untouched…)
    assert written == stored.height == 6
    # 触及 symbol 的同一根被覆盖(keep=last)
    row = stored.filter(
        (pl.col("symbol") == "000001.SZ") & (pl.col("datetime") == datetime(2026, 1, 5, 9, 32))
    )
    assert row["close"].to_list() == [10.9]
    # 未触及 symbol 原样
    assert stored.filter(pl.col("symbol") == "600000.SH").height == 2
    assert stored.filter(pl.col("symbol") == "300750.SZ").height == 1
    # 全局有序
    assert stored["symbol"].to_list() == sorted(stored["symbol"].to_list())
    assert stored.equals(stored.sort("symbol", "datetime"))


def test_healthy_range_returns_all_days(tmp_path):
    """健康分区: 范围查询把所有天都返回(兜底逻辑不得插手健康路径)。"""
    repo = KlineRepository(DataStore(tmp_path))
    _seed_minute_days(repo, [DAY, DAY2])
    df = repo.get_minute_range(["000001.SZ"], date(2026, 1, 5), date(2026, 1, 6), "stock")
    assert df.height == 4
    assert sorted(df["datetime"].dt.date().unique().to_list()) == [
        date(2026, 1, 5), date(2026, 1, 6),
    ]


# ---------------------------------------------------------------- ④ 守卫存在性


def test_write_path_has_no_bare_existing_read():
    """G-T2 结构断言: `_write_minute_partition` 里不许再有裸 `pl.read_parquet(out)`。"""
    import inspect

    from app.services import kline_sync

    src = inspect.getsource(kline_sync._write_minute_partition)
    assert "pl.read_parquet(out)" not in src
    assert "_read_existing_minute_partition" in src
    helper = inspect.getsource(kline_sync._read_existing_minute_partition)
    assert "except Exception" in helper
    assert "logger.error" in helper


@pytest.mark.parametrize("name", ["get_minute_range", "get_minute", "get_minute_batch"])
def test_query_paths_skip_readable_partitions(name):
    """G-T4 结构断言: 查询族不再直接 `scan_parquet(glob)` 一把梭。"""
    import inspect

    from app.tickflow import repository

    src = inspect.getsource(getattr(repository.KlineRepository, name))
    assert "scan_parquet(self._minute_glob_for" not in src
    assert "_readable_minute_partitions" in src


def test_get_minute_by_dates_probes_each_partition_before_scan():
    """G-T4 同族结构断言: `get_minute_by_dates` 逐分区探 footer 后才进 parts 列表。

    它不走向量 glob, 而是按日期拼路径; 兜底手段因而是「每个文件先
    `read_parquet_schema` 探一下」—— 坏的 continue, 好的才 append。
    """
    import inspect

    from app.tickflow import repository

    src = inspect.getsource(repository.KlineRepository.get_minute_by_dates)
    assert "read_parquet_schema" in src
    assert "except Exception" in src
    assert "continue" in src
    # parts 必须在 try 之外**逐个** append, 不能一次性推导全部存在文件
    assert "parts.append" in src
