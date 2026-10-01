"""命令行入口。"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date

from tidewise import __version__
from tidewise.config import (
    AppConfig,
    ConfigError,
    CtpCredentials,
    MissingSecretError,
    load_config,
)


def _load(path: str) -> AppConfig | None:
    try:
        return load_config(path)
    except ConfigError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return None


def _cmd_config_check(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 1
    enabled = cfg.instruments()
    disabled = [i.product for i in cfg.universe if not i.enabled]
    print(f"[OK] 配置: {args.config}")
    print(
        f"  品种: {len(enabled)} 个启用 {[i.product for i in enabled]}"
        + (f"，停用 {disabled}" if disabled else "")
    )
    print(f"  夜盘品种: {[i.product for i in enabled if i.night_session]}")
    print(f"  策略: {cfg.strategy.name} {cfg.strategy.params}")
    print(f"  券商: {cfg.broker.kind} / 审批: {cfg.approval.mode} / 通知: {cfg.notify.kind}")
    if cfg.broker.kind == "ctp":
        try:
            CtpCredentials.from_env()
        except MissingSecretError as e:
            print(f"[FAIL] {e}", file=sys.stderr)
            return 1
        print("  CTP 密钥: 已从环境变量读取")
    return 0


def _cmd_data_update(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 1
    from tidewise.data import DataSourceError, update_product

    try:
        instruments = cfg.instruments(args.products)
    except ValueError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    failed = []
    for inst in instruments:
        try:
            s = update_product(cfg, inst, date.today())
        except (DataSourceError, ValueError) as e:
            print(f"[FAIL] {inst.product}: {e}", file=sys.stderr)
            failed.append(inst.product)
            continue
        print(
            f"[OK] {s.product}: 请求 {s.raw.fetched} 次，合约 {s.raw.contracts} 个，"
            f"连续序列 {s.bars} 根 {s.first_date} ~ {s.last_date}，"
            f"换月 {s.rolls} 次，当前主力 {s.current_contract}"
        )
    return 1 if failed else 0


def _cmd_data_backfill(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 1
    from vnpy.trader.constant import Exchange

    from tidewise.data.exchange import ExchangeStagingStore, merge_into_products
    from tidewise.data.sina import RawBarStore

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()
    by_exchange: dict[Exchange, list[str]] = {}
    for inst in cfg.instruments(args.products):
        by_exchange.setdefault(inst.exchange, []).append(inst.product)

    failed = []
    for exchange, products in sorted(by_exchange.items(), key=lambda kv: kv[0].value):
        try:
            report = ExchangeStagingStore(cfg.storage.exchange_staging_dir).backfill(
                exchange, start, end
            )
        except ValueError as e:
            print(f"[FAIL] {exchange.value}: {e}", file=sys.stderr)
            failed.append(exchange.value)
            continue
        rows = merge_into_products(
            ExchangeStagingStore(cfg.storage.exchange_staging_dir),
            RawBarStore(cfg.storage.raw_bars_dir),
            exchange,
            products,
        )
        detail = ", ".join(f"{p} {n} 行" for p, n in rows.items())
        print(
            f"[OK] {exchange.value}: 交易日 {report.fetched_days}/{report.requested_days}，"
            f"{report.rows} 行；合并：{detail}"
        )
    if failed:
        return 1
    # 回填后重建连续合约与 carry（不联网，只读本地缓存）
    from tidewise.data.carry import CarryStore, build_carry
    from tidewise.data.continuous import build_continuous
    from tidewise.data.pipeline import ContinuousStore

    for products in by_exchange.values():
        for product in products:
            raw = RawBarStore(cfg.storage.raw_bars_dir).load(product)
            df, events = build_continuous(
                product, raw, cfg.data.roll_confirm_days, cfg.data.force_roll_day
            )
            ContinuousStore(cfg.storage.continuous_dir).save(product, df, events)
            CarryStore(cfg.storage.carry_dir).save(
                product,
                build_carry(product, raw, cfg.data.roll_confirm_days, cfg.data.force_roll_day),
            )
            first = df["date"].iloc[0] if len(df) else "-"
            last = df["date"].iloc[-1] if len(df) else "-"
            print(f"[OK] {product}: 连续序列 {len(df)} 根 {first} ~ {last}，换月 {len(events)} 次")
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    if cfg is None:
        return 1
    from tidewise.backtest import run_backtest, write_report

    try:
        result = run_backtest(cfg, args.products)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    label = "-".join(args.products) if args.products else "universe"
    report = write_report(result, cfg.storage.report_dir, label)
    s = result.stats
    print(f"[OK] 回测 {result.trading_start} ~ {result.end}，报告: {report}")
    print(
        f"  年化 {s['annual_return']:.2f}%  最大回撤 {s['max_ddpercent']:.2f}%  "
        f"夏普 {s['sharpe_ratio']:.2f}  成交 {s['total_trade_count']} 笔  "
        f"换月成本 {result.roll_cost_total:,.0f}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tidewise", description="中低频期货 CTA 交易系统")
    parser.add_argument("--version", action="version", version=f"tidewise {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("-c", "--config", default="config/local.yaml")
        return p

    def with_products(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("-p", "--products", nargs="+", help="品种代码，缺省为全部启用品种")
        return p

    config = sub.add_parser("config", help="配置相关")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    with_config(config_sub.add_parser("check", help="校验配置文件与所需密钥")).set_defaults(
        func=_cmd_config_check
    )

    data = sub.add_parser("data", help="数据相关")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    upd = with_products(with_config(data_sub.add_parser("update", help="下载日线并重建连续合约")))
    upd.set_defaults(func=_cmd_data_update)
    bf_parser = data_sub.add_parser("backfill", help="交易所官方日线回填历史（长耗时）")
    bf = with_products(with_config(bf_parser))
    bf.add_argument("--start", default="2010-01-01", help="回填起始日期 YYYY-MM-DD")
    bf.add_argument("--end", default=None, help="回填结束日期，缺省到今天")
    bf.set_defaults(func=_cmd_data_backfill)

    bt = with_products(with_config(sub.add_parser("backtest", help="运行回测并生成报告")))
    bt.set_defaults(func=_cmd_backtest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
