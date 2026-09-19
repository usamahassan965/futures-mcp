"""Loads the range detector from a private, un-versioned folder.

The detection rules are proprietary and are deliberately not part of this
repository. At runtime the module ``range_screener_v6.py`` is imported from
``FUTURES_MCP_DETECTOR_DIR`` (default ``./private``). Without it the range tools
report :class:`DetectorUnavailableError`; every other tool keeps working.

Contract the module must satisfy::

    load(folder) -> DataFrame           # replaced at call time with our frame
    scan(folder, symbol, end_from, end_to, scan_start) -> (completed, anticipation)
    dedupe(hits) -> hits
    demote_by_wider(completed, anticipation) -> (kept, demoted)

Hits are dicts with S, R, H, n_rejections, window_days, score, first_touch,
completion, last_event, broke and events[{side, from, to, extreme}].
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pandas as pd

MODULE_NAME = "range_screener_v6"
REQUIRED = ("load", "scan", "dedupe", "demote_by_wider")


class DetectorUnavailableError(RuntimeError):
    pass


_cache: dict[Path, ModuleType] = {}
_load_lock = threading.Lock()
# scan() reads its frame through the module-global ``load``; swapping it is not
# re-entrant, so scans are serialised.
_scan_lock = threading.Lock()


def load_detector(detector_dir: Path) -> ModuleType:
    path = (detector_dir / f"{MODULE_NAME}.py").resolve()
    with _load_lock:
        if path in _cache:
            return _cache[path]
        if not path.exists():
            raise DetectorUnavailableError(
                f"Range detector not installed: expected {path}. The detection rules are "
                "private; set FUTURES_MCP_DETECTOR_DIR to the folder containing "
                f"{MODULE_NAME}.py. Bars and chart capture work without it."
            )
        spec = importlib.util.spec_from_file_location(f"futures_mcp_private.{MODULE_NAME}", path)
        if spec is None or spec.loader is None:
            raise DetectorUnavailableError(f"Cannot import detector from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        missing = [name for name in REQUIRED if not hasattr(mod, name)]
        if missing:
            raise DetectorUnavailableError(f"Detector at {path} lacks {', '.join(missing)}")
        _cache[path] = mod
        return mod


def detector_available(detector_dir: Path) -> bool:
    return (detector_dir / f"{MODULE_NAME}.py").exists()


def run_scan(mod: ModuleType, df: pd.DataFrame, symbol: str) -> tuple[list[dict[str, Any]], ...]:
    """Scan every session day of ``df``; returns (completed, anticipation, demoted)."""
    days = sorted(df["session_day"].unique())
    if not days:
        return [], [], []
    m: Any = mod  # attributes come from the private module at runtime
    with _scan_lock:
        real = m.load
        m.load = lambda folder: df
        try:
            comp_raw, ant_raw = mod.scan("_frame_", symbol, days[0], days[-1], days[0])
        finally:
            m.load = real
    comp = mod.dedupe(comp_raw)
    ant = mod.dedupe(ant_raw)
    comp, demoted = mod.demote_by_wider(comp, ant)
    return comp, ant, demoted
