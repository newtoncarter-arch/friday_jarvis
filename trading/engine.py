"""The per-cycle loop: mark -> safety checks -> decide -> risk review -> execute -> log."""

from __future__ import annotations

import logging
from datetime import datetime

from . import strategy
from .broker import Broker, LiveBroker, make_broker
from .config import ET, TradingConfig
from .journal import Journal, summarize
from .models import Action, Decision, MarketSnapshot
from .risk import RiskManager

log = logging.getLogger(__name__)


class TradingAssistant:
    def __init__(
        self,
        config: TradingConfig,
        broker: Broker | None = None,
        live_broker: LiveBroker | None = None,
        claude=None,
        journal: Journal | None = None,
    ):
        config.validate()
        self.config = config
        self.broker = broker or make_broker(config, live_broker)
        self.risk = RiskManager(config)
        self.journal = journal or Journal(config)
        self.claude = claude
        if config.decider in ("claude", "consensus") and self.claude is None:
            from .llm import ClaudeDecider

            self.claude = ClaudeDecider(config)

    @property
    def account(self):
        return self.broker.account

    # ------------------------------------------------------------------
    def on_snapshot(self, snapshot: MarketSnapshot | None, now: datetime | None = None, ticker: str = "") -> Decision:
        """Run one decision cycle for one ticker and return what was executed."""
        now = now or datetime.now(ET)
        ticker = (snapshot.ticker if snapshot else ticker).upper()
        self._roll_day(now)

        if self.account.halted:  # already logged when the halt happened
            return Decision(Action.HALT, ticker, reason=self.account.halt_reason)

        # 1. Data hygiene first: bad data never moves marks or triggers trades.
        problem = self.risk.data_problem(snapshot, now, ticker)
        exited_this_bar = False
        if problem is None:
            for trade in self.broker.on_bar(snapshot):
                self.journal.trade_closed(trade)
                exited_this_bar = True

        # 2. Max daily loss -> close everything and stop.
        halt = self.risk.must_halt(self.account, self.broker.last_prices)
        if halt:
            return self._halt(now, ticker, halt)

        # 3. End of window -> close everything, no overnight holds.
        if self.risk.must_flatten(now):
            if self.account.positions:
                return self._halt(now, ticker, "End of trading window; flattening all positions.")
            self.account.halted = True
            self.account.halt_reason = "Trading window closed for the day."
            return self._log(now, ticker, "system", Decision(Action.HALT, ticker, reason=self.account.halt_reason))

        if problem:
            return self._log(now, ticker, "system", Decision.hold(ticker, problem))

        if exited_this_bar:
            return self._log(now, ticker, "system",
                             Decision.hold(ticker, "Bracket exit filled on this bar; no same-bar re-entry."))

        # 4. Skip the decider entirely when no entry is possible and nothing is open here.
        blackout = self.risk.entry_window_block(now, ticker)
        if blackout and ticker not in self.account.positions:
            return self._log(now, ticker, "system", Decision.hold(ticker, blackout))

        # 5. Decide, then review against the hard rules.
        proposed, decider = self._propose(snapshot, now, blackout)
        if proposed.action == Action.HALT:
            return self._halt(now, ticker, f"Decider requested HALT: {proposed.reason}")

        review = self.risk.review(proposed, snapshot, self.account, now)
        executed = review.decision
        if executed.action != Action.HOLD:
            for trade in self.broker.execute(executed, now):
                self.journal.trade_closed(trade)
        self.journal.decision(now, ticker, decider, proposed, executed, review.blocked, review.adjustments)
        return executed

    def _roll_day(self, now: datetime) -> None:
        """Reset daily counters on the first cycle of a new session."""
        today = now.astimezone(ET).date()
        if getattr(self, "_session_date", None) == today:
            return
        if getattr(self, "_session_date", None) is not None:
            a = self.account
            if a.positions:  # should be impossible: we flatten before the close
                for trade in self.broker.flatten_all(now, "stale overnight position"):
                    self.journal.trade_closed(trade)
            a.starting_equity += a.realized_pnl
            a.realized_pnl = 0.0
            a.trades_today = 0
            a.halted = False
            a.halt_reason = ""
        self._session_date = today

    def _propose(self, snapshot: MarketSnapshot, now: datetime, blackout: str | None) -> tuple[Decision, str]:
        mode = self.config.decider
        rules = strategy.evaluate(snapshot, self.account, self.config)
        if mode == "rules":
            return rules, "rules"
        model = self.claude.decide(snapshot, self.account, now, blackout)
        if mode == "claude":
            return model, "claude"
        # consensus: trade only when both agree on the direction; use the
        # deterministic levels so sizing is reproducible.
        if model.action == Action.HALT:
            return model, "consensus"
        if rules.action.opens and model.action == rules.action:
            rules.confidence = min(rules.confidence, model.confidence)
            rules.reason = f"{rules.reason} (Claude agrees: {model.reason})"
            return rules, "consensus"
        if model.action.closes:
            return model, "consensus"
        if rules.action.opens or model.action.opens:
            return (
                Decision.hold(
                    snapshot.ticker,
                    f"Rules said {rules.action.value}, Claude said {model.action.value}; no consensus.",
                ),
                "consensus",
            )
        return rules, "consensus"

    def _halt(self, now: datetime, ticker: str, reason: str) -> Decision:
        for trade in self.broker.flatten_all(now, reason):
            self.journal.trade_closed(trade)
        self.account.halted = True
        self.account.halt_reason = reason
        decision = Decision(Action.HALT, ticker, reason=reason)
        return self._log(now, ticker, "system", decision)

    def _log(self, now: datetime, ticker: str, decider: str, decision: Decision) -> Decision:
        self.journal.decision(now, ticker, decider, decision, decision, [], [])
        return decision

    # ------------------------------------------------------------------
    def end_of_day(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(ET)
        if self.account.positions:
            for trade in self.broker.flatten_all(now, "end-of-day safety flatten"):
                self.journal.trade_closed(trade)
        today = now.astimezone(ET).date().isoformat()
        records = [r for r in self.journal.records if r["time"][:10] == today]
        summary = summarize(records, self.config)
        self.journal.note(now, "end_of_day_summary", summary=summary)
        return summary
