"""做 T 成本敏感性 5c · 名义口径与档位换算（批次 20261025gcost5c）。

**仅供研究，不构成投资建议；执行须人工确认。**

## 这一层为什么存在

5b（批次 20261025gcost）已经把**契约**钉死：`constants.COST_BP_TIERS` 是唯一白名单，
`complete_round` 按档位校验金额。但闸门**故意不做**「费率 × 名义金额 → 金额」的换算 ——
因为名义取哪个价（成交价 / 均价 / 收盘价 / VWAP）、手续费最低 5 元、印花税卖出单边，
这些口径一旦写进闸门，就变成「闸门顺手替研究选了口径」，而研究结果的好坏会被记在
闸门的账上。

所以换算落在**这一层**（离线跑表侧），闸门只管「档位合法 + 金额不矛盾」。

## 名义口径：唯一一个式子

```text
notional      = ((leg1_price + leg2_price) / 2.0) * qty
cost_amount   = (cost_rate_pct / 100.0) * notional
```

- **两腿成交价的算术平均 × qty**。与 `complete_round` 里的净价差
  `(卖出腿价 - 买入腿价) * qty` **同一个「价 × 量」量纲**，所以扣掉的成本与赚到的
  价差可以直接相减，不需要任何折算。
- **不用**收盘价 / VWAP / 当日均价另起一套。两套名义并行是这一层最危险的失败模式：
  两个数都"有道理"，所以谁也不会去查，最后报告里的成本数字取决于哪段代码先跑。
  本模块只提供 `notional_avg_legs` 这一个实现，并有测例把它钉成唯一入口。

## 档位网格：与白名单逐档对齐，不新增档位

| `cost_bp_tier` | `cost_rate_pct` | 语义 |
|---|---|---|
| `0bp` | 0 | 显式零成本 |
| `5bp` | 0.05 | 5 bp |
| `10bp` | 0.10 | 10 bp（散户假设下沿） |
| `15bp` | 0.15 | 15 bp（散户假设上沿） |
| `20bp` | 0.20 | 20 bp |

**跑表行不用 `design_pending`。** 它是占位（"没按费率扣过成本"），不是一档成本；
把它放进敏感性表会让表里出现一个"没有费率"的行，而那一行的数字与 `0bp` 未必相同
（`0bp` 是"测过、确实是零"，`design_pending` 是"没测"）。`design_pending` 仍保留为
**对照路径**（旧 `_settle` 的 pending + 0），用来回归验证 5b 契约没被这一拍改坏。

## 散户 0.1%–0.15% 是**假设**，不是事实

`10bp` / `15bp` 标成「散户假设下沿/上沿」，指的是**双边合计**成本落在 0.10%–0.15%
这个常见区间。这是**假设口径**，不是唯一真实成本：

- 各家券商佣金不同（万 1.5 到千 3 都有，且有最低 5 元）；
- 印花税只在卖出单边收（当前 0.05%），本身就占了 5 bp；
- 滑点与冲击成本随标的流动性、下单方式变化，常常比显性费率更大；
- 因此精确的**拆分费率表**（佣金/印花税/滑点各占多少）仍标注 `design_pending`（G1），
  本拍**没有**假装测过它。

读这张表时正确的理解是："如果双边成本是 X，期望值会变成多少" —— 是一条**敏感性
曲线**，不是"真实成本是 X"的断言。

## 本层不做的事

- 不改 `constants.COST_BP_TIERS` 成员，不改 `complete_round` 的金额语义（5b 已钉死）。
- 不绕过 `complete_round` 扣成本。本模块只**算**金额，登记一律由调用方走闸门。
- 不宣称成本已经验证或「信号可做」。跑表能回答的是"成本假设变化时期望值怎么走"，
  回答不了"这个策略该不该上"。
"""

from __future__ import annotations

from typing import Final

from .. import constants as C

# ---------------------------------------------------------------- 档位网格

COST_RATE_PCT_BY_TIER: Final[dict[str, float]] = {
    "0bp": 0.0,
    "5bp": 0.05,
    "10bp": 0.10,
    "15bp": 0.15,
    "20bp": 0.20,
}
"""敏感性档位 → 费率（百分比，**双边合计**）。

键集合必须是 `constants.COST_BP_TIERS` 去掉 `design_pending` 之后的部分 —— 由
`assert_grid_matches_whitelist()` 在导入时校验，不靠人记得同步。

冒烟测例（`test_t_rounds_offline_costs.py`）把这个映射逐键钉住。改这里任何数字都会
让一批断言变红，这是有意的：费率是研究结论的直接输入，改它必须显式面对。
"""

COST_TIERS_SENSITIVITY: Final[tuple[str, ...]] = tuple(COST_RATE_PCT_BY_TIER)
"""敏感性跑表的行顺序（`0bp` → `20bp`）。

用 `tuple(dict)` 而不是另写一份列表：另写一份就可能与 `COST_RATE_PCT_BY_TIER` 的键
不一致，而不一致的表现是"某一档在表里但算不出金额"，或者更糟 —— 静默少一行。
"""

RETAIL_ASSUMPTION_TIERS: Final[tuple[str, ...]] = ("10bp", "15bp")
"""散户假设档位（下沿 / 上沿）。Regime 附报**至少**要给出这两档的 `expectancy_net`。"""

FOOTNOTE_RETAIL_ASSUMPTION: Final = "散户 0.1–0.15% 为假设口径，非唯一真实成本"
"""固定文案，写进每一行跑表与报告元数据。

一字不改地复用：报告、PR、文档三处出现不同措辞时，读者会以为是三个不同的口径。
"""


def assert_grid_matches_whitelist() -> None:
    """导入时自检：档位网格必须与 5b 白名单逐档对齐。

    为什么放在导入期而不是测试里：如果将来有人在 `constants` 里加了 `25bp` 却忘了
    在这里补费率，跑表会因 `KeyError` 崩在一半 —— 那还算好的；更坏的情形是有人
    反过来把这里的某个档位删掉，跑表就静默少一行，而报告看不出少。
    导入期自检让这类不一致**在第一次 import 时就炸**。
    """
    allowed = C.COST_BP_TIERS - {"design_pending"}
    grid = set(COST_RATE_PCT_BY_TIER)
    if grid != allowed:
        missing = sorted(allowed - grid)
        extra = sorted(grid - allowed)
        raise AssertionError(
            "成本档位网格与白名单不一致: "
            f"白名单有但网格缺 {missing}; 网格有但白名单没有 {extra}"
        )
    for tier in RETAIL_ASSUMPTION_TIERS:
        if tier not in grid:
            raise AssertionError(f"散户假设档位 {tier!r} 不在敏感性网格里")


assert_grid_matches_whitelist()


# ---------------------------------------------------------------- 名义 / 换算


def notional_avg_legs(leg1_price: float, leg2_price: float, qty: float) -> float:
    """名义金额 = **两腿成交价算术平均 × qty**（全包唯一实现）。

    为什么用两腿平均而不是卖出腿价或收盘价：

    - 用卖出腿价：卖出腿在正 T 里是 leg1、在倒 T 里是 leg2，成本会随模式换成另一个
      基准价，A/B 对比时"成本"这一项就带上了模式偏置；
    - 用收盘价 / VWAP：与 `complete_round` 赚到的价差不一个量纲，相减没有意义；
    - 两腿平均：两种模式下同一个式子，且与净价差同量纲。

    `qty` 用 float 是为了让「名义金额」这个纯数值函数不挑类型；股数本身在
    `complete_round` 那边仍是正整数校验，这里不重复设卡。
    """
    return ((float(leg1_price) + float(leg2_price)) / 2.0) * float(qty)


def cost_amount_for_tier(
    tier: str,
    *,
    leg1_price: float,
    leg2_price: float,
    qty: float,
) -> float:
    """按档位算出应扣的**绝对金额**。

    `0bp` → `0.0`（精确 0，不是 1e-17 那种浮点残渣）；非零档 → `> 0`。

    这里**不**做「金额是否合法」的判定 —— 那是 `complete_round` 的闸门的事（5b）。
    本函数返回后，调用方必须把结果喂给 `RoundCost` 再走 `complete_round`，由闸门
    复核「非零档必须 > 0」。分两处判断同一个条件看起来重复，但那两处的职责不同：
    这里回答"该扣多少"，闸门回答"这个组合允许存在吗"。把后者也搬进来，闸门就
    又能被绕过了。
    """
    if tier not in COST_RATE_PCT_BY_TIER:
        raise KeyError(
            f"未知成本档位 {tier!r}; 敏感性档位只有 {sorted(COST_RATE_PCT_BY_TIER)}"
            "(跑表行不用 design_pending)"
        )
    rate_pct = COST_RATE_PCT_BY_TIER[tier]
    if rate_pct == 0.0:
        # 显式返回 0.0。这里的作用是**把意图写在代码里**（零费率 → 零金额），
        # 而不是防浮点残渣 —— `(0.0/100)*notional` 本来就是精确 0.0，删掉这个分支
        # 行为不变（变异测试 M4 证实：删了它测试全绿，因为结果确实一样）。
        # 保留它是为了让 `0bp` 的语义在 `costs.py` 里可读，而不是藏在乘法里。
        return 0.0
    return (rate_pct / 100.0) * notional_avg_legs(leg1_price, leg2_price, qty)


__all__ = [
    "COST_RATE_PCT_BY_TIER",
    "COST_TIERS_SENSITIVITY",
    "FOOTNOTE_RETAIL_ASSUMPTION",
    "RETAIL_ASSUMPTION_TIERS",
    "assert_grid_matches_whitelist",
    "cost_amount_for_tier",
    "notional_avg_legs",
]
