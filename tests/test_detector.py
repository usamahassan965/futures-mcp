"""Golden test: re-scanning the stored bars reproduces the stored analysis.

Runs only where the private detector is installed; it is skipped in public CI.
"""

from __future__ import annotations

import pytest

from futures_mcp.ranges import pipeline
from futures_mcp.ranges.detector import DetectorUnavailableError, load_detector

from .conftest import DETECTOR_DIR, WINDOWS, Window, needs_detector

pytestmark = pytest.mark.detector


@needs_detector
def test_reproduces_stored_analysis(window: Window) -> None:
    got = pipeline.analyze(window.bars, "GC", DETECTOR_DIR)
    assert got["verdict"] == WINDOWS[window.name] == window.analysis["verdict"]
    assert got["structures"] == window.analysis["structures"]


def test_missing_detector_is_a_clear_error(tmp_path) -> None:
    with pytest.raises(DetectorUnavailableError, match="FUTURES_MCP_DETECTOR_DIR"):
        load_detector(tmp_path)


def test_session_day_folds_trailing_sliver(window: Window) -> None:
    df = pipeline.to_frame(window.bars)
    counts = df.groupby("session_day").size()
    assert (counts >= pipeline.MIN_SESSION_BARS).all()
    assert len(df) == len(window.bars)  # nothing dropped: only the first day could be


def test_selection_skips_near_duplicate_anticipation() -> None:
    bars = [{"time_chart_utc5": f"2026-07-01 {h:02d}:00", "high": 110.0, "low": 100.0}
            for h in range(10)]
    ev = [{"side": "R", "from": "2026-07-01 00:00", "to": "2026-07-01 01:00", "extreme": 110.0}]
    comp = [{"S": 100.0, "R": 110.0, "events": ev * 4, "window_days": 2, "score": 4.0,
             "broke": None}]
    dup = {"S": 101.0, "R": 109.5, "events": ev * 3, "window_days": 2, "score": 3.0,
           "broke": None}
    other = dup | {"S": 50.0, "R": 60.0}
    got = pipeline.select_structures(bars, comp, [dup, other])
    assert [s["S"] for s in got] == [100.0, 50.0]
    assert pipeline.verdict_of(got) == "COMPLETED"
    assert pipeline.verdict_of(got[1:]) == "NOT_COMPLETED"
    assert pipeline.verdict_of([]) == "NO_RANGE"
