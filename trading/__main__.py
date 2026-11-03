"""Command line entry point.

    python -m trading prompt   [--config cfg.json]          # print the rendered system prompt
    python -m trading demo     [--config cfg.json] [--seed N]  # replay a synthetic day (paper)
    python -m trading replay   --csv bars.csv [--config cfg.json]
    python -m trading summary  trading_logs/2026-11-03.jsonl [--config cfg.json]

CSV format (5-minute bars, any number of tickers/days, timestamps = bar close):
    timestamp,ticker,open,high,low,close,volume
    2026-11-03T09:35:00-05:00,SPY,580.10,580.55,579.90,580.40,1250000
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .config import ET, TradingConfig
from .engine import TradingAssistant
from .indicators import Bar, build_snapshot
from .journal import format_summary, load_records, summarize
from .prompts import build_system_prompt


def read_bars(path: str | Path) -> list[tuple[str, Bar]]:
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            ts = datetime.fromisoformat(r["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ET)
            rows.append(
                (
                    r["ticker"].strip().upper(),
                    Bar(ts, float(r["open"]), float(r["high"]), float(r["low"]),
                        float(r["close"]), float(r["volume"])),
                )
            )
    rows.sort(key=lambda x: (x[1].timestamp, x[0]))
    return rows


def replay(config: TradingConfig, rows: list[tuple[str, Bar]], verbose: bool = True) -> list[dict]:
    """Feed bars through the assistant in time order; returns one summary per session."""
    if config.live:
        raise SystemExit("Replay is a simulation and never runs with LIVE=true.")
    assistant = TradingAssistant(config)
    history: dict[str, list[Bar]] = defaultdict(list)
    summaries = []
    current_day = None

    for ticker, bar in rows:
        if ticker not in config.tickers:
            continue
        day = bar.timestamp.astimezone(ET).date()
        if current_day is not None and day != current_day:
            summaries.append(_close_day(assistant, last_ts, verbose))
        current_day, last_ts = day, bar.timestamp

        history[ticker].append(bar)
        snap = build_snapshot(ticker, history[ticker], config.volume_lookback, config.swing_lookback)
        if snap is None:
            continue
        was_halted = assistant.account.halted
        decision = assistant.on_snapshot(snap, now=bar.timestamp)
        if verbose and decision.action.value != "HOLD" and not was_halted:
            print(f"{bar.timestamp.astimezone(ET):%Y-%m-%d %H:%M} {json.dumps(decision.to_dict())}")

    if current_day is not None:
        summaries.append(_close_day(assistant, last_ts, verbose))
    return summaries


def _close_day(assistant: TradingAssistant, ts: datetime, verbose: bool) -> dict:
    summary = assistant.end_of_day(ts)
    if verbose:
        print(format_summary(summary))
        print()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trading", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="JSON config file (env TRADING_* overrides it)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prompt")
    demo = sub.add_parser("demo")
    demo.add_argument("--seed", type=int, default=7)
    rp = sub.add_parser("replay")
    rp.add_argument("--csv", required=True)
    sm = sub.add_parser("summary")
    sm.add_argument("log")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    config = TradingConfig.load(args.config)

    if args.cmd == "prompt":
        print(build_system_prompt(config))
    elif args.cmd == "demo":
        from .examples.synthetic import generate_day

        print(f"[{config.mode}] synthetic replay, decider={config.decider}, tickers={','.join(config.tickers)}")
        replay(config, generate_day(config.tickers, seed=args.seed))
    elif args.cmd == "replay":
        print(f"[{config.mode}] replaying {args.csv}, decider={config.decider}")
        replay(config, read_bars(args.csv))
    elif args.cmd == "summary":
        print(format_summary(summarize(load_records(args.log), config)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
