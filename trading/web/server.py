"""Web dashboard for the trading assistant.

    python -m trading serve [--port 8000] [--host 127.0.0.1]

Standard library only. The browser is a thin UI: every decision, risk check and
fill comes from the same Python engine the CLI uses. The web app only runs
simulations, so it forces PAPER mode no matter what the config says.
"""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import tempfile
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import ET, TradingConfig
from ..examples.synthetic import generate_day
from ..journal import format_summary, load_records, summarize
from ..prompts import build_system_prompt

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 8 * 1024 * 1024  # CSV uploads
MAX_BARS = 200_000


class BadRequest(ValueError):
    pass


def config_from_payload(raw: dict | None, base: TradingConfig) -> TradingConfig:
    data = base.to_dict()
    data.update(raw or {})
    data["live"] = False  # the website never trades live
    try:
        return TradingConfig.from_dict(data)
    except (ValueError, TypeError, KeyError) as exc:
        raise BadRequest(str(exc)) from exc


def parse_csv_text(text: str):
    from ..__main__ import read_bars

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        rows = read_bars(path)
    except (KeyError, ValueError) as exc:
        raise BadRequest(
            "Could not read CSV. Expected columns: timestamp,ticker,open,high,low,close,volume "
            f"({exc})"
        ) from exc
    finally:
        Path(path).unlink(missing_ok=True)
    if not rows:
        raise BadRequest("The CSV has no rows.")
    if len(rows) > MAX_BARS:
        raise BadRequest(f"Too many bars ({len(rows)} > {MAX_BARS}).")
    return rows


def run_simulation(payload: dict, base: TradingConfig) -> dict:
    from ..__main__ import replay

    config = config_from_payload(payload.get("config"), base)
    if config.decider != "rules":
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:
            raise BadRequest(
                "The claude and consensus deciders need `pip install anthropic` and an "
                "ANTHROPIC_API_KEY on the server."
            ) from exc

    source = payload.get("source", "demo")
    if source == "demo":
        seed = int(payload.get("seed", 7))
        rows = generate_day(config.tickers, seed=seed)
    elif source == "csv":
        rows = parse_csv_text(str(payload.get("csv", "")))
    else:
        raise BadRequest("source must be 'demo' or 'csv'.")

    series: dict[str, list] = {}
    events: list[dict] = []

    def on_cycle(ticker, bar, snap, decision, was_halted):
        series.setdefault(ticker, []).append(
            {
                "t": bar.timestamp.astimezone(ET).isoformat(timespec="minutes"),
                "o": bar.open, "h": bar.high, "l": bar.low, "c": bar.close, "v": bar.volume,
                "vwap": round(snap.vwap, 4), "ema9": round(snap.ema9, 4), "ema21": round(snap.ema21, 4),
            }
        )

    with tempfile.TemporaryDirectory() as tmp:
        summaries = replay(config, rows, verbose=False, on_cycle=on_cycle, log_dir=tmp)
        records = []
        for f in sorted(Path(tmp).glob("*.jsonl")):
            records.extend(load_records(f))

    for r in records:
        if r.get("event") == "decision":
            events.append(
                {
                    "time": r["time"], "ticker": r["ticker"], "decider": r["decider"],
                    "proposed": r["proposed"], "executed": r["executed"],
                    "blocked": r["blocked"], "adjustments": r["adjustments"],
                }
            )

    return {
        "mode": config.mode,
        "config": config.to_dict(),
        "summaries": summaries,
        "summary_text": "\n\n".join(format_summary(s) for s in summaries),
        "series": series,
        "decisions": events,
    }


def list_logs(log_dir: Path) -> list[dict]:
    if not log_dir.is_dir():
        return []
    return [{"date": p.stem, "bytes": p.stat().st_size}
            for p in sorted(log_dir.glob("*.jsonl"), reverse=True)]


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAssistant/1.0"
    base_config: TradingConfig  # set by make_server

    # -- helpers ------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise BadRequest("Request too large (8 MB max).")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise BadRequest("Body must be JSON.") from exc
        if not isinstance(data, dict):
            raise BadRequest("Body must be a JSON object.")
        return data

    def log_message(self, fmt, *args):  # quieter default logging
        log.info("%s - %s", self.address_string(), fmt % args)

    # -- routes -------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        log_dir = Path(self.base_config.log_dir)
        if path == "/api/config":
            return self._json(self.base_config.to_dict() | {"live": False, "mode": "PAPER"})
        if path == "/api/logs":
            return self._json(list_logs(log_dir))
        if path.startswith("/api/logs/"):
            day = path.rsplit("/", 1)[-1]
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                return self._error(400, "Use /api/logs/YYYY-MM-DD.")
            f = log_dir / f"{day}.jsonl"
            if not f.is_file():
                return self._error(404, "No log for that date.")
            records = load_records(f)
            return self._json({"summary": summarize(records, self.base_config), "records": records})
        if path.startswith("/api/"):
            return self._error(404, "Unknown endpoint.")
        return self._static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            body = self._body()
            if path == "/api/run":
                return self._json(run_simulation(body, self.base_config))
            if path == "/api/prompt":
                cfg = config_from_payload(body.get("config"), self.base_config)
                return self._json({"prompt": build_system_prompt(cfg)})
            if path == "/api/validate":
                cfg = config_from_payload(body.get("config"), self.base_config)
                return self._json({"ok": True, "config": cfg.to_dict()})
            return self._error(404, "Unknown endpoint.")
        except BadRequest as exc:
            return self._error(400, str(exc))
        except Exception:  # never leak a traceback to the browser
            log.exception("Request failed")
            return self._error(500, "Internal error; see the server log.")

    def _static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self._error(404, "Not found.")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def make_server(config: TradingConfig, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"base_config": config})
    return ThreadingHTTPServer((host, port), handler)


def serve(config: TradingConfig, host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = make_server(config, host, port)
    print(f"Trading assistant dashboard on http://{host}:{httpd.server_address[1]}  (PAPER only, Ctrl+C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Warning: listening beyond localhost. There is no login; put it behind auth before exposing it.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
