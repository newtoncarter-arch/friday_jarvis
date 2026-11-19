"""Grok (xAI) trade reviewer. Your Grok bot's instructions live in grok_prompt.md."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import requests

from .strategy import Signal

log = logging.getLogger(__name__)
XAI_URL = "https://api.x.ai/v1/chat/completions"


@dataclass
class GrokVerdict:
    approved: bool
    confidence: int
    stop_loss: float | None
    take_profit: float | None
    reason: str


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start : end + 1]
    return json.loads(text)


class GrokAdvisor:
    def __init__(self, api_key: str, model: str, system_prompt: str, min_confidence: int = 65):
        if not api_key:
            raise ValueError("XAI_API_KEY is missing - add it to .env or set USE_GROK=false")
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt
        self.min_confidence = min_confidence

    def review(self, signal: Signal, context: dict) -> GrokVerdict:
        payload = {
            "proposed_trade": {
                "symbol": signal.symbol,
                "side": "LONG" if signal.side == "buy" else "SHORT",
                "entry": signal.entry,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "technical_reason": signal.reason,
            },
            "market": signal.snapshot,
            "context": context,
        }
        body = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": json.dumps(payload, default=str)},
            ],
        }
        try:
            r = requests.post(
                XAI_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
                timeout=60,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = _extract_json(content)
        except Exception as e:  # network / parse errors -> fail safe (no trade)
            log.warning("Grok review failed for %s: %s", signal.symbol, e)
            return GrokVerdict(False, 0, None, None, f"Grok error: {e}")

        conf = int(float(data.get("confidence", 0)))
        decision = str(data.get("decision", "REJECT")).upper()
        approved = decision == "APPROVE" and conf >= self.min_confidence
        return GrokVerdict(
            approved=approved,
            confidence=conf,
            stop_loss=_num(data.get("stop_loss")),
            take_profit=_num(data.get("take_profit")),
            reason=str(data.get("reason", ""))[:500],
        )


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None
