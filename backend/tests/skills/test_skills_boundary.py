"""Agent Skills 边界门禁（批次 20261025gskills · -A-#3 P2 · 只读 v1）。

**仅供研究，不构成投资建议；执行须人工确认。**

## 这个文件守什么

`skills/` 是**分发面**：一旦装进 AI 客户端，宿主会按 `SKILL.md` 的说法去调工具。
所以「清单里写了什么」等价于「允许 AI 去做什么」。本文件把它当**安全边界**来测：

| 守的东西 | 失败后果（如果不守） |
|---|---|
| 清单存在且可解析 | 清单没了 / 格式歪了 → 后面的断言全部无从谈起（假绿温床） |
| MCP 12 工具全覆盖 | 新增一个写工具却没登记 → 它能被调用，但没人审计过 |
| 无交易工具名 | 清单里混进下单类能力 → 直接违反「禁交易」 |
| 写工具必带确认门 | `write_ext_rows` 没标注确认门 → AI 会在没人同意时写进策略数据面 |

## 为什么红测用「合成临时夹具」而不是改真文件

改真文件再还原，是**测一次就污染一次**的做法：中途 panic / 并发 / 编辑器自动保存都会让仓库
停在坏状态，而 CI 恰好那时跑就会红（或者更糟：改坏了没还原，人以为测试过了）。

所以负例全部在 `tmp_path` 里造一棵**最小假 skills 树**，喂给同一个扫描函数。这样「真骨架绿」
和「注入坏输入红」用的是**同一段判定代码** —— 如果扫描函数写坏了，正例那条会先红。

## 为什么禁 skip / xfail

被跳过的边界测等于没有边界测。`test_zz_no_silent_skip` 扫本模块源码，禁止三类「静默跳过」
写法（`mark.skip` / `mark.xfail` / `skip(` 的 pytest 形式，具体字面量在函数内拆分书写，
免得守卫扫到自己的说明文字）；`test_zz_wired_into_ci` 断言 CI 真的跑 `pytest tests`。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
"""仓库根。`backend/tests/skills/test_*.py` → `parents[3]`。

注意不能用 `parents[2]`：那是 `backend/`。写错的话所有路径断言都会指向一个不存在的
`backend/skills/`，而 pytest 会以 `FileNotFoundError` 报错 —— 看起来像「夹具没找到」，
实际是路径算错。
"""

SKILLS_DIR = REPO_ROOT / "skills"
MANIFEST = SKILLS_DIR / "MANIFEST.md"
BRIEF = REPO_ROOT / "docs" / "agent-skills-brief.md"

# ── 交易工具名黑名单（任务书 §「交易工具名黑名单」最小集，不得收窄） ──────────────
#
# 匹配规则：**整词边界 + 大小写不敏感**。
# 为什么不用子串：`broker` 是普通英文词，出现在 `brokerage fee`（券商佣金，成本讨论里很
# 正常）里时，子串匹配会把一句正常的成本说明判成交易能力 —— 那样守门人会被逼着删掉正确
# 的内容。所以必须带边界：`place_order` 这种带下划线的名字用 `\b` 两侧即可（`_` 是 `\w`，
# 所以 `xplace_order` 不会被误命中）。
TRADE_NAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bplace_order\b",
        r"\bpaper[:._-]trade\b",
        r"\bbroker\b",
        r"\bsubmit_order\b",
        r"\bcreate_order\b",
        r"\bsend_order\b",
        r"\blive_trade\b",
        r"实盘下单",
        r"市价买入",
    )
)

# 负向语境词：黑名单词出现在**禁令句**里是正常的（「禁止下单」这类护栏句必须允许存在）。
#
# 这个设计是有代价的：它挡不住「绕开写」。但反过来 —— 如果禁止黑名单词出现在任何地方，
# 第一个被删掉的就是「本包不提供 place_order」这句护栏本身。守门规则把人逼着删护栏，
# 是比漏检更坏的结局。
NEGATION_CUES: tuple[str, ...] = (
    "禁止", "不得", "不应", "不许", "勿", "没有", "不含", "不提供", "不做", "无",
    "禁", "不",
)

# MCP 12 工具：以 `mcp-server/src/tsp_mcp_server/server.py` 的 `@mcp.tool()` 为准。
# 这里**写死**而不是运行时 import：import 会拉起 httpx / mcp 依赖，且 server.py 在
# import 期就会读环境变量 —— 测试不该因为环境没配而红（那是环境问题，不是边界问题）。
EXPECTED_MCP_TOOLS: tuple[str, ...] = (
    "search_instrument",
    "get_kline",
    "get_index_kline",
    "get_market_overview",
    "get_regime_state",
    "list_strategies",
    "get_strategy_detail",
    "get_recent_alerts",
    "list_ext_tables",
    "query_ext_table",
    "write_ext_rows",
    "run_backtest",
)

WRITE_TOOLS: tuple[str, ...] = ("write_ext_rows",)

# 「空喊」禁词（任务书:禁「信号可做 / 可交易 / 已可下单 / tip 已有官方 skill 包」）
BANNED_CLAIMS: tuple[str, ...] = (
    "信号可做",
    "可交易",
    "已可下单",
    "可以下单",
    "支持下单",
    "已经实盘",
    "已实盘",
    "tip 已有官方 skill 包",
    "tip 上已有官方 skill 包",
)


# ============================================================ 扫描实现（被正例与负例共用）


class SkillsBoundaryError(AssertionError):
    """边界被打破。用 `AssertionError` 的子类：pytest 会把它呈现成断言失败而不是错误。"""


def _rel(path: Path) -> str:
    """尽量给相对路径（日志好读）；临时目录不在仓库下时退回绝对路径。

    直接用 `path.relative_to(REPO_ROOT)` 会在负例（`tmp_path`）上抛 `ValueError` ——
    而那个 ValueError 会被当成「测试写错了」，掩盖真正的边界判定。
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _parse_frontmatter(text: str, *, where: str) -> dict[str, Any]:
    """极简 YAML frontmatter 解析，只支持本仓用到的子集。

    不用 `pyyaml`：`backend/tests` 里已有大量用例可以在没有业务依赖的情况下跑，边界测再多
    引一个依赖没有收益；而这里需要的语法只有「顶层 key: 值」与「两空格缩进的嵌套 key: 值」、
    「`[a, b]` 行内列表」三种。
    """
    if not text.startswith("---"):
        raise SkillsBoundaryError(f"{where} 缺 frontmatter（文件须以 `---` 开头）")
    end = text.find("\n---", 3)
    if end < 0:
        raise SkillsBoundaryError(f"{where} 的 frontmatter 没有闭合 `---`")
    block = text[3:end].strip("\n")

    data: dict[str, Any] = {}
    current: str | None = None
    block_scalar: list[str] | None = None  # 正在收集 `>-` / `|` 的多行内容

    for raw in block.splitlines():
        # 多行块标量：缩进行全部归给它，直到遇到顶格的新 key
        if block_scalar is not None:
            if not raw.strip():
                continue
            if raw.startswith(("  ", "\t")):
                block_scalar.append(raw.strip())
                continue
            data[current] = " ".join(block_scalar)
            block_scalar = None

        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith(("  ", "\t")) and current:
            key, _, value = raw.strip().partition(":")
            container = data.setdefault(current, {})
            if isinstance(container, dict):
                container[key.strip()] = _coerce(value.strip())
            continue
        key, _, value = raw.partition(":")
        current = key.strip()
        data[current] = _coerce(value.strip())
        if value.strip() in (">-", ">", "|", "|-"):
            block_scalar = []

    if block_scalar is not None and current:
        data[current] = " ".join(block_scalar)
    return data


def _coerce(value: str) -> Any:
    # `>-` / `|` 只是块标量标记，真正内容由 `_parse_frontmatter` 逐行收集后填回。
    if value in (">-", ">", "|", "|-"):
        return ""
    if value == "":
        return {}
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value.strip("'\"")


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    """读清单并抽出机读部分。清单缺失/为空 → **抛错**（fail-closed，不当通过）。"""
    if not path.exists():
        raise SkillsBoundaryError(
            f"技能清单不存在: {path}。缺清单不能当作「没有问题」—— 那正是假绿的定义。"
        )
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise SkillsBoundaryError(f"技能清单是空文件: {path}")

    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        if set(cells[0]) <= {"-", ":", " "} or cells[0] in ("#", "MCP 工具"):
            continue
        if not cells[0].isdigit():  # 第一列是序号 → 这是映射表行
            continue
        rows.append(
            {
                "tool": cells[1].strip("`"),
                "skill": cells[2],
                "scope": cells[3].strip("`"),
                "api": cells[4].strip("`"),
                "rw": cells[5],
                "confirm": cells[6] if len(cells) > 6 else "",
            }
        )
    return {"path": path, "text": text, "rows": rows}


def scan_trade_names(text: str, *, where: str) -> list[str]:
    """扫交易工具名，返回命中清单（空 = 通过）。负向语境（禁令句）不计。"""
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pat in TRADE_NAME_PATTERNS:
            for m in pat.finditer(line):
                # 前后各取 40 字符：中文的否定常在**后面**（「禁止…信号可做」），
                # 只看前文会把「禁止把输出写成信号可做」判成空喊。
                ctx = line[max(0, m.start() - 40): m.end() + 40]
                if any(cue in ctx for cue in NEGATION_CUES):
                    continue
                hits.append(f"{where}:{lineno}: {m.group(0)!r} ← …{ctx.strip()}…")
    return hits


def scan_banned_claims(text: str, *, where: str) -> list[str]:
    """扫「信号可做 / 可交易」这类空喊。同样放过否定语境。"""
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for word in BANNED_CLAIMS:
            idx = line.find(word)
            while idx >= 0:
                ctx = line[max(0, idx - 40): idx + len(word) + 40]
                if not any(cue in ctx for cue in NEGATION_CUES):
                    hits.append(f"{where}:{lineno}: {word!r} ← …{ctx.strip()}…")
                idx = line.find(word, idx + len(word))
    return hits


def load_skill_docs(root: Path = SKILLS_DIR) -> dict[str, tuple[Path, dict[str, Any], str]]:
    """读所有 `tsp-*/SKILL.md`，返回 {name: (path, frontmatter, 正文)}。空树 → 抛错。"""
    if not root.exists():
        raise SkillsBoundaryError(f"技能目录不存在: {root}")
    docs: dict[str, tuple[Path, dict[str, Any], str]] = {}
    files = sorted(root.glob("tsp-*/SKILL.md"))
    if not files:
        raise SkillsBoundaryError(
            f"{root} 下没有任何 tsp-*/SKILL.md。空技能树不是「通过」—— fail-closed。"
        )
    for path in files:
        text = path.read_text(encoding="utf-8")
        fm = _parse_frontmatter(text, where=_rel(path))
        name = fm.get("name")
        if not isinstance(name, str) or not name:
            raise SkillsBoundaryError(f"{path} 的 frontmatter 缺 `name`")
        docs[name] = (path, fm, text)
    return docs


def check_boundaries(root: Path = SKILLS_DIR, manifest: Path = MANIFEST) -> None:
    """一口气跑完全部边界断言。任何一条不满足 → 抛 `SkillsBoundaryError`。"""
    man = load_manifest(manifest)
    docs = load_skill_docs(root)

    # 1) 三面齐（少一面说明分发面残缺，但不是 fail-closed 的理由 —— 至少要有 3 面）
    if len(docs) < 3:
        raise SkillsBoundaryError(f"技能面不足 3 个: {sorted(docs)}")

    # 2) 12 工具全覆盖
    listed = {r["tool"] for r in man["rows"]}
    missing = set(EXPECTED_MCP_TOOLS) - listed
    if missing:
        raise SkillsBoundaryError(
            f"映射表漏掉 MCP 工具: {sorted(missing)}。"
            "漏掉一个写工具 = 它能被调用却没人审计过。"
        )

    # 3) 每一行的技能面必须真的存在
    for r in man["rows"]:
        for face in r["skill"].replace("`", "").split("·"):
            face = face.strip()
            if face and not face.startswith("—") and face not in docs:
                raise SkillsBoundaryError(
                    f"映射表把 {r['tool']} 挂在 {face!r}，但 skills/{face}/SKILL.md 不存在"
                )

    # 4) 交易名 / 空喊：清单 + 三面正文
    surfaces = [(man["path"], man["text"])] + [
        (p, t) for (p, _fm, t) in docs.values()
    ]
    trade_hits: list[str] = []
    claim_hits: list[str] = []
    for path, text in surfaces:
        rel = _rel(path)
        trade_hits += scan_trade_names(text, where=rel)
        claim_hits += scan_banned_claims(text, where=rel)
    if trade_hits:
        raise SkillsBoundaryError("出现交易工具名:\n" + "\n".join(trade_hits))
    if claim_hits:
        raise SkillsBoundaryError("出现空喊:\n" + "\n".join(claim_hits))

    # 5) 写工具必须带确认门（清单列 + frontmatter 双标）
    for tool in WRITE_TOOLS:
        row = next((r for r in man["rows"] if r["tool"] == tool), None)
        if row is None:
            continue  # 已由 (2) 覆盖：要么在、要么整体缺
        if "确认" not in row["confirm"]:
            raise SkillsBoundaryError(
                f"清单里 {tool} 的「确认门」列没写确认门。写工具无确认门 = 可被静默调用。"
            )
        owners = [f.strip() for f in row["skill"].replace("`", "").split("·")]
        for face in owners:
            if not face:
                continue
            _p, fm, _t = docs[face]
            meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
            if meta.get("write_requires_confirm") is not True:
                raise SkillsBoundaryError(
                    f"{face} 的 frontmatter 缺 `metadata.write_requires_confirm: true`"
                )
            if tool not in (meta.get("write_tools") or []):
                raise SkillsBoundaryError(
                    f"{face} 的 frontmatter `metadata.write_tools` 没列出 {tool}"
                )

    # 6) 默认只读：每面声明的 scopes 不得含交易类 scope
    for name, (path, fm, _t) in docs.items():
        meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
        for scope in meta.get("tsp_scopes") or []:
            if not isinstance(scope, str):
                continue
            if scope.startswith(("trade", "order")) or "trade" in scope:
                raise SkillsBoundaryError(f"{name} 声明了交易类 scope: {scope!r}")

    # 7) description 必须含研究/确认边界语义（可机读）
    for name, (path, fm, _t) in docs.items():
        desc = str(fm.get("description") or "")
        if not any(k in desc for k in ("仅供研究", "不构成投资建议", "须人工确认")):
            raise SkillsBoundaryError(
                f"{name} 的 description 缺只读/研究边界语义（含「仅供研究」「须人工确认」之一）"
            )


# ============================================================ 合成夹具


def _write_tree(
    root: Path,
    *,
    faces: tuple[str, ...] = ("tsp-scanner", "tsp-review", "tsp-monitor"),
    manifest_extra: str = "",
    skip_manifest: bool = False,
    monitor_confirm: bool = True,
    extra_scopes: str = "  tsp_scopes: [read:market]\n",
    description: str = "  测试用最小技能面。仅供研究，不构成投资建议；执行须人工确认。\n",
    drop_write_tools: bool = False,
) -> tuple[Path, Path]:
    """在 `root` 下造一棵最小假 skills 树，返回 `(skills_dir, manifest_path)`。"""
    skills = root / "skills"
    for face in faces:
        d = skills / face
        d.mkdir(parents=True, exist_ok=True)
        confirm_line = ""
        if face == "tsp-monitor":
            confirm_line = (
                "  write_requires_confirm: true\n"
                + ("" if drop_write_tools else "  write_tools: [write_ext_rows]\n")
                if monitor_confirm
                else "  write_requires_confirm: false\n"
            )
        (d / "SKILL.md").write_text(
            "---\n"
            f"name: {face}\n"
            "description: >-\n"
            f"{description}"
            "metadata:\n"
            f"{extra_scopes}"
            f"{confirm_line}"
            "---\n\n# 测试面\n",
            encoding="utf-8",
        )

    rows = [
        "| # | MCP 工具 | 技能面 | scope | Open API 路径 | 读/写 | 确认门 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, tool in enumerate(EXPECTED_MCP_TOOLS, start=1):
        face = "`tsp-monitor`" if tool in WRITE_TOOLS else "`tsp-scanner`"
        gate = "**逐次人工确认**" if tool in WRITE_TOOLS else "—"
        rw = "**写**" if tool in WRITE_TOOLS else "读"
        rows.append(f"| {i} | `{tool}` | {face} | `read:market` | `GET /x` | {rw} | {gate} |")
    manifest = skills / "MANIFEST.md"
    if not skip_manifest:
        manifest.write_text("\n".join(rows) + "\n" + manifest_extra, encoding="utf-8")
    return skills, manifest


# ============================================================ T1 骨架存在


def test_t1_official_skills_tree_exists_with_three_faces() -> None:
    """T1：`skills/` 存在，≥3 面 `tsp-*/SKILL.md`，MANIFEST 存在且可解析。"""
    assert SKILLS_DIR.is_dir(), f"skills/ 不存在: {SKILLS_DIR}"
    faces = sorted(p.parent.name for p in SKILLS_DIR.glob("tsp-*/SKILL.md"))
    assert len(faces) >= 3, f"技能面不足 3 个: {faces}"
    assert faces == ["tsp-monitor", "tsp-review", "tsp-scanner"]
    man = load_manifest()
    assert len(man["rows"]) >= len(EXPECTED_MCP_TOOLS), (
        f"映射表只有 {len(man['rows'])} 行，少于 12 个工具"
    )
    assert (SKILLS_DIR / "README.md").is_file()
    assert BRIEF.is_file(), f"BRIEF 不存在: {BRIEF}"


def test_t1b_tip_had_no_skills_tree_before_this_batch() -> None:
    """T1b：本包是**新建**，不是「tip 上原本就有」—— 禁止宣称 tip 已有官方 skill 包。

    这条只钉「文档里写明了新建」。真正的「tip 上原本没有 `skills/`」由 PR 的
    `git diff --name-only` 与人审把关 —— 测试不下判断，避免把一个 git 事实硬编码成
    断言，那样每次 rebase 都会误红。
    """
    text = BRIEF.read_text(encoding="utf-8") + (SKILLS_DIR / "README.md").read_text(encoding="utf-8")
    assert "新建" in text, "BRIEF/README 必须写明本 pak 是新建"


# ============================================================ T2 frontmatter


@pytest.mark.parametrize("face", ["tsp-scanner", "tsp-review", "tsp-monitor"])
def test_t2_frontmatter_min_fields(face: str) -> None:
    """T2：每面 frontmatter 有 `name` + `description`，description 含只读/研究边界语义。"""
    path = SKILLS_DIR / face / "SKILL.md"
    fm = _parse_frontmatter(path.read_text(encoding="utf-8"), where=face)
    assert fm.get("name") == face, f"{face} 的 name 必须等于目录名"
    desc = str(fm.get("description") or "")
    assert len(desc) > 20, f"{face} 的 description 太短，写不清边界"
    assert any(k in desc for k in ("仅供研究", "不构成投资建议", "须人工确认")), (
        f"{face} 的 description 缺研究/确认边界语义"
    )


@pytest.mark.parametrize("face", ["tsp-scanner", "tsp-review", "tsp-monitor"])
def test_t2b_body_has_min_sections(face: str) -> None:
    """T2b：正文最小节齐（何时使用 / 能力边界 / 映射工具 / 示例 / 禁止事项 / 脚注）。"""
    text = (SKILLS_DIR / face / "SKILL.md").read_text(encoding="utf-8")
    for section in ("何时使用", "能力边界", "映射工具", "示例", "禁止事项", "脚注"):
        assert section in text, f"{face} 缺最小节：{section}"
    assert "仅供研究，不构成投资建议；执行须人工确认" in text, f"{face} 缺合规脚注"


# ============================================================ T3 12 工具覆盖


def _tools_from_mcp_source(source: str) -> set[str]:
    """从 MCP server 源码里抽出所有 `@mcp.tool()` 装饰的函数名。

    单独抽成函数不是为了好看，是为了**能被负例喂假源码**：扫描逻辑埋在测试函数
    体里的话，就永远无法证明「结果真的被比较过」—— 变异测试会打回一条 `assert True`
    存活（实际发生过，见 `test_t3b_negative_*`）。
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        # `@mcp.tool()` 装饰的 FunctionDef
        for dec in getattr(node, "decorator_list", []):
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "tool"
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
                and isinstance(node, ast.FunctionDef)
            ):
                found.add(node.name)
    return found


def _diff_against_expected(found: set[str], expected: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """返回 `(源码多出, 源码缺少)`。两个都空才算对齐。"""
    return sorted(found - set(expected)), sorted(set(expected) - found)


def test_t3_manifest_covers_all_12_mcp_tools() -> None:
    """T3：映射表覆盖 MCP 12 工具（与 MCP README 交叉）。缺一则红。"""
    man = load_manifest()
    listed = {r["tool"] for r in man["rows"]}
    missing = set(EXPECTED_MCP_TOOLS) - listed
    assert not missing, f"映射表漏掉: {sorted(missing)}"
    # 反向：清单里不能出现 MCP 根本没有的工具（否则是「宣称了不存在的能力」）
    extra = listed - set(EXPECTED_MCP_TOOLS)
    assert not extra, f"映射表出现 MCP 不存在的工具: {sorted(extra)}"


def test_t3b_manifest_matches_mcp_server_source() -> None:
    """T3b：`EXPECTED_MCP_TOOLS` 与 MCP 源码里真实的 `@mcp.tool()` 函数名一致。

    ## 为什么这条重要

    工具名集合如果只写在测试里，MCP 那边加了一个工具，测试照样绿 —— 而新工具
    会**直接被 AI 看到**却从没进过 skill 映射表。所以这里**真的去读源码**：
    只要有 `if _has("scope"):` 分支下的 `@mcp.tool()`，就把它抽出来比。

    这条一旦红，处置顺序是：先看新工具是不是写/交易类 → 决定它归哪面 → 补清单。
    直接改 `EXPECTED_MCP_TOOLS` 让它变绿，等于把门拆了。
    """
    server = REPO_ROOT / "mcp-server" / "src" / "tsp_mcp_server" / "server.py"
    assert server.is_file(), f"MCP server 源码不存在: {server}"

    found = _tools_from_mcp_source(server.read_text(encoding="utf-8"))

    assert found, "没在 server.py 里找到任何 @mcp.tool() —— 解析逻辑可能已经失效"
    extra, missing = _diff_against_expected(found, EXPECTED_MCP_TOOLS)
    assert extra == [], f"MCP 源码多出未登记的工具: {extra}"
    assert missing == [], f"映射表列了 MCP 源码里没有的工具: {missing}"


def test_t3b_negative_parser_really_extracts() -> None:
    """T3b 负例（解析器）：假源码里三个装饰形态 → 只认 `@mcp.tool()`。

    如果 `_tools_from_mcp_source` 退化成「永远返回空集」，上面的 `assert found`
    会红；但如果它退化成「返回一堆无关函数名」，只有这条能抓住 —— 因为那时
    `found` 非空，`assert found` 照样绿。
    """
    fake = """
import x

@mcp.tool()
def alpha(a: int) -> dict:
    return {}

@mcp.tool
def beta() -> dict:
    return {}

@other.tool()
def not_mcp() -> None: ...

def plain() -> None: ...

class K:
    @mcp.tool()
    def method_on_class(self) -> None: ...
"""
    found = _tools_from_mcp_source(fake)
    assert found == {"alpha", "beta", "method_on_class"}, (
        f"解析器抽错了：{sorted(found)}（`@other.tool()` 与裸函数都不该进来）"
    )


def test_t3b_negative_mismatch_is_detectable() -> None:
    """T3b 负例（比对）：源码真集与预期集不一致时，差异必须被算出来。

    上下文：T3b 主用例里的 `extra`/`missing` 在正式骨架上都是 `[]`，单看它
    「比对了」和「没比对」长得一样。所以把判定能力单独拎到这里，用**必然错位**的
    输入证明比较函数真的能算出差集，而不是恒返回空。
    """
    found = _tools_from_mcp_source(
        "@mcp.tool()\ndef search_instrument(): ...\n"
        "@mcp.tool()\ndef brand_new_writer(): ...\n"
    )
    extra, missing = _diff_against_expected(found, ("search_instrument", "get_kline"))

    assert extra == ["brand_new_writer"], f"多出的工具没被抓到：{extra}"
    assert missing == ["get_kline"], f"缺少的工具没被抓到：{missing}"

    # 对齐时两者都必须为空 —— 否则「永远非空」的实现也能让上面两条绿。
    extra_ok, missing_ok = _diff_against_expected({"search_instrument"}, ("search_instrument",))
    assert extra_ok == [] and missing_ok == [], "对齐情形下差异应为空"


# ============================================================ T4 交易名黑名单（红测）


@pytest.mark.parametrize(
    "bad",
    ["place_order", "paper:trade", "paper_trade", "submit_order", "create_order",
     "send_order", "broker", "live_trade", "实盘下单", "市价买入"],
)
def test_t4_injected_trade_name_turns_red(bad: str, tmp_path: Path) -> None:
    """T4 红测：清单/技能面里混进交易工具名 → 必须红。

    注入方式是「合成坏输入」：在最小假树里塞一行带黑名单词的映射行，喂给同一个
    `check_boundaries`。如果这条绿了，说明扫描没生效 —— 那不是「没有问题」，是
    「门装反了」。
    """
    skills, manifest = _write_tree(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + f"| 99 | `{bad}` | `tsp-monitor` | `trade:live` | `POST /order` | **写** | — |\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "交易工具名" in str(ei.value)


def test_t4b_real_skeleton_has_no_trade_name() -> None:
    """T4 绿对照：正式骨架**不得**命中黑名单（否则上面的红测是假红）。

    没有这条对照，「扫描永远返回命中」也能让 T4 全绿 —— 那是假红，比假绿更难查，
    因为它看起来「守得很严」。
    """
    hits = scan_trade_names(MANIFEST.read_text(encoding="utf-8"), where="MANIFEST.md")
    for path in SKILLS_DIR.glob("tsp-*/SKILL.md"):
        hits += scan_trade_names(path.read_text(encoding="utf-8"), where=path.name)
    assert not hits, "正式骨架命中交易名:\n" + "\n".join(hits)


def test_t4c_brokerage_fee_is_not_a_false_positive() -> None:
    """T4c：`brokerage fee`（券商佣金，成本讨论里的正常词）不得被判成交易能力。

    这是「整词边界」的兑现：`\\bbroker\\b` 不匹配 `brokerage`。
    """
    assert not scan_trade_names("成本口径含 brokerage fee 与印花税", where="x")
    assert scan_trade_names("call broker now", where="x")


# ============================================================ T5 / T6 写工具确认门


def test_t5_write_tool_without_confirm_gate_turns_red(tmp_path: Path) -> None:
    """T5 红测：`write_ext_rows` 出现但没确认门标记 → 红。

    两种缺法都测：① 清单列写了「—」；② frontmatter 缺 `write_requires_confirm`。
    """
    # ① 清单确认门列被抹掉
    skills, manifest = _write_tree(tmp_path / "a")
    text = manifest.read_text(encoding="utf-8").replace("**逐次人工确认**", "—")
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "确认门" in str(ei.value), f"应提示确认门缺失，实际: {ei.value}"

    # ② frontmatter 说不用确认
    skills2, manifest2 = _write_tree(tmp_path / "b", monitor_confirm=False)
    with pytest.raises(SkillsBoundaryError) as ei2:
        check_boundaries(skills2, manifest2)
    assert "write_requires_confirm" in str(ei2.value)


def test_t6_real_skeleton_write_tool_has_confirm_gate() -> None:
    """T6：正式骨架里 `write_ext_rows` 在，且**两处**都标了确认门。"""
    man = load_manifest()
    row = next(r for r in man["rows"] if r["tool"] == "write_ext_rows")
    assert "确认" in row["confirm"], "MANIFEST 的确认门列必须是「逐次人工确认」"
    fm = _parse_frontmatter(
        (SKILLS_DIR / "tsp-monitor" / "SKILL.md").read_text(encoding="utf-8"),
        where="tsp-monitor",
    )
    meta = fm["metadata"]
    assert meta.get("write_requires_confirm") is True
    assert "write_ext_rows" in meta.get("write_tools", [])
    # 正文也必须写明「未确认不得调用」
    body = (SKILLS_DIR / "tsp-monitor" / "SKILL.md").read_text(encoding="utf-8")
    assert "不得调用" in body


# ============================================================ T7 fail-closed


def test_t7_missing_manifest_is_fail_closed(tmp_path: Path) -> None:
    """T7：没有清单 → **抛错**，不是「跳过即通过」。"""
    skills, manifest = _write_tree(tmp_path, skip_manifest=True)
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "不存在" in str(ei.value)


def test_t7b_empty_manifest_is_fail_closed(tmp_path: Path) -> None:
    """T7b：清单是空文件 → 抛错（空表不能当成「12 工具都覆盖了」）。"""
    skills, manifest = _write_tree(tmp_path)
    manifest.write_text("   \n", encoding="utf-8")
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "空文件" in str(ei.value)


def test_t7c_empty_skills_tree_is_fail_closed(tmp_path: Path) -> None:
    """T7c：技能目录为空 → 抛错（空树不能当成「边界都守住了」）。"""
    skills = tmp_path / "skills"
    skills.mkdir()
    manifest = tmp_path / "MANIFEST.md"
    manifest.write_text((MANIFEST.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "SKILL.md" in str(ei.value)


def test_t7d_manifest_missing_a_tool_turns_red(tmp_path: Path) -> None:
    """T7d：映射表少一行（例如把写工具那行静默删掉）→ 红，不是「少就少吧」。"""
    skills, manifest = _write_tree(tmp_path)
    lines = [
        ln for ln in manifest.read_text(encoding="utf-8").splitlines()
        if "`write_ext_rows`" not in ln
    ]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "write_ext_rows" in str(ei.value)


def test_t7e_calling_check_on_real_tree_passes() -> None:
    """T7e 绿测：正式骨架整体通过（`check_boundaries` 的正面用例）。"""
    check_boundaries()


# ============================================================ T8 禁空喊


def test_t8_no_banned_claim_in_skills_or_brief() -> None:
    """T8：BRIEF / README / MANIFEST / SKILL 里无「信号可做 / 可交易 / 已可下单」空喊。

    允许否定语境（「禁止可交易」「不构成投资建议」）—— 否则第一个被删的就是护栏句。
    """
    targets = [MANIFEST, SKILLS_DIR / "README.md", BRIEF]
    targets += sorted(SKILLS_DIR.glob("tsp-*/SKILL.md"))
    hits: list[str] = []
    for path in targets:
        hits += scan_banned_claims(
            path.read_text(encoding="utf-8"), where=_rel(path)
        )
    assert not hits, "出现空喊:\n" + "\n".join(hits)


def test_t8b_banned_claim_detector_actually_fires(tmp_path: Path) -> None:
    """T8b 红测：检测器本身要能命中（否则 T8 是假绿）。"""
    skills, manifest = _write_tree(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\n本策略信号可做，可直接下单。\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "空喊" in str(ei.value)


# ============================================================ T9 #217 衔接 + DSA 引用


def test_t9_brief_has_lookahead_linkage() -> None:
    """T9：BRIEF 必须写明「backtest 宣称须过仓内 lookahead 门禁 / 不在热路径重跑」。"""
    text = BRIEF.read_text(encoding="utf-8")
    assert "lookahead" in text
    assert "仓内 CI" in text, "BRIEF 必须钉「门禁在仓内 CI」"
    assert "热路径" in text, "BRIEF 必须写明「不在热路径重跑夹具」"
    assert "fail-closed" in text or "不放行" in text


def test_t9b_dsa_reference_is_doc_only() -> None:
    """T9b：DSA 只做**文档引用**，指向 `vendor/daily_stock_analysis/SKILL.md`。

    断言两件事：① BRIEF 里出现了那个路径；② 那个文件**真的存在**（引用了不存在的
    路径等于假引用）。本拍不复制、不改它的行为。
    """
    text = BRIEF.read_text(encoding="utf-8")
    assert "vendor/daily_stock_analysis/SKILL.md" in text
    assert (REPO_ROOT / "vendor" / "daily_stock_analysis" / "SKILL.md").is_file()
    # 引用它的 SKILL.md 必须同时声明「不接线 / 零改」
    assert ("不接线" in text) or ("零改" in text) or ("不复制" in text)


# ============================================================ T11 补漏（第一轮变异逼出来的）
#
# 第一轮 15 条变异里有 6 条**存活**（改坏判定，35 条用例照样全绿）。逐条查下来是同一个病：
# 正式骨架恰好**不触发**那些分支 —— 交易 scope 没人声明、description 都合格、清单里没有
# 假技能面。断言只走了"真数据一定合规"这一边，没走"坏输入必须被抓"那一边。
#
# 这类存活最危险的地方在于：它看起来是绿的，而且确实是绿的 —— 只是守门代码被删了也不影响。
# 下面每条都用**合成坏输入**把那半边补上。


def test_t11_readonly_default_excludes_trade_scope(tmp_path: Path) -> None:
    """T11（S06）：某面声明了 `trade:*` / `paper:trade` 之类 scope → 必须红。

    正式骨架全是 `read:*`，所以「交易 scope 检查」这条分支从没被走过。删掉它，
    CI 照样绿 —— 但下一个给 skill 加交易 scope 的人就会被放过。
    """
    skills, manifest = _write_tree(tmp_path, extra_scopes="  tsp_scopes: [read:market, trade:live]\n")
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "交易类 scope" in str(ei.value)


def test_t11b_trade_scope_detector_catches_paper_and_order(tmp_path: Path) -> None:
    """T11b：交易 scope 的三种典型写法都要被抓（不只 `trade:live`）。"""
    for scope in ("trade:live", "order:place", "paper:trade"):
        skills, manifest = _write_tree(
            tmp_path / scope.replace(":", "_"), extra_scopes=f"  tsp_scopes: [read:market, {scope}]\n"
        )
        with pytest.raises(SkillsBoundaryError):
            check_boundaries(skills, manifest)


def test_t11c_description_without_boundary_semantics_turns_red(tmp_path: Path) -> None:
    """T11c（S07）：description 只讲功能、不讲只读/研究边界 → 红。

    宿主是靠 description 决定"什么时候调用"的。一句"选股工具，能查行情"装上去，
    AI 就会把它当纯粹的取数器，边界全靠正文 —— 而正文不一定被读。
    """
    skills, manifest = _write_tree(tmp_path, description="  查询行情与选股。\n")
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "边界语义" in str(ei.value)


def test_t11d_manifest_naming_missing_face_turns_red(tmp_path: Path) -> None:
    """T11d（S11）：映射表把工具挂在**不存在的技能面**上 → 红。

    这条防的是"清单里挂了个名字，但那个 SKILL.md 根本没写"。用户装上去以后，
    宿主找不到那个技能面，工具却仍被映射 —— 等于清单在撒谎。
    """
    skills, manifest = _write_tree(tmp_path)
    text = manifest.read_text(encoding="utf-8").replace("`tsp-scanner`", "`tsp-ghost`", 1)
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "不存在" in str(ei.value)


def test_t11e_two_faces_only_turns_red(tmp_path: Path) -> None:
    """T11e（S14）：技能面少于 3 个 → 红（分发面残缺，不能静默通过）。"""
    skills, manifest = _write_tree(tmp_path, faces=("tsp-scanner", "tsp-review"))
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "不足 3 个" in str(ei.value)


def test_t11f_write_requires_confirm_true_but_tool_not_listed(tmp_path: Path) -> None:
    """T11f（S15）：`write_requires_confirm: true` 但 `write_tools` 没列写工具 → 红。

    ## 为什么"true 了还要列清单"

    `write_requires_confirm: true` 是一句**笼统的**承诺；`write_tools: [...]` 才是
    "哪个工具受这条约束"的**具体指名**。只写前者，等于说"我们很谨慎"，但没说对谁谨慎。
    将来多挂一个写工具时，前者仍为 true，后者没更新 —— 于是新工具**不受**任何约束。
    """
    skills, manifest = _write_tree(tmp_path, drop_write_tools=True)
    with pytest.raises(SkillsBoundaryError) as ei:
        check_boundaries(skills, manifest)
    assert "write_tools" in str(ei.value)


# ============================================================ T10 CI 接线


def test_zz_wired_into_ci() -> None:
    """T10：边界测被主 pytest 收集（`ci.yml` 跑 `uv run pytest tests -q`）。

    本文件在 `backend/tests/skills/` 下 → 自然被收集。这里额外钉住 ci.yml 真的跑全量、
    且**没有** `continue-on-error`（那会把红测变绿）。
    """
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pytest tests" in ci, "CI 必须跑全量 pytest，否则 tests/skills 不会被收集"
    assert "continue-on-error" not in ci, "ci.yml 不许 continue-on-error"


def test_zz_no_silent_skip() -> None:
    """T10b：本模块不许出现 skip / xfail 掩盖红测。

    扫**源码**而不是看运行时标记：`skipif` 在某些环境会求值成跳过而不留痕迹，而
    「这个文件里根本不许出现 skip/xfail」是一条源码级纪律。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    body = src.split("def test_zz_no_silent_skip")[0]
    # 禁用词拆开拼：否则守卫会扫到它自己的源码（本函数上半段那段 docstring），
    # 于是「装上守卫」和「违反守卫」长得一模一样 —— 只能靠删文档来过关。
    for banned in ("pytest" + ".mark.skip", "pytest" + ".mark.xfail", "pytest" + ".skip("):
        assert banned not in body, f"边界测不许用 {banned} 掩盖"
