from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]
DETECTOR_DIR = Path(os.environ.get("FUTURES_MCP_DETECTOR_DIR", REPO / "private"))

WINDOWS = {
    # window folder         -> expected verdict
    "gc_01_07_to_03_07": "NO_RANGE",
    "gc_03_07_to_07_07": "COMPLETED",
    "gc_21_07_to_23_07": "NOT_COMPLETED",
}


class Window:
    def __init__(self, name: str):
        self.name = name
        self.dir = FIXTURES / name
        self.payload: dict[str, Any] = json.loads((self.dir / "H1_bars.json").read_text("utf-8"))
        self.bars: list[dict[str, Any]] = self.payload["H1"]["bars"]
        self.analysis: dict[str, Any] = json.loads(
            (self.dir / "range_analysis.json").read_text("utf-8"))
        self.png = next(p for p in self.dir.glob("H1_*.png") if "marked" not in p.name)
        self.marked = next(self.dir.glob("H1_*_marked.png"))


@pytest.fixture(params=sorted(WINDOWS))
def window(request: pytest.FixtureRequest) -> Window:
    return Window(request.param)


def tesseract_available() -> bool:
    if shutil.which("tesseract"):
        return True
    default = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if default.exists():
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = str(default)
        return True
    return False


needs_tesseract = pytest.mark.skipif(not tesseract_available(), reason="tesseract not installed")
needs_detector = pytest.mark.skipif(
    not (DETECTOR_DIR / "range_screener_v6.py").exists(),
    reason="private range detector not present (it is intentionally not in the repo)",
)
