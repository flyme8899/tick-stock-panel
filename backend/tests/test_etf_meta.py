"""ETF 名称参考表：前缀、全称、宽基指数和失败时保留上一版。"""
from __future__ import annotations

import io
import json
from xml.sax.saxutils import escape
from zipfile import ZipFile

import polars as pl

from app.fund_flow import etf_meta, jobs, query, store
from app.fund_flow.circuit import CircuitBreaker
from app.fund_flow.sources import SourceClients


def _row(code: str, name: str, shares: float, day: str) -> dict:
    return {
        "symbol": code,
        "code": code,
        "name": name,
        "trade_date": day,
        "shares": shares,
        "source": "akshare",
    }


def _write_shares(root, day: str, rows: list[tuple[str, str, float]]) -> None:
    store.write_rows(root, "etf_shares", day, [
        _row(code, name, shares, day) for code, name, shares in rows
    ])


def _meta(**overrides) -> dict:
    row = {
        "code": "510300",
        "full_name": "沪深300ETF华泰柏瑞",
        "short_name": "300ETF",
        "index_code": "000300",
        "index_name": "沪深300指数",
        "company": "华泰柏瑞基金管理有限公司",
        "source": "sse",
    }
    row.update(overrides)
    return row


def test_status_prefix_is_stripped_only_for_shanghai_codes(tmp_path) -> None:
    day = "2026-10-09"
    _write_shares(tmp_path, day, [
        ("515180", "XD100红", 1.0),
        ("562420", "N卫星PA", 1.0),
        ("563660", "XDA500银", 1.0),
        ("530880", "XR上证红", 1.0),
        ("510880", "DR红利", 1.0),
        ("511010", "UC国债", 1.0),
        ("159915", "N创业板ETF易方达", 2.0),
    ])
    items = {row["code"]: row for row in query.etf_shares(data_dir=tmp_path)["items"]}
    assert items["515180"]["name"] == "100红"
    assert items["562420"]["name"] == "卫星PA"
    assert items["563660"]["name"] == "A500银"
    assert items["530880"]["name"] == "上证红"
    assert items["510880"]["name"] == "红利"
    assert items["511010"]["name"] == "国债"
    assert items["159915"]["name"] == "N创业板ETF易方达"
    assert items["515180"]["short_name"] is None
    saved = store.read_partition(tmp_path, "etf_shares", day)
    assert saved.filter(pl.col("code") == "515180")["name"][0] == "XD100红"


def test_shanghai_short_name_maps_to_full_name_without_rewriting_partition(tmp_path) -> None:
    day = "2026-10-09"
    _write_shares(tmp_path, day, [("510300", "300ETF", 10.0), ("588000", "科创50", 4.0)])
    etf_meta._write(tmp_path, [
        _meta(),
        _meta(
            code="588000",
            full_name="科创50ETF华夏",
            short_name="科创50",
            index_code="000688",
            index_name="上证科创板50成份指数",
            company="华夏基金管理有限公司",
        ),
    ])
    board = {row["code"]: row for row in query.board(tmp_path)["etf_shares"]["items"]}
    direct = {row["code"]: row for row in query.etf_shares(data_dir=tmp_path)["items"]}
    assert board["510300"]["name"] == "沪深300ETF华泰柏瑞"
    assert board["510300"]["short_name"] == "300ETF"
    assert board["510300"]["broad"] == "沪深300"
    assert direct["588000"]["name"] == "科创50ETF华夏"
    assert direct["588000"]["short_name"] == "科创50"
    assert store.read_partition(tmp_path, "etf_shares", day).filter(
        pl.col("code") == "510300",
    )["name"][0] == "300ETF"


def test_588020_is_not_star50_and_159656_is_not_csi300(tmp_path) -> None:
    prev, day = "2026-10-08", "2026-10-09"
    rows = [
        ("588020", "科创50E", 8.0),
        ("159656", "沪深300成长ETF万家", 6.0),
        ("588000", "科创50", 5.0),
        ("159919", "沪深300ETF嘉实", 4.0),
    ]
    _write_shares(tmp_path, prev, rows)
    _write_shares(tmp_path, day, [(code, name, shares + 1) for code, name, shares in rows])
    etf_meta._write(tmp_path, [
        _meta(
            code="588020",
            full_name="科创成长ETF易方达",
            short_name="科创50E",
            index_code="000690",
            index_name="上证科创板成长指数",
            company="易方达基金管理有限公司",
        ),
        _meta(
            code="588000",
            full_name="科创50ETF华夏",
            short_name="科创50",
            index_code="000688",
            index_name="上证科创板50成份指数",
        ),
    ])
    items = {row["code"]: row for row in query.board(tmp_path)["etf_shares"]["items"]}
    assert items["588020"]["name"] == "科创成长ETF易方达"
    assert items["588020"]["short_name"] == "科创50E"
    assert items["588020"]["broad"] is None
    assert items["159656"]["name"] == "沪深300成长ETF万家"
    assert items["159656"]["broad"] is None
    assert items["588000"]["broad"] == "科创50"
    assert items["159919"]["broad"] == "沪深300"

    # 参考表缺失时，短简称「科创50E」本身也不能靠子串算成科创50。
    etf_meta.meta_path(tmp_path).unlink()
    fallback = {row["code"]: row for row in query.board(tmp_path)["etf_shares"]["items"]}
    assert fallback["588020"]["name"] == "科创50E"
    assert fallback["588020"]["broad"] is None
    assert fallback["588020"]["short_name"] is None
    assert fallback["159656"]["broad"] is None


def test_missing_meta_falls_back_and_keeps_plain_names(tmp_path) -> None:
    _write_shares(tmp_path, "2026-10-09", [
        ("510300", "XD沪深300ETF", 3.0),
        ("159915", "创业板ETF易方达", 2.0),
    ])
    items = {row["code"]: row for row in query.etf_shares(data_dir=tmp_path)["items"]}
    assert items["510300"]["name"] == "沪深300ETF"
    assert items["510300"]["short_name"] is None
    assert items["159915"]["name"] == "创业板ETF易方达"
    board = {row["code"]: row for row in query.board(tmp_path)["etf_shares"]["items"]}
    assert board["510300"]["broad"] == "沪深300"
    assert board["159915"]["broad"] == "创业板"


def _xlsx(rows: list[list[str]]) -> bytes:
    sheet_rows = []
    for r_index, row in enumerate(rows, start=1):
        cells = []
        for c_index, value in enumerate(row):
            col = chr(ord("A") + c_index)
            cells.append(
                f'<c r="{col}{r_index}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            )
        sheet_rows.append(f'<row r="{r_index}">{"".join(cells)}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", "<Types></Types>")
        package.writestr("xl/workbook.xml", workbook)
        package.writestr("xl/worksheets/sheet1.xml", sheet)
        package.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
    return buffer.getvalue()


def _sse_page(rows: list[dict], *, page_count: int = 1) -> bytes:
    return json.dumps({
        "result": rows,
        "pageHelp": {"pageCount": page_count, "total": len(rows)},
    }).encode()


def test_refresh_merges_sources_and_keeps_previous_when_a_source_fails(tmp_path) -> None:
    sse_row = {
        "fundCode": "588020",
        "secNameFull": "科创成长ETF易方达",
        "fundAbbr": "科创50E",
        "INDEX_CODE": "000690",
        "INDEX_NAME": "上证科创板成长指数",
        "companyName": "易方达基金管理有限公司",
    }
    szse = _xlsx([
        ["基金代码", "基金简称", "基金类别", "基金管理人"],
        ["159915", "创业板ETF易方达", "ETF", "易方达基金管理有限公司"],
        ["160706", "沪深300LOF", "LOF", "嘉实基金管理有限公司"],
    ])
    sina = (
        "/*<script>*/IO.XSRV2.CallbackList['da_yPT46_Ll7K6WD']("
        '[{"symbol":"sh510300","code":"510300","name":"沪深300ETF华泰柏瑞"},'
        '{"symbol":"sz159915","code":"159915","name":"不该覆盖深市"}]'
        ");"
    ).encode()

    def get(url, *, params=None, headers=None):
        if "commonSoaQuery.do" in url:
            assert params["sqlId"] == "FUND_LIST"
            assert params["fundType"] == "00"
            return _sse_page([sse_row])
        if "ShowReport" in url:
            assert params["CATALOGID"] == "1105"
            return szse
        if "etf_hq_fund" in (params or {}).get("node", ""):
            return sina
        raise AssertionError(url)

    errors = etf_meta.refresh(tmp_path, get=get)
    assert errors == []
    saved = {row["code"]: row for row in etf_meta.load_rows(tmp_path)}
    assert saved["588020"]["full_name"] == "科创成长ETF易方达"
    assert saved["588020"]["short_name"] == "科创50E"
    assert saved["588020"]["index_code"] == "000690"
    assert saved["588020"]["source"] == "sse"
    assert saved["159915"]["full_name"] == "创业板ETF易方达"
    assert saved["159915"]["source"] == "szse"
    assert "160706" not in saved
    assert saved["510300"]["full_name"] == "沪深300ETF华泰柏瑞"
    assert saved["510300"]["source"] == "sina"

    def fail_szse(url, *, params=None, headers=None):
        if "ShowReport" in url:
            raise RuntimeError("深交所超时")
        if "commonSoaQuery.do" in url:
            return _sse_page([{
                **sse_row,
                "secNameFull": "科创成长ETF易方达更新",
            }])
        if "etf_hq_fund" in (params or {}).get("node", ""):
            raise RuntimeError("新浪超时")
        raise AssertionError(url)

    again = etf_meta.refresh(tmp_path, get=fail_szse)
    assert any("szse" in item for item in again)
    assert any("sina" in item for item in again)
    kept = {row["code"]: row for row in etf_meta.load_rows(tmp_path)}
    assert kept["159915"]["full_name"] == "创业板ETF易方达"
    assert kept["510300"]["source"] == "sina"
    assert kept["588020"]["full_name"] == "科创成长ETF易方达更新"
    assert etf_meta.meta_path(tmp_path).stat().st_size > 0


def test_refresh_failure_does_not_clear_previous_snapshot(tmp_path) -> None:
    etf_meta._write(tmp_path, [_meta()])

    def get(url, *, params=None, headers=None):
        raise RuntimeError("网络不可用")

    errors = etf_meta.refresh(tmp_path, get=get)
    assert len(errors) == 3
    kept = etf_meta.load_index(tmp_path)
    assert kept["510300"]["full_name"] == "沪深300ETF华泰柏瑞"


def test_sync_etf_shares_refreshes_meta_without_dropping_shares(tmp_path) -> None:
    seen = {"n": 0}

    def refresh(data_dir, *, get=None):
        seen["n"] += 1
        etf_meta._write(data_dir, [_meta()])
        return ["etf_meta sina: 超时"]

    from app.fund_flow import jobs as jobs_mod

    original = jobs_mod.etf_meta.refresh
    jobs_mod.etf_meta.refresh = refresh
    try:
        result = jobs.sync_etf_shares(
            tmp_path,
            SourceClients(ak_call=_shares_ak),
            trade_date="2026-10-09",
            breaker=CircuitBreaker("akshare", clock=lambda: 0),
            sleeper=lambda _seconds: None,
        )
    finally:
        jobs_mod.etf_meta.refresh = original
    assert seen["n"] == 1
    assert result.wrote == 2
    assert any("sina" in item for item in result.errors)
    saved = store.read_partition(tmp_path, "etf_shares", "2026-10-09")
    assert saved["name"].to_list() == ["300ETF", "创业板ETF"]
    shown = {row["code"]: row for row in query.etf_shares(data_dir=tmp_path)["items"]}
    assert shown["510300"]["name"] == "沪深300ETF华泰柏瑞"
    assert shown["510300"]["short_name"] == "300ETF"


def _shares_ak(name, **kwargs):
    if name == "fund_etf_scale_sse":
        return [{
            "基金代码": "510300",
            "基金简称": "300ETF",
            "统计日期": "2026-10-09",
            "基金份额": 100.0,
        }]
    if name == "fund_scale_daily_szse":
        return [{
            "基金代码": "159915",
            "基金简称": "创业板ETF",
            "日期": "2026-10-09",
            "基金份额": 80.0,
        }]
    raise AssertionError(name)
