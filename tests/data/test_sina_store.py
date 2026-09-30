from datetime import date

import pandas as pd
import pytest

from tidewise.data.sina import DataSourceError, RawBarStore, clean_sina_frame


def _raw(dates, close=3000.0):
    return pd.DataFrame(
        {
            "date": [d.isoformat() for d in dates],
            "open": close,
            "high": close + 10,
            "low": close - 10,
            "close": close,
            "volume": 100,
            "hold": 1000,
            "settle": close,
        }
    )


def test_clean_drops_invalid_rows():
    raw = _raw([date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)])
    raw.loc[1, "high"] = 1  # high < close
    raw.loc[2, "close"] = 0
    df, dropped = clean_sina_frame(raw, "RB2505")
    assert dropped == 2
    assert df["contract"].tolist() == ["RB2505"]
    assert "open_interest" in df.columns


def test_clean_missing_columns():
    with pytest.raises(DataSourceError, match="缺少列"):
        clean_sina_frame(pd.DataFrame({"date": ["2025-01-02"]}), "RB2505")


def test_store_caches_expired_and_absent(tmp_path):
    calls: list[str] = []
    listed = {"RB2505", "RB2510"}

    def fake_fetch(sym):
        calls.append(sym)
        return _raw([date(2025, 1, 2), date(2025, 1, 3)]) if sym in listed else None

    store = RawBarStore(tmp_path)
    today = date(2025, 11, 1)
    r1 = store.update("rb", 2025, today, fetch=fake_fetch, pause_s=0)
    assert r1.contracts == 2 and r1.rows == 4
    first_calls = len(calls)
    assert first_calls == 24  # 2025、2026 两年各 12 个月

    calls.clear()
    r2 = store.update("rb", 2025, today, fetch=fake_fetch, pause_s=0)
    # 已到期（<2025-11）的月份不再请求：只剩 2025-11/12 与 2026 全年
    assert len(calls) == 14
    assert r2.contracts == 2 and r2.rows == 4
    assert set(store.load("rb")["contract"]) == listed
