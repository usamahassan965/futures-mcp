"""The operations behind the MCP tools, free of protocol concerns."""

from __future__ import annotations

import io
import logging
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import anyio
from PIL import Image

from .capture.browser import BrowserManager
from .capture.tradingview import Capture, Progress, capture_window
from .config import Settings
from .data.bars import BarFeed, check_timeframe
from .data.timewindow import window_utc
from .models import (
    Account,
    BacktestReport,
    BarsResult,
    RangeReport,
    RangeStructure,
    Rejection,
    TradePlan,
    TradePlanReport,
)
from .ranges import overlay, pipeline
from .ranges.detector import detector_available
from .symbols import Instrument, resolve
from .trading import backtest
from .trading.rules import (
    RulesUnavailableError,
    build_plan,
    load_rules,
    rules_available,
)

logger = logging.getLogger(__name__)

RANGE_TIMEFRAMES = ("H1",)
BACKTEST_MAX_DAYS = 120
BACKTEST_EXITS: tuple[str | float, ...] = ("tp", 1.0, 2.0, 3.0)
WINDOWS_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


def _check_days(days: int, lo: int = 1, hi: int = 30) -> int:
    if not lo <= days <= hi:
        raise ValueError(f"days must be between {lo} and {hi} (got {days}).")
    return days


def _range_tf(tf: str) -> str:
    tf = check_timeframe(tf)
    if tf not in RANGE_TIMEFRAMES:
        raise ValueError("Range detection is calibrated on H1 only; use timeframe='H1'.")
    return tf


@dataclass
class RangeChart:
    report: RangeReport
    png: bytes
    path: Path
    drawn: bool
    mode: str
    warnings: list[str] = field(default_factory=list)


class FuturesService:
    def __init__(self, settings: Settings, feed: BarFeed | None = None,
                 browser: BrowserManager | None = None):
        self.settings = settings
        self.feed = feed or BarFeed(settings.data_dir / "bars", settings.live_cache_ttl)
        # One browser per capture mode, each started on first use.
        self._browsers: dict[str, BrowserManager] = {}
        if browser is not None:
            self._browsers[browser.mode] = browser
        if settings.tesseract_cmd:
            overlay.set_tesseract_cmd(settings.tesseract_cmd)
        elif not shutil.which("tesseract") and WINDOWS_TESSERACT.exists():
            overlay.set_tesseract_cmd(str(WINDOWS_TESSERACT))

    # ------------------------------------------------------------------ info --
    def status(self) -> dict[str, Any]:
        return {
            "capture_mode": self.settings.capture_mode,
            "session_mode_available": self.settings.session_mode,
            "range_detector_installed": detector_available(self.settings.detector_dir),
            "trading_rules_installed": rules_available(self.settings.trading_rules_dir),
            "tesseract_found": bool(shutil.which("tesseract") or WINDOWS_TESSERACT.exists()
                                    or self.settings.tesseract_cmd),
            "data_dir": str(self.settings.data_dir.resolve()),
        }

    def browser_for(self, mode: str | None) -> BrowserManager:
        mode = mode or self.settings.capture_mode
        if mode not in ("anonymous", "session"):
            raise ValueError(f"Unknown capture mode {mode!r}: use 'anonymous' or 'session'")
        if mode == "session" and not self.settings.session_mode:
            raise ValueError("Session mode needs TRADINGVIEW_SESSION_ID (and TRADINGVIEW_URL "
                             "for your layout) in ~/.futures-mcp/.env; restart the client "
                             "after setting it. Use mode='anonymous' meanwhile.")
        if mode not in self._browsers:
            self._browsers[mode] = BrowserManager(self.settings, mode)
        return self._browsers[mode]

    # ------------------------------------------------------------------ bars --
    async def bars(self, symbol: str, timeframe: str, days: int, target: date,
                   include_bars: bool = True) -> BarsResult:
        inst = resolve(symbol)
        payload = await self.feed.awindow(inst, check_timeframe(timeframe), target,
                                          _check_days(days))
        result = BarsResult.model_validate(payload)
        if not include_bars:
            result.bars = None
        return result

    # --------------------------------------------------------------- capture --
    async def capture(self, symbol: str, timeframe: str, days: int, target: date,
                      progress: Progress | None = None, mode: str | None = None) -> Capture:
        inst = resolve(symbol)
        return await capture_window(self.browser_for(mode), inst, check_timeframe(timeframe),
                                    target, _check_days(days), self._dir("captures", inst),
                                    progress)

    # ---------------------------------------------------------------- ranges --
    def _analyze_payload(self, inst: Instrument, payload: dict[str, Any]) -> tuple[
            RangeReport, list[dict[str, Any]]]:
        result = pipeline.analyze(payload["bars"], inst.code, self.settings.detector_dir)
        report = RangeReport(
            symbol=payload["symbol"], timeframe=payload["timeframe"],
            target_date=payload["target_date"],
            window_trading_days=payload["window_trading_days"],
            window_utc=payload["window_utc"], window_complete=payload["window_complete"],
            verdict=result["verdict"],
            summary=_summary(result["verdict"], result["structures"]),
            structures=[_structure(s) for s in result["structures"]],
            candidates={k: len(v) for k, v in result["raw"].items()},
        )
        # The rules measure against what the detector measured, not the drawn box.
        return report, [s | {"detector": c}
                        for s, c in zip(result["structures"], result["context"], strict=True)]

    async def analyze(self, symbol: str, timeframe: str, days: int, target: date) -> RangeReport:
        inst = resolve(symbol)
        tf = _range_tf(timeframe)
        payload = await self.feed.awindow(inst, tf, target, _check_days(days, 2, 10))
        report, _ = await anyio.to_thread.run_sync(self._analyze_payload, inst, payload)
        return report

    # ------------------------------------------------------------- trade plan --
    def _account(self, inst: Instrument) -> dict[str, Any]:
        return {"equity": self.settings.account_equity, "risk_pct": self.settings.risk_pct,
                "point_value": inst.point_value, "min_contracts": 1,
                "targets": list(self.settings.targets), "symbol": inst.symbol}

    def _plan_payload(self, inst: Instrument, payload: dict[str, Any]) -> TradePlanReport:
        report, structures = self._analyze_payload(inst, payload)
        mod = load_rules(self.settings.trading_rules_dir)
        account = self._account(inst)
        plans = []
        for i, s in enumerate(structures):
            out = build_plan(mod, payload["bars"], s, account)
            plans.append(TradePlan.model_validate(
                out | {"structure_index": i, "support": s["S"], "resistance": s["R"]}))
        return TradePlanReport(range_report=report, rules_version=str(mod.RULES_VERSION),
                               account=Account.model_validate(account), plans=plans)

    async def trade_plan(self, symbol: str, timeframe: str, days: int,
                         target: date) -> TradePlanReport:
        inst = resolve(symbol)
        tf = _range_tf(timeframe)
        days = _check_days(days, 2, 10)
        # Fail fast, before the bar fetch, when either private module is missing.
        load_rules(self.settings.trading_rules_dir)
        payload = await self.feed.awindow(inst, tf, target, days)
        return await anyio.to_thread.run_sync(self._plan_payload, inst, payload)

    # --------------------------------------------------------------- backtest --
    def _backtest(self, inst: Instrument, start: date, end: date, days: int,
                  tick: backtest.Progress | None) -> BacktestReport:
        mod = load_rules(self.settings.trading_rules_dir)
        history = self.feed.history(inst, "H1")
        # The first bar traded needs a full detector window behind it.
        need = window_utc(start, days)[0]
        if history.index[0] > need:
            raise ValueError(f"H1 history only reaches back to {history.index[0]:%Y-%m-%d}; a "
                             f"{days}-day window from {start} needs bars from {need:%Y-%m-%d}.")
        account = self._account(inst)
        detected = backtest.detect_signals(history, inst, start, end,
                                           self.settings.detector_dir, mod, account, days, tick)
        runs = [backtest.run(detected, mod, x, inst.point_value, days) for x in BACKTEST_EXITS]
        return BacktestReport.model_validate({
            "symbol": inst.symbol, "timeframe": "H1", "start": str(start), "end": str(end),
            "window_trading_days": days, "rules_version": str(mod.RULES_VERSION),
            "account": account, "steps": detected["steps"],
            "signals": len(detected["signals"]),
            "stale_signals": sum(1 for sig in detected["signals"] if sig["stale"]),
            "roll_gaps": runs[0]["roll_gaps"],
            "runs": [r | {"exit": x if x == "tp" else f"{x:g}R"}
                     for r, x in zip(runs, BACKTEST_EXITS, strict=True)],
            "notes": [
                "No commissions or slippage are modelled.",
                "A bar that touches both the stop and the target counts as the stop.",
                "One trade at a time; one signal per range.",
                "Continuous contract: roll gaps are flagged, not adjusted.",
            ],
        })

    async def backtest(self, symbol: str, start: date, end: date, days: int = 3,
                       progress: Progress | None = None) -> BacktestReport:
        inst = resolve(symbol)
        days = _check_days(days, 2, 10)
        if end < start:
            raise ValueError(f"end {end} is before start {start}.")
        if (end - start).days > BACKTEST_MAX_DAYS:
            raise ValueError(f"Backtests are limited to {BACKTEST_MAX_DAYS} calendar days.")
        # Fail fast, before the history fetch, when the rules cannot replay orders.
        if not hasattr(load_rules(self.settings.trading_rules_dir), "simulate"):
            raise RulesUnavailableError("The trading rules have no simulate(); cannot backtest.")
        tick = _thread_progress(progress) if progress else None
        out = await anyio.to_thread.run_sync(self._backtest, inst, start, end, days, tick)
        if progress:
            await progress(1.0, "done")
        return out

    async def range_chart(self, symbol: str, timeframe: str, days: int, target: date,
                          progress: Progress | None = None,
                          mode: str | None = None) -> RangeChart:
        inst = resolve(symbol)
        tf = _range_tf(timeframe)
        days = _check_days(days, 2, 10)
        browser = self.browser_for(mode)
        # Fail fast before spending ~30s in the browser.
        if not detector_available(self.settings.detector_dir):
            pipeline.analyze([], inst.code, self.settings.detector_dir)  # raises a clear error

        async def scaled(p: float, msg: str) -> None:
            if progress:
                await progress(0.8 * p, msg)

        # Bars and screenshot for the same window, fetched concurrently.
        payload: dict[str, Any] = {}

        async def get_bars() -> None:
            payload.update(await self.feed.awindow(inst, tf, target, days))

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(get_bars)
                shot = await capture_window(browser, inst, tf, target, days,
                                            self._dir("captures", inst), scaled)
        except BaseExceptionGroup as group:
            # Surface the first real failure (the other task was cancelled by it).
            raise _first_leaf(group) from group
        if progress:
            await progress(0.85, "detecting ranges and calibrating the chart")
        report, structures = await anyio.to_thread.run_sync(self._analyze_payload, inst, payload)
        out = self._dir("marked", inst) / shot.path.name.replace(".png", "_marked.png")
        drawn, warnings = await anyio.to_thread.run_sync(
            self._mark, shot.path, out, payload["bars"], structures, shot.mode)
        if progress:
            await progress(1.0, "done")
        png = out.read_bytes() if drawn else shot.png
        return RangeChart(report, png, out if drawn else shot.path, drawn, shot.mode,
                          shot.warnings + warnings)

    def _mark(self, png: Path, out: Path, bars: list[dict[str, Any]],
              structures: list[dict[str, Any]], mode: str) -> tuple[bool, list[str]]:
        img = Image.open(io.BytesIO(png.read_bytes()))
        cal, err = overlay.calibrate(img, bars, overlay.PANE_Y[mode])
        if err or not cal or not cal["trustworthy"]:
            why = err or _untrusted(cal)
            return False, [f"Chart calibration failed ({why}); returning the unmarked "
                           "screenshot. The verdict and levels above are unaffected."]
        overlay.render(png, out, bars, cal, structures)
        return True, []

    def _dir(self, kind: str, inst: Instrument) -> Path:
        d = self.settings.data_dir / kind / inst.folder
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def aclose(self) -> None:
        for browser in self._browsers.values():
            await browser.close()


def _thread_progress(progress: Progress) -> backtest.Progress:
    """Report a worker thread's step counter to the async progress callback, ~20 times."""
    async def send(p: float, message: str) -> None:
        await progress(p, message)

    def tick(k: int, n: int) -> None:
        if n and (k == n or k % max(1, n // 20) == 0):
            anyio.from_thread.run(send, 0.95 * k / n, f"replayed {k} of {n} bars")
    return tick


def _untrusted(cal: dict[str, Any]) -> str:
    if not cal["span_ok"]:
        return "the window's high/low do not land inside the price pane"
    return (f"axis fit too loose: price {cal['rms_price_px']:.2f}px, "
            f"time {cal['rms_x_px']:.2f}px")


def _first_leaf(group: BaseExceptionGroup) -> BaseException:
    exc: BaseException = group
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


def _structure(s: dict[str, Any]) -> RangeStructure:
    return RangeStructure(
        verdict=s["verdict"], support=s["S"], resistance=s["R"],
        height=round(s["R"] - s["S"], 5),
        rejections=[Rejection(n=r["n"], side=r["side"], price=r["price"], time=r["time"])
                    for r in s["rejections"]],
        broke=s["broke"], window_days=s["window_days"], score=s["score"], caption=s["caption"],
    )


def _summary(verdict: str, structures: list[dict[str, Any]]) -> str:
    if verdict == "NO_RANGE":
        return "No range structure in this window."
    parts = []
    for s in structures:
        state = "completed" if s["verdict"] == "COMPLETED" else "not completed"
        brk = f", broke {s['broke']}" if s.get("broke") else ", still holding"
        parts.append(f"{state} range S {s['S']} / R {s['R']} with "
                     f"{len(s['rejections'])} rejections{brk}")
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."
