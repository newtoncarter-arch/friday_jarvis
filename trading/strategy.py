"""Deterministic implementation of the default setup.

Long: VWAP reclaim (prev close <= prev VWAP, close > VWAP), volume above its
20-bar average, 9 EMA above 21 EMA. Stop below the recent swing low.
Short: the mirror image. Take-profit is ``target_r_multiple`` R away.

This module only *proposes* entries. Every proposal still goes through
``RiskManager`` before anything reaches a broker.
"""

from __future__ import annotations

from .config import TradingConfig
from .models import AccountState, Action, Decision, MarketSnapshot

STOP_BUFFER_PCT = 0.0005  # place the stop 0.05% beyond the swing point


def evaluate(snapshot: MarketSnapshot, account: AccountState, config: TradingConfig) -> Decision:
    s = snapshot
    if s.ticker in account.positions:
        return Decision.hold(s.ticker, "Already holding a position; letting stop/target manage it.")

    volume_ok = s.volume > s.avg_volume
    long_reclaim = s.prev_close <= s.prev_vwap and s.price > s.vwap
    short_reclaim = s.prev_close >= s.prev_vwap and s.price < s.vwap

    if long_reclaim and volume_ok and s.ema9 > s.ema21:
        stop = s.swing_low * (1 - STOP_BUFFER_PCT)
        risk = s.price - stop
        if risk <= 0:
            return Decision.hold(s.ticker, "Long setup but swing low is not below price.")
        return Decision(
            action=Action.BUY,
            ticker=s.ticker,
            entry=s.price,
            stop_loss=stop,
            take_profit=s.price + config.target_r_multiple * risk,
            confidence=_confidence(s, long=True),
            reason=(
                "Price reclaimed VWAP with volume above its 20-bar average and 9 EMA above "
                "21 EMA; stop below recent swing low."
            ),
        )

    if config.allow_short and short_reclaim and volume_ok and s.ema9 < s.ema21:
        stop = s.swing_high * (1 + STOP_BUFFER_PCT)
        risk = stop - s.price
        if risk <= 0:
            return Decision.hold(s.ticker, "Short setup but swing high is not above price.")
        return Decision(
            action=Action.SHORT,
            ticker=s.ticker,
            entry=s.price,
            stop_loss=stop,
            take_profit=s.price - config.target_r_multiple * risk,
            confidence=_confidence(s, long=False),
            reason=(
                "Price lost VWAP with volume above its 20-bar average and 9 EMA below "
                "21 EMA; stop above recent swing high."
            ),
        )

    return Decision.hold(s.ticker, _why_not(s, volume_ok, long_reclaim, short_reclaim))


def _confidence(s: MarketSnapshot, long: bool) -> int:
    vol_ratio = s.volume / s.avg_volume if s.avg_volume else 1.0
    ema_gap = abs(s.ema9 - s.ema21) / s.price * 100
    score = 55 + min(25, (vol_ratio - 1) * 25) + min(20, ema_gap * 40)
    return int(max(0, min(100, score)))


def _why_not(s: MarketSnapshot, volume_ok: bool, long_reclaim: bool, short_reclaim: bool) -> str:
    if not (long_reclaim or short_reclaim):
        return "No VWAP reclaim or loss on this bar."
    if not volume_ok:
        return "VWAP cross without volume above its 20-bar average."
    return "VWAP cross not confirmed by 9/21 EMA alignment."
