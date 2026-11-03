"""Tests for the web dashboard API (stdlib only)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from trading.config import TradingConfig
from trading.web.server import make_server


class WebServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.httpd = make_server(TradingConfig(log_dir=cls.tmp.name), "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def test_index_served(self):
        status, body = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Intraday Trading Assistant", body)

    def test_config_is_paper(self):
        status, body = self.call("/api/config")
        cfg = json.loads(body)
        self.assertEqual((status, cfg["live"], cfg["mode"]), (200, False, "PAPER"))

    def test_run_demo(self):
        status, body = self.call("/api/run", {"source": "demo", "seed": 3})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "PAPER")
        self.assertEqual(set(data["series"]), {"SPY", "QQQ", "AAPL"})
        self.assertEqual(data["summaries"][0]["rule_violations"], [])

    def test_live_flag_is_ignored(self):
        _, body = self.call("/api/run", {"source": "demo", "config": {"live": True}})
        self.assertEqual(json.loads(body)["mode"], "PAPER")

    def test_invalid_config_rejected(self):
        status, body = self.call("/api/run", {"config": {"risk_per_trade_pct": 50}})
        self.assertEqual(status, 400)
        self.assertIn("risk_per_trade_pct", json.loads(body)["error"])

    def test_csv_upload(self):
        rows = ["timestamp,ticker,open,high,low,close,volume"]
        for i in range(30):
            h, m = divmod(9 * 60 + 35 + 5 * i, 60)
            p = 100 + i * 0.1
            rows.append(f"2026-11-03T{h:02d}:{m:02d}:00-05:00,SPY,{p},{p+0.2},{p-0.2},{p+0.05},1000")
        status, body = self.call("/api/run", {"source": "csv", "csv": "\n".join(rows)})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(json.loads(body)["series"]["SPY"]), 29)

    def test_bad_csv(self):
        status, _ = self.call("/api/run", {"source": "csv", "csv": "a,b\n1,2"})
        self.assertEqual(status, 400)

    def test_prompt(self):
        _, body = self.call("/api/prompt", {"config": {"tickers": ["NVDA"]}})
        self.assertIn("NVDA", json.loads(body)["prompt"])

    def test_path_traversal_blocked(self):
        for path in ("/../config.py", "/%2e%2e/config.py", "/../../server.py"):
            self.assertEqual(self.call(path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
