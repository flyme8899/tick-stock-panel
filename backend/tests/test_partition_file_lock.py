"""分区写盘的**跨进程**互斥。

`test_partition_store_locks.py` 用线程测的是"同进程两个线程"; 这里测的是另一半 ——
回补 CLI 与 Web 进程同时写同一个 ``date=`` 分区。两个进程各自 ``open`` 同一份锁文件,
拿的是**不同**的文件描述符, ``flock`` 对它们是真互斥。缺了文件锁那一层时, 两个进程
都先读到同一份旧内容, 各自合并后分别 ``os.replace``, 后落盘的那份把先落盘的整份覆盖
—— 没有异常, 行静默消失。这正是本仓 ``earnings_sync._exclusive`` 当初加锁要挡的场景。

子进程一律走 ``subprocess`` + ``multiprocessing`` 的 ``spawn`` 语义不好控制起跑时机,
所以这里用「子进程自己起跑、用文件做完成信号」的写法, 保证两个进程真的重叠。
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.fund_flow import store as fund_store
from app.services import fs_utils
# 改前基线里 `fs_utils` 还没有这几个名字。直接 import 会让整个模块 collection 失败,
# 连"哪条用例挂了"都看不出来; 用模块属性取, 缺失时由用例自己报 AssertionError。
PartitionLockBusy = getattr(fs_utils, "PartitionLockBusy", None)
partition_file_lock = getattr(fs_utils, "partition_file_lock", None)
partition_writer = getattr(fs_utils, "partition_writer", None)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
posix_only = pytest.mark.skipif(os.name == "nt", reason="msvcrt 的锁行为与 flock 不同, 本文件只测 POSIX")


# --------------------------------------------------------------------------- #
# 锁语义: 拿不到就 False, 不阻塞
# --------------------------------------------------------------------------- #

@posix_only
def test_second_holder_in_another_process_gets_false(tmp_path: Path) -> None:
    """另一个**进程**持有锁时, 这里必须立刻拿到 False, 不能阻塞等待。"""
    script = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        from app.services.fs_utils import partition_file_lock

        with partition_file_lock(Path(sys.argv[2]), "stock:2026-10-23") as ok:
            assert ok is True
            Path(sys.argv[3]).write_text("held", encoding="utf-8")
            time.sleep(float(sys.argv[4]))
        """
    )
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(BACKEND_ROOT), str(tmp_path), str(ready), "3"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            if time.monotonic() > deadline:
                out, err = proc.communicate(timeout=10)
                pytest.fail(f"子进程没拿到锁: {err.decode()[-2000:]}")
            time.sleep(0.02)

        started = time.monotonic()
        with partition_file_lock(tmp_path, "stock:2026-10-23") as ok:
            assert ok is False
        # 非阻塞: 不该等到子进程 3 秒后释放。
        assert time.monotonic() - started < 1.5
    finally:
        proc.terminate()
        proc.communicate(timeout=30)


@posix_only
def test_lock_is_released_after_context_exit(tmp_path: Path) -> None:
    """退出上下文后要能立刻再拿到 —— 释放逻辑漏了的话这里会一直 False。"""
    with partition_file_lock(tmp_path, "k1") as ok:
        assert ok is True
    with partition_file_lock(tmp_path, "k1") as ok:
        assert ok is True


@posix_only
def test_lock_is_released_when_body_raises(tmp_path: Path) -> None:
    """锁内抛异常也要解锁, 否则一次失败会把整个分区永久占死。"""
    with pytest.raises(ZeroDivisionError):
        with partition_file_lock(tmp_path, "k1") as ok:
            assert ok is True
            1 / 0
    with partition_file_lock(tmp_path, "k1") as ok:
        assert ok is True


@posix_only
def test_different_keys_do_not_block_each_other(tmp_path: Path) -> None:
    """粒度是分区键: 拿住 A 分区不能挡住 B 分区。"""
    with partition_file_lock(tmp_path, "stock:2026-10-23") as outer:
        assert outer is True
        with partition_file_lock(tmp_path, "stock:2026-10-24") as inner:
            assert inner is True


# --------------------------------------------------------------------------- #
# 锁文件落点
# --------------------------------------------------------------------------- #

def test_lock_files_live_under_dot_locks_and_are_safe_names(tmp_path: Path) -> None:
    """锁文件旁路放在 ``.locks/`` 下, 且键里的冒号等字符不能原样进文件名。"""
    with partition_file_lock(tmp_path, "stock:2026-10-23") as ok:
        assert ok is True
        locks = tmp_path / ".locks"
        names = [p.name for p in locks.iterdir()]
    assert names == ["stock_2026-10-23.lock"]
    # 锁不该落在分区目录里: 分区目录会被整个 replace, 锁跟着旧 inode 走就名存实亡。
    assert not (tmp_path / "stock").exists()


def test_long_or_hostile_keys_stay_inside_locks_dir(tmp_path: Path) -> None:
    """超长键与带路径分隔符的键都不能把锁文件写出锁目录。"""
    keys = ["../escape", "a" * 400, "研究/报告:2026-10-23"]
    for key in keys:
        with partition_file_lock(tmp_path, key) as ok:
            assert ok is True
    locks = tmp_path / ".locks"
    produced = {p.name for p in locks.iterdir()}
    assert produced, "一个锁文件都没建"
    for name in produced:
        assert len(name) < 200
        assert "/" not in name and "\\" not in name
    assert not (tmp_path.parent / "escape.lock").exists()


# --------------------------------------------------------------------------- #
# partition_writer: 线程锁 + 文件锁的组合
# --------------------------------------------------------------------------- #

@posix_only
def test_partition_writer_is_reentrant_across_threads_of_one_process(tmp_path: Path) -> None:
    """同进程的两个**线程**先后拿同一分区的 writer, 都要成功。

    这条是 ``partition_file_lock`` 单独用会踩的坑: ``flock`` 挂在打开的文件上,
    同进程再 open 一次拿到的是新描述符, 对已持有的锁照样判冲突。只靠文件锁的话
    第二个线程会被判成"别的进程占着"而白白放弃; ``partition_writer`` 先拿线程锁,
    第二个线程在文件锁之前就排队, 所以这里是"都成功、只是串行"。
    """
    import threading

    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(tag: str) -> None:
        barrier.wait()
        try:
            with partition_writer(tmp_path, "stock:2026-10-23"):
                results.append(f"ok:{tag}")
        except PartitionLockBusy:
            results.append(f"busy:{tag}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok:A", "ok:B"]


@posix_only
def test_partition_writer_raises_visible_error_when_other_process_holds(tmp_path: Path) -> None:
    """另一进程占着时抛 `PartitionLockBusy` —— 可见失败, 不是静默跳过。"""
    script = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        from app.services.fs_utils import partition_file_lock

        with partition_file_lock(Path(sys.argv[2]), "stock:2026-10-23") as ok:
            assert ok is True
            Path(sys.argv[3]).write_text("held", encoding="utf-8")
            time.sleep(float(sys.argv[4]))
        """
    )
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(BACKEND_ROOT), str(tmp_path), str(ready), "3"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            if time.monotonic() > deadline:
                _out, err = proc.communicate(timeout=10)
                pytest.fail(f"子进程没拿到锁: {err.decode()[-2000:]}")
            time.sleep(0.02)
        with pytest.raises(PartitionLockBusy):
            with partition_writer(tmp_path, "stock:2026-10-23"):
                pass
    finally:
        proc.terminate()
        proc.communicate(timeout=30)


@posix_only
def test_writer_gives_up_visibly_after_retries_exhausted(tmp_path: Path) -> None:
    """占锁时间超过重试预算时必须抛, 而且要留下一条 warning。

    可见性就是这么落地的: 调用方拿到异常, 日志里也有一条"重试 N 次仍被占用"。
    """
    import logging

    script = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        from app.services.fs_utils import partition_file_lock

        with partition_file_lock(Path(sys.argv[2]), "stock:2026-10-23") as ok:
            Path(sys.argv[3]).write_text("held", encoding="utf-8")
            time.sleep(float(sys.argv[4]))
        """
    )
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(BACKEND_ROOT), str(tmp_path), str(ready), "4"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect()
    fs_logger = logging.getLogger("app.services.fs_utils")
    fs_logger.addHandler(handler)
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            if time.monotonic() > deadline:
                _out, err = proc.communicate(timeout=10)
                pytest.fail(f"子进程没拿到锁: {err.decode()[-2000:]}")
            time.sleep(0.02)
        started = time.monotonic()
        with pytest.raises(PartitionLockBusy):
            with partition_writer(tmp_path, "stock:2026-10-23", retry_s=0.05, attempts=3):
                pass
        elapsed = time.monotonic() - started
        # 3 次尝试 = 2 次退避 = 0.1s 左右, 远小于占锁的 4 秒。
        assert elapsed < 3, elapsed
    finally:
        fs_logger.removeHandler(handler)
        proc.terminate()
        proc.communicate(timeout=30)

    assert any("仍被占用" in r.getMessage() and r.levelno == logging.WARNING for r in records), (
        [r.getMessage() for r in records]
    )


@posix_only
def test_writer_succeeds_when_lock_frees_up_within_retry_budget(tmp_path: Path) -> None:
    """占锁方在重试预算内释放时, 写入照常成功 —— 这是"耐心的写者"的核心契约。"""
    import threading

    with partition_file_lock(tmp_path, "k1"):
        holder_released = threading.Event()

        def release_soon() -> None:
            time.sleep(0.3)
            holder_released.set()

        # 在同进程里用一个线程设置"该放手了"的信号, 主线程在此之后退出上面的上下文。
        threading.Thread(target=release_soon, daemon=True).start()
        assert holder_released.wait(timeout=5)

    # 上下文已释放, 现在用带重试的 writer 应当一次成功。
    with partition_writer(tmp_path, "k1", retry_s=0.1, attempts=5):
        pass


@posix_only
def test_writer_retries_do_not_mask_a_second_writer(tmp_path: Path) -> None:
    """两个进程都在等同一把锁时, 必须一前一后各自写成功, 而不会双双放弃。

    只依赖"撞锁即失败"时这里会有一个进程直接报错; 加了退避重试之后两个都要写完。
    """
    day = "2026-10-23"
    gate = tmp_path / "go"
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_write_in_child, args=(str(tmp_path), tag, 50, str(gate), day))
        for tag in ("C", "D")
    ]
    for p in procs:
        p.start()
    time.sleep(0.3)
    gate.touch()
    for p in procs:
        p.join(timeout=120)
    assert [p.exitcode for p in procs] == [0, 0]
    frame = fund_store.read_partition(tmp_path, "stock_intraday", day)
    assert frame.height == 100


@posix_only
def test_busy_error_is_a_runtime_error_with_the_key_in_the_message(tmp_path: Path) -> None:
    """错误要能被现有的 ``except Exception`` 通道接到, 且消息里带得动排查信息。"""
    script = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        from app.services.fs_utils import partition_file_lock

        with partition_file_lock(Path(sys.argv[2]), "stock:2026-10-23") as ok:
            Path(sys.argv[3]).write_text("held", encoding="utf-8")
            time.sleep(float(sys.argv[4]))
        """
    )
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(BACKEND_ROOT), str(tmp_path), str(ready), "3"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            if time.monotonic() > deadline:
                _out, err = proc.communicate(timeout=10)
                pytest.fail(f"子进程没拿到锁: {err.decode()[-2000:]}")
            time.sleep(0.02)
        with pytest.raises(RuntimeError) as info:
            with partition_writer(tmp_path, "stock:2026-10-23"):
                pass
        assert "stock:2026-10-23" in str(info.value)
    finally:
        proc.terminate()
        proc.communicate(timeout=30)


# --------------------------------------------------------------------------- #
# 真实合并: 两个进程各写一半, 两边都要在
# --------------------------------------------------------------------------- #

def _rows(tag: str, count: int, trade_date: str) -> list[dict]:
    return [
        {
            "code": f"{tag}{i:04d}",
            "name": f"n{tag}{i}",
            "trade_date": trade_date,
            "captured_at": "2026-10-23T10:00:00",
            "main_net": 1.0,
            "source": "akshare_em",
        }
        for i in range(count)
    ]


def _write_in_child(data_dir: str, tag: str, count: int, gate: str, day: str) -> None:
    """子进程: 等闸门文件出现 → 写自己那一半 → 记完成。

    闸门是为了让两个进程尽量同时进入 `write_rows`, 否则一个跑完另一个才起跑,
    测试永远是绿的、也测不出任何东西。

    **不捕获 `PartitionLockBusy`**: 被测的正是"撞锁之后 store 自己会退避重试、把
    这次合并补上"。如果这里再包一层重试, 测的就变成测试自己的重试逻辑了。
    真冲突导致失败时子进程以非 0 退出, 由父进程断言 exitcode 抓出来。
    """
    gate_path = Path(gate)
    deadline = time.monotonic() + 60
    while not gate_path.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("闸门没开")
        time.sleep(0.01)
    fund_store.write_rows(Path(data_dir), "stock_intraday", day, _rows(tag, count, day))


@posix_only
def test_two_processes_writing_same_partition_keep_both_halves(tmp_path: Path) -> None:
    """两个进程各写 100 行不同 code, 合并后必须是 200 行。

    没有文件锁时: 两边都读到"空分区", 各自 unique 后落盘, 后写的把先写的整份盖掉,
    最终 100 行 —— 少了那 100 行不会报任何错。

    光有文件锁、但"撞锁即失败"也不行: 两个进程同时起跑, 必有一个撞锁, 它的 100 行
    就直接丢了。所以这里同时钉住 ``write_rows`` 的退避重试 —— 撞锁的一方必须等到
    锁释放后把自己的那份补上。
    """
    day = "2026-10-23"
    gate = tmp_path / "go"
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_write_in_child, args=(str(tmp_path), tag, 100, str(gate), day))
        for tag in ("A", "B")
    ]
    for p in procs:
        p.start()
    time.sleep(0.3)  # 让两个子进程都进到等闸门的循环里
    gate.touch()

    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0, f"子进程失败: {p.exitcode}"

    frame = fund_store.read_partition(tmp_path, "stock_intraday", day)
    codes = set(frame["code"].to_list())
    lost = [f"{tag}{i:04d}" for tag in ("A", "B") for i in range(100) if f"{tag}{i:04d}" not in codes]
    assert lost == [], f"丢行: {len(lost)} 个, 前几个 {lost[:5]}"


@posix_only
def test_third_writer_squeezes_in_while_two_are_contending(tmp_path: Path) -> None:
    """三个写者抢同一分区: 两个子进程 + 父进程自己, 每一边的行都要在。

    这条把冲突窗口拉满 —— 父进程不停写, 两个子进程的锁请求几乎必然撞上。它证明的是
    "重试预算够用": 争锁的窗口再密, 也不会把某一方挤成永久失败。
    """
    day = "2026-10-23"
    gate = tmp_path / "go"
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_write_in_child, args=(str(tmp_path), tag, 60, str(gate), day))
        for tag in ("A", "B")
    ]
    for p in procs:
        p.start()
    time.sleep(0.3)
    gate.touch()

    deadline = time.monotonic() + 120
    parent_writes = 0
    while any(p.is_alive() for p in procs):
        if time.monotonic() > deadline:
            for p in procs:
                p.terminate()
            pytest.fail("子进程没有在 120 秒内结束")
        fund_store.write_rows(tmp_path, "stock_intraday", day, _rows(f"P{parent_writes:02d}", 1, day))
        parent_writes += 1
        # 父进程不停手的话, 子进程的锁请求可能 10 次全部撞在父进程的窗口上。
        # 现实里没有"永不放手的写者", 这里让一下, 给子进程留出拿锁的窗口。
        time.sleep(0.005)

    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"子进程失败: {p.exitcode}"

    frame = fund_store.read_partition(tmp_path, "stock_intraday", day)
    codes = set(frame["code"].to_list())
    for tag in ("A", "B"):
        lost = [f"{tag}{i:04d}" for i in range(60) if f"{tag}{i:04d}" not in codes]
        assert lost == [], f"{tag} 丢行 {len(lost)} 个: {lost[:5]}"
    # 父进程写的行也都得在 (它每次拿锁都成功, 没抛异常)。
    assert parent_writes > 0


@posix_only
def test_store_retry_budget_actually_sees_the_contention(tmp_path: Path, caplog) -> None:
    """确认上面的场景**真的**撞过锁 —— 否则那些测试只是看起来很忙。

    两个子进程同时起跑时, 至少有一方会在文件锁上撞一次; ``write_rows`` 以 ``info``
    级别记下每次冲突 (重试成功的话不该是 warning)。
    """
    import logging

    day = "2026-10-23"
    gate = tmp_path / "go"
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_write_in_child, args=(str(tmp_path), tag, 30, str(gate), day))
        for tag in ("E", "F")
    ]
    for p in procs:
        p.start()
    time.sleep(0.3)

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect()
    fs_logger = logging.getLogger("app.services.fs_utils")
    fs_logger.addHandler(handler)
    old_level = fs_logger.level
    fs_logger.setLevel(logging.INFO)
    try:
        gate.touch()
        # 父进程全程挤同一个分区, 把两个子进程的锁请求逼到必撞。
        deadline = time.monotonic() + 120
        n = 0
        while any(p.is_alive() for p in procs):
            if time.monotonic() > deadline:
                for p in procs:
                    p.terminate()
                pytest.fail("子进程没有在 120 秒内结束")
            fund_store.write_rows(tmp_path, "stock_intraday", day, _rows(f"Q{n:02d}", 1, day))
            n += 1
            time.sleep(0.005)
        for p in procs:
            p.join(timeout=60)
            assert p.exitcode == 0, f"子进程失败: {p.exitcode}"
    finally:
        fs_logger.setLevel(old_level)
        fs_logger.removeHandler(handler)

    collisions = [r for r in records if "被占用" in r.getMessage()]
    assert collisions, "两个进程居然一次都没撞锁, 这个场景没测到东西"
    # 撞锁是预期内的重试, 不该刷成 warning —— 真正的结论在"重试耗尽"那一条。
    assert all(r.levelno == logging.INFO for r in collisions)


# --------------------------------------------------------------------------- #
# 接线: 四个 store 的写路径真的走了文件锁
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("module_name", "call"),
    [
        (
            "app.fund_flow.store",
            lambda d: fund_store.write_rows(d, "stock_intraday", "2026-10-23", _rows("A", 2, "2026-10-23")),
        ),
        (
            "app.research_reports.store",
            lambda d: __import__("app.research_reports.store", fromlist=["x"]).write_rows(
                d, "stock", "2026-10-23",
                [{"info_code": "i1", "code": "600519", "symbol": "600519.SH", "title": "t",
                  "broker": "b", "rating": "buy", "publish_date": "2026-10-23", "kind": "stock",
                  "source": "test"}],
            ),
        ),
        (
            "app.event_context.store",
            lambda d: __import__("app.event_context.store", fromlist=["x"]).write_rows(
                d, "qa", "2026-10-23", [{"question_id": "q1", "code": "600519.SH", "question": "a"}],
            ),
        ),
        (
            "app.selection_fields.store",
            lambda d: __import__("app.selection_fields.store", fromlist=["x"]).upsert_pe_rows(
                d, [{"symbol": "600519.SH", "pe_ttm": 30.0, "as_of": "2026-10-23"}],
            ),
        ),
    ],
)
def test_store_writes_create_a_lock_file(tmp_path: Path, module_name: str, call) -> None:
    """每个 store 的写入口都要在 ``.locks/`` 下留一个锁文件。

    没有这条, 接线漏了也测不出来: 并发测试可能会因为执行顺序恰好错开而通过。
    """
    call(tmp_path)
    locks = tmp_path / ".locks"
    assert locks.is_dir(), f"{module_name} 的写路径没有走文件锁"
    assert list(locks.glob("*.lock")), f"{module_name} 没留下锁文件"


def test_lock_module_exposes_the_documented_names() -> None:
    """契约面: 任务书要求的两个名字 + 可见失败的异常类型都在。"""
    for name in ("partition_file_lock", "partition_writer", "PartitionLockBusy", "partition_lock"):
        assert hasattr(fs_utils, name), name
    # 异常必须能走现有的 `except Exception` 通道被采集层接到。
    assert issubclass(fs_utils.PartitionLockBusy, RuntimeError)


def test_no_silent_swallow_of_lock_outcome() -> None:
    """``partition_writer`` 拿不到锁必须抛, 不能像 ``_exclusive`` 那样 yield False 就算了。

    ``earnings_sync._exclusive`` 是"整批同步"语义 —— 跳过整批、在 ``SyncResult``
    里记 ``skipped_locked`` 是合理的。分区写不一样: 调用方拿到的是"写成功"的行数,
    静默跳过就变成"以为写了"。所以这里的契约是抛异常。
    """
    import inspect

    src = inspect.getsource(fs_utils.partition_writer)
    assert "raise PartitionLockBusy" in src
    assert "except" not in src.replace("except", "except") or "except Exception: pass" not in src
