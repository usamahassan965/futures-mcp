"""Trading-day windows and the chart clock.

Every screenshot, bar slice and scan in this project shares one window rule so
that the bars and the picture always describe the same span:

* a window is ``days`` trading days (Mon-Fri) inclusive of the target day;
* it starts 00:00 UTC on the earliest day;
* it ends 22:00 UTC on a Friday target (CME weekly close), otherwise 00:00 UTC
  of the following calendar day.

TradingView's display axis on these charts is UTC+5; bars carry that clock as
``time_chart_utc5`` so they line up with the screenshot's axis labels.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

CHART_TZ = timezone(timedelta(hours=5))


def last_trading_day(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def default_target(now: datetime | None = None) -> date:
    """The trading day whose session is current (or most recent) in UTC."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return last_trading_day(now.date())


def window_utc(target: date, days: int) -> tuple[datetime, datetime]:
    if days < 1:
        raise ValueError("days must be >= 1")
    d, remaining = target, days
    while remaining > 1:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            remaining -= 1
    start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    if target.weekday() == 4:
        end = datetime(target.year, target.month, target.day, 22, tzinfo=UTC)
    else:
        end = datetime(target.year, target.month, target.day, tzinfo=UTC) + timedelta(days=1)
    return start, end


def utc_to_chart(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(CHART_TZ)


def chart_to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CHART_TZ)
    return dt.astimezone(UTC)


def parse_date(value: str | date | None) -> date:
    """``None`` -> current trading day; ``YYYY-MM-DD`` -> that day (weekends roll back)."""
    if value is None or value == "":
        return default_target()
    if isinstance(value, date):
        return last_trading_day(value)
    try:
        return last_trading_day(date.fromisoformat(value))
    except ValueError:
        raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from None
