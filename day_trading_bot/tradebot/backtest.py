"""Replay historical bars through the same strategy + risk rules."""

from __future__ import annotations

import csv
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .broker import SimBroker
from .indicators import Bar
from .risk import RiskManager
from .sessions import minutes_since_open, ny_time, regular_hours
from .strategy import find_signal


def load_csv(path: Path) -> list[Bar]:
    """CSV with columns: timestamp,open,high,low,close,volume (timestamp ISO-8601, UTC)."""
    with path.open() as f:
        return [
            Bar(r["timestamp"], float(r["open"]), float(r["high"]), float(r["low"]),
                float(r["close"]), float(r["volume"]))
            for r in csv.DictReader(f)
        ]


def synthetic_bars(symbol: str, days: int = 20, seed: int | None = None) -> list[Bar]:
    """Random-walk 5-minute bars with intraday trends - for demoing without API keys."""
    rng = random.Random(seed if seed is not None else sum(map(ord, symbol)))
    price, out = 100 + rng.random() * 300, []
    day = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)  # 9:30 ET (EST)
    made = 0
    while made < days:
        if day.weekday() < 5:
            drift = rng.gauss(0, 0.0006)
            for k in range(78):  # 78 x 5min = 6.5h
                if k % 20 == 0:
                    drift = rng.gauss(0, 0.0006)
                t = day + timedelta(minutes=5 * k)
                o = price
                c = o * (1 + drift + rng.gauss(0, 0.0025))
                h = max(o, c) * (1 + abs(rng.gauss(0, 0.001)))
                l = min(o, c) * (1 - abs(rng.gauss(0, 0.001)))  # noqa: E741
                v = abs(rng.gauss(1, 0.4)) * 50_000 * (2 if k < 6 or k > 70 else 1)
                v *= 1 + 25 * abs(c - o) / o
                out.append(Bar(t.isoformat().replace("+00:00", "Z"), o, h, l, c, v))
                price = c
            made += 1
        day += timedelta(days=1)
    return out


def run_backtest(data: dict[str, list[Bar]], settings, grok=None, cash: float = 25_000) -> dict:
    risk = RiskManager(settings)
    sim = SimBroker(cash)
    data = {s: regular_hours(b) for s, b in data.items()}
    # Group every bar by timestamp so all symbols advance together
    timeline: dict[str, dict[str, int]] = defaultdict(dict)
    for sym, bars in data.items():
        for i, b in enumerate(bars):
            timeline[b.t][sym] = i
    times = sorted(timeline)
    day, start_eq, trades_today, halted = None, cash, 0, False
    last_px: dict[str, float] = {}
    equity_curve = []
    grok_calls = 0

    for ti, t in enumerate(times):
        dt = ny_time(t)
        if dt.date() != day:
            day, trades_today, halted = dt.date(), 0, False
            start_eq = sim.equity(last_px)
        is_last_bar_of_day = ti + 1 == len(times) or ny_time(times[ti + 1]).date() != day

        for sym, i in timeline[t].items():
            bar = data[sym][i]
            sim.on_bar(sym, bar)
            last_px[sym] = bar.c

        eq = sim.equity(last_px)
        if not halted and risk.daily_loss_hit(eq, start_eq):
            halted = True
            for sym in list(sim.pos):
                sim.close(sym, last_px[sym], t, "daily_loss")

        mins_left = (dt.replace(hour=16, minute=0) - dt).total_seconds() / 60 - 5
        if is_last_bar_of_day or mins_left <= settings.flatten_minutes_before_close:
            for sym in list(sim.pos):
                sim.close(sym, last_px[sym], t, "eod")
            equity_curve.append((str(day), round(sim.equity(last_px), 2))) if is_last_bar_of_day else None
            continue
        if halted or minutes_since_open(dt) < settings.skip_first_minutes:
            continue

        for sym, i in timeline[t].items():
            if sym in sim.pos:
                continue
            bars = data[sym][: i + 1]
            day_start = next(k for k in range(i, -1, -1) if k == 0 or ny_time(bars[k - 1].t).date() != day)
            sig = find_signal(sym, bars[max(0, day_start - 78):], settings.atr_stop_mult,
                              settings.reward_risk, settings.allow_shorts)
            if not sig:
                continue
            stop, target = sig.stop_loss, sig.take_profit
            if grok:
                grok_calls += 1
                v = grok.review(sig, {"backtest": True, "trades_today": trades_today})
                if not v.approved:
                    continue
            d = risk.size(eq, sim.cash, sig.entry, stop, len(sim.pos), trades_today)
            if d.allowed:
                sim.open(sym, sig.side, d.qty, sig.entry, stop, target, t)
                trades_today += 1

    trades = sim.closed
    wins = [x for x in trades if x["pnl"] > 0]
    losses = [x for x in trades if x["pnl"] <= 0]
    final = sim.equity(last_px)
    peak, max_dd = cash, 0.0
    for _, e in equity_curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)
    gross_win = sum(x["pnl"] for x in wins)
    gross_loss = -sum(x["pnl"] for x in losses)
    return {
        "start_equity": cash,
        "final_equity": round(final, 2),
        "return_pct": round((final - cash) / cash * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else math.inf,
        "max_drawdown_pct": round(max_dd, 2),
        "grok_calls": grok_calls,
        "trade_list": trades,
        "equity_curve": equity_curve,
    }
