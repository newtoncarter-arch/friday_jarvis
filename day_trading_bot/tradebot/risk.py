"""Position sizing and daily guardrails."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class RiskDecision:
    allowed: bool
    qty: int = 0
    reason: str = ""


class RiskManager:
    def __init__(self, s):
        self.s = s

    def daily_loss_hit(self, equity: float, start_equity: float) -> bool:
        if start_equity <= 0:
            return False
        return (start_equity - equity) / start_equity * 100 >= self.s.max_daily_loss_pct

    def size(
        self,
        equity: float,
        buying_power: float,
        entry: float,
        stop: float,
        open_positions: int,
        trades_today: int,
    ) -> RiskDecision:
        if open_positions >= self.s.max_open_positions:
            return RiskDecision(False, reason=f"max open positions ({self.s.max_open_positions})")
        if trades_today >= self.s.max_trades_per_day:
            return RiskDecision(False, reason=f"max trades per day ({self.s.max_trades_per_day})")
        per_share_risk = abs(entry - stop)
        if per_share_risk <= 0 or entry <= 0:
            return RiskDecision(False, reason="invalid stop")
        risk_dollars = equity * self.s.risk_per_trade_pct / 100
        qty = math.floor(risk_dollars / per_share_risk)
        max_by_size = math.floor(equity * self.s.max_position_pct / 100 / entry)
        max_by_bp = math.floor(buying_power * 0.95 / entry)
        qty = min(qty, max_by_size, max_by_bp)
        if qty < 1:
            return RiskDecision(False, reason="position size < 1 share")
        return RiskDecision(True, qty, f"risking ${qty * per_share_risk:,.2f} ({qty} sh)")
