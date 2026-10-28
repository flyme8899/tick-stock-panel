"""日频真实定义工厂: `bss.daily_quality_value`(批次 20261028gp1 · P-bss-p1)。

## 这个模块造的是什么

一条**非夹具**的日频信号定义记录 —— `family="daily_fundamental"`、
`event_class="daily_quality_value"`、`signal_id="bss.daily_quality_value"`,
初态 **`candidate`**。

它**不是**评估器, **不是**影子写入, **不是**触发链路, 更**不是**放量开关。
仓里造出这条记录, 能说明的只有一件事: 「真实日频定义的键存在, 且与夹具键可区分」。
它**不能**说明 calib 已过, 不能说明信号可做, 不能说明 TRIGGERING 该开。

## 为什么需要它

批到 #240 为止, 仓里**只有夹具**能造出日频定义:
`hits_fixtures.daily_fundamental_definition()` 默认 `fx.pub.daily_quality_value`,
而且是 **`publishable`** —— 它是给测例用的(夹具必须是 publishable, 否则测不了
「publishable 才可见」), 所以它**不可能**同时充当生产定义: 一条 publishable 的夹具
当成生产真值, 等于凭空跳过人工发布闸。

真实定义必须另起一个**非 `fx.` 前缀**的键, 并且初态是 `candidate` —— 状态机里
`publishable` 的入边本来就是空的, 唯一入口是 `manual_publish()`。本模块**不调**它。

## PIT 与 ≥30 的口径为什么写在 metadata 里

这两条是**声明**, 不是**实现**:

- PIT: 评估时截断用 `announce_date <= asof`(锚在 `quality_proxy`), 禁止拿
  `period_end` 当前视。本模块只把这条口径钉进记录, 不改 `quality_proxy` 的行为。
- ≥30: 计数域是**全市场影子**(`source.mode=shadow`), 不是 `/hits` 按自选/持仓
  scope 过滤后的可见条数 —— 后者随用户配置变, 拿它凑 30 等于让不同用户看到不同的
  「已达标」。

把口径**先写下来**, 是为了在 P2/P3b 真开跑之前就有一个可测的约定: 将来谁想在
scope 之后计数, 得先改这里的常量并让测例红, 而不是悄悄换口径。

## 本模块刻意不做

- 不调 `manual_publish`, 不产出 `publishable`
- 不写 `HitStore`, 不调 `calib.upsert`, 不接评估器
- 不在 `main` 启动时自动注册(推荐做法; 注册由测例或显式调用触发)

批次 20261028gp1 · P-bss-p1。
仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""
from __future__ import annotations

from typing import Any, Final

from .constants import (
    CALIB_SAMPLE_N_MIN,
    DISCLAIMER_KEY,
    FREQ_LOCK_VERSION,
)
from .freq_lock import FreqLockStamp
from .models import SignalRecord
from .quality_proxy import QUALITY_METRIC_COLUMNS
from .store import SignalRegistryStore

# ---------------------------------------------------------------- 身份

REAL_DAILY_SIGNAL_ID: Final = "bss.daily_quality_value"
"""真实日频定义键。**不是** `fx.pub.daily_quality_value`(那是夹具)。

`bss.` 前缀是本模块自定的命名空间: 仓里既有键用 `fx.`(夹具) 和 `volume_breakout` /
`auction_abnormal`(桩), 真实定义需要一个一眼能看出「这不是夹具」的前缀。
"""

REAL_DAILY_DEFINITION_VERSION: Final = "v1"
"""定义版本。落进 `SignalRecord.version`。

**不**用夹具的 `fixture_v1`, 也**不**留 `SignalRecord.version` 的默认值
(`design_version()` 拼出来的 `design_pin_v1+independent_family_v1:m6` —— 那是
Bonferroni 口径标签, 不是定义版本, 混用会让三键对齐永远对不上)。
"""

REAL_DAILY_FAMILY: Final = "daily_fundamental"
"""日频家族。∈ `SIGNAL_FAMILIES`。"""

REAL_DAILY_EVENT_CLASS: Final = "daily_quality_value"
"""日线质量与估值。∈ `EVENT_CLASSES`。"""

REAL_DAILY_DISPLAY_NAME: Final = "日频质量价值（真实定义 v1）"
"""中文可读名。

刻意**不含**「可放量」「已触发」「已验证」这类词 —— 一条 `candidate` 记录的名字里
出现这些词, 读的人会把它当成状态说明, 而它只是名字。
"""

REAL_DAILY_SPEC_REF: Final = "P-bss-p1"
"""规格引用。指向本批次与 docs 里的小节, 不指向任何「已放量」文档。"""

# ---------------------------------------------------------------- PIT 口径

PIT_CUTOFF_KEY: Final = "pit_cutoff"
PIT_CUTOFF_VALUE: Final = "announce_date"
"""可知性截断键: **公告日**。

用 `period_end`(报告期末)做截断是前视: 三季报 9 月 30 日截止、10 月底才公告,
按 `period_end <= asof` 判的话, 10 月 1 日就能"知道"10 月 28 日才公布的数字。
方向永远对策略有利, 所以它是唯一一处「错得很隐蔽且永远帮多头」的错。
"""

PIT_LOOKAHEAD_FORBIDDEN_KEY: Final = "pit_lookahead_forbidden"
PIT_RULE_TEXT: Final = "announce_date <= asof"
"""判据原文, 与 `quality_proxy` 的 PIT 锚一致。"""

# ---------------------------------------------------------------- ≥30 口径

SAMPLE_N_MIN_KEY: Final = "sample_n_min"
"""样本下限。值**引用** `CALIB_SAMPLE_N_MIN`(现 30), 不另写字面量 —— 两处各写一个
30, 将来改一处就会悄悄漂成两个门槛。"""

SAMPLE_COUNT_DOMAIN_KEY: Final = "sample_count_domain"
SAMPLE_COUNT_DOMAIN_VALUE: Final = "universe_shadow"
"""计数域: **全市场影子**。

不是 `/hits` 的可见条数 —— 那个已经过 scope(自选 ∪ 持仓)过滤。拿 scope 后的条数
说「样本够 30」, 等于把「这只票在你的自选里」当成统计前提。
"""

SAMPLE_SCOPE_FILTERED_FORBIDDEN_KEY: Final = "sample_count_scope_filtered_forbidden"

BATCH_TAG: Final = "20261028gp1"

_NOT_A_QUANTITY_NOTE: Final = "注册完成≠calib passed≠可放量；P3b/P2另口令"


# ---------------------------------------------------------------- 口径戳


def build_daily_quality_value_freq_lock(
    *,
    asof: str = "1970-01-01",
) -> FreqLockStamp:
    """口径戳: `cadence=daily_fundamental`, 钉 PIT 与 ≥30 的元数据。

    `asof` 默认 `"1970-01-01"` 是**占位**: 本拍不跑评估, 这套口径还没有针对任何真实
    评估日锁定过。给一个明显的占位值, 比给「今天」更安全 —— 后者看起来像"已经对某天
    锁过了", 而实际没有。真正评估时由调用方(将来的 P2)传真实时点。

    `params` 刻意**不含**任何 `volume_*` 键: 日频质量口径没有量相关阈值, 凭空加一个
    既没有意义, 又会撞上 K4 的 `volume_unit` 强制声明(见 `freq_lock.require_volume_unit`)。
    """
    return FreqLockStamp(
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        cadence=REAL_DAILY_FAMILY,
        asof=asof,
        params={
            PIT_CUTOFF_KEY: PIT_CUTOFF_VALUE,
            PIT_LOOKAHEAD_FORBIDDEN_KEY: True,
            SAMPLE_N_MIN_KEY: CALIB_SAMPLE_N_MIN,
            SAMPLE_COUNT_DOMAIN_KEY: SAMPLE_COUNT_DOMAIN_VALUE,
            SAMPLE_SCOPE_FILTERED_FORBIDDEN_KEY: True,
            # ---- N1 字段细化旁注: 七列引用, 不挡最小注册
            "quality_metric_columns": list(QUALITY_METRIC_COLUMNS),
            "weighting_status": "design_pending",
        },
        freq_lock_version=FREQ_LOCK_VERSION,
    )


# ---------------------------------------------------------------- 记录工厂


def build_daily_quality_value_record() -> SignalRecord:
    """造真实日频定义记录: 非夹具 · 初态 `candidate` · 非 `publishable`。

    ## 为什么 `version` 必须显式传

    `SignalRecord.version` 的默认值是 `design_version()` —— `design_pin_v1+
    independent_family_v1:m6`, 那是 Bonferroni 多重检验口径的标签。定义版本与它无关,
    顺手用默认值会让「v1 定义」和「m=6 口径」在落盘后看起来是同一种东西。所以这里
    显式给 `v1`, 且与 `FreqLockStamp.definition_version` 保持一致 —— 三键对齐
    (`signal_id` / `definition_version` / `freq_lock_version`)靠的就是这两处相等。

    ## 为什么 `status` 不传

    `SignalRecord.status` 默认就是 `"candidate"`。**不传**比传更安全: 传了就成了
    「由本工厂决定初态」, 将来谁改成 `publishable` 只要改一行字面量; 不传则初态由
    模型层的默认值兜着, 而模型层还有第二道校验(publishable 必须有人签过字)。

    ## metadata 里为什么既有 `pit_*` 又有一份 `freq_lock_*`

    两处都写是**冗余但不是重复**: `FreqLockStamp` 是评估侧用的容器, 序列化后可能
    单独流转; `metadata` 跟着 `SignalRecord` 走。如果只写在 stamp 里, 一条从注册表
    读出来的记录就看不出自己声明的 PIT 口径 —— 而注册表才是最常被读的那个入口。
    """
    stamp = build_daily_quality_value_freq_lock()
    metadata: dict[str, Any] = {
        # ---- 身份: 一眼看出这是真实定义而不是夹具
        "real_daily_definition": True,
        "fixture": False,
        # ---- PIT 口径
        PIT_CUTOFF_KEY: PIT_CUTOFF_VALUE,
        PIT_LOOKAHEAD_FORBIDDEN_KEY: True,
        "pit_rule": PIT_RULE_TEXT,
        # ---- ≥30 口径
        SAMPLE_N_MIN_KEY: CALIB_SAMPLE_N_MIN,
        SAMPLE_COUNT_DOMAIN_KEY: SAMPLE_COUNT_DOMAIN_VALUE,
        SAMPLE_SCOPE_FILTERED_FORBIDDEN_KEY: True,
        # ---- 口径戳的三键与节拍(从 stamp 抄一份)
        "freq_lock_align_key": list(stamp.align_key),
        "freq_lock_cadence": stamp.cadence,
        "freq_lock_version": stamp.freq_lock_version,
        # ---- N1 字段细化旁注
        "quality_metric_columns": list(QUALITY_METRIC_COLUMNS),
        "weighting_status": "design_pending",
        # ---- 批次与边界
        "batch": BATCH_TAG,
        "note": _NOT_A_QUANTITY_NOTE,
    }
    return SignalRecord(
        signal_id=REAL_DAILY_SIGNAL_ID,
        display_name=REAL_DAILY_DISPLAY_NAME,
        family=REAL_DAILY_FAMILY,
        event_class=REAL_DAILY_EVENT_CLASS,
        spec_ref=REAL_DAILY_SPEC_REF,
        version=REAL_DAILY_DEFINITION_VERSION,
        # status 不传: 用模型默认的 "candidate"
        # auto_promote_forbidden / manual_publish_required 默认即 True / True
        weighting_status="design_pending",
        disclaimer_key=DISCLAIMER_KEY,
        metadata=metadata,
    )


def register_daily_fundamental_definition(
    store: SignalRegistryStore,
) -> SignalRecord:
    """把真实日频定义 upsert 进注册表, 返回落库的那条记录。

    **本拍的用法: 测例 / 显式调用。** 生产 `main` 的日管道本拍**不自动挂** —— 自动挂
    等于让每次启动都往注册表里塞一条记录, 而这条记录现在只是口径声明, 没有评估器接它。
    真要挂, 须显式且默认不 publishable(见 PR 描述里的边界说明)。

    ## 为什么要在函数里再断言一次

    工厂已经保证初态是 `candidate`, 这里断言看起来多余。但 `register_*` 是**写库**的
    那一步, 也是将来最可能被人改坏的那一步(比如有人为了测某个下游, 在这里 `dataclasses
    .replace` 成 publishable 再存)。把断言放在写库前, 让"往注册表写 publishable"这条
    路在函数入口就断掉, 而不是靠读代码的人自己注意到。

    刻意**不**在这里调 `manual_publish` / 不写 HitStore / 不碰 calib。
    """
    record = build_daily_quality_value_record()
    if record.status == "publishable":
        raise AssertionError(
            "真实日频定义的初态不得是 publishable; 晋升必须走 manual_publish()"
        )
    return store.upsert(record)


__all__ = [
    "BATCH_TAG",
    "PIT_CUTOFF_KEY",
    "PIT_CUTOFF_VALUE",
    "PIT_LOOKAHEAD_FORBIDDEN_KEY",
    "PIT_RULE_TEXT",
    "REAL_DAILY_DEFINITION_VERSION",
    "REAL_DAILY_DISPLAY_NAME",
    "REAL_DAILY_EVENT_CLASS",
    "REAL_DAILY_FAMILY",
    "REAL_DAILY_SIGNAL_ID",
    "REAL_DAILY_SPEC_REF",
    "SAMPLE_COUNT_DOMAIN_KEY",
    "SAMPLE_COUNT_DOMAIN_VALUE",
    "SAMPLE_N_MIN_KEY",
    "SAMPLE_SCOPE_FILTERED_FORBIDDEN_KEY",
    "build_daily_quality_value_freq_lock",
    "build_daily_quality_value_record",
    "register_daily_fundamental_definition",
]
