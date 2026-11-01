"""gvol 隔离扫（批次 20261025gauction · P1-3 放量窄开）：守住"勿真开"和"竞价不做"。

这一拍和 gwlist 一样，**代码几乎什么都没做** —— 新建一个空桩文件，加两个只读常量，
补几段注释。真正的风险不是写错了什么，是**将来某一次"顺手"**：

- 顺手把 `VOLUME_BREAKOUT_STATUS` 改成 `"ready"`，或者让 stub 返回 `triggered=False`
- 顺手给量比 / 突破幅度填一个"看起来合理"的数（8.5 / 3.2% —— 假数比空值更难发现）
- 顺手把 `auction_stat_bucket` 填成 `auction_0925`，让放量"顺便"进竞价桶
- 顺手扩 `POST_OPEN_NOISE_BUCKETS`，把 PHASE3 的示意名当已测口径塞进去
- 顺手新建一个 `auction_engine.py` / `volume_breakout_engine.py`
- 顺手写「放量已测通 / 正式可推」的文案

这些都不会让既有测例变红 —— 所以这里逐条静态守住。

扫描方式沿用 gval / gwlist 的教训：**不能直接搜文本**。本包的文档里必须能写
"禁止把 `volume_breakout` 写进 `auction_0925`"，全文搜索会把正当的禁令判成违规。
做法是把 AST 里所有字符串字面量换成占位符再扫代码部分。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.buy_sell_signals import constants as C
from app.buy_sell_signals import models as M
from app.buy_sell_signals import quality_rank as qr
from app.buy_sell_signals import volume_breakout_stub as vb

BACKEND = Path(__file__).resolve().parents[1]
PKG = BACKEND / "app" / "buy_sell_signals"


# ------------------------------------------------------------------ 工具


def _sources() -> list[Path]:
    return sorted(PKG.glob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _code_without_strings(path: Path) -> str:
    """把源码里**所有**字符串字面量换成占位符后的 AST dump。

    本包的 docstring 必须能写"禁止把 volume_breakout 写进 auction_0925"，
    必须能写"不要填假量比"。全文搜索会把这类正当禁令判成违规，所以只扫代码骨架。
    """
    tree = _tree(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = "<STR>"
    return ast.dump(tree)


def _string_constants(path: Path) -> list[str]:
    return [
        node.value
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


# ------------------------------------------------ ① 身份钉（PHASE3 §3）


def test_signal_id_is_volume_breakout() -> None:
    assert vb.VOLUME_BREAKOUT_SIGNAL_ID == "volume_breakout"


def test_event_class_is_intraday_volume_and_known() -> None:
    """`event_class` 必须是盘中放量，且是 `EVENT_CLASSES` 的合法成员。"""
    assert vb.VOLUME_BREAKOUT_EVENT_CLASS == "intraday_volume"
    assert vb.VOLUME_BREAKOUT_EVENT_CLASS in C.EVENT_CLASSES


def test_event_class_is_not_auction() -> None:
    """放量**不是**竞价。这一条单独写，因为它是最容易被"顺手"改错的一个字段。"""
    assert vb.VOLUME_BREAKOUT_EVENT_CLASS != "auction"


def test_family_is_minute_event_and_known() -> None:
    assert vb.VOLUME_BREAKOUT_FAMILY == "minute_event"
    assert vb.VOLUME_BREAKOUT_FAMILY in C.SIGNAL_FAMILIES


def test_auction_stat_bucket_is_none() -> None:
    """放量恒不带竞价桶。"""
    assert vb.VOLUME_BREAKOUT_AUCTION_STAT_BUCKET is None


def test_auction_stat_bucket_is_not_the_0925_constant() -> None:
    """直接与竞价桶常量对比 —— 有人可能把常量整个赋过来，而不是写字符串。"""
    assert vb.VOLUME_BREAKOUT_AUCTION_STAT_BUCKET != C.AUCTION_STAT_BUCKET_0925


def test_auction_rule_id_is_none() -> None:
    """放量不经过竞价规则。`None` 而不是空串：空串像"有规则但名字是空的"。"""
    assert vb.VOLUME_BREAKOUT_AUCTION_RULE_ID is None
    assert vb.VOLUME_BREAKOUT_AUCTION_RULE_ID == C.AUCTION_RULE_ID_NONE


def test_signal_id_is_defined_in_exactly_one_module() -> None:
    """`VOLUME_BREAKOUT_SIGNAL_ID` 只能在 stub 里定义一次。

    注意判据是「谁把这个字符串**赋给** signal_id」，不是「谁提到了这个字符串」——
    `__init__.py` 的说明文字里出现它是正常的。
    """
    owners = [
        path.name
        for path in _sources()
        if re.search(
            r"^VOLUME_BREAKOUT_SIGNAL_ID\s*:\s*Final\s*=\s*[\"']" + re.escape(
                vb.VOLUME_BREAKOUT_SIGNAL_ID
            ),
            path.read_text(encoding="utf-8"),
            re.M,
        )
    ]
    assert owners == ["volume_breakout_stub.py"], owners


def test_record_identity_matches_the_constants() -> None:
    """构造出来的记录必须与常量一致 —— 常量对、记录错，等于白钉。"""
    record = vb.build_volume_breakout_record()
    assert record.signal_id == vb.VOLUME_BREAKOUT_SIGNAL_ID
    assert record.event_class == vb.VOLUME_BREAKOUT_EVENT_CLASS
    assert record.family == vb.VOLUME_BREAKOUT_FAMILY
    assert record.auction_stat_bucket is None
    assert record.auction_rule_id is None


def test_record_status_is_a_lifecycle_state_not_the_eval_state() -> None:
    """生命周期 `status` 必须是 `SIGNAL_STATUSES` 的成员。

    `data_pending` 是**评估**态，不在 `SIGNAL_STATUSES` 里，也不能加进去 ——
    往生命周期取值域塞一个评估态，状态机会多出一条谁都不知道怎么迁移的状态。
    """
    record = vb.build_volume_breakout_record()
    assert record.status in M.SIGNAL_STATUSES
    assert vb.VOLUME_BREAKOUT_RECORD_STATUS in M.SIGNAL_STATUSES
    assert vb.VOLUME_BREAKOUT_STATUS not in M.SIGNAL_STATUSES


def test_record_carries_the_eval_state_in_metadata() -> None:
    """评估态没丢，只是走 `metadata`，两个域不混。"""
    record = vb.build_volume_breakout_record()
    assert record.metadata["volume_breakout_status"] == "data_pending"
    assert record.metadata["volume_breakout_triggering_enabled"] is False


# ------------------------------------------------ ② 分列契约（混桶必拒）


def test_volume_record_with_auction_bucket_is_rejected() -> None:
    """把放量写进 `auction_0925` 必须**构造即失败**。

    这是本拍最要紧的一条契约。放量是连续成交、竞价是离散撮合，混在一起算出来的
    均值没有解释意义，而且**不会报错** —— 只会让两份统计一起失真。
    """
    with pytest.raises(M.SignalValidationError) as exc:
        M.SignalRecord(
            signal_id=vb.VOLUME_BREAKOUT_SIGNAL_ID,
            display_name="放量突破",
            family=vb.VOLUME_BREAKOUT_FAMILY,
            event_class=vb.VOLUME_BREAKOUT_EVENT_CLASS,
            spec_ref="PHASE3 §3",
            auction_stat_bucket=C.AUCTION_STAT_BUCKET_0925,
        )
    assert "auction_stat_bucket" in str(exc.value)


def test_volume_record_with_any_bucket_value_is_rejected() -> None:
    """不止 `auction_0925`：任何非 None 的桶都不行。"""
    for bad in (C.AUCTION_STAT_BUCKET_0925, "auction_0930", "auction"):
        with pytest.raises(M.SignalValidationError):
            M.SignalRecord(
                signal_id=vb.VOLUME_BREAKOUT_SIGNAL_ID,
                display_name="放量突破",
                family=vb.VOLUME_BREAKOUT_FAMILY,
                event_class=vb.VOLUME_BREAKOUT_EVENT_CLASS,
                spec_ref="PHASE3 §3",
                auction_stat_bucket=bad,
            )


def test_stub_payload_keeps_bucket_none_and_flags_noise_mix() -> None:
    """空桩返回的 payload 里桶是 None，并且明确告知混桶禁令。"""
    payload = vb.evaluate_volume_breakout("600000", "2026-10-25").to_dict()
    assert payload["auction_stat_bucket"] is None
    assert payload["auction_rule_id"] is None
    assert payload["noise_mix_forbidden"] is True
    assert payload["event_class"] == "intraday_volume"


def test_noise_mix_forbidden_constant_is_true() -> None:
    assert C.NOISE_MIX_FORBIDDEN is True


def test_noise_buckets_closed_set_is_not_extended() -> None:
    """本拍**不扩** `POST_OPEN_NOISE_BUCKETS`。

    PHASE3 §3 的示意名 `post_open_15m` 尚未入集；把它塞进来等于悄悄修改一个
    封闭取值域，而封闭域的意义正是"不让人随手往里加"。
    """
    expected_buckets = {"open_0930_1000", "intraday_1000_1430", "close_1430_1500"}
    actual_buckets = set(C.POST_OPEN_NOISE_BUCKETS)
    assert actual_buckets == expected_buckets
    assert "post_open_15m" not in C.POST_OPEN_NOISE_BUCKETS


def test_auction_bucket_constant_itself_is_untouched() -> None:
    assert C.AUCTION_STAT_BUCKET_0925 == "auction_0925"


# ---------------------------------------------- ③ 空桩：无假触发、无假数


def test_stub_never_reports_triggered() -> None:
    """`triggered` 恒 `None` —— "不知道"，不是 `False`（"查过，没放量"）。"""
    for symbol in ("600000", "000001", "300750"):
        stub = vb.evaluate_volume_breakout(symbol)
        assert stub.triggered is None, symbol
        assert stub.passed is False, symbol


def test_stub_status_is_data_pending() -> None:
    assert vb.VOLUME_BREAKOUT_STATUS == "data_pending"
    stub = vb.evaluate_volume_breakout("600000")
    assert stub.status == "data_pending"
    assert stub.data_pending is True


def test_stub_never_fills_volume_ratio_or_breakout_pct() -> None:
    """量比与突破幅度恒 `None`。填任何数都是假数 —— 阈值根本没标定过。"""
    stub = vb.evaluate_volume_breakout("600000")
    assert stub.volume_ratio is None
    assert stub.breakout_pct is None


def test_triggering_is_disabled_by_constant() -> None:
    assert vb.VOLUME_BREAKOUT_TRIGGERING_ENABLED is False


def test_no_volume_ratio_or_breakout_threshold_is_pinned() -> None:
    """本拍**不钉**任何量比 / 突破幅度 / 窗口分钟数的数值。

    扫代码里的浮点常量：空桩文件里不该出现"看起来像阈值"的数字。
    """
    floats = [
        node.value
        for node in ast.walk(_tree(PKG / "volume_breakout_stub.py"))
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    # 唯一允许出现的浮点是负证据里抄来的四个回测收益率(它们是引用的既有数据,
    # 不是阈值)。注意 `-23.86` 在 AST 里是 `UnaryOp(USub, Constant(23.86))`,
    # 所以 `Constant` 取到的是正值 —— 白名单要取绝对值。
    allowed = {abs(v) for v in vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE["returns"]}
    unexpected = [f for f in floats if abs(f) not in allowed]
    assert unexpected == [], f"出现疑似阈值浮点: {unexpected}"


def test_stub_api_takes_no_dataframe() -> None:
    """空桩不读数据源。签名只有 symbol 与 asof，连 df 都不收。

    收了 df 就等于承诺"我能算" —— 而算所需的分钟库本机没有、阈值没有、
    触发表也没有。签名应当诚实地只表达"我知道你问的是谁、哪天"。
    """
    import inspect

    params = list(inspect.signature(vb.evaluate_volume_breakout).parameters)
    assert params == ["symbol", "asof"], params


# -------------------------------------------- ④ pending 四条可见


def test_evidence_pending_has_exactly_three_tags() -> None:
    """批次 20261101gevid 清掉 `trigger_evidence_table_data_pending` 后**恰好三条**。

    这条同时是「不许顺手多清」的守卫：其余三条（分钟库 / HTTP 鉴权 / 阈值锁）
    本拍一条都没动，少一条都会在这里红。
    """
    expected = {
        "local_minute_lib_data_pending",
        "http_minute_listing_auth_pending",
        "threshold_lock_data_pending",
    }
    assert set(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING) == expected
    # 已清的那条必须**真的不在集合里**（不是改成 _done 留在里面）
    assert "trigger_evidence_table_data_pending" not in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING


def test_evidence_tags_all_end_with_pending() -> None:
    """每条都是**缺口**，不是完成项。清掉某条时应当删标签，而不是改成 `_done`。"""
    for tag in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING:
        assert tag.endswith("pending"), tag


def test_evidence_human_table_keys_match() -> None:
    assert set(vb.VOLUME_BREAKOUT_EVIDENCE) == set(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING)


def test_evidence_human_table_explains_each_gate() -> None:
    for tag, pair in vb.VOLUME_BREAKOUT_EVIDENCE.items():
        assert isinstance(pair, tuple) and len(pair) == 2, tag
        gate_name, what_is_missing = pair
        assert gate_name.strip(), f"{tag} 缺门槛名"
        assert len(what_is_missing.strip()) >= 8, f"{tag} 的说明太短, 等于没说"


def test_evidence_covers_minute_lib_auth_and_threshold() -> None:
    """在册三条缺口各自的**关键词**都要出现，防止写成"数据没到"这种糊过去的说明。"""
    text = " ".join(f"{a} {b}" for a, b in vb.VOLUME_BREAKOUT_EVIDENCE.values())
    for keyword in ("分钟", "401", "阈值"):
        assert keyword in text, keyword


def test_cleared_evidence_table_gap_has_a_real_deliverable() -> None:
    """「触发表」这条缺口被清，靠的是**真有模块**，不是把标签抹掉。

    人读表里已经没有「触发」描述了 —— 因为缺口没了。但缺口没了必须有对应交付物：
    `volume_breakout_evidence_table` 四行在册。没有这个模块却删标签，就是空删。
    """
    from app.buy_sell_signals import volume_breakout_evidence_table as table

    assert table.VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION == (
        "volume_breakout_evidence_table_v1"
    )
    assert len(list(table.iter_rows())) == 4
    # 人读表里不该再有「触发表缺失」这类说法
    text = " ".join(f"{a} {b}" for a, b in vb.VOLUME_BREAKOUT_EVIDENCE.values())
    assert "没有放量突破的回测表" not in text


def test_stub_exposes_pending_tags_to_callers() -> None:
    """调用方拿到结果时就该看见"还有四条缺口"，而不是去翻源码才发现。"""
    stub = vb.evaluate_volume_breakout("600000")
    assert set(stub.evidence_pending) == set(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING)


def test_auth_pending_tag_writes_no_secret() -> None:
    """凭证那条只允许写"未交"，不允许把凭证本身写进来。"""
    pattern = re.compile(r"(password|secret|token|api[_-]?key)\s*=\s*[\"'][^\"']{8,}", re.I)
    offenders = []
    for path in _sources():
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{no}")
    assert offenders == [], offenders


# ---------------------------------------- ⑤ 既有反证被如实记录（勿真开的依据）


def test_negative_evidence_is_recorded() -> None:
    """仓内 `quant_evidence.yaml` 已有四个口径全为负的回测结论。

    这不是本拍造的数，是从既有文件抄来的。记录它的意义：开闸前必须先解释这个负号，
    而不是"等数据到了就开跑"。
    """
    neg = vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE
    assert neg["verdict"] == "strongly_negative"
    assert all(r < 0 for r in neg["returns"]), neg["returns"]
    assert len(neg["returns"]) == len(neg["matched"]) == 4


def test_negative_evidence_matches_the_source_file() -> None:
    """抄来的数必须与源文件对得上 —— 否则它就是本拍自己编的数。"""
    source = BACKEND / "app" / "custom" / "dsa" / "quant_evidence.yaml"
    text = source.read_text(encoding="utf-8")
    assert "volume_breakout:" in text
    for value in vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE["returns"]:
        assert f"{value}" in text, f"{value} 在源文件里找不到"


# --------------------------------------- ⑥ 无竞价实现路径 / 无引擎


def test_no_auction_implementation_in_the_diff() -> None:
    """本包不得出现竞价回测 / 09:25 实现路径的符号（与 gwlist 隔离同族）。"""
    banned = ("auction_backtest", "pre_open_1m", "open_1m_as_auction", "as_auction")
    offenders = []
    for path in _sources():
        for word in banned:
            if word in _code_without_strings(path):
                offenders.append(f"{path.name}:{word}")
    assert offenders == [], offenders


def test_no_auction_or_engine_module_is_created() -> None:
    """本拍不新建竞价引擎 / 放量触发引擎。出现这类文件名说明有人开始造引擎了。"""
    for pattern in ("auction_engine*.py", "auction_0925*.py", "volume_breakout_engine*.py"):
        found = list(PKG.glob(pattern))
        assert found == [], found


def test_no_0925_masquerade_in_new_module() -> None:
    """新模块里不得出现"用开盘后 bar 冒充 09:25"的叙事。

    只扫肯定句：文档里必须能写"禁止用 09:30 冒充 09:25"，那是禁令不是行为。
    """
    text = (PKG / "volume_breakout_stub.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if "09:30" in line and "09:25" in line:
            # 允许出现在禁令语境里
            assert any(k in line for k in ("禁止", "不得", "不要", "冒充")), line


def test_no_trigger_engine_surface_in_stub() -> None:
    """空桩不得暴露"计算触发"的入口：没有 threshold / detect / scan 这类函数名。"""
    tree = _tree(PKG / "volume_breakout_stub.py")
    funcs = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for name in funcs:
        for bad in ("detect", "scan", "calibrate", "trigger_at", "is_triggered"):
            assert bad not in name, f"出现疑似触发入口: {name}"


def test_no_triggered_true_assignment_anywhere() -> None:
    """`triggered` 不得被赋 `True`。扫 AST，不扫文本（注释里要能写这句话）。"""
    offenders = []
    for path in _sources():
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant) and node.value.value is True):
                continue
            for tgt in node.targets:
                name = getattr(tgt, "id", None) or getattr(tgt, "attr", None)
                if name == "triggered":
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], offenders


# ------------------------------------------------- ⑦ 三 env / 上游不回退


def test_env_names_unchanged() -> None:
    assert C.PERSIST_ENV_NAME == "BUY_SELL_SIGNALS_PERSIST"
    assert C.PUSH_ENV_NAME == "BUY_SELL_SIGNALS_PUSH"
    assert C.DINGTALK_ENV_NAME == "BUY_SELL_SIGNALS_DINGTALK"


def test_truthy_set_is_unchanged() -> None:
    """两个 `_TRUTHY` 副本必须仍然一致且等于同一组真值。"""
    from app.buy_sell_signals import push as push_mod
    from app.buy_sell_signals import store as store_mod

    expected = {"1", "true", "yes", "on"}
    assert set(store_mod._TRUTHY) == expected
    assert set(push_mod._TRUTHY) == expected


def test_cooldown_unchanged() -> None:
    assert C.PUSH_DEDUPE_COOLDOWN_MIN == 30


def test_no_source_writes_environment() -> None:
    """本拍没有、也不该出现"在代码里把闸打开"。"""
    offenders: list[str] = []
    for path in _sources():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Assign) and any(
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Attribute)
                and tgt.value.attr == "environ"
                for tgt in node.targets
            ):
                offenders.append(f"{path.name}:{node.lineno} environ[]=")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "putenv"
            ):
                offenders.append(f"{path.name}:{node.lineno} putenv")
    assert offenders == [], offenders


def test_gwlist_whitelist_still_unlocked() -> None:
    """#209 不回退：白名单仍未锁。"""
    assert qr.WHITELIST_PERCENTILE_LOCKED is None
    assert qr.WHITELIST_THRESHOLD_LOCKED is False
    assert qr.WHITELIST_PERCENTILE_TRIAL == "p40"
    assert qr.WHITELIST_TRIAL_QUANTILE == 0.60


def test_gval_valuation_and_health_still_pending() -> None:
    """#208 不回退：估值 / 健康仍 pending。"""
    from datetime import date

    from app.buy_sell_signals import health_veto_stub as hv
    from app.buy_sell_signals import valuation_stub as vs

    day = date(2026, 10, 25)

    stub = vs.valuation_asof("600000", day)
    assert stub.v_score is None
    assert stub.data_pending is True

    health = hv.health_veto_asof("600000", day)
    assert health.health_veto_hit is None
    assert health.passed is False


def test_volume_module_does_not_couple_to_reminder_layer() -> None:
    """禁 import 提醒层的 BAND / 冷却常量。"""
    text = (PKG / "volume_breakout_stub.py").read_text(encoding="utf-8")
    for banned in ("trade_marks", "news.push", "VWAP_BAND", "RANGE_BAND", "T_COOLDOWN_MIN"):
        assert banned not in text, banned


def test_volume_module_does_not_couple_to_t_rounds() -> None:
    text = (PKG / "volume_breakout_stub.py").read_text(encoding="utf-8")
    assert "t_rounds" not in text
    assert "EPSILON_VALUE_PCT" not in text


# --------------------------------------------------------- ⑧ 文案红线


def test_sources_make_no_ready_or_locked_claims() -> None:
    """禁「放量正式可推 / 放量已测通 / 放量已锁定 / 竞价已就绪」这类肯定句。

    先去掉断言自己的字符串与 `# noqa` 之外的注释帧不处理 —— 这里只扫源码里的字符串
    常量与文档字符串，允许它们出现在"禁止…"的否定语境里。
    """
    claims = ("放量正式可推", "放量已测通", "放量已锁定", "竞价已就绪")
    offenders = []
    for path in _sources():
        for text in _string_constants(path):
            for claim in claims:
                if claim in text and not any(k in text for k in ("禁止", "不得", "未", "不")):
                    offenders.append(f"{path.name}:{claim}")
    assert offenders == [], offenders


def test_module_docstring_has_disclaimer() -> None:
    text = (PKG / "volume_breakout_stub.py").read_text(encoding="utf-8")
    assert "仅供研究，不构成投资建议；执行须人工确认" in text


def test_docs_have_a_gauction_section() -> None:
    doc = BACKEND.parent / "docs" / "buy-sell-signals-skeleton.md"
    text = doc.read_text(encoding="utf-8")
    assert "gauction" in text
    assert "放量" in text
    assert "竞价" in text
    # 软提醒 7 条里的关键事实
    for keyword in ("MINUTE_CALIBER", "18.2bp", "Bonferroni", "红涨绿跌"):
        assert keyword in text, keyword


# ------------------------------------------------------------ ⑨ 导出


@pytest.mark.parametrize(
    "name",
    [
        "VOLUME_BREAKOUT_EVIDENCE",
        "VOLUME_BREAKOUT_EVIDENCE_PENDING",
        "VOLUME_BREAKOUT_EVENT_CLASS",
        "VOLUME_BREAKOUT_NEGATIVE_EVIDENCE",
        "VOLUME_BREAKOUT_SIGNAL_ID",
        "VOLUME_BREAKOUT_STATUS",
        "VolumeBreakoutStub",
        "build_volume_breakout_record",
        "evaluate_volume_breakout",
    ],
)
def test_new_symbols_are_exported(name: str) -> None:
    import app.buy_sell_signals as pkg

    assert name in pkg.__all__
    assert hasattr(pkg, name)


def test_all_has_no_duplicates_and_resolves() -> None:
    import app.buy_sell_signals as pkg

    assert len(pkg.__all__) == len(set(pkg.__all__))
    assert [n for n in pkg.__all__ if not hasattr(pkg, n)] == []
