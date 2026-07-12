"""
The Backtest Machine - Indian Stock Market Edition
===================================================
Backtests long-only strategies on NSE stocks, indices and ETFs, per the
spec in "The_Backtest_Machine_Indian_Stock_Market_Edition.docx".

Strategies (daily timeframe)
----------------------------
- ema_crossover : buy EMA20 x-above EMA50, exit on cross back down
- supertrend_ema: buy Supertrend(10,3) bullish + close > EMA50,
                  exit when Supertrend turns bearish
- rsi_ema       : buy RSI(14) x-above 55 while close > EMA50,
                  exit RSI x-below 45
- volume_breakout: buy close > 20-day high on volume > 1.5x 20-day avg,
                  exit on close < EMA20 (skipped for volume-less indices)

All strategies share: ATR(14) x 2 stop loss, 1:2 risk:reward target,
signals on close executed at next day's open.

Risk management (from the doc)
------------------------------
- Initial capital : Rs. 1,00,000 per symbol
- Risk per trade  : 1% of current equity
- Costs           : 0.03% brokerage + exchange/GST/SEBI/stamp approximation
                    (configurable, default 0.10% per side for delivery trades)

Usage
-----
    python backtest.py                               # all strategies, default symbols
    python backtest.py --strategy ema_crossover      # one strategy
    python backtest.py RELIANCE.NS TCS.NS            # specific Yahoo symbols
"""

import sys
import math
import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------- config ---
INITIAL_CAPITAL = 1_000_000        # Rs. 10,00,000
RISK_PER_TRADE = 0.01              # 1% of equity
COST_PER_SIDE = 0.0010             # 0.10% per side (brokerage+STT+GST+stamp approx)
ATR_MULT = 2.0
RR = 2.0                           # risk:reward 1:2
EMA_FAST, EMA_SLOW, ATR_LEN = 20, 50, 14
START, END = "2021-06-01", None    # warm-up before Jan 2022
BACKTEST_FROM = "2022-01-01"
TRADING_DAYS = 252

SYMBOLS = {
    "^NSEI": "NIFTY 50",
    "^NSEBANK": "BANK NIFTY",
    "RELIANCE.NS": "RELIANCE",
    "TCS.NS": "TCS",
    "INFY.NS": "INFY",
    "HDFCBANK.NS": "HDFCBANK",
    "ICICIBANK.NS": "ICICIBANK",
    "SBIN.NS": "SBIN",
    "NIFTYBEES.NS": "NIFTYBEES",
}

# ------------------------------------------------------------- indicators ---
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def atr(df: pd.DataFrame, n: int) -> pd.Series:
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()

def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)

def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.Series:
    """Returns +1 (bullish) / -1 (bearish) direction series."""
    a = atr(df, n)
    mid = (df["High"] + df["Low"]) / 2
    upper = (mid + mult * a).to_numpy()
    lower = (mid - mult * a).to_numpy()
    close = df["Close"].to_numpy()
    direction = np.ones(len(df), dtype=int)
    ub, lb = upper[0], lower[0]
    for i in range(1, len(df)):
        ub = min(upper[i], ub) if close[i - 1] <= ub else upper[i]
        lb = max(lower[i], lb) if close[i - 1] >= lb else lower[i]
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < lb else 1
        else:
            direction[i] = 1 if close[i] > ub else -1
    return pd.Series(direction, index=df.index)

# ------------------------------------------------------------- strategies ---
# Each annotates df with boolean 'entry_sig' / 'exit_sig' columns.
def strat_ema_crossover(df):
    f, s = ema(df["Close"], EMA_FAST), ema(df["Close"], EMA_SLOW)
    df["entry_sig"] = (f > s) & (f.shift() <= s.shift())
    df["exit_sig"] = (f < s) & (f.shift() >= s.shift())

def strat_supertrend_ema(df):
    st = supertrend(df, 10, 3.0)
    e50 = ema(df["Close"], 50)
    df["entry_sig"] = (st == 1) & (st.shift() == -1) & (df["Close"] > e50)
    df["exit_sig"] = (st == -1) & (st.shift() == 1)

def strat_rsi_ema(df):
    r = rsi(df["Close"], 14)
    e50 = ema(df["Close"], 50)
    df["entry_sig"] = (r > 55) & (r.shift() <= 55) & (df["Close"] > e50)
    df["exit_sig"] = (r < 45) & (r.shift() >= 45)

def strat_volume_breakout(df):
    if "Volume" not in df or df["Volume"].fillna(0).sum() == 0:
        raise ValueError("no volume data")
    hh = df["High"].rolling(20).max()
    vavg = df["Volume"].rolling(20).mean()
    df["entry_sig"] = (df["Close"] > hh.shift()) & (df["Volume"] > 1.5 * vavg.shift())
    e20 = ema(df["Close"], EMA_FAST)
    df["exit_sig"] = df["Close"] < e20

STRATEGIES = {
    "ema_crossover": strat_ema_crossover,
    "supertrend_ema": strat_supertrend_ema,
    "rsi_ema": strat_rsi_ema,
    "volume_breakout": strat_volume_breakout,
}

# ---------------------------------------------------------------- engine ---
def run_backtest(df: pd.DataFrame, strategy_fn):
    df = df.copy()
    df["atr"] = atr(df, ATR_LEN)
    strategy_fn(df)
    df = df[df.index >= BACKTEST_FROM]

    equity = INITIAL_CAPITAL
    equity_curve, in_market = [], []
    trades = []
    pos = None  # dict(entry, stop, target, qty, date)
    pending_entry = False
    pending_exit = False

    for date, row in df.iterrows():
        # --- execute pending signals at today's open ---
        if pos and pending_exit:
            exit_px = row["Open"]
            equity += close_trade(trades, pos, exit_px, date, "Exit signal")
            pos = None
        pending_exit = False

        if pos is None and pending_entry and not math.isnan(row["atr"]):
            entry = row["Open"]
            stop_dist = ATR_MULT * row["atr"]
            if stop_dist > 0:
                qty = int((equity * RISK_PER_TRADE) / stop_dist)
                max_affordable = int(equity / (entry * (1 + COST_PER_SIDE)))
                qty = min(qty, max_affordable)
                if qty > 0:
                    cost = entry * qty * COST_PER_SIDE
                    equity -= cost
                    pos = {"entry": entry, "stop": entry - stop_dist,
                           "target": entry + RR * stop_dist, "qty": qty,
                           "date": date, "entry_cost": cost}
        pending_entry = False

        # --- intraday stop / target checks on current bar ---
        if pos:
            if row["Low"] <= pos["stop"]:
                equity += close_trade(trades, pos, pos["stop"], date, "Stop loss")
                pos = None
            elif row["High"] >= pos["target"]:
                equity += close_trade(trades, pos, pos["target"], date, "Target")
                pos = None

        # --- signals computed on close, acted on next open ---
        if pos is None and row["entry_sig"]:
            pending_entry = True
        if pos and row["exit_sig"]:
            pending_exit = True

        mtm = equity + (pos["qty"] * (row["Close"] - pos["entry"]) if pos else 0)
        equity_curve.append((date, mtm))
        in_market.append(pos is not None)

    # close any open position at last close
    if pos:
        last_date = df.index[-1]
        equity += close_trade(trades, pos, df["Close"].iloc[-1], last_date, "End of backtest")
        equity_curve[-1] = (last_date, equity)

    ec = pd.Series(dict(equity_curve)).sort_index()
    return compute_metrics(ec, trades, np.mean(in_market)), trades, ec


def close_trade(trades, pos, exit_px, date, reason):
    gross = (exit_px - pos["entry"]) * pos["qty"]
    exit_cost = exit_px * pos["qty"] * COST_PER_SIDE
    net = gross - exit_cost  # entry cost already deducted from equity
    trades.append({
        "entry_date": pos["date"].date(), "exit_date": date.date(),
        "entry": round(pos["entry"], 2), "exit": round(exit_px, 2),
        "qty": pos["qty"], "reason": reason,
        "pnl": round(net - pos["entry_cost"], 2),
    })
    return net

# --------------------------------------------------------------- metrics ---
def compute_metrics(ec: pd.Series, trades, exposure):
    rets = ec.pct_change().dropna()
    years = (ec.index[-1] - ec.index[0]).days / 365.25
    cagr = (ec.iloc[-1] / ec.iloc[0]) ** (1 / years) - 1 if years > 0 else 0

    peak = ec.cummax()
    dd = (ec - peak) / peak
    max_dd = dd.min()

    sharpe = rets.mean() / rets.std() * np.sqrt(TRADING_DAYS) if rets.std() > 0 else 0
    downside = rets[rets < 0].std()
    sortino = rets.mean() / downside * np.sqrt(TRADING_DAYS) if downside and downside > 0 else 0
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls) if pnls else 0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")

    return {
        "Final Equity": round(ec.iloc[-1]),
        "Net P&L": round(ec.iloc[-1] - INITIAL_CAPITAL),
        "CAGR %": round(cagr * 100, 2),
        "Win Rate %": round(win_rate * 100, 1),
        "Profit Factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
        "Max DD %": round(max_dd * 100, 2),
        "Sharpe": round(sharpe, 2),
        "Sortino": round(sortino, 2),
        "Calmar": round(calmar, 2),
        "Exposure %": round(exposure * 100, 1),
        "Trades": len(trades),
    }

# ------------------------------------------------------------------ main ---
def main():
    args = sys.argv[1:]
    strat_names = list(STRATEGIES)
    if "--strategy" in args:
        i = args.index("--strategy")
        strat_names = [args[i + 1]]
        args = args[:i] + args[i + 2:]
    symbols = args or list(SYMBOLS)

    # download once per symbol
    data = {}
    for sym in symbols:
        name = SYMBOLS.get(sym, sym)
        try:
            df = yf.download(sym, start=START, end=END, auto_adjust=True,
                             progress=False, multi_level_index=False)
        except Exception as e:
            print(f"  {name}: download failed ({e})")
            continue
        if df is None or len(df) < EMA_SLOW + 10:
            print(f"  {name}: not enough data, skipped")
            continue
        data[name] = df

    pd.set_option("display.width", 200)
    all_trades = []
    comparison = {}

    for strat in strat_names:
        results = {}
        for name, df in data.items():
            try:
                metrics, trades, _ = run_backtest(df, STRATEGIES[strat])
            except ValueError as e:
                print(f"  [{strat}] {name}: skipped ({e})")
                continue
            results[name] = metrics
            for t in trades:
                t["symbol"], t["strategy"] = name, strat
            all_trades.extend(trades)

        if not results:
            continue
        table = pd.DataFrame(results).T
        print(f"\n=== {strat} | Jan 2022 - present | Daily | "
              f"Rs.{INITIAL_CAPITAL:,} per symbol | 1% risk/trade ===\n")
        print(table.to_string())
        table.to_csv(f"results_{strat}.csv")
        comparison[strat] = {
            "Total Net P&L": table["Net P&L"].sum(),
            "Avg CAGR %": round(table["CAGR %"].mean(), 2),
            "Avg Win Rate %": round(table["Win Rate %"].mean(), 1),
            "Avg Max DD %": round(table["Max DD %"].mean(), 2),
            "Avg Sharpe": round(table["Sharpe"].mean(), 2),
            "Profitable Symbols": f"{(table['Net P&L'] > 0).sum()}/{len(table)}",
            "Total Trades": int(table["Trades"].sum()),
        }

    if len(comparison) > 1:
        print("\n=== STRATEGY COMPARISON (across all symbols) ===\n")
        comp = pd.DataFrame(comparison).T
        print(comp.to_string())
        comp.to_csv("comparison.csv")

    trades_df = pd.DataFrame(all_trades)
    trades_df.to_csv("trades.csv", index=False)
    print(f"\nSaved {len(trades_df)} trades to trades.csv")


if __name__ == "__main__":
    main()
