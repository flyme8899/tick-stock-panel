"""文件系统小工具 — 原子写等。

历史遗留: json_report_store / strategy_cache / kline_sync 等模块里各有一份内联的
同款原子写。新代码统一用本模块的 atomic_write_text, 一处实现一处维护。
"""
from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只做类型标注; JSON 原子写的调用方 (preferences/secrets) 不必加载 polars
    import polars as pl


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """临时文件 + os.replace 原子替换, 避免读侧读到半截 JSON。

    `mode` 在替换*之前*打到临时文件上。凭证类文件如果先落盘再 chmod, 中间有一段
    以默认权限存在的窗口; 先改临时文件就没有这个窗口。Windows 上 chmod 只影响
    只读位, 失败不该让写入失败, 所以吞掉 OSError, 与原调用处的处理一致。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
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

