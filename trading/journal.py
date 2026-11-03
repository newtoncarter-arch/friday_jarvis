"""Decision log and end-of-day summary."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .config import ET, TradingConfig
from .models import Trade


class Journal:
    """Writes one JSON line per event to ``<log_dir>/<YYYY-MM-DD>.jsonl``."""

    def __init__(self, config: TradingConfig, log_dir: str | Path | None = None):
        self.config = config
        self.dir = Path(log_dir or config.log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []

    def path_for(self, day: date) -> Path:
        return self.dir / f"{day.isoformat()}.jsonl"

    def _write(self, record: dict, now: datetime) -> None:
        record = {"time": now.astimezone(ET).isoformat(timespec="seconds"), "mode": self.config.mode, **record}
        self.records.append(record)
        with self.path_for(now.astimezone(ET).date()).open("a") as fh:
            fh.write(json.dumps(record) + "\n")

    def decision(self, now, ticker, decider, proposed, executed, blocked, adjustments) -> None:
        self._write(
            {
                "event": "decision",
                "ticker": ticker,
                "decider": decider,
                "proposed": proposed.to_dict(),
                "executed": executed.to_dict(),
                "blocked": blocked,
                "adjustments": adjustments,
            },
            now,
        )

    def trade_closed(self, trade: Trade) -> None:
        self._write(
            {
                "event": "trade_closed",
                "ticker": trade.ticker,
                "side": trade.side,
                "quantity": trade.quantity,
                "entry": round(trade.entry, 2),
                "exit": round(trade.exit, 2),
                "pnl": trade.pnl,
                "exit_reason": trade.exit_reason,
                "opened_at": trade.opened_at.astimezone(ET).isoformat(timespec="seconds"),
            },
            trade.closed_at,
        )

    def note(self, now: datetime, message: str, **extra) -> None:
        self._write({"event": "note", "message": message, **extra}, now)


def summarize(records: list[dict], config: TradingConfig) -> dict:
    """End-of-day summary from journal records (works on a re-read log file too)."""
    decisions = [r for r in records if r.get("event") == "decision"]
    closed = [r for r in records if r.get("event") == "trade_closed"]
    entries = [d for d in decisions if d["executed"]["action"] in ("BUY", "SHORT")]
    wins = [t for t in closed if t["pnl"] > 0]
    pnl = round(sum(t["pnl"] for t in closed), 2)

    blocked = [
        {"time": d["time"], "ticker": d["ticker"], "decider": d["decider"],
         "proposed": d["proposed"]["action"], "rule": d["blocked"]}
        for d in decisions if d["blocked"]
    ]

    # Violations that actually happened (should always be empty — the guard
    # exists to keep it that way — but we check the record independently).
    violations = []
    if len(entries) > config.max_trades_per_day:
        violations.append(f"{len(entries)} trades taken > max {config.max_trades_per_day}.")
    if pnl < -config.max_daily_loss_dollars * 1.0001:
        violations.append(
            f"Day loss ${-pnl:,.2f} exceeded the ${config.max_daily_loss_dollars:,.2f} limit "
            "(likely slippage/gap through a stop)."
        )
    for t in closed:
        risk_budget = config.risk_per_trade_dollars
        if t["pnl"] < -risk_budget * 1.05:
            violations.append(
                f"{t['ticker']} {t['opened_at']}: lost ${-t['pnl']:,.2f}, more than the "
                f"${risk_budget:,.2f} per-trade risk budget."
            )
    for e in entries:
        ex = e["executed"]
        if not ex["stop_loss"] or not ex["take_profit"]:
            violations.append(f"{e['ticker']} {e['time']}: entry without stop/target.")
    late = [
        t for t in closed
        if datetime.fromisoformat(t["time"]).time() > config.market_close
    ]
    if late:
        violations.append(f"{len(late)} position(s) closed after the market close.")
    open_at_end = _open_positions_from(records)
    if open_at_end:
        violations.append(f"Positions still open at end of log: {', '.join(open_at_end)}.")

    return {
        "date": records[0]["time"][:10] if records else None,
        "mode": config.mode,
        "trades_taken": len(entries),
        "trades_closed": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate_pct": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
        "realized_pnl": pnl,
        "return_pct": round(100 * pnl / config.account_size, 3),
        "best_trade": max((t["pnl"] for t in closed), default=0.0),
        "worst_trade": min((t["pnl"] for t in closed), default=0.0),
        "decisions_logged": len(decisions),
        "blocked_proposals": blocked,
        "rule_violations": violations,
        "trades": [
            {k: t[k] for k in ("ticker", "side", "quantity", "entry", "exit", "pnl", "exit_reason", "opened_at")}
            | {"closed_at": t["time"]}
            for t in closed
        ],
    }


def _open_positions_from(records: list[dict]) -> list[str]:
    open_: set[str] = set()
    for r in records:
        if r.get("event") == "decision" and r["executed"]["action"] in ("BUY", "SHORT"):
            open_.add(r["ticker"])
        elif r.get("event") == "trade_closed":
            open_.discard(r["ticker"])
    return sorted(open_)


def load_records(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def format_summary(s: dict) -> str:
    lines = [
        f"End-of-day summary {s['date']} [{s['mode']}]",
        f"  Trades taken:   {s['trades_taken']} (closed {s['trades_closed']})",
        f"  Win rate:       {s['win_rate_pct']}% ({s['wins']}W / {s['losses']}L)",
        f"  Realized P&L:   ${s['realized_pnl']:,.2f} ({s['return_pct']}%)",
        f"  Best / worst:   ${s['best_trade']:,.2f} / ${s['worst_trade']:,.2f}",
        f"  Decisions:      {s['decisions_logged']} logged, {len(s['blocked_proposals'])} blocked by risk rules",
    ]
    for t in s["trades"]:
        lines.append(
            f"    {t['opened_at'][11:16]}-{t['closed_at'][11:16]} {t['side']:5} {t['ticker']:5} "
            f"x{t['quantity']:<5} {t['entry']:>9.2f} -> {t['exit']:>9.2f}  "
            f"{t['pnl']:>9.2f}  ({t['exit_reason']})"
        )
    if s["blocked_proposals"]:
        lines.append("  Blocked proposals:")
        for b in s["blocked_proposals"]:
            lines.append(f"    {b['time'][11:16]} {b['ticker']} {b['proposed']} ({b['decider']}): {'; '.join(b['rule'])}")
    lines.append(
        "  Rule violations: " + ("none" if not s["rule_violations"] else "")
    )
    for v in s["rule_violations"]:
        lines.append(f"    - {v}")
    return "\n".join(lines)
