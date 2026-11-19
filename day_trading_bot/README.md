# 🤖 Grok Day Trading Bot

An automated intraday stock trading bot. A technical strategy finds setups,
**your Grok bot (xAI) reviews every trade and approves or rejects it**, a risk
manager sizes the position, and the order goes to **Alpaca** with a stop-loss
and take-profit attached on the broker side.

> ⚠️ **Read this first.** Day trading loses most people money. This bot is not
> financial advice, and nothing guarantees profit, Grok included. It starts in
> **paper trading (fake money)**. Run it on paper for weeks before you think
> about real money, and never trade money you can't afford to lose.

## How a trade happens

```
every 60s ─► scan watchlist (5-min candles)
              │
              ▼
   Strategy: EMA9/21 trend + price vs VWAP + RSI + MACD + volume spike
              │ setup found
              ▼
   Grok: gets candles, indicators, account P&L, today's trades
         → {"decision": "APPROVE"/"REJECT", "confidence": 0-100, ...}
              │ approved and confidence ≥ GROK_MIN_CONFIDENCE
              ▼
   Risk manager: size = 0.5% of equity ÷ stop distance, capped
              │
              ▼
   Alpaca bracket order: market entry + stop-loss + take-profit
```

**Built-in safety:**
- Paper trading by default. Real money needs two `.env` flags **and** typing `YES`.
- Daily loss circuit breaker: flattens everything and stops for the day at −2%.
- Closes every position 10 minutes before the bell (no overnight holds).
- Skips the first 15 minutes after the open.
- Max open positions, max trades per day, max position size.
- Respects the Pattern Day Trader rule (accounts under $25k).
- If Grok is down or gives a reply the bot can't parse, the trade is **skipped**, never forced.
- Every signal, Grok verdict and order is logged to `logs/trades.csv`.

## Setup (about 10 minutes)

1. **Get keys**
   - Grok: https://console.x.ai → create an API key
   - Alpaca: https://alpaca.markets → free account → **Paper Trading** → API keys
2. **Install**
   ```bash
   cd day_trading_bot
   python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env        # then paste your keys into .env
   ```
3. **Test it**
   ```bash
   python run.py check                     # verifies Alpaca + Grok keys
   python run.py backtest --demo           # try it on fake data, no keys needed
   python run.py backtest --days 30        # backtest on real market history
   ```
4. **Run it**
   ```bash
   python run.py paper --dry-run   # watches the live market, logs what it WOULD do
   python run.py paper             # trades your Alpaca paper account
   ```

Run it during market hours (9:30–4:00 ET). Leave it running all day. When the
market is closed it sleeps until the next open.

## Uploading your own Grok bot

All of Grok's instructions live in **`grok_prompt.md`**. To use your own Grok
bot, paste its system prompt/personality into that file. The only rule is that
it must still reply with the JSON shape at the bottom of the file:

```json
{"decision": "APPROVE", "confidence": 78, "stop_loss": null, "take_profit": null, "reason": "..."}
```

You can also keep several prompts and switch with `GROK_PROMPT_FILE=my_bot.md`
in `.env`. Change the model with `GROK_MODEL` (default `grok-4`).

Grok can suggest a tighter stop or a different target. The bot only uses them
if they make sense: a stop on the correct side of entry and no looser than the
original.

## Settings (`.env`)

| Setting | Default | What it does |
|---|---|---|
| `WATCHLIST` | SPY,QQQ,AAPL,NVDA,TSLA,AMD,MSFT | Symbols to scan |
| `USE_GROK` | true | false = trade on technical signals alone |
| `GROK_MIN_CONFIDENCE` | 65 | Grok must be at least this sure |
| `RISK_PER_TRADE_PCT` | 0.5 | % of equity lost if the stop is hit |
| `MAX_DAILY_LOSS_PCT` | 2.0 | Stop trading for the day after this loss |
| `MAX_OPEN_POSITIONS` | 3 | Concurrent positions |
| `MAX_TRADES_PER_DAY` | 10 | Entries per day |
| `ATR_STOP_MULT` | 1.5 | Stop distance = ATR × this |
| `REWARD_RISK` | 2.0 | Target distance = stop distance × this |
| `ALLOW_SHORTS` | false | Enable short selling |
| `LIVE_TRADING` + `I_UNDERSTAND_THE_RISK` | false | Both true = real money |

## Backtesting

```bash
python run.py backtest --demo --days 30                 # synthetic data
python run.py backtest --days 60 --symbols SPY,NVDA     # Alpaca history
python run.py backtest --csv data/SPY.csv               # your own CSV
python run.py backtest --days 10 --grok                 # include Grok (uses API credits!)
```

CSV format: `timestamp,open,high,low,close,volume` (ISO timestamps, UTC).
Backtests fill at the bar close plus $0.01 slippage. When a bar touches both
the stop and the target, the stop counts first. Real fills are usually worse
than backtests, so treat good results with suspicion.

## Running it 24/7 (optional)

Any always-on machine works, for example a $5/month VPS:

```bash
nohup python run.py paper > logs/console.log 2>&1 &
```

## Project layout

```
run.py               CLI: check | backtest | paper | live
grok_prompt.md       ← your Grok bot's instructions
tradebot/
  strategy.py        setup detection
  grok.py            xAI API call + JSON verdict parsing
  risk.py            position sizing + daily limits
  broker.py          Alpaca client + backtest simulator
  bot.py             live trading loop
  backtest.py        historical replay
  indicators.py      EMA, RSI, ATR, VWAP, MACD (pure Python)
tests/               pytest suite (pip install pytest && pytest)
```
