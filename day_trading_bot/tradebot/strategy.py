"""Technical setup detection: EMA trend + VWAP + momentum + volume.

The strategy proposes trades; Grok then approves or rejects them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .indicators import Bar, compute_all

MIN_BARS = 35


@dataclass
class Signal:
    symbol: str
    side: str  # "buy" (long) or "sell" (short)
    entry: float
    stop_loss: float
    take_profit: float
    reason: str
    snapshot: dict


def _crossed_up(a, b, i) -> bool:
    return None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i - 1] <= b[i - 1] and a[i] > b[i]


def _crossed_down(a, b, i) -> bool:
    return None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i - 1] >= b[i - 1] and a[i] < b[i]


def find_signal(
    symbol: str,
    bars: list[Bar],
    atr_mult: float = 1.5,
    reward_risk: float = 2.0,
    allow_shorts: bool = False,
) -> Signal | None:
    if len(bars) < MIN_BARS:
        return None
    ind = compute_all(bars)
    i = len(bars) - 1
    last = bars[i]
    e9, e21, r, a, vw = ind["ema9"], ind["ema21"], ind["rsi14"], ind["atr14"], ind["vwap"]
    hist, vavg = ind["macd_hist"], ind["vol_avg20"]
    if None in (e9[i], e21[i], r[i], a[i], hist[i]) or a[i] <= 0:
        return None

    vol_ok = last.v >= 1.2 * vavg[i - 1] if vavg[i - 1] else False
    # Trigger: fresh EMA cross, or a pullback-to-EMA9 bounce within an existing trend
    long_trigger = _crossed_up(e9, e21, i) or (
        e9[i] > e21[i] and bars[i - 1].l <= e9[i - 1] and last.c > e9[i] and last.c > last.o
    )
    short_trigger = _crossed_down(e9, e21, i) or (
        e9[i] < e21[i] and bars[i - 1].h >= e9[i - 1] and last.c < e9[i] and last.c < last.o
    )

    side = None
    if long_trigger and last.c > vw[i] and 50 < r[i] < 75 and hist[i] > 0 and vol_ok:
        side = "buy"
    elif (
        allow_shorts
        and short_trigger
        and last.c < vw[i]
        and 25 < r[i] < 50
        and hist[i] < 0
        and vol_ok
    ):
        side = "sell"
    if side is None:
        return None

    risk = a[i] * atr_mult
    entry = last.c
    if side == "buy":
        stop, target = entry - risk, entry + risk * reward_risk
    else:
        stop, target = entry + risk, entry - risk * reward_risk

    reason = (
        f"{'Long' if side == 'buy' else 'Short'}: EMA9/21 trend, price "
        f"{'above' if side == 'buy' else 'below'} VWAP, RSI {r[i]:.0f}, "
        f"MACD hist {hist[i]:+.3f}, volume {last.v / vavg[i - 1]:.1f}x avg"
    )
    snapshot = {
        "symbol": symbol,
        "last_15_candles": [
            {"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": int(b.v)} for b in bars[-15:]
        ],
        "indicators_now": {
            "ema9": round(e9[i], 4),
            "ema21": round(e21[i], 4),
            "rsi14": round(r[i], 2),
            "vwap": round(vw[i], 4),
            "atr14": round(a[i], 4),
            "macd_hist": round(hist[i], 4),
            "volume_vs_avg": round(last.v / vavg[i - 1], 2),
            "pct_from_vwap": round((last.c - vw[i]) / vw[i] * 100, 3),
        },
    }
    return Signal(symbol, side, round(entry, 2), round(stop, 2), round(target, 2), reason, snapshot)
