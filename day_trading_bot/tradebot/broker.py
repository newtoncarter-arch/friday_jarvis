"""Broker adapters: Alpaca (paper or live) and an in-memory simulator for backtests."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from .indicators import Bar

log = logging.getLogger(__name__)
DATA_URL = "https://data.alpaca.markets/v2"


class AlpacaBroker:
    def __init__(self, key: str, secret: str, trading_url: str, feed: str = "iex"):
        if not key or not secret:
            raise ValueError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing in .env")
        self.base = trading_url.rstrip("/") + "/v2"
        self.feed = feed
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _req(self, method: str, url: str, **kw):
        r = self.s.request(method, url, timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {url} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    # --- account / market ---
    def account(self) -> dict:
        a = self._req("GET", f"{self.base}/account")
        return {
            "equity": float(a["equity"]),
            "last_equity": float(a["last_equity"]),
            "buying_power": float(a["buying_power"]),
            "pattern_day_trader": a.get("pattern_day_trader", False),
            "daytrade_count": int(a.get("daytrade_count", 0)),
        }

    def clock(self) -> dict:
        c = self._req("GET", f"{self.base}/clock")
        return {
            "is_open": c["is_open"],
            "now": _parse(c["timestamp"]),
            "next_close": _parse(c["next_close"]),
            "next_open": _parse(c["next_open"]),
        }

    def bars(self, symbol: str, timeframe: str = "5Min", lookback_days: int = 3) -> list[Bar]:
        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        out, token = [], None
        while True:
            params = {"timeframe": timeframe, "start": start, "limit": 10000, "feed": self.feed}
            if token:
                params["page_token"] = token
            d = self._req("GET", f"{DATA_URL}/stocks/{symbol}/bars", params=params)
            for b in d.get("bars") or []:
                out.append(Bar(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]))
            token = d.get("next_page_token")
            if not token:
                return out

    # --- positions / orders ---
    def positions(self) -> dict[str, dict]:
        return {
            p["symbol"]: {
                "qty": float(p["qty"]),
                "side": p["side"],
                "entry": float(p["avg_entry_price"]),
                "unrealized_pl": float(p["unrealized_pl"]),
            }
            for p in self._req("GET", f"{self.base}/positions")
        }

    def open_order_symbols(self) -> set[str]:
        return {o["symbol"] for o in self._req("GET", f"{self.base}/orders", params={"status": "open"})}

    def bracket_order(self, symbol: str, side: str, qty: int, stop: float, target: float) -> dict:
        """Market entry with attached stop-loss and take-profit (OCO) on the broker side."""
        return self._req(
            "POST",
            f"{self.base}/orders",
            json={
                "symbol": symbol,
                "qty": str(qty),
                "side": side,
                "type": "market",
                "time_in_force": "day",
                "order_class": "bracket",
                "take_profit": {"limit_price": f"{target:.2f}"},
                "stop_loss": {"stop_price": f"{stop:.2f}"},
            },
        )

    def flatten_all(self) -> None:
        self._req("DELETE", f"{self.base}/orders")
        self._req("DELETE", f"{self.base}/positions", params={"cancel_orders": "true"})


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class SimBroker:
    """Fills bracket orders against historical bars. Used by the backtester."""

    def __init__(self, cash: float = 25_000.0, slippage: float = 0.01):
        self.cash = cash
        self.slippage = slippage
        self.pos: dict[str, dict] = {}
        self.closed: list[dict] = []

    def equity(self, prices: dict[str, float]) -> float:
        eq = self.cash
        for s, p in self.pos.items():
            px = prices.get(s, p["entry"])
            eq += p["qty"] * px if p["side"] == "buy" else p["qty"] * (2 * p["entry"] - px)
        return eq

    def open(self, symbol, side, qty, price, stop, target, t):
        fill = price + self.slippage if side == "buy" else price - self.slippage
        self.cash -= qty * fill
        self.pos[symbol] = dict(side=side, qty=qty, entry=fill, stop=stop, target=target, t=t)

    def on_bar(self, symbol: str, bar: Bar) -> dict | None:
        p = self.pos.get(symbol)
        if not p:
            return None
        if p["side"] == "buy":
            if bar.l <= p["stop"]:  # check stop first (conservative)
                return self.close(symbol, min(p["stop"], bar.o) - self.slippage, bar.t, "stop")
            if bar.h >= p["target"]:
                return self.close(symbol, max(p["target"], bar.o), bar.t, "target")
        else:
            if bar.h >= p["stop"]:
                return self.close(symbol, max(p["stop"], bar.o) + self.slippage, bar.t, "stop")
            if bar.l <= p["target"]:
                return self.close(symbol, min(p["target"], bar.o), bar.t, "target")
        return None

    def close(self, symbol: str, price: float, t: str, why: str) -> dict:
        p = self.pos.pop(symbol)
        if p["side"] == "buy":
            pnl = (price - p["entry"]) * p["qty"]
            self.cash += p["qty"] * price
        else:
            pnl = (p["entry"] - price) * p["qty"]
            self.cash += p["qty"] * p["entry"] + pnl
        trade = dict(symbol=symbol, side=p["side"], qty=p["qty"], entry=p["entry"], exit=price,
                     pnl=round(pnl, 2), opened=p["t"], closed=t, exit_reason=why)
        self.closed.append(trade)
        return trade
