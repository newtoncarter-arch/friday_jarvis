"""Technical indicators implemented in plain Python (no pandas needed)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Bar:
    t: str  # ISO timestamp
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def atr(bars: list[Bar], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    if len(bars) <= period:
        return out
    trs = [bars[0].h - bars[0].l]
    for i in range(1, len(bars)):
        pc = bars[i - 1].c
        trs.append(max(bars[i].h - bars[i].l, abs(bars[i].h - pc), abs(bars[i].l - pc)))
    prev = sum(trs[1 : period + 1]) / period
    out[period] = prev
    for i in range(period + 1, len(bars)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def vwap(bars: list[Bar]) -> list[float]:
    """Session VWAP; resets whenever the calendar date changes."""
    out, pv, vol, day = [], 0.0, 0.0, None
    for b in bars:
        d = b.t[:10]
        if d != day:
            pv, vol, day = 0.0, 0.0, d
        typical = (b.h + b.l + b.c) / 3
        pv += typical * b.v
        vol += b.v
        out.append(pv / vol if vol else b.c)
    return out


def macd(closes: list[float], fast=12, slow=26, signal=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    valid = [x for x in line if x is not None]
    sig_valid = ema(valid, signal)
    sig: list[float | None] = [None] * (len(line) - len(valid)) + sig_valid
    hist = [m - s if m is not None and s is not None else None for m, s in zip(line, sig)]
    return line, sig, hist


def compute_all(bars: list[Bar]) -> dict[str, list]:
    closes = [b.c for b in bars]
    m_line, m_sig, m_hist = macd(closes)
    return {
        "ema9": ema(closes, 9),
        "ema21": ema(closes, 21),
        "rsi14": rsi(closes, 14),
        "atr14": atr(bars, 14),
        "vwap": vwap(bars),
        "macd": m_line,
        "macd_signal": m_sig,
        "macd_hist": m_hist,
        "vol_avg20": [
            sum(b.v for b in bars[max(0, i - 19) : i + 1]) / min(i + 1, 20)
            for i in range(len(bars))
        ],
    }
