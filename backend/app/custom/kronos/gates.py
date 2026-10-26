"""门禁门闩：Kronos 因子未过门禁时，**拒绝**进信号默认集。

本仓的因子注册表是「注册即可见」的语义：``ext_factors.ensure_synced`` 把扩展表
数值列注册成 ``ext_<config>_<field>``，注册表的条目会自动进
``custom_signals.allowed_fields()``（信号条件字段白名单）。对普通扩展表这是想要
的行为，对**模型预测**不是——一个未过费后 + 样本外的预测分布，如果能被随手选进
买卖信号条件，门禁就形同虚设。

所以这里做三件事，都在**不改 BSS 接线**的前提下完成：

1. ``is_kronos_factor(fid)``：认出 Kronos 因子族（按扩展表 id 前缀，不按硬编码
   列名清单，schema 加列不会漏）。
2. ``assert_allowed_in_signals(fids)``：门闩。未过门禁时把这些 id 加进信号条件
   就是错误，抛 ``KronosGateError`` 并带明确原因；过门禁也只允许「风险降档」用途。
3. ``filter_signal_fields(fields)``：给白名单做减法，未过门禁时把 Kronos 族剔除。

**本模块不 import `buy_sell_signals`，也不往那里写任何东西。** BSS 的接线与默认
因子集本拍零碰；门闩是「拦住别人接进来」，不是「自己接进去」。
"""
from __future__ import annotations

from collections.abc import Iterable

from app.custom.kronos import config
from app.custom.kronos.schema import CONFIG_ID

FACTOR_PREFIX = f"ext_{CONFIG_ID}_"
"""Kronos 因子族在注册表里的 id 前缀（``ext_factors.ext_column_name`` 的产物）。"""


class KronosGateError(ValueError):
    """未过门禁却要把 Kronos 因子接进信号层。"""


def is_kronos_factor(factor_id: str) -> bool:
    return factor_id.startswith(FACTOR_PREFIX)


def kronos_factors(factor_ids: Iterable[str]) -> list[str]:
    return sorted(fid for fid in factor_ids if is_kronos_factor(fid))


def assert_allowed_in_signals(factor_ids: Iterable[str]) -> None:
    """拒绝把未过门禁的 Kronos 因子加进信号条件。过门禁则放行（仅降档用途）。

    为什么不做成「过门禁就随便用」：S3 的结论上限是「仅提醒 / 只降档」，抬升上限
    须另口令 + 新评估包。所以即便 ``gate_passed=true``，这里也只代表「可以当风险
    标签用」，不代表「可以当买入理由」。
    """
    selected = kronos_factors(factor_ids)
    if not selected:
        return
    passed, detail = config.gate_passed()
    if not passed:
        sample = ", ".join(selected[:3]) + ("…" if len(selected) > 3 else "")
        raise KronosGateError(
            f"Kronos 预测因子未过门禁，不能加入信号默认集（{detail}；涉及 {sample}）。"
            "门禁四前置：费后检验口径一致 / 样本外可复核 / 显式 passed / 用途不超出风险降档。"
        )
    # 过门禁也只允许降档用途；加码类用法在 gate 记录里已被 missing_prerequisites 拦下。


def filter_signal_fields(fields: Iterable[str]) -> frozenset[str]:
    """信号字段白名单的减法：未过门禁时剔除 Kronos 族。"""
    items = list(fields)
    passed, _ = config.gate_passed()
    if passed:
        return frozenset(items)
    return frozenset(fid for fid in items if not is_kronos_factor(fid))


def gate_payload() -> dict:
    """门禁自述：给前端/文档/测试看同一份口径。"""
    passed, detail = config.gate_passed()
    return {
        "gate_passed": passed,
        "detail": detail,
        "factor_prefix": FACTOR_PREFIX,
        "prerequisites": list(config.GATE_PREREQUISITES),
        "allowed_usage": "risk_downgrade_only" if passed else "none",
        "blocked_paths": ["custom_signals.allowed_fields", "buy_sell_signals 默认因子集"],
        "note": "抬升结论上限须另口令 + 新评估包；本模块不接线 BSS。",
    }
