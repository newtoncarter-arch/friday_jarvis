"""Settings loaded from environment variables / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass
class Settings:
    # Grok
    xai_api_key: str = field(default_factory=lambda: os.getenv("XAI_API_KEY", ""))
    grok_model: str = field(default_factory=lambda: os.getenv("GROK_MODEL", "grok-4"))
    use_grok: bool = field(default_factory=lambda: _bool("USE_GROK", True))
    grok_min_confidence: int = field(default_factory=lambda: _int("GROK_MIN_CONFIDENCE", 65))
    grok_prompt_file: Path = field(
        default_factory=lambda: Path(os.getenv("GROK_PROMPT_FILE", ROOT / "grok_prompt.md"))
    )

    # Broker
    alpaca_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    data_feed: str = field(default_factory=lambda: os.getenv("ALPACA_DATA_FEED", "iex"))
    live_trading: bool = field(
        default_factory=lambda: _bool("LIVE_TRADING", False) and _bool("I_UNDERSTAND_THE_RISK", False)
    )

    # Strategy
    watchlist: list[str] = field(
        default_factory=lambda: [
            s.strip().upper()
            for s in os.getenv("WATCHLIST", "SPY,QQQ,AAPL,NVDA,TSLA").split(",")
            if s.strip()
        ]
    )
    timeframe: str = field(default_factory=lambda: os.getenv("TIMEFRAME", "5Min"))
    loop_seconds: int = field(default_factory=lambda: _int("LOOP_SECONDS", 60))

    # Risk
    risk_per_trade_pct: float = field(default_factory=lambda: _float("RISK_PER_TRADE_PCT", 0.5))
    max_daily_loss_pct: float = field(default_factory=lambda: _float("MAX_DAILY_LOSS_PCT", 2.0))
    max_open_positions: int = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", 3))
    max_position_pct: float = field(default_factory=lambda: _float("MAX_POSITION_PCT", 20))
    max_trades_per_day: int = field(default_factory=lambda: _int("MAX_TRADES_PER_DAY", 10))
    atr_stop_mult: float = field(default_factory=lambda: _float("ATR_STOP_MULT", 1.5))
    reward_risk: float = field(default_factory=lambda: _float("REWARD_RISK", 2.0))
    skip_first_minutes: int = field(default_factory=lambda: _int("SKIP_FIRST_MINUTES", 15))
    flatten_minutes_before_close: int = field(
        default_factory=lambda: _int("FLATTEN_MINUTES_BEFORE_CLOSE", 10)
    )
    allow_shorts: bool = field(default_factory=lambda: _bool("ALLOW_SHORTS", False))

    journal_file: Path = field(default_factory=lambda: ROOT / "logs" / "trades.csv")

    @property
    def trading_url(self) -> str:
        return (
            "https://api.alpaca.markets"
            if self.live_trading
            else "https://paper-api.alpaca.markets"
        )
