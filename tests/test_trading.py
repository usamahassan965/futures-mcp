"""The rules loader, the arithmetic guard, and the public toy rules end to end."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from futures_mcp.ranges import pipeline
from futures_mcp.trading.rules import (
    RulesOutputError,
    RulesUnavailableError,
    build_plan,
    load_rules,
    rules_available,
    validate,
)

from .conftest import EXAMPLE_DETECTOR, EXAMPLE_RULES, WINDOWS, Window

ACCOUNT: dict[str, Any] = {"equity": 100_000.0, "risk_pct": 1.0, "point_value": 100.0,
                           "min_contracts": 1, "targets": [1.0, 2.0, 3.0]}

GOOD: dict[str, Any] = {
    "signal": True, "direction": "short", "entry": 100.0, "stop": 110.0, "risk_points": 10.0,
    "contracts": 2, "targets": [{"r_multiple": 1.0, "price": 90.0},
                                {"r_multiple": 2.0, "price": 80.0}],
}


def test_missing_rules_are_a_clear_error(tmp_path: Path) -> None:
    assert not rules_available(tmp_path)
    with pytest.raises(RulesUnavailableError, match="FUTURES_MCP_RULES_DIR"):
        load_rules(tmp_path)


def test_incomplete_rules_module_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "entry_rules.py").write_text("RULES_VERSION = 'x'\n", encoding="utf-8")
    with pytest.raises(RulesUnavailableError, match="lack plan"):
        load_rules(tmp_path)


def test_validate_accepts_a_consistent_plan() -> None:
    validate(GOOD, ACCOUNT)


@pytest.mark.parametrize(("patch", "message"), [
    ({"stop": 95.0}, "at or below entry"),
    ({"direction": "long"}, "at or above entry"),
    ({"direction": "sideways"}, "Unknown trade direction"),
    ({"risk_points": 8.0}, "does not match"),
    ({"targets": [{"r_multiple": 1.0, "price": 91.0}]}, "is not"),
    ({"contracts": 0}, "below the minimum"),
    ({"entry": None}, "missing entry"),
])
def test_validate_rejects_bad_arithmetic(patch: dict[str, Any], message: str) -> None:
    with pytest.raises(RulesOutputError, match=message):
        validate(GOOD | patch, ACCOUNT)


def test_build_plan_normalises_a_no_signal(tmp_path: Path) -> None:
    (tmp_path / "entry_rules.py").write_text(
        "RULES_VERSION = 'x'\n"
        "def plan(bars, structure, account):\n"
        "    return {'signal': False}\n", encoding="utf-8")
    out = build_plan(load_rules(tmp_path), [], {}, ACCOUNT)
    assert out == {"signal": False, "reason": "No entry in this window.", "notes": []}


def test_build_plan_refuses_a_non_dict(tmp_path: Path) -> None:
    (tmp_path / "entry_rules.py").write_text(
        "RULES_VERSION = 'x'\n"
        "def plan(bars, structure, account):\n"
        "    return 'a trade, honest'\n", encoding="utf-8")
    with pytest.raises(RulesOutputError, match="not a dict"):
        build_plan(load_rules(tmp_path), [], {}, ACCOUNT)


@pytest.mark.parametrize("name", sorted(WINDOWS))
def test_toy_rules_meet_the_contract(name: str) -> None:
    window = Window(name)
    report = pipeline.analyze(window.bars, "GC", EXAMPLE_DETECTOR)
    mod = load_rules(EXAMPLE_RULES)
    for structure in report["structures"]:
        plan = build_plan(mod, window.bars, structure, ACCOUNT)  # validates every armed plan
        assert isinstance(plan["signal"], bool)
        assert plan["reason"]
        if plan["signal"]:
            assert plan["contracts"] >= 1
            assert plan["risk_dollars"] == pytest.approx(
                plan["contracts"] * plan["risk_points"] * ACCOUNT["point_value"])
