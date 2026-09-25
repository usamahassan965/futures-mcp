"""Typed tool outputs. FastMCP turns these into each tool's ``outputSchema``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["COMPLETED", "NOT_COMPLETED", "NO_RANGE"]


class Bar(BaseModel):
    time_chart_utc5: str = Field(description="Bar open time on TradingView's UTC+5 axis")
    time_utc: str
    day_chart: str
    session: str = Field(
        description="asian | london | london_ny | new_york | post_ny (by UTC hour)")
    high: float
    low: float
    close: float
    bar_direction: str | None = Field(description="close vs previous close: up | down | flat")
    close_position_in_range: float | None = Field(description="0 = closed at low, 1 = at high")
    volume: int
    sma14: float | None
    rel_volume: float | None = Field(description="volume / SMA14 (Relative Volume [ND])")
    volume_zone: str | None
    hist_color: str | None
    frac_of_window_max_volume: float | None


class TopVolumeBar(BaseModel):
    time_chart_utc5: str
    volume: int
    frac_of_window_max_volume: float | None
    volume_zone: str | None
    bar_direction: str | None
    close_position_in_range: float | None


class DaySummary(BaseModel):
    day_chart: str
    bars: int
    high: float
    low: float
    close: float
    total_volume: int
    top3_volume_bars: list[TopVolumeBar]


class BarsResult(BaseModel):
    symbol: str
    timeframe: str
    target_date: str
    window_trading_days: int
    window_utc: list[str] = Field(description="[start, end) in UTC")
    window_complete: bool = Field(description="False while the window's last session is still open")
    timezone_note: str
    relative_volume_indicator: str
    n_bars: int
    window_max_volume_bar: Bar
    days: list[DaySummary]
    bars: list[Bar] | None = Field(description="Omitted when include_bars=false")


class Rejection(BaseModel):
    n: int
    side: Literal["R", "S"] = Field(description="R = rejected at resistance, S = at support")
    price: float
    time: str = Field(description="UTC+5 chart time of the bar carrying the extreme")


class RangeStructure(BaseModel):
    verdict: Literal["COMPLETED", "NOT_COMPLETED"]
    support: float
    resistance: float
    height: float
    rejections: list[Rejection]
    broke: str | None = Field(description="UTC+5 chart time the structure broke, if it did")
    window_days: int = Field(description="Session days the detector needed to see it")
    score: float
    caption: str


class TradeTarget(BaseModel):
    r_multiple: float = Field(description="Multiple of the risk distance")
    price: float


class Account(BaseModel):
    equity: float
    risk_pct: float = Field(description="Percent of equity risked per trade")
    contract: str = Field(description="Contract the position is sized in, e.g. MGC")
    point_value: float = Field(description="Account currency per 1.00 point, one contract")
    min_contracts: int
    targets: list[float]


class TradePlan(BaseModel):
    """One structure's trade, or why there isn't one. Prices come from the rules, never
    from a model; the server re-checks the arithmetic before returning them."""

    structure_index: int = Field(description="Index into the range report's structures")
    support: float
    resistance: float
    signal: bool = Field(description="False when the rules see no trade yet")
    reason: str = Field(description="What armed the trade, or what is missing")
    direction: Literal["long", "short"] | None = None
    order: str | None = Field(default=None, description="buy_stop | sell_stop | market")
    entry: float | None = None
    entry_basis: str | None = None
    stop: float | None = None
    stop_basis: str | None = None
    risk_points: float | None = Field(default=None, description="|entry - stop|")
    risk_per_contract: float | None = None
    contracts: int | None = None
    risk_dollars: float | None = None
    risk_pct_actual: float | None = Field(
        default=None, description="Risk of the sized position, which can exceed risk_pct")
    take_profit: float | None = Field(
        default=None, description="The strategy's own exit level, when it is not an R multiple")
    take_profit_basis: str | None = None
    take_profit_r: float | None = Field(default=None, description="Take profit in R")
    targets: list[TradeTarget] = []
    signal_rejection: int | None = Field(default=None, description="Rejection that armed it")
    confirm_time: str | None = Field(default=None, description="UTC+5 chart time")
    order_life: str | None = Field(default=None, description="When the order is cancelled")
    exits: list[str] = []
    triggered: bool | None = Field(
        default=None, description="Whether the order already traded through in this window")
    triggered_time: str | None = None
    notes: list[str] = []


class RangeReport(BaseModel):
    symbol: str
    timeframe: str
    target_date: str
    window_trading_days: int
    window_utc: list[str]
    window_complete: bool
    verdict: Verdict
    summary: str
    structures: list[RangeStructure]
    candidates: dict[str, int] = Field(
        description="Detector hit counts before selection: completed / anticipation / demoted")


class TradePlanReport(BaseModel):
    range_report: RangeReport
    rules_version: str = Field(description="Version string of the loaded trading rules")
    account: Account
    plans: list[TradePlan] = Field(description="One per detected structure, in the same order")


class BacktestTrade(BaseModel):
    detected_utc: str = Field(description="Bar at which the detector first saw the signal")
    confirm_utc: str
    direction: Literal["long", "short"]
    S: float
    R: float
    entry: float
    stop: float
    target: float | None = Field(description="The exit level simulated in this run")
    risk_points: float
    contracts: int
    status: Literal["cancelled", "closed", "open"]
    cancel_reason: str | None
    ambiguous: bool = Field(description="A bar touched both stop and target; counted as the stop")
    fill_utc: str | None = None
    fill_price: float | None = None
    exit_utc: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    r: float | None = Field(description="Result in multiples of the planned risk")
    pnl: float | None = Field(description="Account currency, no costs")
    held_bars: int | None = None
    roll_gap_inside: bool | None = Field(
        default=None, description="A likely contract-roll gap fell inside the trade")


class BacktestStats(BaseModel):
    orders: int
    cancelled: dict[str, int]
    filled: int
    closed: int
    still_open: int
    wins: int
    losses: int
    win_rate: float | None
    avg_r: float | None
    total_r: float
    total_pnl: float
    max_drawdown: float
    profit_factor: float | None
    exit_reasons: dict[str, int]
    ambiguous_bars: int
    roll_gap_trades: int


class BacktestRun(BaseModel):
    exit: str = Field(description="'tp' (the strategy's own take profit) or an R multiple")
    stats: BacktestStats
    skipped: dict[str, int] = Field(description="Signals not traded, by reason")
    trades: list[BacktestTrade]


class BacktestReport(BaseModel):
    symbol: str
    timeframe: str
    start: str
    end: str
    window_trading_days: int
    rules_version: str
    account: Account
    steps: int = Field(description="Bars at which the detector was re-run")
    signals: int = Field(description="Unique signals the rules armed")
    stale_signals: int = Field(
        description="Signals whose order had already filled or been cancelled when detected")
    roll_gaps: list[str] = Field(description="Bars that opened far from the previous close")
    runs: list[BacktestRun]
    notes: list[str]
