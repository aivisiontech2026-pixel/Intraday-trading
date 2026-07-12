"""
Paper/live trader - The Backtest Machine, Indian Stock Market Edition
=====================================================================
Daily runner implementing the doc's automation workflow:

    fetch NSE OHLC -> indicators -> signals -> orders -> SQLite state
    -> Telegram alert

Run it once per day AFTER market close (15:30 IST). It processes every
completed daily bar since the last run (missed days are caught up
automatically), so running it late or skipping a day is safe:

    python paper_trader.py            # process new bars, place orders
    python paper_trader.py --status   # show portfolio, no processing

Signals are generated on the close; entries/exits execute at the NEXT
day's open — in paper mode the fill is simulated on the next run, in
live mode an AMO market order is placed (fills at next open).

Risk guardrails (from the doc, enforced before any new entry):
  1% risk per trade, 2% max daily loss, 5% max weekly loss,
  max 5 open positions.

State lives in trades.db (SQLite). Delete it to restart from scratch.
"""

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from backtest import STRATEGIES, atr, ATR_LEN, ATR_MULT, RR
from brokers import get_broker

HERE = Path(__file__).parent
DB = HERE / "trades.db"
CONFIG = json.loads((HERE / "config.json").read_text())
LOOKBACK_DAYS = 250  # indicator warm-up

# ------------------------------------------------------------------ state ---
def db():
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS positions(
        symbol TEXT PRIMARY KEY, qty INTEGER, entry REAL, stop REAL,
        target REAL, entry_date TEXT, strategy TEXT);
    CREATE TABLE IF NOT EXISTS pending_orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT,
        created TEXT, reason TEXT, broker_order_id TEXT);
    CREATE TABLE IF NOT EXISTS closed_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, qty INTEGER,
        entry REAL, exit_px REAL, entry_date TEXT, exit_date TEXT,
        pnl REAL, reason TEXT, strategy TEXT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    return conn

def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

def cash(conn):
    return float(meta_get(conn, "cash", CONFIG["capital"]))

# ------------------------------------------------------------------ alerts ---
def telegram(msg):
    tg = CONFIG.get("telegram", {})
    if not (tg.get("bot_token") and tg.get("chat_id")):
        return
    import requests
    try:
        requests.post(
            f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
            json={"chat_id": tg["chat_id"], "text": msg}, timeout=10)
    except Exception as e:
        print(f"  (telegram alert failed: {e})")

# ------------------------------------------------------------- risk checks ---
def realized_pnl_since(conn, since: date):
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM closed_trades WHERE exit_date >= ?",
        (since.isoformat(),)).fetchone()
    return row[0]

def entries_allowed(conn, equity, today: date, log):
    npos = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    if npos >= CONFIG["max_open_positions"]:
        log.append(f"max open positions ({npos}) reached - no new entries")
        return False
    day_pnl = realized_pnl_since(conn, today)
    if day_pnl < -CONFIG["max_daily_loss"] * equity:
        log.append(f"daily loss limit hit (Rs.{day_pnl:,.0f}) - no new entries")
        return False
    week_pnl = realized_pnl_since(conn, today - timedelta(days=today.weekday()))
    if week_pnl < -CONFIG["max_weekly_loss"] * equity:
        log.append(f"weekly loss limit hit (Rs.{week_pnl:,.0f}) - no new entries")
        return False
    return True

# ------------------------------------------------------------------ engine ---
def close_position(conn, pos, exit_px, exit_date, reason, log):
    symbol, qty, entry = pos["symbol"], pos["qty"], pos["entry"]
    gross = (exit_px - entry) * qty
    costs = (entry + exit_px) * qty * CONFIG["cost_per_side"]
    pnl = gross - costs
    conn.execute(
        "INSERT INTO closed_trades(symbol,qty,entry,exit_px,entry_date,"
        "exit_date,pnl,reason,strategy) VALUES(?,?,?,?,?,?,?,?,?)",
        (symbol, qty, entry, exit_px, pos["entry_date"],
         exit_date.isoformat(), pnl, reason, pos["strategy"]))
    conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
    meta_set(conn, "cash", cash(conn) + exit_px * qty
             - exit_px * qty * CONFIG["cost_per_side"])
    log.append(f"CLOSED {symbol} x{qty} @ {exit_px:.2f} ({reason}) "
               f"P&L Rs.{pnl:,.0f}")

def process_symbol(conn, broker, symbol, df, today, log):
    """Walk each unprocessed completed bar for one symbol."""
    strat = STRATEGIES[CONFIG["strategy"]]
    df = df.copy()
    df["atr"] = atr(df, ATR_LEN)
    strat(df)

    last_key = f"last_processed:{symbol}"
    last = meta_get(conn, last_key, "1970-01-01")
    new_bars = df[df.index > last]

    for ts, row in new_bars.iterrows():
        bar_date = ts.date()
        pos = conn.execute(
            "SELECT symbol,qty,entry,stop,target,entry_date,strategy "
            "FROM positions WHERE symbol=?", (symbol,)).fetchone()
        pos = dict(zip(
            ["symbol", "qty", "entry", "stop", "target", "entry_date",
             "strategy"], pos)) if pos else None

        # 1. fill pending orders at this bar's open
        for oid, side, reason in conn.execute(
                "SELECT id,side,reason FROM pending_orders WHERE symbol=? "
                "AND created < ?", (symbol, ts.strftime("%Y-%m-%d"))).fetchall():
            if side == "SELL" and pos:
                close_position(conn, pos, row["Open"], bar_date, reason, log)
                pos = None
            elif side == "BUY" and pos is None:
                equity = cash(conn)
                stop_dist = ATR_MULT * row["atr"]
                if stop_dist > 0:
                    qty = int(equity * CONFIG["risk_per_trade"] / stop_dist)
                    qty = min(qty, int(equity / (row["Open"] *
                                                 (1 + CONFIG["cost_per_side"]))))
                    if qty > 0:
                        entry = row["Open"]
                        conn.execute(
                            "INSERT INTO positions VALUES(?,?,?,?,?,?,?)",
                            (symbol, qty, entry, entry - stop_dist,
                             entry + RR * stop_dist, bar_date.isoformat(),
                             CONFIG["strategy"]))
                        meta_set(conn, "cash", equity - entry * qty
                                 - entry * qty * CONFIG["cost_per_side"])
                        pos = {"symbol": symbol, "qty": qty, "entry": entry,
                               "stop": entry - stop_dist,
                               "target": entry + RR * stop_dist,
                               "entry_date": bar_date.isoformat(),
                               "strategy": CONFIG["strategy"]}
                        log.append(f"OPENED {symbol} x{qty} @ {entry:.2f} "
                                   f"stop {pos['stop']:.2f} "
                                   f"target {pos['target']:.2f}")
            conn.execute("DELETE FROM pending_orders WHERE id=?", (oid,))

        # 2. stop / target on this bar
        if pos:
            if row["Low"] <= pos["stop"]:
                close_position(conn, pos, pos["stop"], bar_date, "Stop loss", log)
                pos = None
            elif row["High"] >= pos["target"]:
                close_position(conn, pos, pos["target"], bar_date, "Target", log)
                pos = None

        # 3. signals on close -> pending orders for next open
        equity = cash(conn) + (pos["qty"] * row["Close"] if pos else 0)
        if pos and row["exit_sig"]:
            oid = broker.place_order(symbol, "SELL", pos["qty"])
            conn.execute(
                "INSERT INTO pending_orders(symbol,side,created,reason,"
                "broker_order_id) VALUES(?,?,?,?,?)",
                (symbol, "SELL", bar_date.isoformat(), "Exit signal", str(oid)))
            log.append(f"SIGNAL exit {symbol} (close {row['Close']:.2f}) "
                       f"-> SELL at next open [{broker.name}]")
        elif pos is None and row["entry_sig"] and not row.isna()["atr"]:
            if entries_allowed(conn, equity, bar_date, log):
                stop_dist = ATR_MULT * row["atr"]
                est_qty = int(equity * CONFIG["risk_per_trade"] / stop_dist) \
                    if stop_dist > 0 else 0
                if est_qty > 0:
                    oid = broker.place_order(symbol, "BUY", est_qty)
                    conn.execute(
                        "INSERT INTO pending_orders(symbol,side,created,"
                        "reason,broker_order_id) VALUES(?,?,?,?,?)",
                        (symbol, "BUY", bar_date.isoformat(), "Entry signal",
                         str(oid)))
                    log.append(f"SIGNAL entry {symbol} (close "
                               f"{row['Close']:.2f}) -> BUY ~{est_qty} at "
                               f"next open [{broker.name}]")

        meta_set(conn, last_key, ts.strftime("%Y-%m-%d"))

# ------------------------------------------------------------------ status ---
def show_status(conn, prices=None):
    print(f"\n=== Portfolio status ({CONFIG['mode']} mode, "
          f"strategy: {CONFIG['strategy']}) ===")
    print(f"Cash: Rs.{cash(conn):,.0f}")
    rows = conn.execute("SELECT * FROM positions").fetchall()
    if rows:
        print("\nOpen positions:")
        for r in rows:
            line = (f"  {r[0]}: {r[1]} @ {r[2]:.2f} | stop {r[3]:.2f} | "
                    f"target {r[4]:.2f} | since {r[5]}")
            if prices and r[0] in prices:
                line += f" | last {prices[r[0]]:.2f}"
            print(line)
    else:
        print("No open positions.")
    pend = conn.execute(
        "SELECT symbol,side,created,reason FROM pending_orders").fetchall()
    if pend:
        print("\nPending orders (execute at next open):")
        for p in pend:
            print(f"  {p[1]} {p[0]} ({p[3]}, signalled {p[2]})")
    n, total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM closed_trades").fetchone()
    print(f"\nClosed trades: {n} | Realized P&L: Rs.{total:,.0f}")

# -------------------------------------------------------------------- main ---
def main():
    conn = db()
    broker = get_broker(CONFIG)
    today = date.today()

    if "--status" in sys.argv:
        show_status(conn)
        return

    if CONFIG["mode"] == "live":
        print("*** LIVE MODE - real orders will be placed ***")

    start = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    log = []
    prices = {}
    for symbol in CONFIG["symbols"]:
        try:
            df = yf.download(symbol, start=start, auto_adjust=True,
                             progress=False, multi_level_index=False)
        except Exception as e:
            print(f"  {symbol}: download failed ({e})")
            continue
        if df is None or df.empty:
            print(f"  {symbol}: no data")
            continue
        prices[symbol] = float(df["Close"].iloc[-1])
        process_symbol(conn, broker, symbol, df, today, log)

    conn.commit()

    if log:
        print("\n".join(log))
        telegram("Backtest Machine (" + CONFIG["mode"] + "):\n" + "\n".join(log))
    else:
        print("No new bars / signals since last run.")

    show_status(conn, prices)
    conn.close()


if __name__ == "__main__":
    main()
