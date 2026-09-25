"""MCP server: tools, resources and a prompt over :class:`FuturesService`.

Runs over stdio. stdout carries the protocol, so everything else logs to stderr.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from importlib.resources import files
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .capture.browser import CaptureError
from .config import get_settings
from .data.bars import DataUnavailableError
from .data.timewindow import parse_date
from .models import BacktestReport, BarsResult, RangeReport, TradePlanReport
from .ranges.detector import DetectorUnavailableError
from .service import FuturesService
from .symbols import REGISTRY
from .trading.rules import RulesOutputError, RulesUnavailableError

logger = logging.getLogger("futures_mcp")

INSTRUCTIONS = """\
Futures market tools backed by TradingView.

- get_futures_bars: OHLCV bars with relative volume for a trading-day window.
- capture_chart: a TradingView screenshot framed on exactly that window.
- analyze_range: detects whether a range (support/resistance with numbered
  rejections) formed in the window, and whether it completed.
- get_range_chart: the screenshot with the detected range drawn on it.
- get_trade_plan: turns a detected range into a trade: direction, entry order,
  stop, targets and position size, from the operator's own rules.
- run_backtest: replays the detector and the rules bar by bar over past dates,
  with no hindsight, and reports every trade and the statistics per exit.

Windows count N trading days ending on end_date (default: the current trading
day). Chart times are UTC+5, the TradingView axis the charts use. Captures take
20-40 seconds. Range tools use H1 only."""

Symbol = Annotated[str, Field(description="Futures symbol, e.g. 'GC1!' (aliases: GC, GOLD)")]
Timeframe = Annotated[Literal["H1", "H4"], Field(description="Bar timeframe")]
RangeTimeframe = Annotated[Literal["H1"], Field(description="Range detection is calibrated on H1")]
EndDate = Annotated[
    str | None,
    Field(description="Last trading day of the window, YYYY-MM-DD. Omit for the current one."),
]

# MCP Apps: hosts that support the extension (e.g. Claude Desktop) render this view
# inline under the tool call; others ignore the metadata and still get the image.
CHART_VIEW_URI = "ui://futures-mcp/chart.html"
CHART_VIEW_MIME = "text/html;profile=mcp-app"
CHART_VIEW = {"ui": {"resourceUri": CHART_VIEW_URI}, "ui/resourceUri": CHART_VIEW_URI}

Mode = Annotated[
    Literal["anonymous", "session"] | None,
    Field(description="Chart to capture on: 'anonymous' (TradingView's public chart) or "
                      "'session' (the user's saved layout, e.g. when they say 'on my "
                      "layout'). Omit for the server default."),
]

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)

# Failures a caller can act on: their message is passed through as the tool error.
EXPECTED_ERRORS = (ValueError, DataUnavailableError, CaptureError, DetectorUnavailableError,
                   RulesUnavailableError, RulesOutputError)


class ChartResult(BaseModel):
    path: str = Field(description="Where the PNG was saved on the server machine")
    mode: str = Field(description="anonymous (default chart) or session (your saved layout)")
    window_utc: list[str]
    warnings: list[str]


class RangeChartResult(BaseModel):
    report: RangeReport
    path: str
    drawn: bool = Field(description="False when chart calibration failed and nothing was drawn")
    mode: str = Field(description="anonymous (default chart) or session (your saved layout)")
    warnings: list[str]


@contextmanager
def _tool_errors() -> Iterator[None]:
    try:
        yield
    except EXPECTED_ERRORS as exc:
        raise ToolError(str(exc)) from exc


def _progress(ctx: Context | None) -> Callable[[float, str], Any] | None:
    if ctx is None:
        return None

    async def report(p: float, message: str) -> None:
        await ctx.report_progress(round(p * 100), 100, message)

    return report


def _image_result(png: bytes, structured: BaseModel) -> CallToolResult:
    data = structured.model_dump(mode="json")
    return CallToolResult(
        content=[Image(data=png, format="png").to_image_content(),
                 TextContent(type="text", text=json.dumps(data, indent=2))],
        structured_content=data,
    )


def build_server(service: FuturesService | None = None) -> MCPServer:
    """Create the server. Tests pass a service with fake data and browser layers."""
    svc = service or FuturesService(get_settings())

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await svc.aclose()

    mcp = MCPServer(
        name="futures-mcp",
        title="Futures MCP",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
    )

    @mcp.tool(title="Get futures bars", annotations=READ_ONLY)
    async def get_futures_bars(
        symbol: Symbol = "GC1!",
        timeframe: Timeframe = "H1",
        days: Annotated[int, Field(ge=1, le=30, description="Trading days in the window")] = 4,
        end_date: EndDate = None,
        include_bars: Annotated[
            bool, Field(description="False returns only the per-day summaries")] = True,
    ) -> BarsResult:
        """OHLCV bars for a trading-day window, with Relative Volume (volume/SMA14),
        its zone, the session each bar belongs to, and per-day summaries."""
        with _tool_errors():
            return await svc.bars(symbol, timeframe, days, parse_date(end_date), include_bars)

    @mcp.tool(title="Capture TradingView chart", annotations=READ_ONLY, meta=CHART_VIEW)
    async def capture_chart(
        symbol: Symbol = "GC1!",
        timeframe: Timeframe = "H1",
        days: Annotated[int, Field(ge=1, le=30, description="Trading days in the window")] = 4,
        end_date: EndDate = None,
        mode: Mode = None,
        ctx: Context | None = None,
    ) -> Annotated[CallToolResult, ChartResult]:
        """Screenshot of the TradingView chart framed on exactly the trading-day window.
        Takes 20-40 seconds."""
        with _tool_errors():
            shot = await svc.capture(symbol, timeframe, days, parse_date(end_date),
                                     _progress(ctx), mode)
        return _image_result(shot.png, ChartResult(
            path=str(shot.path), mode=shot.mode,
            window_utc=[t.isoformat() for t in shot.window_utc], warnings=shot.warnings))

    @mcp.tool(title="Analyze range setup", annotations=READ_ONLY)
    async def analyze_range(
        symbol: Symbol = "GC1!",
        timeframe: RangeTimeframe = "H1",
        days: Annotated[int, Field(ge=2, le=10, description="Trading days to scan")] = 3,
        end_date: EndDate = None,
    ) -> RangeReport:
        """Detect a range in the window. Verdict: COMPLETED (a range met every
        rule), NOT_COMPLETED (a range is forming but not yet confirmed), or NO_RANGE.
        Each structure lists support, resistance, its numbered rejections and,
        if price has left it, when it broke."""
        with _tool_errors():
            return await svc.analyze(symbol, timeframe, days, parse_date(end_date))

    @mcp.tool(title="Range chart", annotations=READ_ONLY, meta=CHART_VIEW)
    async def get_range_chart(
        symbol: Symbol = "GC1!",
        timeframe: RangeTimeframe = "H1",
        days: Annotated[int, Field(ge=2, le=10, description="Trading days to scan")] = 3,
        end_date: EndDate = None,
        mode: Mode = None,
        ctx: Context | None = None,
    ) -> Annotated[CallToolResult, RangeChartResult]:
        """The TradingView chart for the window with the detected range drawn on it
        (support/resistance box, numbered rejections, caption), plus the report.
        Takes 30-50 seconds."""
        with _tool_errors():
            chart = await svc.range_chart(symbol, timeframe, days, parse_date(end_date),
                                          _progress(ctx), mode)
        return _image_result(chart.png, RangeChartResult(
            report=chart.report, path=str(chart.path), drawn=chart.drawn, mode=chart.mode,
            warnings=chart.warnings))

    @mcp.tool(title="Trade plan for a detected range", annotations=READ_ONLY)
    async def get_trade_plan(
        symbol: Symbol = "GC1!",
        timeframe: RangeTimeframe = "H1",
        days: Annotated[int, Field(ge=2, le=10, description="Trading days to scan")] = 3,
        end_date: EndDate = None,
    ) -> TradePlanReport:
        """Detect the range, then apply the configured trading rules to each structure:
        direction, entry order and level, stop, R-multiple targets and position size for
        the configured account, plus the exit instructions. Plans that the rules do not
        arm come back with signal=false and the reason. Every level is computed by the
        rules and re-checked by the server; do not invent or adjust prices or sizes.
        Analysis only, not advice."""
        with _tool_errors():
            return await svc.trade_plan(symbol, timeframe, days, parse_date(end_date))

    @mcp.tool(title="Backtest the trading rules", annotations=READ_ONLY)
    async def run_backtest(
        start_date: Annotated[str, Field(description="First day to trade, YYYY-MM-DD")],
        end_date: Annotated[str, Field(description="Last day to trade, YYYY-MM-DD")],
        symbol: Symbol = "GC1!",
        days: Annotated[int, Field(ge=2, le=10, description="Trading days the detector sees")] = 3,
        ctx: Context | None = None,
    ) -> BacktestReport:
        """Walk-forward backtest: at every H1 bar the detector sees only the last `days`
        trading days, the rules decide whether to place an order, and the order is
        replayed on the bars that follow. Reports one run per exit (the strategy's take
        profit, 1R, 2R, 3R) with every trade and its statistics. Takes about a minute
        per month of bars. Quote the numbers as returned; results exclude costs."""
        with _tool_errors():
            return await svc.backtest(symbol, parse_date(start_date), parse_date(end_date),
                                      days, _progress(ctx))

    @mcp.resource("futures://symbols", name="symbols", mime_type="application/json",
                  description="Symbols the tools accept")
    def symbols() -> str:
        return json.dumps([{"symbol": i.symbol, "tradingview": i.tv_symbol,
                            "description": i.description} for i in REGISTRY.values()], indent=2)

    @mcp.resource("futures://status", name="status", mime_type="application/json",
                  description="Capture mode, detector and OCR availability")
    def status() -> str:
        return json.dumps(svc.status(), indent=2)

    @mcp.resource(CHART_VIEW_URI, name="chart_view", mime_type=CHART_VIEW_MIME,
                  description="Inline chart view for MCP Apps hosts",
                  meta={"ui": {"csp": {"resourceDomains": ["https://unpkg.com"]}}})
    def chart_view() -> str:
        return (files("futures_mcp") / "ui" / "chart.html").read_text(encoding="utf-8")

    @mcp.prompt(title="Plan the trade on a range")
    def trade_plan(symbol: str = "GC1!", days: str = "3") -> str:
        """Check a symbol for a range setup and, if there is one, plan the trade."""
        return (
            f"Check {symbol} on H1 over the last {days} trading days with analyze_range, then "
            f"call get_trade_plan for the same window. For each structure report whether a "
            f"trade is armed and why, and when it is: the direction, the order type and its "
            f"level, the stop and what it is anchored to, the targets with their R multiples, "
            f"the position size and the risk in dollars and percent, then the order life and "
            f"the exit rules. Quote the numbers exactly as the tool returned them - never "
            f"recompute or round them - and repeat any notes the plan carries. Finish with "
            f"the reminder that this is analysis, not advice."
        )

    @mcp.prompt(title="Check for a range setup")
    def range_check(symbol: str = "GC1!", days: str = "3") -> str:
        """Check a symbol for a range setup and show it on the chart."""
        return (
            f"Check {symbol} on H1 for a range setup over the last {days} trading days. "
            f"Call analyze_range first. If the verdict is not NO_RANGE, call get_range_chart "
            f"to show it. Report the verdict, support and resistance, the rejections in "
            f"order with their times (UTC+5), and whether and when the range broke."
        )

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
