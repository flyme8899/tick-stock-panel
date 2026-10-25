"""做 T 轮次存储: 协议 + 内存实现 + (默认不可用的)磁盘空壳。

## 默认不落盘

`DiskTRoundRegistryStore` 构造时必须显式 `enable_persist=True`, 默认 False 直接抛
`PersistDisabledError`。即便显式打开, 本批次也只是 `NotImplementedError`。
`main.py` / 日管道里**没有**任何 `t_rounds` 引用。

## 轮次上限在这里守

`upsert()` 在写入前会检查同 `(symbol, trade_date)` 已消耗的轮次。放在 Store 而不是
调用方, 是因为"最多 1 轮"是一条**不变量**, 而不是某个调用点的业务规则 —— 只要
有人能绕过 Store 直接塞记录, 这条不变量就破了。

## 与 `buy_sell_signals.store` 的关系

结构参照, **不共享实例、不 import**。两个包的记录形状与不变量完全不同, 共用一个
Store 只会让两边的约束互相牵制。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import threading
from datetime import date
from typing import Protocol

from . import constants as C
from .models import TRoundRecord, TRoundValidationError
from .state_machine import (
    STATUSES_CONSUMING_QUOTA,
    TRoundStateError,
    check_round_quota,
)


class TRoundRegistryError(RuntimeError):
    """存储层错误基类。"""


class PersistDisabledError(TRoundRegistryError):
    """磁盘存储未显式启用。"""


class TRoundRegistryStore(Protocol):
    """轮次注册表存储协议。"""

    def get(self, t_round_id: str, version: str | None = None) -> TRoundRecord | None: ...

    def upsert(self, record: TRoundRecord) -> TRoundRecord: ...

    def list(
        self,
        *,
        symbol: str | None = None,
        trade_date: date | None = None,
        round_status: str | None = None,
    ) -> list[TRoundRecord]: ...

    def count_rounds(self, symbol: str, trade_date: date) -> int: ...


class InMemoryTRoundRegistryStore:
    """线程安全的内存实现。主键 `(t_round_id, version)`。"""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], TRoundRecord] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 读

    def get(self, t_round_id: str, version: str | None = None) -> TRoundRecord | None:
        """按 id 取。

        不传 `version` 时: 返回 `updated_at` 最新的一版。做 T 轮次不像信号那样有
        "发布指针"的概念, 一条轮次的生命周期是短促的, 最新的就是当前状态。
        """
        with self._lock:
            if version is not None:
                return self._records.get((t_round_id, version))
            candidates = [rec for (rid, _), rec in self._records.items() if rid == t_round_id]
        if not candidates:
            return None
        return max(candidates, key=lambda rec: rec.updated_at)

    def list(
        self,
        *,
        symbol: str | None = None,
        trade_date: date | None = None,
        round_status: str | None = None,
    ) -> list[TRoundRecord]:
        with self._lock:
            values = list(self._records.values())
        if symbol is not None:
            values = [rec for rec in values if rec.symbol == symbol]
        if trade_date is not None:
            values = [rec for rec in values if rec.trade_date == trade_date]
        if round_status is not None:
            values = [rec for rec in values if rec.round_status == round_status]
        return sorted(values, key=lambda rec: (rec.symbol, rec.trade_date, rec.t_round_id))

    def count_rounds(self, symbol: str, trade_date: date) -> int:
        """同 `(symbol, trade_date)` 已**消耗**的轮次数。

        口径与 `state_machine.STATUSES_CONSUMING_QUOTA` 一致: `void_data` 不占名额。
        """
        with self._lock:
            records = [
                rec
                for rec in self._records.values()
                if rec.symbol == symbol and rec.trade_date == trade_date
            ]
        return sum(1 for rec in records if rec.round_status in _CONSUMING)

    # ------------------------------------------------------------ 写

    def upsert(self, record: TRoundRecord) -> TRoundRecord:
        if not isinstance(record, TRoundRecord):
            raise TRoundValidationError("upsert 只接受 TRoundRecord")

        with self._lock:
            key = (record.t_round_id, record.version)
            previous = self._records.get(key)

            # 只有当本次写入**新增**一个消耗名额的轮次时才查配额。
            # 同一条记录的状态推进(open_leg1 → open_leg2)不应重复占名额。
            consumes_now = record.round_status in _CONSUMING
            consumes_before = previous is not None and previous.round_status in _CONSUMING
            if consumes_now and not consumes_before:
                others = [
                    rec
                    for (rid, _ver), rec in self._records.items()
                    if rid != record.t_round_id
                ]
                check_round_quota(others, record.symbol, record.trade_date)
            elif consumes_before and previous is not None:
                # 已是计数态的记录改了 symbol / trade_date, 相当于把名额挪到另一只票
                # 或另一天 —— 那也得重新查。
                if (previous.symbol, previous.trade_date) != (record.symbol, record.trade_date):
                    others = [
                        rec
                        for (rid, _ver), rec in self._records.items()
                        if rid != record.t_round_id
                    ]
                    check_round_quota(others, record.symbol, record.trade_date)

            self._records[key] = record
        return record

    def delete(self, t_round_id: str, version: str | None = None) -> None:
        """删除一个轮次。仅用于测试与作废清理, 不提供"取消占用名额"的语义。"""
        with self._lock:
            if version is not None:
                self._records.pop((t_round_id, version), None)
                return
            for key in [k for k in self._records if k[0] == t_round_id]:
                self._records.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


_CONSUMING = STATUSES_CONSUMING_QUOTA
"""消耗名额的状态 —— **直接引用** `state_machine.STATUSES_CONSUMING_QUOTA`, 不再自带一份。

原来这里复制了一份字面量, 理由写的是"避免两个模块互相引用"。但 Store 本来就
import 了 `state_machine.check_round_quota`, 方向是 Store → state_machine 的单向,
再取一个常量并不会成环。而复制一份的代价在批次 20261025gtrounds_patch 兑现了:
状态机那边的结局桶已经换成 `unreverted_at_cutoff` / `data_truncated` / `limit_locked`,
这里还留着 `incomplete` / `abandoned` —— 于是**同一个 `void_data` 之外的三类轮次,
在状态机里占名额、在 Store 里不占**, 当日配额出现空洞。

两份常量之间的"相等"靠测试守着, 而测试只能守已经写下来的期望; 真脱钩时它
恰好是最后才被想起来的地方。现在改成一个来源, 测试退化为"别名是否还指着原对象"。
"""


class DiskTRoundRegistryStore:
    """磁盘存储空壳。**本批次不接线, 默认构造即拒绝。**"""

    def __init__(self, data_dir: object, *, enable_persist: bool = False) -> None:
        if not enable_persist:
            raise PersistDisabledError(
                "DiskTRoundRegistryStore 需要显式 enable_persist=True 才能构造; "
                "本批次(20261024h)不接线落盘, 也没有开启它的默认路径"
            )
        self._data_dir = data_dir
        raise NotImplementedError(
            "磁盘实现本批次未实现(骨架批次禁止落盘); 若确需落盘, 请先在 "
            "docs/t-rounds-skeleton.md 里补设计评审"
        )

    def get(self, t_round_id: str, version: str | None = None) -> TRoundRecord | None:
        raise NotImplementedError

    def upsert(self, record: TRoundRecord) -> TRoundRecord:
        raise NotImplementedError

    def list(self, **kwargs: object) -> list[TRoundRecord]:
        raise NotImplementedError

    def count_rounds(self, symbol: str, trade_date: date) -> int:
        raise NotImplementedError


__all__ = [
    "DiskTRoundRegistryStore",
    "InMemoryTRoundRegistryStore",
    "PersistDisabledError",
    "TRoundRegistryError",
    "TRoundRegistryStore",
]
