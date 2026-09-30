from datetime import date

import pandas as pd
import pytest
from vnpy.trader.constant import Exchange

from tidewise.data.exchange import (
    ExchangeStagingStore,
    merge_into_products,
    normalize_contract,
    normalize_exchange_frame,
)
from tidewise.data.sina import RAW_COLUMNS, RawBarStore

D = date(2012, 9, 4)


@pytest.mark.parametrize(
    ("symbol", "day", "expected"),
    [
        ("RB0909", date(2009, 9, 4), "RB0909"),
        ("rb2601", date(2026, 1, 5), "RB2601"),
        ("TA209", date(2012, 9, 4), "TA1209"),
        ("TA301", date(2012, 9, 4), "TA1301"),
        ("CF009", date(2010, 8, 25), "CF1009"),
        ("MA609", date(2026, 9, 4), "MA2609"),
        ("MA109", date(2020, 9, 4), "MA2109"),  # 3 位码 = 年份个位 + 月份 → 2021-09
        ("MA912", date(2019, 12, 2), "MA1912"),
        ("MA001", date(2019, 12, 2), "MA2001"),  # 跨年进位
        ("IF1010", date(2010, 5, 12), "IF1010"),
    ],
)
def test_normalize_contract(symbol, day, expected):
    assert normalize_contract(symbol, day) == expected


def test_normalize_contract_rejects():
    with pytest.raises(ValueError):
        normalize_contract("RB", D)
    with pytest.raises(ValueError):
        normalize_contract("", D)


def _exchange_day(symbols=("RB0909", "RB1001"), close=3900.0):
    return pd.DataFrame(
        {
            "symbol": symbols,
            "date": D,
            "open": close - 30,
            "high": close + 30,
            "low": close - 60,
            "close": close,
            "volume": 1000,
            "open_interest": 5000,
            "settle": close,
            "pre_settle": close,
            "variety": "RB",
        }
    )


def test_normalize_exchange_frame():
    df = normalize_exchange_frame(_exchange_day(), D)
    assert df["contract"].tolist() == ["RB0909", "RB1001"]
    assert df["date"].tolist() == [D, D]
    assert list(df.columns) == RAW_COLUMNS


def test_normalize_exchange_frame_drops_zero_price():
    raw = _exchange_day()
    raw.loc[1, ["open", "high", "low", "close"]] = 0  # 未成交合约
    df = normalize_exchange_frame(raw, D)
    assert df["contract"].tolist() == ["RB0909"]


def test_backfill_resumable(tmp_path):
    days = [date(2012, 9, 3), date(2012, 9, 4), date(2012, 9, 5)]
    calls = []
    import tidewise.data.exchange as ex

    cal = ex.trading_calendar
    ex.trading_calendar = lambda: days  # 测试用假日历
    try:

        def fake(exch, d):
            calls.append(d)
            return _exchange_day()

        store = ExchangeStagingStore(tmp_path)
        r1 = store.backfill(Exchange.SHFE, days[0], days[-1], fetch=fake, pause_s=0, flush_every=2)
        assert r1.fetched_days == 3 and r1.rows == 6
        r2 = store.backfill(Exchange.SHFE, days[0], days[-1], fetch=fake, pause_s=0)
        assert r2.fetched_days == 0
        assert len(calls) == 3  # 不重复抓取
    finally:
        ex.trading_calendar = cal


def test_merge_prefers_exchange(tmp_path):
    staging = ExchangeStagingStore(tmp_path / "stg")
    raw_store = RawBarStore(tmp_path / "raw")
    day = date(2019, 6, 3)

    # 新浪已有：RB1909 close=3700（假设有错）
    sina = pd.DataFrame(
        [
            {
                "contract": "RB1909",
                "date": day,
                "open": 3690,
                "high": 3710,
                "low": 3680,
                "close": 3700,
                "volume": 100,
                "open_interest": 900,
            }
        ]
    )
    raw_store.merge_exchange("rb", pd.DataFrame(columns=RAW_COLUMNS))  # 无交易所数据时保持原样
    path, _ = raw_store._paths("rb")
    path.parent.mkdir(parents=True, exist_ok=True)
    sina.to_parquet(path, index=False)

    # 交易所：同日同合约 close=3705，且多一个老合约 RB1809
    ex_df = pd.DataFrame(
        [
            {
                "contract": "RB1909",
                "date": day,
                "open": 3695,
                "high": 3715,
                "low": 3685,
                "close": 3705,
                "volume": 200,
                "open_interest": 1000,
            },
            {
                "contract": "RB1809",
                "date": date(2018, 6, 1),
                "open": 3500,
                "high": 3510,
                "low": 3490,
                "close": 3505,
                "volume": 50,
                "open_interest": 800,
            },
        ]
    )
    stg_path = staging.path(Exchange.SHFE)
    stg_path.parent.mkdir(parents=True, exist_ok=True)
    ex_df.to_parquet(stg_path, index=False)

    out = merge_into_products(staging, raw_store, Exchange.SHFE, ["rb"])
    df = raw_store.load("rb")
    row = df[(df.contract == "RB1909") & (df.date == day)].iloc[0]
    assert row.close == 3705 and row.open_interest == 1000  # 交易所覆盖新浪
    assert "RB1809" in set(df.contract)
    assert out["rb"] == 2  # RB1909 重叠去重后：交易所 1 行 + 历史 RB1809 1 行
