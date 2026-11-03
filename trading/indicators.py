"""Turn raw 5-minute OHLCV bars into the per-cycle MarketSnapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import MarketSnapshot


@dataclass
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def vwap_series(bars: list[Bar]) -> list[float]:
    """Session VWAP using typical price; resets when the date changes."""
    out: list[float] = []
    cum_pv = cum_v = 0.0
    day = None
    for b in bars:
        if b.timestamp.date() != day:
            day, cum_pv, cum_v = b.timestamp.date(), 0.0, 0.0
        typical = (b.high + b.low + b.close) / 3.0
        cum_pv += typical * b.volume
        cum_v += b.volume
        out.append(cum_pv / cum_v if cum_v else b.close)
    return out


def build_snapshot(
    ticker: str,
    bars: list[Bar],
    volume_lookback: int = 20,
    swing_lookback: int = 5,
) -> MarketSnapshot | None:
    """Snapshot for the latest bar, or ``None`` if there is not enough history.

    Only bars from the current session are used so overnight gaps don't leak in.
    """
    if not bars:
        return None
    session_day = bars[-1].timestamp.date()
    session = [b for b in bars if b.timestamp.date() == session_day]
    if len(session) < 2:
        return None

    closes = [b.close for b in session]
    e9 = ema_series(closes, 9)
    e21 = ema_series(closes, 21)
    vw = vwap_series(session)

    prior = session[:-1]
    vol_window = prior[-volume_lookback:]
    swing_window = prior[-swing_lookback:]
    last = session[-1]

    return MarketSnapshot(
        ticker=ticker,
        timestamp=last.timestamp,
        price=last.close,
        high=last.high,
        low=last.low,
        volume=last.volume,
        vwap=vw[-1],
        ema9=e9[-1],
        ema21=e21[-1],
        avg_volume=sum(b.volume for b in vol_window) / len(vol_window),
        prev_close=session[-2].close,
        prev_vwap=vw[-2],
        swing_low=min(b.low for b in swing_window + [last]),
        swing_high=max(b.high for b in swing_window + [last]),
    )
