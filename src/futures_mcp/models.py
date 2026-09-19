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
