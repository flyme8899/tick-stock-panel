"""原子写与分区写入的并发安全。

回归的是两个叠在一起的问题:

1. ``atomic_write_parquet`` 原先固定用 ``<name>.tmp``, 同一分区的并发写会撞同一个
   临时文件 —— 一个线程 ``replace`` 把它搬走, 另一个线程随后 ``replace`` 已不存在的
   源就抛 ``FileNotFoundError``。
2. ``write_rows`` 的「读旧分区 → 合并 → 写回」原先在锁外完成, 两个线程都读到同一份
   旧内容, 后落盘的那份把先落盘的整份覆盖掉 —— 不报错, 数据静默消失。

两个问题必须一起修: 只修临时文件名能让异常消失, 但丢数据依然存在。
"""
from __future__ import annotations

import threading
from pathlib import Path

import polars as pl
import pytest

from app.fund_flow import store
from app.services.fs_utils import atomic_write_parquet


def _intraday_rows(tag: str, count: int = 40) -> list[dict]:
    return [
        {
            "code": f"{tag}{i:03d}",
            "name": f"n{tag}{i}",
            "trade_date": "2026-10-23",
            "captured_at": "2026-10-23T10:00:00",
            "main_net": 1.0,
            "source": "akshare_em",
        }
        for i in range(count)
    ]


def _run_together(worker, tags: str) -> list[tuple[str, str]]:
    """让各线程尽量同时起跑, 收异常类的名字回来。"""
    barrier = threading.Barrier(len(tags))
    errors: list[tuple[str, str]] = []

    def runner(tag: str) -> None:
        barrier.wait()
        try:
            worker(tag)
        except Exception as exc:  # noqa: BLE001 - 测试要收集任何异常
            errors.append((tag, type(exc).__name__))

    threads = [threading.Thread(target=runner, args=(tag,)) for tag in tags]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def test_concurrent_atomic_write_leaves_no_error_and_no_tmp(tmp_path: Path) -> None:
    """并发写同一目标不留 FileNotFoundError, 也不留 .tmp 垃圾。"""
    target = tmp_path / "part.parquet"

    def worker(tag: str) -> None:
        atomic_write_parquet(pl.DataFrame({"tag": [tag] * 40}), target)

    errors = _run_together(worker, "ABCDEFGH")
    assert errors == []
    assert target.exists()
    # 整体覆盖语义: 最后落盘的那份胜出, 行数固定。
    assert pl.read_parquet(target).height == 40
    assert list(tmp_path.glob("*.tmp")) == []


def test_unique_tmp_paths_differ_per_call(tmp_path: Path) -> None:
    """临时文件名每次不同, 否则并发写会互相搬走对方的源文件。"""
    from app.services.fs_utils import _unique_tmp_path

    target = tmp_path / "part.parquet"
    names = {_unique_tmp_path(target).name for _ in range(20)}
    assert len(names) == 20
    for name in names:
        # 后缀仍是 .tmp: 不会匹配 *.parquet 的 glob, 读侧不会误扫。
        assert name.startswith("part.parquet.")
        assert name.endswith(".tmp")


@pytest.mark.parametrize("tags", ["ABCD", "ABCDEFGH", "ABCDEFGHIJKLMNOP"])
def test_concurrent_write_rows_keeps_every_row(tmp_path: Path, tags: str) -> None:
    """并发往同一分区写不同主键, 每一行都要保住。"""
    def worker(tag: str) -> None:
        store.write_rows(tmp_path, "stock_intraday", "2026-10-23", _intraday_rows(tag))

    errors = _run_together(worker, tags)
    assert errors == []
    frame = store.read_partition(tmp_path, "stock_intraday", "2026-10-23")
    # 主键是 code, 各组用不同前缀, 所以期望行数 = 组数 × 每组行数
    assert frame.height == len(tags) * 40


def test_concurrent_write_rows_across_partitions_are_independent(tmp_path: Path) -> None:
    """不同交易日的分区各写各的, 互不干扰。"""
    days = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]

    def worker(day: str) -> None:
        rows = [
            {
                "code": f"5103{idx:02d}",
                "name": f"etf{idx}",
                "trade_date": day,
                "captured_at": f"{day}T10:00:00",
                "main_net": 1.0,
                "source": "akshare_em",
            }
            for idx in range(10)
        ]
        store.write_rows(tmp_path, "stock_intraday", day, rows)

    errors = _run_together(worker, days)
    assert errors == []
    for day in days:
        assert store.read_partition(tmp_path, "stock_intraday", day).height == 10


def test_write_rows_merges_existing_partition(tmp_path: Path) -> None:
    """锁不改变原有合并语义: 后写的主键覆盖先写, 不同的都保留。"""
    store.write_rows(tmp_path, "stock_intraday", "2026-10-23", _intraday_rows("A", 3))
    store.write_rows(tmp_path, "stock_intraday", "2026-10-23", _intraday_rows("B", 3))
    frame = store.read_partition(tmp_path, "stock_intraday", "2026-10-23")
    assert frame.height == 6
