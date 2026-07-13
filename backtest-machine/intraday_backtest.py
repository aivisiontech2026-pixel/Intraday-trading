"""
Intraday Backtest - The Backtest Machine, Indian Stock Market Edition
=====================================================================
Portfolio-level 5-minute intraday backtester for NSE stocks. Unlike
backtest.py (per-symbol daily swing), this runs ONE shared capital pool
across all symbols simultaneously, exactly as a live intraday bot would.

Strategy (all conditions must hold on a 5-min close)
----------------------------------------------------
Entry (long only):
  - EMA9 > EMA21
  - Close above session VWAP
  - RSI(14) between 55 and 70
  - MACD(12,26) above signal(9)
  - Volume > 1.5x average of previous 20 bars
  - NIFTY 50 trend bullish (EMA9 > EMA21 and above VWAP on ^NSEI 5m)
  - Fresh setup only: the full condition set was NOT true on the
    previous bar (prevents re-entering every bar of a trend)
  - Time window: entries allowed 09:30 - 14:30 IST only

Exit (whichever comes first):
  - EMA9 crosses below EMA21
  - Close below VWAP
  - RSI(14) < 45
  - ATR trailing stop hit (initial stop = entry - 1.5 x ATR(14),
    trailed up to high - 1.5 x ATR as the trade moves in favour)
  - Forced square-off at 15:15 IST (flat before 15:20, always)

Risk management (portfolio level, enforced before every entry)
--------------------------------------------------------------
  - Capital            : Rs. 1,00,000 (one shared pool)
  - Risk per trade     : 1% of equity
  - Max capital/trade  : Rs. 25,000 notional
  - Max open positions : 4
  - Daily loss limit   : -2% -> no new entries for the rest of the day
  - Daily profit stop  : +5% -> no new entries for the rest of the day
  - Costs              : 0.03%/side + 0.02% slippage (intraday MIS approx)

Signals fire on a completed bar's close and fill at the NEXT bar's open
(no look-ahead). Stops fill at the stop price within the bar.

Data: Yahoo Finance 5-minute bars - free history is limited to the last
~60 days, so that is the backtest window.

Usage
-----
    python intraday_backtest.py                    # symbols from intraday_config.json
    python intraday_backtest.py RELIANCE.NS LT.NS  # specific symbols
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from backtest import ema, atr, rsi

HERE = Path(__file__).parent
CFG_FILE = HERE / "intraday_config.json"
CFG = json.loads(CFG_FILE.read_text()) if CFG_FILE.exists() else {}

CAPITAL = CFG.get("capital", 100_000)
RISK_PCT = CFG.get("risk_per_trade_percent", 1) / 100
MAX_PER_TRADE = CFG.get("max_capital_per_trade", 25_000)
MAX_POSITIONS = CFG.get("max_open_positions", 4)
MAX_DAY_LOSS = CFG.get("max_daily_loss_percent", 2) / 100
MAX_DAY_PROFIT = CFG.get("max_daily_profit_percent", 5) / 100
COST_PER_SIDE = CFG.get("cost_per_side", 0.0003)
SLIPPAGE = CFG.get("slippage", 0.0002)
TRAIL_MULT = CFG.get("atr_trail_mult", 1.5)
ATR_LEN = 14
ENTRY_START = CFG.get("entry_start", "09:30")
ENTRY_END = CFG.get("entry_end", "14:30")
SQUARE_OFF = CFG.get("square_off", "15:15")
INTERVAL = CFG.get("interval", "5m")
PERIOD = CFG.get("period", "60d")
NIFTY = "^NSEI"

SYMBOLS = CFG.get("symbols", [
    "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS",
    "TCS.NS", "INFY.NS", "AXISBANK.NS", "LT.NS",
])

def _t(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

T_ENTRY_START, T_ENTRY_END, T_SQUARE_OFF = map(_t, (ENTRY_START, ENTRY_END, SQUARE_OFF))

def bar_minutes(ts):
    return ts.hour * 60 + ts.minute

# ------------------------------------------------------------- indicators ---
def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Annotate a 5-min OHLCV frame with all strategy columns."""
    df = df.copy()
    df["ema9"] = ema(df["Close"], 9)
    df["ema21"] = ema(df["Close"], 21)
    df["rsi"] = rsi(df["Close"], 14)
    macd_line = ema(df["Close"], 12) - ema(df["Close"], 26)
    df["macd"], df["macd_sig"] = macd_line, ema(macd_line, 9)
    df["atr"] = atr(df, ATR_LEN)

    # session VWAP - resets every trading day
    day = df.index.date
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = (tp * df["Volume"]).groupby(day).cumsum()
    vv = df["Volume"].groupby(day).cumsum().replace(0, np.nan)
    df["vwap"] = pv / vv

    vavg = df["Volume"].rolling(20).mean().shift()
    df["vol_ok"] = df["Volume"] > 1.5 * vavg

    # raw entry confluence (NIFTY filter merged in by the engine)
    df["cond_raw"] = ((df["ema9"] > df["ema21"])
                      & (df["Close"] > df["vwap"])
                      & df["rsi"].between(55, 70)
                      & (df["macd"] > df["macd_sig"])
                      & df["vol_ok"])

    ema_cross_dn = (df["ema9"] < df["ema21"]) & (df["ema9"].shift() >= df["ema21"].shift())
    df["exit_sig"] = ema_cross_dn | (df["Close"] < df["vwap"]) | (df["rsi"] < 45)
    return df

def nifty_bull(df: pd.DataFrame) -> pd.Series:
    """Bullish regime filter on NIFTY 50 5-min bars (no volume on index)."""
    e9, e21 = ema(df["Close"], 9), ema(df["Close"], 21)
    day = df.index.date
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    counts = pd.Series(1, index=df.index).groupby(day).cumsum()
    vwap = tp.groupby(day).cumsum() / counts        # volume-less proxy VWAP
    return (e9 > e21) & (df["Close"] > vwap)

# ------------------------------------------------------------------ data ---
def fetch(symbol):
    df = yf.download(symbol, period=PERIOD, interval=INTERVAL,
                     auto_adjust=True, progress=False, multi_level_index=False)
    if df is None or df.empty:
        return None
    return df.dropna(subset=["Open", "High", "Low", "Close"])

# ---------------------------------------------------------------- engine ---
def run(symbols=None):
    symbols = symbols or SYMBOLS

    ndf = fetch(NIFTY)
    if ndf is None:
        raise SystemExit("could not download NIFTY 5-min data")
    regime = nifty_bull(ndf)

    frames = {}
    for sym in symbols:
        df = fetch(sym)
        if df is None or len(df) < 60:
            print(f"  {sym}: no/insufficient 5-min data, skipped")
            continue
        df = prepare(df)
        # align NIFTY regime to this symbol's timestamps (last known value)
        nb = regime.reindex(df.index, method="ffill").fillna(False)
        full = df["cond_raw"] & nb
        df["entry_sig"] = full & ~full.shift(fill_value=False)
        frames[sym] = df

    if not frames:
        raise SystemExit("no data for any symbol")

    timeline = sorted(set().union(*[set(df.index) for df in frames.values()]))
    index_sets = {s: set(df.index) for s, df in frames.items()}

    cash = float(CAPITAL)
    positions = {}          # sym -> dict
    pending = {}            # sym -> "BUY" | "SELL"
    trades = []
    equity_curve = []
    cur_day, day_start_eq, day_blocked, block_reason = None, CAPITAL, False, ""

    def last_closes(ts):
        return {s: frames[s]["Close"].asof(ts) for s in positions}

    def equity(ts):
        return cash + sum(positions[s]["qty"] * px
                          for s, px in last_closes(ts).items() if not np.isnan(px))

    def fill(px, side):
        return px * (1 + SLIPPAGE) if side == "BUY" else px * (1 - SLIPPAGE)

    def close_pos(sym, px, ts, reason):
        nonlocal cash
        pos = positions.pop(sym)
        exit_px = fill(px, "SELL")
        proceeds = exit_px * pos["qty"] * (1 - COST_PER_SIDE)
        cash += proceeds
        pnl = proceeds - pos["outlay"]
        trades.append({
            "symbol": sym, "date": ts.date(),
            "entry_time": pos["time"].strftime("%H:%M"),
            "exit_time": ts.strftime("%H:%M"),
            "entry": round(pos["entry"], 2), "exit": round(exit_px, 2),
            "qty": pos["qty"], "reason": reason, "pnl": round(pnl, 2),
        })

    for ts in timeline:
        # ---- new trading day: reset daily circuit breakers ----
        if ts.date() != cur_day:
            cur_day = ts.date()
            day_start_eq = equity(ts)
            day_blocked, block_reason = False, ""
            pending.clear()

        minutes = bar_minutes(ts)

        for sym, df in frames.items():
            if ts not in index_sets[sym]:
                continue
            row = df.loc[ts]
            pos = positions.get(sym)

            # 1. forced square-off window: exit everything, take no orders
            if minutes >= T_SQUARE_OFF:
                pending.pop(sym, None)
                if pos:
                    close_pos(sym, row["Open"], ts, "Square-off 15:15")
                continue

            # 2. fill pending orders at this bar's open
            side = pending.pop(sym, None)
            if side == "SELL" and pos:
                close_pos(sym, row["Open"], ts, "Exit signal")
                pos = None
            elif side == "BUY" and pos is None and not day_blocked \
                    and len(positions) < MAX_POSITIONS and not np.isnan(row["atr"]):
                entry = fill(row["Open"], "BUY")
                stop_dist = TRAIL_MULT * row["atr"]
                if stop_dist > 0:
                    eq = equity(ts)
                    qty = int(eq * RISK_PCT / stop_dist)
                    qty = min(qty, int(MAX_PER_TRADE / entry),
                              int(cash / (entry * (1 + COST_PER_SIDE))))
                    if qty > 0:
                        outlay = entry * qty * (1 + COST_PER_SIDE)
                        cash -= outlay
                        positions[sym] = {
                            "qty": qty, "entry": entry, "outlay": outlay,
                            "stop": entry - stop_dist, "time": ts,
                        }
                        pos = positions[sym]

            # 3. stop / trailing stop on this bar
            if pos:
                if row["Low"] <= pos["stop"]:
                    close_pos(sym, pos["stop"], ts, "Trailing stop")
                    pos = None
                elif not np.isnan(row["atr"]):
                    pos["stop"] = max(pos["stop"], row["High"] - TRAIL_MULT * row["atr"])

            # 4. signals on this close -> orders for next bar's open
            if pos and row["exit_sig"]:
                pending[sym] = "SELL"
            elif pos is None and row["entry_sig"] and not day_blocked \
                    and T_ENTRY_START <= minutes <= T_ENTRY_END:
                pending[sym] = "BUY"

        # ---- daily circuit breakers on marked-to-market equity ----
        eq = equity(ts)
        day_pnl = eq - day_start_eq
        if not day_blocked:
            if day_pnl <= -MAX_DAY_LOSS * day_start_eq:
                day_blocked, block_reason = True, "daily loss limit"
            elif day_pnl >= MAX_DAY_PROFIT * day_start_eq:
                day_blocked, block_reason = True, "daily profit target"
            if day_blocked:
                pending.clear()
        equity_curve.append((ts, eq))

    # safety: liquidate anything left (missing tail bars)
    for sym in list(positions):
        ts = frames[sym].index[-1]
        close_pos(sym, frames[sym]["Close"].iloc[-1], ts, "End of data")

    return trades, pd.Series(dict(equity_curve)).sort_index()

# --------------------------------------------------------------- reports ---
def report(trades, ec):
    if not trades:
        print("No trades taken in the backtest window.")
        return
    tdf = pd.DataFrame(trades)
    pnls = tdf["pnl"]
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    peak = ec.cummax()
    max_dd = ((ec - peak) / peak).min()
    daily = tdf.groupby("date")["pnl"].sum()

    days = (ec.index[-1].date() - ec.index[0].date()).days or 1
    print(f"\n=== Intraday backtest | {ec.index[0].date()} to {ec.index[-1].date()} "
          f"({len(daily)} trading days) | {INTERVAL} bars ===")
    print(f"Capital           : Rs.{CAPITAL:,}")
    print(f"Final equity      : Rs.{ec.iloc[-1]:,.0f}")
    print(f"Net P&L           : Rs.{ec.iloc[-1] - CAPITAL:,.0f} "
          f"({(ec.iloc[-1] / CAPITAL - 1) * 100:+.2f}%)")
    print(f"Trades            : {len(tdf)}  "
          f"(~{len(tdf) / max(len(daily), 1):.1f}/day)")
    print(f"Win rate          : {len(wins) / len(pnls) * 100:.1f}%")
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    print(f"Profit factor     : {pf:.2f}")
    print(f"Avg win / loss    : Rs.{wins.mean() if len(wins) else 0:,.0f} / "
          f"Rs.{losses.mean() if len(losses) else 0:,.0f}")
    print(f"Max drawdown      : {max_dd * 100:.2f}%")
    print(f"Best / worst day  : Rs.{daily.max():,.0f} / Rs.{daily.min():,.0f}")
    print(f"Green days        : {(daily > 0).sum()}/{len(daily)}")

    print("\nPer-symbol P&L:")
    print(tdf.groupby("symbol")["pnl"].agg(["count", "sum"])
          .rename(columns={"count": "trades", "sum": "pnl"})
          .sort_values("pnl", ascending=False).to_string())

    print("\nExit reasons:")
    print(tdf.groupby("reason")["pnl"].agg(["count", "sum"]).to_string())

    tdf.to_csv(HERE / "intraday_trades_backtest.csv", index=False)
    daily.to_csv(HERE / "intraday_daily_pnl.csv")
    print("\nSaved intraday_trades_backtest.csv and intraday_daily_pnl.csv")

def main():
    symbols = sys.argv[1:] or None
    trades, ec = run(symbols)
    report(trades, ec)

if __name__ == "__main__":
    main()
