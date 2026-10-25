"""gwlist 隔离扫(批次 20261025gwlist · P1-2 白名单锁定窄开): 守住"没真锁"。

这一拍的性质决定了测例必须比代码更有分量: **代码几乎什么都没改**, 只加了两个常量
(frozenset + 人读表)和几段注释。真正的风险不是写错了什么, 而是**将来某一次"顺手"**:

- 顺手把试跑值 `"p40"` / `0.60` 写进 `WHITELIST_PERCENTILE_LOCKED`(最典型的一次)
- 顺手给 `THRESHOLD_LOCKED` 手写一个 `True`
- 顺手把 A–D pending 清单删掉, 然后写"已锁定"的文案
- 顺手拿 `v_score` / `hit` 当锁依据(两条腿都还 pending)

这些都不会让既有测例变红 —— 所以这里逐条静态守住。

扫描方式沿用 `test_buy_sell_signal_gval_isolation.py` 的教训: **不能直接搜文本**。
本包的文档里必须能写"禁止把 `p40` 写进 LOCKED", 全文搜索会把正当的禁令判成违规。
做法是把 AST 里所有字符串字面量换成占位符再扫代码部分。
"""
from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

import pytest

from app.buy_sell_signals import constants as C
from app.buy_sell_signals import quality_rank as qr

PKG = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
BACKEND = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------- AST 工具


def _sources() -> list[Path]:
    return sorted(PKG.glob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _code_without_strings(path: Path) -> str:
    """把源码里所有字符串字面量换成占位符后的 AST dump。

    docstring 与报错文案里必须能提到 `p40` / `0.60` / "已锁定" 这些词(否则禁令没处写),
    所以扫代码时要先把它们抹掉。
    """
    tree = _tree(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = "<STR>"
    return ast.dump(tree)


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, (ast.Tuple, ast.List)):
        out: set[str] = set()
        for elt in node.elts:
            out |= _target_names(elt)
        return out
    return set()


def _assignments_to(name: str) -> list[tuple[str, int, ast.AST]]:
    """全包所有给 `name` 赋值的 (文件名, 行号, 值节点)。"""
    found: list[tuple[str, int, ast.AST]] = []
    for path in _sources():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            names: set[str] = set()
            for tgt in targets:
                names |= _target_names(tgt)
            if name in names:
                found.append((path.name, node.lineno, node.value))
    return found


# ------------------------------------------------------- ① 口径复核(数值零改)


def test_trial_label_is_still_p40() -> None:
    """`p40` 是**标签**(行业前 40%), 不是阈值。"""
    assert qr.WHITELIST_PERCENTILE_TRIAL == "p40"


def test_trial_quantile_is_still_060() -> None:
    """阈值 = `1 - 0.40 = 0.60`, 越高越好的分位取上半部。"""
    assert qr.WHITELIST_TRIAL_QUANTILE == 0.60


def test_quantile_is_derived_from_the_label() -> None:
    """标签与阈值必须同步: 前 40% ⇔ 0.60。换算只允许一处定义。"""
    front_fraction = int(qr.WHITELIST_PERCENTILE_TRIAL[1:]) / 100
    assert abs(qr.WHITELIST_TRIAL_QUANTILE - (1.0 - front_fraction)) < 1e-12


def test_pass_uses_closed_interval_ge() -> None:
    """判定是 `>=`(闭区间)。恰好等于阈值必须通过。

    走真实入口 `rank_within_industry`, 不猜 dataclass 字段, 也不靠源码文本。
    """
    from app.buy_sell_signals.quality_proxy import QualityProxy
    from app.buy_sell_signals.quality_rank import (
        WHITELIST_PERCENTILE_TRIAL,
        WHITELIST_TRIAL_QUANTILE,
        rank_within_industry,
    )

    # 阈值本身: 标签 `p40` = 行业前 40% = rank >= 1 - 0.40 = 0.60
    assert WHITELIST_PERCENTILE_TRIAL == "p40"
    assert abs(WHITELIST_TRIAL_QUANTILE - 0.60) < 1e-12

    day = date(2026, 10, 25)

    # q_score 0..5 六只票同一天同行业 → percentile_rank 落在 [0, 1]。
    # q_missing_flag 默认 True(没跑过=不知道), 这里显式给 False 才有 rank 资格。
    proxies = [
        QualityProxy(symbol=f"60000{i}", asof=day, q_score=i, q_missing_flag=False)
        for i in range(6)
    ]
    industries = {p.symbol: "801780" for p in proxies}
    result = rank_within_industry(proxies, industries, asof=day)

    entries = list(result.entries.values())
    ranked = [e for e in entries if e.q_rank_within_ind is not None]
    assert len(ranked) >= 2, "构造失败: 至少要有两只票拿到分位, 否则这条测试是空转"

    for entry in entries:
        if entry.q_rank_within_ind is None:
            assert entry.whitelist_pass is False, f"{entry.symbol}: 没分位不可能进白名单"
            continue
        expected = entry.q_rank_within_ind >= WHITELIST_TRIAL_QUANTILE
        assert entry.whitelist_pass is expected, (
            f"{entry.symbol}: pct={entry.q_rank_within_ind} 时 whitelist_pass 应为 {expected}"
        )

    # 边界: 分位**恰好等于**阈值的票必须通过。6 只票时 percentile_rank 正好给出 0.6,
    # 这一只单独钉死 —— 否则改 `>=` 为 `>` 时"最小值仍 >= 阈值"照样成立, 测不出来。
    boundary = [e for e in entries if e.q_rank_within_ind is not None and abs(e.q_rank_within_ind - WHITELIST_TRIAL_QUANTILE) < 1e-12]
    assert boundary, "构造失败: 6 只票必须有一只分位恰好落在 0.60, 否则测不到闭区间"
    for e in boundary:
        assert e.whitelist_pass is True, (
            f"{e.symbol}: pct 恰好等于 {WHITELIST_TRIAL_QUANTILE} 时必须通过(闭区间), "
            f"写成 > 就会把这只票误判出去"
        )

    # 未通过者里最大的分位必须严格小于阈值。
    failed = [e for e in ranked if not e.whitelist_pass]
    if failed:
        max_fail = max(e.q_rank_within_ind for e in failed)
        assert max_fail < WHITELIST_TRIAL_QUANTILE


def test_no_retired_040_as_current_rule() -> None:
    """`>= 0.40` 是 #202 旧式(前 60%), 只能以"已废弃"身份出现在注释里。

    扫描的是**代码**: 判定式与常量里不许出现 0.40。
    """
    body = _code_without_strings(PKG / "quality_rank.py")
    assert "0.4" not in body, "判定式/常量里出现 0.40 —— 那是废弃的旧式"
    assert "0.6" in body, "闭区间下界 0.60 不见了"


# ------------------------------------------------- ② locked 空桩 + A–D pending


def test_locked_stays_none() -> None:
    assert qr.WHITELIST_PERCENTILE_LOCKED is None


def test_threshold_locked_stays_false() -> None:
    assert qr.WHITELIST_THRESHOLD_LOCKED is False


def test_threshold_locked_is_derived_not_handwritten() -> None:
    """派生而非手写: 两者结构上不可能分叉。"""
    assignments = _assignments_to("WHITELIST_THRESHOLD_LOCKED")
    assert len(assignments) == 1, assignments
    _, _, value = assignments[0]
    assert isinstance(value, ast.Compare), "`WHITELIST_THRESHOLD_LOCKED` 必须由 LOCKED 派生"
    assert value.ops and isinstance(value.ops[0], ast.IsNot), value.ops


def test_no_path_can_assign_a_non_empty_locked() -> None:
    """除 `= None` 这一处定义外, 全包没有任何地方给它赋值 —— 尤其没有非空值。

    这条是整批的核心: 允许的唯一赋值是 `None`。写成 `"p40"` / `0.60` / 配置加载
    / env 覆盖, 全部算违规。
    """
    offenders: list[str] = []
    for filename, lineno, value in _assignments_to("WHITELIST_PERCENTILE_LOCKED"):
        is_none = isinstance(value, ast.Constant) and value.value is None
        if not is_none:
            offenders.append(f"{filename}:{lineno}")
    assert offenders == [], f"出现非空 LOCKED 赋值: {offenders}"


def test_lock_evidence_covers_all_four_gates() -> None:
    """A 敏感性 / B 样本外 / C 成本 18.2bp / D 显著性 p 值, 四条一条不能少。"""
    expected = {
        "A_sensitivity_data_pending",
        "B_oos_data_pending",
        "C_cost_18_2bp_measured_pending",
        "D_significance_m6_p_pending",
    }
    assert set(qr.WHITELIST_LOCK_EVIDENCE_PENDING) == expected


def test_lock_evidence_is_all_pending() -> None:
    """每一条标签都必须以 `_data_pending` / `_pending` 结尾 —— 它们全是缺口, 不是完成项。"""
    for tag in qr.WHITELIST_LOCK_EVIDENCE_PENDING:
        assert tag.endswith("pending"), f"{tag} 看起来不像 pending 标签"


def test_lock_evidence_human_table_keys_match() -> None:
    """人读表的键集合必须与 pending 标签集合一致, 否则其中一处会悄悄过期。"""
    assert set(qr.WHITELIST_LOCK_EVIDENCE) == set(qr.WHITELIST_LOCK_EVIDENCE_PENDING)


def test_lock_evidence_human_table_explains_each_gate() -> None:
    """每一行都要有门槛名与"缺什么", 不能留空壳。"""
    for tag, pair in qr.WHITELIST_LOCK_EVIDENCE.items():
        assert isinstance(pair, tuple) and len(pair) == 2, tag
        gate_name, what_is_missing = pair
        assert gate_name.strip(), f"{tag} 缺门槛名"
        assert len(what_is_missing.strip()) >= 8, f"{tag} 的说明太短, 等于没说"


def test_lock_evidence_mentions_bonferroni_m6_and_18_2bp() -> None:
    """C 与 D 的两个易错点必须出现在说明里: 18.2bp 只是锚, m=6 只是分母。"""
    text = " ".join(f"{a} {b}" for a, b in qr.WHITELIST_LOCK_EVIDENCE.values())
    assert "18.2bp" in text
    assert "m=6" in text or "m = 6" in text


def test_pending_set_does_not_gate_anything() -> None:
    """pending 清单是**清单不是开关**: 它不参与判定, 清空它也不会让 LOCKED 变成非空。"""
    body = _code_without_strings(PKG / "quality_rank.py")
    # 判定只看 pct 与 quantile, 不读 pending 集合
    assert "WHITELIST_LOCK_EVIDENCE_PENDING" in body, "常量本身自然在代码里"
    # 但判定式附近不得引用它
    text = (PKG / "quality_rank.py").read_text(encoding="utf-8")
    judgement_zone = text[text.index("pct >= WHITELIST_TRIAL_QUANTILE") - 200 :]
    judgement_zone = judgement_zone[:400]
    assert "WHITELIST_LOCK_EVIDENCE_PENDING" not in judgement_zone


# ------------------------------------------------------------- ③ 三 env 不动


def test_env_names_unchanged() -> None:
    assert C.PERSIST_ENV_NAME == "BUY_SELL_SIGNALS_PERSIST"
    assert C.PUSH_ENV_NAME == "BUY_SELL_SIGNALS_PUSH"
    assert C.DINGTALK_ENV_NAME == "BUY_SELL_SIGNALS_DINGTALK"


def test_truthy_sets_still_identical() -> None:
    from app.buy_sell_signals import push as push_mod
    from app.buy_sell_signals import store as store_mod

    expected = {"1", "true", "yes", "on"}
    assert set(store_mod._TRUTHY) == expected
    assert set(push_mod._TRUTHY) == expected


def test_cooldown_unchanged() -> None:
    assert C.PUSH_DEDUPE_COOLDOWN_MIN == 30


def _is_environ_subscript(node: ast.AST) -> bool:
    """`os.environ["X"] = ...` 的赋值目标。"""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "environ"
    )


def _is_putenv_call(node: ast.AST) -> bool:
    """`os.putenv(...)` 调用。"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "putenv"
    )


def test_no_source_writes_environment() -> None:
    """本拍没有、也不该出现"在代码里把闸打开"。"""
    offenders: list[str] = []
    for path in _sources():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Assign) and any(
                _is_environ_subscript(tgt) for tgt in node.targets
            ):
                offenders.append(f"{path.name}:{node.lineno} environ[]=")
            if _is_putenv_call(node):
                offenders.append(f"{path.name}:{node.lineno} putenv")
    assert offenders == [], offenders


# ----------------------------------------------- ④ #208 三个文件零碰 + 无新实装


def test_z208_modules_have_no_lock_logic() -> None:
    """质量/估值/健康三个文件里不许出现白名单锁定逻辑 —— 本拍零碰它们。

    这不是"它们不该有", 而是"如果哪天有人把锁逻辑塞进估值桩, 说明边界被越过了"。
    """
    for filename in ("valuation_stub.py", "health_veto_stub.py", "quality_proxy.py"):
        body = _code_without_strings(PKG / filename)
        for banned in ("WHITELIST_PERCENTILE_LOCKED", "WHITELIST_TRIAL_QUANTILE", "whitelist_pass"):
            assert banned not in body, f"{filename} 出现 {banned}"


def test_no_valuation_or_health_used_as_lock_basis() -> None:
    """`v_score` / `health_veto_hit` 仍 pending, 不得作为锁依据出现在 quality_rank。"""
    body = _code_without_strings(PKG / "quality_rank.py")
    for banned in ("v_score", "health_veto_hit", "ValuationStub", "HealthVetoStub"):
        assert banned not in body, f"quality_rank 出现 {banned}"


def test_quality_rank_never_reaches_valuation_or_health_modules() -> None:
    """连**模块名**都不许出现 —— 防止用 `__import__` / `importlib` 绕过上面的名字扫描。

    上面那条只扫 `v_score` 这类符号名。用
    `__import__("app.buy_sell_signals.valuation_stub", ...)` 拿一个符号,
    名字一次都不出现, 耦合却已经建立。这里直接扫字符串字面量。
    """
    text = (PKG / "quality_rank.py").read_text(encoding="utf-8")
    for banned in ("valuation_stub", "health_veto_stub", "importlib", "__import__"):
        assert banned not in text, f"quality_rank 通过 {banned} 接触了估值/健康腿"


def test_no_lock_engine_module_exists() -> None:
    """本拍不新建锁定引擎。出现这类文件名说明有人开始造引擎了。"""
    for pattern in ("*whitelist_lock*", "*lock_engine*"):
        found = list(PKG.glob(pattern))
        assert found == [], found


def test_no_auction_or_volume_event_code() -> None:
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for banned in ("auction_backtest", "pre_open_1m", "open_1m_as_auction", "as_auction"):
            assert banned not in text, f"{path.name} 出现 {banned}"


def test_auction_bucket_constant_untouched() -> None:
    assert C.AUCTION_STAT_BUCKET_0925 == "auction_0925"


def test_no_reminder_layer_coupling() -> None:
    for path in _sources():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "trade_marks" not in alias.name, f"{path.name}: {alias.name}"
                    assert not alias.name.startswith("app.news.push"), f"{path.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert "trade_marks" not in mod, f"{path.name}: {mod}"
                assert mod != "app.news.push", f"{path.name}: {mod}"
                for alias in node.names:
                    assert alias.name not in ("VWAP_BAND", "RANGE_BAND", "T_COOLDOWN_MIN"), (
                        f"{path.name}: {alias.name}"
                    )


def test_no_t_rounds_coupling() -> None:
    body = _code_without_strings(PKG / "quality_rank.py")
    assert "t_rounds" not in body
    assert "EPSILON_VALUE_PCT" not in body


# ---------------------------------------------------------------- ⑤ 文案红线


CLAIMS = (
    "白名单已锁定",
    "已锁定白名单",
    "正式可推",
    "门槛已测通",
    "估值已测通",
    "健康门禁已验证",
)


def test_sources_make_no_lock_claims() -> None:
    """肯定语境也不许出现这些短语。

    本包的写法是"未锁定 / 不得宣称已锁", 这些否定表述不含上列**完整短语**之外的
    肯定结论; 若真出现 `白名单已锁定` 四个字连写, 无论前后文都按违规处理 ——
    因为文案一旦被截取展示, 前文就丢了。
    """
    offenders: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for claim in CLAIMS:
            if claim in text:
                offenders.append(f"{path.name}: {claim}")
    assert offenders == [], offenders


def test_module_docstrings_carry_disclaimer() -> None:
    for filename in ("quality_rank.py", "constants.py"):
        text = (PKG / filename).read_text(encoding="utf-8")
        assert "仅供研究，不构成投资建议；执行须人工确认" in text, filename


def test_docs_state_not_locked() -> None:
    """文档必须同时出现"未锁定"与 A–D 四条, 否则读者拿不到完整结论。"""
    doc = BACKEND.parent / "docs" / "buy-sell-signals-skeleton.md"
    text = doc.read_text(encoding="utf-8")
    assert "gwlist" in text
    assert "未锁定" in text
    for gate in ("敏感性", "样本外", "18.2bp", "显著性"):
        assert gate in text, gate


def test_no_secret_looking_literals() -> None:
    pattern = re.compile(r"(password|secret|token|api[_-]?key)\s*=\s*[\"'][^\"']{8,}", re.I)
    offenders: list[str] = []
    for path in _sources():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{line_no}")
    assert offenders == [], offenders


@pytest.mark.parametrize("name", ["WHITELIST_LOCK_EVIDENCE_PENDING", "WHITELIST_LOCK_EVIDENCE"])
def test_new_constants_are_exported(name: str) -> None:
    import app.buy_sell_signals as pkg

    assert name in pkg.__all__
    assert hasattr(pkg, name)
