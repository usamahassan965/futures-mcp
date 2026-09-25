"""Toy trading rules, so the trade-plan tool can be run end to end from this repo.

These are NOT the rules behind the real results. The real entry, stop and sizing
rules are private and live outside the repository. This module only satisfies the
same interface (see ``futures_mcp/trading/rules.py``) with deliberately naive logic:

* the trade is taken on the last rejection in the structure: short if it happened
  at resistance, long if at support;
* the entry is that bar's close, with no confirmation and no resting order;
* the stop is that same bar's own extreme;
* targets are fixed R multiples and the size is whatever the risk budget buys;
* ``simulate`` walks forward until the stop or the target trades, nothing else.

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


def simulate(bars: list[dict[str, Any]], plan: dict[str, Any], structure: dict[str, Any],
             placed_idx: int, exit: str | float = "tp") -> dict[str, Any]:
    """Toy replay: filled at the entry on the next bar; out at the stop or the target."""
    short = plan["direction"] == "short"
    entry, stop, risk = float(plan["entry"]), float(plan["stop"]), float(plan["risk_points"])
    multiple = float(plan["targets"][0]["r_multiple"]) if exit == "tp" else float(exit)
    target = entry + (-1.0 if short else 1.0) * risk * multiple
    res: dict[str, Any] = {"status": "open", "cancel_reason": None, "cancel_idx": None,
                           "fill_idx": None, "fill_price": None, "exit_idx": None,
                           "exit_price": None, "exit_reason": None, "ambiguous": False,
                           "target": target}
    if placed_idx + 1 >= len(bars):
        return res
    res.update(fill_idx=placed_idx + 1, fill_price=entry)
    for j in range(placed_idx + 1, len(bars)):
        b = bars[j]
        hit_stop = b["high"] >= stop if short else b["low"] <= stop
        hit_tgt = b["low"] <= target if short else b["high"] >= target
        if hit_stop or hit_tgt:
            res.update(status="closed", exit_idx=j, ambiguous=hit_stop and hit_tgt,
                       exit_price=stop if hit_stop else target,
                       exit_reason="stop" if hit_stop else "target")
            break
    return res
