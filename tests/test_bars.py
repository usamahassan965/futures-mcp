from __future__ import annotations

from datetime import UTC, date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from futures_mcp.data import bars as B
from futures_mcp.data.timewindow import parse_date, window_utc
from futures_mcp.symbols import UnknownSymbolError, resolve

from .conftest import Window

GC = resolve("GC1!")


def test_window_counts_trading_days_and_skips_weekend() -> None:
    # Tue 2026-07-07, 3 trading days -> Fri 03, Mon 06, Tue 07
    start, end = window_utc(date(2026, 7, 7), 3)
    assert start == datetime(2026, 7, 3, tzinfo=UTC)
    assert end == datetime(2026, 7, 8, tzinfo=UTC)


def test_friday_window_ends_at_cme_weekly_close() -> None:
    start, end = window_utc(date(2026, 7, 3), 3)
    assert start == datetime(2026, 7, 1, tzinfo=UTC)
    assert end == datetime(2026, 7, 3, 22, tzinfo=UTC)


def test_window_matches_fixture_span(window: Window) -> None:
    last = date.fromisoformat(window.payload["window"]["trading_days"][-1])
    start, end = window_utc(last, 3)
    assert [f"{start:%Y-%m-%d %H:%M}", f"{end:%Y-%m-%d %H:%M}"] == \
        window.payload["window"]["utc_span"]


def test_parse_date_rolls_weekend_back() -> None:
    assert parse_date("2026-07-05") == date(2026, 7, 3)  # Sunday -> Friday
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_date("07/05/2026")


@pytest.mark.parametrize(("rel", "zone"), [
    (0.49, "very_low"), (0.5, "below_average"), (1.0, "above_average"),
    (1.9, "high"), (2.2, "very_high"), (3.5, "ultra_high"), (9.0, "ultra_high"),
])
def test_volume_zones(rel: float, zone: str) -> None:
    assert B.volume_zone(rel) == zone


def test_symbol_registry() -> None:
    assert resolve("gc").tv_symbol == "COMEX:GC1!"
    with pytest.raises(UnknownSymbolError, match="Supported: GC1!"):
        resolve("ES1!")


def test_timeframe_validation() -> None:
    assert B.check_timeframe("h4") == "H4"
    with pytest.raises(ValueError, match="Supported: H1, H4"):
        B.check_timeframe("M5")


def _synthetic(n: int = 24 * 12) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-06-24 00:00", periods=n, freq="h")  # naive, like tvdatafeed
    close = 4000 + rng.normal(0, 5, n).cumsum()
    return pd.DataFrame({
        "open": close + rng.normal(0, 1, n),
        "high": close + 6,
        "low": close - 6,
        "close": close,
        "volume": rng.integers(1000, 20000, n).astype(float),
    }, index=idx)


def test_payload_has_the_fixture_schema(window: Window) -> None:
    df = B.prepare(_synthetic(), source_tz=UTC)
    got = B.window_payload(df, GC, "H1", date(2026, 7, 3), 3)
    assert got["window_utc"] == ["2026-07-01 00:00", "2026-07-03 22:00"]
    assert got["n_bars"] == 70
    assert set(got["bars"][0]) == set(window.bars[0])
    assert got["bars"][0]["time_chart_utc5"] == "2026-07-01 05:00"
    b = got["bars"][20]
    assert b["rel_volume"] == pytest.approx(b["volume"] / b["sma14"], abs=0.01)
    assert b["volume_zone"] == B.volume_zone(b["volume"] / b["sma14"])
    assert sum(d["bars"] for d in got["days"]) == got["n_bars"]


def test_empty_window_is_explained() -> None:
    df = B.prepare(_synthetic(), source_tz=UTC)
    with pytest.raises(B.DataUnavailableError, match="no bars in window"):
        B.window_payload(df, GC, "H1", date(2025, 1, 6), 3)


def test_local_timestamps_are_converted_to_utc() -> None:
    df = B.prepare(_synthetic(30), source_tz=timezone(pd.Timedelta(hours=5).to_pytimedelta()))
    assert str(df.index[0]) == "2026-06-23 19:00:00+00:00"


def test_feed_retries_then_raises(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    pytest.importorskip("tvDatafeed")

    class Dead:
        calls = 0

        def get_hist(self, **_: object) -> None:
            Dead.calls += 1

        def __init__(self) -> None:
            pass

    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    feed = B.BarFeed(tmp_path, pace_seconds=0)
    feed._tv = Dead()
    with pytest.raises(B.DataUnavailableError, match="after 4 attempts"):
        feed.history(GC, "H1")
    assert Dead.calls == 4


def test_closed_window_is_cached_on_disk(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    feed = B.BarFeed(tmp_path)
    frame = B.prepare(_synthetic(), source_tz=UTC)
    calls = []
    monkeypatch.setattr(feed, "history", lambda inst, tf: calls.append(tf) or frame)
    a = feed.window(GC, "H1", date(2026, 7, 3), 3)
    b = feed.window(GC, "H1", date(2026, 7, 3), 3)
    assert a == b and calls == ["H1"]
    assert (tmp_path / "gc1" / "H1_2026-07-03_3d.json").exists()
