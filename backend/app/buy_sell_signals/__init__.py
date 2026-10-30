"""买卖信号注册表与状态机骨架 (批次 g / g2 / gp / gpush)。

模块边界:

- `constants`  元数据 / Bonferroni 口径 / 与提醒层分家的冷却字段名
- `models`     `SignalRecord` 及其校验
- `state_machine` 转移表 + 人工发布闸 + 硬禁自动晋升
- `store`      `SignalRegistryStore` 协议 + 内存实现 + 落盘实现(env 门控) + 工厂
- `push`       推送出口: env 门控的 `try_push`, 通道 = SSE + 站内告警(gpush) + 钉钉(gding)
- `sw_pit`     申万一级行业 PIT 只读查询(history × l1_map, valid_from/valid_to)
- `quality_proxy` 日线质量代理(财务按 announce_date 做 PIT)
- `quality_rank`  申万一级行业内的质量相对分位 + 白名单**试跑**判定(g2 窄化)
- `rebalance_snapshot` 月度再平衡快照(纯内存, 不调度不写盘)
- `valuation_stub` / `health_veto_stub` 估值与健康接口桩, 返回 pending

**g2 窄化做了什么**: `limit_reopen` 构造即 `observe`; 行业内相对分位; 白名单试跑
`p40` 并明确**未锁定**。**没做**: 估值腿实装、健康规则引擎、权重标定、p30/p50
敏感性。

**gp 做了什么**: `DiskSignalRegistryStore` 真的能落盘了 —— 但只在
`BUY_SELL_SIGNALS_PERSIST` 为真值时, 经 `build_signal_registry_store()` 工厂开启;
默认仍是内存实现, 进程退出即消失。落盘写 `data/buy_sell_signals/registry.json`,
不碰 `partition_writer` 的生产分区。**gp 没做**: 推送、路由、日管道自动 upsert
—— **落盘 ≠ 推送**。

**gpush 做了什么**: 推送出口 `push.try_push()` —— 但只在 `BUY_SELL_SIGNALS_PUSH`
为真值时真实扇出, 默认关。通道写死为 **SSE(`QuoteService.push_alerts`) + 站内
(`alert_store.append_many` → `alerts.jsonl`)**, 复用既有通道, 不新建总线、不新建
SSE 路由。触发 = 状态变化 且 `publishable` 且 `push_gate=armed` 且 `push_eligible`;
成功后写 `last_pushed_at` + 30 分钟冷却 + `push_gate=cooldown`。**gpush 没做**:
`news.push`、日管道自动扫晋升、任何自动 `publishable`。

**gding 做了什么**: 在同一个 `try_push` 的资格门之后、写回冷却之前, 接上**既有**
钉钉 webhook —— 条件是 `BUY_SELL_SIGNALS_PUSH` 开 **且** `BUY_SELL_SIGNALS_DINGTALK`
开 **且** `settings.dingtalk_webhook_url` 非空(三者**与**关系, 钉钉门默认关)。
发送只调既有的 `app.news.dingtalk.send_markdown`, 与资讯/ETF/价位提醒同一个机器人,
不新建 webhook、不新建机器人、不复制加签实现。同一次状态变化可以 SSE + 站内 + 钉钉
同时发, 但 `last_pushed_at` / `push_dedupe_cooldown_until` / `push_gate=cooldown`
**只写一次**。钉钉是 best-effort: 发送失败只 warning, 不回滚已成功的站内/SSE,
仍写一次冷却。**gding 没做**: 飞书(**已取消, 从路线图去掉, 不再排期**)、
钉钉独立冷却、钉钉独立触发时钟、日管道自动扫晋升。

**gval 做了什么(批次 20261025gval · 估值/健康窄开)**: 只做**契约加固 + 设计钉 +
测例/文档**, 一行打分逻辑都没有。

- **估值**: `VALUATION_REQUIRED_INPUTS` 四项写死可见; `v_score` 恒 `None`;
  `tested_status` 恒 `untested`; 新增 `VALUATION_SNAPSHOT_PE_FORBIDDEN` 把"禁快照
  pe"从注释变成可被扫描的符号; `ValuationStub.score_emitted` 恒 `False`。
- **健康**: 新增**草稿**原因码目录 `HEALTH_VETO_REASON_CATALOG_DRAFT`(PHASE2 §3.3
  五码)与禁止码集 `HEALTH_VETO_FORBIDDEN_PASS_CODES`(`data_insufficient_treat_as_pass`);
  `health_veto_hit` 仍恒 `None`; `health_veto_rule_version` 仍 `design_pending`;
  新增 `HealthVetoStub.passed`, 定义只有 `hit is False` 才算通过(当前恒 `False`)。
  文档写死 `hit=None` 在 `if hit:` 下是"**不阻止**", **不是**"已健康通过"。
- **质量**: 字段契约表进 docstring(`q_score` 是计数不是因子值); 原因码闭集
  (`QUALITY_MISSING_REASON_CODES` / `..._PREFIXES`)落地; `w_value` 进 `to_dict`;
  把过时的"0–5"改成实际的 0–7。**七列没有增删, 权重仍 `None` + `design_pending`**。

**gval 没做**: 逐日 PE/PB 采集、任何 `v_score` 实装、健康规则引擎(`hit=true/false`)、
白名单锁定、p30/p50 敏感性、竞价/放量、改 PERSIST/PUSH/DINGTALK、日管道接线。

**gwlist 做了什么(批次 20261025gwlist · 白名单锁定窄开 · P1-2)**: **勿真锁**。

- **口径复核**: `WHITELIST_PERCENTILE_TRIAL == "p40"`(标签, 行业前 40%) ·
  `WHITELIST_TRIAL_QUANTILE == 0.60`(阈值 = `1 - 0.40`) · 判定 `>= 0.60`(闭区间)。
  **已齐, 仅复核** —— 三个数值与判定式一个字节没改。
- **空桩复核**: `WHITELIST_PERCENTILE_LOCKED is None`、`WHITELIST_THRESHOLD_LOCKED is
  False`。本拍不加任何能写非空的路径。
- **缺口钉死**: 新增 `WHITELIST_LOCK_EVIDENCE_PENDING`(四条标签)与
  `WHITELIST_LOCK_EVIDENCE`(人读表), 把真锁还缺的 A 敏感性 / B 样本外 /
  C 成本 18.2bp 实测 / D 显著性 p 值写成代码里可扫的事实。**四条全 `data_pending`**。

**gwlist 没做**: 真锁(另拍)、敏感性实测、样本外回放、成本实测回填、Bonferroni p 值实测、
锁定引擎(`whitelist_lock_engine.py` 之类)、把 `score_version` 升成"已锁"叙事。
**质量/估值/健康零碰**(#208 行为字节未改)。

**gauction/gvol 做了什么(批次 20261025gauction · 放量窄开 · P1-3)**: **勿真开**,
且**竞价整段不做**。

- **身份钉**: 新建 `volume_breakout_stub.py`, 把 `signal_id="volume_breakout"` ·
  `event_class="intraday_volume"` · `family="minute_event"` ·
  `auction_stat_bucket=None`(恒) · `auction_rule_id=None` 写成可扫常量。
- **分列契约**: 放量**不得**写入 `auction_0925`; `SignalRecord` 的混桶拒斥本就在 tip
  生效, 本拍只显式覆盖放量路径并测下来。缺竞价包不得用放量回填竞价。
- **空桩**: `evaluate_volume_breakout()` 恒返回 `data_pending`; `triggered` 恒 `None`
  (**不是** `False` —— "不知道"与"没放量"是两件事); 量比 / 突破幅度恒 `None`, 不填假数。
- **四条缺口**: `VOLUME_BREAKOUT_EVIDENCE_PENDING` 覆盖本机分钟库 / 触发证据表 /
  HTTP 凭证 / 阈值锁定, 全部 `data_pending`。
- **既有反证**: 仓内 `app/custom/dsa/quant_evidence.yaml` 的 `volume_breakout` 四个口径
  回测全为负(-23.86% / -32.44% / -13.80% / -19.81%, `strongly_negative`)。
  开闸前要先解释这个负号 —— 见 `VOLUME_BREAKOUT_NEGATIVE_EVIDENCE`。
- **竞价不做**: 无 09:25 真实包; **永久禁止**用 09:30 或任意开盘后 bar 冒充 09:25。
  本拍不新建任何竞价引擎 / 回测 / 09:25 路径。

**gauction 没做**: 放量触发引擎、阈值锁定(量比/突破幅度/窗口分钟数一个数值都没钉)、
假量回测表、竞价实现(另等 09:25 包)、扩 `POST_OPEN_NOISE_BUCKETS`、改三 env、
改 `push.py` / `store.py` / `main.py` / `t_rounds`。
**质量/估值/健康/白名单零碰**(#208 / #209 行为字节未改)。

**gauction2 做了什么(批次 20261026gauction2 · 竞价窄开)**: 竞价信号腿**定义钉 +
读存档适配 + 显式 pending**; **不重做放量**(#210 行为字节未改)。

- **身份钉**: 新建 `auction_abnormal_stub.py`, 把 `signal_id="auction_abnormal"` ·
  `event_class="auction"` · `family="minute_event"` ·
  `auction_stat_bucket="auction_0925"`(恒) · `post_open_noise_flag=False` ·
  `post_open_noise_bucket=None`(禁带) · `auction_rule_id=None` 写成可扫常量。
- **只读适配**: 只读 `data/auction_scan/{date}.json`(经 `get_scan`), 映射出候选;
  缺文件 fail-closed → `no_data` / `data_pending`; **不**重抓快照 / **不**跑
  SCAN_WINDOW / **不**盘后补扫。`auction_ts` 由 `trade_date` + 固定 09:25:00+08:00
  推出, **不读分钟 bar**。
- **四条 pending**: `auction_price_not_persisted`(落盘无竞价价) ·
  `threshold_lock_data_pending`(UI 阈值 != 回测门禁) ·
  `backtest_evidence_data_pending`(无回测/触发表) ·
  `minute_0925_still_absent`(分钟库仍无 09:25, 且永不能用 09:30 清掉)。
- **恒不触发**: `AUCTION_ABNORMAL_TRIGGERING_ENABLED=False`; `triggered` 恒 `None`
  (**不是** `False`); `auction_price` 恒 `None`(不填 0、不用 gap 倒推)。
- **与混合桶正交**: 竞价恒 `auction_0925`、放量恒 `None`; 双向混桶均被
  `SignalRecord` 拒; 本拍**零改** `volume_breakout_stub.py`。

**gauction2 没做**: 补 scan 落盘 `auction_price`(默不开) · 阈值锁定 · 回测证据表 ·
分钟 09:25 bar · 真锁 · 估值打分 · 健康引擎 · 改 `SCAN_WINDOW` / 落盘 schema ·
改三 env · 竞价触发引擎。
**放量 / 质量 / 估值 / 健康 / 白名单零碰**(#208 / #209 / #210 行为字节未改)。

现行口径: **落盘已开 · 推送已开(SSE/站内) · 钉钉可选(env) · 飞书已取消 · 竞价由 blocked 转部分解阻(B1 有 scan 存档 / B2 分钟 09:25 仍缺) · 放量身份/契约已钉、触发仍 pending · 竞价身份/只读适配已钉、四缺口 pending · 估值/健康仍 pending · 白名单试跑未锁**。

**gp3b 做了什么(批次 20261028gp3b · P-bss-p3b · 影子生产写入)**: 补上**影子生产
写入路径** —— 在这之前, 影子命中只能活在测例的临时目录里, 落不到 `data/`。

- **新模块** `hits_shadow_store.py`: 写/读/计数四件套
  (`upsert_shadow_hit` / `write_shadow_hits` / `list_shadow_hits` /
  `count_shadow_hits`), 外加 `shadow_bucket_dir` / `shadow_bucket_path` 两个纯路径函数。
- **独立三键分桶**: `<data_dir>/buy_sell_signals/shadow/<signal_id>/
  <definition_version>/<freq_lock_version>/hits.json`(常量 `SHADOW_SUBDIR` +
  `SHADOW_BUCKET_KEY_FIELDS`)。**不是**把影子混进 live `hits.json`, 也**不是**
  一个"shadow 标志位" —— 分桶是物理隔离, `DiskHitStore.list_all()` 读不到影子。
- **契约**: 只收 `source.mode == "shadow"` 的命中(`live` / `fixture` 直接
  `ShadowWriteError`); `freq_lock_version` 必填且必须非空; 三键拒路径穿越
  (`.`, `..`, `/`, `\\`, NUL)。
- **原子写**: `tempfile.mkstemp(dir=<桶目录>)` + `os.replace`, 同文件系统内原子;
  批写全有或全无(任一键非法 → 一条都不落)。
- **幂等**: 同 `hit_id` 覆盖、不同 `hit_id` 追加; 三键不同 → 各自独立的桶, 互不串。
- **零放闸**: 写进去之后 `visible_hits` 仍 0 条(拒因 `NON_LIVE_MODE`)、
  `try_push_hit` 仍 `shadow_not_pushable`、opener 零调用 —— P3a 三闸**一点没松**。
  `HitStore` Protocol 仍只有 `list_all()`, 没加 upsert/write/save/append;
  三 env(`PERSIST`/`PUSH`/`DINGTALK`)一个字节没改。

**gp3b 没做**: 日管道自动写影子(接线另拍)、影子命中进 UI、影子→live 晋升、
任何 `TRIGGERING_ENABLED = True`、任何 `allow_shadow` 开关、改可见性/推送判定、
改三 env、改 `push.py` / `store.py` / `main.py`。

**P3b 之后的口径**: 影子**能落盘了**, 但**落盘 ≠ 可见 ≠ 可推** —— 三重门原样还在。

估值与健康**未测通**, 只返回 `data_pending` / `design_pending`; `health_veto_hit`
保持 `None`(不是 `False` —— `False` 是一个"查过、没命中"的结论)。

本包与 `app.services.trade_marks`、`app.news.push` **没有** import 关系, 也不共用
任何提醒层常量(`VWAP_BAND` / `RANGE_BAND` / `T_COOLDOWN_MIN`); 与 `app.t_rounds`
零符号耦合。理由见 `constants`。`push.py` 复用 `QuoteService.push_alerts` 与
`alert_store` 这两个**通道**、`app.news.dingtalk.send_markdown` 这个**发送函数**,
但不 import 它们的节流参数, 也不走飞书支路(已取消)。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from .auction_abnormal_stub import (
    AUCTION_ABNORMAL_AUCTION_STAT_BUCKET,
    AUCTION_ABNORMAL_EVENT_CLASS,
    AUCTION_ABNORMAL_EVIDENCE,
    AUCTION_ABNORMAL_EVIDENCE_PENDING,
    AUCTION_ABNORMAL_SIGNAL_ID,
    AUCTION_ABNORMAL_STATUS,
    AUCTION_ABNORMAL_TRIGGERING_ENABLED,
    AuctionCandidate,
    AuctionEvaluation,
    auction_ts_for,
    build_auction_abnormal_record,
    evaluate_auction_abnormal,
)
from .calib_p25_lock import (
    CALIB_AUX_HORIZONS_TD,
    CALIB_PRIMARY_HORIZON_TD,
    P25_ALIGN_KEY_FIELDS,
    P25_ANY_GATE_FAIL_STATUS,
    P25_BEFORE_P2,
    P25_CROSS_VERSION_MERGE_FORBIDDEN,
    P25_HOLM_BH_SUBSTITUTE_FORBIDDEN,
    P25_LOCK_NOTE,
    P25_PRIMARY_SWAP_FORBIDDEN,
    P25_SAMPLE_N_MIN,
    WILSON_CONFIDENCE,
    WILSON_HIT_RATE_LB_MIN,
    WILSON_POSITIVE_RULE,
    p25_gate_verdict,
    wilson_hit_rate_lower_bound,
)
from .calib_p2_eval import (
    P2_EVAL_BATCH_TAG,
    P2_EVAL_NOTE,
    P2_EVAL_SIGNAL_ID,
    P2_EVAL_SPEC_REF,
    P2_PATH_STATUS_GATE_MET,
    P2_PATH_STATUS_REACHED,
    P2EvalInputError,
    EvalRow,
    truncated_replay_once,
    truncated_replay_twice_equal,
)
from .constants import (
    BONFERRONI_M,
    BONFERRONI_SCHEME_ID,
    DINGTALK_ENV_NAME,
    DISCLAIMER_KEY,
    NAMESPACE,
    PERSIST_ENV_NAME,
    PERSIST_SUBDIR,
    PUSH_ENV_NAME,
    REGISTRY_FILENAME,
    SHADOW_BUCKET_KEY_FIELDS,
    SHADOW_SUBDIR,
    SIGNAL_DESIGN_PIN,
    design_version,
)
from .disclaimer import DISCLAIMER_TEXT
from .first_seen_audit import (
    EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE,
    EVENT_TRACK_SHADOW_BLOCK_TOKEN,
    FIRST_SEEN_MEANING,
    FIRST_SEEN_NOT,
    PREFERRED_SOURCE,
    REASON_ASOF_BEFORE_FIRST_SEEN,
    REASON_FIRST_SEEN_MISSING,
    REASON_UNPARSABLE,
    RESEARCH_FIRST_SEEN_SUBDIR,
    assert_asof_not_before_first_seen,
)
from .health_veto_stub import (
    HEALTH_VETO_REASON_CATALOG_DRAFT,
    HealthVetoStub,
    health_veto_asof,
)
from .hits_shadow_store import (
    SHADOW_BUCKET_KEYS,
    SHADOW_MODE,
    ShadowWriteError,
    count_shadow_hits,
    list_shadow_hits,
    shadow_bucket_dir,
    shadow_bucket_path,
    upsert_shadow_hit,
    write_shadow_hits,
)
from .calib_p25_evt_lock import (
    EVT_CALIB_AUX_HORIZONS,
    EVT_CALIB_PRIMARY_HORIZON,
    EVT_P25_ANY_GATE_FAIL_STATUS,
    EVT_P25_BEFORE_P2,
    EVT_P25_COST_BP_ROUNDTRIP,
    EVT_P25_COST_BP_SENSITIVITY,
    EVT_P25_HOLM_BH_SUBSTITUTE_FORBIDDEN,
    EVT_P25_HORIZON_STARTS_AT_FIRST_SEEN,
    EVT_P25_LOCK_NOTE,
    EVT_P25_MISSING_FIRST_SEEN_FAIL_CLOSED,
    EVT_P25_PRIMARY_SWAP_FORBIDDEN,
    EVT_P25_SAMPLE_N_MIN,
    assert_evt_lock_invariants,
    is_shadow_calib_claim_allowed,
)
from .models import SignalRecord, SignalValidationError
from .naming_budget_a1 import (
    A1_BUDGET_EMPTY,
    A1_BUDGET_OCCUPIED_OR_RESERVED,
    A1_BUDGET_SLOTS,
    A1_BUDGET_SLOT_COUNT,
    A1_D1_SIGNAL_ID,
    A1_D2A_SIGNAL_ID,
    A1_D2B_SIGNAL_ID,
    A1_D2_INITIAL_CALIB_STATUS,
    A1_D2_MUST_NOT_INHERIT_D1_CALIB_PASSED,
    A1_E1_SIGNAL_ID,
    A1_PREFIX_DAY,
    A1_PREFIX_D1_GRANDFATHER,
    A1_PREFIX_EVT,
    A1_PREFIX_FIXTURE,
    A1_SEVENTH_BUMP_TOKEN,
    A1_SEVENTH_REQUIRES_SCHEME_BUMP,
    assert_budget_invariants,
    assert_d2_not_riding_d1,
    assert_no_seventh_without_bump,
)
from .push import (
    PushDisabled,
    PushError,
    PushRejected,
    PushResult,
    build_dingtalk_text,
    dingtalk_env_enabled,
    dingtalk_webhook_ready,
    push_env_enabled,
    try_push,
)
from .quality_proxy import (
    QUALITY_METRIC_COLUMNS,
    QUALITY_MISSING_REASON_CODES,
    QUALITY_MISSING_REASON_PREFIXES,
    QualityProxy,
    evaluate_quality,
)
from .quality_rank import (
    WHITELIST_LOCK_EVIDENCE,
    WHITELIST_LOCK_EVIDENCE_PENDING,
    WHITELIST_PERCENTILE_LOCKED,
    WHITELIST_PERCENTILE_TRIAL,
    WHITELIST_THRESHOLD_LOCKED,
    IndustryRankResult,
    percentile_rank,
    rank_within_industry,
)
from .rebalance_snapshot import (
    RebalanceSnapshot,
    build_rebalance_snapshot,
    make_rebalance_id,
)
from .state_machine import (
    MANUAL_PUBLISH_CHECKLIST,
    ManualPublishRejected,
    SignalStateError,
    SignalTransitionError,
    manual_publish,
    transition,
)
from .store import (
    DiskSignalRegistryStore,
    InMemorySignalRegistryStore,
    PersistDisabledError,
    PublishPointerConflict,
    SignalRegistryCorruptError,
    SignalRegistryError,
    SignalRegistryStore,
    build_signal_registry_store,
)
from .volume_breakout_stub import (
    VOLUME_BREAKOUT_EVENT_CLASS,
    VOLUME_BREAKOUT_EVIDENCE,
    VOLUME_BREAKOUT_EVIDENCE_PENDING,
    VOLUME_BREAKOUT_NEGATIVE_EVIDENCE,
    VOLUME_BREAKOUT_SIGNAL_ID,
    VOLUME_BREAKOUT_STATUS,
    VolumeBreakoutStub,
    build_volume_breakout_record,
    evaluate_volume_breakout,
)

__all__ = [
    "AUCTION_ABNORMAL_AUCTION_STAT_BUCKET",
    "AUCTION_ABNORMAL_EVENT_CLASS",
    "AUCTION_ABNORMAL_EVIDENCE",
    "AUCTION_ABNORMAL_EVIDENCE_PENDING",
    "AUCTION_ABNORMAL_SIGNAL_ID",
    "AUCTION_ABNORMAL_STATUS",
    "AUCTION_ABNORMAL_TRIGGERING_ENABLED",
    "AuctionCandidate",
    "AuctionEvaluation",
    "BONFERRONI_M",
    "BONFERRONI_SCHEME_ID",
    "CALIB_AUX_HORIZONS_TD",
    "CALIB_PRIMARY_HORIZON_TD",
    "DINGTALK_ENV_NAME",
    "DISCLAIMER_KEY",
    "DISCLAIMER_TEXT",
    "DiskSignalRegistryStore",
    "EvalRow",
    "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE",
    "EVENT_TRACK_SHADOW_BLOCK_TOKEN",
    "FIRST_SEEN_MEANING",
    "FIRST_SEEN_NOT",
    "PREFERRED_SOURCE",
    "REASON_ASOF_BEFORE_FIRST_SEEN",
    "REASON_FIRST_SEEN_MISSING",
    "REASON_UNPARSABLE",
    "RESEARCH_FIRST_SEEN_SUBDIR",
    "assert_asof_not_before_first_seen",
    "HEALTH_VETO_REASON_CATALOG_DRAFT",
    "HealthVetoStub",
    "InMemorySignalRegistryStore",
    "IndustryRankResult",
    "MANUAL_PUBLISH_CHECKLIST",
    "ManualPublishRejected",
    "NAMESPACE",
    "A1_BUDGET_EMPTY",
    "A1_BUDGET_OCCUPIED_OR_RESERVED",
    "A1_BUDGET_SLOTS",
    "A1_BUDGET_SLOT_COUNT",
    "A1_D1_SIGNAL_ID",
    "A1_D2A_SIGNAL_ID",
    "A1_D2B_SIGNAL_ID",
    "A1_D2_INITIAL_CALIB_STATUS",
    "A1_D2_MUST_NOT_INHERIT_D1_CALIB_PASSED",
    "A1_E1_SIGNAL_ID",
    "A1_PREFIX_DAY",
    "A1_PREFIX_D1_GRANDFATHER",
    "A1_PREFIX_EVT",
    "A1_PREFIX_FIXTURE",
    "A1_SEVENTH_BUMP_TOKEN",
    "A1_SEVENTH_REQUIRES_SCHEME_BUMP",
    "assert_budget_invariants",
    "EVT_CALIB_AUX_HORIZONS",
    "EVT_CALIB_PRIMARY_HORIZON",
    "EVT_P25_ANY_GATE_FAIL_STATUS",
    "EVT_P25_BEFORE_P2",
    "EVT_P25_COST_BP_ROUNDTRIP",
    "EVT_P25_COST_BP_SENSITIVITY",
    "EVT_P25_HOLM_BH_SUBSTITUTE_FORBIDDEN",
    "EVT_P25_HORIZON_STARTS_AT_FIRST_SEEN",
    "EVT_P25_LOCK_NOTE",
    "EVT_P25_MISSING_FIRST_SEEN_FAIL_CLOSED",
    "EVT_P25_PRIMARY_SWAP_FORBIDDEN",
    "EVT_P25_SAMPLE_N_MIN",
    "assert_evt_lock_invariants",
    "is_shadow_calib_claim_allowed",
    "assert_d2_not_riding_d1",
    "assert_no_seventh_without_bump",
    "P25_ALIGN_KEY_FIELDS",
    "P25_ANY_GATE_FAIL_STATUS",
    "P25_BEFORE_P2",
    "P25_CROSS_VERSION_MERGE_FORBIDDEN",
    "P25_HOLM_BH_SUBSTITUTE_FORBIDDEN",
    "P25_LOCK_NOTE",
    "P25_PRIMARY_SWAP_FORBIDDEN",
    "P25_SAMPLE_N_MIN",
    "P2EvalInputError",
    "P2_EVAL_BATCH_TAG",
    "P2_EVAL_NOTE",
    "P2_EVAL_SIGNAL_ID",
    "P2_EVAL_SPEC_REF",
    "P2_PATH_STATUS_GATE_MET",
    "P2_PATH_STATUS_REACHED",
    "PERSIST_ENV_NAME",
    "PERSIST_SUBDIR",
    "PUSH_ENV_NAME",
    "PersistDisabledError",
    "PublishPointerConflict",
    "PushDisabled",
    "PushError",
    "PushRejected",
    "PushResult",
    "QUALITY_METRIC_COLUMNS",
    "QUALITY_MISSING_REASON_CODES",
    "QUALITY_MISSING_REASON_PREFIXES",
    "QualityProxy",
    "REGISTRY_FILENAME",
    "RebalanceSnapshot",
    "SHADOW_BUCKET_KEYS",
    "SHADOW_BUCKET_KEY_FIELDS",
    "SHADOW_MODE",
    "SHADOW_SUBDIR",
    "SIGNAL_DESIGN_PIN",
    "ShadowWriteError",
    "SignalRecord",
    "SignalRegistryCorruptError",
    "SignalRegistryError",
    "SignalRegistryStore",
    "SignalStateError",
    "SignalTransitionError",
    "SignalValidationError",
    "VOLUME_BREAKOUT_EVENT_CLASS",
    "VOLUME_BREAKOUT_EVIDENCE",
    "VOLUME_BREAKOUT_EVIDENCE_PENDING",
    "VOLUME_BREAKOUT_NEGATIVE_EVIDENCE",
    "VOLUME_BREAKOUT_SIGNAL_ID",
    "VOLUME_BREAKOUT_STATUS",
    "VolumeBreakoutStub",
    "WHITELIST_LOCK_EVIDENCE",
    "WHITELIST_LOCK_EVIDENCE_PENDING",
    "WHITELIST_PERCENTILE_LOCKED",
    "WHITELIST_PERCENTILE_TRIAL",
    "WHITELIST_THRESHOLD_LOCKED",
    "WILSON_CONFIDENCE",
    "WILSON_HIT_RATE_LB_MIN",
    "WILSON_POSITIVE_RULE",
    "auction_ts_for",
    "build_auction_abnormal_record",
    "build_dingtalk_text",
    "build_rebalance_snapshot",
    "build_signal_registry_store",
    "build_volume_breakout_record",
    "count_shadow_hits",
    "design_version",
    "dingtalk_env_enabled",
    "dingtalk_webhook_ready",
    "evaluate_auction_abnormal",
    "evaluate_quality",
    "evaluate_volume_breakout",
    "health_veto_asof",
    "list_shadow_hits",
    "make_rebalance_id",
    "manual_publish",
    "p25_gate_verdict",
    "percentile_rank",
    "push_env_enabled",
    "rank_within_industry",
    "shadow_bucket_dir",
    "shadow_bucket_path",
    "transition",
    "truncated_replay_once",
    "truncated_replay_twice_equal",
    "try_push",
    "upsert_shadow_hit",
    "wilson_hit_rate_lower_bound",
    "write_shadow_hits",
]
