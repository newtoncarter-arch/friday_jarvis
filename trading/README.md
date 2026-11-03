# Intraday Trading Assistant

A disciplined, rule-enforced intraday trading assistant. It **starts in PAPER mode** and can
only send live orders when `LIVE=true` **and** you plug in a real broker adapter.

> Not financial advice. Nothing here has been validated against real market data. Paper trade it,
> review the logs, and backtest on your own data before risking money.

## Design: the model proposes, the code decides

```
5-min bar ─▶ indicators ─▶ data checks ─▶ halt / flatten checks ─▶ decider ─▶ RiskManager ─▶ broker
                          (stale, missing,   (daily loss, end of     (rules |    (hard rules,  (paper or
                           conflicting)        window)                claude |    sizing)       live adapter)
                                                                      consensus)
                                         every step ─▶ journal (JSONL) ─▶ end-of-day summary
```

Every risk rule from the spec is enforced in code (`risk.py`), not left to the prompt. A proposal
that breaks one becomes a HOLD, and the journal records it as a blocked proposal.

| Rule | Where it's enforced |
|---|---|
| Paper unless `LIVE=true` | `broker.make_broker`: live needs the flag *and* an adapter, otherwise it refuses to start |
| Only approved tickers | `RiskManager.review` |
| 1% max risk per trade | Quantity is **computed** from the stop distance (and capped by cash, no leverage); a model's larger size gets cut down |
| 3% max daily loss → close all, stop | `RiskManager.must_halt` checks realized + unrealized P&L every cycle, then `_halt` flattens |
| Max 3 open positions, 10 trades/day | `RiskManager.review` |
| Stop-loss and target on every entry | Entries without both, or with levels on the wrong side, are blocked |
| Reward:risk ≥ 1.5 | Blocked below the minimum. Stops tighter than 0.05% of price are blocked too |
| No trading in the first or last 15 min, or within 5 min of news | `entry_window_block`. Exits are always allowed. Earnings events can be per-ticker |
| No overnight holds | Everything is flattened at `trade_end` (15:45 ET), with a backstop in `end_of_day` |
| Never average down | Adding to any existing position is blocked. Adding to a losing one is labeled as averaging down |
| Missing, stale or conflicting data → HOLD | `RiskManager.data_problem`: NaN fields, data older than 90s, bad bar ranges |
| Default to HOLD | Model errors, refusals, truncation or malformed JSON all become HOLD |

Two extra safeguards: the entry price is always reset to the live price (the model can't invent
one), and there is no re-entry on the same bar where a stop or target filled.

## Website (dashboard)

```bash
python -m trading serve            # then open http://127.0.0.1:8000
```

A browser dashboard on top of the same engine, using only the standard library. Set the risk
rules, strategy, decider and news calendar in the left panel, then run a paper session on a
demo day or on your own CSV. Results include P&L, win rate, blocked proposals and rule
violations, plus:

- Price charts with VWAP, EMA 9/21, entries, exits and stop/target levels
- An equity curve
- A trades table and a filterable decision log that shows why each proposal was blocked or adjusted
- The end-of-day summary, downloadable as JSON
- The exact Claude prompt your settings produce

It works on phones and follows the system's light/dark setting.

The website **only simulates**. It forces PAPER mode even if a request sends `live: true`.
It listens on `127.0.0.1` by default and has no login, so put it behind authentication before
using `--host 0.0.0.0`. Files: `web/server.py` (JSON API) and `web/static/` (HTML/CSS/JS, no
build step and no external requests).

| Endpoint | What it does |
|---|---|
| `GET /api/config` | Server defaults |
| `POST /api/run` | `{config, source: "demo"\|"csv", seed, csv}` → summaries, bar series, decisions |
| `POST /api/prompt` | Rendered system prompt for a config |
| `GET /api/logs`, `/api/logs/YYYY-MM-DD` | Journals written by the CLI or a live process |

## Quick start

```bash
python -m trading demo                     # replay a synthetic session (rules decider, paper)
python -m trading prompt                   # show the rendered Claude system prompt
python -m trading replay --csv bars.csv    # replay your own 5-min bars
python -m trading summary trading_logs/2026-11-03.jsonl
python -m trading serve                    # web dashboard at http://127.0.0.1:8000
python -m unittest discover -s trading -t .   # 44 tests, stdlib only
```

The `rules` decider only needs the Python standard library. The Claude deciders need
`pip install anthropic` and `ANTHROPIC_API_KEY`.

CSV format (bar-close timestamps, any number of tickers and days):

```
timestamp,ticker,open,high,low,close,volume
2026-11-03T09:35:00-05:00,SPY,580.10,580.55,579.90,580.40,1250000
```

## Filling in the placeholders

Each `[PLACEHOLDER]` from the spec is a field in `TradingConfig`. Copy
`examples/config.example.json` and edit it, or override any field with `TRADING_<FIELD>` env vars:

```bash
TRADING_TICKERS=SPY,QQQ TRADING_ACCOUNT_SIZE=50000 python -m trading --config my.json demo
```

| Spec placeholder | Field |
|---|---|
| `[BROKER/PLATFORM]` | `broker` |
| `[TICKERS/MARKET]` | `tickers` |
| `[9:45 AM–3:45 PM ET]` | `trade_start`, `trade_end` |
| `$[X]` | `account_size` |
| `[1]%` / `[3]%` | `risk_per_trade_pct` / `max_daily_loss_pct` |
| `[3]` positions / `[10]` trades | `max_open_positions` / `max_trades_per_day` |
| `[1.5:1]` | `min_reward_risk` (`target_r_multiple` sets where targets go, default 2R) |
| `[Describe your setup]` | `strategy_description` (the prompt) and `strategy.py` (the deterministic rules) |
| FOMC / CPI / earnings | `news_events`. **You have to keep this calendar up to date yourself.** |

## Deciders (`decider`)

- **`rules`** (default): the VWAP-reclaim, EMA 9/21 and volume setup in `strategy.py`. Deterministic and free.
- **`claude`**: Claude (`claude-opus-5-5`, low effort) gets the cycle inputs as JSON. Its answer is
  constrained to the decision JSON schema with structured outputs, and server-side refusal
  fallback is turned on.
- **`consensus`**: trades only when the rules and Claude agree on direction, using the
  deterministic stop and target. This is the recommended way to use Claude.

Each decider's output goes through the same `RiskManager`.

## Output

Each cycle returns the spec's JSON:

```json
{"action": "BUY", "ticker": "SPY", "quantity": 43, "entry": 573.82, "stop_loss": 571.08,
 "take_profit": 579.29, "confidence": 77, "reason": "Price reclaimed VWAP with volume above ..."}
```

`trading_logs/<date>.jsonl` gets one line per decision: what the decider proposed, what was
executed, which rules blocked it, and any size or price changes. Closed trades and the
end-of-day summary are logged there too. The summary covers trades taken, win rate, P&L,
best and worst trade, blocked proposals, and an independent check for rule violations.

## Going live (read before setting `LIVE=true`)

1. Subclass `broker.LiveBroker` for your broker. Send every entry as **one bracket/OCO order**
   so the stop exists at the broker before the entry can fill. Rebuild positions and P&L from
   the broker's data, not from local state.
2. Feed real-time 5-minute bars into `TradingAssistant.on_snapshot(snapshot)` (use
   `indicators.build_snapshot`) and call `end_of_day()` after the close.
3. Paper trade for weeks first. Know the limits of the simulation: fills happen exactly at
   stop and target prices (no slippage, gaps or partial fills), and when one bar touches both
   the stop and the target, it assumes the stop filled first.
