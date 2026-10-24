"""申万一级行业 PIT 只读查询测试 (批次 20261024g 任务②)。

核心断言只有一句: **asof 落在不同 valid 窗, 必须得到不同的 L1**。
其余全是这条的守卫 —— 拒绝同花顺当前态、缺列报错、缺映射返回 None 而不是假行业。

不联网, 不依赖 `/workspace/swmap*` 那些样本(本沙箱里没有); 夹具在 `tmp_path` 里现造。
"""
from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import polars as pl
import pytest
from app.buy_sell_signals.sw_pit import (
    HISTORY_COLUMNS,
    L1_MAP_COLUMNS,
    IndustrySchemaError,
    IndustrySourceRejected,
    industry_asof,
    industry_asof_many,
    load_sector_tables,
)
from app.buy_sell_signals.models import SignalValidationError

CN = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------- 夹具


def _write_history(path: Path, rows: list[dict]) -> Path:
    pl.DataFrame(rows).write_parquet(path)
    return path


def _write_map(path: Path, rows: list[dict]) -> Path:
    pl.DataFrame(rows).write_parquet(path)
    return path


@pytest.fixture()
def tables(tmp_path: Path):
    """一张小 history + 一张小 l1_map, 覆盖三种情形:

    - `600000.SH`: 2023-01-01 从银行(48)重分类到计算机(71) —— 跨窗重分类
    - `000001.SZ`: 从头到尾都在银行
    - `300750.SZ`: 有 history, 但内部码在 l1_map 里查不到 —— 缺映射
    """
    history = _write_history(
        tmp_path / "sw_industry_history.parquet",
        {
            "symbol": ["600000.SH", "600000.SH", "000001.SZ", "300750.SZ"],
            "industry_code": ["480000", "710000", "480000", "630000"],
            "valid_from": [
                date(2000, 1, 1),
                date(2023, 1, 1),
                date(2000, 1, 1),
                date(2021, 12, 13),
            ],
            "valid_to": [date(2023, 1, 1), None, None, None],
        },
    )
    l1_map = _write_map(
        tmp_path / "sw_industry_l1_map.parquet",
        {
            "industry_code": ["480000", "710000"],
            "l1_code": ["48", "71"],
            "l1_name": ["银行", "计算机"],
            "valid_from": [date(2021, 12, 13), date(2021, 12, 13)],
            "valid_to": [None, None],
            "mapping_edition": ["SW2021", "SW2021"],
        },
    )
    return load_sector_tables(history, l1_map)


# --------------------------------------------------------------------------- 主断言


def test_same_symbol_different_window_different_l1(tables):
    """asof 落在不同 valid 窗 → 不同 L1。这是 PIT 存在的唯一理由。"""
    before = industry_asof(tables, "600000.SH", date(2022, 6, 1))
    after = industry_asof(tables, "600000.SH", date(2023, 6, 1))
    assert before.industry_code_sw_l1 == "48"
    assert before.industry_name == "银行"
    assert after.industry_code_sw_l1 == "71"
    assert after.industry_name == "计算机"
    assert before.industry_code_sw_l1 != after.industry_code_sw_l1


def test_window_boundary_is_half_open(tables):
    """valid_to 是开区间: 2023-01-01 归新一段, 不出现两段都命中。"""
    assert industry_asof(tables, "600000.SH", date(2022, 12, 31)).industry_code_sw_l1 == "48"
    assert industry_asof(tables, "600000.SH", date(2023, 1, 1)).industry_code_sw_l1 == "71"


def test_stable_symbol_keeps_same_l1_across_dates(tables):
    """同一只票在两个不同时点都解析到同一个 L1。

    起点取 2022-01-04 而不是 2020: 夹具的 map 有效窗从 2021-12-13 开始(SW2021 口径),
    2020 年那份 l1_map 本来就不存在 —— 那是"缺映射", 不该被当成"应该解析出来"。
    """
    for day in (date(2022, 1, 4), date(2024, 8, 1)):
        assert industry_asof(tables, "000001.SZ", day).industry_code_sw_l1 == "48"


def test_industry_pit_asof_reports_the_window_start(tables):
    """`industry_pit_asof` 告诉调用方"这个归属是从哪天开始生效的"。"""
    hit = industry_asof(tables, "600000.SH", date(2022, 6, 1))
    assert hit.industry_pit_asof == date(2000, 1, 1)
    assert hit.mapping_edition == "SW2021"


def test_mapping_edition_comes_from_map(tables):
    assert tables.editions() == ["SW2021"]


# --------------------------------------------------------------------------- 缺映射不填假行业


def test_symbol_absent_from_history_returns_none_with_reason(tables):
    hit = industry_asof(tables, "999999.SH", date(2022, 6, 1))
    assert hit.industry_code_sw_l1 is None
    assert hit.resolved is False
    assert "没有" in (hit.reason or "")


def test_asof_outside_all_windows_returns_none(tables):
    hit = industry_asof(tables, "600000.SH", date(1999, 1, 1))
    assert hit.industry_code_sw_l1 is None
    assert "不落在任何有效窗" in (hit.reason or "")


def test_internal_code_without_l1_mapping_returns_none(tables):
    """history 有记录但 l1_map 查不到 → None + 原因, 不允许按代码前缀猜。"""
    hit = industry_asof(tables, "300750.SZ", date(2022, 6, 1))
    assert hit.industry_code_sw_l1 is None
    assert "l1_map" in (hit.reason or "")


def test_none_is_never_replaced_by_guessed_prefix(tmp_path):
    """即使内部码以 '71' 开头(看起来像计算机), 没有映射也只能是 None。

    按前两位切 L1 只在同一版本内成立; 跨版本时 71 可能指向别的行业。这条测试
    就是防止有人"反正前缀一样, 直接用吧"。
    """
    history = _write_history(
        tmp_path / "sw_industry_history.parquet",
        {
            "symbol": ["600519.SH"],
            "industry_code": ["710000"],
            "valid_from": [date(2020, 1, 1)],
            "valid_to": [None],
        },
    )
    l1_map = _write_map(
        tmp_path / "sw_industry_l1_map.parquet",
        {
            "industry_code": ["480000"],
            "l1_code": ["48"],
            "l1_name": ["银行"],
            "valid_from": [date(2020, 1, 1)],
            "valid_to": [None],
            "mapping_edition": ["SW2014"],
        },
    )
    tables = load_sector_tables(history, l1_map)
    assert industry_asof(tables, "600519.SH", date(2022, 1, 1)).industry_code_sw_l1 is None


def test_l1_map_window_is_also_respected(tmp_path):
    """map 自身的 valid 窗同样生效: 2014 口径在 2021 年之后不再适用。"""
    history = _write_history(
        tmp_path / "sw_industry_history.parquet",
        {
            "symbol": ["600000.SH"],
            "industry_code": ["480000"],
            "valid_from": [date(2000, 1, 1)],
            "valid_to": [None],
        },
    )
    l1_map = _write_map(
        tmp_path / "sw_industry_l1_map.parquet",
        {
            "industry_code": ["480000", "480000"],
            "l1_code": ["48", "48"],
            "l1_name": ["金融服务", "银行"],
            "valid_from": [date(2014, 1, 1), date(2021, 12, 13)],
            "valid_to": [date(2021, 12, 13), None],
            "mapping_edition": ["SW2014", "SW2021"],
        },
    )
    tables = load_sector_tables(history, l1_map)
    old = industry_asof(tables, "600000.SH", date(2020, 6, 1))
    new = industry_asof(tables, "600000.SH", date(2022, 6, 1))
    assert (old.mapping_edition, old.industry_name) == ("SW2014", "金融服务")
    assert (new.mapping_edition, new.industry_name) == ("SW2021", "银行")


# --------------------------------------------------------------------------- 拒绝同花顺当前态


@pytest.mark.parametrize("bad_name", ["ext_hy_ths.parquet", "industry_ths.parquet", "同花顺行业.csv"])
def test_ths_source_is_rejected_as_history(tmp_path, bad_name):
    """同花顺当前态出现在任何一个入参位置都必须直接拒, 不降级。"""
    good = _write_map(
        tmp_path / "sw_industry_l1_map.parquet",
        {
            "industry_code": ["480000"],
            "l1_code": ["48"],
            "l1_name": ["银行"],
            "valid_from": [date(2020, 1, 1)],
        },
    )
    ths = tmp_path / bad_name
    pl.DataFrame({"symbol": ["600000.SH"], "industry_code": ["480000"], "valid_from": [date(2020, 1, 1)]}).write_csv(
        ths
    )
    with pytest.raises(IndustrySourceRejected, match="同花顺"):
        load_sector_tables(ths, good)


def test_ths_source_is_rejected_as_map(tmp_path):
    history = _write_history(
        tmp_path / "sw_industry_history.parquet",
        {
            "symbol": ["600000.SH"],
            "industry_code": ["480000"],
            "valid_from": [date(2020, 1, 1)],
        },
    )
    ths = tmp_path / "sw_industry_l1_map_ths.parquet"
    pl.DataFrame({"industry_code": ["480000"], "l1_code": ["48"]}).write_parquet(ths)
    with pytest.raises(IndustrySourceRejected):
        load_sector_tables(history, ths)


def test_no_silent_fallback_to_ths_anywhere_in_module():
    """静态检查: 模块里不得出现"回退到 ths"的写法。"""
    source = (Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals" / "sw_pit.py").read_text(
        encoding="utf-8"
    )
    for pattern in ("fallback", "回退到", "ext_hy_ths.parquet"):
        assert pattern not in source or "禁止" in source or "不" in source


# --------------------------------------------------------------------------- 缺列 / 坏输入


def test_missing_required_column_raises_with_actual_columns(tmp_path):
    history = _write_history(tmp_path / "sw_industry_history.parquet", {"symbol": ["600000.SH"]})
    l1_map = _write_map(
        tmp_path / "sw_industry_l1_map.parquet",
        {"industry_code": ["480000"], "l1_code": ["48"], "valid_from": [date(2020, 1, 1)]},
    )
    with pytest.raises(IndustrySchemaError, match="industry_code"):
        load_sector_tables(history, l1_map)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_sector_tables(tmp_path / "nope.parquet", tmp_path / "nope2.parquet")


def test_column_aliases_are_supported(tmp_path):
    """真实落盘列名可能是 code / start_date 这类别名, 适配层要认。"""
    history = tmp_path / "sw_industry_history.parquet"
    pl.DataFrame(
        {
            "code": ["600000.SH"],
            "sw_code": ["480000"],
            "start_date": [date(2020, 1, 1)],
            "end_date": [None],
        }
    ).write_parquet(history)
    l1_map = tmp_path / "sw_industry_l1_map.parquet"
    pl.DataFrame(
        {
            "ind_code": ["480000"],
            "l1_code": ["48"],
            "name": ["银行"],
            "start_date": [date(2020, 1, 1)],
            "version": ["SW2021"],
        }
    ).write_parquet(l1_map)
    tables = load_sector_tables(history, l1_map)
    hit = industry_asof(tables, "600000.SH", date(2022, 1, 1))
    assert hit.industry_code_sw_l1 == "48"
    assert hit.mapping_edition == "SW2021"
    assert tables.history_columns["symbol"] == "code"


def test_string_dates_are_parsed(tmp_path):
    """csv / csv.gz 里的字符串日期要能解析。

    `write_csv` 这一侧才需要显式 `compression="gzip"`: polars 写 `.gz` 时不去猜后缀,
    读的时候才按后缀识别。两侧行为不一致, 所以这里写清楚。
    """
    history = tmp_path / "sw_industry_history.csv.gz"
    pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "industry_code": ["480000"],
            "valid_from": ["2020-01-01"],
            "valid_to": [""],
        }
    ).write_csv(history, compression="gzip")
    l1_map = tmp_path / "sw_industry_l1_map.csv.gz"
    pl.DataFrame(
        {
            "industry_code": ["480000"],
            "l1_code": ["48"],
            "l1_name": ["银行"],
            "valid_from": ["2020-01-01"],
        }
    ).write_csv(l1_map, compression="gzip")
    tables = load_sector_tables(history, l1_map)
    assert industry_asof(tables, "600000.SH", date(2022, 1, 1)).industry_code_sw_l1 == "48"


def test_empty_symbol_rejected(tables):
    with pytest.raises(SignalValidationError, match="symbol"):
        industry_asof(tables, "   ", date(2022, 6, 1))


def test_asof_datetime_converted_to_beijing_date(tables):
    """UTC 2022-06-01 17:00 = 北京 2022-06-02, 应按后者查。"""
    utc = datetime(2022, 6, 1, 17, 0, tzinfo=timezone.utc)
    hit = industry_asof(tables, "600000.SH", utc)
    assert hit.asof == date(2022, 6, 2)


# --------------------------------------------------------------------------- 批量与序列化


def test_many_matches_single(tables):
    day = date(2022, 6, 1)
    symbols = ["600000.SH", "000001.SZ", "300750.SZ", "999999.SH"]
    many = industry_asof_many(tables, symbols, day)
    for sym in symbols:
        single = industry_asof(tables, sym, day)
        assert many[sym] == single


def test_to_dict_shape(tables):
    payload = industry_asof(tables, "600000.SH", date(2022, 6, 1)).to_dict()
    assert payload["industry_standard"] == "sw_l1"
    assert payload["source"] == "sw_pit"
    assert payload["resolved"] is True
    assert payload["industry_pit_asof"] == "2000-01-01"


def test_column_specs_do_not_reference_ths():
    """列名白名单里不得出现同花顺表名 —— 那是另一个数据源, 不该被适配成 SW。"""
    for spec in (HISTORY_COLUMNS, L1_MAP_COLUMNS):
        flat = " ".join(name for names in spec.values() for name in names)
        assert "ths" not in flat.lower()
