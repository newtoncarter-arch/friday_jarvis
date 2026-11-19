"""Live / paper trading loop."""

from __future__ import annotations

import logging
import time
from .broker import AlpacaBroker
from .config import Settings
from .grok import GrokAdvisor
from .journal import Journal
from .risk import RiskManager
from .sessions import minutes_since_open, regular_hours
from .strategy import find_signal

log = logging.getLogger("tradebot")


class TradingBot:
    def __init__(self, s: Settings, dry_run: bool = False):
        self.s = s
        self.dry_run = dry_run
        self.broker = AlpacaBroker(s.alpaca_key, s.alpaca_secret, s.trading_url, s.data_feed)
        self.risk = RiskManager(s)
        self.journal = Journal(s.journal_file)
        self.grok = (
            GrokAdvisor(
                s.xai_api_key,
                s.grok_model,
                s.grok_prompt_file.read_text(),
                s.grok_min_confidence,
            )
            if s.use_grok
            else None
        )
        self.day: str | None = None
        self.trades_today = 0
        self.today_events: list[str] = []
        self.reviewed: set[tuple[str, str]] = set()  # (symbol, bar time) already sent to Grok
        self.halted = False
        self.flattened = False

    # ------------------------------------------------------------------
    def run(self) -> None:
        mode = "LIVE MONEY" if self.s.live_trading else "PAPER"
        log.info("Starting bot | mode=%s | grok=%s | dry_run=%s | watchlist=%s",
                 mode, self.s.grok_model if self.grok else "off", self.dry_run,
                 ",".join(self.s.watchlist))
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Tick failed; continuing")
            time.sleep(self.s.loop_seconds)

    def tick(self) -> None:
        clock = self.broker.clock()
        if not clock["is_open"]:
            log.info("Market closed. Next open %s", clock["next_open"])
            time.sleep(max(0, min(1800, (clock["next_open"] - clock["now"]).total_seconds() - 60)))
            return

        today = clock["now"].date().isoformat()
        if today != self.day:
            self._new_day(today)

        acct = self.broker.account()
        mins_to_close = (clock["next_close"] - clock["now"]).total_seconds() / 60

        # 1) End of day: day traders don't hold overnight
        if mins_to_close <= self.s.flatten_minutes_before_close:
            if not self.flattened:
                log.info("%.0f min to close -> flattening all positions", mins_to_close)
                if not self.dry_run:
                    self.broker.flatten_all()
                self.journal.log(event="flatten_eod", detail=f"equity={acct['equity']}")
                self.flattened = True
            return

        # 2) Daily loss circuit breaker
        if not self.halted and self.risk.daily_loss_hit(acct["equity"], acct["last_equity"]):
            log.warning("Daily loss limit hit (equity %.2f vs start %.2f). Halting for today.",
                        acct["equity"], acct["last_equity"])
            if not self.dry_run:
                self.broker.flatten_all()
            self.journal.log(event="halt_daily_loss", detail=f"equity={acct['equity']}")
            self.halted = True
        if self.halted:
            return

        if minutes_since_open(clock["now"]) < self.s.skip_first_minutes:
            log.info("Waiting out the opening volatility")
            return

        # PDT rule: under $25k, US brokers limit you to 3 day trades per 5 days
        if acct["equity"] < 25_000 and acct["daytrade_count"] >= 3:
            log.info("PDT limit reached (equity < $25k, %d day trades). No new entries.",
                     acct["daytrade_count"])
            return

        positions = self.broker.positions()
        busy = set(positions) | self.broker.open_order_symbols()

        # 3) Scan watchlist
        for sym in self.s.watchlist:
            if sym in busy:
                continue
            bars = regular_hours(self.broker.bars(sym, self.s.timeframe))
            sig = find_signal(sym, bars, self.s.atr_stop_mult, self.s.reward_risk,
                              self.s.allow_shorts)
            if not sig:
                continue
            key = (sym, bars[-1].t)
            if key in self.reviewed:
                continue
            self.reviewed.add(key)
            log.info("Signal %s %s @ %.2f stop %.2f target %.2f | %s",
                     sig.side.upper(), sym, sig.entry, sig.stop_loss, sig.take_profit, sig.reason)
            self.journal.log(symbol=sym, side=sig.side, entry=sig.entry, stop=sig.stop_loss,
                             target=sig.take_profit, event="signal", detail=sig.reason)

            stop, target = sig.stop_loss, sig.take_profit
            if self.grok:
                verdict = self.grok.review(sig, {
                    "account_equity": acct["equity"],
                    "pnl_today": round(acct["equity"] - acct["last_equity"], 2),
                    "open_positions": positions,
                    "trades_today": self.trades_today,
                    "today_events": self.today_events[-10:],
                    "minutes_to_close": round(mins_to_close),
                })
                log.info("Grok %s (%d%%): %s", "APPROVED" if verdict.approved else "REJECTED",
                         verdict.confidence, verdict.reason)
                self.journal.log(symbol=sym, side=sig.side, event="grok_" + (
                    "approve" if verdict.approved else "reject"),
                    grok_confidence=verdict.confidence, detail=verdict.reason)
                if not verdict.approved:
                    continue
                stop, target = self._accept_grok_levels(sig, verdict, stop, target)

            decision = self.risk.size(acct["equity"], acct["buying_power"], sig.entry, stop,
                                      len(busy), self.trades_today)
            if not decision.allowed:
                log.info("Risk blocked %s: %s", sym, decision.reason)
                continue

            log.info(">>> %s %d %s | stop %.2f target %.2f | %s", sig.side.upper(),
                     decision.qty, sym, stop, target, decision.reason)
            if not self.dry_run:
                self.broker.bracket_order(sym, sig.side, decision.qty, stop, target)
            self.journal.log(symbol=sym, side=sig.side, entry=sig.entry, stop=stop, target=target,
                             qty=decision.qty, event="order_dry_run" if self.dry_run else "order",
                             detail=decision.reason)
            self.trades_today += 1
            self.today_events.append(f"{sig.side} {decision.qty} {sym} @ {sig.entry}")
            busy.add(sym)

    # ------------------------------------------------------------------
    @staticmethod
    def _accept_grok_levels(sig, verdict, stop, target):
        """Use Grok's stop/target only if they are on the correct side of entry."""
        if sig.side == "buy":
            if verdict.stop_loss and stop <= verdict.stop_loss < sig.entry:
                stop = verdict.stop_loss
            if verdict.take_profit and verdict.take_profit > sig.entry:
                target = verdict.take_profit
        else:
            if verdict.stop_loss and sig.entry < verdict.stop_loss <= stop:
                stop = verdict.stop_loss
            if verdict.take_profit and verdict.take_profit < sig.entry:
                target = verdict.take_profit
        return round(stop, 2), round(target, 2)

    def _new_day(self, today: str) -> None:
        log.info("=== New trading day %s ===", today)
        self.day, self.trades_today, self.today_events = today, 0, []
        self.reviewed.clear()
        self.halted = self.flattened = False

