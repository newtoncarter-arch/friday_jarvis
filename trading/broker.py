"""Order execution.

``PaperBroker`` simulates fills, bracket (stop/target) exits and P&L.
``LiveBroker`` is the interface a real broker adapter must implement; it can
only be selected when ``config.live`` is true *and* an adapter is supplied.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime

from .config import TradingConfig
from .models import AccountState, Action, Decision, MarketSnapshot, Position, Trade

log = logging.getLogger(__name__)


class Broker(ABC):
    account: AccountState
    last_prices: dict[str, float]

    @abstractmethod
    def execute(self, decision: Decision, now: datetime) -> list[Trade]:
        """Send an approved decision. Returns any round trips it closed."""

    @abstractmethod
    def on_bar(self, snapshot: MarketSnapshot) -> list[Trade]:
        """Update marks and process stop/target hits for the snapshot's ticker."""

    @abstractmethod
    def flatten_all(self, now: datetime, reason: str) -> list[Trade]:
        """Market-close every open position."""


class PaperBroker(Broker):
    """In-memory simulator. Fills at the snapshot price; brackets fill at their level.

    If a single bar touches both stop and target we assume the stop filled first.
    """

    def __init__(self, starting_equity: float):
        self.account = AccountState(starting_equity=starting_equity)
        self.last_prices: dict[str, float] = {}
        self.trades: list[Trade] = []

    def execute(self, decision: Decision, now: datetime) -> list[Trade]:
        a = decision.action
        if a in (Action.BUY, Action.SHORT):
            self.account.positions[decision.ticker] = Position(
                ticker=decision.ticker,
                side="long" if a == Action.BUY else "short",
                quantity=decision.quantity,
                entry=decision.entry,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit,
                opened_at=now,
            )
            self.account.trades_today += 1
            self.last_prices[decision.ticker] = decision.entry
            return []
        if a in (Action.SELL, Action.COVER):
            return [self._close(decision.ticker, decision.entry, now, "signal exit")]
        return []

    def on_bar(self, snapshot: MarketSnapshot) -> list[Trade]:
        self.last_prices[snapshot.ticker] = snapshot.price
        pos = self.account.positions.get(snapshot.ticker)
        if pos is None:
            return []
        if pos.side == "long":
            if snapshot.low <= pos.stop_loss:
                return [self._close(pos.ticker, pos.stop_loss, snapshot.timestamp, "stop-loss")]
            if snapshot.high >= pos.take_profit:
                return [self._close(pos.ticker, pos.take_profit, snapshot.timestamp, "take-profit")]
        else:
            if snapshot.high >= pos.stop_loss:
                return [self._close(pos.ticker, pos.stop_loss, snapshot.timestamp, "stop-loss")]
            if snapshot.low <= pos.take_profit:
                return [self._close(pos.ticker, pos.take_profit, snapshot.timestamp, "take-profit")]
        return []

    def flatten_all(self, now: datetime, reason: str) -> list[Trade]:
        closed = []
        for ticker in list(self.account.positions):
            price = self.last_prices.get(ticker, self.account.positions[ticker].entry)
            closed.append(self._close(ticker, price, now, reason))
        return closed

    def _close(self, ticker: str, price: float, now: datetime, reason: str) -> Trade:
        pos = self.account.positions.pop(ticker)
        trade = Trade(
            ticker=ticker,
            side=pos.side,
            quantity=pos.quantity,
            entry=pos.entry,
            exit=price,
            opened_at=pos.opened_at,
            closed_at=now,
            exit_reason=reason,
        )
        self.account.realized_pnl += trade.pnl
        self.trades.append(trade)
        return trade


class LiveBroker(Broker, ABC):
    """Subclass this to connect a real broker (Alpaca, IBKR, Tradier, ...).

    Requirements for an adapter:
    - submit every entry as a bracket/OCO order (entry + stop + target) in one request,
      so the stop exists at the broker before the entry can fill;
    - reconcile ``account``/``last_prices`` from the broker, not from local state;
    - ``flatten_all`` must cancel open orders and market-close every position.
    """


def make_broker(config: TradingConfig, live_broker: LiveBroker | None = None) -> Broker:
    if not config.live:
        if live_broker is not None:
            log.warning("LIVE is not true; ignoring the live broker and paper trading.")
        return PaperBroker(config.account_size)
    if live_broker is None:
        raise RuntimeError(
            "LIVE=true but no live broker adapter was provided. Refusing to start "
            "rather than silently paper trading."
        )
    log.warning("LIVE TRADING ENABLED via %s", config.broker)
    return live_broker
