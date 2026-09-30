import yaml

from tidewise.cli import main


def test_config_check_ok(example_config, capsys):
    assert main(["config", "check", "-c", str(example_config)]) == 0
    out = capsys.readouterr().out
    assert "[OK]" in out and "rb" in out


def test_config_check_invalid(tmp_path, capsys):
    p = tmp_path / "bad.yaml"
    p.write_text("universe: []\nstrategy: {name: x}\n", encoding="utf-8")
    assert main(["config", "check", "-c", str(p)]) == 1
    assert "[FAIL]" in capsys.readouterr().err


def test_config_check_ctp_missing_secret(tmp_path, monkeypatch, capsys, example_raw):
    for k in ("TIDEWISE_CTP_USER_ID", "TIDEWISE_CTP_PASSWORD", "TIDEWISE_CTP_AUTH_CODE"):
        monkeypatch.delenv(k, raising=False)
    example_raw["broker"] = {
        "kind": "ctp",
        "broker_id": "9999",
        "td_address": "tcp://127.0.0.1:10201",
        "md_address": "tcp://127.0.0.1:10211",
        "app_id": "simnow_client_test",
    }
    p = tmp_path / "ctp.yaml"
    p.write_text(yaml.safe_dump(example_raw, allow_unicode=True), encoding="utf-8")
    assert main(["config", "check", "-c", str(p)]) == 1
    assert "TIDEWISE_CTP_USER_ID" in capsys.readouterr().err
