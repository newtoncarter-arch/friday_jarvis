"""Synthetic 5-minute bars for demos and tests. Not market data."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from ..config import ET
from ..indicators import Bar

START_PRICES = {"SPY": 580.0, "QQQ": 505.0, "AAPL": 228.0}


def generate_day(
    tickers, day: date = date(2026, 11, 3), seed: int = 7
) -> list[tuple[str, Bar]]:
    """A full 9:35–16:00 session per ticker: random walk with regime swings
    so VWAP gets crossed a few times."""
    rng = random.Random(seed)
    rows = []
    for ticker in tickers:
        price = START_PRICES.get(ticker, 100.0 + rng.random() * 100)
        drift = 0.0
        t = datetime(day.year, day.month, day.day, 9, 35, tzinfo=ET)
        end = datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET)
        while t <= end:
            if rng.random() < 0.08:  # regime change
                drift = rng.choice([-1, 1]) * rng.uniform(0.0004, 0.0012)
            step = price * (drift + rng.gauss(0, 0.0012))
            open_ = price
            close = max(1.0, price + step)
            wick = price * abs(rng.gauss(0, 0.0007))
            high = max(open_, close) + wick
            low = min(open_, close) - wick
            base = 1_000_000 if ticker in ("SPY", "QQQ") else 400_000
            surge = 1.8 if abs(step) > price * 0.0015 else 1.0
            volume = base * surge * rng.uniform(0.6, 1.4)
            rows.append((ticker, Bar(t, round(open_, 2), round(high, 2), round(low, 2),
                                     round(close, 2), round(volume))))
            price = close
            t += timedelta(minutes=5)
    rows.sort(key=lambda x: (x[1].timestamp, x[0]))
    return rows
