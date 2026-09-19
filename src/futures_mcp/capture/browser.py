"""One shared headless Chromium page, started lazily and reused across calls.

A cold TradingView load costs ~10s, so the page stays open between tool calls.
Captures are serialised with an ``asyncio.Lock``: the chart is a single piece
of UI state (symbol, timeframe, zoom) and two captures must not interleave.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from ..config import Settings

logger = logging.getLogger(__name__)

VIEWPORT = {"width": 1920, "height": 1080}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ANON_CHART = "https://www.tradingview.com/chart/?symbol={symbol}&interval={interval}"


class CaptureError(RuntimeError):
    pass


class BrowserManager:
    def __init__(self, settings: Settings, mode: str = "anonymous"):
        self.settings = settings
        self._mode = mode
        self._lock = asyncio.Lock()
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    @property
    def mode(self) -> str:
        return self._mode

    def start_url(self, tv_symbol: str, interval: str) -> str:
        if self._mode == "session":
            return self.settings.tradingview_url
        return ANON_CHART.format(symbol=quote(tv_symbol, safe=""), interval=interval)

    async def _launch(self, first_url: str) -> Any:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise CaptureError("playwright is not installed") from exc
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=not self.settings.headful)
        except Exception as exc:
            await self.close()
            raise CaptureError(
                f"Could not start Chromium ({exc}). Run: python -m playwright install chromium"
            ) from exc
        context = await self._browser.new_context(viewport=VIEWPORT, user_agent=USER_AGENT)
        sid = self.settings.tradingview_session_id.get_secret_value()
        if self._mode == "session" and sid:
            await context.add_cookies([{
                "name": "sessionid", "value": sid, "domain": ".tradingview.com", "path": "/",
                "httpOnly": True, "secure": True, "sameSite": "Lax",
            }])
        page = await context.new_page()
        logger.info("opening TradingView (%s mode)", self.mode)
        await page.goto(first_url, wait_until="domcontentloaded", timeout=60_000)
        # TradingView never reaches networkidle (live websockets): give the canvas time.
        await page.wait_for_timeout(8000)
        for _ in range(2):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
        return page

    @asynccontextmanager
    async def page(self, first_url: str) -> AsyncIterator[Any]:
        """Exclusive access to the chart page; relaunches it if it died."""
        async with self._lock:
            if self._page is None or self._page.is_closed():
                await self.close()
                self._page = await self._launch(first_url)
            try:
                yield self._page
            except Exception:
                # Unknown UI state after a failure: start clean next time.
                await self.close()
                raise

    async def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                with contextlib.suppress(Exception):  # best-effort teardown
                    await getattr(obj, meth)()
        self._pw = self._browser = self._page = None
