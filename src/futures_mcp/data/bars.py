"""OHLCV bars from TradingView (via tvdatafeed) with Relative Volume [ND] replicated.

Ported from Trading_bot ``src/data/bar_fetcher.py``. The output of
:func:`window_payload` matches that module's per-timeframe section, which was
verified against the chart legend: OHLC exact to the tick, volume exact to the
contract, SMA14 exact to display precision.

Relative Volume [ND]::

    sma14 = sma(volume, 14);  rel_volume = volume / sma14
    zones  < 0.5 very_low | < 1.0 below_average | < 1.9 above_average
           < 2.2 high     | < 3.5 very_high     | else ultra_high
    histogram colour: pink if volume fell vs both prior bars; blue/red if it
    rose and the bar closed up/down; otherwise black.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import anyio
import pandas as pd

from ..symbols import Instrument
from .timewindow import CHART_TZ, UTC, window_utc

logger = logging.getLogger(__name__)

RELVOL_LENGTH = 14
VOLUME_ZONES: list[tuple[float, str]] = [
    (0.5, "very_low"),
    (1.0, "below_average"),
    (1.9, "above_average"),
    (2.2, "high"),
    (3.5, "very_high"),
    (float("inf"), "ultra_high"),
]
#: timeframe -> (tvdatafeed interval name, bars requested). H1 3000 ~ 5.5 months.
TIMEFRAMES: dict[str, tuple[str, int]] = {
    "H1": ("in_1_hour", 3000),
    "H4": ("in_4_hour", 600),
}
FETCH_RETRIES = 4
RELVOL_NOTE = (
    "Relative Volume [ND] replicated: rel_volume = volume / SMA(volume,14); "
    "zones from band multipliers 0.5/1.9/2.2/3.5"
)
TIMEZONE_NOTE = (
    "time_chart_utc5 matches the TradingView chart axis (UTC+5 display); "
    "day_chart groups bars by that axis's calendar day"
)


class DataUnavailableError(RuntimeError):
    """TradingView returned nothing usable for the request."""


def check_timeframe(tf: str) -> str:
    tf = tf.strip().upper()
    if tf not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {tf!r}. Supported: {', '.join(TIMEFRAMES)}.")
    return tf


# --------------------------------------------------------------- pure parts --
def session_tag(hour_utc: int) -> str:
    if hour_utc < 7:
        return "asian"
    if hour_utc < 12:
        return "london"
    if hour_utc < 16:
        return "london_ny"
    if hour_utc < 21:
        return "new_york"
    return "post_ny"


def volume_zone(rel: float) -> str:
    for bound, label in VOLUME_ZONES:
        if rel < bound:
            return label
    return "ultra_high"


def prepare(df: pd.DataFrame, source_tz: Any = None) -> pd.DataFrame:
    """Localise tvdatafeed's naive timestamps and add the indicator inputs.

    tvdatafeed returns *machine-local* naive timestamps; the conversion is pinned
    explicitly rather than trusting OS settings. ``source_tz`` overrides the
    machine zone (used by tests).
    """
    tz = source_tz or datetime.now().astimezone().tzinfo
    df = df.copy()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize(tz)
    df.index = idx.tz_convert(UTC)
    df["sma14"] = df["volume"].rolling(RELVOL_LENGTH).mean()
    df["vol_prev1"] = df["volume"].shift(1)
    df["vol_prev2"] = df["volume"].shift(2)
    df["close_prev"] = df["close"].shift(1)
    return df


def _hist_color(row: Any) -> str:
    if row.volume < row.vol_prev1 and row.volume < row.vol_prev2:
        return "pink_falling"
    if row.volume > row.vol_prev1 or row.volume > row.vol_prev2:
        if row.close > row.open:
            return "blue_rising_bull"
        if row.close < row.open:
            return "red_rising_bear"
    return "black_neutral"


def bar_records(win: pd.DataFrame) -> list[dict[str, Any]]:
    win_max = float(win["volume"].max())
    last_close = win["close"].iloc[-1]
    price_dec = 5 if last_close < 100 else (3 if last_close < 1000 else 1)
    records = []
    for ts, row in win.iterrows():
        ts_utc = pd.Timestamp(ts)  # type: ignore[arg-type]
        ts_chart = ts_utc.tz_convert(CHART_TZ)
        rng = row.high - row.low
        has_sma = pd.notna(row.sma14) and bool(row.sma14)
        rel = row.volume / row.sma14 if has_sma else None
        if pd.notna(row.close_prev):
            direction = ("up" if row.close > row.close_prev
                         else "down" if row.close < row.close_prev else "flat")
        else:
            direction = None
        records.append({
            "time_chart_utc5": ts_chart.strftime("%Y-%m-%d %H:%M"),
            "time_utc": ts_utc.strftime("%Y-%m-%d %H:%M"),
            "day_chart": ts_chart.strftime("%Y-%m-%d"),
            "session": session_tag(ts_utc.hour),
            "high": round(float(row.high), price_dec),
            "low": round(float(row.low), price_dec),
            "close": round(float(row.close), price_dec),
            "bar_direction": direction,
            "close_position_in_range": round(float((row.close - row.low) / rng), 2)
            if rng > 0 else None,
            "volume": int(row.volume),
            "sma14": round(float(row.sma14), 1) if pd.notna(row.sma14) else None,
            "rel_volume": round(float(rel), 2) if rel is not None else None,
            "volume_zone": volume_zone(rel) if rel is not None else None,
            "hist_color": _hist_color(row) if pd.notna(row.vol_prev2) else None,
            "frac_of_window_max_volume": round(float(row.volume) / win_max, 2)
            if win_max else None,
        })
    return records


def day_summaries(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    days: dict[str, list[dict[str, Any]]] = {}
    for b in bars:
        days.setdefault(b["day_chart"], []).append(b)
    out = []
    for day, rows in sorted(days.items()):
        top3 = sorted(rows, key=lambda r: r["volume"], reverse=True)[:3]
        out.append({
            "day_chart": day,
            "bars": len(rows),
            "high": max(r["high"] for r in rows),
            "low": min(r["low"] for r in rows),
            "close": rows[-1]["close"],
            "total_volume": sum(r["volume"] for r in rows),
            "top3_volume_bars": [
                {k: r[k] for k in ("time_chart_utc5", "volume", "frac_of_window_max_volume",
                                   "volume_zone", "bar_direction", "close_position_in_range")}
                for r in top3
            ],
        })
    return out


def window_payload(
    df: pd.DataFrame, inst: Instrument, tf: str, target: date, days: int,
    end_utc: datetime | None = None,
) -> dict[str, Any]:
    """Slice a prepared frame to the trading-day window and build the payload."""
    start, end = window_utc(target, days)
    if end_utc is not None:
        end = min(end, end_utc)
    win = df.loc[(df.index >= start) & (df.index < end)]
    if win.empty:
        have = f"{df.index[0]:%Y-%m-%d %H:%M}..{df.index[-1]:%Y-%m-%d %H:%M} UTC" if len(df) \
            else "nothing"
        raise DataUnavailableError(
            f"{inst.symbol} {tf}: no bars in window {start:%Y-%m-%d %H:%M}..{end:%Y-%m-%d %H:%M} "
            f"UTC (fetched history covers {have}). Is the date a market holiday or too far back?"
        )
    bars = bar_records(win)
    return {
        "symbol": inst.tv_symbol,
        "timeframe": tf,
        "target_date": str(target),
        "window_trading_days": days,
        "window_utc": [start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")],
        "window_complete": end <= datetime.now(UTC),
        "timezone_note": TIMEZONE_NOTE,
        "relative_volume_indicator": RELVOL_NOTE,
        "n_bars": len(bars),
        "window_max_volume_bar": max(bars, key=lambda b: b["volume"]),
        "days": day_summaries(bars),
        "bars": bars,
    }


# ------------------------------------------------------------------ network --
@dataclass
class _Cached:
    fetched_at: float
    frame: pd.DataFrame


class BarFeed:
    """Thread-safe tvdatafeed client with an in-memory history cache and a disk
    cache of closed windows (a closed window never changes, so it is kept)."""

    def __init__(self, cache_dir: Path, live_ttl: int = 300, pace_seconds: float = 3.0):
        self.cache_dir = cache_dir
        self.live_ttl = live_ttl
        self.pace_seconds = pace_seconds
        self._tv: Any = None
        self._lock = threading.Lock()
        self._history: dict[tuple[str, str], _Cached] = {}
        self._last_fetch = 0.0

    # -- tvdatafeed ---------------------------------------------------------
    def _client(self) -> Any:
        if self._tv is None:
            try:
                from tvDatafeed import TvDatafeed
            except ImportError as exc:  # pragma: no cover - environment problem
                raise DataUnavailableError(
                    "tvdatafeed is not installed: pip install "
                    '"tvdatafeed @ git+https://github.com/stefanomorni/fork-tvdatafeed.git"'
                ) from exc
            logging.getLogger("tvDatafeed").setLevel(logging.ERROR)
            self._tv = TvDatafeed()  # anonymous: enough for delayed CME continuous contracts
        return self._tv

    def _fetch_hist(self, inst: Instrument, tf: str) -> pd.DataFrame:
        from tvDatafeed import Interval

        interval_name, n_bars = TIMEFRAMES[tf]
        interval = getattr(Interval, interval_name)
        tv = self._client()
        for attempt in range(1, FETCH_RETRIES + 1):
            # Pace consecutive connections: TradingView throttles rapid anonymous sockets.
            wait = self.pace_seconds - (time.monotonic() - self._last_fetch)
            if wait > 0:
                time.sleep(wait)
            try:
                df = tv.get_hist(symbol=inst.symbol, exchange=inst.exchange,
                                 interval=interval, n_bars=n_bars)
            except Exception as exc:  # tvdatafeed raises bare websocket errors
                logger.warning("fetch %s %s attempt %d raised %r", inst.tv_symbol, tf, attempt, exc)
                df = None
            self._last_fetch = time.monotonic()
            if df is not None and not df.empty:
                return df
            logger.warning("fetch %s %s attempt %d/%d returned no data",
                           inst.tv_symbol, tf, attempt, FETCH_RETRIES)
            if attempt < FETCH_RETRIES:
                time.sleep(5 * attempt)
                tv.__init__()  # fresh session/chart ids for the next websocket
        raise DataUnavailableError(
            f"TradingView returned no data for {inst.tv_symbol} {tf} after {FETCH_RETRIES} "
            "attempts (likely throttling). Wait a minute and retry."
        )

    def history(self, inst: Instrument, tf: str) -> pd.DataFrame:
        key = (inst.symbol, tf)
        with self._lock:
            hit = self._history.get(key)
            if hit and time.monotonic() - hit.fetched_at < self.live_ttl:
                return hit.frame
            frame = prepare(self._fetch_hist(inst, tf))
            self._history[key] = _Cached(time.monotonic(), frame)
            return frame

    # -- windows ------------------------------------------------------------
    def _disk_path(self, inst: Instrument, tf: str, target: date, days: int) -> Path:
        return self.cache_dir / inst.folder / f"{tf}_{target}_{days}d.json"

    def window(self, inst: Instrument, tf: str, target: date, days: int) -> dict[str, Any]:
        tf = check_timeframe(tf)
        path = self._disk_path(inst, tf, target, days)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        payload = window_payload(self.history(inst, tf), inst, tf, target, days)
        if payload["window_complete"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        return payload

    async def awindow(self, inst: Instrument, tf: str, target: date, days: int) -> dict[str, Any]:
        return await anyio.to_thread.run_sync(self.window, inst, tf, target, days)
