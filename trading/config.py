"""Configuration for the intraday trading assistant.

Every bracketed placeholder from the original spec ([BROKER], [TICKERS], [X], ...)
is a field here. Values can come from a JSON file, environment variables
(prefixed ``TRADING_``), or both (env wins). Defaults are conservative and the
assistant always starts in PAPER mode unless ``LIVE=true`` is set explicitly.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_time(value: str | time) -> time:
    if isinstance(value, time):
        return value
    return time.fromisoformat(str(value).strip())


@dataclass(frozen=True)
class NewsEvent:
    """A scheduled market-moving event (FOMC, CPI, earnings, ...).

    ``ticker`` is ``None`` for market-wide events; earnings are per-ticker.
    """

    name: str
    at: datetime  # timezone-aware
    ticker: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "NewsEvent":
        at = datetime.fromisoformat(raw["at"])
        if at.tzinfo is None:
            at = at.replace(tzinfo=ET)
        return cls(name=raw["name"], at=at, ticker=raw.get("ticker"))


@dataclass
class TradingConfig:
    # --- Platform -------------------------------------------------------
    broker: str = "paper"  # [BROKER/PLATFORM]
    live: bool = False  # LIVE=true is the ONLY way to send real orders
    tickers: tuple[str, ...] = ("SPY", "QQQ", "AAPL")  # [TICKERS/MARKET]

    # --- Session --------------------------------------------------------
    market_open: time = time(9, 30)
    market_close: time = time(16, 0)
    trade_start: time = time(9, 45)  # first 15 minutes blocked
    trade_end: time = time(15, 45)  # last 15 minutes blocked; flatten here
    news_blackout_minutes: int = 5

    # --- Account & risk -------------------------------------------------
    account_size: float = 25_000.0  # $[X]
    risk_per_trade_pct: float = 1.0  # [1]%
    max_daily_loss_pct: float = 3.0  # [3]%
    max_open_positions: int = 3  # [3]
    max_trades_per_day: int = 10  # [10]
    min_reward_risk: float = 1.5  # [1.5:1]
    allow_short: bool = True

    # --- Strategy (default: VWAP reclaim + EMA trend + volume) -----------
    target_r_multiple: float = 2.0  # take-profit distance in R
    swing_lookback: int = 5  # bars used to find the recent swing low/high
    volume_lookback: int = 20  # bars for the volume average
    min_stop_distance_pct: float = 0.05  # reject stops tighter than this % of price
    strategy_description: str = (
        "Long when price reclaims VWAP (previous close below VWAP, current close "
        "above VWAP) with volume above its 20-bar average and the 9 EMA above the "
        "21 EMA on the 5-min chart. Short is the reverse. Stop goes below the most "
        "recent swing low (long) or above the most recent swing high (short)."
    )

    # --- Data hygiene ---------------------------------------------------
    max_data_age_seconds: int = 90  # snapshots older than this are stale

    # --- Decision engine ------------------------------------------------
    decider: str = "rules"  # rules | claude | consensus
    model: str = "claude-opus-5-5"
    effort: str = "low"

    # --- Logging --------------------------------------------------------
    log_dir: str = "trading_logs"

    news_events: tuple[NewsEvent, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------
    @property
    def risk_per_trade_dollars(self) -> float:
        return self.account_size * self.risk_per_trade_pct / 100.0

    @property
    def max_daily_loss_dollars(self) -> float:
        return self.account_size * self.max_daily_loss_pct / 100.0

    @property
    def mode(self) -> str:
        return "LIVE" if self.live else "PAPER"

    def validate(self) -> None:
        problems = []
        if self.account_size <= 0:
            problems.append("account_size must be > 0")
        if not 0 < self.risk_per_trade_pct <= 5:
            problems.append("risk_per_trade_pct must be in (0, 5]")
        if not 0 < self.max_daily_loss_pct <= 20:
            problems.append("max_daily_loss_pct must be in (0, 20]")
        if self.min_reward_risk < 1:
            problems.append("min_reward_risk must be >= 1")
        if self.target_r_multiple < self.min_reward_risk:
            problems.append("target_r_multiple must be >= min_reward_risk")
        if not (self.market_open <= self.trade_start < self.trade_end <= self.market_close):
            problems.append("trading window must sit inside market hours")
        if self.decider not in {"rules", "claude", "consensus"}:
            problems.append("decider must be rules, claude or consensus")
        if not self.tickers:
            problems.append("at least one ticker is required")
        if problems:
            raise ValueError("Invalid trading config: " + "; ".join(problems))

    def to_dict(self) -> dict:
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, time):
                data[key] = value.isoformat("minutes")
        data["news_events"] = [
            {"name": e.name, "at": e.at.isoformat(), "ticker": e.ticker}
            for e in self.news_events
        ]
        data["tickers"] = list(self.tickers)
        return data

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None, env: dict | None = None) -> "TradingConfig":
        """Build a config from an optional JSON file, then ``TRADING_*`` / ``LIVE`` env vars."""
        raw: dict = {}
        if path:
            raw.update(json.loads(Path(path).read_text()))

        env = dict(os.environ if env is None else env)
        names = {f.name for f in fields(cls)}
        for name in names:
            key = f"TRADING_{name.upper()}"
            if key in env:
                raw[name] = env[key]
        if "LIVE" in env:  # the spec's literal switch
            raw["live"] = env["LIVE"]

        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "TradingConfig":
        """Build and validate a config from plain JSON-style values (unknown keys ignored)."""
        kwargs: dict = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if f.name == "tickers":
                if isinstance(value, str):
                    value = [t for t in value.split(",") if t.strip()]
                value = tuple(t.strip().upper() for t in value)
            elif f.name == "news_events":
                if isinstance(value, str):
                    value = json.loads(value)
                value = tuple(NewsEvent.from_dict(e) for e in value)
            elif f.type in ("bool", bool):
                value = _parse_bool(value)
            elif f.type in ("int", int):
                value = int(value)
            elif f.type in ("float", float):
                value = float(value)
            elif f.type in ("time", time):
                value = _parse_time(value)
            kwargs[f.name] = value

        config = cls(**kwargs)
        config.validate()
        return config
