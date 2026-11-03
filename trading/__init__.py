"""Rule-enforced intraday trading assistant (paper trading by default).

See trading/README.md. Quick start:  python -m trading demo
"""

from .config import NewsEvent, TradingConfig
from .engine import TradingAssistant
from .models import AccountState, Action, Decision, MarketSnapshot, Position, Trade

__all__ = [
    "AccountState", "Action", "Decision", "MarketSnapshot", "NewsEvent", "Position",
    "Trade", "TradingAssistant", "TradingConfig",
]
