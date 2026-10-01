"""每日报告渲染：Markdown（控制台与文件共用）。"""

from __future__ import annotations

from tidewise.live.report import DailyReport


def render_markdown(report: DailyReport) -> str:
    r = report
    lines = [
        f"# 每日决策报告 — 信号日 {r.signal_date}",
        "",
        f"生成时间 {r.generated_at:%Y-%m-%d %H:%M:%S}；持仓文件日期 {r.positions_as_of}；"
        f"组合波动 scalar {r.scalar:.2f}；目标保证金占用 {r.margin_usage:.1%}",
        "",
    ]
    if r.warnings:
        lines += ["## 告警", ""] + [f"- {w}" for w in r.warnings] + [""]
    if r.positions_as_of < r.signal_date:
        lines += [
            f"> 注意：持仓文件日期 {r.positions_as_of} 早于信号日 {r.signal_date}，"
            "请先更新 var/positions.yaml",
            "",
        ]

    lines += [
        "## 品种明细",
        "",
        "| 品种 | 主力合约 | 信号 | 目标 | 持仓 | 合约 | 最新价 | 保证金 | 备注 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for ln in r.lines:
        f = "-" if ln.forecast is None else f"{ln.forecast:+.1f}"
        lines.append(
            f"| {ln.product} | {ln.contract} | {f} | {ln.target:+d} | {ln.held:+d} | "
            f"{ln.held_contract or '-'} | {ln.price:.0f} | {ln.margin:,.0f} | {ln.note} |"
        )

    lines += ["", "## 调仓清单（下一交易时段执行）", ""]
    if not r.orders:
        lines += ["无需调仓。", ""]
    else:
        lines += ["| 合约 | 方向 | 开平 | 手数 | 原因 |", "|---|---|---|---|---|"]
        for o in r.orders:
            direction = "买入" if o.direction.value == "多" else "卖出"
            offset = {"开": "开仓", "平": "平仓", "平今": "平今", "平昨": "平昨"}[o.offset.value]
            reason = {
                "REBALANCE": "调仓",
                "ROLL_CLOSE": "换月平仓",
                "ROLL_OPEN": "换月开仓",
                "EXIT": "退出",
            }[o.reason.value]
            lines.append(f"| {o.vt_symbol} | {direction} | {offset} | {o.volume} | {reason} |")
        lines.append("")
    lines += [
        "> 以上为系统建议，非投资建议。执行前请确认持仓文件与账户一致。",
    ]
    return "\n".join(lines) + "\n"
