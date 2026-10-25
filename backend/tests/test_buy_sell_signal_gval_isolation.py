"""gval 隔离扫(批次 20261025gval): 用静态扫描守住"本派什么都没动"。

这份测例的存在理由很具体: gval 是一个**看起来什么都没做**的批次 —— 它只加常量、
改 docstring、补测例。这种改动最容易夹带私货: 顺手把 `v_score` 算出来、顺手把
`hit` 改成 `False`、顺手把白名单锁了。而这三件事都不会让任何既有测试变红。

所以这里不测行为, 只扫**代码形状**:

1. 三 env 名字与语义未被本派改写(生产可为 1, 本派零碰)
2. 无「快照 pe → v_score」赋值路径
3. 无 `health_veto_hit=False` 赋值; 无 `w_health`
4. 无竞价 / 放量冒充路径
5. 无 `trade_marks` / `news.push` BAND·cooldown 耦合; 无 `t_rounds` 门禁耦合
6. 无"已测通 / 已锁定"文案

扫描用 `ast` 而不是全文搜索: 注释里**必须**能提到 `v_score = 0` 这种反面例子,
否则文档没法写。全文搜索会把正当的解释误伤掉。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.buy_sell_signals import constants as C
from app.buy_sell_signals import health_veto_stub as hv
from app.buy_sell_signals import quality_proxy as qp
from app.buy_sell_signals import quality_rank as qr
from app.buy_sell_signals import valuation_stub as vs

PKG = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
BACKEND = Path(__file__).resolve().parents[1]


def _sources() -> list[Path]:
    return sorted(PKG.glob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    """收集 `import x` / `from x import y` 的模块路径(不含被导入的符号名)。"""
    found: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                found.add("." * node.level + node.module)
            elif node.module:
                found.add(node.module)
    return found


def _string_constants(path: Path) -> set[str]:
    return {
        node.value
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _assigned_targets(path: Path) -> set[str]:
    """所有被赋值的**名字**(含属性链的末段), 用于查 `v_score = ...`。"""
    targets: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                targets |= _target_names(tgt)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets |= _target_names(node.target)
    return targets


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


def _code_without_strings(path: Path) -> str:
    """把源码里**所有**字符串字面量换成占位符后的 AST dump。

    用来扫"代码里有没有出现某个名字", 同时不被 docstring / 报错文案误伤。
    比按 `\"\"\"` 切分可靠: 多段 docstring 时切分点飘忽, 而且切不开单行字符串。
    """
    tree = _tree(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = "<STR>"
    return ast.dump(tree)


# --------------------------------------------------------------------- 三 env 不动


def test_env_names_are_the_production_names() -> None:
    """env 名一个字都不许改 —— 生产侧是按这三串字面量配的。"""
    assert C.PERSIST_ENV_NAME == "BUY_SELL_SIGNALS_PERSIST"
    assert C.PUSH_ENV_NAME == "BUY_SELL_SIGNALS_PUSH"
    assert C.DINGTALK_ENV_NAME == "BUY_SELL_SIGNALS_DINGTALK"


def test_truthy_sets_across_gates_are_still_identical() -> None:
    """三个闸各持一份真值集合, 且必须一模一样。

    生产 `.env` 里写什么就算开, 本派不动集合本身。加 `"y"` / `"enabled"` 之类会
    改变生产上"算不算开"的判定 —— 那不是本派能顺手做的事。

    集合没有集中到一个常量(各闸自己持有, 见 `push.py` / `store.py` 的 `_TRUTHY`),
    所以这里逐个从模块里取出来比对, 而不是读一个不存在的"全局真值集合"。
    """
    from app.buy_sell_signals import push as push_mod
    from app.buy_sell_signals import store as store_mod

    expected = {"1", "true", "yes", "on"}
    assert set(store_mod._TRUTHY) == expected
    assert set(push_mod._TRUTHY) == expected


def test_cooldown_constant_is_unchanged() -> None:
    """推送冷却与 #205/#206 正交, 本派零改。"""
    assert C.PUSH_DEDUPE_COOLDOWN_MIN == 30


def test_no_source_file_mentions_switching_env_on() -> None:
    """源码里不许出现"把它改成 1 / true"这类把闸默认打开的写法。

    正常用法是 `os.environ.get(PUSH_ENV_NAME)` —— 只读; 出现
    `os.environ[...] = ...` 或 `putenv` 才是改环境, 那种事一个库都不该做。
    """
    offenders: list[str] = []
    for path in _sources():
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    # os.environ["X"] = "1"
                    if isinstance(tgt, ast.Subscript) and _looks_like_environ(tgt.value):
                        offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                writes_env = node.func.attr in ("putenv", "setdefault") and _looks_like_environ(
                    node.func.value
                )
                if writes_env:
                    offenders.append(f"{path.name}:{node.lineno} {node.func.attr}")
    assert not offenders, f"源码在写环境变量(本派禁止): {offenders}"


def _looks_like_environ(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" or (
            isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr == "environ"
        )
    return False


# ------------------------------------------------------- 无「快照 pe → v_score」


def test_valuation_module_never_reads_a_pe_column() -> None:
    """估值桩既不读表也不联网 —— 结构上不可能拿到 pe。

    扫描对象是**去掉所有字符串字面量之后**的 AST dump。直接用文本扫会把
    `VALUATION_SNAPSHOT_PE_FORBIDDEN` 的 docstring 命中 —— 那段正是**要**提到
    `pe_ttm` 才能把禁令讲清楚的地方。按三引号切分也不行: 模块里有多段 docstring,
    切出来的可能正好是中间那段。
    """
    body = _code_without_strings(PKG / "valuation_stub.py")
    for banned in ("pe_ttm", "financials_metrics", "read_parquet", "scan_parquet", "urlopen"):
        assert banned not in body, f"valuation_stub 执行代码里出现 {banned}"


def test_valuation_module_does_not_import_picker() -> None:
    mods = _imported_modules(PKG / "valuation_stub.py")
    assert not any("picker" in m or "selection" in m for m in mods), mods


def test_no_v_score_is_ever_assigned_a_number() -> None:
    """全包扫: 没有任何一处把 `v_score` 赋成数字。

    当前唯一出现位置是 dataclass 字段声明的默认值 `None`, 由 AST 里
    `AnnAssign` 的常量值判定 —— 只要不是 `None` 就说明有人在实装打分。
    """
    offenders: list[str] = []
    for path in _sources():
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.AnnAssign):
                continue
            if not (isinstance(node.target, ast.Name) and node.target.id == "v_score"):
                continue
            default = node.value
            is_none = isinstance(default, ast.Constant) and default.value is None
            if not is_none:
                offenders.append(f"{path.name}:{node.lineno} v_score 默认值不是 None")
        # 运行期赋值 (obj.v_score = x / v_score = x)
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Assign):
                names: set[str] = set()
                for tgt in node.targets:
                    names |= _target_names(tgt)
                if "v_score" in names:
                    offenders.append(f"{path.name}:{node.lineno} 对 v_score 赋值")
    assert not offenders, offenders


def test_valuation_score_emitted_is_false_for_stub() -> None:
    stub = vs.valuation_asof("600000.SH", __import__("datetime").date(2024, 11, 1))
    assert stub.score_emitted is False


def test_snapshot_pe_forbidden_flag_is_a_constant_true() -> None:
    """这个常量没有开关作用 —— 改成 False 等于取消禁令。"""
    assert vs.VALUATION_SNAPSHOT_PE_FORBIDDEN is True
    assert "pe" in vs.VALUATION_SNAPSHOT_SOURCE_PREFIXES


# ------------------------------------------------- 健康: hit 恒 None, 不进权重


def test_no_health_veto_hit_is_assigned_false() -> None:
    """`health_veto_hit=False` 是"查过没命中"的结论; 本派没有规则, 不能给结论。"""
    offenders: list[str] = []
    for path in _sources():
        for node in ast.walk(_tree(path)):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            names: set[str] = set()
            for tgt in targets:
                names |= _target_names(tgt)
            if "health_veto_hit" not in names:
                continue
            value = node.value
            if isinstance(value, ast.Constant) and value.value is False:
                offenders.append(f"{path.name}:{node.lineno} health_veto_hit=False")
    assert not offenders, offenders


def test_no_w_health_anywhere() -> None:
    """健康是 veto, 不是加权项。出现 `w_health` 就说明有人在把它摊进权重。

    扫的是**去掉字符串之后的代码**。`quality_proxy` 的文档里有一句"没有
    `w_health` 这个字段, 也不允许出现" —— 那句话必须能写, 否则禁令没有落点。
    查文本会把它自己判成违规。
    """
    offenders: list[str] = []
    for path in _sources():
        if "w_health" in _code_without_strings(path):
            offenders.append(path.name)
        if "w_health" in _assigned_targets(path):
            offenders.append(f"{path.name}: 赋值")
    assert not offenders, f"出现 w_health: {offenders}"


def test_health_stub_never_reports_passed() -> None:
    """`passed` 必须恒 False, 且不能用 `not hit` 实现(那会把 None 读成通过)。"""
    import datetime as dt

    stub = hv.health_veto_asof("600000.SH", dt.date(2024, 11, 1))
    assert stub.health_veto_hit is None
    assert stub.passed is False
    assert stub.reason_codes == ()


def test_forbidden_pass_code_is_not_in_catalog() -> None:
    """`data_insufficient_treat_as_pass` 只能出现在"禁止"集合里, 不能进草稿目录。"""
    assert "data_insufficient_treat_as_pass" not in hv.HEALTH_VETO_REASON_CATALOG_DRAFT
    assert "data_insufficient_treat_as_pass" in hv.HEALTH_VETO_FORBIDDEN_PASS_CODES


def test_reason_catalog_matches_phase2_section() -> None:
    """PHASE2 §3.3 的五码, 一个不多一个不少。"""
    assert set(hv.HEALTH_VETO_REASON_CATALOG_DRAFT) == {
        "audit_opinion_bad",
        "leverage_stress",
        "cashflow_stress",
        "regulatory_risk",
        "other_rule",
    }


def test_health_rule_version_stays_design_pending() -> None:
    assert hv.HEALTH_RULE_VERSION_PENDING == "design_pending"
    assert hv.HEALTH_REASON_CATALOG_STATUS == "draft"


# --------------------------------------------------------------- 权重仍 pending


def test_weights_stay_none_and_pending() -> None:
    proxy = qp.QualityProxy(symbol="600000.SH", asof=__import__("datetime").date(2024, 11, 1))
    assert proxy.w_quality is None
    assert proxy.w_value is None
    assert proxy.weighting_status == "design_pending"
    assert qp.QUALITY_WEIGHT_STATUS == "design_pending"


def test_quality_metric_columns_are_not_extended() -> None:
    """本派不新造因子: 七列名单原样。"""
    assert qp.QUALITY_METRIC_COLUMNS == (
        "roe",
        "gross_margin",
        "debt_to_asset_ratio",
        "net_income_yoy",
        "revenue_yoy",
        "eps",
        "bps",
    )


def test_quality_missing_reason_codes_cover_documented_set() -> None:
    """文档表里列的码 == 常量里的码。加码不改表, 表就会骗人。"""
    expected_bare = {
        "no_financials_known_at_asof",
        "no_kline_source",
        "no_kline_rows",
    }
    expected_prefixes = {
        "missing_column:",
        "null_metric:",
        "bad_metric:",
        "kline_coverage_low:",
    }
    assert expected_bare == qp.QUALITY_MISSING_REASON_CODES
    assert expected_prefixes == set(qp.QUALITY_MISSING_REASON_PREFIXES)


# --------------------------------------------------------------- 白名单未锁


def test_whitelist_is_still_unlocked() -> None:
    assert qr.WHITELIST_PERCENTILE_TRIAL == "p40"
    assert qr.WHITELIST_TRIAL_QUANTILE == 0.60
    assert qr.WHITELIST_PERCENTILE_LOCKED is None
    assert qr.WHITELIST_THRESHOLD_LOCKED is False


def test_no_source_can_lock_the_whitelist() -> None:
    """除了常量定义, 没有任何地方给 `WHITELIST_PERCENTILE_LOCKED` 赋值。"""
    offenders: list[str] = []
    for path in _sources():
        for node in ast.walk(_tree(path)):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names: set[str] = set()
            for tgt in targets:
                names |= _target_names(tgt)
            if "WHITELIST_PERCENTILE_LOCKED" in names:
                value = node.value
                if not (isinstance(value, ast.Constant) and value.value is None):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


# ----------------------------------------------------- 无竞价 / 无提醒层耦合


def test_no_auction_impersonation_code() -> None:
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for banned in ("auction_backtest", "pre_open_1m", "open_1m_as_auction", "as_auction"):
            assert banned not in text, f"{path.name} 出现 {banned}"


def test_auction_bucket_constant_untouched() -> None:
    """竞价桶名字写死, 本派不碰 —— 尤其不碰"用 09:30 冒充 09:25"那条线。"""
    assert C.AUCTION_STAT_BUCKET_0925 == "auction_0925"


def test_no_reminder_layer_imports() -> None:
    """禁 import `trade_marks` / `news.push` 的 BAND 与冷却常量。

    `push.py` 允许调 `app.news.dingtalk` 的**发送函数**(gding), 但仍不许引
    `app.news.push` 的业务逻辑。
    """
    banned_modules = ("app.services.trade_marks", "trade_marks", "app.news.push")
    offenders: dict[str, set[str]] = {}
    for path in _sources():
        mods = _imported_modules(path)
        hit = {m for m in mods if m in banned_modules}
        if hit:
            offenders[path.name] = hit
    assert not offenders, offenders


def test_no_band_or_cooldown_constant_imports() -> None:
    banned_names = ("VWAP_BAND", "RANGE_BAND", "T_COOLDOWN_MIN")
    offenders: list[str] = []
    for path in _sources():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in banned_names:
                        offenders.append(f"{path.name}: {alias.name}")
    assert not offenders, offenders


def test_no_t_rounds_coupling() -> None:
    """与 #207 hfeat(`t_rounds` 离线)正交: 本包不 import 它的门禁常量。"""
    offenders: list[str] = []
    for path in _sources():
        mods = _imported_modules(path)
        hit = {m for m in mods if "t_rounds" in m}
        if hit:
            offenders.append(f"{path.name}: {sorted(hit)}")
        if "EPSILON_VALUE_PCT" in path.read_text(encoding="utf-8"):
            offenders.append(f"{path.name}: 出现 EPSILON_VALUE_PCT")
    assert not offenders, offenders


# ---------------------------------------------------------------- 文案红线


CLAIM_PATTERNS = (
    "估值已测通",
    "健康门禁已验证",
    "白名单已锁定",
    "正式可推",
)


def test_sources_make_no_tested_or_locked_claims() -> None:
    offenders: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for claim in CLAIM_PATTERNS:
            if claim in text:
                offenders.append(f"{path.name}: {claim}")
    assert not offenders, offenders


@pytest.mark.parametrize("filename", ["valuation_stub.py", "health_veto_stub.py", "quality_proxy.py"])
def test_module_docstrings_carry_the_disclaimer(filename: str) -> None:
    text = (PKG / filename).read_text(encoding="utf-8")
    assert "仅供研究，不构成投资建议；执行须人工确认" in text, filename


def test_health_docstring_explains_if_hit_semantics() -> None:
    """软提醒第 3 条: `hit=None` 被 `if hit:` 当假值 ⇒ "不阻止", 不是"已通过"。"""
    text = (PKG / "health_veto_stub.py").read_text(encoding="utf-8")
    assert "if hit:" in text
    assert "不阻止" in text
    assert "is False" in text


def test_docs_carry_gval_section() -> None:
    doc = BACKEND.parent / "docs" / "buy-sell-signals-skeleton.md"
    text = doc.read_text(encoding="utf-8")
    assert "gval" in text
    assert "data_insufficient_treat_as_pass" in text
    assert "if hit:" in text


def test_no_secret_looking_literals_in_new_modules() -> None:
    """任务书明令: 密码 / 密钥不进仓。"""
    pattern = re.compile(r"(password|secret|token|api[_-]?key)\s*=\s*[\"'][^\"']{8,}", re.I)
    offenders: list[str] = []
    for path in _sources():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{line_no}")
    assert not offenders, offenders
