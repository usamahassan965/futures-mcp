"""Loads the trading rules from a private, un-versioned folder.

Where the range *detector* answers "is there a structure?", these rules answer
"what is the trade on it?" - where the order goes, where the stop goes, what the
targets are and how big the position is. They are proprietary and deliberately
not part of this repository: at runtime ``entry_rules.py`` is imported from
``FUTURES_MCP_RULES_DIR`` (default: the detector folder, ``./private``).

Contract the module must satisfy::

    RULES_VERSION: str
    plan(bars, structure, account) -> dict
    simulate(bars, plan, structure, placed_idx, exit) -> dict   # optional; backtests

``bars`` are the window's bar records (``time_chart_utc5``, ``time_utc``,
``day_chart``, ``high``, ``low``, ``close``; the backtest adds ``open``),
``structure`` is one entry of ``pipeline.analyze(...)["structures"]`` (``S``,
``R``, ``verdict``, ``broke`` and ``rejections`` with
``n``/``side``/``idx``/``price``/``time``) plus the detector's own measurements
under ``"detector"``, and ``account`` carries ``equity``, ``risk_pct``,
``point_value``, ``min_contracts`` and the target ``targets`` R multiples.

The returned dict is either ``{"signal": False, "reason": ...}`` or a plan with
``direction``, ``order``, ``entry``, ``stop``, ``risk_points``, ``contracts``,
``targets``, an optional ``take_profit`` and the exit instructions. Every plan
is re-checked by :func:`validate` before it leaves the server, so a rules bug
cannot hand the caller a stop on the wrong side of the entry.

``simulate`` replays one order over the bars after ``placed_idx`` and reports
``status`` (``cancelled`` | ``closed`` | ``open``), ``fill_idx``/``fill_price``,
``exit_idx``/``exit_price``/``exit_reason``, ``cancel_idx``/``cancel_reason`` and
``ambiguous``; ``exit`` is ``"tp"`` or an R multiple.
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

MODULE_NAME = "entry_rules"
REQUIRED = ("RULES_VERSION", "plan")


class RulesUnavailableError(RuntimeError):
    pass


class RulesOutputError(RuntimeError):
    """A loaded rules module returned something the server will not pass on."""


_cache: dict[Path, ModuleType] = {}
_load_lock = threading.Lock()


def load_rules(rules_dir: Path) -> ModuleType:
    path = (rules_dir / f"{MODULE_NAME}.py").resolve()
    with _load_lock:
        if path in _cache:
            return _cache[path]
        if not path.exists():
            raise RulesUnavailableError(
                f"Trading rules not installed: expected {path}. The entry, stop and sizing "
                "rules are private; set FUTURES_MCP_RULES_DIR to the folder containing "
                f"{MODULE_NAME}.py (examples/rules holds a toy one). Bars, charts and range "
                "detection work without it."
            )
        spec = importlib.util.spec_from_file_location(f"futures_mcp_private.{MODULE_NAME}", path)
        if spec is None or spec.loader is None:
            raise RulesUnavailableError(f"Cannot import trading rules from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        missing = [name for name in REQUIRED if not hasattr(mod, name)]
        if missing:
            raise RulesUnavailableError(f"Trading rules at {path} lack {', '.join(missing)}")
        _cache[path] = mod
        return mod


def rules_available(rules_dir: Path) -> bool:
    return (rules_dir / f"{MODULE_NAME}.py").exists()


def build_plan(mod: ModuleType, bars: list[dict[str, Any]], structure: dict[str, Any],
               account: dict[str, Any]) -> dict[str, Any]:
    """Run the private rules over one structure and validate what comes back."""
    out = mod.plan(bars, structure, account)
    if not isinstance(out, dict):
        raise RulesOutputError(f"{MODULE_NAME}.plan returned {type(out).__name__}, not a dict")
    if not out.get("signal"):
        return {"signal": False, "reason": str(out.get("reason") or "No entry in this window."),
                "notes": list(out.get("notes") or [])}
    validate(out, account)
    return out


def validate(plan: dict[str, Any], account: dict[str, Any]) -> None:
    """Arithmetic the rules must not get wrong. An LLM never gets past this."""
    missing = [k for k in ("direction", "entry", "stop", "risk_points", "contracts", "targets")
               if plan.get(k) is None]
    if missing:
        raise RulesOutputError(f"Trade plan is missing {', '.join(missing)}")
    direction, entry, stop = plan["direction"], float(plan["entry"]), float(plan["stop"])
    if direction not in ("long", "short"):
        raise RulesOutputError(f"Unknown trade direction {direction!r}")
    if direction == "short" and stop <= entry:
        raise RulesOutputError(f"Short with stop {stop} at or below entry {entry}")
    if direction == "long" and stop >= entry:
        raise RulesOutputError(f"Long with stop {stop} at or above entry {entry}")
    risk = float(plan["risk_points"])
    if abs(risk - abs(stop - entry)) > 1e-6 or risk <= 0:
        raise RulesOutputError(f"risk_points {risk} does not match |{entry} - {stop}|")
    sign = -1.0 if direction == "short" else 1.0
    for t in plan["targets"]:
        want = entry + sign * risk * float(t["r_multiple"])
        if abs(float(t["price"]) - want) > 1e-4:
            raise RulesOutputError(
                f"Target {t['r_multiple']}R at {t['price']} is not {want} from entry {entry}")
    if plan.get("take_profit") is not None:
        tp = float(plan["take_profit"])
        if (direction == "short" and tp >= entry) or (direction == "long" and tp <= entry):
            raise RulesOutputError(f"Take profit {tp} is not on the profit side of entry {entry}")
    contracts = int(plan["contracts"])
    if contracts < int(account.get("min_contracts", 1)):
        raise RulesOutputError(f"Position of {contracts} contracts is below the minimum")
