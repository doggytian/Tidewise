"""配置 schema。未知字段一律拒绝，防止拼写错误被静默忽略。

密钥不在此 schema 中出现，见 tidewise.config.secrets。
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from vnpy.trader.constant import Exchange


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeeRateConfig(_Strict):
    ratio: float = Field(0.0, ge=0)
    per_lot: float = Field(0.0, ge=0)

    def as_ratio(self, price: float, multiplier: float) -> float:
        """折算为成交额比例（vn.py 回测只支持比例手续费）。"""
        return self.ratio + self.per_lot / (price * multiplier)


class CommissionConfig(_Strict):
    open: FeeRateConfig = FeeRateConfig()
    close: FeeRateConfig = FeeRateConfig()
    close_today: FeeRateConfig | None = None  # 缺省同 close；日线策略不做日内平今


class InstrumentConfig(_Strict):
    product: str = Field(min_length=1, max_length=2, pattern=r"^[A-Za-z]+$")
    exchange: Exchange
    multiplier: float = Field(gt=0)
    price_tick: float = Field(gt=0)
    margin_ratio: float = Field(gt=0, lt=1)
    night_session: bool
    commission: CommissionConfig = CommissionConfig()
    cluster: str = ""  # 板块（相关性分组），如 black/oils；空 = 独立板块
    enabled: bool = True

    @field_validator("exchange")
    @classmethod
    def _futures_exchange(cls, v: Exchange) -> Exchange:
        if v not in _FUTURES_EXCHANGES:
            raise ValueError(f"不支持的期货交易所: {v.value}")
        return v

    @property
    def continuous_vt_symbol(self) -> str:
        """主力连续（比例后复权）的虚拟合约代码，仅用于研究与回测。"""
        return f"{self.product}888.{self.exchange.value}"

    def commission_rate(self, ref_price: float) -> float:
        """开平平均后的比例费率，用于 vn.py 回测。"""
        c = self.commission
        return (
            c.open.as_ratio(ref_price, self.multiplier)
            + c.close.as_ratio(ref_price, self.multiplier)
        ) / 2


_FUTURES_EXCHANGES = frozenset(
    {Exchange.SHFE, Exchange.INE, Exchange.DCE, Exchange.CZCE, Exchange.CFFEX, Exchange.GFEX}
)


class DataConfig(_Strict):
    source: Literal["akshare_sina"] = "akshare_sina"
    history_start_year: int = Field(2018, ge=2010, le=2100)
    roll_confirm_days: int = Field(3, ge=1, le=20)
    force_roll_day: int = Field(15, ge=1, le=28)  # 交割月前一个月的该日强制换月


class StrategyConfig(_Strict):
    name: Literal["ewmac_trend"] = "ewmac_trend"
    params: dict[str, Any] = Field(default_factory=dict)


class SizingConfig(_Strict):
    """波动率定仓：平均信号强度下，单品种年化波动（资金）占权益的比例。"""

    instrument_vol_target: float = Field(0.025, gt=0, le=0.2)
    vol_lookback_days: int = Field(25, ge=5, le=250)
    buffer_fraction: float = Field(0.1, ge=0, le=0.5)
    # 小资金筛选：平均信号（forecast=10）下不足该手数的品种停用（强信号时仍应能持 1 手）
    min_avg_lots: float = Field(0.25, gt=0, le=2)


class RiskConfig(_Strict):
    max_margin_usage: float = Field(0.4, gt=0, le=0.9)
    max_instrument_margin: float = Field(0.1, gt=0, le=0.5)
    max_cluster_margin: float = Field(0.15, gt=0, le=0.5)  # 同板块品种合计保证金上限
    max_lots_per_leg: int = Field(20, ge=1)
    daily_loss_halt: float = Field(0.03, gt=0, le=0.2)
    drawdown_halt: float = Field(0.15, gt=0, le=0.5)
    max_cancels_per_day: int = Field(100, ge=1)


class PortfolioConfig(_Strict):
    """组合层波动率目标：按策略自身近期盈亏波动缩减整体仓位（Carver vol scalar）。"""

    annual_vol_target: float = Field(0.10, gt=0, le=0.5)
    realized_vol_lookback: int = Field(25, ge=5, le=120)
    max_scalar: float = Field(1.0, gt=0, le=1.0)  # 只缩减不放大的上限


class ExecutionConfig(_Strict):
    night_window: time = time(21, 5)
    day_window: time = time(9, 5)
    order_timeout_s: int = Field(15, ge=3, le=300)
    max_chase: int = Field(3, ge=0, le=10)
    chase_ticks: int = Field(1, ge=1, le=10)


class BacktestConfig(_Strict):
    start: date = date(2019, 1, 1)
    end: date | None = None
    capital: float = Field(300_000, gt=0)
    slippage_ticks: float = Field(1.0, ge=0)


class ApprovalConfig(_Strict):
    mode: Literal["manual"] = "manual"  # 全自动模式在第二阶段开放


class BrokerConfig(_Strict):
    kind: Literal["sim", "ctp"] = "sim"
    broker_id: str = ""
    td_address: str = ""
    md_address: str = ""
    app_id: str = ""
    environment: Literal["simulation", "live"] = "simulation"

    @model_validator(mode="after")
    def _ctp_fields(self) -> BrokerConfig:
        if self.kind == "ctp":
            missing = [
                n
                for n in ("broker_id", "td_address", "md_address", "app_id")
                if not getattr(self, n)
            ]
            if missing:
                raise ValueError(f"broker.kind=ctp 时缺少字段: {', '.join(missing)}")
            for n in ("td_address", "md_address"):
                if not getattr(self, n).startswith(("tcp://", "ssl://")):
                    raise ValueError(f"broker.{n} 必须以 tcp:// 或 ssl:// 开头")
        return self


class StorageConfig(_Strict):
    root: Path = Path("var")

    @property
    def raw_bars_dir(self) -> Path:
        return self.root / "bars" / "raw"

    @property
    def exchange_staging_dir(self) -> Path:
        return self.root / "bars" / "exchange_staging"

    @property
    def continuous_dir(self) -> Path:
        return self.root / "bars" / "continuous"

    @property
    def carry_dir(self) -> Path:
        return self.root / "bars" / "carry"

    @property
    def report_dir(self) -> Path:
        return self.root / "reports"

    @property
    def state_db(self) -> Path:
        return self.root / "state.db"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"


class NotifyConfig(_Strict):
    kind: Literal["console", "feishu"] = "console"
    feishu_chat_id: str = ""

    @model_validator(mode="after")
    def _feishu_fields(self) -> NotifyConfig:
        if self.kind == "feishu" and not self.feishu_chat_id:
            raise ValueError("notify.kind=feishu 时必须配置 feishu_chat_id")
        return self


class AppConfig(_Strict):
    universe: list[InstrumentConfig] = Field(min_length=1)
    data: DataConfig = DataConfig()
    strategy: StrategyConfig = StrategyConfig()
    sizing: SizingConfig = SizingConfig()
    risk: RiskConfig = RiskConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    execution: ExecutionConfig = ExecutionConfig()
    backtest: BacktestConfig = BacktestConfig()
    approval: ApprovalConfig = ApprovalConfig()
    broker: BrokerConfig = BrokerConfig()
    storage: StorageConfig = StorageConfig()
    notify: NotifyConfig = NotifyConfig()

    @field_validator("universe")
    @classmethod
    def _unique_products(cls, v: list[InstrumentConfig]) -> list[InstrumentConfig]:
        seen: set[str] = set()
        for inst in v:
            if inst.product in seen:
                raise ValueError(f"品种重复: {inst.product}")
            seen.add(inst.product)
        return v

    @model_validator(mode="after")
    def _consistency(self) -> AppConfig:
        if self.risk.max_instrument_margin > self.risk.max_margin_usage:
            raise ValueError("risk.max_instrument_margin 不能大于 risk.max_margin_usage")
        if self.risk.max_cluster_margin > self.risk.max_margin_usage:
            raise ValueError("risk.max_cluster_margin 不能大于 risk.max_margin_usage")
        if self.risk.max_cluster_margin < self.risk.max_instrument_margin:
            raise ValueError("risk.max_cluster_margin 不能小于 risk.max_instrument_margin")
        if not any(i.enabled for i in self.universe):
            raise ValueError("universe 中至少需要一个 enabled 品种")
        if self.backtest.end and self.backtest.end <= self.backtest.start:
            raise ValueError("backtest.end 必须晚于 backtest.start")
        return self

    def instruments(self, products: list[str] | None = None) -> list[InstrumentConfig]:
        """启用的品种；传 products 时按其筛选（可包含停用品种，便于单独研究）。"""
        if products is None:
            return [i for i in self.universe if i.enabled]
        by_product = {i.product: i for i in self.universe}
        unknown = [p for p in products if p not in by_product]
        if unknown:
            raise ValueError(f"配置中不存在的品种: {unknown}")
        return [by_product[p] for p in products]
