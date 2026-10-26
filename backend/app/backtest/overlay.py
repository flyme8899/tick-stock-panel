"""回测临时叠加 (ephemeral overlay) — 多选现有策略当次合成, 不落盘。

为什么单独成模块: 回测「叠加」tab 要让用户多选现有 builtin/custom/ai 策略直接跑
一次回测, 而不必先 `POST /composite/save` 存一个 `composite_*` 策略文件。
落盘方案有两个问题:
1. 用户的当次尝试会在 `data/strategies/composite/` 里堆出一串一次性的
   `composite_xxx.py`, 越试越脏, 且这些文件会被策略列表/选股复用;
2. 落盘是不可逆的副作用 —— 一次失败的参数探索也会留下痕迹。

所以这里的做法是: 请求里带 `overlay_children` + `merge_mode` + `min_confirm`,
服务端**在内存里**合成一个 `execution_backend="composite"` 的 StrategyDef,
注册进当次回测用的策略引擎, 跑完随进程/引擎一起消失。合成后的定义与落盘
composite 走完全相同的解析路径 (`_resolve_composite_feature_plan` →
`_generate_composite_signal_matrix` → `app.strategy.composite.merge_signal_matrices`),
因此合并语义与选股侧 `merge_results` 同轨, 不存在"选股一套、回测另一套"的漂移。

口径钉死 (PR 与文档同步):
- `strategy_id` 固定占位 `__ephemeral_overlay__`; 请求里传的 `strategy_id` 在这条路径
  下被忽略 —— 否则前端传的 id 与合成 id 会形成两个真源, 日志里分不清是哪一个。
- `overlay_children` **至少 2 个**才启用本路径; 1 个直接拒绝并提示改单选 ——
  单个子策略没有"合并"可言, 静默降级会让用户在"叠加" tab 里单选出意料之外的结果。
- `merge_mode` 默认 `intersect` (对齐 `CompositeStrategyDialog` 的默认值), 可选 `union`;
  `min_confirm` 默认 0 (= 全部子策略都命中)。
- 权重取请求值, 缺省 1.0; 不在此处归一化 —— 归一是合并器的事, 这里归一化会让
  两处权重口径分叉。
- **禁嵌套**: 任一子策略 `source=='composite'` / `execution_backend=='composite'` /
  id 前缀 `composite_` → 拒绝 (与 `_validate_composite_references` 及
  `_save_composite_strategy` 的禁嵌套语义一致)。
- 子策略的存在性、asset_types / timeframes 兼容性沿用引擎既有校验, 不在这里重写。

合法边界: 只读请求参数, 不写任何文件; 除返回值与目标引擎的注册表外不改调用方状态。
异常边界: 参数非法抛 `OverlaySpecError` (API 层转 400), 其余异常照常上抛。

仅供研究, 不构成投资建议; 执行须人工确认。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from app.strategy.composite import MERGE_MODES, normalize_merge_mode
from app.strategy.engine import MAX_COMPOSITE_CHILDREN, CompositeChild, CompositeSpec

if TYPE_CHECKING:
    from app.strategy.engine import StrategyDef, StrategyEngine

# 合成策略的占位 id。请求里的 strategy_id 在本路径下被忽略, 统一用这个值,
# 保证"前端传什么"与"服务端算什么"不会各留一份。
EPHEMERAL_OVERLAY_STRATEGY_ID = "__ephemeral_overlay__"
# 叠加路径最少子策略数: 1 个没有合并语义, 直接拒并提示改单选。
MIN_OVERLAY_CHILDREN = 2


class OverlaySpecError(ValueError):
    """叠加参数非法 (API 层转 400)。消息直接面向用户, 不复用内部异常文案。"""


@dataclass(frozen=True, slots=True)
class OverlaySpec:
    """一次回测的临时叠加定义 (纯数据, 不含策略对象)。"""

    children: tuple[CompositeChild, ...]
    merge_mode: str = "intersect"
    min_confirm: int = 0

    @property
    def strategy_id(self) -> str:
        return EPHEMERAL_OVERLAY_STRATEGY_ID

    @property
    def weights(self) -> tuple[tuple[str, float], ...]:
        """(strategy_id, weight) 序列, 顺序与 children 一致。"""
        return tuple((c.strategy_id, c.weight) for c in self.children)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "merge_mode": self.merge_mode,
            "min_confirm": self.min_confirm,
            "children": [
                {"strategy_id": c.strategy_id, "weight": c.weight} for c in self.children
            ],
        }


def parse_overlay_children(
    raw: Any,
    *,
    max_children: int = MAX_COMPOSITE_CHILDREN,
) -> tuple[CompositeChild, ...]:
    """把请求里的 `overlay_children` 解析成 CompositeChild 序列, 非法即抛。

    只负责结构与权重; 存在性/嵌套/资产兼容性交给引擎的引用校验。
    """
    if not isinstance(raw, list) or not raw:
        raise OverlaySpecError("overlay_children 必须是非空数组")
    if len(raw) < MIN_OVERLAY_CHILDREN:
        raise OverlaySpecError(
            f"叠加至少需要 {MIN_OVERLAY_CHILDREN} 个子策略; "
            "只跑一个策略请直接用单选, 不要走叠加"
        )
    if len(raw) > max_children:
        raise OverlaySpecError(f"叠加子策略最多 {max_children} 个, 当前 {len(raw)} 个")
    children: list[CompositeChild] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise OverlaySpecError(f"overlay_children[{i}] 必须是对象")
        cid = item.get("strategy_id")
        if not isinstance(cid, str) or not cid.strip():
            raise OverlaySpecError(f"overlay_children[{i}] 缺少 strategy_id")
        cid = cid.strip()
        if cid in seen:
            raise OverlaySpecError(f"overlay_children[{i}] 子策略 {cid!r} 重复")
        seen.add(cid)
        weight_raw = item.get("weight", 1.0)
        if weight_raw is None:
            weight_raw = 1.0
        try:
            weight = float(weight_raw)
        except (TypeError, ValueError) as exc:
            raise OverlaySpecError(f"overlay_children[{i}] weight 必须是数字") from exc
        if weight < 0:
            raise OverlaySpecError(f"overlay_children[{i}] weight 不能为负")
        children.append(CompositeChild(strategy_id=cid, weight=weight))
    return tuple(children)


def build_overlay_spec(
    *,
    overlay_children: Any,
    merge_mode: str | None = None,
    min_confirm: int | None = None,
) -> OverlaySpec:
    """从请求字段构造 OverlaySpec。字段非法时抛 OverlaySpecError。"""
    children = parse_overlay_children(overlay_children)
    try:
        mode = normalize_merge_mode(merge_mode)
    except ValueError as exc:
        raise OverlaySpecError(
            f"merge_mode 仅支持 {'、'.join(MERGE_MODES)}"
        ) from exc
    try:
        confirm = int(min_confirm or 0)
    except (TypeError, ValueError) as exc:
        raise OverlaySpecError("min_confirm 必须是整数") from exc
    if confirm < 0 or confirm > len(children):
        raise OverlaySpecError(
            f"min_confirm 必须在 0~{len(children)} 之间, 当前 {confirm}"
        )
    return OverlaySpec(children=children, merge_mode=mode, min_confirm=confirm)


def _overlay_meta(spec: OverlaySpec) -> dict[str, Any]:
    """合成 StrategyDef 的 META。参数名与落盘 composite 保持一致。"""
    return {
        "id": spec.strategy_id,
        "name": "叠加(当次多选)",
        "description": "回测当次多选的临时叠加, 不落盘",
        "asset_types": ["stock"],
        "timeframes": ["1d"],
        "params": [
            {
                "id": "merge_mode",
                "type": "select",
                "options": list(MERGE_MODES),
                "default": spec.merge_mode,
            },
            {"id": "min_confirm", "type": "int", "default": spec.min_confirm},
        ],
        "children": [
            {"strategy_id": c.strategy_id, "weight": c.weight} for c in spec.children
        ],
        # 与落盘 composite 的区别标记: 前端/日志可据此区分"当次"与"已存"。
        "ephemeral": True,
    }


def compose_overlay_strategy_def(base: StrategyDef, spec: OverlaySpec) -> StrategyDef:
    """以落盘子策略的 META 为模板, 合成一个 composite 形状的 StrategyDef。

    用 `replace` 而不是手工构造: StrategyDef 字段多且会继续长, 手工构造一旦漏字段
    就是 AttributeError, 或者更糟 —— 默认值沉默生效。模板取自首个子策略, 只是为了让
    asset_types/timeframes 落到合法值; 真正的兼容性校验交给引擎的引用校验。
    """
    meta = {**base.meta, **_overlay_meta(spec)}
    return replace(
        base,
        meta=meta,
        source="composite",
        execution_backend="composite",
        composite=CompositeSpec(children=spec.children),
        matrix_strategy=None,
        filter_fn=None,
        filter_history_fn=None,
        filter_minute_history_fn=None,
        minute_daily_bars=0,
        file_path=None,
    )


def resolve_overlay_children(
    engine: StrategyEngine,
    spec: OverlaySpec,
) -> list[tuple[str, StrategyDef]]:
    """按 spec 顺序取出子策略定义; 缺失或嵌套即抛 OverlaySpecError。"""
    resolved: list[tuple[str, StrategyDef]] = []
    for child in spec.children:
        cid = child.strategy_id
        if not engine.has(cid):
            raise OverlaySpecError(f"子策略 {cid!r} 不存在")
        child_def = engine.get(cid)
        if (
            getattr(child_def, "source", "") == "composite"
            or getattr(child_def, "execution_backend", "") == "composite"
            or cid.startswith("composite_")
        ):
            raise OverlaySpecError(f"子策略 {cid!r} 是叠加策略, 叠加不支持再叠叠加")
        resolved.append((cid, child_def))
    return resolved


def register_overlay(engine: StrategyEngine, spec: OverlaySpec) -> StrategyDef:
    """在给定引擎上合成并注册 ephemeral 叠加策略, 返回合成后的定义。

    只改该引擎实例的注册表 (不写盘, 不碰其它引擎)。重复注册同一 id 时覆盖,
    便于同一进程内多次回测复用同一引擎 (子进程场景每次都是新引擎)。
    """
    resolved = resolve_overlay_children(engine, spec)
    overlay = compose_overlay_strategy_def(resolved[0][1], spec)
    # 复用引擎既有的引用校验 (存在性/禁嵌套/资产与周期兼容/数量上限), 不另写一份。
    # 已存在的同 id 临时定义先摘掉, 否则校验会把它自己当成子策略。
    probe_map = {cid: cdef for cid, cdef in resolved}
    error = engine._validate_composite_references(
        spec.strategy_id, overlay, probe_map
    )
    if error is not None:
        raise OverlaySpecError(f"叠加子策略不可用: {error}")
    # 引擎没有 register API, 且刻意不新增 —— 新增会被当成"支持运行期注册任意策略"
    # 的口子。这里只写当次引擎实例的注册表, engine.get/has 立即生效。
    engine._strategies[spec.strategy_id] = overlay
    return overlay


def is_overlay_requested(overlay_children: Any) -> bool:
    """请求是否要走 ephemeral overlay 路径 (只看有没有传, 不看是否合法)。"""
    return bool(overlay_children)
