"""Hard risk rules, enforced in code.

Nothing — neither the rule-based strategy nor Claude — can place an order
without passing through ``RiskManager.review``. The model's output is treated
as a *proposal*; the rules below are not negotiable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import ET, TradingConfig
from .models import AccountState, Action, Decision, MarketSnapshot


@dataclass
class Review:
    decision: Decision  # what will actually be executed (may be HOLD/HALT)
    proposed: Decision  # what the decider asked for
    blocked: list[str] = field(default_factory=list)  # rules that rejected the proposal
    adjustments: list[str] = field(default_factory=list)  # e.g. quantity reduced

    @property
    def approved(self) -> bool:
        return not self.blocked


class RiskManager:
    def __init__(self, config: TradingConfig):
        self.config = config

    # ------------------------------------------------------------------
    # Session-level checks (run before asking any decider)
    # ------------------------------------------------------------------
    def must_halt(self, account: AccountState, prices: dict[str, float]) -> str | None:
        if account.halted:
            return account.halt_reason or "Trading halted for the day."
        loss = -account.day_pnl(prices)
        if loss >= self.config.max_daily_loss_dollars:
            return (
                f"Max daily loss hit (${loss:,.2f} >= "
                f"${self.config.max_daily_loss_dollars:,.2f}); closing all positions."
            )
        return None

    def must_flatten(self, now: datetime) -> bool:
        """True at/after the end of the trading window — no overnight holds."""
        return now.astimezone(ET).time() >= self.config.trade_end

    def entry_window_block(self, now: datetime, ticker: str) -> str | None:
        t = now.astimezone(ET)
        if t.weekday() >= 5:
            return "Market closed (weekend)."
        if t.time() < self.config.trade_start:
            return "Inside the first 15 minutes / before the trading window."
        if t.time() >= self.config.trade_end:
            return "Inside the last 15 minutes / after the trading window."
        window = timedelta(minutes=self.config.news_blackout_minutes)
        for event in self.config.news_events:
            if event.ticker and event.ticker != ticker:
                continue
            if abs(t - event.at) <= window:
                return f"Within {self.config.news_blackout_minutes} min of {event.name}."
        return None

    def data_problem(self, snapshot: MarketSnapshot | None, now: datetime, ticker: str) -> str | None:
        if snapshot is None:
            return f"No market data for {ticker}."
        missing = snapshot.missing_fields()
        if missing:
            return f"Missing data fields: {', '.join(missing)}."
        age = (now - snapshot.timestamp).total_seconds()
        if age > self.config.max_data_age_seconds:
            return f"Stale data ({int(age)}s old > {self.config.max_data_age_seconds}s)."
        if age < -5:
            return "Data timestamp is in the future; clock or feed problem."
        conflicts = snapshot.conflicts()
        if conflicts:
            return "Conflicting data: " + "; ".join(conflicts) + "."
        return None

    # ------------------------------------------------------------------
    # Per-proposal review
    # ------------------------------------------------------------------
    def max_quantity(self, entry: float, stop: float, account: AccountState) -> int:
        per_share = abs(entry - stop)
        if per_share <= 0 or entry <= 0:
            return 0
        by_risk = math.floor(self.config.risk_per_trade_dollars / per_share)
        # No leverage: notional of all open positions + this one <= equity.
        committed = sum(p.entry * p.quantity for p in account.positions.values())
        equity = account.starting_equity + account.realized_pnl
        by_cash = math.floor(max(0.0, equity - committed) / entry)
        return max(0, min(by_risk, by_cash))

    def review(
        self,
        proposed: Decision,
        snapshot: MarketSnapshot,
        account: AccountState,
        now: datetime,
    ) -> Review:
        cfg = self.config
        d = Decision(**{**proposed.__dict__})  # working copy
        review = Review(decision=d, proposed=proposed)

        def block(reason: str) -> Review:
            review.blocked.append(reason)
            review.decision = Decision.hold(
                snapshot.ticker, f"Blocked by risk rules: {reason}"
            )
            return review

        if d.action in (Action.HOLD, Action.HALT):
            d.ticker = d.ticker or snapshot.ticker
            return review

        if d.ticker != snapshot.ticker:
            return block(f"Decision ticker {d.ticker!r} does not match data ticker {snapshot.ticker!r}.")
        if d.ticker not in cfg.tickers:
            return block(f"{d.ticker} is not on the approved ticker list.")

        position = account.positions.get(d.ticker)

        # --- Exits are always allowed (they reduce risk) ---------------
        if d.action.closes:
            want = "long" if d.action == Action.SELL else "short"
            if position is None or position.side != want:
                return block(f"{d.action.value} with no open {want} position in {d.ticker}.")
            d.quantity = position.quantity
            d.entry = snapshot.price
            return review

        # --- Entries ----------------------------------------------------
        if d.action == Action.SHORT and not cfg.allow_short:
            return block("Short selling is disabled in config.")
        window = self.entry_window_block(now, d.ticker)
        if window:
            return block(window)
        if position is not None:
            # Covers both pyramiding and averaging down on a loser.
            if position.unrealized(snapshot.price) < 0:
                return block("Never average down on a losing trade.")
            return block(f"Already holding {d.ticker}; one position per ticker.")
        if len(account.positions) >= cfg.max_open_positions:
            return block(f"Max {cfg.max_open_positions} open positions reached.")
        if account.trades_today >= cfg.max_trades_per_day:
            return block(f"Max {cfg.max_trades_per_day} trades per day reached.")

        # Entry price is the live price, never a number the model invented.
        if d.entry and abs(d.entry - snapshot.price) / snapshot.price > 0.002:
            review.adjustments.append(
                f"Entry {d.entry:.2f} re-anchored to market {snapshot.price:.2f}."
            )
        d.entry = snapshot.price

        if not d.stop_loss or not d.take_profit:
            return block("Every entry needs a stop-loss and a profit target.")
        if d.action == Action.BUY and not (d.stop_loss < d.entry < d.take_profit):
            return block("Long needs stop < entry < target.")
        if d.action == Action.SHORT and not (d.take_profit < d.entry < d.stop_loss):
            return block("Short needs target < entry < stop.")

        risk = abs(d.entry - d.stop_loss)
        if risk / d.entry * 100 < cfg.min_stop_distance_pct:
            return block("Stop is too tight to survive normal noise.")
        reward = abs(d.take_profit - d.entry)
        rr = reward / risk
        if rr + 1e-9 < cfg.min_reward_risk:
            return block(f"Reward:risk {rr:.2f} below minimum {cfg.min_reward_risk}.")

        cap = self.max_quantity(d.entry, d.stop_loss, account)
        if cap <= 0:
            return block("Position size rounds to zero within the risk budget.")
        if d.quantity <= 0 or d.quantity > cap:
            if d.quantity > cap:
                review.adjustments.append(
                    f"Quantity {d.quantity} reduced to {cap} (max {cfg.risk_per_trade_pct}% risk)."
                )
            d.quantity = cap
        return review
