"""收盘后热门榜切到下一场：盘中和隔夜一起排，待开盘不扣分，未来时间戳不进榜。"""
from __future__ import annotations

from datetime import date, datetime

from app.market_time import CN_TZ
from app.news.hot_events import build_top_hot_events, display_view_key
from app.news.service import get_store, public_hot_event, reset_store_for_tests
from app.services.picker import _confirmation_brief

FRIDAY = date(2026, 10, 9)
SATURDAY = date(2026, 10, 10)
SUNDAY = date(2026, 10, 11)
MONDAY = date(2026, 10, 12)
TUESDAY = date(2026, 10, 6)
WEDNESDAY = date(2026, 10, 7)
THURSDAY = date(2026, 10, 8)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ)


def _insert(source_id: str, published: datetime, title: str, concept: str = "") -> None:
    mentions = [("sector", concept, concept, "", "structured")] if concept else []
    status = get_store().insert_item(
        source="cls",
        source_id=source_id,
        published_at=published,
        author="",
        title=title,
        clean_text=title,
        raw=None,
        content_hash=source_id,
        url="",
        level="",
        media_ids=[],
        extra=None,
        mentions=mentions,
    )
    assert status == "inserted"


def _insert_space_timestamp(source_id: str, published_at: str, title: str) -> None:
    """空格分隔的时间字符串序会排在同一天的 ISO 时间前面，查询窗口挡不住。"""
    store = get_store()
    store._conn.execute(
        """
        INSERT INTO news_items (
            source, source_id, published_at, author, title, clean_text,
            content_hash, url, level, media_ids, ingested_at
        ) VALUES ('cls', ?, ?, '', ?, ?, ?, '', '', '[]', ?)
        """,
        (source_id, published_at, title, title, source_id, "2026-10-09T10:00:00+08:00"),
    )
    store._conn.commit()


def _events(now: datetime) -> dict:
    return build_top_hot_events(now, get_store(), refresh=True)


def _by_name(snapshot: dict) -> dict[str, dict]:
    return {event["name"]: event for event in snapshot["events"]}


def _pending(event: dict) -> bool:
    confirm = event["confirmation"]
    assert confirm["label"] == "待开盘验证"
    assert confirm["pending"] is True
    assert confirm["phase"] == "auction"
    assert confirm["score"] == 0
    parts = event["breakdown"]
    assert parts["confirmation"] == 0
    assert all(float(value) >= 0 for value in parts.values())
    assert abs(event["score"] - sum(float(value) for value in parts.values())) < 0.02
    return True


def test_list_switches_at_1500_and_ignores_future_timestamps(tmp_path):
    reset_store_for_tests(tmp_path / "switch.sqlite")
    _insert("intraday", _at(FRIDAY, 10, 0), "工信部出台算力补贴")
    _insert("on-close", _at(FRIDAY, 15, 0), "平安银行回购股份")
    _insert("after", _at(FRIDAY, 15, 1), "碳酸锂报价继续上涨", "碳酸锂")
    _insert("later", _at(FRIDAY, 18, 0), "存储芯片厂宣布涨价")
    _insert("tomorrow", _at(MONDAY, 15, 0), "美联储宣布降息")
    _insert_space_timestamp("space-later", "2026-10-09 18:00:00", "原油期货报价上调")

    before = _events(_at(FRIDAY, 14, 59))
    before_names = set(_by_name(before))
    assert before["as_of"] == "2026-10-09"
    assert before["trading_day"] == "2026-10-09"
    assert before["updated_at"] == "14:59"
    assert before["hint"] == "更新于 14:59"
    assert "工信部出台算力补贴" in before_names
    assert "平安银行回购股份" not in before_names
    assert "碳酸锂报价继续上涨" not in before_names
    assert "存储芯片厂宣布涨价" not in before_names
    assert "美联储宣布降息" not in before_names
    assert "原油期货报价上调" not in before_names

    after = _events(_at(FRIDAY, 15, 1))
    found = _by_name(after)
    assert after["as_of"] == "2026-10-12"
    assert after["trading_day"] == "2026-10-12"
    assert after["fallback"] is False
    assert after["updated_at"] == "15:01"
    assert after["hint"] == "交易日 10月12日 · 等待竞价 · 更新于 15:01"
    assert "工信部出台算力补贴" in found
    assert "平安银行回购股份" in found
    assert "碳酸锂报价继续上涨" in found
    assert "存储芯片厂宣布涨价" not in found
    assert "美联储宣布降息" not in found
    assert "原油期货报价上调" not in found
    assert found["工信部出台算力补贴"]["confirmation"]["pending"] is False
    assert found["平安银行回购股份"]["confirmation"]["pending"] is False
    assert _pending(found["碳酸锂报价继续上涨"])
    public = public_hot_event(found["碳酸锂报价继续上涨"])
    assert public["confirmation"]["pending"] is True
    assert public["confirmation"]["label"] == "待开盘验证"
    brief = _confirmation_brief(found["碳酸锂报价继续上涨"]["confirmation"])
    assert brief is not None and brief["pending"] is True


def test_fresh_post_close_outranks_stale_intraday(tmp_path):
    """同一重要性、都还没有盘面分时，收盘后的新消息靠时间衰减排到前面。"""
    reset_store_for_tests(tmp_path / "decay.sqlite")
    _insert("old", _at(FRIDAY, 9, 30), "创新药临床获批", "创新药")
    _insert("new", _at(FRIDAY, 17, 40), "存储芯片厂宣布涨价", "存储芯片")
    found = _by_name(_events(_at(FRIDAY, 18, 0)))
    morning = found["创新药临床获批"]
    evening = found["存储芯片厂宣布涨价"]
    assert morning["confirmation"]["pending"] is False
    assert morning["breakdown"]["confirmation"] == 0
    assert _pending(evening)
    assert evening["breakdown"]["freshness"] > morning["breakdown"]["freshness"]
    assert evening["score"] > morning["score"]


def test_weekend_keeps_friday_intraday_and_marks_overnight_pending(tmp_path):
    reset_store_for_tests(tmp_path / "weekend.sqlite")
    _insert("session", _at(FRIDAY, 11, 0), "工信部出台算力补贴")
    _insert("after", _at(FRIDAY, 15, 1), "碳酸锂报价继续上涨", "碳酸锂")
    _insert("sat", _at(SATURDAY, 11, 30), "橡胶价格大涨超预期")
    _insert("sun", _at(SUNDAY, 9, 20), "原油期货报价上调")
    _insert_space_timestamp("space-sat", "2026-10-10 15:00:00", "美联储宣布降息")

    saturday = _events(_at(SATURDAY, 8, 30))
    sat = _by_name(saturday)
    assert saturday["as_of"] == "2026-10-12"
    assert saturday["updated_at"] == "08:30"
    assert saturday["hint"] == "交易日 10月12日 · 等待竞价 · 更新于 08:30"
    assert "工信部出台算力补贴" in sat
    assert "碳酸锂报价继续上涨" in sat
    assert "橡胶价格大涨超预期" not in sat
    assert "原油期货报价上调" not in sat
    assert "美联储宣布降息" not in sat
    assert sat["工信部出台算力补贴"]["confirmation"]["pending"] is False
    assert _pending(sat["碳酸锂报价继续上涨"])

    sunday = _by_name(_events(_at(SUNDAY, 18, 0)))
    assert "工信部出台算力补贴" in sunday
    assert "橡胶价格大涨超预期" in sunday
    assert "原油期货报价上调" in sunday
    # 周六 15:00 的空格时间戳，到周日已经是过去，要进下一场并标待开盘。
    assert "美联储宣布降息" in sunday
    assert sunday["工信部出台算力补贴"]["confirmation"]["pending"] is False
    assert _pending(sunday["橡胶价格大涨超预期"])
    assert _pending(sunday["原油期货报价上调"])
    assert _pending(sunday["美联储宣布降息"])

    opened = _by_name(_events(_at(MONDAY, 9, 26)))
    assert "工信部出台算力补贴" not in opened
    assert opened["碳酸锂报价继续上涨"]["confirmation"]["pending"] is False
    assert opened["原油期货报价上调"]["confirmation"]["pending"] is False
    assert opened["碳酸锂报价继续上涨"]["confirmation"]["label"] != "待开盘验证"


def test_previous_intraday_stays_until_the_auction(tmp_path):
    """上一场盘中留到 9:25。午夜展示日不变，9:25 成员变了，视图签名也要变。"""
    reset_store_for_tests(tmp_path / "auction-hold.sqlite")
    _insert("session", _at(FRIDAY, 11, 0), "工信部出台算力补贴")
    _insert("after", _at(FRIDAY, 18, 0), "碳酸锂报价继续上涨", "碳酸锂")

    late = _by_name(_events(_at(FRIDAY, 23, 59)))
    early = _by_name(_events(_at(MONDAY, 0, 1)))
    before_open = _by_name(_events(_at(MONDAY, 9, 24)))
    opened = _by_name(_events(_at(MONDAY, 9, 26)))
    for found in (late, early, before_open):
        assert "工信部出台算力补贴" in found
        assert "碳酸锂报价继续上涨" in found
        assert found["工信部出台算力补贴"]["confirmation"]["pending"] is False
        assert found["碳酸锂报价继续上涨"]["confirmation"]["pending"] is True
    assert "工信部出台算力补贴" not in opened
    assert opened["碳酸锂报价继续上涨"]["confirmation"]["pending"] is False

    carried = "2026-10-12|2026-10-09"
    assert display_view_key(_at(FRIDAY, 23, 59)) == carried
    assert display_view_key(_at(MONDAY, 0, 1)) == carried
    assert display_view_key(_at(MONDAY, 9, 24)) == carried
    assert display_view_key(_at(MONDAY, 9, 26)) == "2026-10-12|2026-10-12"
    assert _events(_at(FRIDAY, 23, 59))["updated_at"] == "23:59"
    assert _events(_at(MONDAY, 0, 1))["updated_at"] == "00:01"
    assert _events(_at(MONDAY, 9, 24))["updated_at"] == "09:24"
    assert _events(_at(MONDAY, 9, 26))["updated_at"] == "09:26"


def test_holiday_preview_waits_for_the_next_open(tmp_path, monkeypatch):
    """已知休市日看下一开市，并把休市当天的稿留到那场开盘再验证。"""
    holiday = WEDNESDAY

    def known(day: date) -> bool:
        return day == holiday

    monkeypatch.setattr("app.news.market_confirm._known_holiday", known)
    reset_store_for_tests(tmp_path / "holiday.sqlite")
    _insert("tue", _at(TUESDAY, 11, 0), "工信部出台算力补贴")
    _insert("tue-late", _at(TUESDAY, 18, 0), "碳酸锂报价继续上涨", "碳酸锂")
    _insert("wed", _at(WEDNESDAY, 10, 0), "平安银行回购股份")
    _insert("thu-future", _at(THURSDAY, 15, 0), "美联储宣布降息")

    preview = _events(_at(WEDNESDAY, 11, 0))
    found = _by_name(preview)
    assert preview["as_of"] == "2026-10-08"
    assert preview["updated_at"] == "11:00"
    assert "等待竞价" in preview["hint"]
    assert "工信部出台算力补贴" in found
    assert "碳酸锂报价继续上涨" in found
    assert "平安银行回购股份" in found
    assert "美联储宣布降息" not in found
    assert found["工信部出台算力补贴"]["confirmation"]["pending"] is False
    assert _pending(found["碳酸锂报价继续上涨"])
    assert _pending(found["平安银行回购股份"])

    opened = _by_name(_events(_at(THURSDAY, 9, 30)))
    assert "平安银行回购股份" in opened
    assert "碳酸锂报价继续上涨" in opened
    assert "工信部出台算力补贴" not in opened
    assert opened["平安银行回购股份"]["confirmation"]["pending"] is False
    assert opened["碳酸锂报价继续上涨"]["confirmation"]["pending"] is False
