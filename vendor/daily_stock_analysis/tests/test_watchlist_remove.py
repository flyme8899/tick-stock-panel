"""观察池批量移出只按等价代码匹配，不改写其余代码的原始文本。"""

from api.v1.endpoints.stocks import _drop_watchlist_codes, _watchlist_match_key


def test_drop_matches_exchange_suffix_without_rewriting_others() -> None:
    codes = ["600519", "300750.SZ", "603626.SH", "HK00700"]
    keys = {_watchlist_match_key("603626"), _watchlist_match_key("00700.HK")}

    kept, removed = _drop_watchlist_codes(codes, keys)

    assert removed == 2
    assert kept == ["600519", "300750.SZ"]


def test_unknown_code_removes_nothing() -> None:
    kept, removed = _drop_watchlist_codes(["600519"], {_watchlist_match_key("000001")})

    assert removed == 0
    assert kept == ["600519"]
