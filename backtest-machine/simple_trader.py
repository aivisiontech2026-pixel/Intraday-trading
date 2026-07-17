"""
Simple Intraday Trader - Market Open Strategy
==============================================
Buy at 09:30 IST (market open), target 20-30 points profit by 15:15.

Strategy:
  - Entry: 09:30 AM IST (market open)
  - Exit: +20-30 points profit OR -1% stop loss OR 15:15 square-off
  - Stocks: RELIANCE, HDFCBANK, ICICIBANK, SBIN (most liquid)
  - Max 4 positions, Rs.25k notional each

State persists in simple_trades.db.
"""

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import yfinance as yf

HERE = Path(__file__).parent
DB = HERE / "simple_trades.db"
CFG_FILE = HERE / "intraday_config.json"
CFG = json.loads(CFG_FILE.read_text()) if CFG_FILE.exists() else {}

CAPITAL = 100_000
MAX_PER_TRADE = 25_000
PROFIT_POINTS = 25  # target profit in rupees per share
STOP_LOSS_PCT = -0.01  # -1% stop
T_ENTRY = 9 * 60 + 30  # 09:30 IST
T_SQUARE_OFF = 15 * 60 + 15  # 15:15 IST
SYMBOLS = ["RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS"]

# ----------------------------------------------------------------- db ---
def db_init():
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS positions(
        symbol TEXT PRIMARY KEY, qty INTEGER, entry REAL, entry_time TEXT);
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, qty INTEGER, entry REAL, exit REAL,
        entry_time TEXT, exit_time TEXT, pnl REAL, reason TEXT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    return conn

def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

def cash(conn):
    return float(meta_get(conn, "cash", CAPITAL))

# ----------------------------------------------------------------- telegram ---
def telegram(msg):
    tg = CFG.get("telegram", {})
    if not (tg.get("bot_token") and tg.get("chat_id")):
        return
    import requests
    try:
        requests.post(
            f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
            json={"chat_id": tg["chat_id"], "text": msg}, timeout=10)
    except Exception as e:
        print(f"  (telegram alert failed: {e})")

# ----------------------------------------------------------------- process ---
def process(conn, log, today):
    """Buy at market open, hold for profit target or stop loss."""
    now = datetime.now().astimezone()
    now_min = now.hour * 60 + now.minute

    # Fetch prices
    prices = {}
    for sym in SYMBOLS:
        try:
            df = yf.download(sym, period="1d", interval="1m",
                           auto_adjust=True, progress=False, multi_level_index=False)
            if df is not None and not df.empty:
                prices[sym] = float(df["Close"].iloc[-1])
        except:
            pass

    # Get existing positions
    positions = {r[0]: {"qty": r[1], "entry": r[2], "entry_time": r[3]}
                for r in conn.execute("SELECT symbol,qty,entry,entry_time FROM positions").fetchall()}

    # ---- ENTRY: Buy at 09:30 AM (once per day per stock)
    if T_ENTRY <= now_min < T_ENTRY + 5:  # 5-min window after open
        for sym in SYMBOLS:
            if sym not in positions and sym in prices:
                entry_px = prices[sym]
                qty = int(MAX_PER_TRADE / entry_px)
                if qty > 0 and cash(conn) >= entry_px * qty * 1.0003:  # with costs
                    conn.execute("INSERT INTO positions VALUES(?,?,?,?)",
                               (sym, qty, entry_px, now.isoformat()))
                    meta_set(conn, "cash", cash(conn) - entry_px * qty * 1.0003)
                    msg = f"📈 BOUGHT {sym} x{qty} @ Rs.{entry_px:.2f} (entry at open)"
                    log.append(msg)
                    telegram(msg)

    # ---- EXIT: Profit target, stop loss, or market close
    for sym, pos in list(positions.items()):
        if sym not in prices:
            continue

        current_px = prices[sym]
        pnl = (current_px - pos["entry"]) * pos["qty"]
        pnl_pct = (current_px - pos["entry"]) / pos["entry"]
        reason = None

        # Check exit conditions
        if pnl >= PROFIT_POINTS * pos["qty"]:  # profit target hit
            reason = f"Profit target +{PROFIT_POINTS} points"
        elif pnl_pct <= STOP_LOSS_PCT:  # stop loss hit
            reason = f"Stop loss {STOP_LOSS_PCT*100:.0f}%"
        elif now_min >= T_SQUARE_OFF:  # market close
            reason = "Market close 15:15"

        if reason:
            conn.execute(
                "INSERT INTO trades(symbol,qty,entry,exit,entry_time,exit_time,pnl,reason) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (sym, pos["qty"], pos["entry"], current_px, pos["entry_time"],
                 now.isoformat(), pnl, reason))
            conn.execute("DELETE FROM positions WHERE symbol=?", (sym,))
            meta_set(conn, "cash", cash(conn) + current_px * pos["qty"] * 0.9997)

            emoji = "✅" if pnl > 0 else "❌"
            msg = f"{emoji} SOLD {sym} x{pos['qty']} @ Rs.{current_px:.2f} | P&L Rs.{pnl:,.0f} ({pnl_pct:+.2%}) | {reason}"
            log.append(msg)
            telegram(msg)
            del positions[sym]

    conn.commit()
    return positions

# ----------------------------------------------------------------- main ---
def main():
    conn = db_init()
    today = date.today()
    log = []

    # Send market open message once per day
    if not meta_get(conn, f"market_open:{today}"):
        msg = f"🔔 SIMPLE TRADER STARTED | {today} 09:30 IST\n💰 Capital: Rs.{cash(conn):,.0f}"
        telegram(msg)
        meta_set(conn, f"market_open:{today}", "1")

    # Process
    process(conn, log, today)

    # Show status
    positions = conn.execute("SELECT symbol,qty,entry FROM positions").fetchall()
    if not positions:
        status = f"[{datetime.now():%H:%M}] Simple trader: no open positions | Cash: Rs.{cash(conn):,.0f}"
    else:
        status = f"[{datetime.now():%H:%M}] Simple trader: {len(positions)} position(s)"

    print(status)
    for sym, qty, entry in positions:
        print(f"  {sym} x{qty} @ {entry:.2f}")

    if log:
        print("\n".join(log))
        telegram("\n".join(log))

    # All-time P&L
    n, total = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM trades").fetchone()
    print(f"Total: {n} trades | P&L Rs.{total:,.0f}")

    conn.close()

if __name__ == "__main__":
    main()
