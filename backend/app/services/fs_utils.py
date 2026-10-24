"""文件系统小工具 — 原子写等。

历史遗留: json_report_store / strategy_cache / kline_sync 等模块里各有一份内联的
同款原子写。新代码统一用本模块的 atomic_write_text, 一处实现一处维护。
"""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:  # 只做类型标注; JSON 原子写的调用方 (preferences/secrets) 不必加载 polars
    import polars as pl

logger = logging.getLogger(__name__)


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """临时文件 + os.replace 原子替换, 避免读侧读到半截 JSON。

    `mode` 在替换*之前*打到临时文件上。凭证类文件如果先落盘再 chmod, 中间有一段
    以默认权限存在的窗口; 先改临时文件就没有这个窗口。Windows 上 chmod 只影响
    只读位, 失败不该让写入失败, 所以吞掉 OSError, 与原调用处的处理一致。

    临时文件必须每次唯一 (见 `_unique_tmp_path`), 与 `atomic_write_parquet` 同款:
    固定叫 `<name>.tmp` 时, 同一进程的多个线程会往**同一个**临时文件里写 ——
    一个线程 `os.replace` 把它搬走, 另一个线程紧接着 `os.replace` 已不存在的源就抛
    FileNotFoundError; 更糟的是 `write_text` 会先截断已有临时文件, 此刻被搬走就是
    把**空文件**提交成正式文件, 读侧读到空串 (配置被静默清空)。
    """
    tmp = _unique_tmp_path(path)
    try:
        tmp.write_text(text, encoding="utf-8")
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        _discard_quietly(tmp)


def _unique_tmp_path(path: Path) -> Path:
    """临时文件名带上 pid 与随机后缀。

    固定叫 `<name>.tmp` 时, 同一进程的多个线程 (以及多个进程) 会往**同一个**临时
    文件里写: 一个线程 write_parquet 写完, 另一个线程 `replace` 把它搬走, 前一个
    线程紧接着 `replace` 同一个已不存在的源就抛 FileNotFoundError; 即使不抛, 后来
    的写入也会覆盖先前的数据 —— 合并读到的 previous 已经过期, 更新就此丢失。
    唯一化之后每个写入方各写各的临时文件, `os.replace` 仍是原子替换。
    """
    token = secrets.token_hex(4)
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{token}.tmp")


def _discard_quietly(tmp: Path) -> None:
    """写入或替换失败时清掉自己的临时文件, 不留垃圾。"""
    try:
        tmp.unlink()
    except OSError:
        pass


def atomic_write_parquet(df: pl.DataFrame, path: Path) -> None:
    """parquet 版原子写: 先写唯一临时文件再替换, 与 repository / kline_sync 的
    `_atomic_write_parquet` 同语义。

    直接 `df.write_parquet(path)` 在进程被 kill (dev.sh 清端口用 kill -9)、断电或
    磁盘写满时会留下半截文件, 之后读侧 `read_parquet` / `scan_parquet` 整条报错。
    `.tmp` 后缀不匹配 `*.parquet` glob, 不会被视图误读。Windows 下目标正被并发读取时
    由 `replace_with_retry` 短退避穿过。

    临时文件必须每次唯一 (见 `_unique_tmp_path`): 固定的 `<name>.tmp` 会让并发写
    互相搬走对方的源文件, 结果是丢写 + FileNotFoundError。原子替换这一步本身只
    保证「读侧看不到半截文件」, 不保证「并发写不丢」。
    """
    from app.tickflow.repository import replace_with_retry  # 惰性导入, 避免模块级环

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp_path(path)
    try:
        df.write_parquet(tmp)
        replace_with_retry(tmp, path)
    finally:
        _discard_quietly(tmp)



# --------------------------------------------------------------------------- #
# 分区级进程内互斥
# --------------------------------------------------------------------------- #

_partition_locks: dict[str, threading.Lock] = {}
_partition_locks_guard = threading.Lock()


def partition_lock(key: str) -> threading.Lock:
    """按分区键取一把进程内互斥锁, 用于「读旧分区 → 合并 → 写回」串行化。

    并发写同一分区时, 两个线程都先读到同一份旧内容, 各自合并后分别 ``os.replace``,
    后落盘的那份把先落盘的整份覆盖掉 —— 另一个线程写入的行直接消失, 而且**不报错**。
    ``atomic_write_*`` 的唯一临时文件只保证两边不会互相搬走源文件, 挡不住这种逻辑覆盖。

    锁按业务分区键 (如 ``pe_ttm`` / ``research_report:2026-10-23``) 分粒度, 不同
    分区互不影响。锁对象常驻于字典, 因此不会出现「一个线程持有 A 锁、另一个线程拿到
    新建的 B 锁」的分裂。

    范围限定: 只做**进程内线程**互斥。跨进程 (例如 web 进程与独立回补脚本同时写同
    一分区) 由 `partition_file_lock` 的文件锁负责; `partition_writer` 把两者叠起来用,
    业务代码一般应该调它而不是直接调本函数。
    """
    with _partition_locks_guard:
        lock = _partition_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _partition_locks[key] = lock
        return lock


# --------------------------------------------------------------------------- #
# 分区级跨进程互斥 (文件锁)
# --------------------------------------------------------------------------- #

class PartitionLockBusy(RuntimeError):
    """分区正被另一个进程写, 本次合并放弃。

    继承 ``RuntimeError`` 而不是自己抛一个无关的异常类型: 调用方本来就都在
    ``try/except Exception`` 里收采集失败, 让这个错误走同一条 visible 通道,
    比新造一条没人接的异常链安全。真正要紧的是它**不会被静默吞掉**。
    """


_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
"""锁文件名里只留这些字符, 其余一律换成 ``_``。

分区键会带交易日 (``stock:2026-10-23``), 冒号在 Windows 上是非法文件名字符;
直接拼进路径还会让 ``../`` 之类的键跳出锁目录。"""
_HASHED_KEY_LIMIT = 80
"""超过这个长度的键改为「可读前缀 + 内容哈希」, 避免文件名超过 255 字节上限。"""


def _lock_file_for(lock_dir: Path, key: str) -> Path:
    """把业务键映射成锁文件路径 —— 同一个 key 在任何进程里都必须落到同一个文件。"""
    safe = _UNSAFE_KEY_CHARS.sub("_", key).strip("._") or "empty"
    if len(safe) > _HASHED_KEY_LIMIT:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        safe = f"{safe[:_HASHED_KEY_LIMIT]}-{digest}"
    return lock_dir / f"{safe}.lock"


def _try_flock(handle) -> bool:
    """拿一次非阻塞独占锁。拿不到返回 False, 真出错抛原异常。

    Windows 用 ``msvcrt.locking`` (锁 1 字节), 其余用 ``fcntl.flock`` —— 与
    ``mining_process_lock._try_lock_file`` 同一套做法, 平台差异只在这里出现一次。
    """
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        # EWOULDBLOCK 与 EAGAIN 在 Linux 上同值; 分开写只是为了兼顾别的平台。
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        raise
    return True


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def partition_file_lock(
    lock_dir: Path,
    key: str,
    *,
    on_busy: str = "warn",
) -> Iterator[bool]:
    """分区级**跨进程**互斥: 拿不到锁就 ``yield False``, 绝不阻塞等待。

    参考 ``earnings_sync._exclusive`` 的语义 —— 那一份是「整批同步抢占一把大锁」,
    这里是「按分区键各占各的」, 粒度对齐 `partition_lock`。

    **不做进程内可重入**: ``flock`` 挂在「打开的文件」上, 同一进程再 open 一次同一
    路径拿到的是新描述符, 对已持有的锁是非阻塞冲突。所以"先拿线程锁再拿文件锁"的
    顺序写反 (先文件锁后线程锁) 会让同一进程的两个线程互相判成「对方占着」。
    正确的组合见 `partition_writer`。

    锁文件旁路存放在 ``<lock_dir>/.locks/<safe_key>.lock``: 不锁进 parquet 本身 ——
    目标文件会被 ``os.replace`` 换掉, 锁跟着旧 inode 消失, 互斥就名存实亡。

    ``on_busy``: 拿不到锁这一条记什么日志。``"warn"`` (默认) 用于调用方会把结果
    变成异常/错误状态的场景, ``"info"`` 用于调用方会自己重试、失败已经可见的场景
    —— 重试期间每次冲突都打 warning 会把日志刷满, 而真正的结论在重试耗尽之后。

    范围限定: 只对**同一台机器、同一个文件系统**上的进程有效。NFS/SMB 上的 ``flock``
    语义依服务端实现而定, 不保证跨主机互斥; 多机部署请把 ``lock_dir`` 指到本地盘,
    或用外部锁服务。
    """
    lock_dir = Path(lock_dir) / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = _lock_file_for(lock_dir, key)
    handle = path.open("a+b")
    try:
        if not _try_flock(handle):
            log = logger.info if on_busy == "info" else logger.warning
            log("分区文件锁被占用, 本次放弃合并: %s (%s)", key, path)
            yield False
            return
        try:
            yield True
        finally:
            _unlock_file(handle)
    finally:
        handle.close()


@contextmanager
def partition_writer(
    lock_dir: Path,
    key: str,
    *,
    retry_s: float = 0.0,
    attempts: int = 10,
    on_busy: str = "warn",
) -> Iterator[None]:
    """「读旧分区 → 合并 → 写回」整段串行化: 先线程锁, 再文件锁。

    两层缺一不可:
    - ``partition_lock(key)`` 挡住**同进程其它线程**。它不可省 —— 见 `partition_file_lock`
      的说明, ``flock`` 对同进程的第二个描述符同样判冲突, 只靠文件锁的话同进程两个
      线程会有一个直接放弃, 明明是"自己人"却白白丢一次写。
    - ``partition_file_lock`` 挡住**其它进程** (web 进程 vs 独立回补脚本)。

    ``retry_s``: 拿不到文件锁时的退避间隔, 0 表示不等直接失败。参考
    ``replace_with_retry`` 的做法 —— 争锁的窗口通常是一个"读分区 + 合并 + replace",
    亚秒到几秒级; 短退避几次就能穿过, 把一次本来该成功的采集变成一次失败反而是更坏
    的结果。``attempts`` 次仍拿不到就抛, 不让它无限等下去。

    拿不到锁时抛 `PartitionLockBusy`。这里不做静默降级: 一次"以为写成功、实际整段
    合并没发生"的静默丢弃, 比一次可见的失败难查得多。重试全部失败时额外打一条
    warning, 保证「有人等了 10 次还是没写进去」这件事在日志里留得下痕迹。
    """
    with partition_lock(key):
        last_attempt = max(1, attempts)
        for attempt in range(last_attempt):
            with partition_file_lock(lock_dir, key, on_busy=on_busy) as acquired:
                if acquired:
                    yield
                    return
            if attempt < last_attempt - 1 and retry_s > 0:
                time.sleep(retry_s)
        logger.warning(
            "分区 %s 的文件锁重试 %d 次仍被占用, 本次写入失败(可见)", key, last_attempt,
        )
        raise PartitionLockBusy(f"分区 {key} 正被另一个进程写入, 本次合并已放弃")
