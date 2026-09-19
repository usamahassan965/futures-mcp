"""Calibrate a TradingView H1 screenshot and draw detected ranges onto it.

Ported unchanged in behaviour from Trading_bot `screener/chart_overlay.py`; the
golden test re-draws the stored GC July charts pixel-for-pixel. Geometry
constants assume the 1920x1080 capture produced by `capture.tradingview`.

Calibration uses the chart's own axes rather than its candles — the candle
pixels are unreliable once drawn levels, order lines and close-dot markers are
on the chart, but the axis labels are always clean:

  price:  OCR the right-hand gridline labels -> price = m*y + c
  time:   OCR the bottom time labels, match each to the bar carrying that
          chart time -> x = k*i + x0

Both fits are least-squares with outlier trimming and report an RMS residual,
so a bad calibration is detectable instead of silently drawing in the wrong
place. Nothing is drawn unless both residuals are sub-pixel.

Marking convention follows the user's own pen legend:
  black  = completed range (>= 4 rejections)
  orange = structure that never completed
  grey   = approach discarded by rule 7 (labelled with its reach %)
"""
from __future__ import annotations

import re
from datetime import datetime

import numpy as np
import pytesseract
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- geometry --
PANE_X0, PANE_X1 = 56, 1790
PANE_Y0, PANE_Y1 = 110, 762
# Price-pane vertical extent per capture mode. The saved session layout has the
# Relative Volume pane below price (the constants above); the anonymous default
# chart has a single pane reaching the time axis. Only the bottom differs, so
# draw() (which uses the top edge alone) needs no per-mode handling.
PANE_Y = {"session": (PANE_Y0, PANE_Y1), "anonymous": (PANE_Y0, 1005)}
AXIS_X0, AXIS_X1 = 1793, 1872
AXIS_FALLBACK_PAD = 15      # widen the price-axis OCR box only when it reads <4 labels
TIME_Y0, TIME_Y1 = 1015, 1040

# Covers both comma-grouped index prices (GC "4,168.4") and FX quotes
# (6B "1.3380"). OCR occasionally tacks a bracket onto the last-price badge,
# so strip stray punctuation before matching.
PRICE_RE = re.compile(r"^\d{1,3}(?:,\d{3})*\.\d+$")
STRIP = "]|)_[( "
TIME_RE = re.compile(r"^([0-2]?\d):([0-5]\d)$")
DAY_RE = re.compile(r"^([0-3]?\d)$")

BLACK = (17, 17, 17)
ORANGE = (232, 126, 4)
GREY = (120, 120, 120)


def _ocr(img, box, scale=3):
    crop = img.crop(box)
    crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
    d = pytesseract.image_to_data(crop, output_type=pytesseract.Output.DICT)
    out = []
    for txt, left, top, w, h, conf in zip(d["text"], d["left"], d["top"],
                                          d["width"], d["height"], d["conf"]):
        t = txt.strip()
        if t and float(conf) >= 60:
            out.append((t,
                        box[0] + (left + w / 2) / scale,
                        box[1] + (top + h / 2) / scale))
    return out


def _theil_sen(a, b):
    """Median of pairwise slopes — tolerates ~29% gross outliers."""
    n = len(a)
    sl = [(b[j] - b[i]) / (a[j] - a[i])
          for i in range(n) for j in range(i + 1, n) if a[j] != a[i]]
    if not sl:
        return None
    m = float(np.median(sl))
    return m, float(np.median(b - m * a))


def _fit(a, b, max_iter=8, floor=0.8):
    """Least squares b = m*a + c with iterative outlier trimming.

    Seeded from a Theil-Sen estimate rather than a plain least-squares pass: a
    single gross OCR misread (e.g. "41.2700" read off a "1.3270" gridline) will
    otherwise dominate the seed, inflate the median residual, and survive every
    trimming round.
    """
    a, b = np.asarray(a, float), np.asarray(b, float)
    live = np.ones(len(a), bool)
    seed = _theil_sen(a, b)
    if seed is not None:
        m0, c0 = seed
        res0 = np.abs(b - (m0 * a + c0))
        cand = res0 <= max(floor, 3 * np.median(res0))
        if cand.sum() >= max(3, 0.5 * len(a)):
            live = cand
    m = c = 0.0
    for _ in range(max_iter):
        m, c = np.polyfit(a[live], b[live], 1)
        res = np.abs(b - (m * a + c))
        new = res <= max(floor, 3 * np.median(res[live]))
        if new.sum() < max(3, 0.5 * len(a)) or (new == live).all():
            break
        live = new
    m, c = np.polyfit(a[live], b[live], 1)
    rms = float(np.sqrt(np.mean((b[live] - (m * a[live] + c)) ** 2)))
    return float(m), float(c), rms, live


def calibrate(img, bars, pane_y=(PANE_Y0, PANE_Y1)):
    """Return (calibration dict, error string). Price and time from the axes."""
    PANE_Y0, PANE_Y1 = pane_y  # shadows the module defaults
    # ---- price axis -------------------------------------------------------
    # AXIS_X0 clips the leading digit on symbols whose labels are wide enough
    # to start left of it: 6N's "0.58720" OCRs as ").58720", the ")" is peeled
    # by STRIP, and PRICE_RE then rejects a bare ".58720" — 4 of 16 6N charts
    # read ZERO labels this way. Widening AXIS_X0 outright fixes them but also
    # perturbs ~half the calibrations that already work (an extra label joins
    # the fit and moves px_per_unit in the 5th decimal), which would redraw
    # marked charts for no gain. So the wider box is a FALLBACK: it only runs
    # when the normal box comes up short, leaving working charts bit-identical.
    def _price_labels(x0):
        out = []
        for t, _, y in _ocr(img, (x0, PANE_Y0, AXIS_X1, PANE_Y1)):
            t = t.strip(STRIP)
            if PRICE_RE.match(t):
                out.append((float(t.replace(",", "")), y))
        return out

    labs = _price_labels(AXIS_X0)
    if len(labs) < 4:
        labs = _price_labels(AXIS_X0 - AXIS_FALLBACK_PAD)
    if len(labs) < 4:
        return None, f"only {len(labs)} price labels"
    prices = np.array(sorted(p for p, _ in labs))
    steps = np.diff(prices)
    step = float(np.median(steps[steps > 0]))
    # Keep only labels sitting on the regular gridline interval — this drops
    # the last-price badge and any order-line badges, which sit off-grid.
    # Tolerance is relative so it holds for a 20.0 gold step and a 0.0010 FX one.
    grid = [(p, y) for p, y in labs if abs(p / step - round(p / step)) < 1e-4]
    if len(grid) < 3:
        return None, f"only {len(grid)} gridline labels at step {step}"
    # Fit y = m2*price + c2 so the residual (and therefore the trim floor) is
    # in PIXELS and scale-invariant across symbols, then invert to price(y).
    m2, c2, rms_p_px, _ = _fit([p for p, _ in grid], [y for _, y in grid], floor=1.5)
    if abs(m2) < 1e-12:
        return None, "degenerate price axis"
    m, c = 1.0 / m2, -c2 / m2

    # ---- time axis --------------------------------------------------------
    times = [datetime.strptime(b["time_chart_utc5"], "%Y-%m-%d %H:%M")
             for b in bars]
    toks = _ocr(img, (PANE_X0, TIME_Y0, PANE_X1, TIME_Y1), scale=2)
    toks.sort(key=lambda t: t[1])
    # A label like "09:00" can match a bar on any day in the window, and the
    # crosshair tooltip adds spurious tokens. Collect every candidate pairing
    # and let RANSAC pick the consistent set instead of matching greedily.
    cands = []            # (x, [possible bar indices])
    for t, x, _ in toks:
        mt, md = TIME_RE.match(t), DAY_RE.match(t)
        if mt:
            hh, mm = int(mt.group(1)), int(mt.group(2))
            hits = [j for j, tv in enumerate(times)
                    if tv.hour == hh and tv.minute == mm]
        elif md:
            day = int(md.group(1))
            hits = [j for j, tv in enumerate(times)
                    if tv.day == day and (j == 0 or times[j - 1].day != day)]
        else:
            continue
        if hits:
            cands.append((x, hits))
    if len(cands) < 5:
        return None, f"only {len(cands)} usable time labels"

    best = None
    for ai in range(len(cands)):
        for bi in range(ai + 1, len(cands)):
            xa, ha = cands[ai]
            xb, hb = cands[bi]
            for ja in ha:
                for jb in hb:
                    if jb == ja:
                        continue
                    kk = (xb - xa) / (jb - ja)
                    if not 4 < kk < 60:
                        continue
                    xx0 = xa - kk * ja
                    inl = []
                    for x, hits in cands:
                        j = min(hits, key=lambda j: abs(kk * j + xx0 - x))
                        if abs(kk * j + xx0 - x) <= 2.0:
                            inl.append((j, x))
                    if best is None or len(inl) > len(best):
                        best = inl
    if best is None or len(best) < 5:
        return None, "time-axis RANSAC found no consistent label set"
    idxs = [j for j, _ in best]
    xs = [x for _, x in best]
    k, x0, rms_x, _ = _fit(idxs, xs)

    hi = max(b["high"] for b in bars)
    lo = min(b["low"] for b in bars)
    y_hi, y_lo = (hi - c) / m, (lo - c) / m
    # Sanity check, not an accuracy check: it catches "these bars are not the
    # bars on this chart". TradingView's auto-scale occasionally clips a wick
    # on the window's final sliver bar by a pixel or two — 6E 03_07_to_07_07
    # overshot by 6px on one bar with a price fit RMS of 5e-11 — so the bottom
    # allowance is loose enough to survive that without admitting a real
    # mismatch (a wrong chart misses by hundreds of px).
    span_ok = PANE_Y0 - 45 < y_hi < y_lo < PANE_Y1 + 25

    cal = {
        "m": m, "c": c, "k": k, "x0": x0,
        "grid_step": step, "n_price_labels": len(grid), "n_time_labels": len(idxs),
        "rms_price_px": rms_p_px, "rms_x_px": rms_x,
        "px_per_unit": float(-1 / m),
        "y_window_high": float(y_hi), "y_window_low": float(y_lo),
        "span_ok": bool(span_ok), "pane_y": list(pane_y),
        "trustworthy": bool(rms_p_px < 1.0 and rms_x < 1.5 and span_ok),
    }
    return cal, None


def px(cal, price):
    return (price - cal["c"]) / cal["m"]


def bx(cal, i):
    return cal["k"] * i + cal["x0"]


# ------------------------------------------------------------------ drawing --
def _font(size, bold=False):
    for nm in (("arialbd.ttf", "arial.ttf") if bold else ("arial.ttf",)):
        try:
            return ImageFont.truetype(nm, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _badge(d, xy, text, colour, font, pad=4):
    x, y = xy
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    w, h = r - l, b - t
    d.rectangle([x - pad, y - pad, x + w + pad, y + h + pad],
                fill=(255, 255, 255), outline=colour, width=2)
    d.text((x - l, y - t), text, fill=colour, font=font)
    return w + 2 * pad, h + 2 * pad


def draw(png_in, png_out, bars, cal, structures):
    """Draw each structure's boundaries, numbered rejections and discards."""
    img = Image.open(png_in).convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")
    f_num = _font(15, bold=True)
    f_cap = _font(16, bold=True)
    f_small = _font(13)

    for st in structures:
        colour = BLACK if st["verdict"] == "COMPLETED" else ORANGE
        i0 = min(e["idx"] for e in st["rejections"] + st.get("discards", []))
        i1 = max(e["idx"] for e in st["rejections"] + st.get("discards", []))
        x_a, x_b = bx(cal, i0) - 14, bx(cal, i1) + 14
        y_r, y_s = px(cal, st["R"]), px(cal, st["S"])

        d.rectangle([x_a, y_r, x_b, y_s], fill=colour + (18,))
        for y in (y_r, y_s):
            d.line([x_a, y, x_b, y], fill=colour, width=3)
        d.line([x_a, y_r, x_a, y_s], fill=colour + (120,), width=1)
        d.line([x_b, y_r, x_b, y_s], fill=colour + (120,), width=1)

        for e in st["rejections"]:
            x = bx(cal, e["idx"])
            y = px(cal, e["price"])
            up = e["side"] == "R"
            cy = y - 22 if up else y + 22
            d.ellipse([x - 12, cy - 12, x + 12, cy + 12],
                      fill=(255, 255, 255), outline=colour, width=3)
            t = str(e["n"])
            l, tp, r, b = d.textbbox((0, 0), t, font=f_num)
            d.text((x - (r - l) / 2 - l, cy - (b - tp) / 2 - tp), t,
                   fill=colour, font=f_num)
            d.line([x, cy + (12 if up else -12), x, y], fill=colour, width=2)

        for e in st.get("discards", []):
            x = bx(cal, e["idx"])
            y = px(cal, e["price"])
            up = e["side"] == "R"
            ty = y - 30 if up else y + 18
            d.line([x, y, x, ty + (12 if up else -2)], fill=GREY, width=2)
            d.text((x + 5, ty), f"x {e['reach_pct']:.0f}%", fill=GREY, font=f_small)

        cap = st["caption"]
        cx = max(x_a, PANE_X0 + 4)
        cy_cap = max(min(y_r, y_s) - 44, PANE_Y0 + 4)
        # A rejection circle sitting just above the R line lands in the
        # caption's band and punches a white hole through the text. Slide the
        # caption right until it clears every colliding circle.
        l, t, r, b = d.textbbox((0, 0), cap, font=f_cap)
        cap_w, cap_h = r - l, b - t
        for e in st["rejections"]:
            ex = bx(cal, e["idx"])
            ey = px(cal, e["price"]) + (-22 if e["side"] == "R" else 22)
            if ey + 12 > cy_cap - 4 and ey - 12 < cy_cap + cap_h + 4 and ex + 12 > cx:
                cx = ex + 16
        # Sliding right can push the badge off the pane. Pull it back inside and
        # lift it a row instead, so it stays legible and still clears the marks.
        if cx + cap_w > PANE_X1 - 4:
            cx = max(PANE_X0 + 4, PANE_X1 - cap_w - 4)
            cy_cap = max(cy_cap - (cap_h + 14), PANE_Y0 + 4)
        _badge(d, (cx, cy_cap), cap, colour, f_cap)

    img.save(png_out)
    return png_out


# ------------------------------------------------------------ MCP additions --
NO_RANGE_TEXT = "NO RANGE in this window"


def set_tesseract_cmd(cmd: str | None) -> None:
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd


def render(png_in, png_out, bars, cal, structures):
    """Draw the structures, or the grey NO RANGE badge when there are none."""
    if structures:
        return draw(png_in, png_out, bars, cal, structures)
    img = Image.open(png_in).convert("RGB")
    _badge(ImageDraw.Draw(img, "RGBA"), (70, 130), NO_RANGE_TEXT, (90, 90, 90),
           _font(17, bold=True))
    img.save(png_out)
    return png_out
