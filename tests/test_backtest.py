"""The walk-forward backtest over the public toy detector and toy rules."""

from __future__ import annotations

from datetime import date
from itertools import pairwise
from typing import Any

import pandas as pd
import pytest

from futures_mcp.data.bars import prepare
from futures_mcp.symbols import resolve
from futures_mcp.trading import backtest
from futures_mcp.trading.rules import RulesUnavailableError, load_rules

from .conftest import EXAMPLE_DETECTOR, EXAMPLE_RULES, Window

ACCOUNT: dict[str, Any] = {"equity": 100_000.0, "risk_pct": 1.0, "point_value": 100.0,
                           "min_contracts": 1, "targets": [1.0, 2.0, 3.0]}


def history_of(window: Window) -> pd.DataFrame:
    """A prepared H1 frame from a recorded window; open = previous close."""
    df = pd.DataFrame(
        [{"high": b["high"], "low": b["low"], "close": b["close"], "volume": b["volume"]}
         for b in window.bars],
        index=pd.DatetimeIndex([pd.Timestamp(b["time_utc"]) for b in window.bars]))
    df.insert(0, "open", df["close"].shift(1).fillna(df["close"]))
    return prepare(df, source_tz="UTC")


@pytest.fixture(scope="module")
def detected() -> dict[str, Any]:
    history = history_of(Window("gc_03_07_to_07_07"))
    return backtest.detect_signals(history, resolve("GC1!"), date(2026, 7, 6), date(2026, 7, 7),
                                   EXAMPLE_DETECTOR, load_rules(EXAMPLE_RULES), ACCOUNT, days=2)


def test_detector_runs_at_every_bar_of_the_period(detected: dict[str, Any]) -> None:
    in_period = [b for b in detected["bars"] if b["time_utc"] >= "2026-07-06"]
    assert detected["steps"] == len(in_period)
    assert all(b["open"] is not None for b in detected["bars"])


def test_signals_are_placed_when_seen_never_before(detected: dict[str, Any]) -> None:
    assert detected["signals"], "the toy rules arm on any rejection"
    bars = detected["bars"]
    for sig in detected["signals"]:
        assert bars[sig["placed_idx"]]["time_utc"] == sig["detected_utc"]
        assert sig["confirm_utc"] <= sig["detected_utc"]
        assert sig["plan"]["signal"] is True


@pytest.mark.parametrize("exit", ["tp", 2.0])
def test_run_accounts_for_every_signal(detected: dict[str, Any], exit: str | float) -> None:
    out = backtest.run(detected, load_rules(EXAMPLE_RULES), exit, 100.0, days=2)
    assert len(out["trades"]) + sum(out["skipped"].values()) == len(detected["signals"])
    multiple = 1.0 if exit == "tp" else exit
    for t in out["trades"]:
        if t["r"] is not None:  # the toy fills at the entry and exits exactly at a level
            assert t["r"] == pytest.approx(-1.0 if t["exit_reason"] == "stop" else multiple)
            assert t["pnl"] == pytest.approx(t["r"] * t["risk_points"] * t["contracts"] * 100.0)
    # One trade at a time: no two trades overlap.
    spans = [(t["detected_utc"], t.get("exit_utc") or "9999") for t in out["trades"]]
    for (_, end), (start, _) in pairwise(spans):
        assert start > end
    stats = out["stats"]
    assert stats["orders"] == len(out["trades"])
    assert stats["closed"] == stats["wins"] + stats["losses"]


def test_stats_on_known_trades() -> None:
    trades = [{"r": 2.0, "pnl": 200.0, "status": "closed", "cancel_reason": None,
               "fill_utc": "a", "exit_reason": "target", "ambiguous": False},
              {"r": -1.0, "pnl": -100.0, "status": "closed", "cancel_reason": None,
               "fill_utc": "b", "exit_reason": "stop", "ambiguous": True},
              {"r": -1.0, "pnl": -100.0, "status": "closed", "cancel_reason": None,
               "fill_utc": "c", "exit_reason": "stop", "ambiguous": False},
              {"r": None, "pnl": None, "status": "cancelled", "cancel_reason": "day ended",
               "ambiguous": False}]
    s = backtest.stats(trades)
    assert (s["orders"], s["filled"], s["closed"], s["wins"], s["losses"]) == (4, 3, 3, 1, 2)
    assert s["cancelled"] == {"day ended": 1}
    assert s["total_r"] == 0.0 and s["total_pnl"] == 0.0
    assert s["max_drawdown"] == 200.0 and s["profit_factor"] == 1.0
    assert s["exit_reasons"] == {"target": 1, "stop": 2} and s["ambiguous_bars"] == 1


def test_roll_gaps_flag_only_large_opening_jumps() -> None:
    bars = [{"time_utc": f"t{i}", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
            for i in range(10)]
    for b in bars[5:]:  # the series steps up once and stays there
        b.update(open=120.0, high=121.0, low=119.0, close=120.0)
    assert backtest.roll_gaps(bars) == [{"time_utc": "t5", "gap": 20.0}]


def test_rules_without_simulate_cannot_backtest(tmp_path: Any) -> None:
    (tmp_path / "entry_rules.py").write_text(
        "RULES_VERSION = 'x'\ndef plan(bars, structure, account):\n    return {}\n",
        encoding="utf-8")
    with pytest.raises(RulesUnavailableError, match="simulate"):
        backtest.detect_signals(pd.DataFrame(), resolve("GC1!"), date(2026, 7, 6),
                                date(2026, 7, 7), EXAMPLE_DETECTOR, load_rules(tmp_path), ACCOUNT)
