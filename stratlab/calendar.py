"""Trading-calendar expectations per asset class.

Used to decide whether an old bar is a feed failure or simply a market that
was closed. Getting this wrong in the cautious direction halts a healthy bot on
every weekend; getting it wrong the other way trades on a dead feed.

Only the weekend calendar is modelled. Indian exchange holidays are not encoded
here, so a `holiday_grace_days` allowance covers them rather than pretending to
know the NSE holiday list.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pandas as pd

SATURDAY = 5
SUNDAY = 6
NSE_CLOSE_UTC_HOUR = 10  # 15:30 IST


def is_weekend(day: date) -> bool:
    return day.weekday() in (SATURDAY, SUNDAY)


def previous_weekday(day: date) -> date:
    cursor = day
    while is_weekend(cursor):
        cursor -= timedelta(days=1)
    return cursor


def expected_last_bar(
    today: date,
    asset_class: str,
    now_hour_utc: int = NSE_CLOSE_UTC_HOUR,
) -> date:
    """Latest date on which a completed daily bar should exist.

    Crypto trades every calendar day. Indian equities trade Mon-Fri and close at
    15:30 IST, which is 10:00 UTC, so before that hour a weekday has not produced
    a completed bar yet and the previous session is still the newest one.
    """
    if asset_class == "crypto":
        return today

    if is_weekend(today):
        return previous_weekday(today)
    if now_hour_utc < NSE_CLOSE_UTC_HOUR:
        return previous_weekday(today - timedelta(days=1))
    return today


def is_stale(
    last_bar: date,
    today: date,
    asset_class: str,
    holiday_grace_days: int = 4,
) -> bool:
    """True only when the gap exceeds what the calendar can explain.

    The grace window is what stops an unmodelled exchange holiday from being
    reported as a dead feed.
    """
    expected = expected_last_bar(today, asset_class)
    return (expected - last_bar).days > holiday_grace_days


def behind_calendar(last_bar: date, today: date, asset_class: str) -> bool:
    """True when a bar newer than `last_bar` should already have been published.

    This is the *refresh* trigger, deliberately stricter than is_stale: no grace
    window, because the question is "should today's data exist yet", not "is this
    feed dead". A flat refresh interval cannot answer that, which is how a
    dashboard ends up showing yesterday while the exchange shows today.
    """
    return last_bar < expected_last_bar(today, asset_class)


def session_end(bar_date: date, asset_class: str) -> datetime:
    """When the trading session covered by `bar_date` finished.

    Indian equities close at 15:30 IST. Crypto has no close; its UTC day simply
    rolls over at midnight.
    """
    if asset_class == "crypto":
        return datetime.combine(bar_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return datetime.combine(bar_date, time(NSE_CLOSE_UTC_HOUR), tzinfo=timezone.utc)


def session_closed(bar_date: date, asset_class: str, now: datetime | None = None) -> bool:
    """True once the session behind this bar is over, so its close is final."""
    now = now or datetime.now(timezone.utc)
    return now >= session_end(bar_date, asset_class)


def drop_unfinished(frame, asset_class: str, now: datetime | None = None):
    """Remove the newest bar while its session is still running.

    Yahoo publishes a daily candle the moment the session opens and revises it
    until the close. A strategy reading that bar is reading a number that can
    still move, so it is not eligible for a signal or an execution.
    """
    if frame is None or frame.empty:
        return frame
    last = pd.Timestamp(frame.index[-1]).date()
    if session_closed(last, asset_class, now):
        return frame
    return frame.iloc[:-1]


def bars_expected_since(last_bar: date, today: date, asset_class: str) -> int:
    """How many sessions should have appeared between two dates."""
    expected = expected_last_bar(today, asset_class)
    if expected <= last_bar:
        return 0
    if asset_class == "crypto":
        return (expected - last_bar).days
    count = 0
    cursor = last_bar + timedelta(days=1)
    while cursor <= expected:
        if not is_weekend(cursor):
            count += 1
        cursor += timedelta(days=1)
    return count