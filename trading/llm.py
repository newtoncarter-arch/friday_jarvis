"""Claude-backed decider.

Claude only *proposes* a decision. The output is constrained to the decision
JSON schema with structured outputs, and anything it returns still goes through
``RiskManager.review``. Any API failure, refusal or malformed output degrades
to HOLD — never to a trade.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from .config import ET, TradingConfig
from .models import AccountState, Decision, MarketSnapshot
from .prompts import build_system_prompt

log = logging.getLogger(__name__)

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "SHORT", "COVER", "HOLD", "HALT"]},
        "ticker": {"type": "string"},
        "quantity": {"type": "integer"},
        "entry": {"type": "number"},
        "stop_loss": {"type": "number"},
        "take_profit": {"type": "number"},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": [
        "action", "ticker", "quantity", "entry", "stop_loss", "take_profit", "confidence", "reason",
    ],
    "additionalProperties": False,
}


def build_cycle_input(
    snapshot: MarketSnapshot,
    account: AccountState,
    now: datetime,
    blackout: str | None,
) -> dict:
    s = snapshot
    prices = {s.ticker: s.price}
    return {
        "now_et": now.astimezone(ET).isoformat(timespec="seconds"),
        "market": {
            "ticker": s.ticker,
            "bar_close_time": s.timestamp.astimezone(ET).isoformat(timespec="seconds"),
            "price": s.price,
            "high": s.high,
            "low": s.low,
            "volume": s.volume,
            "avg_volume_20": round(s.avg_volume, 2),
            "vwap": round(s.vwap, 4),
            "prev_close": s.prev_close,
            "prev_vwap": round(s.prev_vwap, 4),
            "ema9": round(s.ema9, 4),
            "ema21": round(s.ema21, 4),
            "swing_low": s.swing_low,
            "swing_high": s.swing_high,
        },
        "open_positions": [
            {
                "ticker": p.ticker,
                "side": p.side,
                "quantity": p.quantity,
                "entry": p.entry,
                "stop_loss": p.stop_loss,
                "take_profit": p.take_profit,
            }
            for p in account.positions.values()
        ],
        "account": {
            "starting_equity": account.starting_equity,
            "realized_pnl": round(account.realized_pnl, 2),
            "unrealized_pnl": round(account.unrealized_pnl(prices), 2),
            "trades_today": account.trades_today,
        },
        "news_blackout": blackout,
    }


class ClaudeDecider:
    def __init__(self, config: TradingConfig, client=None):
        self.config = config
        self.system_prompt = build_system_prompt(config)
        if client is None:
            import anthropic  # imported lazily so the rules-only mode has no dependency

            client = anthropic.Anthropic()
        self.client = client

    def decide(
        self,
        snapshot: MarketSnapshot,
        account: AccountState,
        now: datetime,
        blackout: str | None = None,
    ) -> Decision:
        payload = build_cycle_input(snapshot, account, now, blackout)
        try:
            response = self.client.beta.messages.create(
                model=self.config.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=[
                    {
                        "type": "text",
                        "text": self.system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config={
                    "effort": self.config.effort,
                    "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
                },
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
        except Exception as exc:  # network, rate limit, auth, ... -> never trade blind
            log.warning("Claude call failed: %s", exc)
            return Decision.hold(snapshot.ticker, f"Model unavailable ({type(exc).__name__}); holding.")

        if response.stop_reason == "refusal":
            return Decision.hold(snapshot.ticker, "Model declined the request; holding.")
        if response.stop_reason == "max_tokens":
            return Decision.hold(snapshot.ticker, "Model output was truncated; holding.")

        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        try:
            return Decision.from_dict(json.loads(text))
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("Unparseable model output: %r (%s)", text[:200], exc)
            return Decision.hold(snapshot.ticker, "Model returned malformed JSON; holding.")
