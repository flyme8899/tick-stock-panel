"""VOLUME 触发证据表薄模块（批次 20261101gevid · P-bss-evidence · #-A-#9）。

## 这是什么

把 tip **已有**的两处负证据形式化成可验的**触发表行**（契约层）：

- `volume_breakout_stub.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE`（代码里的 Mapping）
- `app/custom/dsa/quant_evidence.yaml` · `volume_breakout`（回测原始结论）

version = `volume_breakout_evidence_table_v1`。四行，对应四个突破口径。

**这是形式化，不是新证据。** 四个 `return` 全部来自 yaml 原文，四个 `sample_size`
同样。本模块不产生任何新数字 —— 它只是把「我们已经有的负证据」从两个散落的
Mapping/文件，收成一组能被 `iter_rows()` 逐行读、能被测例逐项比对的表行。

## 它不是什么

- **不是** 真翻 TRIGGERING：这是表，不是闸
- **不是** 可放量：表里四行**全负**，恰恰是可放量的反证
- **不是** 阈值已锁：表里没有量比阈值、没有突破幅度阈值、没有窗口分钟数
- **不是** 完整触发引擎 / 调度 / 真算量比
- **不是** 新造 returns / sample（**禁止 fabricate**）

## 为什么这张表反而挡在闸前

四行 `return` 是 `-23.86 / -32.44 / -13.80 / -19.81` —— 四个口径**全负**，无一例外。
摆在这里不是为了让谁"看到数据就开跑"，而是让"补上数据就开跑"的冲动先撞上它。

**表 PRESENT ≠ 可翻闸。** 一张全负的表存在，只说明"我们知道它过去是亏的"，
不说明"现在可以放量"。恰恰相反：开闸之前必须先解释这个负号 —— 负号没解释，
表再完整也只是把问题钉在了墙上。

## 表行 strongly_negative ≠ 阈值已锁

`verdict = strongly_negative` 是对**回测结果**的判断，不是对**阈值**的锁定。
两者的区别是：

| 问题 | 谁回答 | 本模块 |
|---|---|---|
| 过去这四个口径赚钱吗？ | `verdict` / `return` | ✅ 回答（全负） |
| 量比多少才算放量？ | 阈值标定 | ❌ **不回答** |
| 突破幅度几个点？ | 阈值标定 | ❌ **不回答** |
| 窗口几分钟？ | 阈值标定 | ❌ **不回答** |

所以 `threshold_lock_data_pending` **仍然开着** —— 本拍只清了「无触发表」这一条，
没有、也不该顺手把阈值那条一起清掉。

## 为什么用**模块别名**而不是 `from x import CONST`

交叉比对要读 `VOLUME_BREAKOUT_NEGATIVE_EVIDENCE` 这个 Mapping。若写
`from .volume_breakout_stub import VOLUME_BREAKOUT_NEGATIVE_EVIDENCE`，import 期就把
Mapping 对象**拷进本模块命名空间** —— 之后有人 monkeypatch 改了源模块的 Mapping，
本模块拿的还是旧对象，交叉断言就退化成**永远为真的守卫**，负控会假绿。

`from . import volume_breakout_stub as _vol` 是**活引用**：每次读都回到源模块。

## 门禁大字

表 PRESENT ≠ 可翻闸 ≠ 阈值已锁 ≠ 可放量；
负证据表 PRESENT ≠ 可开 TRIGGERING；
表行 strongly_negative ≠ 阈值已锁；
清零一刀 ≠ 可翻闸；
路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING；
K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING；
门禁契约 ≠ 授权翻闸；合入 ≠ 部署；tip = prod。

批次 20261101gevid · P-bss-evidence。
仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。
成本来回 18.2bp。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final

from . import volume_breakout_stub as _vol

__all__ = [
    "VOLUME_BREAKOUT_EVIDENCE_TABLE_KEY",
    "VOLUME_BREAKOUT_EVIDENCE_TABLE_POLARITY",
    "VOLUME_BREAKOUT_EVIDENCE_TABLE_ROWS",
    "VOLUME_BREAKOUT_EVIDENCE_TABLE_SOURCE",
    "VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT",
    "VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION",
    "assert_rows_match_negative_evidence",
    "assert_table_present",
    "iter_rows",
]

# ---------------------------------------------------------------- 版本与出处

VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION: Final = "volume_breakout_evidence_table_v1"
"""触发表契约版本。

改行数 / 改口径 / 改出处**必须** bump。把数字改了却不 bump，
等于让"表被改过"这件事在版本号上不可见。
"""

VOLUME_BREAKOUT_EVIDENCE_TABLE_SOURCE: Final = "app/custom/dsa/quant_evidence.yaml"
VOLUME_BREAKOUT_EVIDENCE_TABLE_KEY: Final = "volume_breakout"
VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT: Final = "strongly_negative"
VOLUME_BREAKOUT_EVIDENCE_TABLE_POLARITY: Final = "negative"
"""行极性：四行**全负**。这不是标签，是这四行唯一的共同事实。"""

# ---------------------------------------------------------------- 表行（**仅** tip 已有数字）

VOLUME_BREAKOUT_EVIDENCE_TABLE_ROWS: Final[tuple[Mapping[str, Any], ...]] = (
    {
        "matched": "放量创60日新高",
        "return": -23.86,
        "sample_size": 78,
        "source": VOLUME_BREAKOUT_EVIDENCE_TABLE_SOURCE,
        "key": VOLUME_BREAKOUT_EVIDENCE_TABLE_KEY,
        "verdict": VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT,
        "polarity": VOLUME_BREAKOUT_EVIDENCE_TABLE_POLARITY,
    },
    {
        "matched": "趋势突破",
        "return": -32.44,
        "sample_size": 77,
        "source": VOLUME_BREAKOUT_EVIDENCE_TABLE_SOURCE,
        "key": VOLUME_BREAKOUT_EVIDENCE_TABLE_KEY,
        "verdict": VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT,
        "polarity": VOLUME_BREAKOUT_EVIDENCE_TABLE_POLARITY,
    },
    {
        "matched": "平台整理突破",
        "return": -13.80,
        "sample_size": 79,
        "source": VOLUME_BREAKOUT_EVIDENCE_TABLE_SOURCE,
        "key": VOLUME_BREAKOUT_EVIDENCE_TABLE_KEY,
        "verdict": VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT,
        "polarity": VOLUME_BREAKOUT_EVIDENCE_TABLE_POLARITY,
    },
    {
        "matched": "布林突破",
        "return": -19.81,
        "sample_size": 110,
        "source": VOLUME_BREAKOUT_EVIDENCE_TABLE_SOURCE,
        "key": VOLUME_BREAKOUT_EVIDENCE_TABLE_KEY,
        "verdict": VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT,
        "polarity": VOLUME_BREAKOUT_EVIDENCE_TABLE_POLARITY,
    },
)
"""四行触发表数据 —— 逐字来自 `quant_evidence.yaml` · `volume_breakout`。

**禁止改数字。** 改了就变成一份本仓自己造的回测结论，而我们从没跑过这个回测。
四个 `return` 全负（-23.86 / -32.44 / -13.80 / -19.81），四个 `sample_size`
（78 / 77 / 79 / 110）与 yaml 同序。

`matched` 是**口径名**，不是股票名、不是信号名，更不是可执行标的。
"""


# ---------------------------------------------------------------- 只读 helper


def iter_rows() -> Iterator[Mapping[str, Any]]:
    """逐行产出触发表。只读，不返回内部可变对象。

    每次 `yield` 一个新 `dict`：调用方改了返回值也污染不到 `..._ROWS`。
    若直接 `yield` 元组里的原 dict，某个调用方 `row["return"] = 0` 就会把
    契约数据改掉，而且下次读到的是被改过的值 —— 那是最难查的一类污染。
    """
    for row in VOLUME_BREAKOUT_EVIDENCE_TABLE_ROWS:
        yield dict(row)


def assert_table_present() -> None:
    """表必须在场、四行、极性全负、字段齐全（由测例调用；import 不硬 raise）。"""
    if not VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION:
        raise AssertionError("VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION must be non-empty")
    if VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION != "volume_breakout_evidence_table_v1":
        raise AssertionError(
            "VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION must be "
            "'volume_breakout_evidence_table_v1', "
            f"got {VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION!r}"
        )
    rows = list(iter_rows())
    if len(rows) != 4:
        raise AssertionError(f"evidence table must have 4 rows, got {len(rows)}")

    required_fields = (
        "matched",
        "return",
        "sample_size",
        "source",
        "key",
        "verdict",
        "polarity",
    )
    for index, row in enumerate(rows):
        missing = [f for f in required_fields if f not in row]
        if missing:
            raise AssertionError(f"row #{index} missing fields: {missing}")
        if row["polarity"] != "negative":
            raise AssertionError(
                f"row #{index} polarity must be 'negative', got {row['polarity']!r}"
            )
        if not isinstance(row["return"], (int, float)):
            raise AssertionError(f"row #{index} return must be numeric")
        if not isinstance(row["sample_size"], int):
            raise AssertionError(f"row #{index} sample_size must be int")


def assert_rows_match_negative_evidence() -> None:
    """表行必须与 `VOLUME_BREAKOUT_NEGATIVE_EVIDENCE` **逐项相等**。

    这是「不 fabricate」的**结构保证**：不是靠人记得"我只抄了原数"，
    而是靠一条可跑的等式。有人改了表里的 return 而没改 stub（或反过来），
    这里当场炸。

    只比 stub 里**有**的两项（`returns` / `matched`）：`sample_sizes` 在 yaml 里、
    stub Mapping 没抄，所以那一项由测例直接对 yaml 原文核。
    """
    negative = _vol.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE
    rows = list(iter_rows())

    table_returns = tuple(row["return"] for row in rows)
    stub_returns = tuple(negative.get("returns", ()))
    if table_returns != stub_returns:
        raise AssertionError(
            f"table returns drifted from NEGATIVE_EVIDENCE: "
            f"{table_returns} != {stub_returns}"
        )

    table_matched = tuple(row["matched"] for row in rows)
    stub_matched = tuple(negative.get("matched", ()))
    if table_matched != stub_matched:
        raise AssertionError(
            f"table matched drifted from NEGATIVE_EVIDENCE: "
            f"{table_matched} != {stub_matched}"
        )

    if negative.get("verdict") != VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT:
        raise AssertionError(
            "verdict drifted from NEGATIVE_EVIDENCE: "
            f"{negative.get('verdict')!r} != {VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT!r}"
        )
    if negative.get("source") != VOLUME_BREAKOUT_EVIDENCE_TABLE_SOURCE:
        raise AssertionError(
            "source drifted from NEGATIVE_EVIDENCE: "
            f"{negative.get('source')!r} != {VOLUME_BREAKOUT_EVIDENCE_TABLE_SOURCE!r}"
        )
    if negative.get("key") != VOLUME_BREAKOUT_EVIDENCE_TABLE_KEY:
        raise AssertionError(
            "key drifted from NEGATIVE_EVIDENCE: "
            f"{negative.get('key')!r} != {VOLUME_BREAKOUT_EVIDENCE_TABLE_KEY!r}"
        )
