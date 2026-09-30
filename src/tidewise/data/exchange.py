"""交易所官方日线回填：逐交易日抓取 → 可断点续传的暂存区 → 合并进逐合约缓存。

覆盖范围（akshare get_futures_daily 实测）：
- 上期所：约 2006 年起
- 郑商所：2010-08-25 起（且为 3 位合约代码，如 TA209 = 2012 年 9 月）
- 中金所：2010 年起
- 大商所：官方接口被 WAF 拦截（HTTP 412），暂不可用

逐日抓取耗时较长（每交易日一次请求），暂存区按交易所落盘，中断后自动续传。
重叠日期以交易所官方数据为准（比新浪权威）。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from vnpy.trader.constant import Exchange

from tidewise.data.sina import RAW_COLUMNS, DataSourceError, RawBarStore

log = logging.getLogger(__name__)

SYMBOL_RE = re.compile(r"^([A-Za-z]{1,2})(\d{3,4})$")

CZCE_MIN_DATE = date(2010, 8, 25)

DayFetcher = Callable[[Exchange, date], pd.DataFrame]


def trading_calendar() -> list[date]:
    """期货交易日历（akshare 内置，1990-12-19 起）。"""
    from akshare.futures.futures_daily_bar import calendar

    return [date(int(s[:4]), int(s[4:6]), int(s[6:])) for s in calendar]


def fetch_exchange_day(exchange: Exchange, day: date, retries: int = 3) -> pd.DataFrame:
    """抓取某交易所某交易日的全部合约日线；非交易日/接口无数据返回空表。

    网络与解析类临时故障重试 retries 次（间隔递增）后才抛 DataSourceError。
    """
    import akshare as ak

    d = day.strftime("%Y%m%d")
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return ak.get_futures_daily(start_date=d, end_date=d, market=exchange.value)
        except ValueError as e:
            msg = str(e)
            if "Length mismatch" in msg:
                return pd.DataFrame()
            if "Expecting value" in msg:
                # 个别历史交易日接口返回 404 HTML（如 SHFE 2009-01-19），视为无数据
                log.warning("%s %s: 接口无数据（JSON 解析失败），按空交易日处理", exchange.value, d)
                return pd.DataFrame()
            last_err = e
        except Exception as e:  # noqa: BLE001 - SSL/连接等临时故障，重试
            last_err = e
        if attempt + 1 < retries:
            time.sleep(2 * (attempt + 1))
            log.warning(
                "%s %s: 抓取失败（%s），重试 %d/%d",
                exchange.value,
                d,
                type(last_err).__name__,
                attempt + 2,
                retries,
            )
    raise DataSourceError(f"{exchange.value} {d}: {type(last_err).__name__}: {last_err}")


def normalize_contract(symbol: str, row_date: date) -> str:
    """交易所合约代码 → 新浪格式（大写品种 + 4 位年月）。

    郑商所 3 位代码的年份只有个位数，用行情日期消歧：交割年月应落在
    [行情当月, 行情当月 + 18 个月] 区间内（合约挂牌到交割约 12~18 个月）。
    """
    m = SYMBOL_RE.match(symbol)
    if not m:
        raise ValueError(f"无法识别的合约代码: {symbol!r}")
    product, digits = m.group(1).upper(), m.group(2)
    if len(digits) == 4:
        return f"{product}{digits}"
    digit, month = int(digits[0]), int(digits[1:])
    year = row_date.year // 10 * 10 + digit
    while (year, month) < (row_date.year, row_date.month):
        year += 10
    while (year - row_date.year) * 12 + month - row_date.month > 18:
        year -= 10
    return f"{product}{year % 100:02d}{month:02d}"


def normalize_exchange_frame(raw: pd.DataFrame, day: date) -> pd.DataFrame:
    """交易所日线 → 统一 schema（contract 为新浪格式），并剔除无效行。"""
    required = {"symbol", "open", "high", "low", "close", "volume", "open_interest"}
    if raw.empty:
        return pd.DataFrame(columns=RAW_COLUMNS)
    missing = required - set(raw.columns)
    if missing:
        raise DataSourceError(f"{day}: 交易所数据缺少列 {sorted(missing)}")
    df = raw.copy()
    # 交易所数据含 TAS 等特殊代码（如 SC_TAS2611），不是常规合约，跳过
    df = df[df["symbol"].astype(str).str.match(SYMBOL_RE)]
    df["contract"] = [normalize_contract(s, day) for s in df["symbol"]]
    df["date"] = day
    for c in ("open", "high", "low", "close", "volume", "open_interest"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    valid = (
        df[["open", "high", "low", "close"]].gt(0).all(axis=1)
        & (df["high"] >= df[["open", "close", "low"]].max(axis=1))
        & (df["low"] <= df[["open", "close"]].min(axis=1))
        & df["volume"].ge(0)
        & df["open_interest"].ge(0)
    )
    df = df[valid]
    return df[RAW_COLUMNS].reset_index(drop=True)


@dataclass(frozen=True)
class BackfillReport:
    exchange: Exchange
    requested_days: int
    fetched_days: int
    rows: int


class ExchangeStagingStore:
    """每交易所一个暂存 Parquet；已抓取日期由文件内容推断，无需单独 manifest。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, exchange: Exchange) -> Path:
        return self.root / f"{exchange.value}.parquet"

    def load(self, exchange: Exchange) -> pd.DataFrame:
        p = self.path(exchange)
        if not p.exists():
            return pd.DataFrame(columns=RAW_COLUMNS)
        df = pd.read_parquet(p)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def fetched_days(self, exchange: Exchange) -> set[date]:
        return set(self.load(exchange)["date"].unique())

    def backfill(
        self,
        exchange: Exchange,
        start: date,
        end: date,
        fetch: DayFetcher = fetch_exchange_day,
        pause_s: float = 0.25,
        flush_every: int = 50,
    ) -> BackfillReport:
        self.root.mkdir(parents=True, exist_ok=True)
        days = [d for d in trading_calendar() if start <= d <= end]
        done = self.fetched_days(exchange)
        todo = [d for d in days if d not in done]
        if not todo:
            return BackfillReport(exchange, len(days), 0, 0)

        staged = self.load(exchange)
        frames = [] if staged.empty else [staged]
        fetched = 0
        for i, d in enumerate(todo, 1):
            df = normalize_exchange_frame(fetch(exchange, d), d)
            if not df.empty:
                frames.append(df)
            fetched += 1
            if pause_s:
                time.sleep(pause_s)
            if i % flush_every == 0 or i == len(todo):
                out = pd.concat(frames, ignore_index=True)
                out = out.sort_values(["contract", "date"]).reset_index(drop=True)
                out.to_parquet(self.path(exchange), index=False)
                log.info("%s: 已抓取 %d/%d 个交易日", exchange.value, i, len(todo))
        total = self.load(exchange)
        return BackfillReport(exchange, len(days), fetched, len(total))


# 品种历史代码别名：郑商所甲醇 2015 年前代码为 ME（当时乘数 50 吨/手，后为 10 吨）。
# 注意：别名期的乘数与现行不同，回测统一按配置乘数折算（等价于交易若干"虚拟手"），
# 持仓量横截面排序不受影响。
PRODUCT_ALIASES: dict[str, tuple[str, ...]] = {"MA": ("ME",)}


def merge_into_products(
    staging: ExchangeStagingStore,
    raw_store: RawBarStore,
    exchange: Exchange,
    products: list[str],
) -> dict[str, int]:
    """把暂存区数据合并进逐合约缓存；重叠日期以交易所数据为准。返回各品种行数。"""
    df = staging.load(exchange)
    out: dict[str, int] = {}
    for product in products:
        names = (product.upper(), *PRODUCT_ALIASES.get(product.upper(), ()))
        rows = df[df["contract"].str.startswith(names)].copy()
        # 别名代码统一改写为现行品种代码
        for alias in PRODUCT_ALIASES.get(product.upper(), ()):
            mask = rows["contract"].str.startswith(alias)
            rows.loc[mask, "contract"] = (
                product.upper() + rows.loc[mask, "contract"].str[len(alias) :]
            )
        merged = raw_store.merge_exchange(product, rows)
        out[product] = len(merged)
    return out
