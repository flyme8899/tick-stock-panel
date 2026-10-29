"""A2 · 轨①首见审计契约 + minute_event 影子硬闸(批次 20261030ga2)。

## 这组测例守什么

三件事, 分开测, 因为它们坏起来的样子不一样:

1. **闸在**(G-T5)。`minute_event` 写影子必须被拒, 且错误里能认出是 A2 闸挡的
   —— 如果只断言"抛异常了", 那么哪天有人把 `_require_shadow_hit` 改坏成"什么都拒",
   测例照样绿, 而日频样本全写不进去了。
2. **语义钉住**(G-T2)。首见＝我方首次可得, 排除 published / HotEvents 展示首见 /
   asof 三者。这几条用常量断言, 因为它们的价值就在于"字面写死"。
3. **不误伤**(G-T6)。`daily_fundamental` 与 `composite` 走同一条写口, 必须仍能写。

另有 G-T3(研究归档不进 hits/shadow)、G-T4(时序)、G-T7(COMPLETE=False)、
G-T8(零碰 Bonferroni/TRIGGERING) 若干断言。

## 与既有 `test_buy_sell_signal_hits_shadow_store.py` 的分工

那个文件是 #246(gp3b)的三键分桶原表, 夹具默认 `family="daily_fundamental"`。本文件
独立, 不搅乱它 —— 只在本文件末尾用一条 import 级断言确认它的夹具默认值没被本拍改动。

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

from __future__ import annotations

import dataclasses
import io
import tokenize
from datetime import datetime
from pathlib import Path

import pytest

from app.market_time import CN_TZ
from app.buy_sell_signals import constants as C
from app.buy_sell_signals import first_seen_audit as F
from app.buy_sell_signals import hits_shadow_store as S
from app.buy_sell_signals.hits_models import HitSource, SignalHit

_APP_DIR = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
_SHADOW_STORE_PATH = _APP_DIR / "hits_shadow_store.py"
_FIRST_SEEN_PATH = _APP_DIR / "first_seen_audit.py"

#: 一个"看起来完全合法"的事件轨三键 —— 拒写必须来自 A2 闸, 不能来自别的校验。
#: 所以这里的每个字段都要过得了 `_require_shadow_hit` 的前三条与三键检查。
_EVENT_SIGNAL_ID = "bss.research_event_fixture"
_EVENT_DEFINITION_VERSION = "research_v0"


def _code_only(path: Path) -> str:
    """剥掉字符串与注释后的源码。

    docstring 里写满了"不许"、注释里写着 `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE`,
    直接 `in src` 搜会恒命中。这个模块的源码断言必须走这里。
    """
    src = path.read_text(encoding="utf-8")
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (
                tokenize.STRING,
                tokenize.COMMENT,
                getattr(tokenize, "FSTRING_START", -1),
                getattr(tokenize, "FSTRING_MIDDLE", -1),
                getattr(tokenize, "FSTRING_END", -1),
            ):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return src
    return " ".join(out)


def _event_hit(
    i: int = 0,
    *,
    family: str = "minute_event",
    mode: str = "shadow",
    asof: str = "2026-10-30T10:00:00+08:00",
) -> SignalHit:
    """研究夹具: 一条合法的事件轨影子命中。**手编字段, 非产线样本。**

    刻意让它在除 family 之外**处处合法**: 三键齐全、mode=shadow、namespace 默认对。
    这样"被拒"就只能是被 A2 闸拒的。
    """
    return SignalHit(
        hit_id=f"evt-shadow-{i:04d}",
        signal_id=_EVENT_SIGNAL_ID,
        definition_version=_EVENT_DEFINITION_VERSION,
        symbol=f"{600000 + i}.SH",
        asof=asof,
        source=HitSource(evaluator="a2-research-fixture", mode=mode),
        family=family,
        # `EVENT_CLASSES` 是闭集, 这里取真实取值「盘中放量」——事件轨的原型场景。
        # 不用自造串(如 research_burst): 那会被 hits_models 的字段校验先拒掉, 于是
        # "被拒" 就证明不了是 A2 闸拒的。
        event_class="intraday_volume",
        direction_label="research_buy",
        freq_lock_version=C.FREQ_LOCK_VERSION,
        recorded_at=datetime(2026, 10, 30, 10, 5, 0, tzinfo=CN_TZ),
    )


def _daily_hit(i: int = 0) -> SignalHit:
    """研究夹具: 一条日频影子命中(#246 主路径的家族)。"""
    return SignalHit(
        hit_id=f"daily-shadow-{i:04d}",
        signal_id="bss.daily_quality_value",
        definition_version="v1",
        symbol=f"{600000 + i}.SH",
        asof="2026-10-20",
        source=HitSource(evaluator="a2-research-fixture", mode="shadow"),
        family="daily_fundamental",
        event_class="daily_quality_value",
        direction_label="research_buy",
        freq_lock_version=C.FREQ_LOCK_VERSION,
        recorded_at=datetime(2026, 10, 20, 16, 0, 0, tzinfo=CN_TZ),
    )


# ================================================================ G-T5 闸: 事件轨拒写


def test_g_t5_minute_event_shadow_write_is_blocked_until_a2(tmp_path):
    """G-T5: `minute_event` 写影子分桶 → 硬拒, 且错误可指认是 A2 闸。"""
    assert F.EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False, "本拍不得翻 True"

    hit = _event_hit()
    with pytest.raises(S.ShadowWriteError) as exc:
        S.upsert_shadow_hit(hit, data_dir=tmp_path)

    message = str(exc.value)
    assert F.EVENT_TRACK_SHADOW_BLOCK_TOKEN in message
    assert "event_track_shadow_blocked_until_a2" in message  # 字面量, 防 token 被改名
    assert "minute_event" in message

    # 拒写必须是真的没写: 桶目录都不该被建出来。
    bucket = S.shadow_bucket_path(
        tmp_path,
        signal_id=_EVENT_SIGNAL_ID,
        definition_version=_EVENT_DEFINITION_VERSION,
        freq_lock_version=C.FREQ_LOCK_VERSION,
    )
    assert not bucket.exists(), "被拒的写入不许留下任何文件"


def test_g_t5b_batch_write_is_blocked_too(tmp_path):
    """G-T5: `write_shadow_hits` 与 `upsert_shadow_hit` 共用同一道闸, 同样拒。"""
    with pytest.raises(S.ShadowWriteError) as exc:
        S.write_shadow_hits([_event_hit(0)], data_dir=tmp_path)
    assert F.EVENT_TRACK_SHADOW_BLOCK_TOKEN in str(exc.value)


def test_g_t5c_mixed_batch_writes_nothing(tmp_path):
    """G-T5: 一条日频 + 一条事件轨 → 整批拒, 日频那条也不许落盘。

    这是 `write_shadow_hits` 既有的"先全部校验再落盘"语义。若闸只加在 upsert 上,
    这条会红 —— 而它红的方式恰好说明"批量入口漏了闸"。
    """
    batch = [_daily_hit(0), _event_hit(1)]
    with pytest.raises(S.ShadowWriteError) as exc:
        S.write_shadow_hits(batch, data_dir=tmp_path)
    assert F.EVENT_TRACK_SHADOW_BLOCK_TOKEN in str(exc.value)

    daily_bucket = S.shadow_bucket_path(
        tmp_path,
        signal_id="bss.daily_quality_value",
        definition_version="v1",
        freq_lock_version=C.FREQ_LOCK_VERSION,
    )
    assert not daily_bucket.exists(), "整批被拒时, 日频那条也不许留下半个文件"


def test_g_t5d_block_is_by_family_not_by_caller(tmp_path):
    """G-T5: 判别只看 family, 不看 evaluator / signal_id / asof 长什么样。"""
    # 换掉所有"看起来可怀疑"的字段, 只保留 family=minute_event —— 仍须拒。
    hit = dataclasses.replace(
        _event_hit(2),
        source=HitSource(evaluator="some-other-evaluator", mode="shadow"),
        asof="2026-10-30",
    )
    with pytest.raises(S.ShadowWriteError) as exc:
        S.upsert_shadow_hit(hit, data_dir=tmp_path)
    assert F.EVENT_TRACK_SHADOW_BLOCK_TOKEN in str(exc.value)


# ================================================================ G-T2 语义钉


def test_g_t2_first_seen_means_my_first_available():
    """G-T2: 首见＝我方首次可得, 且三个冒充者被逐一点名排除。"""
    assert F.FIRST_SEEN_MEANING == "my_first_available"
    assert F.FIRST_SEEN_NOT == frozenset(
        {"published_at", "hot_events_first_seen", "signal_hit_asof"}
    )
    assert F.PREFERRED_SOURCE == "news_items.ingested_at"


def test_g_t2b_docstring_states_the_three_impostors():
    """G-T2: docstring 里必须写着三个冒充者 —— 这是给人看的那一份, 常量是给机器看的。"""
    doc = F.__doc__ or ""
    assert "我方" in doc and "首次" in doc
    for impostor in ("published_at", "hot_events.py", "SignalHit.asof"):
        assert impostor in doc, f"docstring 未点名冒充者: {impostor}"
    # 明确"不得顶上" —— 缺首见时的 fail-closed 口径要写在人读得到的地方。
    assert "不得" in doc


def test_g_t2c_hot_events_first_seen_is_published_based():
    """G-T2: 反向钉住 —— 上游 `hot_events.py` 确实按 published 造展示首见。

    这条断言的是**别人**的代码。写它的理由: 我们的排除项引用了一个外部事实, 若哪天
    hot_events 改了口径, 这里的注释与常量就该重新审一遍, 而测例会先红。
    """
    hot_events = (_APP_DIR.parent / "news" / "hot_events.py").read_text(encoding="utf-8")
    assert 'first = members[0]["published"]' in hot_events
    assert '"first_seen": first_seen' in hot_events


# ================================================================ G-T3 研究归档


def test_g_t3_research_subdir_is_pinned_and_outside_shadow():
    """G-T3: 研究归档路径写死, 且不在 shadow 分桶形态里。"""
    assert F.RESEARCH_FIRST_SEEN_SUBDIR == ("buy_sell_signals", "research", "first_seen")
    joined = "/".join(F.RESEARCH_FIRST_SEEN_SUBDIR)
    assert C.SHADOW_SUBDIR not in joined, "研究归档不许落在 shadow 子树里"
    assert "hits.json" not in joined


def test_g_t3b_first_seen_module_writes_nothing(tmp_path):
    """G-T3: 契约模块不落盘 —— 它连影子写口和 live 写口都不许调用。

    注意这里走 `_code_only`: docstring 里写着 "upsert_shadow_hit / DiskHitStore" 这些
    词(为了说明"不做什么"), 直接搜原文会恒绿 —— 那条"绿"证明不了任何事。
    """
    code = _code_only(_FIRST_SEEN_PATH)
    for forbidden in (
        "upsert_shadow_hit",
        "write_shadow_hits",
        "DiskHitStore",
        "shadow_bucket_path",
        "hits_shadow_store",
        "hits_store",
    ):
        assert forbidden not in code, f"first_seen_audit 不许调用 {forbidden}"

    # 也不许真的写文件: 没有 write_text / mkdir / open。
    assert "write_text" not in code
    assert "mkdir" not in code
    assert "open" not in code

    # 目录辅助只拼字符串, 不建目录。
    got = F.research_first_seen_dir(str(tmp_path))
    assert got.endswith("buy_sell_signals/research/first_seen") or got.endswith(
        "buy_sell_signals\\research\\first_seen"
    )
    assert not Path(got).exists(), "拼路径不该顺手建目录"


# ================================================================ G-T4 时序


@pytest.mark.parametrize(
    ("asof", "first_seen_at", "expected"),
    [
        # 晚于首见 → 通过
        ("2026-10-30T10:00:00+08:00", "2026-10-30T09:25:00+08:00", None),
        # 等于首见 → 通过(边界是包含, 不是严格大于)
        ("2026-10-30T09:25:00+08:00", "2026-10-30T09:25:00+08:00", None),
        # 早于首见 → 前视
        ("2026-10-30T09:20:00+08:00", "2026-10-30T09:25:00+08:00", "asof_before_first_seen"),
        # 缺首见 → fail-closed
        ("2026-10-30T10:00:00+08:00", None, "first_seen_missing"),
        ("2026-10-30T10:00:00+08:00", "   ", "first_seen_missing"),
        ("2026-10-30T10:00:00+08:00", "", "first_seen_missing"),
        # 解不开 → fail-closed, 不许当"大概没问题"放过去
        ("不是时间", "2026-10-30T09:25:00+08:00", "timestamp_unparsable"),
        ("2026-10-30T10:00:00+08:00", "2026-13-45", "timestamp_unparsable"),
    ],
)
def test_g_t4_asof_must_not_precede_first_seen(asof, first_seen_at, expected):
    """G-T4: `asof >= first_seen_at`; 缺首见 / 解不开一律 fail-closed。"""
    assert F.assert_asof_not_before_first_seen(asof, first_seen_at) == expected


def test_g_t4b_cross_timezone_is_compared_as_instant_not_as_string():
    """G-T4: 比较的是时刻, 不是字符串。

    `2026-10-30T02:00:00Z` 与 `2026-10-30T09:00:00+08:00` 是同一瞬间。按字符串比
    `'0' < '9'`, 会判"早于" —— 那是错的。这条测例就是为了挡住那种写法。
    """
    assert F.assert_asof_not_before_first_seen(
        "2026-10-30T02:00:00Z", "2026-10-30T09:00:00+08:00"
    ) is None
    # 反过来: 字符串更大, 但时刻更早。
    assert (
        F.assert_asof_not_before_first_seen(
            "2026-10-30T08:00:00+08:00", "2026-10-30T01:00:00Z"
        )
        == "asof_before_first_seen"
    )


def test_g_t4c_naive_timestamp_is_read_as_asia_shanghai():
    """G-T4: 无时区串按 Asia/Shanghai 解读。

    若误按 UTC 解读, 会把首见往早推 8 小时 —— 那正好是前视的方向。这里用"同一串两边
    相减为 0"来钉住"同一个时区假设", 再用跨时区那条钉住"换算正确"。
    """
    assert F.assert_asof_not_before_first_seen(
        "2026-10-30 10:00:00", "2026-10-30T02:00:00+00:00"
    ) is None


def test_g_t4d_daily_asof_is_conservative():
    """G-T4: 日级 asof 按当日 00:00 解读, 对同日更晚的首见判"早于"。

    这是**故意偏保守**: 日级精度本就证明不了"不早于"。宁可多拒一条, 不放过一条前视。
    """
    assert (
        F.assert_asof_not_before_first_seen("2026-10-30", "2026-10-30T09:25:00+08:00")
        == "asof_before_first_seen"
    )
    # 次日的日级 asof 则明确晚于首见。
    assert F.assert_asof_not_before_first_seen("2026-10-31", "2026-10-30T09:25:00+08:00") is None


# ================================================================ G-T6 日频不误伤


def test_g_t6_daily_fundamental_shadow_write_still_works(tmp_path):
    """G-T6: 日频影子写入不受本闸影响, 且读回仍是 shadow。"""
    path = S.upsert_shadow_hit(_daily_hit(0), data_dir=tmp_path)
    assert path.exists()

    back = S.list_shadow_hits(
        data_dir=tmp_path,
        signal_id="bss.daily_quality_value",
        definition_version="v1",
        freq_lock_version=C.FREQ_LOCK_VERSION,
    )
    assert [h.hit_id for h in back] == ["daily-shadow-0000"]
    assert back[0].source.mode == "shadow"


def test_g_t6b_batch_of_daily_hits_still_works(tmp_path):
    """G-T6: 批量入口对日频照旧可用。"""
    result = S.write_shadow_hits([_daily_hit(0), _daily_hit(1)], data_dir=tmp_path)
    assert sum(result.values()) == 2


def test_g_t6c_composite_family_is_not_blocked(tmp_path):
    """G-T6: 闸只认 `minute_event`。`composite` 不在闸内, 照旧可写。

    这条是"闸没写宽"的证据: 若哪天有人把条件改成 `!= "daily_fundamental"`, 它会红。
    """
    hit = dataclasses.replace(_event_hit(3), family="composite")
    path = S.upsert_shadow_hit(hit, data_dir=tmp_path)
    assert path.exists()


# ================================================================ G-T7 COMPLETE 默认 False


def test_g_t7_complete_defaults_to_false_and_is_final():
    """G-T7: COMPLETE 默认 False, 且是单一来源的 Final。"""
    assert F.EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False

    src = _FIRST_SEEN_PATH.read_text(encoding="utf-8")
    assert "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE: Final = False" in src


def test_g_t7b_shadow_store_imports_the_gate_from_first_seen_audit():
    """G-T7: 闸门是单一来源 —— `hits_shadow_store` 从契约模块导入, 不另立一份。"""
    code = _code_only(_SHADOW_STORE_PATH)
    assert "from . first_seen_audit import" in code
    # 不许在影子写口里重新定义这个常量(定义需要赋值, 导入不带 `=` 与 `False`)。
    assert "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE =" not in code


# ================================================================ G-T8 零碰


def test_g_t8_bonferroni_and_triggering_are_untouched():
    """G-T8: Bonferroni 数值与两处 TRIGGERING 仍为原值。"""
    assert C.BONFERRONI_M == 6
    assert C.BONFERRONI_SCHEME_ID == "independent_family_v1"

    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_TRIGGERING_ENABLED,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_TRIGGERING_ENABLED,
    )

    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_g_t8b_signal_families_set_is_unchanged():
    """G-T8: 不新增 family —— 闸按既有取值, 不去扩集合。"""
    assert C.SIGNAL_FAMILIES == frozenset(
        {"minute_event", "daily_fundamental", "composite"}
    )


# ================================================================ 兼容性


def test_existing_gp3b_fixture_default_family_is_still_daily():
    """不破 #246 原表: 它的夹具默认仍是 daily_fundamental。

    本拍没有改那个文件。若哪天有人为了"让闸过得去"把夹具改成 minute_event 之外的
    什么, 或反过来把默认改成 minute_event, 这条会红。
    """
    text = (_APP_DIR.parents[1] / "tests" / "test_buy_sell_signal_hits_shadow_store.py").read_text(
        encoding="utf-8"
    )
    assert 'family="daily_fundamental"' in text
    assert 'family="minute_event"' not in text
