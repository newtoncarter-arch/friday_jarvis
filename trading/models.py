"""Plain data types shared across the trading assistant."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum


class Action(str, Enum):
    BUY = "BUY"  # open long
    SELL = "SELL"  # close long
    SHORT = "SHORT"  # open short
    COVER = "COVER"  # close short
    HOLD = "HOLD"
    HALT = "HALT"  # flatten everything, stop for the day

    @property
    def opens(self) -> bool:
        return self in (Action.BUY, Action.SHORT)

    @property
    def closes(self) -> bool:
        return self in (Action.SELL, Action.COVER)


@dataclass
class MarketSnapshot:
    """One cycle's inputs for one ticker (5-minute bar close)."""

    ticker: str
    timestamp: datetime  # bar close time, timezone-aware
    price: float
    high: float
    low: float
    volume: float
    vwap: float
    ema9: float
    ema21: float
    avg_volume: float  # 20-bar average volume
    prev_close: float
    prev_vwap: float
    swing_low: float
    swing_high: float

    def missing_fields(self) -> list[str]:
        bad = []
        for name, value in asdict(self).items():
            if value is None:
                bad.append(name)
            elif isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                bad.append(name)
        return bad

    def conflicts(self) -> list[str]:
        issues = []
        if self.price <= 0 or self.vwap <= 0:
            issues.append("non-positive price or VWAP")
        if self.low > self.high:
            issues.append("bar low above bar high")
        elif not (self.low - 1e-9 <= self.price <= self.high + 1e-9):
            issues.append("price outside bar range")
        if self.volume < 0 or self.avg_volume < 0:
            issues.append("negative volume")
        if self.swing_low > self.swing_high:
            issues.append("swing low above swing high")
        return issues


@dataclass
class Position:
    ticker: str
    side: str  # "long" | "short"
    quantity: int
    entry: float
    stop_loss: float
    take_profit: float
    opened_at: datetime

    def unrealized(self, price: float) -> float:
        sign = 1 if self.side == "long" else -1
        return sign * (price - self.entry) * self.quantity


@dataclass
class Trade:
    """A completed round trip."""

    ticker: str
    side: str
    quantity: int
    entry: float
    exit: float
    opened_at: datetime
    closed_at: datetime
    exit_reason: str

    @property
    def pnl(self) -> float:
        sign = 1 if self.side == "long" else -1
        return round(sign * (self.exit - self.entry) * self.quantity, 2)


@dataclass
class AccountState:
    starting_equity: float
    realized_pnl: float = 0.0
    trades_today: int = 0  # entries taken today
    positions: dict[str, Position] = field(default_factory=dict)
    halted: bool = False
    halt_reason: str = ""

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        return sum(p.unrealized(prices.get(t, p.entry)) for t, p in self.positions.items())

    def day_pnl(self, prices: dict[str, float]) -> float:
        return self.realized_pnl + self.unrealized_pnl(prices)


@dataclass
class Decision:
    action: Action
    ticker: str = ""
    quantity: int = 0
    entry: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    confidence: int = 0
    reason: str = ""

    @classmethod
    def hold(cls, ticker: str, reason: str) -> "Decision":
        return cls(action=Action.HOLD, ticker=ticker, reason=reason)

    @classmethod
    def from_dict(cls, raw: dict) -> "Decision":
        return cls(
            action=Action(str(raw.get("action", "HOLD")).upper()),
            ticker=str(raw.get("ticker", "")).upper(),
            quantity=int(raw.get("quantity", 0) or 0),
            entry=float(raw.get("entry", 0) or 0),
            stop_loss=float(raw.get("stop_loss", 0) or 0),
            take_profit=float(raw.get("take_profit", 0) or 0),
            confidence=max(0, min(100, int(raw.get("confidence", 0) or 0))),
            reason=str(raw.get("reason", "")),
        )

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "ticker": self.ticker,
            "quantity": self.quantity,
            "entry": round(self.entry, 2),
            "stop_loss": round(self.stop_loss, 2),
            "take_profit": round(self.take_profit, 2),
            "confidence": self.confidence,
            "reason": self.reason,
        }
