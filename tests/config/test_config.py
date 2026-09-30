import copy

import pytest
from vnpy.trader.constant import Exchange

from tidewise.config import (
    ConfigError,
    CtpCredentials,
    MissingSecretError,
    Secret,
    load_config,
    parse_config,
)


def test_example_config_loads(example_config):
    cfg = load_config(example_config)
    enabled = {i.product: i for i in cfg.instruments()}
    assert "cu" not in enabled  # enabled: false
    assert enabled["rb"].exchange is Exchange.SHFE
    assert enabled["rb"].continuous_vt_symbol == "rb888.SHFE"
    assert not enabled["AP"].night_session
    # 停用品种可显式点名研究
    assert [i.product for i in cfg.instruments(["cu"])] == ["cu"]


def test_instruments_unknown_product(example_config):
    cfg = load_config(example_config)
    with pytest.raises(ValueError, match="zz"):
        cfg.instruments(["zz"])


def test_commission_rate_converts_per_lot(example_config):
    cfg = load_config(example_config)
    ma = cfg.instruments(["MA"])[0]  # 开平各 2 元/手，乘数 10
    assert ma.commission_rate(2500) == pytest.approx(2 / 25_000)
    rb = cfg.instruments(["rb"])[0]  # 开平各 万分之一
    assert rb.commission_rate(3500) == pytest.approx(0.0001)


def test_non_futures_exchange_rejected(example_raw):
    example_raw["universe"][0]["exchange"] = "SSE"
    with pytest.raises(ConfigError, match="期货交易所"):
        parse_config(example_raw)


def test_backtest_range(example_raw):
    example_raw["backtest"]["end"] = "2019-01-01"
    with pytest.raises(ConfigError, match="backtest.end"):
        parse_config(example_raw)


def test_unknown_field_rejected(example_raw):
    example_raw["risk"]["max_margn_usage"] = 0.5
    with pytest.raises(ConfigError, match="max_margn_usage"):
        parse_config(example_raw)


def test_duplicate_product_rejected(example_raw):
    example_raw["universe"].append(copy.deepcopy(example_raw["universe"][0]))
    with pytest.raises(ConfigError, match="品种重复"):
        parse_config(example_raw)


def test_all_disabled_rejected(example_raw):
    for inst in example_raw["universe"]:
        inst["enabled"] = False
    with pytest.raises(ConfigError, match="enabled"):
        parse_config(example_raw)


def test_risk_consistency(example_raw):
    example_raw["risk"]["max_instrument_margin"] = 0.5
    example_raw["risk"]["max_margin_usage"] = 0.3
    with pytest.raises(ConfigError):
        parse_config(example_raw)


@pytest.mark.parametrize("key", ["password", "app_secret", "auth_code", "AuthCode", "token"])
def test_secret_keys_forbidden_anywhere(example_raw, key):
    example_raw["strategy"]["params"][key] = "x"
    with pytest.raises(ConfigError, match="密钥"):
        parse_config(example_raw)


def test_ctp_broker_requires_fields(example_raw):
    example_raw["broker"] = {"kind": "ctp", "broker_id": "9999"}
    with pytest.raises(ConfigError, match="td_address"):
        parse_config(example_raw)


def test_ctp_address_scheme(example_raw):
    example_raw["broker"] = {
        "kind": "ctp",
        "broker_id": "9999",
        "td_address": "1.2.3.4:10201",
        "md_address": "tcp://1.2.3.4:10211",
        "app_id": "simnow_client_test",
    }
    with pytest.raises(ConfigError, match="tcp://"):
        parse_config(example_raw)


def test_feishu_requires_chat_id(example_raw):
    example_raw["notify"] = {"kind": "feishu"}
    with pytest.raises(ConfigError, match="feishu_chat_id"):
        parse_config(example_raw)


def test_execution_windows_parsed(example_config):
    from datetime import time

    cfg = load_config(example_config)
    assert cfg.execution.night_window == time(21, 5)
    assert cfg.execution.day_window == time(9, 5)


def test_invalid_yaml(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("universe: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(p)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="无法读取"):
        load_config(tmp_path / "nope.yaml")


def test_ctp_credentials_from_env():
    env = {
        "TIDEWISE_CTP_USER_ID": "u",
        "TIDEWISE_CTP_PASSWORD": "p@ss",
        "TIDEWISE_CTP_AUTH_CODE": "a",
    }
    cred = CtpCredentials.from_env(env)
    assert cred.password.reveal() == "p@ss"
    assert "p@ss" not in repr(cred)
    assert "p@ss" not in str(cred.password)


def test_ctp_credentials_missing():
    with pytest.raises(MissingSecretError, match="TIDEWISE_CTP_PASSWORD"):
        CtpCredentials.from_env({"TIDEWISE_CTP_USER_ID": "u", "TIDEWISE_CTP_AUTH_CODE": "a"})


def test_secret_blank_counts_as_missing():
    with pytest.raises(MissingSecretError):
        CtpCredentials.from_env(
            {
                "TIDEWISE_CTP_USER_ID": " ",
                "TIDEWISE_CTP_PASSWORD": "p",
                "TIDEWISE_CTP_AUTH_CODE": "a",
            }
        )


def test_secret_equality():
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")
