"""Walk-forward backtest of the private trading rules over the range detector.

No hindsight: at every bar ``t`` the detector sees only the last ``days`` trading
days up to and including ``t``, exactly what the live tool would have seen at that
moment. A structure whose rules arm a trade places the order on ``t``; what the
order does afterwards is replayed by the rules' own ``simulate`` over the bars that
follow. The rules decide fills and exits; this module only schedules signals,
enforces portfolio constraints and counts.

Portfolio constraints:

* one shot per range - once a range has produced a signal, later signals on a range
  within a quarter of its height on both lines are ignored for ``days`` trading days
  (the sliding window can renumber the same range's rejections);
* one trade at a time - a signal is skipped while an order rests or a trade is open;
* a signal whose order would already have filled or been cancelled between its
  confirming bar and the bar the detector first saw it is counted as stale, not traded.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd

from ..data.bars import bar_records, window_payload
from ..ranges import pipeline
from ..symbols import Instrument
from .rules import RulesUnavailableError, build_plan

#: A gap between one bar's open and the previous close this many median bar ranges wide
#: is flagged as a possible contract roll on a continuous series.
ROLL_GAP_MEDIANS = 4.0

Progress = Callable[[int, int], None]


def _target_of(t: datetime) -> date:
    """The window's end date for a bar: its UTC date, weekend bars to the next Monday."""
    d = t.date()
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _with_open(bars: list[dict[str, Any]], frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Bar records carry no open; the simulator needs it for gaps."""
    stamps = pd.DatetimeIndex(frame.index).strftime("%Y-%m-%d %H:%M")
    opens = dict(zip(stamps, frame["open"].astype(float), strict=True))
    return [b | {"open": opens.get(b["time_utc"])} for b in bars]


def _overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    h = b["R"] - b["S"]
    return abs(a["S"] - b["S"]) < 0.25 * h and abs(a["R"] - b["R"]) < 0.25 * h


def detect_signals(history: pd.DataFrame, inst: Instrument, start: date, end: date,
                   detector_dir: Path, rules: ModuleType, account: dict[str, Any],
                   days: int = 3, progress: Progress | None = None) -> dict[str, Any]:
    """Run the detector and the rules at every H1 bar from ``start`` to ``end``.

    ``history`` is a prepared H1 frame (UTC index, ``open``/``high``/``low``/``close``/
    ``volume`` plus the relative-volume inputs) covering ``days`` trading days before
    ``start``. Returns the signals in detection order and the bar list they index."""
    if not hasattr(rules, "simulate"):
        raise RulesUnavailableError("The trading rules have no simulate(); cannot backtest.")
    lo = datetime(start.year, start.month, start.day, tzinfo=UTC)
    hi = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    steps = history.index[(history.index >= lo) & (history.index < hi)]
    all_bars = _with_open(bar_records(history.loc[history.index >= lo]), history)
    pos = {b["time_utc"]: i for i, b in enumerate(all_bars)}

    seen: set[tuple[str, str]] = set()
    signals: list[dict[str, Any]] = []
    for k, ts in enumerate(steps):
        if progress:
            progress(k, len(steps))
        t = pd.Timestamp(ts).to_pydatetime()
        try:
            payload = window_payload(history, inst, "H1", _target_of(t), days,
                                     end_utc=t + timedelta(hours=1))
        except Exception:
            continue
        bars = _with_open(payload["bars"], history)
        result = pipeline.analyze(bars, inst.code, detector_dir)
        for s, ctx in zip(result["structures"], result["context"], strict=True):
            structure = s | {"detector": ctx}
            plan = build_plan(rules, bars, structure, account)
            if not plan["signal"]:
                continue
            confirm_idx = int(plan.get("confirm_idx", len(bars) - 1))
            confirm = bars[confirm_idx]
            key = (confirm["time_utc"], plan["direction"])
            if key in seen:
                continue
            seen.add(key)
            # What the order would already have done before the detector saw it.
            stale = None
            if confirm_idx < len(bars) - 1:
                before = rules.simulate(bars, plan, structure, confirm_idx, "tp")
                if before["fill_idx"] is not None:
                    stale = "filled before the detector saw the signal"
                elif before["status"] == "cancelled":
                    stale = ("cancelled before the detector saw the signal "
                             f"({before['cancel_reason']})")
            signals.append({
                "detected_utc": bars[-1]["time_utc"], "placed_idx": pos[bars[-1]["time_utc"]],
                "confirm_utc": confirm["time_utc"], "lag_bars": len(bars) - 1 - confirm_idx,
                "S": s["S"], "R": s["R"], "verdict": s["verdict"],
                "rejections": len(s["rejections"]), "plan": plan, "structure": structure,
                "stale": stale,
            })
    if progress:
        progress(len(steps), len(steps))
    return {"signals": signals, "bars": all_bars, "steps": len(steps)}


def roll_gaps(bars: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bars that open far from the previous close: likely contract rolls."""
    ranges = sorted(b["high"] - b["low"] for b in bars)
    med = ranges[len(ranges) // 2] if ranges else 0.0
    out = []
    for prev, b in pairwise(bars):
        if b.get("open") is None:
            continue
        gap = b["open"] - prev["close"]
        if med and abs(gap) > ROLL_GAP_MEDIANS * med:
            out.append({"time_utc": b["time_utc"], "gap": round(gap, 2)})
    return out


def run(detected: dict[str, Any], rules: ModuleType, exit: str | float,
        point_value: float, days: int = 3) -> dict[str, Any]:
    """Apply the portfolio constraints and replay every placed order for one exit mode."""
    bars = detected["bars"]
    gaps = {g["time_utc"] for g in roll_gaps(bars)}
    used: list[dict[str, Any]] = []
    busy_until = -1
    trades: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    for sig in detected["signals"]:
        placed = sig["placed_idx"]
        recent = [u for u in used if placed - u["placed_idx"] <= days * 24]
        if any(_overlaps(sig, u) for u in recent):
            skipped["same range already signalled"] += 1
            continue
        used.append(sig)
        if sig["stale"]:
            skipped["stale: " + sig["stale"]] += 1
            continue
        if placed <= busy_until:
            skipped["another order or trade was live"] += 1
            continue
        plan = sig["plan"]
        sim = rules.simulate(bars, plan, sig["structure"], placed, exit)
        if sim["status"] == "closed":
            busy_until = sim["exit_idx"]
        elif sim["status"] == "cancelled":
            busy_until = sim.get("cancel_idx") or placed
        else:
            busy_until = len(bars)
        trade = {
            "detected_utc": sig["detected_utc"], "confirm_utc": sig["confirm_utc"],
            "direction": plan["direction"], "S": sig["S"], "R": sig["R"],
            "entry": plan["entry"], "stop": plan["stop"], "target": sim["target"],
            "risk_points": plan["risk_points"], "contracts": plan["contracts"],
            "status": sim["status"], "cancel_reason": sim["cancel_reason"],
            "ambiguous": sim["ambiguous"], "r": None, "pnl": None,
        }
        if sim["fill_idx"] is not None:
            trade |= {"fill_utc": bars[sim["fill_idx"]]["time_utc"],
                      "fill_price": sim["fill_price"]}
        if sim["status"] == "closed":
            sign = -1.0 if plan["direction"] == "short" else 1.0
            move = sign * (sim["exit_price"] - sim["fill_price"])
            trade |= {"exit_utc": bars[sim["exit_idx"]]["time_utc"],
                      "exit_price": sim["exit_price"], "exit_reason": sim["exit_reason"],
                      "r": round(move / plan["risk_points"], 3),
                      "pnl": round(move * plan["contracts"] * point_value, 2),
                      "held_bars": sim["exit_idx"] - sim["fill_idx"] + 1}
            span = {bars[j]["time_utc"] for j in range(sim["fill_idx"], sim["exit_idx"] + 1)}
            trade["roll_gap_inside"] = bool(span & gaps)
        trades.append(trade)
    return {"exit": exit, "trades": trades, "skipped": dict(skipped),
            "stats": stats(trades), "roll_gaps": sorted(gaps)}


def stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [t for t in trades if t["r"] is not None]
    wins = [t for t in closed if t["r"] > 0]
    losses = [t for t in closed if t["r"] < 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in closed:
        equity += t["pnl"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    return {
        "orders": len(trades),
        "cancelled": dict(Counter(t["cancel_reason"] for t in trades
                                  if t["status"] == "cancelled")),
        "filled": sum(1 for t in trades if "fill_utc" in t),
        "closed": len(closed),
        "still_open": sum(1 for t in trades if t["status"] == "open" and "fill_utc" in t),
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "avg_r": round(sum(t["r"] for t in closed) / len(closed), 3) if closed else None,
        "total_r": round(sum(t["r"] for t in closed), 3),
        "total_pnl": round(sum(t["pnl"] for t in closed), 2),
        "max_drawdown": round(max_dd, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "exit_reasons": dict(Counter(t["exit_reason"] for t in closed)),
        "ambiguous_bars": sum(1 for t in trades if t["ambiguous"]),
        "roll_gap_trades": sum(1 for t in closed if t.get("roll_gap_inside")),
    }
