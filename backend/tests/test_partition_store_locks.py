"""三个分区 store 的「读改写」并发安全。

回归的是同一类问题: ``write_rows`` / ``upsert_pe_rows`` 的「读旧分区 → 合并 → 写回」
原先整段在锁外, 两个线程都读到同一份旧内容, 各自合并后分别 ``os.replace`` ——
后落盘的那份把先落盘的整份覆盖掉, 另一个线程写入的行直接消失, 而且**不报错**。

``atomic_write_parquet`` 的唯一临时文件只解决「互相搬走源文件」(FileNotFoundError),
挡不住这种逻辑覆盖, 所以每个 partition 还要一把进程内互斥锁。

断言口径统一: 每组线程写自己独立的主键前缀, 全部结束后分区行数必须等于组数 × 每组
行数 —— 少了就是丢了。
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.event_context import store as event_store
from app.research_reports import store as report_store
from app.selection_fields import store as selection_store


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


# --------------------------------------------------------------------------- #
# selection_fields: upsert_pe_rows
# --------------------------------------------------------------------------- #

def _pe_rows(tag: str, count: int = 25) -> list[dict]:
    return [
        {
            "symbol": f"{tag}{i:05d}",
            "pe_ttm": float(i),
            "as_of": "2026-10-23",
        }
        for i in range(count)
    ]


@pytest.mark.parametrize("tags", ["ABCD", "ABCDEFGH"])
def test_concurrent_upsert_pe_rows_keeps_every_symbol(tmp_path: Path, tags: str) -> None:
    """并发增量合并 PE, 每只股票都要在。"""
    def worker(tag: str) -> None:
        selection_store.upsert_pe_rows(tmp_path, _pe_rows(tag))

    errors = _run_together(worker, tags)
    assert errors == []
    frame = selection_store.read_frame(tmp_path, "pe_ttm")
    assert frame.height == len(tags) * 25
    # 主键唯一性: 合并用的是 unique(keep="last"), 不该出现重复 symbol。
    assert frame["symbol"].n_unique() == frame.height


def test_upsert_pe_rows_still_overwrites_same_symbol(tmp_path: Path) -> None:
    """加锁不改变原有合并语义: 同一 symbol 后写覆盖先写。"""
    selection_store.upsert_pe_rows(tmp_path, [{"symbol": "600519.SH", "pe_ttm": 30.0, "as_of": "d1"}])
    selection_store.upsert_pe_rows(tmp_path, [{"symbol": "600519.SH", "pe_ttm": 31.5, "as_of": "d2"}])
    frame = selection_store.read_frame(tmp_path, "pe_ttm")
    assert frame.height == 1
    assert frame["pe_ttm"][0] == pytest.approx(31.5)
    assert frame["as_of"][0] == "d2"


# --------------------------------------------------------------------------- #
# research_reports: write_rows
# --------------------------------------------------------------------------- #

def _report_rows(tag: str, publish_date: str, count: int = 20) -> list[dict]:
    return [
        {
            "info_code": f"{tag}-{i:04d}",
            "code": f"6000{i:02d}",
            "symbol": f"6000{i:02d}.SH",
            "title": f"t{tag}{i}",
            "broker": "broker",
            "rating": "buy",
            "publish_date": publish_date,
            "kind": "stock",
            "source": "test",
        }
        for i in range(count)
    ]


@pytest.mark.parametrize("tags", ["ABCD", "ABCDEFGH"])
def test_concurrent_report_write_rows_keeps_every_row(tmp_path: Path, tags: str) -> None:
    """并发往同一发布日分区写不同 info_code, 每一行都要保住。"""
    day = "2026-10-23"

    def worker(tag: str) -> None:
        report_store.write_rows(tmp_path, "stock", day, _report_rows(tag, day))

    errors = _run_together(worker, tags)
    assert errors == []
    frame = report_store.read_partition(tmp_path, "stock", day)
    assert frame.height == len(tags) * 20


def test_concurrent_report_write_rows_across_dates_are_independent(tmp_path: Path) -> None:
    """不同发布日的分区各写各的, 互不干扰。"""
    days = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]

    def worker(day: str) -> None:
        report_store.write_rows(tmp_path, "stock", day, _report_rows(day[:2], day, count=10))

    errors = _run_together(worker, days)
    assert errors == []
    for day in days:
        assert report_store.read_partition(tmp_path, "stock", day).height == 10


# --------------------------------------------------------------------------- #
# event_context: write_rows
# --------------------------------------------------------------------------- #

def _margin_rows(tag: str, day: str, count: int = 20) -> list[dict]:
    return [
        {
            "code": f"{tag}{i:05d}",
            "name": f"n{i}",
            "trade_date": day,
            "market": "SH",
            "margin_balance": 1.0,
            "short_balance": 2.0,
            "margin_buy": 3.0,
            "source": "test",
        }
        for i in range(count)
    ]


@pytest.mark.parametrize("tags", ["ABCD", "ABCDEFGH"])
def test_concurrent_event_write_rows_keeps_every_row(tmp_path: Path, tags: str) -> None:
    """融资融券按 (code, trade_date) 合并, 并发写不同 code 不能互相覆盖。"""
    day = "2026-10-23"

    def worker(tag: str) -> None:
        event_store.write_rows(tmp_path, "margin", day, _margin_rows(tag, day))

    errors = _run_together(worker, tags)
    assert errors == []
    frame = event_store.read_partition(tmp_path, "margin", day)
    assert frame.height == len(tags) * 20


def test_event_write_rows_respects_per_kind_keys(tmp_path: Path) -> None:
    """qa 分区的主键是 question_id, 加锁不影响这个语义。"""
    day = "2026-10-23"
    event_store.write_rows(
        tmp_path, "qa", day,
        [{"question_id": "q1", "code": "600519.SH", "question": "a"}],
    )
    event_store.write_rows(
        tmp_path, "qa", day,
        [
            {"question_id": "q1", "code": "600519.SH", "question": "a2"},
            {"question_id": "q2", "code": "600000.SH", "question": "b"},
        ],
    )
    frame = event_store.read_partition(tmp_path, "qa", day)
    assert frame.height == 2
    row = frame.filter(frame["question_id"] == "q1").to_dicts()[0]
    assert row["question"] == "a2"


# --------------------------------------------------------------------------- #
# 锁的粒度: 不同分区不该被同一把锁串起来
# --------------------------------------------------------------------------- #

def test_partition_lock_is_granular_and_stable() -> None:
    """同 key 同锁 (可重入判定靠 id), 不同 key 不同锁。"""
    from app.services.fs_utils import partition_lock

    assert partition_lock("pe_ttm") is partition_lock("pe_ttm")
    assert partition_lock("stock:2026-10-23") is not partition_lock("stock:2026-10-24")
    assert partition_lock("stock:2026-10-23") is not partition_lock("pe_ttm")
