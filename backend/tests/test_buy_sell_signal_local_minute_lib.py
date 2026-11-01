"""VOLUME 本机分钟库探针清零一刀 + 契约回归（批次 20261101gevid3 · P-bss-evidence · #-A-#9）。

## 这组测在测什么

这一拍从 `VOLUME_BREAKOUT_EVIDENCE_PENDING` 里**删了一个标签**
（`local_minute_lib_data_pending`），并新建了 `local_minute_lib` 作为它的顶替交付物。

删标签 + 建模块这个组合，坏的写法有三种，前两种都不会报错：

1. **空删** —— 标签删了，模块没建或建了但不可验。「缺口消失」了却说不清是哪件事
   让它消失。这是最坏的一种：进度看着前进了，实际没有。
2. **假删** —— 标签改成 `local_minute_lib_data_done` 仍留在集合里。集合非空、
   断言照过，但"还有几条没清"从此失真。
3. **借机多删** —— 顺手把 `threshold_lock_data_pending` 或 `http_minute_listing_auth_pending`
   也清掉。缺口数更好看，但阈值一个都没钉、鉴权一次都没通。

所以主线是：**探针真的在且可验**（T1）、**只删了那一条且删得干净**（T2）、
**总数随之派生而变、两断言仍过**（T3）。

## 为什么夹具是 tmp_path 而不是真实数据

本机确实有 `data/kline_minute`（5.1G · 362 分区），但**测例不许依赖它**：
CI 上没有这份数据，依赖它只会让红灯变成"环境没挂数据"而不是"契约被改坏了"。
更重要的是 —— 一旦测例读真实数据，"探针 PRESENT"和"数据可用"就被悄悄划上等号，
而这正是本模块反复声明**不**主张的那件事。

夹具只需要两个 `date=*/part.parquet` 空目录，就足够验布局契约。

## 不测什么

不测真翻闸、不测触发引擎、不测 calib、不解 parquet、不读行情、不调度。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

# ruff: noqa: SIM300
# SIM300: 本文件满是 `assert CONST == 字面量`, ruff 把全大写标识符当"常量应在右",
#         在 assert 上系统性误报 —— 把字面量翻到左边才是真的不可读。

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "local_minute_lib.py"
_STUB_PATH = _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "volume_breakout_stub.py"
_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-trigger-gate.md"
_SKELETON_PATH = _REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md"

_CLEARED_TAG = "local_minute_lib_data_pending"
_FOOTNOTE = "*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*"

_REMAINING_VOLUME_TAGS = {
    "http_minute_listing_auth_pending",
    "threshold_lock_data_pending",
}


# ---------------------------------------------------------------- 夹具


def _fake_partitions(root: Path, dates: list[str], *, with_parts: bool = True) -> None:
    """在 `root` 下造 `date=<d>/part.parquet` 最小分区。

    `part.parquet` 写 0 字节：本拍探针**只验布局**，不解 parquet —— 写 0 字节
    恰好把"探针不读内容"这件事也钉住：如果哪天有人让探针去 `read_parquet`，
    这个夹具会立刻炸，而不是悄悄变成第二个数据质量管线。
    """
    for d in dates:
        part = root / f"date={d}"
        part.mkdir(parents=True, exist_ok=True)
        if with_parts:
            (part / "part.parquet").write_bytes(b"")


# ---------------------------------------------------------------- T1 探针可验


def test_t1_module_present_with_version_and_relative_name():
    """G-T2: 模块 PRESENT、version 非空且被钉住、相对名与 helper 约定一致。"""
    from app.buy_sell_signals import local_minute_lib as m

    assert m.LOCAL_MINUTE_LIB_VERSION == "local_minute_lib_v1"
    assert m.LOCAL_MINUTE_LIB_REL == "kline_minute"
    assert m.LOCAL_MINUTE_LIB_PARTITION_PREFIX == "date="
    assert m.LOCAL_MINUTE_LIB_PART_NAME == "part.parquet"


def test_t1_resolve_root_matches_helper_convention():
    """G-T2: `resolve_local_minute_root(data_dir) == data_dir / "kline_minute"`。

    与 `scripts/check_minute_kline.py::default_minute_root()` 同一约定。
    两处不一致的话，「补数脚本看得到的数据」与「BSS 探针认为存在的数据」会是
    两个目录 —— 这种分叉不会报错，只会让两边各说各话。
    """
    from app.buy_sell_signals import local_minute_lib as m

    helper_text = (_REPO_ROOT / "scripts" / "check_minute_kline.py").read_text(encoding="utf-8")
    assert 'Path(settings.data_dir) / "kline_minute"' in helper_text, (
        "helper 的约定变了 —— 本模块的相对名必须跟着改，并 bump LOCAL_MINUTE_LIB_VERSION"
    )

    assert m.resolve_local_minute_root("/srv/tsp/data") == Path("/srv/tsp/data/kline_minute")
    assert m.resolve_local_minute_root(Path("/srv/tsp/data")) == Path("/srv/tsp/data/kline_minute")


def test_t1_probe_tmp_fixture_ok(tmp_path: Path):
    """G-T2: 两个 `date=*/part.parquet` 夹具 → present=True / count=2 / ok=True。"""
    from app.buy_sell_signals import local_minute_lib as m

    root = m.resolve_local_minute_root(tmp_path)
    root.mkdir()
    _fake_partitions(root, ["2025-05-07", "2026-10-30"])

    probe = m.probe_local_minute_lib(root)
    assert probe.present is True
    assert probe.partition_count == 2
    assert probe.ok is True
    assert probe.date_first == "date=2025-05-07"
    assert probe.date_last == "date=2026-10-30"
    assert probe.sample_path is not None
    assert probe.sample_path.endswith("part.parquet")
    # 首尾排序按名字，不按 mtime —— 目录名就是日期，lexicographic 即时间序
    assert "date=2026-10-30" in probe.sample_path


def test_t1_probe_empty_dir_is_not_ok(tmp_path: Path):
    """G-T2 / 空桶: 根在但**没有** `date=` 分区 → present=True 但 ok=False。

    「目录建好了」是最容易被当成「已经好了」的中间态。空目录报 ok，
    等于让一个空壳把缺口标记成已清。
    """
    from app.buy_sell_signals import local_minute_lib as m

    empty = tmp_path / "empty_kline"
    empty.mkdir()

    probe = m.probe_local_minute_lib(empty)
    assert probe.present is True
    assert probe.partition_count == 0
    assert probe.ok is False
    assert probe.date_first is None
    assert probe.date_last is None
    assert probe.sample_path is None


def test_t1_probe_missing_root_is_absent(tmp_path: Path):
    """G-T2: 根不存在 → present=False / ok=False（不假装触达）。"""
    from app.buy_sell_signals import local_minute_lib as m

    probe = m.probe_local_minute_lib(tmp_path / "nope")
    assert probe.present is False
    assert probe.partition_count == 0
    assert probe.ok is False


def test_t1_probe_ignores_non_date_dirs_and_files(tmp_path: Path):
    """G-T2: 只认 `date=` 前缀的子目录；普通目录 / 文件不算分区。

    `kline_minute` 里可能躺着备份目录（helper 的修复说明就要求把备份放到外面，
    但仍可能有人放进来）。把这些算成分区，会让 `partition_count` 撒谎。
    """
    from app.buy_sell_signals import local_minute_lib as m

    root = tmp_path / "kline_minute"
    root.mkdir()
    _fake_partitions(root, ["2026-10-30"])
    (root / "backup_20261030").mkdir()
    (root / "README.txt").write_text("not a partition", encoding="utf-8")

    probe = m.probe_local_minute_lib(root)
    assert probe.partition_count == 1
    assert probe.ok is True
    assert probe.date_first == "date=2026-10-30"


def test_t1_probe_ok_does_not_require_part_parquet(tmp_path: Path):
    """G-T2: 有 `date=` 分区但分区里**没有** part.parquet → ok 仍 True、sample=None。

    这条把 `ok` 的语义钉死在「布局在不在」上：`sample_path` 是便利字段，
    不是 `ok` 的必要条件。反过来写，会让"分区刚建好、数据还没落"的中间态
    把 `ok` 打成 False —— 而那不是本模块要回答的问题（数据质量是
    `check_minute_kline.py` 的事）。
    """
    from app.buy_sell_signals import local_minute_lib as m

    root = tmp_path / "kline_minute"
    root.mkdir()
    _fake_partitions(root, ["2026-10-30"], with_parts=False)

    probe = m.probe_local_minute_lib(root)
    assert probe.present is True
    assert probe.partition_count == 1
    assert probe.ok is True
    assert probe.sample_path is None


def test_t1_assert_helper_passes_on_fixture_and_fires_on_empty(tmp_path: Path):
    """G-T5: 断言助手在假根上通过；负控 —— 空根必须炸。"""
    from app.buy_sell_signals import local_minute_lib as m

    good = tmp_path / "kline_minute"
    good.mkdir()
    _fake_partitions(good, ["2025-05-07", "2026-10-30"])
    probe = m.assert_local_minute_lib_probeable(good)
    assert probe.ok is True

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AssertionError, match="布局不符合契约"):
        m.assert_local_minute_lib_probeable(empty)


def test_t1_module_import_does_not_raise_and_has_no_io_or_network():
    """G-T7: import 期不硬 raise；模块不碰 I/O 写、不联网、不调度、不读 parquet。"""
    from app.buy_sell_signals import local_minute_lib as m

    assert m.LOCAL_MINUTE_LIB_VERSION  # import 成功即足够

    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    forbidden = {
        "requests",
        "httpx",
        "subprocess",
        "socket",
        "shutil",
        "polars",
        "duckdb",
        "pyarrow",
        "pandas",
        "apscheduler",
        "schedule",
        "asyncio",
        "tempfile",
    }
    assert not (forbidden & imported), f"探针模块不该 import: {sorted(forbidden & imported)}"
    # 允许 pathlib（读目录）与 dataclasses / typing；不允许 open() 写
    text = _MODULE_PATH.read_text(encoding="utf-8")
    assert "open(" not in text, "探针模块不该直接 open() 文件"
    assert "write_text" not in text and "write_bytes" not in text
    assert "mkdir" not in text, "探针只读，不该建目录"
    # 连「摸一下大小」也不许：rglob/glob/stat 都会把探针往数据质量管线上推
    for needle in ("rglob(", ".glob(", ".stat(", "read_parquet", "read_bytes"):
        assert needle not in text, f"探针模块不该出现 {needle}"


def test_t1_probe_does_not_stat_or_open_partition_files(tmp_path: Path, monkeypatch):
    """G-T2 负控: 探针对 `part.parquet` **一次 stat / open 都不做**。

    上一条用 0 字节文件说明"没解内容"，但那只能挡住 `read_parquet`——
    把 `rglob("*.parquet")` + `stat()` 写进去，0 字节文件照样绿，静默多出来的
    目录遍历却没人发现。

    这条改成**行为**断言：把 `Path.open` 与 `Path.stat` 包一层记账，
    探针跑完后对 `*.parquet` 的调用必须为空。探针只允许 `is_file()`
    （`Path.is_file()` 走的是 `stat` 的 C 层，不经 Python 的 `Path.stat`，
    所以不受这层拦截影响，夹具里 `sample_path` 仍能取到）。
    """
    from app.buy_sell_signals import local_minute_lib as m

    root = tmp_path / "kline_minute"
    root.mkdir()
    _fake_partitions(root, ["2025-05-07", "2026-10-30"])

    calls: list[str] = []
    real_open = Path.open

    def guarded_open(self, *args, **kwargs):  # pragma: no cover - 只在违规时触发
        calls.append(f"open:{self}")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    probe = m.probe_local_minute_lib(root)
    assert probe.ok is True
    assert calls == [], f"探针对分区文件做了 open(): {calls}"


# ---------------------------------------------------------------- T2 删标签（干净、不空删）


def test_t2_pending_exactly_two_without_cleared_tag():
    """G-T3: `VOLUME_BREAKOUT_EVIDENCE_PENDING` 恰 **2** 条且**不含**已清标签。"""
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert len(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING) == 2
    assert _CLEARED_TAG not in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING
    assert set(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING) == _REMAINING_VOLUME_TAGS


def test_t2_cleared_tag_is_not_left_as_variant():
    """G-T3 负控: 已清标签的任何后缀变体都不在集合里（不是改成 `_done` 留着）。"""
    from app.buy_sell_signals import volume_breakout_stub as vb

    for tag in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING:
        assert tag.endswith("pending"), f"缺口标签应以 pending 结尾: {tag}"
        assert "_done" not in tag, f"出现 _done 后缀: {tag}"
        assert "local_minute" not in tag, f"本机分钟库那条应已整条删除: {tag}"

    for variant in (
        "local_minute_lib_data_done",
        "local_minute_lib_done",
        "local_minute_lib_data_pending_done",
        "local_minute_lib_data_pending",
    ):
        assert variant not in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING


def test_t2_human_table_keys_match_pending():
    """G-T3: 人读表键集与机器集**相等**（删键，不是只删 pending）。"""
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert set(vb.VOLUME_BREAKOUT_EVIDENCE) == set(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING)
    assert _CLEARED_TAG not in vb.VOLUME_BREAKOUT_EVIDENCE
    # 人读表里旧句「本机既没挂载也没落盘」不得残留 —— 它已过期
    human_text = repr(vb.VOLUME_BREAKOUT_EVIDENCE)
    assert "本机既没挂载也没落盘" not in human_text


def test_t2_status_and_triggering_untouched():
    """G-T3 / G-T9: STATUS 仍 `data_pending`；TRIGGERING 仍 `False`；无 `_done`。"""
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert vb.VOLUME_BREAKOUT_STATUS == "data_pending"
    assert vb.VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert vb.VOLUME_BREAKOUT_RECORD_STATUS == "candidate"
    # 负证据数字本拍零碰
    assert vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE["returns"] == (-23.86, -32.44, -13.80, -19.81)


def test_t2_stub_does_not_keep_stale_three_tag_docstring():
    """G-T3: stub docstring 不得再说「三条」「四条全部 data_pending」。"""
    text = _STUB_PATH.read_text(encoding="utf-8")
    assert "三条证据缺口" not in text
    assert "四条证据缺口" not in text
    assert "四条全部 `data_pending`" not in text
    # 已清两条都要在 docstring 里有交代（免得下一个人以为从没清过）
    assert "trigger_evidence_table_data_pending" in text
    assert _CLEARED_TAG in text


# ---------------------------------------------------------------- T3 COUNT 派生 = 6


def test_t3_count_is_six_and_derived():
    """G-T4: COUNT == 6，且**等于两族之和**（不是手抄的 6）。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_EVIDENCE_PENDING,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_EVIDENCE_PENDING,
    )

    assert len(VOLUME_BREAKOUT_EVIDENCE_PENDING) == 2
    assert len(AUCTION_ABNORMAL_EVIDENCE_PENDING) == 4
    assert g.TRIGGER_GATE_EVIDENCE_PENDING_COUNT == 6
    assert g.TRIGGER_GATE_EVIDENCE_PENDING_COUNT == (
        len(VOLUME_BREAKOUT_EVIDENCE_PENDING)
        + len(AUCTION_ABNORMAL_EVIDENCE_PENDING)
    )


def test_t3_count_source_is_a_sum_not_a_literal():
    """G-T4: COUNT 的**源码**必须是求和，不许写成字面量 6。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    tree = ast.parse(Path(g.__file__).read_text(encoding="utf-8"))
    target = None
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "TRIGGER_GATE_EVIDENCE_PENDING_COUNT"
        ):
            target = node.value
            break
    assert target is not None, "找不到 TRIGGER_GATE_EVIDENCE_PENDING_COUNT 定义"
    assert not isinstance(target, ast.Constant), "COUNT 被写成了字面量"
    assert isinstance(target, ast.BinOp) and isinstance(target.op, ast.Add)


def test_t3_lock_assertions_still_pass():
    """G-T4: 清第 2 条后，`assert_evidence_pending_still_open` /
    `assert_triggering_still_false` **仍应通过**（两族都还非空、STATUS 未漂）。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    g.assert_evidence_pending_still_open()
    g.assert_triggering_still_false()
    g.assert_complete_still_false()
    g.assert_trigger_gate_lock_invariants()


def test_t3_evidence_assert_still_fires_when_family_emptied(monkeypatch):
    """G-T4 负控: 把 VOLUME 清**空**，断言必须发火（清一条不炸、清空才炸）。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals import volume_breakout_stub as vb

    monkeypatch.setattr(vb, "VOLUME_BREAKOUT_EVIDENCE_PENDING", frozenset())
    with pytest.raises(AssertionError, match="EVIDENCE_PENDING is empty"):
        g.assert_evidence_pending_still_open()


# ---------------------------------------------------------------- T4 披露同步


def test_t4_gate_doc_discloses_six_left_two_cleared():
    """G-T4 / G-T10: 门禁文档披露「已清 2 / 剩 6」，且旧口径不残留。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    assert "已清 2 / 剩 6" in text
    assert "证据缺口现 6 条（已诚实清 2）" in text
    assert "仍 2 条" in text
    assert "仍 4 条" in text
    # 旧口径不得残留
    assert "已清 1 / 剩 7" not in text
    assert "证据缺口现 7 条" not in text
    assert "仍 3 条" not in text
    # 已清标签只在「已清」语境里出现，且带大字
    assert _CLEARED_TAG in text
    assert "落盘 ≠ 可开 TRIGGERING" in text
    assert "探针 PRESENT ≠ 可开 TRIGGERING" in text
    assert "清零 ≠ 可翻闸" in text


def test_t4_gate_doc_keeps_big_letters_and_footnote():
    """G-T10: 门禁大字与脚注仍在，末行仍是脚注。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    for needle in (
        "路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING",
        "K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING",
        "门禁契约 ≠ 授权翻闸",
        "合入 ≠ 部署",
        "仅供研究，不构成投资建议；执行须人工确认",
        "红涨绿跌",
        "18.2bp",
    ):
        assert needle in text, f"门禁文档缺: {needle}"

    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[-1] == _FOOTNOTE, f"末行不是脚注: {lines[-1]!r}"


def test_t4_lock_note_discloses_six_left():
    """G-T4: 锁面 NOTE 披露「已清2/剩6」，带大字，且不再出现「已清1/剩7」。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    note = g.TRIGGER_GATE_LOCK_NOTE
    assert "已清2/剩6" in note
    assert "已清1/剩7" not in note
    assert "清零一刀!=可翻闸" in note
    assert "落盘!=可开TRIGGERING" in note
    assert "探针PRESENT!=可开TRIGGERING" in note
    # 不 bump 的披露要在 NOTE 可追溯到（NOTE 里挂 version 字面）
    assert "version=trigger_gate_v1" in note


def test_t4_skeleton_discloses_current_state():
    """G-T4: skeleton 相关披露已同步（不再说「三条」/「已清 1 / 剩 7」）。"""
    text = _SKELETON_PATH.read_text(encoding="utf-8")
    assert "### 两条证据缺口" in text
    assert "已清 2 / 剩 6" in text
    assert "已清 1 / 剩 7" not in text
    assert "### 三条证据缺口" not in text
    # 历史快照行必须标注，不能让读者以为标签仍 pending
    assert "历史快照" in text
    assert "local_minute_lib.py" in text

    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[-1] == _FOOTNOTE, "skeleton 脚注应仍在末行"


def test_t4_no_stale_disclosure_across_docs_and_lock():
    """G-T4: docs / 锁面 / stub 不得残留旧的「已清 1 / 剩 7」「仍 3 条」口径。

    注意排除本文件自己：测例里本来就要写旧字符串去断言"它不存在"。
    """
    needles = ("已清 1 / 剩 7", "仍 3 条", "证据缺口现 7 条", "三条证据缺口")
    candidates = [
        *(_REPO_ROOT / "docs").glob("*.md"),
        Path(__import__("app").__file__).parent / "buy_sell_signals" / "trigger_gate_lock.py",
        _STUB_PATH,
    ]
    offenders: list[str] = []
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"仍有旧披露残留: {offenders}"


def test_t4_stub_docstring_records_batch_and_big_letters():
    """G-T10: stub docstring 记批次号 + 大字（清零≠可翻闸 / 落盘≠可开 TRIGGERING）。"""
    text = _STUB_PATH.read_text(encoding="utf-8")
    assert "20261101gevid3" in text
    assert "清零 ≠ 可翻闸" in text
    assert "落盘 ≠ 可开 TRIGGERING" in text
    assert "探针 PRESENT ≠ 可开 TRIGGERING" in text


# ---------------------------------------------------------------- T5 bump 决策 / 零碰


def test_t5_lock_version_explicitly_not_bumped():
    """G-T4 选项 β: 本拍**显式不 bump**，version 仍 `trigger_gate_v1`。

    bump 会迫使 `schema_push_lock` / `schema_push_live_lock` 的 `EXPECTED_*_VERSION`
    同步改字面量，而那两个文件标零碰。所以本拍只改 NOTE 与文档披露，
    version 留待另拍与交叉面一起动。**未 bump ≠ 未披露**。
    """
    from app.buy_sell_signals import trigger_gate_lock as g

    assert g.TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    # 不 bump 的理由必须写在锁面里，不能只活在 PR 描述里
    doc = Path(g.__file__).read_text(encoding="utf-8")
    assert "显式不 bump" in doc
    assert "EXPECTED_TRIGGER_GATE_LOCK_VERSION" in doc


def test_t5_schema_push_expected_version_untouched():
    """G-T4 / Z1: 两个 schema_push 锁面的 EXPECTED_* 本拍零碰（仍 v1）。"""
    from app.buy_sell_signals import schema_push_live_lock as live
    from app.buy_sell_signals import schema_push_lock as base

    assert base.EXPECTED_TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    assert live.EXPECTED_TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"


def test_t5_direction_bonferroni_schema_untouched():
    """G-T5 / G-T7 / G-T8: DIRECTION 三值；m=6；HITS schema 仍 "1"。"""
    from app.buy_sell_signals.constants import (
        BONFERRONI_M,
        DIRECTION_LABELS,
        HITS_SCHEMA_VERSION,
    )

    assert DIRECTION_LABELS == {"research_buy", "research_sell", "research_watch"}
    assert BONFERRONI_M == 6
    assert HITS_SCHEMA_VERSION == "1"


def test_t5_auction_family_untouched():
    """G-T3 / Z8: AUCTION 四条一条不动、TRIGGERING 仍 False。"""
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_EVIDENCE_PENDING,
        AUCTION_ABNORMAL_TRIGGERING_ENABLED,
    )

    assert AUCTION_ABNORMAL_EVIDENCE_PENDING == frozenset(
        {
            "auction_price_not_persisted",
            "threshold_lock_data_pending",
            "backtest_evidence_data_pending",
            "minute_0925_still_absent",
        }
    )
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_t5_complete_still_false():
    """G-T9: `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` 仍 False（本拍零碰）。"""
    from app.buy_sell_signals.first_seen_audit import EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE

    assert EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False


def test_t5_no_allow_shadow_and_no_action_phrases():
    """G-T7 / G-T5: 探针模块与锁面无 `allow_shadow`；模块文本无六词动作指令式。"""
    from app.buy_sell_signals import local_minute_lib as m
    from app.buy_sell_signals import trigger_gate_lock as g

    banned = {"allow_shadow", "ALLOW_SHADOW", "AllowShadow"}
    for module in (m, g):
        assert not (banned & set(dir(module))), f"{module.__name__} 出现 allow_shadow"

    text = _MODULE_PATH.read_text(encoding="utf-8")
    for phrase in (
        "可以买入",
        "建议买入",
        "强烈买入",
        "可以卖出",
        "建议卖出",
        "强烈卖出",
    ):
        assert phrase not in text, f"探针模块出现六词动作指令: {phrase}"


def test_t5_module_has_no_price_quantity_position_fields():
    """G-T5: 探针模块不写价格 / 数量 / 仓位 / 订单字段。"""
    text = _MODULE_PATH.read_text(encoding="utf-8")
    for field in ("quantity", "position_size", "order_id", "price="):
        assert field not in text, f"探针模块不该出现 {field}"


def test_t5_no_minute_data_paths_hardcoded_outside_relative_name():
    """G-T2: 模块里只允许相对名 `kline_minute`，不许出现真实绝对路径 / 数据文件名。"""
    text = _MODULE_PATH.read_text(encoding="utf-8")
    offenders = re.findall(r"[A-Za-z]:\\[^\s\"']+|/(?:home|root|mnt|srv)/[^\s\"'`]+", text)
    assert not offenders, f"模块出现绝对路径: {offenders}"
