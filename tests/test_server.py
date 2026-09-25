"""End-to-end MCP protocol tests: an in-memory client against the real server.

Only the network edges are faked: the bar feed serves a recorded window and the
browser capture returns the recorded screenshot. Everything between (tool
schemas, validation, error mapping, detection, calibration, drawing, image
encoding) runs for real.
"""

from __future__ import annotations

import base64
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from mcp import Client
from mcp.types import ImageContent, TextContent
from pydantic import SecretStr

from futures_mcp import service as service_mod
from futures_mcp.capture.tradingview import Capture
from futures_mcp.config import Settings
from futures_mcp.data.bars import RELVOL_NOTE, TIMEZONE_NOTE
from futures_mcp.server import build_server
from futures_mcp.service import FuturesService
from futures_mcp.symbols import Instrument

from .conftest import (
    DETECTOR_DIR,
    EXAMPLE_DETECTOR,
    EXAMPLE_RULES,
    RULES_DIR,
    WINDOWS,
    Window,
    needs_detector,
    needs_rules,
    needs_tesseract,
)
from .test_backtest import history_of

TOOLS = {"get_futures_bars", "capture_chart", "analyze_range", "get_range_chart",
         "get_trade_plan", "run_backtest"}


class FakeFeed:
    def __init__(self, window: Window):
        self.w = window
        self.calls = 0

    async def awindow(self, inst: Instrument, tf: str, target: date, days: int) -> dict[str, Any]:
        self.calls += 1
        bars = self.w.bars
        span = self.w.payload["window"]["utc_span"]
        return {
            "symbol": inst.symbol, "timeframe": tf, "target_date": str(target),
            "window_trading_days": days, "window_utc": span, "window_complete": True,
            "timezone_note": TIMEZONE_NOTE, "relative_volume_indicator": RELVOL_NOTE,
            "n_bars": len(bars), "window_max_volume_bar": max(bars, key=lambda b: b["volume"]),
            "days": [], "bars": bars,
        }

    def history(self, inst: Instrument, tf: str) -> pd.DataFrame:
        return history_of(self.w)


class FakeBrowser:
    mode = "anonymous"

    async def close(self) -> None:
        pass


@pytest.fixture
def ranged() -> Window:
    return Window("gc_03_07_to_07_07")


def make_service(tmp_path: Path, window: Window, detector_dir: Path = DETECTOR_DIR,
                 monkeypatch: pytest.MonkeyPatch | None = None,
                 rules_dir: Path = RULES_DIR) -> FuturesService:
    settings = Settings(FUTURES_MCP_DATA_DIR=tmp_path, FUTURES_MCP_DETECTOR_DIR=detector_dir,
                        FUTURES_MCP_RULES_DIR=rules_dir, tradingview_session_id=SecretStr(""),
                        FUTURES_MCP_DEFAULT_MODE=None)
    svc = FuturesService(settings, feed=FakeFeed(window), browser=FakeBrowser())  # type: ignore[arg-type]
    if monkeypatch is not None:
        async def fake_capture(browser: Any, inst: Instrument, tf: str, target: date, days: int,
                               out_dir: Path, progress: Any = None) -> Capture:
            if progress:
                await progress(0.5, "fake capture")
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{tf}_{target}_{days}d_anonymous.png"
            shutil.copy(window.png, path)
            return Capture(path, path.read_bytes(), "anonymous",
                           (datetime(2026, 7, 3), datetime(2026, 7, 8)), [])

        monkeypatch.setattr(service_mod, "capture_window", fake_capture)
    return svc


async def test_lists_tools_with_schemas(tmp_path: Path, ranged: Window) -> None:
    async with Client(build_server(make_service(tmp_path, ranged))) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == TOOLS
    for t in tools.values():
        assert t.annotations and t.annotations.read_only_hint
        assert t.output_schema, f"{t.name} has no output schema"
        assert t.description
    tf = tools["analyze_range"].input_schema["properties"]["timeframe"]
    assert tf.get("const", tf.get("enum", [None])[0]) == "H1"
    assert tools["get_futures_bars"].input_schema["properties"]["days"]["maximum"] == 30


async def test_get_futures_bars(tmp_path: Path, ranged: Window) -> None:
    async with Client(build_server(make_service(tmp_path, ranged))) as client:
        full = await client.call_tool("get_futures_bars", {"days": 3, "end_date": "2026-07-07"})
        brief = await client.call_tool("get_futures_bars", {"days": 3, "end_date": "2026-07-07",
                                                            "include_bars": False})
    assert not full.is_error
    assert full.structured_content is not None and brief.structured_content is not None
    assert full.structured_content["n_bars"] == len(ranged.bars)
    assert full.structured_content["bars"][0] == ranged.bars[0]
    assert brief.structured_content["bars"] is None


@pytest.mark.parametrize(("tool", "args", "message"), [
    ("get_futures_bars", {"symbol": "ES1!"}, "Supported: GC1!"),
    ("get_futures_bars", {"end_date": "07/07/2026"}, "expected YYYY-MM-DD"),
    ("get_futures_bars", {"days": 0}, "days"),
    ("analyze_range", {"timeframe": "H4"}, "timeframe"),
    ("analyze_range", {"days": 1}, "days"),
])
async def test_bad_arguments_are_tool_errors(tmp_path: Path, ranged: Window, tool: str,
                                             args: dict[str, Any], message: str) -> None:
    async with Client(build_server(make_service(tmp_path, ranged))) as client:
        result = await client.call_tool(tool, args)
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    assert message in result.content[0].text


async def test_missing_detector_fails_fast(tmp_path: Path, ranged: Window,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    svc = make_service(tmp_path, ranged, detector_dir=tmp_path / "nope", monkeypatch=monkeypatch)
    async with Client(build_server(svc)) as client:
        result = await client.call_tool("get_range_chart", {"end_date": "2026-07-07"})
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    assert "FUTURES_MCP_DETECTOR_DIR" in result.content[0].text
    assert svc.feed.calls == 0  # type: ignore[attr-defined]


async def test_capture_chart_returns_image(tmp_path: Path, ranged: Window,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    svc = make_service(tmp_path, ranged, monkeypatch=monkeypatch)
    progress: list[tuple[float, str | None]] = []

    async def on_progress(p: float, total: float | None, message: str | None) -> None:
        progress.append((p, message))

    async with Client(build_server(svc)) as client:
        result = await client.call_tool("capture_chart",
                                        {"end_date": "2026-07-07", "mode": "anonymous"},
                                        progress_callback=on_progress)
    assert not result.is_error
    image = result.content[0]
    assert isinstance(image, ImageContent) and image.mime_type == "image/png"
    assert base64.b64decode(image.data) == ranged.png.read_bytes()
    assert result.structured_content and result.structured_content["mode"] == "anonymous"
    assert progress == [(50, "fake capture")]


async def test_session_mode_without_cookie_is_a_tool_error(tmp_path: Path, ranged: Window,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    svc = make_service(tmp_path, ranged, monkeypatch=monkeypatch)
    async with Client(build_server(svc)) as client:
        for tool in ("capture_chart", "get_range_chart"):
            result = await client.call_tool(tool, {"end_date": "2026-07-07", "mode": "session"})
            assert result.is_error
            assert isinstance(result.content[0], TextContent)
            assert "TRADINGVIEW_SESSION_ID" in result.content[0].text
    assert svc.feed.calls == 0  # type: ignore[attr-defined]


async def test_missing_rules_fail_fast(tmp_path: Path, ranged: Window) -> None:
    svc = make_service(tmp_path, ranged, rules_dir=tmp_path / "nope")
    async with Client(build_server(svc)) as client:
        result = await client.call_tool("get_trade_plan", {"end_date": "2026-07-07"})
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    assert "FUTURES_MCP_RULES_DIR" in result.content[0].text
    assert svc.feed.calls == 0  # type: ignore[attr-defined]


async def test_run_backtest_on_the_toy_rules(tmp_path: Path, ranged: Window) -> None:
    svc = make_service(tmp_path, ranged, detector_dir=EXAMPLE_DETECTOR, rules_dir=EXAMPLE_RULES)
    async with Client(build_server(svc)) as client:
        result = await client.call_tool("run_backtest", {
            "start_date": "2026-07-06", "end_date": "2026-07-07", "days": 2})
    assert not result.is_error, result.content
    out = result.structured_content
    assert out is not None
    assert out["rules_version"] == "example-toy-v1"
    assert [r["exit"] for r in out["runs"]] == ["tp", "1R", "2R", "3R"]
    assert out["steps"] > 0 and out["signals"] > 0
    for run in out["runs"]:
        assert run["stats"]["orders"] == len(run["trades"])
        assert len(run["trades"]) + sum(run["skipped"].values()) == out["signals"]


async def test_run_backtest_needs_enough_history(tmp_path: Path, ranged: Window) -> None:
    svc = make_service(tmp_path, ranged, detector_dir=EXAMPLE_DETECTOR, rules_dir=EXAMPLE_RULES)
    async with Client(build_server(svc)) as client:
        result = await client.call_tool("run_backtest", {
            "start_date": "2026-07-03", "end_date": "2026-07-07", "days": 3})
        backwards = await client.call_tool("run_backtest", {
            "start_date": "2026-07-07", "end_date": "2026-07-03"})
    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    assert "only reaches back" in result.content[0].text
    assert backwards.is_error


async def test_trade_plan_runs_on_the_toy_rules(tmp_path: Path, ranged: Window) -> None:
    svc = make_service(tmp_path, ranged, detector_dir=EXAMPLE_DETECTOR, rules_dir=EXAMPLE_RULES)
    async with Client(build_server(svc)) as client:
        result = await client.call_tool("get_trade_plan", {"end_date": "2026-07-07"})
    assert not result.is_error, result.content
    out = result.structured_content
    assert out is not None
    assert out["rules_version"] == "example-toy-v1"
    assert out["account"] == {"equity": 100000.0, "risk_pct": 1.0, "point_value": 100.0,
                              "min_contracts": 1, "targets": [1.0, 2.0, 3.0]}
    assert len(out["plans"]) == len(out["range_report"]["structures"])
    for plan, structure in zip(out["plans"], out["range_report"]["structures"], strict=True):
        assert plan["support"] == structure["support"]
        assert plan["reason"]
        if plan["signal"]:
            assert abs(plan["entry"] - plan["stop"]) == pytest.approx(plan["risk_points"])
            assert [t["r_multiple"] for t in plan["targets"]] == [1.0, 2.0, 3.0]


@needs_detector
@needs_rules
@pytest.mark.detector
async def test_trade_plan_on_the_private_rules(tmp_path: Path, ranged: Window) -> None:
    """The July 7 window carries one armed short; the other structure broke first."""
    async with Client(build_server(make_service(tmp_path, ranged))) as client:
        result = await client.call_tool("get_trade_plan", {"end_date": "2026-07-07"})
    assert not result.is_error, result.content
    out = result.structured_content
    assert out is not None and len(out["plans"]) == 2
    dead, armed = out["plans"]
    assert not dead["signal"] and "broke" in dead["reason"]
    assert armed["signal"] and armed["direction"] == "short"
    assert armed["order"] == "sell_stop"
    assert armed["contracts"] >= 1
    assert armed["risk_pct_actual"] > 0
    assert [t["r_multiple"] for t in armed["targets"]] == [1.0, 2.0, 3.0]


@needs_detector
@pytest.mark.detector
async def test_analyze_range_verdicts(tmp_path: Path, window: Window) -> None:
    async with Client(build_server(make_service(tmp_path, window))) as client:
        result = await client.call_tool("analyze_range", {"end_date": "2026-07-07"})
    assert not result.is_error, result.content
    report = result.structured_content
    assert report is not None
    assert report["verdict"] == WINDOWS[window.name]
    assert len(report["structures"]) == len(window.analysis["structures"])
    for got, want in zip(report["structures"], window.analysis["structures"], strict=True):
        assert (got["support"], got["resistance"]) == (want["S"], want["R"])
        assert got["caption"] == want["caption"]


@needs_detector
@needs_tesseract
@pytest.mark.detector
async def test_get_range_chart_draws_the_range(tmp_path: Path, ranged: Window,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    svc = make_service(tmp_path, ranged, monkeypatch=monkeypatch)
    async with Client(build_server(svc)) as client:
        result = await client.call_tool("get_range_chart", {"end_date": "2026-07-07"})
    assert not result.is_error, result.content
    out = result.structured_content
    assert out is not None
    assert out["drawn"] and not out["warnings"]
    assert out["report"]["verdict"] == "COMPLETED"
    assert Path(out["path"]).name.endswith("_marked.png")
    image = result.content[0]
    assert isinstance(image, ImageContent)
    assert base64.b64decode(image.data) == Path(out["path"]).read_bytes()


async def test_resources_and_prompt(tmp_path: Path, ranged: Window) -> None:
    async with Client(build_server(make_service(tmp_path, ranged))) as client:
        symbols = await client.read_resource("futures://symbols")
        status = await client.read_resource("futures://status")
        prompt = await client.get_prompt("range_check", {"symbol": "GC1!"})
    assert '"COMEX:GC1!"' in symbols.contents[0].text  # type: ignore[union-attr]
    assert '"capture_mode": "anonymous"' in status.contents[0].text  # type: ignore[union-attr]
    assert '"session_mode_available": false' in status.contents[0].text  # type: ignore[union-attr]
    text = prompt.messages[0].content
    assert isinstance(text, TextContent) and "analyze_range" in text.text


async def test_chart_tools_link_the_mcp_apps_view(tmp_path: Path, ranged: Window) -> None:
    async with Client(build_server(make_service(tmp_path, ranged))) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        uri = (tools["get_range_chart"].meta or {})["ui"]["resourceUri"]
        view = await client.read_resource(uri)
    assert uri.startswith("ui://")
    assert (tools["capture_chart"].meta or {})["ui"]["resourceUri"] == uri
    assert "ui" not in (tools["analyze_range"].meta or {})
    page = view.contents[0]
    assert page.mime_type == "text/html;profile=mcp-app"
    assert "ontoolresult" in page.text  # type: ignore[union-attr]
