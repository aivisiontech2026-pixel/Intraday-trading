# The Backtest Machine — Indian Stock Market Edition

Python implementation of the backtesting spec in
`The_Backtest_Machine_Indian_Stock_Market_Edition.docx`.

## What it does

Backtests four long-only strategies (daily bars) on NSE stocks, indices
and ETFs from **Jan 2022 to present** using free Yahoo Finance data.

| Strategy | Entry | Exit signal |
|---|---|---|
| `ema_crossover` | EMA20 crosses above EMA50 | EMA20 crosses below EMA50 |
| `supertrend_ema` | Supertrend(10,3) turns bullish + close > EMA50 | Supertrend turns bearish |
| `rsi_ema` | RSI(14) crosses above 55 + close > EMA50 | RSI crosses below 45 |
| `volume_breakout` | Close > 20-day high on volume > 1.5× avg | Close < EMA20 |

All strategies share:

- **Stop loss**: entry − 2 × ATR(14)
- **Target**: 1:2 risk:reward
- **Position sizing**: 1% of equity risked per trade
- **Capital**: ₹10,00,000 per symbol
- **Costs**: 0.10% per side (brokerage + STT + GST + stamp approximation)

Signals are computed on the close and executed at the **next day's open**
(no look-ahead). Stops/targets are checked against intraday high/low.

## Usage

```
python backtest.py                              # all strategies, default symbols
python backtest.py --strategy ema_crossover     # a single strategy
python backtest.py RELIANCE.NS TCS.NS           # any Yahoo Finance symbols
```

Outputs `results_<strategy>.csv` (per-symbol metrics: CAGR, win rate,
profit factor, max drawdown, Sharpe, Sortino, Calmar, exposure, trade
count), `comparison.csv` (strategy-level summary) and `trades.csv`
(every trade with entry/exit dates, prices and P&L).

## Paper / live trader

`paper_trader.py` implements the doc's automation workflow: fetch NSE
OHLC → indicators → signals → orders → SQLite trade state → Telegram
alert. Run it once per day **after market close** (15:30 IST):

```
python paper_trader.py            # process new bars, generate orders
python paper_trader.py --status   # portfolio snapshot only
```

- Signals fire on the close and execute at the **next day's open**.
  Missed days are caught up automatically on the next run.
- State lives in `trades.db` (SQLite). Delete it to restart.
- Settings are in `config.json`: strategy, symbols, capital, and the
  doc's risk guardrails (1% risk/trade, 2% max daily loss, 5% max
  weekly loss, max 5 open positions) — all enforced before new entries.
- **Telegram alerts**: create a bot via @BotFather, put `bot_token` and
  `chat_id` in `config.json`. Left blank = silently skipped.
- **Live mode (Zerodha)**: `pip install kiteconnect`, put your
  `api_key` and daily `access_token` in `config.json`, set
  `"mode": "live"`. Orders go in as AMO market orders (fill at next
  open, matching the simulation). Prove the strategy in paper mode
  first — live mode places real orders with real money.

To automate the daily run, schedule it for a weekday evening, e.g.:

```
schtasks /create /tn "BacktestMachine" /tr "python \"D:\stock market\backtest-machine\paper_trader.py\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 18:00
```

## Intraday system (5-minute)

A separate portfolio-level intraday machine — one shared ₹1,00,000 pool
across all symbols, the way a real intraday bot trades. Configured in
`intraday_config.json`.

**Entry** (all must hold on a 5-min close, fresh setup only):
EMA9 > EMA21 · close > session VWAP · RSI(14) 55–70 · MACD(12,26,9)
bullish · volume > 1.5× 20-bar average · NIFTY 50 5-min trend bullish ·
time 09:30–14:30 IST.

**Exit**: EMA9 crosses below EMA21, close below VWAP, RSI < 45,
1.5×ATR trailing stop, or forced square-off at 15:15 IST.

**Risk rules** (enforced before every entry): 1% risk/trade, max
₹25,000 notional/trade, max 4 open positions, −2% daily loss or +5%
daily profit stops all new entries for the day.

```
python intraday_backtest.py                # backtest, last ~60 days of 5m data
python intraday_trader.py                  # process new 5-min bars once
python intraday_trader.py --loop           # run all day, wakes every 5 min
python intraday_trader.py --status         # portfolio snapshot
```

- Backtest fills at the **next bar's open** (no look-ahead), with
  0.03%/side costs + 0.02% slippage. Outputs
  `intraday_trades_backtest.csv` and `intraday_daily_pnl.csv`.
- The trader keeps state in `intraday_trades.db`; missed runs catch up
  automatically, in-progress bars are ignored, and a wall-clock 15:15
  safety net squares off even if data is missing.
- Live mode places **MIS** market orders via Zerodha Kite (set
  `"mode": "live"` in `intraday_config.json`). Yahoo 5-min data lags
  ~1 min; for serious live use, switch data to the Kite websocket.
- Free Yahoo 5-minute history covers only ~60 days, so that's the
  backtest window.

## Requirements

```
pip install yfinance pandas numpy
```

## Notes & limitations

- Indices (^NSEI, ^NSEBANK) are included for reference but are not directly
  tradable. Use NIFTYBEES (or futures/options, not modelled here) for
  index exposure.
- The daily backtester covers Jan 2022–present; the intraday one is
  limited to the last ~60 days by free 5-minute data.
- Yahoo data is adjusted for splits/dividends (`auto_adjust=True`).
- This is a research tool, not investment advice.
