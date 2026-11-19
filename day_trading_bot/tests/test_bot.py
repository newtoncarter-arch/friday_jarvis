import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from tradebot.backtest import run_backtest, synthetic_bars
from tradebot.config import Settings
from tradebot.grok import GrokAdvisor, _extract_json
from tradebot.indicators import Bar, ema, rsi, vwap
from tradebot.risk import RiskManager
from tradebot.strategy import Signal, find_signal


@pytest.fixture
def s(monkeypatch):
    monkeypatch.setenv("USE_GROK", "false")
    monkeypatch.setenv("LIVE_TRADING", "false")
    return Settings()


def test_ema_and_rsi_basics():
    vals = [float(x) for x in range(1, 31)]
    e = ema(vals, 5)
    assert e[3] is None and e[4] == 3.0
    assert rsi(vals)[-1] == 100.0  # only gains


def test_vwap_resets_each_day():
    bars = [Bar("2026-01-05T15:00Z", 10, 10, 10, 10, 100), Bar("2026-01-06T15:00Z", 20, 20, 20, 20, 1)]
    assert vwap(bars) == [10, 20]


def test_risk_sizing_respects_limits(s):
    r = RiskManager(s)
    d = r.size(equity=25_000, buying_power=100_000, entry=100, stop=99, open_positions=0, trades_today=0)
    # 0.5% of 25k = $125 risk / $1 per share = 125 sh, capped by 20% size = 50 sh
    assert d.allowed and d.qty == 50
    assert not r.size(25_000, 100_000, 100, 99, s.max_open_positions, 0).allowed
    assert r.daily_loss_hit(24_400, 25_000)
    assert not r.daily_loss_hit(24_900, 25_000)


def test_live_mode_needs_two_flags(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("I_UNDERSTAND_THE_RISK", "false")
    assert Settings().live_trading is False
    monkeypatch.setenv("I_UNDERSTAND_THE_RISK", "true")
    assert Settings().live_trading is True


def test_extract_json_handles_markdown():
    assert _extract_json('Sure!\n```json\n{"decision": "APPROVE"}\n```')["decision"] == "APPROVE"
    assert _extract_json('blah {"a": 1} blah') == {"a": 1}


def _sig():
    return Signal("AAPL", "buy", 100.0, 99.0, 102.0, "test", {"symbol": "AAPL"})


def _fake_grok_response(content):
    resp = mock.Mock()
    resp.raise_for_status = lambda: None
    resp.json = lambda: {"choices": [{"message": {"content": content}}]}
    return resp


def test_grok_approve_and_confidence_threshold():
    g = GrokAdvisor("key", "grok-4", "prompt", min_confidence=70)
    ok = json.dumps({"decision": "APPROVE", "confidence": 80, "stop_loss": 99.3, "reason": "good"})
    with mock.patch("tradebot.grok.requests.post", return_value=_fake_grok_response(ok)):
        v = g.review(_sig(), {})
    assert v.approved and v.stop_loss == 99.3
    weak = json.dumps({"decision": "APPROVE", "confidence": 50, "reason": "meh"})
    with mock.patch("tradebot.grok.requests.post", return_value=_fake_grok_response(weak)):
        assert not g.review(_sig(), {}).approved


def test_grok_failure_means_no_trade():
    g = GrokAdvisor("key", "grok-4", "prompt")
    with mock.patch("tradebot.grok.requests.post", side_effect=ConnectionError("down")):
        assert not g.review(_sig(), {}).approved


def test_backtest_runs_and_flattens_daily(s):
    data = {sym: synthetic_bars(sym, 10) for sym in ["SPY", "NVDA", "QQQ"]}
    res = run_backtest(data, s)
    assert res["trades"] > 0
    for t in res["trade_list"]:
        assert t["opened"][:10] == t["closed"][:10]  # never held overnight


def test_backtest_with_grok_rejecting_everything(s):
    grok = mock.Mock()
    grok.review.return_value = mock.Mock(approved=False)
    res = run_backtest({"SPY": synthetic_bars("SPY", 5)}, s, grok=grok)
    assert res["trades"] == 0 and res["grok_calls"] > 0


class FakeBroker:
    def __init__(self, bars):
        self._bars = bars
        self.orders = []
        now = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)  # 12:00 ET
        self._clock = {"is_open": True, "now": now, "next_close": now + timedelta(hours=4),
                       "next_open": now + timedelta(days=1)}

    def clock(self):
        return self._clock

    def account(self):
        return {"equity": 30_000.0, "last_equity": 30_000.0, "buying_power": 60_000.0,
                "pattern_day_trader": False, "daytrade_count": 0}

    def positions(self):
        return {}

    def open_order_symbols(self):
        return set()

    def bars(self, sym, tf):
        return self._bars

    def bracket_order(self, *a):
        self.orders.append(a)

    def flatten_all(self):
        self.orders.append("flatten")


def test_live_tick_places_bracket_order_when_grok_approves(s, tmp_path):
    from tradebot.bot import TradingBot

    bars = synthetic_bars("NVDA", 5)
    # find a prefix of bars that produces a signal
    prefix = next(bars[: i + 1] for i in range(40, len(bars)) if find_signal("NVDA", bars[: i + 1]))
    s.watchlist = ["NVDA"]
    s.journal_file = tmp_path / "trades.csv"
    with mock.patch("tradebot.bot.AlpacaBroker", return_value=FakeBroker(prefix)):
        bot = TradingBot(s)
    bot.grok = mock.Mock()
    bot.grok.review.return_value = mock.Mock(approved=True, confidence=90, stop_loss=None,
                                             take_profit=None, reason="looks good")
    with mock.patch("tradebot.bot.regular_hours", side_effect=lambda b: b):
        bot.tick()
    assert len(bot.broker.orders) == 1
    sym, side, qty, stop, target = bot.broker.orders[0]
    assert sym == "NVDA" and side == "buy" and qty > 0 and stop < target
    assert "order" in s.journal_file.read_text()
