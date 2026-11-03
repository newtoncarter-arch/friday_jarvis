"""Run with:  python -m unittest discover -s trading -t .   (stdlib only)"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from trading.config import ET, NewsEvent, TradingConfig
from trading.engine import TradingAssistant
from trading.journal import Journal, summarize
from trading.llm import ClaudeDecider
from trading.models import AccountState, Action, Decision, MarketSnapshot, Position
from trading.risk import RiskManager
from trading.examples.synthetic import generate_day
from trading.__main__ import replay

DAY = (2026, 11, 3)  # a Tuesday


def at(h: int, m: int, s: int = 0) -> datetime:
    return datetime(*DAY, h, m, s, tzinfo=ET)


def snap(ticker="SPY", t=None, price=100.0, **kw) -> MarketSnapshot:
    """A snapshot that satisfies the long setup by default."""
    base = dict(
        ticker=ticker, timestamp=t or at(10, 30), price=price, high=price + 0.2, low=price - 0.3,
        volume=2_000_000, vwap=price - 0.1, ema9=price - 0.05, ema21=price - 0.3,
        avg_volume=1_000_000, prev_close=price - 0.4, prev_vwap=price - 0.1,
        swing_low=price - 1.0, swing_high=price + 1.0,
    )
    base.update(kw)
    return MarketSnapshot(**base)


def long_decision(ticker="SPY", entry=100.0, stop=99.0, target=102.0, qty=0) -> Decision:
    return Decision(Action.BUY, ticker, qty, entry, stop, target, 70, "test")


class ConfigTests(unittest.TestCase):
    def test_defaults_are_paper(self):
        cfg = TradingConfig.load(env={})
        self.assertFalse(cfg.live)
        self.assertEqual(cfg.mode, "PAPER")
        self.assertEqual(cfg.risk_per_trade_dollars, 250.0)
        self.assertEqual(cfg.max_daily_loss_dollars, 750.0)

    def test_env_overrides_and_live_switch(self):
        cfg = TradingConfig.load(env={
            "LIVE": "true", "TRADING_TICKERS": "msft, nvda", "TRADING_ACCOUNT_SIZE": "50000",
            "TRADING_TRADE_START": "10:00",
        })
        self.assertTrue(cfg.live)
        self.assertEqual(cfg.tickers, ("MSFT", "NVDA"))
        self.assertEqual(cfg.account_size, 50000.0)
        self.assertEqual(cfg.trade_start.hour, 10)

    def test_json_file_and_news_events(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"tickers": ["SPY"], "news_events": [
                {"name": "CPI", "at": "2026-11-03T08:30:00"}]}, fh)
        cfg = TradingConfig.load(fh.name, env={})
        self.assertEqual(cfg.news_events[0].at.tzinfo, ET)

    def test_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            TradingConfig.load(env={"TRADING_RISK_PER_TRADE_PCT": "25"})
        with self.assertRaises(ValueError):
            TradingConfig.load(env={"TRADING_TARGET_R_MULTIPLE": "1.0"})


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.cfg = TradingConfig(news_events=(NewsEvent("FOMC", at(14, 0)),
                                              NewsEvent("AAPL earnings", at(11, 0), "AAPL")))
        self.risk = RiskManager(self.cfg)
        self.acct = AccountState(starting_equity=25_000)

    def review(self, decision, s=None, now=None, acct=None):
        s = s or snap(t=now or at(10, 30))
        return self.risk.review(decision, s, acct or self.acct, now or s.timestamp)

    def test_valid_long_sized_to_one_percent(self):
        r = self.review(long_decision())
        self.assertTrue(r.approved, r.blocked)
        # $250 risk / $1 stop = 250 shares; $25k cash / $100 = 250 shares.
        self.assertEqual(r.decision.quantity, 250)

    def test_quantity_clamped_not_increased(self):
        r = self.review(long_decision(qty=10_000))
        self.assertEqual(r.decision.quantity, 250)
        self.assertTrue(r.adjustments)

    def test_cash_limit_caps_size(self):
        r = self.review(long_decision(stop=99.9, target=100.3))
        self.assertEqual(r.decision.quantity, 250)  # risk alone would allow 2500

    def test_time_windows(self):
        for t, ok in [(at(9, 40), False), (at(9, 45), True), (at(15, 44), True),
                      (at(15, 45), False), (at(16, 30), False)]:
            r = self.review(long_decision(), s=snap(t=t), now=t)
            self.assertEqual(r.approved, ok, (t, r.blocked))

    def test_weekend_blocked(self):
        sat = datetime(2026, 11, 7, 11, 0, tzinfo=ET)
        self.assertFalse(self.review(long_decision(), s=snap(t=sat), now=sat).approved)

    def test_news_blackout(self):
        self.assertFalse(self.review(long_decision(), s=snap(t=at(13, 56)), now=at(13, 56)).approved)
        self.assertTrue(self.review(long_decision(), s=snap(t=at(13, 54)), now=at(13, 54)).approved)
        # Earnings only blocks its own ticker.
        aapl = long_decision("AAPL")
        self.assertFalse(self.review(aapl, s=snap("AAPL", t=at(11, 3)), now=at(11, 3)).approved)
        self.assertTrue(self.review(long_decision(), s=snap(t=at(11, 3)), now=at(11, 3)).approved)

    def test_requires_stop_and_target(self):
        self.assertFalse(self.review(long_decision(stop=0)).approved)
        self.assertFalse(self.review(long_decision(target=0)).approved)

    def test_wrong_side_levels(self):
        self.assertFalse(self.review(long_decision(stop=101, target=103)).approved)
        short = Decision(Action.SHORT, "SPY", 0, 100, 99, 98, 50, "x")
        self.assertFalse(self.review(short).approved)

    def test_min_reward_risk(self):
        self.assertFalse(self.review(long_decision(stop=99, target=101.4)).approved)
        self.assertTrue(self.review(long_decision(stop=99, target=101.5)).approved)

    def test_entry_reanchored_to_market(self):
        r = self.review(long_decision(entry=95.0, stop=99, target=102))
        self.assertEqual(r.decision.entry, 100.0)
        self.assertTrue(r.adjustments)

    def test_unlisted_ticker(self):
        self.assertFalse(self.review(long_decision("TSLA"), s=snap("TSLA")).approved)

    def test_max_positions_and_trades(self):
        acct = AccountState(starting_equity=1_000_000)
        for t in ("QQQ", "AAPL", "IWM"):
            acct.positions[t] = Position(t, "long", 1, 10, 9, 12, at(10, 0))
        self.assertIn("open positions", self.review(long_decision(), acct=acct).blocked[0])
        acct = AccountState(starting_equity=25_000, trades_today=10)
        self.assertIn("trades per day", self.review(long_decision(), acct=acct).blocked[0])

    def test_never_average_down(self):
        self.acct.positions["SPY"] = Position("SPY", "long", 100, 101.0, 99.5, 104, at(10, 0))
        r = self.review(long_decision())
        self.assertIn("average down", r.blocked[0])

    def test_exit_allowed_inside_blackout(self):
        self.acct.positions["SPY"] = Position("SPY", "long", 100, 99.0, 98, 102, at(10, 0))
        sell = Decision(Action.SELL, "SPY", 5, 0, 0, 0, 50, "exit")
        r = self.review(sell, s=snap(t=at(15, 50)), now=at(15, 50))
        self.assertTrue(r.approved)
        self.assertEqual(r.decision.quantity, 100)

    def test_sell_without_position_blocked(self):
        self.assertFalse(self.review(Decision(Action.SELL, "SPY")).approved)

    def test_shorts_can_be_disabled(self):
        risk = RiskManager(TradingConfig(allow_short=False))
        short = Decision(Action.SHORT, "SPY", 0, 100, 101, 98, 50, "x")
        self.assertFalse(risk.review(short, snap(), self.acct, at(10, 30)).approved)

    def test_data_problems(self):
        now = at(10, 30)
        self.assertIn("No market data", self.risk.data_problem(None, now, "SPY"))
        self.assertIn("Stale", self.risk.data_problem(snap(t=at(10, 25)), now, "SPY"))
        self.assertIn("Missing", self.risk.data_problem(snap(vwap=float("nan")), now, "SPY"))
        self.assertIn("Conflicting", self.risk.data_problem(snap(high=90.0), now, "SPY"))
        self.assertIsNone(self.risk.data_problem(snap(), now, "SPY"))


class FakeClaude:
    def __init__(self, decision: Decision | None = None, exc: Exception | None = None):
        self.decision, self.exc, self.calls = decision, exc, 0

    def decide(self, snapshot, account, now, blackout=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.decision


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, **cfg):
        config = TradingConfig(log_dir=self.tmp.name, **cfg)
        return TradingAssistant(config, claude=cfg.get("decider") and FakeClaude())

    def test_rules_entry_then_target(self):
        bot = self.make()
        d = bot.on_snapshot(snap(), now=at(10, 30))
        self.assertEqual(d.action, Action.BUY)
        self.assertEqual(bot.account.trades_today, 1)
        d = bot.on_snapshot(snap(t=at(10, 35), price=103, high=104, low=102.5), now=at(10, 35))
        self.assertEqual(d.action, Action.HOLD)  # no same-bar re-entry after the exit
        self.assertNotIn("SPY", bot.account.positions)
        self.assertGreater(bot.account.realized_pnl, 0)

    def test_stale_data_holds(self):
        bot = self.make()
        d = bot.on_snapshot(snap(t=at(10, 0)), now=at(10, 30))
        self.assertEqual(d.action, Action.HOLD)
        self.assertIn("Stale", d.reason)

    def test_daily_loss_halts_and_flattens(self):
        bot = self.make()
        bot.account.realized_pnl = -700
        bot.on_snapshot(snap(), now=at(10, 30))  # opens SPY
        self.assertIn("SPY", bot.account.positions)
        # Down $0.5 on 250 shares = -$125 unrealized -> total -825 > $750 limit.
        d = bot.on_snapshot(snap(t=at(10, 35), price=99.5, low=99.4, high=99.7,
                                 prev_close=99.6, swing_low=98.0), now=at(10, 35))
        self.assertEqual(d.action, Action.HALT)
        self.assertFalse(bot.account.positions)
        self.assertEqual(bot.on_snapshot(snap(t=at(11, 0)), now=at(11, 0)).action, Action.HALT)

    def test_end_of_window_flattens(self):
        bot = self.make()
        bot.on_snapshot(snap(), now=at(10, 30))
        d = bot.on_snapshot(snap(t=at(15, 45), price=100.2, high=100.3, low=100.1), now=at(15, 45))
        self.assertEqual(d.action, Action.HALT)
        self.assertFalse(bot.account.positions)

    def test_new_day_resets(self):
        bot = self.make()
        bot.on_snapshot(snap(t=at(15, 50)), now=at(15, 50))
        self.assertTrue(bot.account.halted)
        tomorrow = datetime(2026, 11, 4, 10, 30, tzinfo=ET)
        self.assertEqual(bot.on_snapshot(snap(t=tomorrow), now=tomorrow).action, Action.BUY)

    def test_live_requires_adapter(self):
        with self.assertRaises(RuntimeError):
            TradingAssistant(TradingConfig(live=True, log_dir=self.tmp.name))

    def test_claude_rule_breaking_proposal_is_blocked_and_logged(self):
        bad = long_decision(stop=0)  # no stop
        bot = TradingAssistant(TradingConfig(decider="claude", log_dir=self.tmp.name),
                               claude=FakeClaude(bad))
        d = bot.on_snapshot(snap(), now=at(10, 30))
        self.assertEqual(d.action, Action.HOLD)
        rec = [r for r in bot.journal.records if r["event"] == "decision"][-1]
        self.assertTrue(rec["blocked"])
        self.assertEqual(rec["proposed"]["action"], "BUY")

    def test_claude_not_called_in_blackout_without_position(self):
        fake = FakeClaude(long_decision())
        bot = TradingAssistant(TradingConfig(decider="claude", log_dir=self.tmp.name), claude=fake)
        bot.on_snapshot(snap(t=at(9, 40)), now=at(9, 40))
        self.assertEqual(fake.calls, 0)

    def test_consensus_requires_agreement(self):
        fake = FakeClaude(Decision.hold("SPY", "not convinced"))
        bot = TradingAssistant(TradingConfig(decider="consensus", log_dir=self.tmp.name), claude=fake)
        self.assertEqual(bot.on_snapshot(snap(), now=at(10, 30)).action, Action.HOLD)
        fake.decision = long_decision(qty=9999)
        d = bot.on_snapshot(snap(t=at(10, 35)), now=at(10, 35))
        self.assertEqual(d.action, Action.BUY)
        self.assertTrue(0 < d.quantity <= 250)  # sized by the risk engine, not the model


class ClaudeDeciderTests(unittest.TestCase):
    def client(self, text=None, stop="end_turn", exc=None):
        def create(**kwargs):
            self.kwargs = kwargs
            if exc:
                raise exc
            return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])
        return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))

    def decide(self, **kw):
        dec = ClaudeDecider(TradingConfig(), client=self.client(**kw))
        return dec.decide(snap(), AccountState(25_000), at(10, 30))

    def test_parses_structured_output(self):
        d = self.decide(text=json.dumps(long_decision().to_dict()))
        self.assertEqual(d.action, Action.BUY)
        fmt = self.kwargs["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(self.kwargs["model"], "claude-opus-5-5")

    def test_failures_degrade_to_hold(self):
        self.assertEqual(self.decide(exc=TimeoutError()).action, Action.HOLD)
        self.assertEqual(self.decide(text="not json").action, Action.HOLD)
        self.assertEqual(self.decide(text="", stop="refusal").action, Action.HOLD)
        self.assertEqual(self.decide(text='{"action": "BU', stop="max_tokens").action, Action.HOLD)


class ReplayAndSummaryTests(unittest.TestCase):
    def test_synthetic_replay_respects_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = TradingConfig(log_dir=tmp)
            for seed in range(1, 15):
                for s in replay(cfg, generate_day(cfg.tickers, seed=seed), verbose=False):
                    self.assertEqual(s["rule_violations"], [], (seed, s))
                    self.assertLessEqual(s["trades_taken"], cfg.max_trades_per_day)

    def test_summary_math(self):
        cfg = TradingConfig()
        recs = [
            {"event": "decision", "time": "2026-11-03T10:00:00-05:00", "ticker": "SPY",
             "decider": "rules", "blocked": [], "proposed": long_decision().to_dict(),
             "executed": long_decision(qty=10).to_dict()},
            {"event": "trade_closed", "time": "2026-11-03T10:30:00-05:00", "ticker": "SPY",
             "side": "long", "quantity": 10, "entry": 100, "exit": 102, "pnl": 20.0,
             "exit_reason": "take-profit", "opened_at": "2026-11-03T10:00:00-05:00"},
        ]
        s = summarize(recs, cfg)
        self.assertEqual((s["trades_taken"], s["wins"], s["win_rate_pct"], s["realized_pnl"]),
                         (1, 1, 100.0, 20.0))
        self.assertEqual(s["rule_violations"], [])

    def test_summary_flags_open_position(self):
        recs = [{"event": "decision", "time": "2026-11-03T10:00:00-05:00", "ticker": "SPY",
                 "decider": "rules", "blocked": [], "proposed": long_decision().to_dict(),
                 "executed": long_decision(qty=10).to_dict()}]
        self.assertTrue(summarize(recs, TradingConfig())["rule_violations"])


if __name__ == "__main__":
    unittest.main()
