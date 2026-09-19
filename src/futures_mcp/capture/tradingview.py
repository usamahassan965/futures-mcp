"""TradingView UI automation: symbol/timeframe switching and range-zoom capture.

Async port of Trading_bot ``src/browser/screenshot_capture.py``. The capture
frames exactly the trading-day window of :func:`timewindow.window_utc` through
TradingView's Go-to (Alt+G) "Custom range" dialog, so a screenshot and the bars
fetched for the same window describe the same span, which is what lets the
overlay calibrate one against the other.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..data.timewindow import window_utc
from ..symbols import Instrument
from .browser import BrowserManager, CaptureError

logger = logging.getLogger(__name__)

TIMEFRAME_MAP = {"H1": "60", "H4": "240", "D1": "1D"}
Progress = Callable[[float, str], Awaitable[None]]

_PANEL_OPEN_JS = """() => {
  const el = document.querySelector('div.widgetbar-pages, [data-name="widgetbar-pages-with-tabs"]');
  if (!el) return false;
  const r = el.getBoundingClientRect();
  return r.width > 50 && r.height > 50;
}"""

_INDICATOR_JS = r"""() => {
  const nodes = document.querySelectorAll(
    '[class*="pane-legend"], [class*="legend"], [data-name*="legend"]');
  for (const n of nodes) {
    const t = n.innerText || n.textContent || "";
    if (/Relative Volume/i.test(t)) {
      const empties = (t.match(/∅/g) || []).length;
      const afterPeriod = t.split(/Relative Volume[^\d]*\d+/i)[1] || "";
      return { empties, hasNums: /\d[\d,\.]*/.test(afterPeriod) };
    }
  }
  return null;
}"""


@dataclass
class Capture:
    path: Path
    png: bytes
    mode: str
    window_utc: tuple[datetime, datetime]
    warnings: list[str]


# ------------------------------------------------------------------ helpers --
async def _title_ticker(page: Any) -> str:
    try:
        title = (await page.title() or "").strip()
        return title.split()[0].upper() if title else ""
    except Exception:
        return ""


async def verify_symbol(page: Any, tv_symbol: str, attempts: int = 20) -> bool:
    """Poll the page title (TradingView puts the active ticker first)."""
    expected = tv_symbol.split(":")[-1].upper().replace("!", "")
    for _ in range(attempts):
        current = (await _title_ticker(page)).replace("!", "")
        if current and (expected == current or expected in current):
            return True
        await page.wait_for_timeout(400)
    return False


async def wait_for_indicator(page: Any, timeout_ms: int = 10_000) -> bool:
    """Wait until the Relative Volume legend shows numbers instead of ∅ placeholders.

    Capturing before that yields an empty indicator and an unsettled canvas on
    which the Alt+G range zoom silently misfires.
    """
    for _ in range(max(1, timeout_ms // 300)):
        try:
            r = await page.evaluate(_INDICATOR_JS)
            if r and r.get("empties", 1) == 0 and r.get("hasNums"):
                return True
        except Exception:
            pass
        await page.wait_for_timeout(300)
    return False


async def collapse_right_panel(page: Any) -> bool:
    """Close the watchlist/details sidebar, which steals ~270px of chart width.

    TradingView stores its open state per symbol in the layout, so this must run
    after every symbol switch, not just once per page load.
    """
    try:
        if not await page.evaluate(_PANEL_OPEN_JS):
            return False
        btn = page.locator('button[data-name="base"][aria-label*="Watchlist" i]')
        if await btn.count() == 0 or not await btn.first.is_visible():
            return False
        await btn.first.click(timeout=1500)
        await page.wait_for_timeout(500)
        closed = not await page.evaluate(_PANEL_OPEN_JS)
        if not closed:
            logger.warning("right panel toggle did not collapse it")
        return closed
    except Exception as exc:
        logger.warning("right panel collapse raised %r", exc)
        return False


async def _open_symbol_search(page: Any) -> bool:
    for _ in range(2):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
    for sel in ('#header-toolbar-symbol-search', '[data-name="symbol-search-button"]',
                'button[aria-label*="Symbol Search" i]'):
        try:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=2000)
                await page.wait_for_timeout(800)
                for probe in ('input[data-role="search"]',
                              'div[data-name="symbol-search-items-dialog"]',
                              'div[role="dialog"] input'):
                    if await page.locator(probe).count() > 0:
                        return True
        except Exception:
            continue
    try:  # typing on the focused chart opens the search dialog
        await page.mouse.click(960, 450)
        await page.wait_for_timeout(200)
        await page.keyboard.type("a")
        await page.wait_for_timeout(700)
        if await page.locator('div[role="dialog"] input').count() > 0:
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            return True
    except Exception:
        pass
    return False


async def switch_symbol(page: Any, tv_symbol: str, max_attempts: int = 3) -> bool:
    if await verify_symbol(page, tv_symbol, attempts=1):
        return True
    for attempt in range(1, max_attempts + 1):
        if not await _open_symbol_search(page):
            logger.warning("symbol search did not open (attempt %d)", attempt)
            await page.wait_for_timeout(1000)
            continue
        inputs = page.locator('div[role="dialog"] input')
        try:
            if await inputs.count() > 0:
                await inputs.first.fill("")
                await inputs.first.type(tv_symbol, delay=30)
            else:
                await page.keyboard.press("Control+a")
                await page.keyboard.type(tv_symbol, delay=30)
        except Exception:
            await page.keyboard.press("Control+a")
            await page.keyboard.type(tv_symbol, delay=30)
        await page.wait_for_timeout(1200)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2000)
        if await verify_symbol(page, tv_symbol):
            return True
        for _ in range(2):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
    return False


async def switch_timeframe(page: Any, tf: str) -> None:
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(200)
    for ch in TIMEFRAME_MAP.get(tf, tf):
        await page.keyboard.press(ch)
    await page.wait_for_timeout(300)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(3000)


CHART_TIMEZONE = "(UTC+5) Karachi"  # fixed UTC+5, no DST


async def ensure_chart_timezone(page: Any) -> bool:
    """Put the chart axis on UTC+5, the zone every chart time in this server uses.

    A saved layout already has it; the anonymous chart starts on UTC. Overlay
    calibration matches axis labels to bar times, so the zones must agree.
    """
    btn = page.locator('[data-name="time-zone-menu"]')
    try:
        if await btn.count() == 0:
            return False
        current = (await btn.first.get_attribute("title") or "") + await btn.first.inner_text()
        if current.strip().endswith("UTC+5"):
            return True
        await btn.first.click(timeout=2000)
        await page.wait_for_timeout(600)
        await page.get_by_text(CHART_TIMEZONE, exact=True).first.click(timeout=3000)
        await page.wait_for_timeout(1000)
        return True
    except Exception as exc:
        logger.warning("chart timezone switch failed: %r", exc)
        await page.keyboard.press("Escape")
        return False


async def set_back_adjustment(page: Any, on: bool) -> bool | None:
    """Set the B-ADJ toggle (back-adjust prices for contract rolls).

    The bars come unadjusted from tvDatafeed; a back-adjusted chart shifts every
    price by the roll gaps, so drawn levels would land in the wrong place.
    Returns whether the toggle changed, or None if it could not be set.
    """
    btn = page.locator('[data-name="backAdj"]')
    try:
        if await btn.count() == 0:
            return False
        if (await btn.first.get_attribute("aria-pressed") == "true") == on:
            return False
        await btn.first.click(timeout=2000)
        await page.wait_for_timeout(1500)
        if (await btn.first.get_attribute("aria-pressed") == "true") != on:
            return None
        return True
    except Exception as exc:
        logger.warning("back-adjustment toggle failed: %r", exc)
        return None


async def clear_overlays(page: Any) -> None:
    """Dismiss first-visit hints and park the mouse so no crosshair is captured."""
    for label in ("Got it!", "Got it"):
        try:
            loc = page.get_by_role("button", name=label)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click(timeout=1500)
        except Exception:
            pass
    await page.mouse.move(1900, 540)  # right toolbar, off the chart
    await page.wait_for_timeout(300)


# --------------------------------------------------------------- range zoom --
async def _dialog_visible(page: Any) -> bool:
    try:
        return bool(await page.locator('div[role="dialog"]').first.is_visible())
    except Exception:
        return False


async def _click_custom_range(page: Any) -> bool:
    for sel in ('div[role="dialog"] button:has-text("Custom range")',
                'div[role="dialog"] [role="tab"]:has-text("Custom")',
                'text="Custom range"'):
        try:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click()
                await page.wait_for_timeout(400)
                return True
        except Exception:
            continue
    return False


async def _fill_custom_range(page: Any, start: datetime, end: datetime) -> bool:
    """Intraday layouts show [from_date, from_time, to_date, to_time]; on daily+
    the time inputs are disabled; some layouts show dates only."""
    inputs = page.locator('div[role="dialog"] input')
    count = await inputs.count()
    if count < 2:
        return False
    try:
        if count >= 4:
            await inputs.nth(0).fill(f"{start:%Y-%m-%d}")
            if await inputs.nth(1).is_enabled():
                await inputs.nth(1).fill(f"{start:%H:%M}")
            await inputs.nth(2).fill(f"{end:%Y-%m-%d}")
            if await inputs.nth(3).is_enabled():
                await inputs.nth(3).fill(f"{end:%H:%M}")
        else:
            await inputs.nth(0).fill(f"{start:%Y-%m-%d}")
            await inputs.nth(1).fill(f"{end:%Y-%m-%d}")
        await page.wait_for_timeout(400)
        return True
    except Exception as exc:
        logger.warning("custom range fill failed: %r", exc)
        return False


async def _submit(page: Any) -> None:
    for sel in ('div[role="dialog"] button:has-text("Go to")',
                'div[role="dialog"] button:has-text("Apply")',
                'div[role="dialog"] button[type="submit"]'):
        try:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.click()
                await page.wait_for_timeout(2800)  # re-render + indicator recompute
                return
        except Exception:
            continue
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(2800)


async def range_zoom(page: Any, start: datetime, end: datetime, has_indicator: bool) -> None:
    """Frame [start, end) (naive UTC wall times, as the dialog expects them)."""
    last = "unknown"
    for attempt in (1, 2):
        if has_indicator:
            await wait_for_indicator(page, timeout_ms=4000)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        await page.mouse.click(960, 450)
        await page.wait_for_timeout(200)
        await page.keyboard.press("Alt+g")
        await page.wait_for_timeout(800)
        if not await _dialog_visible(page):
            last = "Go-to dialog did not open"
        elif not await _click_custom_range(page):
            last = "Custom range tab not found"
        elif not await _fill_custom_range(page, start, end):
            last = "could not fill the custom range"
        else:
            await _submit(page)
            return
        logger.warning("range zoom attempt %d: %s", attempt, last)
        for _ in range(3):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
        await page.wait_for_timeout(1500)
    raise CaptureError(f"TradingView range zoom failed: {last}")


# ------------------------------------------------------------------ capture --
async def capture_window(
    browser: BrowserManager, inst: Instrument, tf: str, target: date, days: int,
    out_dir: Path, progress: Progress | None = None,
) -> Capture:
    async def step(p: float, msg: str) -> None:
        logger.info("capture %s %s: %s", inst.symbol, tf, msg)
        if progress:
            await progress(p, msg)

    start, end = window_utc(target, days)
    warnings: list[str] = []
    session = browser.mode == "session"
    await step(0.05, "opening TradingView")
    async with browser.page(browser.start_url(inst.tv_symbol, TIMEFRAME_MAP[tf])) as page:
        await step(0.35, f"switching to {inst.tv_symbol}")
        if not await switch_symbol(page, inst.tv_symbol):
            raise CaptureError(f"Could not load {inst.tv_symbol} on TradingView")
        await collapse_right_panel(page)
        await step(0.5, f"switching to {tf}")
        await switch_timeframe(page, tf)
        if not await ensure_chart_timezone(page):
            warnings.append("Could not set the chart timezone to UTC+5; the overlay may "
                            "refuse to calibrate")
        adj_changed = await set_back_adjustment(page, on=False)
        if adj_changed is None:
            warnings.append("Could not turn off back-adjustment (B-ADJ); chart prices may "
                            "differ from the bars")
        if session and not await wait_for_indicator(page):
            warnings.append("Relative Volume indicator not ready; captured anyway")
        await step(0.65, f"framing {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC")
        await range_zoom(page, start.replace(tzinfo=None), end.replace(tzinfo=None), session)
        await step(0.9, "taking screenshot")
        await clear_overlays(page)
        png: bytes = await page.screenshot(full_page=False)
        if adj_changed:
            # Leave a saved layout as we found it (TradingView autosaves it).
            await set_back_adjustment(page, on=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{tf}_{target}_{days}d_{browser.mode}.png"
    path.write_bytes(png)
    await step(1.0, "done")
    return Capture(path, png, browser.mode, (start, end), warnings)
