from datetime import date

import pytest

from tidewise.live import Positions, build_daily_report, load_positions, render_markdown
from tidewise.live.positions import PositionsError, save_template


def test_load_positions_valid(tmp_path):
    p = tmp_path / "positions.yaml"
    p.write_text("as_of: 2026-09-30\npositions:\n  RB2601: 2\n  MA601: -1\n", encoding="utf-8")
    pos = load_positions(p)
    assert pos.as_of == date(2026, 9, 30)
    assert pos.net == {"RB2601": 2, "MA601": -1}


def test_load_positions_missing_file(tmp_path):
    pos = load_positions(tmp_path / "nope.yaml")
    assert pos.net == {}


@pytest.mark.parametrize(
    "content,match",
    [
        ("positions: {}\n", "as_of"),
        ("as_of: 2026-09-30\npositions:\n  RB2601: 2.5\n", "整数"),
        ("as_of: 2026-09-30\npositions:\n  BAD!!: 1\n", "无法识别"),
    ],
)
def test_load_positions_invalid(tmp_path, content, match):
    p = tmp_path / "positions.yaml"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(PositionsError, match=match):
        load_positions(p)


def test_save_template(tmp_path):
    p = tmp_path / "positions.yaml"
    save_template(p, date(2026, 10, 1), ["RB2701", "MA701"])
    text = p.read_text(encoding="utf-8")
    assert "as_of: 2026-10-01" in text and "RB2701" in text


def test_report_end_to_end(cfg):
    """合成数据 + 空持仓 → 报告含明细与调仓清单；持仓与目标一致 → 清单为空。"""
    report = build_daily_report(cfg, Positions(date(2021, 2, 1), {}), date(2021, 3, 1))
    assert report.signal_date >= date(2021, 2, 1)
    assert len(report.lines) == 1 and report.lines[0].product == "rb"
    text = render_markdown(report)
    assert "每日决策报告" in text and "rb" in text

    # 持仓与目标一致 → 无调仓
    ln = report.lines[0]
    report2 = build_daily_report(
        cfg, Positions(report.signal_date, {ln.contract: ln.target}), date(2021, 3, 1)
    )
    assert report2.is_empty


def test_report_warns_stale_data(cfg):
    """today 远晚于数据日期 → 告警数据过期。"""
    report = build_daily_report(cfg, Positions(date(2021, 2, 1), {}), date(2030, 1, 1))
    assert any("数据停在" in w for w in report.warnings)
