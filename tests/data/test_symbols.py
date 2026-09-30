from datetime import date

import pytest
from vnpy.trader.constant import Exchange

from tidewise.data.symbols import ContractId, parse_sina


def test_sina_and_vnpy_formats():
    rb = ContractId("rb", 2026, 1)
    assert rb.sina_symbol == "RB2601"
    assert rb.vnpy_symbol(Exchange.SHFE) == "rb2601"
    ma = ContractId("MA", 2026, 9)
    assert ma.sina_symbol == "MA2609"
    assert ma.vnpy_symbol(Exchange.CZCE) == "MA609"


def test_parse_sina_roundtrip():
    assert parse_sina("MA", "MA2609") == ContractId("MA", 2026, 9)
    assert parse_sina("i", "I2505") == ContractId("i", 2025, 5)


@pytest.mark.parametrize("bad", ["RB2601", "MA26", "MA2613x", ""])
def test_parse_sina_rejects(bad):
    with pytest.raises(ValueError):
        parse_sina("MA", bad)


def test_force_roll_date():
    assert ContractId("rb", 2026, 5).force_roll_date(15) == date(2026, 4, 15)
    assert ContractId("rb", 2026, 1).force_roll_date(15) == date(2025, 12, 15)


def test_invalid_month():
    with pytest.raises(ValueError):
        ContractId("rb", 2026, 13)
