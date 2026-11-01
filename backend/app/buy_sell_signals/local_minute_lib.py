"""VOLUME 本机分钟库路径契约 + 可验探针薄模块（批次 20261101gevid3 · P-bss-evidence · #-A-#9）。

## 这是什么

把「本机分钟库到底在不在」这件事，从一句口头旁证变成**可验契约**：

- 约定根：`settings.data_dir / "kline_minute"`（与 `scripts/check_minute_kline.py`
  的 `default_minute_root()` 同一个约定，刻意不另起一套）
- 分区布局：Hive 风格 `date=YYYY-MM-DD/part.parquet`
- 只读探针 `probe_local_minute_lib(root)`：返回 `present` / `partition_count` /
  `date_first` / `date_last` / `sample_path` / `ok`
- version = `local_minute_lib_v1`

**这是路径契约 + 布局探针，不是数据本身。** 5.1G 的分钟 parquet 留在
`data/kline_minute`（gitignore `data/**`），**不**进仓，**不**进 PR，**不**进本模块
的任何常量。测例用 `tmp_path` 造两个最小 `date=*/part.parquet` 目录来验布局，
所以断言跟线上有没有那 5.1G 无关。

## 它不是什么

- **不是** 真翻 TRIGGERING：探针说「目录在」，不等于闸开了
- **不是** calib：这里没有回测、没有阈值、没有量比计算
- **不是** 可放量：目录在 ≠ 该放量
- **不是** 完整触发引擎 / 调度：没有任何调度、没有读数据的常驻循环
- **不是** 行情读取：只 `iterdir()` 看目录名与 `part.parquet` 在不在，不解 parquet

## 为什么探针不能"顺手读一行数据"

一旦这里开始解 parquet，它就会变成「分钟数据可用」的隐式断言 —— 而数据能不能用
是 `check_minute_kline.py` 那类工具的事（它要看 dtype、看重复行、看缺失列）。
本模块只回答一个更窄的问题：**布局在不在**。窄问题才能被 CI 稳定断言；
宽问题会在这里长成第二个数据质量管线。

## 大字

落盘 ≠ calib ≠ 可放量 ≠ TRIGGERING；
路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING；
探针 PRESENT ≠ 可开 TRIGGERING；
清零一刀 ≠ 可翻闸（本模块只是让 `local_minute_lib_data_pending` 这一条有据可清）；
门禁契约 ≠ 授权翻闸；
K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING；
合入 ≠ 部署；tip = prod。

批次 20261101gevid3 · P-bss-evidence。
仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。
成本来回 18.2bp。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "LOCAL_MINUTE_LIB_PARTITION_PREFIX",
    "LOCAL_MINUTE_LIB_PART_NAME",
    "LOCAL_MINUTE_LIB_REL",
    "LOCAL_MINUTE_LIB_VERSION",
    "LocalMinuteLibLayoutError",
    "LocalMinuteLibProbe",
    "assert_local_minute_lib_probeable",
    "probe_local_minute_lib",
    "resolve_local_minute_root",
]

# ---------------------------------------------------------------- 版本与约定

LOCAL_MINUTE_LIB_VERSION: Final = "local_minute_lib_v1"
"""本机分钟库**路径契约**版本。

改相对目录名 / 改分区命名（`date=` 前缀）/ 改 part 文件名 / 改 `ok` 判据
**必须** bump —— 这些是别处（补数脚本、落盘计划、后续读取实现）会照抄的约定，
悄悄改等于让照抄方读到一份过期的地图。
"""

LOCAL_MINUTE_LIB_REL: Final = "kline_minute"
"""相对 `settings.data_dir` 的分钟库目录名。

与 `scripts/check_minute_kline.py::default_minute_root()` 里写死的
`Path(settings.data_dir) / "kline_minute"` **必须**一致。两处不一致的话，
「补数脚本看得到的数据」与「BSS 探针认为存在的数据」会是两个目录 —— 这种分叉
不会报错，只会让两边各说各话。
"""

LOCAL_MINUTE_LIB_PARTITION_PREFIX: Final = "date="
"""Hive 分区目录名前缀。`date=2026-10-30` 这样的子目录才算一个分区。"""

LOCAL_MINUTE_LIB_PART_NAME: Final = "part.parquet"
"""每个分区里的数据文件名。`kline_sync._write_minute_partition` 写的就是它。"""


class LocalMinuteLibLayoutError(AssertionError):
    """布局不符合契约时由 `assert_local_minute_lib_probeable` 抛出。

    继承 `AssertionError` 是刻意的：调用方（测例 / 自检脚本）本来就按"断言失败"
    来理解这件事，不必再引入一个新的异常族去做 `except`。**模块 import 期不抛**
    —— 只有在有人显式调用断言函数时才可能炸。
    """


@dataclass(frozen=True, slots=True)
class LocalMinuteLibProbe:
    """一次**只读**布局探针的结果。

    每个字段都在描述"看到了什么"，而不是"数据能不能用"：

    | 字段 | 含义 | 不含义 |
    |---|---|---|
    | `present` | 根目录存在且是目录 | 不代表有数据 |
    | `partition_count` | 匹配 `date=` 的子目录数 | 不代表分区里有 parquet |
    | `date_first` / `date_last` | 排序首尾的分区名（含 `date=` 前缀原样） | 不代表区间内无空洞 |
    | `sample_path` | 任取一个 `date=*/part.parquet` 的路径 | 不代表该文件可解析 |
    | `ok` | `present and partition_count > 0` | **不代表** calib / 可放量 / 可开 TRIGGERING |

    特别注意 `ok`：它只说明"布局看起来是那个样子"。把它读成"分钟数据可用"
    是本模块最容易被误读的地方 —— 数据质量是 `check_minute_kline.py` 的事。
    """

    present: bool
    partition_count: int
    date_first: str | None
    date_last: str | None
    sample_path: str | None
    ok: bool
    root: str


def resolve_local_minute_root(data_dir: Path | str) -> Path:
    """按约定把 `data_dir` 解析成分钟库根目录：`<data_dir>/kline_minute`。

    只做路径拼接，**不**读盘、**不**建目录。传 `settings.data_dir` 进来就是
    生产约定；传 `tmp_path` 进来就是测例约定 —— 两者走同一条代码，测的才是
    真约定而不是测例自造的假约定。
    """
    return Path(data_dir) / LOCAL_MINUTE_LIB_REL


def probe_local_minute_lib(root: Path | str) -> LocalMinuteLibProbe:
    """只读探一次布局。**不做任何 I/O 写、不解析 parquet、不联网。**

    判据（写死，测例逐条钉住）：

    - 根不存在 / 不是目录 → `present=False`、`partition_count=0`、`ok=False`
    - 根在但没有任何 `date=` 子目录 → `present=True`、`partition_count=0`、`ok=False`
      （**空目录不得报 ok**：目录建好但一条数据都没有，是最容易被当成"已经好了"的状态）
    - 根在且至少一个 `date=` 子目录 → `present=True`、`ok=True`。
      此时**不**要求分区里真有 `part.parquet`：`sample_path` 是"如果能取到就给路径"
      的便利字段，取不到就是 `None`。把它当成 `ok` 的必要条件，会让一个只建了
      空分区目录的中间态把 `ok` 打成 False —— 而那不是本模块要回答的问题。
    """
    root_path = Path(root)
    if not root_path.is_dir():
        return LocalMinuteLibProbe(
            present=False,
            partition_count=0,
            date_first=None,
            date_last=None,
            sample_path=None,
            ok=False,
            root=str(root_path),
        )

    partitions = sorted(
        (p for p in root_path.iterdir() if p.is_dir() and p.name.startswith(LOCAL_MINUTE_LIB_PARTITION_PREFIX)),
        key=lambda p: p.name,
    )
    sample: str | None = None
    for part in reversed(partitions):
        candidate = part / LOCAL_MINUTE_LIB_PART_NAME
        if candidate.is_file():
            sample = str(candidate)
            break

    return LocalMinuteLibProbe(
        present=True,
        partition_count=len(partitions),
        date_first=partitions[0].name if partitions else None,
        date_last=partitions[-1].name if partitions else None,
        sample_path=sample,
        ok=bool(partitions),
        root=str(root_path),
    )


def assert_local_minute_lib_probeable(root: Path | str) -> LocalMinuteLibProbe:
    """由测例 / 自检脚本显式调用；**import 期不炸**。

    两件事：version 非空（防止有人把"版本"删空后仍宣称契约在场），
    以及探针布局 `ok`（根在且有 `date=` 分区）。

    断了**不代表**「可以翻闸了」，只代表「这条路径契约的前提不成立了，
    要么数据没挂、要么约定被改了」。前者属运维，后者属版本 —— 两者都不该
    被解释成闸的状态。
    """
    if not LOCAL_MINUTE_LIB_VERSION:
        raise LocalMinuteLibLayoutError(
            "LOCAL_MINUTE_LIB_VERSION 不能为空 —— 路径契约必须有版本号"
        )
    probe = probe_local_minute_lib(root)
    if not probe.ok:
        raise LocalMinuteLibLayoutError(
            f"本机分钟库布局不符合契约: {probe!r}; "
            f"期望 <data_dir>/{LOCAL_MINUTE_LIB_REL}/"
            f"{LOCAL_MINUTE_LIB_PARTITION_PREFIX}YYYY-MM-DD/{LOCAL_MINUTE_LIB_PART_NAME}。"
            "注意: 这里断只说明路径/布局前提不成立, 与 TRIGGERING 取值无关"
        )
    return probe
