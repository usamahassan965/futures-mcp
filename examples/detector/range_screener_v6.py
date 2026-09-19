"""A toy range detector, so the range tools can be run end to end from this repo.

This is NOT the detector used for the real results. The real detection rules are
private and live outside the repository. This module only satisfies the same
interface (see ``futures_mcp/ranges/detector.py``) with deliberately naive logic:

* the box is the high/low of the first session day in the window;
* a rejection is a bar that tags the resistance (or support) edge, counted only
  when it alternates with the previous rejection's side;
* 4+ rejections = completed, 2-3 = anticipation (still forming);
* the box breaks on the first close beyond an edge after the last rejection.

Try it:  FUTURES_MCP_DETECTOR_DIR=examples/detector
"""

from __future__ import annotations

from typing import Any

import pandas as pd

TOUCH_FRACTION = 0.10  # an edge counts as tagged within 10% of the box height
COMPLETED_AT = 4
FORMING_AT = 2


def load(folder: str) -> pd.DataFrame:
    """Replaced at call time by the server with the window's bar frame."""
    raise RuntimeError("load() is supplied by futures_mcp at scan time")


def _t(ts: Any) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M")


def _box_hit(df: pd.DataFrame) -> dict[str, Any] | None:
    days = sorted(df["session_day"].unique())
    first = df[df["session_day"] == days[0]]
    rest = df[df["session_day"] != days[0]]
    s, r = float(first["low"].min()), float(first["high"].max())
    h = r - s
    if h <= 0 or rest.empty:
        return None
    tol = TOUCH_FRACTION * h
    events: list[dict[str, Any]] = []
    broke = None
    for row in rest.itertuples():
        if row.close > r + tol or row.close < s - tol:
            broke = _t(row.chart_time)
            break
        side = "R" if row.high >= r - tol else "S" if row.low <= s + tol else None
        if side and (not events or events[-1]["side"] != side):
            extreme = float(row.high if side == "R" else row.low)
            events.append({"side": side, "from": _t(row.chart_time),
                           "to": _t(row.chart_time), "extreme": extreme})
    if len(events) < FORMING_AT:
        return None
    return {
        "S": round(s, 2), "R": round(r, 2), "H": round(h, 2), "H_in_med": None,
        "n_rejections": len(events), "window_days": len(days), "sloping": False,
        "score": float(len(events)), "first_touch": events[0]["from"],
        "completion": events[-1]["to"] if len(events) >= COMPLETED_AT else None,
        "last_event": events[-1]["to"], "broke": broke, "events": events,
    }


def scan(folder: str, symbol: str, end_from: Any, end_to: Any,
         scan_start: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    df = load(folder)
    df = df[(df["session_day"] >= scan_start) & (df["session_day"] <= end_to)]
    hit = _box_hit(df) if not df.empty else None
    if hit is None:
        return [], []
    return ([hit], []) if hit["n_rejections"] >= COMPLETED_AT else ([], [hit])


def dedupe(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for h in hits:
        key = (h["S"], h["R"])
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def demote_by_wider(completed: list[dict[str, Any]], anticipation: list[dict[str, Any]]
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return completed, []
