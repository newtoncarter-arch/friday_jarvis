"""US regular-trading-hours helpers."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from .indicators import Bar

NY = ZoneInfo("America/New_York")
OPEN, CLOSE = time(9, 30), time(16, 0)


def ny_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(NY)


def regular_hours(bars: list[Bar]) -> list[Bar]:
    return [b for b in bars if OPEN <= ny_time(b.t).time() < CLOSE]


def minutes_since_open(dt: datetime) -> float:
    dt = dt.astimezone(NY)
    return (dt - dt.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60
