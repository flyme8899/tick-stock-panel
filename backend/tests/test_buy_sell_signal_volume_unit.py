"""P-bss-k4：FreqLock / stub 的评估单位声明闸（补 K4）。

仅供研究，不构成投资建议；执行须人工确认。

## 这个文件测什么，不测什么

**测**：`params` 里出现量相关键时，`volume_unit` 必须显式写成「手」或「股」；
缺 / 非法 / 混形态一律拒评；单位进指纹；空 `params` 仍可建戳。

**不测**：「#7 量审计已过」。本文件全绿只能说明**闸装上了**，不能说明任何一条
放量阈值是对的、更不说明可以开放触发。`TRIGGERING` 仍 `False` 是断言项，不是待办。
"""
from __future__ import annotations

import pytest

from app.buy_sell_signals import hits_fixtures as fx
from app.buy_sell_signals.auction_abnormal_stub import (
    AUCTION_ABNORMAL_TRIGGERING_ENABLED,
)
from app.buy_sell_signals.constants import (
    VOLUME_UNIT_PARAM_KEY,
    VOLUME_UNITS,
    VOLUME_UNIT_TRIGGER_KEYS,
)
from app.buy_sell_signals.freq_lock import (
    FreqLockError,
    FreqLockStamp,
    require_volume_unit,
)
from app.buy_sell_signals.volume_breakout_stub import (
    VOLUME_BREAKOUT_TRIGGERING_ENABLED,
    evaluate_volume_breakout,
)


def _stamp(params: dict) -> FreqLockStamp:
    return FreqLockStamp(
        signal_id="fx.k4.volume_unit",
        definition_version="k4_v1",
        cadence="minute_event",
        asof="2026-10-27",
        params=params,
    )


# ------------------------------------------------------------ ① 常量 / 封闭集


def test_constants_volume_units_closed_set():
    assert VOLUME_UNIT_PARAM_KEY == "volume_unit"
    assert VOLUME_UNITS == frozenset({"手", "股"})
    # 封闭集里不该混进英文 alias —— 那是"同一件事两种写法"的起点
    for alias in ("lot", "share", "hand", "LOT", "SHARE"):
        assert alias not in VOLUME_UNITS


def test_volume_unit_key_itself_is_not_a_trigger_key():
    """`volume_unit` 以 `volume_` 开头，但它自己不能触加强制（否则永远要单位嵌套）。"""
    assert VOLUME_UNIT_PARAM_KEY not in VOLUME_UNIT_TRIGGER_KEYS


# ---------------------------------------------------------------- ② 缺单位拒评


@pytest.mark.parametrize(
    "params",
    [
        {"volume_ratio_min": 2.5},
        {"volume_ratio": 2.5},
        {"volume_ratio_max": 5.0},
        # 前缀规则：将来新增的 `volume_*` 自动落闸，不用回来改表
        {"volume_ma": 20},
        {"volume_window": 30},
        {"breakout_pct_min": 3.0, "volume_ratio_min": 2.5},
    ],
)
def test_require_volume_unit_rejects_missing_when_ratio_key_present(params):
    """含任一量相关键而无 `volume_unit` → 拒（含前缀键，不只看封闭集三个）。"""
    with pytest.raises(FreqLockError):
        require_volume_unit(params)
    # N3：助手装了还不够，构造戳这条入口也必须拒
    with pytest.raises(FreqLockError):
        _stamp(params)


def test_missing_unit_via_from_dict_also_rejected():
    """`from_dict` 不是后门：文件里没写单位，读进来同样拒。"""
    payload = {
        "signal_id": "fx.k4.volume_unit",
        "definition_version": "k4_v1",
        "cadence": "minute_event",
        "asof": "2026-10-27",
        "params": {"volume_ratio_min": 2.5},
        "freq_lock_version": "freq_lock_v1",
    }
    with pytest.raises(FreqLockError):
        FreqLockStamp.from_dict(payload)


def test_missing_unit_error_message_is_greppable():
    """文案要能被 rg 到「缺」与字段名 —— 运维看日志得知道补哪个键。"""
    with pytest.raises(FreqLockError) as exc:
        require_volume_unit({"volume_ratio_min": 2.5})
    msg = str(exc.value)
    assert "volume_unit" in msg
    assert "缺" in msg


# ---------------------------------------------------------------- ③ 非法值拒评


@pytest.mark.parametrize(
    "bad",
    [
        "lot",
        "share",
        "hand",
        "LOT",
        "",
        "  ",
        "手 ",          # 首尾空白
        " 手",
        "千股",          # 不在封闭集
        "万手",
    ],
)
def test_require_volume_unit_rejects_illegal_value(bad):
    with pytest.raises(FreqLockError):
        require_volume_unit({"volume_ratio_min": 2.5, "volume_unit": bad})


def test_require_volume_unit_rejects_none_explicitly():
    """显式 `None` 与"缺键"同等对待 —— 都不能被当成"没填就别管了"。"""
    with pytest.raises(FreqLockError):
        require_volume_unit({"volume_ratio_min": 2.5, "volume_unit": None})


# ---------------------------------------------------------------- ④ 混形态拒评


@pytest.mark.parametrize(
    "mixed",
    [
        ["手", "股"],
        ("手", "股"),
        {"手", "股"},
        "手,股",
        "手，股",
        "手/股",
        0,
        1,
        True,
    ],
)
def test_require_volume_unit_rejects_mixed_form(mixed):
    """多值与拼接一律拒。本拍**不**在这里挑一个、也**不**做隐式 ×100 换算。"""
    with pytest.raises(FreqLockError):
        require_volume_unit({"volume_ratio_min": 2.5, "volume_unit": mixed})


# ---------------------------------------------------------------- ⑤ 合法值放行


@pytest.mark.parametrize("unit", ["手", "股"])
def test_require_volume_unit_accepts_shou_and_gu(unit):
    require_volume_unit({"volume_ratio_min": 2.5, "volume_unit": unit})
    stamp = _stamp({"volume_ratio_min": 2.5, "volume_unit": unit})
    assert stamp.params["volume_unit"] == unit


def test_require_volume_unit_returns_none_not_the_unit():
    """返回值刻意是 `None`：返回单位值会被误读成"现在可以按它换算了"。"""
    assert require_volume_unit({"volume_ratio_min": 2.5, "volume_unit": "手"}) is None
    assert require_volume_unit({}) is None


def test_require_volume_unit_rejects_non_mapping():
    with pytest.raises(FreqLockError):
        require_volume_unit(["volume_ratio_min"])  # type: ignore[arg-type]


# ------------------------------------------------------------ ⑥ 单位进指纹


def test_volume_unit_change_changes_fingerprint():
    """同阈值、只换单位 → 指纹必须变。数值一个没动，含义却换了一套口径。"""
    a = _stamp({"volume_ratio_min": 2.5, "volume_unit": "手"})
    b = _stamp({"volume_ratio_min": 2.5, "volume_unit": "股"})
    assert a.fingerprint != b.fingerprint


def test_fixture_gu_variant_differs_only_by_unit_and_changes_fingerprint():
    """夹具侧的旁证：`FIXTURE_FREQ_PARAMS` 与 `..._GU` 只差单位，指纹不同。"""
    diff_keys = {
        k
        for k in set(fx.FIXTURE_FREQ_PARAMS) | set(fx.FIXTURE_FREQ_PARAMS_GU)
        if fx.FIXTURE_FREQ_PARAMS.get(k) != fx.FIXTURE_FREQ_PARAMS_GU.get(k)
    }
    assert diff_keys == {"volume_unit"}
    a = fx.freq_lock_stamp(params=fx.FIXTURE_FREQ_PARAMS)
    b = fx.freq_lock_stamp(params=fx.FIXTURE_FREQ_PARAMS_GU)
    assert a.fingerprint != b.fingerprint


def test_same_unit_same_fingerprint():
    """反向对照：单位相同、其余相同 → 指纹必须一致（否则这条闸就是在制造噪音）。"""
    a = _stamp({"volume_ratio_min": 2.5, "volume_unit": "手"})
    b = _stamp({"volume_ratio_min": 2.5, "volume_unit": "手"})
    assert a.fingerprint == b.fingerprint


# -------------------------------------------------------- ⑦ 空 / 无量键仍放行


def test_empty_params_still_builds_stamp():
    stamp = _stamp({})
    assert dict(stamp.params) == {}


@pytest.mark.parametrize(
    "params",
    [
        {"breakout_pct_min": 3.0},
        {"breakout_pct_min": 3.0, "window_minutes": 30},
        {"window_minutes": 30},
        # 只写单位、没有量键：允许（不强制，但也不能因为"多了个键"就拒）
        {"volume_unit": "手"},
    ],
)
def test_params_without_volume_keys_still_build_stamp(params):
    """无触发键 → 不要求单位。'节拍已定、阈值未标定'是合法中间态。"""
    require_volume_unit(params)
    stamp = _stamp(params)
    assert dict(stamp.params) == params


# ------------------------------------------------------ ⑧ 开关仍关 / 空桩诚实


def test_triggering_flags_remain_false():
    """补 K4 **不是**开放触发。这两条一旦变 True，本拍就该被挡下。"""
    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_stub_never_fills_volume_ratio_and_carries_no_unit():
    """空桩：量比 `None`，单位也 `None` —— 不替未来那个评估器预先挑单位。"""
    stub = evaluate_volume_breakout("600000")
    assert stub.volume_ratio is None
    assert stub.volume_unit is None
    assert stub.to_dict()["volume_ratio"] is None
    assert stub.to_dict()["volume_unit"] is None


def test_stub_evidence_pending_untouched():
    """`VOLUME_BREAKOUT_EVIDENCE_PENDING` 是"还不能跑"的证据，不许为装绿清空。"""
    stub = evaluate_volume_breakout("600000")
    assert stub.evidence_pending
    assert stub.data_pending is True
    assert stub.triggered is None
