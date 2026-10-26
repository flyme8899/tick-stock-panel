"""Kronos S3 产物的扩展表 schema（单一真源）。

上游 S3 研究包的 ``features.csv`` 表头（已核，见 §I）：

    date,symbol,n_paths,seed,agree_h5,dir_entropy_h5,p90_p10_h5,std_h5,mean_h5,
    median_h5,agree_h20,dir_entropy_h20,p90_p10_h20,std_h20,mean_h20,median_h20

这里只取**分布列**进主列：多路径采样给出的是分布，`agree`（路径方向一致率）、
`dir_entropy`（方向熵）、`p90_p10`（分位带宽）、`std` / `mean` / `median`。
单点 `pred_ret` 只作**可选研究对照列**——S2 主评估不通过（h20 Rank IC NW
t=1.984 < 2.498），点预测不能单独当信号依据。

字段名一律 ASCII：``ext_factors`` 会把数值列注册成 ``ext_<config>_<field>``，
DSL 公式标识符是 ASCII-only，中文 id 会被跳过注册。这里天然全 ASCII。

缺日不填 0：导入时某天没有分区就不写那天，宁可在缺口报告里报出来。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

CONFIG_ID: Final = "kronos_pred"
"""扩展表 id。落地为 ``data/ext_data/kronos_pred/``，列名前缀 ``ext_kronos_pred_``。"""

CONFIG_LABEL: Final = "Kronos 预测分布（S3 · 仅供研究）"
CONFIG_DESCRIPTION: Final = (
    "Kronos-mini 多路径采样的分布特征（S3 研究包）。仅作风险标签候选，"
    "未过费后 + 样本外门禁，不进买卖信号默认因子集。"
)

# 分布列族：(指标, 中文标签, 说明)。horizon 单独拼 h5 / h20。
DISTRIBUTION_METRICS: Final = (
    ("agree", "方向一致率", "多路径中与主方向一致的占比"),
    ("dir_entropy", "方向熵", "方向分布熵，越大越分歧"),
    ("p90_p10", "分位带宽", "90% 分位减 10% 分位，路径离散度"),
    ("std", "路径标准差", "多路径收益标准差"),
    ("mean", "路径均值", "多路径收益均值"),
    ("median", "路径中位数", "多路径收益中位数"),
)

HORIZONS: Final = (5, 20)

# 元数据列（不进因子注册，只作可追溯）
META_FIELDS: Final = (
    ("n_paths", "采样路径数"),
    ("seed", "随机种子"),
)

# 可选研究对照列：来自 predictions.csv，**不得**单独作为可做信号依据。
OPTIONAL_FIELDS: Final = (
    ("pred_ret_h5", "点预测 h5 收益（研究对照）"),
    ("pred_ret_h20", "点预测 h20 收益（研究对照）"),
)

SOURCE_REF: Final = "kronos_s3_mini_n20_20261021"
"""上游研究包名，进 ``source_refs`` 便于门禁审计。"""

SCHEMA_VERSION: Final = "1"
"""schema 版本。列族变化时递增，status 会带出去。"""


@dataclass(frozen=True)
class ExtColumn:
    name: str
    dtype: str
    label: str

    def to_dict(self) -> dict:
        return {"name": self.name, "dtype": self.dtype, "label": self.label}


def distribution_columns() -> list[ExtColumn]:
    """主分布列：6 个指标 × 2 个 horizon = 12 列。"""
    return [
        ExtColumn(f"{metric}_h{horizon}", "float", f"{label} h{horizon}")
        for metric, label, _ in DISTRIBUTION_METRICS
        for horizon in HORIZONS
    ]


def optional_columns() -> list[ExtColumn]:
    return [ExtColumn(name, "float", label) for name, label in OPTIONAL_FIELDS]


def meta_columns() -> list[ExtColumn]:
    # n_paths / seed 是整数计数，按 int 落，不参与因子注册（dtype 仍是数值，
    # 但字段名带 n_ 前缀的计数对 IC 无意义，导入端不写进主列，仅保留可追溯）。
    return [ExtColumn(name, "int", label) for name, label in META_FIELDS]


def schema_columns() -> list[ExtColumn]:
    """完整列清单：主分布列 + 元数据列 + 可选对照列。"""
    return [*distribution_columns(), *meta_columns(), *optional_columns()]


def distribution_field_names() -> tuple[str, ...]:
    return tuple(column.name for column in distribution_columns())


def primary_fields() -> tuple[str, ...]:
    """``features.csv`` 里本仓要消费的列（分布 + 元数据）。"""
    return (*distribution_field_names(), *(name for name, _ in META_FIELDS))


def source_refs() -> dict:
    return {
        "package": SOURCE_REF,
        "features": "features.csv",
        "predictions": "predictions.csv",
        "run_meta": "run_meta.json",
    }


def schema_payload() -> dict:
    return {
        "config_id": CONFIG_ID,
        "label": CONFIG_LABEL,
        "mode": "timeseries",
        "schema_version": SCHEMA_VERSION,
        "columns": [column.to_dict() for column in schema_columns()],
        "distribution_fields": list(distribution_field_names()),
        "optional_fields": [name for name, _ in OPTIONAL_FIELDS],
        "note": "pred_ret_h5/h20 仅为研究对照列，不得单独作为信号依据。",
    }
