"""Bars -> detected structures -> verdict -> (optionally) a marked chart.

Faithful port of the Trading_bot sweep recipe that produced the GC/6B July
reference charts, so the same bars give the same structures and the same
picture:

1. rebuild the detector frame, folding sub-6-bar calendar days (the chart's
   UTC+5 axis leaves a sliver of each session after midnight) into the
   previous session;
2. scan, dedupe, demote completions shadowed by a wider structure;
3. map each event to the bar carrying its extreme, number the rejections;
4. keep every completed structure plus the first not-completed one that is
   not a near-duplicate of a box already kept;
5. verdict: COMPLETED if any completed, NOT_COMPLETED if only an unfinished
   one, else NO_RANGE.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .detector import load_detector, run_scan

HIT_FIELDS = ("S", "R", "H", "H_in_med", "n_rejections", "window_days", "sloping",
              "score", "first_touch", "completion", "last_event", "broke")
#: Detector internals a trading rule needs to measure things the detector's way.
CONTEXT_FIELDS = ("S_seed", "R_seed", "end_day")
MIN_SESSION_BARS = 6


def to_frame(bars: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame({
        "chart_time": pd.to_datetime([b["time_chart_utc5"] for b in bars]),
        "high": [b["high"] for b in bars],
        "low": [b["low"] for b in bars],
        "close": [b["close"] for b in bars],
    })
    df["open"] = df["close"].shift(1).fillna(df["close"])
    df["day"] = df["chart_time"].dt.date
    counts = df.groupby("day").size()
    big = {d for d, n in counts.items() if n >= MIN_SESSION_BARS}
    remap: dict[Any, Any] = {}
    prev = None
    for d in sorted(counts.index):
        if d in big:
            prev = d
        remap[d] = prev
    df["session_day"] = df["day"].map(remap)
    return df[df["session_day"].notna()]


def fmt_hit(h: dict[str, Any]) -> dict[str, Any]:
    return {k: h[k] for k in HIT_FIELDS} | {k: h.get(k) for k in CONTEXT_FIELDS} | {
        "events": [{"side": e["side"], "from": e["from"], "to": e["to"],
                    "extreme": e["extreme"]} for e in h["events"]]}


def idx_of(bars: list[dict[str, Any]], t_from: str, t_to: str, extreme: float,
           side: str) -> int | None:
    best: float | None = None
    bi: int | None = None
    for i, b in enumerate(bars):
        t = b["time_chart_utc5"]
        if not (t_from <= t <= t_to):
            continue
        v = b["high"] if side == "R" else b["low"]
        d = abs(v - extreme)
        if best is None or d < best:
            best, bi = d, i
    if bi is None:
        for i, b in enumerate(bars):
            v = b["high"] if side == "R" else b["low"]
            if abs(v - extreme) < 1e-9:
                return i
    return bi


def build(bars: list[dict[str, Any]], hit: dict[str, Any], verdict: str) -> dict[str, Any] | None:
    rejs = []
    for n, e in enumerate(hit["events"], 1):
        i = idx_of(bars, e["from"], e["to"], e["extreme"], e["side"])
        if i is None:
            return None
        rejs.append({"n": n, "side": e["side"], "idx": i, "price": e["extreme"],
                     "time": bars[i]["time_chart_utc5"]})
    n = len(rejs)
    cap = (f"COMPLETED range — {n} rejections  (S {hit['S']} / R {hit['R']})"
           if verdict == "COMPLETED"
           else f"NOT completed — {n} rejections  (S {hit['S']} / R {hit['R']})")
    if hit.get("broke"):
        cap += f"  · broke {hit['broke']}"
    return {"S": hit["S"], "R": hit["R"], "verdict": verdict,
            "rejections": rejs, "discards": [], "caption": cap,
            "broke": hit.get("broke"), "window_days": hit["window_days"],
            "score": hit["score"]}


def select_structures(bars: list[dict[str, Any]], completed: list[dict[str, Any]],
                      anticipation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s, _ in _select(bars, completed, anticipation)]


def _select(bars: list[dict[str, Any]], completed: list[dict[str, Any]],
            anticipation: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Structures kept, each paired with the hit it was built from."""
    kept: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for h in completed:
        s = build(bars, h, "COMPLETED")
        if s:
            kept.append((s, h))
    for h in anticipation:
        s = build(bars, h, "NOT_COMPLETED")
        if not s:
            continue
        if any(abs(s["S"] - t["S"]) < 0.25 * (t["R"] - t["S"])
               and abs(s["R"] - t["R"]) < 0.25 * (t["R"] - t["S"])
               for t, _ in kept):
            continue
        kept.append((s, h))
        break
    return kept


def detector_context(frame: pd.DataFrame, hit: dict[str, Any]) -> dict[str, Any]:
    """What the detector measured a structure against: its seed lines, the median
    bar range of the sub-window it scanned, and each event's first/last touch.

    Kept out of the structure itself so the reported structures stay exactly the
    Trading_bot ones; trading rules read it to measure price the way the detector did."""
    med = None
    if hit.get("end_day"):
        days = sorted(frame["session_day"].unique())
        end = pd.Timestamp(hit["end_day"]).date()
        if end in days:
            i = days.index(end)
            win = frame[frame["session_day"].isin(days[max(0, i - hit["window_days"] + 1):i + 1])]
            med = float((win["high"] - win["low"]).median())
    return {k: hit.get(k) for k in CONTEXT_FIELDS} | {
        "window_days": hit["window_days"], "med": med,
        "events": [{"from": e["from"], "to": e["to"]} for e in hit["events"]]}


def verdict_of(structures: list[dict[str, Any]]) -> str:
    if any(s["verdict"] == "COMPLETED" for s in structures):
        return "COMPLETED"
    return "NOT_COMPLETED" if structures else "NO_RANGE"


def analyze(bars: list[dict[str, Any]], code: str, detector_dir: Path) -> dict[str, Any]:
    """Run the detector over a window of H1 bar records."""
    mod = load_detector(detector_dir)
    frame = to_frame(bars)
    comp, ant, dem = run_scan(mod, frame, code)
    comp_f, ant_f, dem_f = ([fmt_hit(x) for x in lst] for lst in (comp, ant, dem))
    kept = _select(bars, comp_f, ant_f)
    structures = [s for s, _ in kept]
    return {
        "verdict": verdict_of(structures),
        "structures": structures,
        "context": [detector_context(frame, h) for _, h in kept],
        "raw": {"completed": comp_f, "anticipation": ant_f, "demoted": dem_f},
    }
