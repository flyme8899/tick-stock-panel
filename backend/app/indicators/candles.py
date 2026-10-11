"""A 股日 K 经典蜡烛形态。

数值沿用 TA-Lib ``CDL*``：``100`` 看涨、``-100`` 看跌、``0`` 未出现。
单边形态（锤头、上吊）在 TA-Lib 里也返回 ``100``，这里按语义改成
看跌为 ``-100``，选股条件才能直接写「看涨 / 看跌」。

计算引擎：
- 进程能 ``import talib`` 时走 TA-Lib（Docker 里装得上就用它）。
- 否则用 Polars 实现主体形态。和 TA-Lib 对不齐的形态（三白兵、三只乌鸦、
  上升下降三法、长蜡烛、分离线、弃婴、条形三明治）以及规则更长的冷门形态
  标 ``builtin=False``，选股下拉不提供，也不写出这些列。

只依赖 OHLC。调用方须按标的、日期排好序；本模块会再排一次。
形态窗口很短（实体均值 10 根、星线 3 根），全市场只重算最近几十个
交易日，不扫多年历史。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import polars as pl

BULL = 100
BEAR = -100


@dataclass(frozen=True)
class CandlePattern:
    id: str
    talib: str
    name: str
    # bull / bear / both / neutral。neutral 只在正向命中，选股侧记为「出现」。
    bias: str
    builtin: bool = True

    @property
    def column(self) -> str:
        return f"cdl_{self.id}"

    @property
    def sides(self) -> tuple[str, ...]:
        if self.bias == "both":
            return ("bull", "bear")
        if self.bias == "bear":
            return ("bear",)
        return ("bull",)


# 与 InStock STOCK_KLINE_PATTERN_DATA 同一组 TA-Lib CDL*。
CATALOG: tuple[CandlePattern, ...] = (
    CandlePattern("doji", "CDLDOJI", "十字", "neutral"),
    CandlePattern("doji_star", "CDLDOJISTAR", "十字星", "both"),
    CandlePattern("dragonfly_doji", "CDLDRAGONFLYDOJI", "蜻蜓十字", "bull"),
    CandlePattern("gravestone_doji", "CDLGRAVESTONEDOJI", "墓碑十字", "bear"),
    CandlePattern("long_legged_doji", "CDLLONGLEGGEDDOJI", "长脚十字", "neutral"),
    CandlePattern("rickshaw_man", "CDLRICKSHAWMAN", "黄包车夫", "neutral"),
    CandlePattern("hammer", "CDLHAMMER", "锤头", "bull"),
    CandlePattern("hanging_man", "CDLHANGINGMAN", "上吊线", "bear"),
    CandlePattern("inverted_hammer", "CDLINVERTEDHAMMER", "倒锤头", "bull"),
    CandlePattern("shooting_star", "CDLSHOOTINGSTAR", "射击之星", "bear"),
    CandlePattern("takuri", "CDLTAKURI", "探水竿", "bull"),
    CandlePattern("engulfing", "CDLENGULFING", "吞没", "both"),
    CandlePattern("harami", "CDLHARAMI", "孕线", "both"),
    CandlePattern("harami_cross", "CDLHARAMICROSS", "十字孕线", "both"),
    CandlePattern("piercing", "CDLPIERCING", "刺透", "bull"),
    CandlePattern("dark_cloud_cover", "CDLDARKCLOUDCOVER", "乌云盖顶", "bear"),
    CandlePattern("morning_star", "CDLMORNINGSTAR", "晨星", "bull"),
    CandlePattern("evening_star", "CDLEVENINGSTAR", "暮星", "bear"),
    CandlePattern("morning_doji_star", "CDLMORNINGDOJISTAR", "十字晨星", "bull"),
    CandlePattern("evening_doji_star", "CDLEVENINGDOJISTAR", "十字暮星", "bear"),
    # 下面七个 Polars 规则和 TA-Lib 差得远，没有 TA-Lib 时不提供。
    CandlePattern("abandoned_baby", "CDLABANDONEDBABY", "弃婴", "both", builtin=False),
    CandlePattern("three_white_soldiers", "CDL3WHITESOLDIERS", "三个白兵", "bull", builtin=False),
    CandlePattern("three_black_crows", "CDL3BLACKCROWS", "三只乌鸦", "bear", builtin=False),
    CandlePattern("identical_three_crows", "CDLIDENTICAL3CROWS", "三胞胎乌鸦", "bear"),
    CandlePattern("three_inside", "CDL3INSIDE", "三内部", "both"),
    CandlePattern("three_outside", "CDL3OUTSIDE", "三外部", "both"),
    CandlePattern("marubozu", "CDLMARUBOZU", "光头光脚", "both"),
    CandlePattern("closing_marubozu", "CDLCLOSINGMARUBOZU", "收盘光头", "both"),
    CandlePattern("belt_hold", "CDLBELTHOLD", "捉腰带", "both"),
    CandlePattern("spinning_top", "CDLSPINNINGTOP", "纺锤", "both"),
    CandlePattern("high_wave", "CDLHIGHWAVE", "风高浪大", "both"),
    CandlePattern("long_line", "CDLLONGLINE", "长蜡烛", "both", builtin=False),
    CandlePattern("short_line", "CDLSHORTLINE", "短蜡烛", "both"),
    CandlePattern("homing_pigeon", "CDLHOMINGPIGEON", "家鸽", "bull"),
    CandlePattern("matching_low", "CDLMATCHINGLOW", "相同低价", "bull"),
    CandlePattern("counterattack", "CDLCOUNTERATTACK", "反击线", "both"),
    CandlePattern("separating_lines", "CDLSEPARATINGLINES", "分离线", "both", builtin=False),
    CandlePattern("in_neck", "CDLINNECK", "颈内线", "bear"),
    CandlePattern("on_neck", "CDLONNECK", "颈上线", "bear"),
    CandlePattern("thrusting", "CDLTHRUSTING", "插入", "bear"),
    CandlePattern("kicking", "CDLKICKING", "反冲", "both"),
    CandlePattern("tristar", "CDLTRISTAR", "三星", "both"),
    CandlePattern("stick_sandwich", "CDLSTICKSANDWICH", "条形三明治", "bull", builtin=False),
    CandlePattern("rising_falling_three", "CDLRISEFALL3METHODS", "上升下降三法", "both", builtin=False),
    # 下面这些规则长、歧义多，没有 TA-Lib 时不猜测。
    CandlePattern("two_crows", "CDL2CROWS", "两只乌鸦", "bear", builtin=False),
    CandlePattern("upside_gap_two_crows", "CDLUPSIDEGAP2CROWS", "向上跳空两只乌鸦", "bear", builtin=False),
    CandlePattern("three_line_strike", "CDL3LINESTRIKE", "三线打击", "both", builtin=False),
    CandlePattern("hikkake", "CDLHIKKAKE", "陷阱", "both", builtin=False),
    CandlePattern("modified_hikkake", "CDLHIKKAKEMOD", "修正陷阱", "both", builtin=False),
    CandlePattern("stalled", "CDLSTALLEDPATTERN", "停顿", "bear", builtin=False),
    CandlePattern("advance_block", "CDLADVANCEBLOCK", "大敌当前", "bear", builtin=False),
    CandlePattern("gap_side_side_white", "CDLGAPSIDESIDEWHITE", "并列阳线", "both", builtin=False),
    CandlePattern("three_stars_south", "CDL3STARSINSOUTH", "南方三星", "bull", builtin=False),
    CandlePattern("breakaway", "CDLBREAKAWAY", "脱离", "both", builtin=False),
    CandlePattern("concealing_baby", "CDLCONCEALBABYSWALL", "藏婴吞没", "bull", builtin=False),
    CandlePattern("kicking_by_length", "CDLKICKINGBYLENGTH", "长缺影反冲", "both", builtin=False),
    CandlePattern("ladder_bottom", "CDLLADDERBOTTOM", "梯底", "bull", builtin=False),
    CandlePattern("mat_hold", "CDLMATHOLD", "铺垫", "bull", builtin=False),
    CandlePattern("tasuki_gap", "CDLTASUKIGAP", "跳空并列阴阳", "both", builtin=False),
    CandlePattern("unique_3_river", "CDLUNIQUE3RIVER", "奇特三河床", "bull", builtin=False),
    CandlePattern("upside_downside_gap", "CDLXSIDEGAP3METHODS", "跳空三法", "both", builtin=False),
)

_BY_ID = {p.id: p for p in CATALOG}


def get_pattern(pattern_id: str) -> CandlePattern | None:
    return _BY_ID.get(pattern_id)


def pattern_columns() -> frozenset[str]:
    return frozenset(p.column for p in CATALOG)


def talib_module():
    """已安装的 TA-Lib。没装或被 TSP_CANDLE_ENGINE=builtin 关掉时返回 None。"""
    if os.environ.get("TSP_CANDLE_ENGINE", "").strip().lower() == "builtin":
        return None
    try:
        import talib
    except ImportError:
        return None
    return talib


def active_engine() -> str:
    return "talib" if talib_module() is not None else "builtin"


def available_patterns(engine: str | None = None) -> tuple[CandlePattern, ...]:
    eng = engine or active_engine()
    if eng == "talib":
        return CATALOG
    return tuple(p for p in CATALOG if p.builtin)


def pattern_available(pattern: CandlePattern, engine: str | None = None) -> bool:
    eng = engine or active_engine()
    return eng == "talib" or pattern.builtin


def catalog_payload(engine: str | None = None) -> dict:
    eng = engine or active_engine()
    items = []
    for pat in CATALOG:
        ok = pattern_available(pat, eng)
        items.append({
            "id": pat.id,
            "name": pat.name,
            "talib": pat.talib,
            "bias": pat.bias,
            "sides": list(pat.sides),
            "builtin": pat.builtin,
            "available": ok,
            "column": pat.column,
        })
    return {"engine": eng, "patterns": items}


def side_value(side: str) -> int:
    if side == "bear":
        return BEAR
    if side == "bull":
        return BULL
    raise ValueError(f"side 必须是 bull 或 bear: {side}")


def _sh(name: str, n: int = 1) -> pl.Expr:
    return pl.col(name).shift(n).over("symbol")


def _top(n: int = 0) -> pl.Expr:
    expr = pl.max_horizontal("open", "close")
    return expr.shift(n).over("symbol") if n else expr


def _bot(n: int = 0) -> pl.Expr:
    expr = pl.min_horizontal("open", "close")
    return expr.shift(n).over("symbol") if n else expr


def _mid(n: int = 0) -> pl.Expr:
    expr = (pl.col("open") + pl.col("close")) / 2
    return expr.shift(n).over("symbol") if n else expr


def _px(name: str, n: int = 0) -> pl.Expr:
    expr = pl.col(name)
    return expr.shift(n).over("symbol") if n else expr


def _flag(cond: pl.Expr) -> pl.Expr:
    return cond.fill_null(False)


def _prepare(df: pl.DataFrame) -> pl.DataFrame:
    """TA-Lib 默认蜡烛设置：实体/影线均值不含当根。"""
    body = (pl.col("close") - pl.col("open")).abs()
    upper = pl.col("high") - pl.max_horizontal("open", "close")
    lower = pl.min_horizontal("open", "close") - pl.col("low")
    hl = pl.col("high") - pl.col("low")
    # close == open 在 TA-Lib 里算阳线。
    white = pl.col("close") >= pl.col("open")
    body_avg = body.shift(1).rolling_mean(10, min_samples=10).over("symbol")
    hl_avg = hl.shift(1).rolling_mean(10, min_samples=10).over("symbol")
    hl_avg5 = hl.shift(1).rolling_mean(5, min_samples=5).over("symbol")
    near = 0.2 * hl_avg5
    equal = 0.05 * hl_avg5
    return df.with_columns(
        body.alias("_body"),
        upper.alias("_upper"),
        lower.alias("_lower"),
        hl.alias("_hl"),
        white.alias("_white"),
        body_avg.alias("_body_avg"),
        hl_avg.alias("_hl_avg"),
        near.alias("_near"),
        equal.alias("_equal"),
    ).with_columns(
        (pl.col("_body") < pl.col("_body_avg")).alias("_body_short"),
        (pl.col("_body") > pl.col("_body_avg")).alias("_body_long"),
        (
            (pl.col("_hl") > 0)
            & pl.col("_hl_avg").is_not_null()
            & (pl.col("_hl_avg") > 0)
            & (pl.col("_body") <= pl.col("_hl_avg") * 0.1)
        ).alias("_doji"),
        (pl.col("_upper") < pl.col("_hl_avg") * 0.1).alias("_us_vshort"),
        (pl.col("_lower") < pl.col("_hl_avg") * 0.1).alias("_ls_vshort"),
        (pl.col("_upper") > pl.col("_body")).alias("_us_long"),
        (pl.col("_lower") > pl.col("_body")).alias("_ls_long"),
        (pl.col("_lower") > pl.col("_body") * 2).alias("_ls_vlong"),
        (pl.col("_upper") > pl.col("_body") * 2).alias("_us_vlong"),
    )


def _builtin_flags() -> dict[str, pl.Expr]:
    """每个内置形态的布尔条件。双向形态用 ``_bull`` / ``_bear`` 后缀。"""
    white = pl.col("_white")
    black = ~white
    doji = pl.col("_doji")
    near_low = _bot(0) <= _px("low", 1) + pl.col("_near")
    near_high = _top(0) >= _px("high", 1) - pl.col("_near")
    o, c = pl.col("open"), pl.col("close")

    engulf_bull = _flag(
        _sh("_white").not_() & white
        & (pl.col("_body") > _sh("_body"))
        & (c >= _px("open", 1))
        & (o <= _px("close", 1))
    )
    engulf_bear = _flag(
        _sh("_white") & black
        & (pl.col("_body") > _sh("_body"))
        & (o >= _px("close", 1))
        & (c <= _px("open", 1))
    )
    inside = (_top(0) <= _top(1)) & (_bot(0) >= _bot(1)) & (pl.col("_body") < _sh("_body"))
    harami_bull = _flag(_sh("_white").not_() & white & inside & _sh("_body_long"))
    harami_bear = _flag(_sh("_white") & black & inside & _sh("_body_long"))

    maru_bull = _flag(white & pl.col("_body_long") & pl.col("_us_vshort") & pl.col("_ls_vshort"))
    maru_bear = _flag(black & pl.col("_body_long") & pl.col("_us_vshort") & pl.col("_ls_vshort"))

    def _star(bullish: bool, middle_doji: bool) -> pl.Expr:
        # TA-Lib 默认 optInPenetration=0.3：第三根收盘进入第一根实体的 30%，不是中点。
        if bullish:
            first = _sh("_white", 2).not_() & _sh("_body_long", 2)
            gap = _top(1) < _bot(2)
            third = white & doji.not_() & (c > _px("close", 2) + _sh("_body", 2) * 0.3)
        else:
            first = _sh("_white", 2) & _sh("_body_long", 2)
            gap = _bot(1) > _top(2)
            third = black & doji.not_() & (c < _px("close", 2) - _sh("_body", 2) * 0.3)
        middle = _sh("_doji", 1) if middle_doji else _sh("_body_short", 1)
        return _flag(first & middle & gap & third)

    flags = {
        "doji": _flag(doji),
        "long_legged_doji": _flag(doji & pl.col("_us_long") & pl.col("_ls_long")),
        "dragonfly_doji": _flag(doji & pl.col("_us_vshort") & pl.col("_ls_long") & (pl.col("_lower") > 0)),
        "gravestone_doji": _flag(doji & pl.col("_ls_vshort") & pl.col("_us_long") & (pl.col("_upper") > 0)),
        "rickshaw_man": _flag(
            doji & pl.col("_us_long") & pl.col("_ls_long")
            & ((pl.col("_upper") - pl.col("_lower")).abs() <= pl.col("_equal"))
        ),
        "doji_star_bull": _flag(
            _sh("_white").not_() & _sh("_body_long") & doji & (_top(0) < _bot(1))
        ),
        "doji_star_bear": _flag(
            _sh("_white") & _sh("_body_long") & doji & (_bot(0) > _top(1))
        ),
        "hammer": _flag(
            pl.col("_body_short") & pl.col("_ls_long") & pl.col("_us_vshort") & near_low
        ),
        "hanging_man": _flag(
            pl.col("_body_short") & pl.col("_ls_long") & pl.col("_us_vshort") & near_high
        ),
        "inverted_hammer": _flag(
            pl.col("_body_short") & pl.col("_us_long") & pl.col("_ls_vshort") & near_low
        ),
        "shooting_star": _flag(
            pl.col("_body_short") & pl.col("_us_long") & pl.col("_ls_vshort") & near_high
        ),
        "takuri": _flag(
            pl.col("_body_short") & pl.col("_ls_vlong") & pl.col("_us_vshort") & near_low
        ),
        "engulfing_bull": engulf_bull,
        "engulfing_bear": engulf_bear,
        "harami_bull": harami_bull,
        "harami_bear": harami_bear,
        # 十字孕线的当根是十字，不要求它再满足孕线的阴阳色（十字在 TA-Lib 里算阳线）。
        "harami_cross_bull": _flag(
            _sh("_white").not_() & _sh("_body_long") & doji & inside
        ),
        "harami_cross_bear": _flag(
            _sh("_white") & _sh("_body_long") & doji & inside
        ),
        "piercing": _flag(
            _sh("_white").not_() & _sh("_body_long") & white
            & (o < _px("low", 1))
            & (c > _mid(1))
            & (c < _px("open", 1))
        ),
        "dark_cloud_cover": _flag(
            _sh("_white") & _sh("_body_long") & black
            & (o > _px("high", 1))
            & (c < _mid(1))
            & (c > _px("open", 1))
        ),
        "morning_star": _star(True, False),
        "evening_star": _star(False, False),
        "morning_doji_star": _star(True, True),
        "evening_doji_star": _star(False, True),
        # 开盘贴近前收。三只乌鸦要求开在前一根实体内部，没有 TA-Lib 时不提供那条规则。
        "identical_three_crows": _flag(
            black & _sh("_white", 1).not_() & _sh("_white", 2).not_()
            & (c < _px("close", 1)) & (_px("close", 1) < _px("close", 2))
            & ((o - _px("close", 1)).abs() <= pl.col("_equal"))
            & ((_px("open", 1) - _px("close", 2)).abs() <= _sh("_equal", 1))
            & pl.col("_body_long") & _sh("_body_long", 1) & _sh("_body_long", 2)
        ),
        "marubozu_bull": maru_bull,
        "marubozu_bear": maru_bear,
        "closing_marubozu_bull": _flag(white & pl.col("_body_long") & pl.col("_us_vshort")),
        "closing_marubozu_bear": _flag(black & pl.col("_body_long") & pl.col("_ls_vshort")),
        "belt_hold_bull": _flag(white & pl.col("_body_long") & pl.col("_ls_vshort")),
        "belt_hold_bear": _flag(black & pl.col("_body_long") & pl.col("_us_vshort")),
        "spinning_top_bull": _flag(white & pl.col("_body_short") & pl.col("_us_long") & pl.col("_ls_long")),
        "spinning_top_bear": _flag(black & pl.col("_body_short") & pl.col("_us_long") & pl.col("_ls_long")),
        "high_wave_bull": _flag(white & pl.col("_body_short") & pl.col("_us_vlong") & pl.col("_ls_vlong")),
        "high_wave_bear": _flag(black & pl.col("_body_short") & pl.col("_us_vlong") & pl.col("_ls_vlong")),
        "short_line_bull": _flag(white & pl.col("_body_short") & pl.col("_us_vshort") & pl.col("_ls_vshort")),
        "short_line_bear": _flag(black & pl.col("_body_short") & pl.col("_us_vshort") & pl.col("_ls_vshort")),
        "homing_pigeon": _flag(
            black & _sh("_white").not_() & inside & _sh("_body_long")
        ),
        "matching_low": _flag(
            black & _sh("_white").not_()
            & ((c - _px("close", 1)).abs() <= pl.col("_equal"))
        ),
        "counterattack_bull": _flag(
            _sh("_white").not_() & white & _sh("_body_long") & pl.col("_body_long")
            & ((c - _px("close", 1)).abs() <= pl.col("_equal"))
        ),
        "counterattack_bear": _flag(
            _sh("_white") & black & _sh("_body_long") & pl.col("_body_long")
            & ((c - _px("close", 1)).abs() <= pl.col("_equal"))
        ),
        "in_neck": _flag(
            _sh("_white").not_() & _sh("_body_long") & white
            & (o < _px("low", 1))
            & ((c - _px("close", 1)).abs() <= pl.col("_equal"))
            & (c < _px("open", 1))
        ),
        "on_neck": _flag(
            _sh("_white").not_() & _sh("_body_long") & white
            & (o < _px("low", 1))
            & ((c - _px("low", 1)).abs() <= pl.col("_equal"))
        ),
        "thrusting": _flag(
            _sh("_white").not_() & _sh("_body_long") & white
            & (o < _px("low", 1))
            & (c > _px("close", 1))
            & (c < _mid(1))
        ),
        "tristar_bull": _flag(
            _sh("_doji", 2) & _sh("_doji", 1) & doji
            & (_top(1) < _bot(2))
            & (_bot(0) > _top(1))
        ),
        "tristar_bear": _flag(
            _sh("_doji", 2) & _sh("_doji", 1) & doji
            & (_bot(1) > _top(2))
            & (_top(0) < _bot(1))
        ),
    }
    return flags


_SINGLE = {
    "doji": ("doji", None),
    "long_legged_doji": ("long_legged_doji", None),
    "dragonfly_doji": ("dragonfly_doji", None),
    "gravestone_doji": (None, "gravestone_doji"),
    "rickshaw_man": ("rickshaw_man", None),
    "hammer": ("hammer", None),
    "hanging_man": (None, "hanging_man"),
    "inverted_hammer": ("inverted_hammer", None),
    "shooting_star": (None, "shooting_star"),
    "takuri": ("takuri", None),
    "piercing": ("piercing", None),
    "dark_cloud_cover": (None, "dark_cloud_cover"),
    "morning_star": ("morning_star", None),
    "evening_star": (None, "evening_star"),
    "morning_doji_star": ("morning_doji_star", None),
    "evening_doji_star": (None, "evening_doji_star"),
    "identical_three_crows": (None, "identical_three_crows"),
    "homing_pigeon": ("homing_pigeon", None),
    "matching_low": ("matching_low", None),
    "in_neck": (None, "in_neck"),
    "on_neck": (None, "on_neck"),
    "thrusting": (None, "thrusting"),
}
_BOTH = (
    "doji_star", "engulfing", "harami", "harami_cross",
    "three_inside", "three_outside", "marubozu", "closing_marubozu", "belt_hold",
    "spinning_top", "high_wave", "short_line", "counterattack",
    "kicking", "tristar",
)


def _dependent_flag_exprs() -> list[pl.Expr]:
    """三内部 / 三外部 / 反冲要看上一根是否已经是孕线、吞没或光头光脚。

    这些条件先落成列再 shift。对已经带 over 的表达式再 shift，窗口会对不齐。
    """
    white = pl.col("_white")
    black = ~white
    close = pl.col("close")
    return [
        _flag(
            white
            & pl.col("_f_harami_bull").shift(1).over("symbol")
            & (close > _px("close", 1))
        ).alias("_f_three_inside_bull"),
        _flag(
            black
            & pl.col("_f_harami_bear").shift(1).over("symbol")
            & (close < _px("close", 1))
        ).alias("_f_three_inside_bear"),
        _flag(
            white
            & pl.col("_f_engulfing_bull").shift(1).over("symbol")
            & (close > _px("close", 1))
        ).alias("_f_three_outside_bull"),
        _flag(
            black
            & pl.col("_f_engulfing_bear").shift(1).over("symbol")
            & (close < _px("close", 1))
        ).alias("_f_three_outside_bear"),
        _flag(
            pl.col("_f_marubozu_bear").shift(1).over("symbol")
            & pl.col("_f_marubozu_bull")
            & (pl.col("low") > _px("high", 1))
        ).alias("_f_kicking_bull"),
        _flag(
            pl.col("_f_marubozu_bull").shift(1).over("symbol")
            & pl.col("_f_marubozu_bear")
            & (pl.col("high") < _px("low", 1))
        ).alias("_f_kicking_bear"),
    ]


def _int_expr(bull: str | None, bear: str | None) -> pl.Expr:
    bull_e = pl.col(f"_f_{bull}") if bull else pl.lit(False)
    bear_e = pl.col(f"_f_{bear}") if bear else pl.lit(False)
    return (
        pl.when(bull_e).then(BULL)
        .when(bear_e).then(BEAR)
        .otherwise(0)
        .cast(pl.Int8)
    )


def _apply_builtin(df: pl.DataFrame, ids: set[str] | None) -> pl.DataFrame:
    flags = _builtin_flags()
    wanted = {p.id for p in CATALOG if p.builtin} if ids is None else set(ids)
    df = df.with_columns([
        flags[name].alias(f"_f_{name}") for name in flags
    ]).with_columns(_dependent_flag_exprs())
    out: list[pl.Expr] = []
    for pid, (bull, bear) in _SINGLE.items():
        if pid in wanted:
            out.append(_int_expr(bull, bear).alias(f"cdl_{pid}"))
    for pid in _BOTH:
        if pid in wanted:
            out.append(_int_expr(f"{pid}_bull", f"{pid}_bear").alias(f"cdl_{pid}"))
    missing = [
        p.id for p in CATALOG
        if p.builtin and p.id in wanted and f"cdl_{p.id}" not in {e.meta.output_name() for e in out}
    ]
    if missing:
        raise RuntimeError(f"内置形态缺少表达式: {missing}")
    if out:
        df = df.with_columns(out)
    drop = [c for c in df.columns if c.startswith("_")]
    return df.drop(drop)


def _normalize_sign(raw, bias: str):
    import numpy as np

    values = np.nan_to_num(np.asarray(raw, dtype=np.float64), nan=0.0)
    out = np.zeros(values.shape, dtype=np.int16)
    if bias == "both":
        out[values > 0] = BULL
        out[values < 0] = BEAR
        return out
    hit = values != 0
    out[hit] = BEAR if bias == "bear" else BULL
    return out


def _apply_talib(df: pl.DataFrame, patterns: tuple[CandlePattern, ...]) -> pl.DataFrame:
    import numpy as np

    talib = talib_module()
    if talib is None:
        raise RuntimeError("TA-Lib 不可用")
    n = df.height
    opens = df["open"].to_numpy().astype(np.float64, copy=False)
    highs = df["high"].to_numpy().astype(np.float64, copy=False)
    lows = df["low"].to_numpy().astype(np.float64, copy=False)
    closes = df["close"].to_numpy().astype(np.float64, copy=False)
    symbols = df["symbol"].to_numpy()
    if n == 0:
        return df
    cuts = np.flatnonzero(symbols[1:] != symbols[:-1]) + 1
    starts = np.concatenate(([0], cuts))
    ends = np.concatenate((cuts, [n]))
    series = []
    for pat in patterns:
        fn = getattr(talib, pat.talib)
        arr = np.zeros(n, dtype=np.int16)
        for start, end in zip(starts, ends, strict=True):
            if end - start < 2:
                continue
            arr[start:end] = _normalize_sign(
                fn(opens[start:end], highs[start:end], lows[start:end], closes[start:end]),
                pat.bias,
            )
        series.append(pl.Series(pat.column, arr).cast(pl.Int8))
    return df.with_columns(series)


def compute_candle_patterns(
    df: pl.DataFrame,
    *,
    ids: set[str] | None = None,
    engine: str | None = None,
) -> pl.DataFrame:
    """给 OHLC 帧加上 ``cdl_*`` 列。``ids`` 为空时算当前引擎能算的全部。"""
    if df.is_empty():
        return df
    required = {"symbol", "open", "high", "low", "close"}
    if not required <= set(df.columns):
        missing = sorted(required - set(df.columns))
        raise ValueError(f"K 线缺少列: {missing}")
    eng = engine or active_engine()
    selected = []
    for pat in CATALOG:
        if ids is not None and pat.id not in ids:
            continue
        if not pattern_available(pat, eng):
            continue
        selected.append(pat)
    if not selected:
        return df
    work = df.sort(["symbol", "date"])
    if eng == "talib":
        return _apply_talib(work, tuple(selected))
    work = _prepare(work)
    return _apply_builtin(work, {p.id for p in selected})


def markers_for_rows(rows: list[dict]) -> list[dict]:
    """单只股票的日 K 行 → 图表买卖标记。失败由调用方吞掉。"""
    records = []
    for row in rows:
        try:
            open_, high, low, close = (
                float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not all(map(_finite_positive, (open_, high, low, close))):
            continue
        records.append({
            "symbol": "_",
            "date": str(row.get("date", ""))[:10],
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
        })
    if len(records) < 2:
        return []
    frame = compute_candle_patterns(pl.DataFrame(records))
    grouped: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []
    for pat in available_patterns():
        if pat.column not in frame.columns:
            continue
        hits = frame.filter(pl.col(pat.column) != 0)
        if hits.is_empty():
            continue
        for item in hits.select("date", pat.column).iter_rows():
            day = str(item[0])[:10]
            side = "neutral" if pat.bias == "neutral" else ("bear" if item[1] < 0 else "bull")
            key = (day, side)
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            if pat.name not in grouped[key]:
                grouped[key].append(pat.name)
    markers = []
    for key in order:
        names = grouped[key]
        label = "·".join(names[:3])
        if len(names) > 3:
            label = f"{label}+{len(names) - 3}"
        markers.append({
            "date": key[0],
            "side": key[1],
            "name": label,
            "patterns": names,
        })
    return markers


def _finite_positive(value: float) -> bool:
    return value == value and value > 0 and value != float("inf")
