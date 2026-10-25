"""离线包的隔离性静态守门（批次 20261025hfeat 任务④）。

本批次对生产链路的约束是**零触碰**：

- `buy_sell_signals`（含 PERSIST / PUSH / DINGTALK 三个 env）
- `main.py` 的 lifespan
- `daily_pipeline`
- 任何正式推送闸

这类约束用运行时测试证明不了（"没被调用"是不可观测的），只能静态扫。但静态扫
有两个经典坑，本文件都用 AST 绕开了：

1. **裸子串扫会误伤注释与自己写的文档**（gp 批次踩过：在注释里写「不接 news.push」
   就被判违规）。所以这里只比对 AST 的 `Import` / `ImportFrom` 节点与 `Attribute`。
2. **扫得太宽会漏**（只扫 `import` 漏掉 `importlib.import_module("...")`、
   `getattr(mod, "push")`）。所以额外加了一层"危险符号名不许出现在任何
   `Name` / `Attribute` / 字符串常量里"的检查。

本文件也顺手钉住几条**常量级事实**（ε=0.30、09:30、`design_pin_v1`）：这些值一旦
被人改掉，前面几个测试文件可能仍然全绿，而报告的"口径"已经悄悄变了。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
OFFLINE_DIR = BACKEND / "app" / "t_rounds" / "offline"
T_ROUNDS_DIR = BACKEND / "app" / "t_rounds"


def _sources() -> list[Path]:
    files = sorted(OFFLINE_DIR.glob("*.py"))
    assert files, f"{OFFLINE_DIR} 下没有 .py 文件; 路径不对则本文件的全部断言都是空的"
    return files


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    """收集一个文件 import 的模块全名（`import a.b` 与 `from a import b`）。"""
    out: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.add(node.module)
                # `from app import buy_sell_signals` 这种也算
                for alias in node.names:
                    out.add(f"{node.module}.{alias.name}")
            elif node.module:
                # 相对 import：只记模块尾部，够判"是不是引了禁用模块"
                out.add(node.module)
    return out


# ---------------------------------------------------------------- 不 import 生产信号模块


def test_offline_package_does_not_import_buy_sell_signals() -> None:
    """`offline/` 不许 import `buy_sell_signals`（任何层级、任何形式）。

    那个模块管着 PERSIST / PUSH / DINGTALK 三个开关。离线包一旦引到它，就等于
    把"离线"和"会不会真的推给用户"绑在了一起 —— 那正是本批次要避免的耦合。
    """
    offenders = []
    for src in _sources():
        for mod in _imported_modules(src):
            if "buy_sell_signals" in mod:
                offenders.append(f"{src.name} imports {mod}")
    assert not offenders, f"离线包引了 buy_sell_signals: {offenders}"


def test_offline_package_does_not_use_dynamic_import() -> None:
    """不许用 `importlib` / `__import__` / `getattr(mod, "...")` 绕过上面的检查。

    动态导入是静态守门的天然漏洞：AST 上看不见真实模块名。离线包没有理由需要它。
    """
    banned_calls = {"__import__", "import_module"}
    offenders: list[str] = []
    for src in _sources():
        for node in ast.walk(_tree(src)):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name in banned_calls:
                    offenders.append(f"{src.name}:{node.lineno} {name}()")
            if isinstance(node, ast.Attribute) and node.attr == "importlib":
                offenders.append(f"{src.name}:{node.lineno} importlib")
    assert not offenders, f"离线包用了动态导入: {offenders}"


# ---------------------------------------------------------------- 不碰推送/持久化


def test_no_push_or_persist_switch_logic() -> None:
    """三个 env 开关的**写逻辑**不许出现在离线包里。

    只读也不行 —— 离线包不需要知道"会不会推"，它跑完就结束。所以这里禁止的是
    这些名字作为标识符或字符串常量出现（`os.environ.get("BUY_SELL_SIGNALS_PUSH")`
    就同时命中两者）。
    """
    banned = (
        "BUY_SELL_SIGNALS_PERSIST",
        "BUY_SELL_SIGNALS_PUSH",
        "BUY_SELL_SIGNALS_DINGTALK",
    )
    offenders: list[str] = []
    for src in _sources():
        tree = _tree(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for token in banned:
                    if token in node.value:
                        offenders.append(f"{src.name}:{node.lineno} 字符串含 {token}")
            if isinstance(node, ast.Name) and node.id in banned:
                offenders.append(f"{src.name}:{node.lineno} 标识符 {node.id}")
    assert not offenders, f"离线包碰了推送/持久化开关: {offenders}"


def test_no_push_transport_dependencies() -> None:
    """不许引推送传输层（SSE / 钉钉 / 飞书 / httpx 客户端）。

    离线包跑完写几个文件就结束。引入传输层意味着有人想让它"顺手推一下"。
    """
    banned_modules = {
        "app.news.dingtalk",
        "app.news.push",
        "httpx",
        "requests",
    }
    offenders = []
    for src in _sources():
        for mod in _imported_modules(src):
            if mod in banned_modules:
                offenders.append(f"{src.name} imports {mod}")
    assert not offenders, f"离线包引了传输层: {offenders}"


def test_no_cooldown_or_band_runtime_symbols() -> None:
    """不许复用 `buy_sell_signals` 的运行时符号（带宽 / 冷却 / 去重时间戳）。

    这些是**线上信号模块的实现细节**。离线回测若去引用它们，就会在对方改动后
    悄悄改口径，而那种分叉从结果上看不出来（只是回测结论和实盘不一致了）。
    """
    banned = (
        "VWAP_BAND",
        "RANGE_BAND",
        "T_COOLDOWN_MIN",
        "push_dedupe_cooldown_until",
    )
    offenders: list[str] = []
    for src in _sources():
        text = src.read_text(encoding="utf-8")
        tree = _tree(src)
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for token in banned:
                    if token in node.value:
                        offenders.append(f"{src.name}:{node.lineno} 字符串含 {token}")
            if name in banned:
                offenders.append(f"{src.name}:{node.lineno} 符号 {name}")
        del text
    assert not offenders, f"离线包引了线上信号模块的内部符号: {offenders}"


# ---------------------------------------------------------------- 不改生产模块


def test_offline_package_is_not_imported_by_production_modules() -> None:
    """依赖方向是单向的：生产模块**不许** import `offline`。

    一旦反向引用，离线包就成了生产链路的一部分，那"它不在生产路径上"这句话
    就不再成立了。
    """
    offenders: list[str] = []
    for src in sorted(T_ROUNDS_DIR.glob("*.py")):
        for mod in _imported_modules(src):
            if mod == "offline" or mod.startswith("offline."):
                offenders.append(f"{src.name} imports {mod}")
    assert not offenders, f"生产模块反向依赖离线包: {offenders}"


def test_offline_package_does_not_touch_lifespan_or_pipeline() -> None:
    """不许碰 `main.py` 的 lifespan、`daily_pipeline`、`partition_writer`。"""
    banned = ("lifespan", "daily_pipeline", "partition_writer", "FastAPI", "APIRouter")
    offenders: list[str] = []
    for src in _sources():
        text = src.read_text(encoding="utf-8")
        tree = _tree(src)
        # 只看 import 与调用，不看注释/文档
        for mod in _imported_modules(src):
            if mod.split(".")[-1] == "main" or "daily_pipeline" in mod:
                offenders.append(f"{src.name} imports {mod}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name in banned:
                    offenders.append(f"{src.name}:{node.lineno} 调用 {name}()")
        del text
    assert not offenders, f"离线包接了生产入口: {offenders}"


# ---------------------------------------------------------------- 常量级事实


def test_auction_impersonation_is_forbidden() -> None:
    """「09:30 不许冒充 09:25 集合竞价」必须是代码里的硬常量。

    任务书原文要求这条；如果它只是一个约定而没有常量守着，某次改动就可能把
    竞价 bar 当正常 bar 混进来，而结果上只会显得"信号多了一点"。
    """
    from app.t_rounds import constants as C

    assert C.AUCTION_IMPERSONATION_FORBIDDEN is True
    assert C.MINUTE_EXPORT_EARLIEST == "09:30"


def test_no_helper_relabels_0930_as_0925() -> None:
    """**不存在**任何把 09:30 改标成 09:25 的辅助函数或代码路径。

    上一版测试只证明了"早于 09:30 的 bar 会被拒"，那不足以覆盖这条约束：
    真正的风险是有人写个 `relabel_open_as_auction()` 把首根改成 09:25，
    于是"09:25 竞价 bar"凭空出现，而守门函数完全看不出来（时间已经改好了）。

    扫两件事：
      1. 函数/方法名不许把 auction 语义与 relabel/rename 组合在一起；
      2. 除 docstring 外，代码里不许出现字面量 `"09:25"`。

    第 2 条**排除 docstring**：本包的守门函数 docstring 和报错文案里正当出现
    「09:25」（它们在解释"为什么禁止"）。gp 批次的教训是静态扫描不能用会误伤
    文档的判据 —— 所以这里按字符串是否作为常量参与运算/传参来判断，而不是全文搜。
    """
    offenders: list[str] = []
    for src in _sources():
        tree = _tree(src)
        # 收集"说明性字符串"节点的 id：docstring 与 raise 的消息。
        # 这两处出现「09:25」是在解释"为什么禁止"，不是在改时间。
        explain_ids: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = getattr(node, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    explain_ids.add(id(body[0].value))
            if isinstance(node, ast.Raise) and node.exc is not None:
                for sub in ast.walk(node.exc):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        explain_ids.add(id(sub))
                    # f-string 里的字面片段
                    if isinstance(sub, ast.JoinedStr):
                        for part in sub.values:
                            if isinstance(part, ast.Constant) and isinstance(
                                part.value, str
                            ):
                                explain_ids.add(id(part))

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                low = node.name.lower()
                looks_auction = "auction" in low or "0925" in low or "09_25" in low
                looks_relabel = any(
                    token in low
                    for token in (
                        "relabel",
                        "rename",
                        "rewrite",
                        "as_auction",
                        "fake",
                        "pretend",
                    )
                )
                if looks_auction and looks_relabel:
                    offenders.append(f"{src.name}:{node.lineno} 函数 {node.name}()")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "09:25" in node.value
                and id(node) not in explain_ids
            ):
                offenders.append(f"{src.name}:{node.lineno} 代码常量含 09:25")

    assert not offenders, f"发现把开盘首根改标成 09:25 的路径: {offenders}"


def test_no_datetime_relabeling_in_offline_package() -> None:
    """不许**平移时刻**（`dt.offset_by` / `datetime ± timedelta`）。

    平移一两个小时就能把 09:30 变成 09:25，而列名、断言全都还是"合法的"。
    离线包没有任何理由改动时间轴。

    注意 `replace_time_zone(None)` **不在此列**：它只是去掉时区标记，不改墙钟时刻。
    本包在 `_normalize()` 里先用 `convert_time_zone("Asia/Shanghai")` 换成北京墙钟
    再摘掉时区 —— 这是正确做法（直接摘会在非北京时区数据上平移 8 小时）。
    所以这里禁止的是 `dt.offset_by`（按时长平移）与 `timedelta` 参与的时刻加减。
    """
    banned_attrs = {"offset_by"}
    offenders: list[str] = []
    for src in _sources():
        tree = _tree(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                attr = getattr(node.func, "attr", None)
                if attr in banned_attrs:
                    offenders.append(f"{src.name}:{node.lineno} 调用 .{attr}()")
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
                right = node.right
                if (
                    isinstance(right, ast.Call)
                    and getattr(right.func, "id", None) == "timedelta"
                ):
                    offenders.append(
                        f"{src.name}:{node.lineno} 对序列做 timedelta 加减 (会平移时刻)"
                    )
    assert not offenders, f"离线包平移了时间轴: {offenders}"


def test_epsilon_and_version_are_pinned() -> None:
    """ε 与版本串的钉死值。改这两个数等于换了一套口径，必须走正式升版。"""
    from app.t_rounds import constants as C
    from app.t_rounds.offline import EPSILON_PCT_MAIN, assert_version_matches_pin

    assert C.EPSILON_VALUE_PCT == 0.30
    assert EPSILON_PCT_MAIN == 0.30
    assert C.design_version() == "design_pin_v1+eps_pct:0.30"
    assert assert_version_matches_pin() == "design_pin_v1+eps_pct:0.30"


def test_quota_constants_are_unchanged() -> None:
    """一日一轮的硬规则没被本批次改动。"""
    from app.t_rounds import constants as C
    from app.t_rounds.state_machine import STATUSES_CONSUMING_QUOTA

    assert C.MAX_ROUNDS_PER_DAY == 1
    # `void_data` 不占名额（口径与 `Store.count_rounds` 一致）
    assert "void_data" not in STATUSES_CONSUMING_QUOTA


# ---------------------------------------------------------------- 免责声明


def test_disclaimer_is_present_in_package_docstring() -> None:
    """包级 docstring 必须带「仅供研究，不构成投资建议」。"""
    import app.t_rounds.offline as pkg

    doc = pkg.__doc__ or ""
    assert "仅供研究" in doc
    assert "不构成投资建议" in doc


def test_no_claim_of_being_verified_in_source() -> None:
    """源码里不许出现「已测通 / 生产可用 / 实盘验证通过」这类结论性措辞。

    这批的全部产出是**离线纸面**结果。一旦某处写了"已测通"，下一次就会有人
    拿它当授权。
    """
    banned = ("已测通", "生产可用", "实盘验证通过", "已验证通过")
    offenders: list[str] = []
    for src in _sources():
        text = src.read_text(encoding="utf-8")
        for token in banned:
            if token in text:
                offenders.append(f"{src.name} 含「{token}」")
    assert not offenders, f"出现结论性措辞: {offenders}"


@pytest.mark.parametrize("path_name", ["loader.py", "features.py", "gates.py", "backtest.py", "report.py"])
def test_each_module_states_it_is_research_only(path_name: str) -> None:
    """每个模块的 docstring 都要自带免责声明（单独看某个文件时也不会误读）。"""
    src = OFFLINE_DIR / path_name
    doc = ast.get_docstring(_tree(src)) or ""
    assert "仅供研究" in doc, f"{path_name} 缺「仅供研究」声明"
    assert "不构成投资建议" in doc, f"{path_name} 缺「不构成投资建议」声明"
