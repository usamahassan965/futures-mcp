"""Golden test: calibrating and drawing the stored structures onto the stored raw
screenshot reproduces the stored marked chart. Needs no private code."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFont

from futures_mcp.ranges import overlay

from .conftest import Window, needs_tesseract


def _has_arial() -> bool:
    try:
        ImageFont.truetype("arial.ttf", 12)
        return True
    except OSError:
        return False


@needs_tesseract
def test_calibration_matches_stored(window: Window) -> None:
    cal, err = overlay.calibrate(Image.open(window.png), window.bars)
    assert err is None
    assert cal["trustworthy"]
    want = window.analysis["calibration"]
    assert cal["px_per_unit"] == pytest.approx(want["px_per_unit"], rel=1e-6)
    assert cal["rms_price_px"] < 1.0 and cal["rms_x_px"] < 1.5


@needs_tesseract
def test_marked_chart_is_reproduced(window: Window, tmp_path: Path) -> None:
    cal, err = overlay.calibrate(Image.open(window.png), window.bars)
    assert err is None
    out = tmp_path / "marked.png"
    overlay.render(window.png, out, window.bars, cal, window.analysis["structures"])

    got = np.asarray(Image.open(out).convert("RGB"))
    raw = np.asarray(Image.open(window.png).convert("RGB"))
    assert (got != raw).any(), "nothing was drawn"
    if not _has_arial():
        pytest.skip("Arial not installed: glyphs differ, pixel equality not meaningful")
    want = np.asarray(Image.open(window.marked).convert("RGB"))
    diff = int((got != want).any(axis=2).sum())
    assert diff == 0, f"{diff} pixels differ from the stored marked chart"


@needs_tesseract
def test_calibration_refuses_a_blank_image() -> None:
    blank = Image.new("RGB", (1920, 1080), "white")
    cal, err = overlay.calibrate(blank, [{"time_chart_utc5": "2026-07-01 00:00",
                                          "high": 1.0, "low": 0.0}])
    assert cal is None and "price labels" in err



@needs_tesseract
def test_anonymous_chart_needs_its_own_pane_extent() -> None:
    """The default (logged-out) chart has no indicator pane, so price runs lower."""
    w = Window("gc_03_07_to_07_07")
    img = Image.open(w.dir / "anonymous_H1_2026-07-07.png")
    cal, err = overlay.calibrate(img, w.bars, overlay.PANE_Y["anonymous"])
    assert err is None and cal is not None
    assert cal["trustworthy"], cal
    session_cal, _ = overlay.calibrate(img, w.bars, overlay.PANE_Y["session"])
    assert session_cal is not None and not session_cal["span_ok"]
