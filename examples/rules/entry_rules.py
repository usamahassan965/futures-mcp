"""Toy trading rules, so the trade-plan tool can be run end to end from this repo.

These are NOT the rules behind the real results. The real entry, stop and sizing
rules are private and live outside the repository. This module only satisfies the
same interface (see ``futures_mcp/trading/rules.py``) with deliberately naive logic:

* the trade is taken on the last rejection in the structure: short if it happened
  at resistance, long if at support;
* the entry is that bar's close, with no confirmation and no resting order;
* the stop is that same bar's own extreme;
* targets are fixed R multiples and the size is whatever the risk budget buys.

Try it:  FUTURES_MCP_RULES_DIR=examples/rules
"""

from __future__ import annotations

from typing import Any

RULES_VERSION = "example-toy-v1"


def plan(bars: list[dict[str, Any]], structure: dict[str, Any],
         account: dict[str, Any]) -> dict[str, Any]:
    rejections = structure.get("rejections") or []
    if not rejections or not bars:
        return {"signal": False, "reason": "No rejection to trade in this window."}

    last = rejections[-1]
    idx = int(last["idx"])
    if idx >= len(bars):
        return {"signal": False, "reason": "The rejection is outside the bars supplied."}

    direction = "short" if last["side"] == "R" else "long"
    entry = float(bars[idx]["close"])
    stop = float(bars[idx]["high"] if direction == "short" else bars[idx]["low"])
    # A bar that closed on its extreme leaves no room; say so instead of inventing a trade.
    if (direction == "short" and stop <= entry) or (direction == "long" and stop >= entry):
        return {"signal": False,
                "reason": f"The rejection bar's own extreme ({stop}) is not a usable stop for a "
                          f"{direction} entered at {entry}."}

    risk_points = round(abs(stop - entry), 5)
    sign = -1.0 if direction == "short" else 1.0
    targets = [{"r_multiple": float(m), "price": round(entry + sign * risk_points * float(m), 5)}
               for m in (account.get("targets") or (1.0, 2.0, 3.0))]

    point_value = float(account.get("point_value", 100.0))
    equity = float(account.get("equity", 100_000.0))
    risk_pct = float(account.get("risk_pct", 1.0))
    per_contract = risk_points * point_value
    contracts = max(int(account.get("min_contracts", 1)),
                    int(equity * risk_pct / 100.0 // per_contract))
    risk_dollars = round(contracts * per_contract, 2)

    return {
        "signal": True,
        "reason": f"Toy rule: rejection {last['n']} at {last['price']} ({last['time']}).",
        "direction": direction,
        "order": "market",
        "entry": round(entry, 5),
        "entry_basis": "the rejection bar's close",
        "stop": round(stop, 5),
        "stop_basis": "the rejection bar's own extreme",
        "risk_points": risk_points,
        "risk_per_contract": round(per_contract, 2),
        "contracts": contracts,
        "risk_dollars": risk_dollars,
        "risk_pct_actual": round(risk_dollars / equity * 100, 3),
        "targets": targets,
        "signal_rejection": int(last["n"]),
        "confirm_time": str(last["time"]),
        "order_life": "Immediate: the toy rules do not rest an order.",
        "exits": ["Stop or target, whichever price reaches first."],
        "triggered": True,
        "triggered_time": str(last["time"]),
        "notes": ["Toy rules: for wiring and tests only, not a strategy."],
    }
