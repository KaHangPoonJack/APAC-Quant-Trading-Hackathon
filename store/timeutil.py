"""UTC time helpers and a SQLAlchemy type that stores tz-aware datetimes.

We standardise on UTC everywhere (crypto trades 24/7 and Binance bars are UTC).
SQLite can't natively preserve tzinfo, so `UTCDateTime` persists ISO-8601
strings with a fixed +00:00 offset — which also sorts lexicographically in the
correct chronological order. Display-side code may convert to local time.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import String, TypeDecorator

UTC_TZ = timezone.utc


def now_utc() -> datetime:
    """Current time as a tz-aware UTC datetime."""
    return datetime.now(UTC_TZ)


def to_utc(dt: datetime) -> datetime:
    """Normalise any datetime to UTC (assume UTC if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(UTC_TZ)


def today_utc() -> date:
    return now_utc().date()


def from_epoch_ms(ms: float) -> datetime:
    """Roostoo/Binance 13-digit millisecond timestamps -> tz-aware UTC."""
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC_TZ)


class UTCDateTime(TypeDecorator):
    """Persist tz-aware datetimes as UTC ISO-8601 strings."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return to_utc(value).isoformat()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return to_utc(datetime.fromisoformat(value))
