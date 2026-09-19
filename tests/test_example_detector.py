"""The public toy detector runs through the real pipeline (so CI exercises the range tools)."""

import json
from pathlib import Path

import pytest

from futures_mcp.ranges import pipeline

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO / "examples" / "detector"
FIXTURES = REPO / "tests" / "fixtures"


@pytest.mark.parametrize("window", sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()))
def test_toy_detector_meets_contract(window: str) -> None:
    bars = json.loads((FIXTURES / window / "H1_bars.json").read_text())["H1"]["bars"]
    report = pipeline.analyze(bars, "GC", EXAMPLE_DIR)
    assert report["verdict"] in {"COMPLETED", "NOT_COMPLETED", "NO_RANGE"}
    for s in report["structures"]:
        assert s["S"] < s["R"]
        assert [r["n"] for r in s["rejections"]] == list(range(1, len(s["rejections"]) + 1))
