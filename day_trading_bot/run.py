#!/usr/bin/env python3
"""Grok Day Trading Bot - command line entry point.

  python run.py check                 # verify keys + connections
  python run.py backtest --demo       # backtest on fake data (no keys needed)
  python run.py backtest --days 30    # backtest on real Alpaca history
  python run.py paper --dry-run       # scan live market, log signals, place NO orders
  python run.py paper                 # trade the Alpaca paper account (fake money)
  python run.py live                  # real money (needs LIVE_TRADING + I_UNDERSTAND_THE_RISK)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from tradebot.config import Settings


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("logs/bot.log")],
    )


def make_grok(s: Settings):
    from tradebot.grok import GrokAdvisor

    return GrokAdvisor(s.xai_api_key, s.grok_model, s.grok_prompt_file.read_text(),
                       s.grok_min_confidence)


def cmd_check(s: Settings) -> int:
    ok = True
    print(f"Mode: {'LIVE' if s.live_trading else 'PAPER'}  ({s.trading_url})")
    print(f"Watchlist: {', '.join(s.watchlist)}")
    try:
        from tradebot.broker import AlpacaBroker

        b = AlpacaBroker(s.alpaca_key, s.alpaca_secret, s.trading_url, s.data_feed)
        a = b.account()
        print(f"[OK] Alpaca: equity ${a['equity']:,.2f}, buying power ${a['buying_power']:,.2f}")
        bars = b.bars(s.watchlist[0], s.timeframe)
        print(f"[OK] Market data: {len(bars)} bars for {s.watchlist[0]}")
    except Exception as e:
        ok = False
        print(f"[FAIL] Alpaca: {e}")
    if s.use_grok:
        try:
            import requests

            r = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {s.xai_api_key}"},
                json={"model": s.grok_model, "max_tokens": 5,
                      "messages": [{"role": "user", "content": "Reply OK"}]},
                timeout=30,
            )
            r.raise_for_status()
            print(f"[OK] Grok ({s.grok_model}) responded")
        except Exception as e:
            ok = False
            print(f"[FAIL] Grok: {e}")
    else:
        print("[--] Grok disabled (USE_GROK=false)")
    return 0 if ok else 1


def cmd_backtest(s: Settings, args) -> int:
    from tradebot.backtest import load_csv, run_backtest, synthetic_bars

    symbols = args.symbols.split(",") if args.symbols else s.watchlist
    if args.demo:
        data = {sym: synthetic_bars(sym, args.days) for sym in symbols}
    elif args.csv:
        data = {Path(p).stem.upper(): load_csv(Path(p)) for p in args.csv}
    else:
        from tradebot.broker import AlpacaBroker

        b = AlpacaBroker(s.alpaca_key, s.alpaca_secret, s.trading_url, s.data_feed)
        data = {sym: b.bars(sym, s.timeframe, lookback_days=int(args.days * 1.5)) for sym in symbols}
    grok = make_grok(s) if args.grok else None
    res = run_backtest(data, s, grok=grok, cash=args.cash)
    for t in res.pop("trade_list")[-args.show:]:
        print(f"  {t['opened'][:16]} {t['side']:4} {t['qty']:>4} {t['symbol']:<5} "
              f"{t['entry']:>9.2f} -> {t['exit']:>9.2f}  {t['pnl']:>+9.2f}  ({t['exit_reason']})")
    res.pop("equity_curve")
    print(json.dumps(res, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Grok-powered day trading bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    bt = sub.add_parser("backtest")
    bt.add_argument("--demo", action="store_true", help="use synthetic data (no API keys)")
    bt.add_argument("--csv", nargs="+", help="CSV files named SYMBOL.csv")
    bt.add_argument("--symbols", help="comma list (default: WATCHLIST)")
    bt.add_argument("--days", type=int, default=20)
    bt.add_argument("--cash", type=float, default=25_000)
    bt.add_argument("--grok", action="store_true", help="ask Grok on every signal (costs API $)")
    bt.add_argument("--show", type=int, default=15, help="print last N trades")
    for name in ("paper", "live"):
        sp = sub.add_parser(name)
        sp.add_argument("--dry-run", action="store_true", help="log signals but place no orders")
    args = p.parse_args()

    s = Settings()
    if args.cmd == "check":
        return cmd_check(s)
    if args.cmd == "backtest":
        return cmd_backtest(s, args)

    setup_logging()
    if args.cmd == "live" and not s.live_trading:
        print("Refusing to trade real money: set LIVE_TRADING=true AND I_UNDERSTAND_THE_RISK=true "
              "in .env. Run `python run.py paper` first.")
        return 2
    if args.cmd == "paper" and s.live_trading:
        print("LIVE_TRADING is on in .env - turn it off to use paper mode.")
        return 2
    if args.cmd == "live":
        if input("Type YES to trade REAL money: ").strip() != "YES":
            return 1

    from tradebot.bot import TradingBot

    try:
        TradingBot(s, dry_run=args.dry_run).run()
    except KeyboardInterrupt:
        print("\nStopped. Open positions (if any) are still protected by their bracket stops.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
