"""
Intraday paper/live trader - The Backtest Machine
=================================================
Live runner for the 5-minute intraday strategy in intraday_backtest.py:

    fetch 5-min bars -> indicators -> confluence signals -> orders
    -> SQLite state -> Telegram alert

Run it every 5 minutes during market hours (09:15-15:30 IST):

    python intraday_trader.py            # process new bars once, exit
    python intraday_trader.py --loop     # keep running, wake every 5 min
    python intraday_trader.py --status   # portfolio snapshot only

Each run processes every completed-but-unprocessed 5-min bar, so a
missed run is caught up automatically. In-progress bars are ignored.

Paper mode fills at the signal bar's close (the price a market order
placed seconds after bar completion would roughly get). Live mode
places MIS market orders through Zerodha Kite.

Hard rules enforced every run:
  - entries only 09:30-14:30 IST
  - max 4 open positions, Rs.25,000 notional per trade, 1% risk/trade
  - -2% daily loss  -> no new entries for the day
  - +5% daily profit -> no new entries for the day
  - 15:15 IST -> square off EVERYTHING, no exceptions

State lives in intraday_trades.db. Delete it to restart from scratch.
"""

import json
import sqlite3
import sys
import time as time_mod
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import yfinance as yf

from intraday_backtest import (
    CFG, CAPITAL, RISK_PCT, MAX_PER_TRADE, MAX_POSITIONS, MAX_DAY_LOSS,
    MAX_DAY_PROFIT, COST_PER_SIDE, TRAIL_MULT, T_ENTRY_START, T_ENTRY_END,
    T_SQUARE_OFF, INTERVAL, NIFTY, SYMBOLS, prepare, nifty_bull, bar_minutes,
)
from brokers import get_broker

HERE = Path(__file__).parent
DB = HERE / "intraday_trades.db"

# ------------------------------------------------------------------ state ---
def db():
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS positions(
        symbol TEXT PRIMARY KEY, qty INTEGER, entry REAL, stop REAL,
        outlay REAL, entry_time TEXT);
    CREATE TABLE IF NOT EXISTS closed_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, qty INTEGER,
        entry REAL, exit_px REAL, entry_time TEXT, exit_time TEXT,
        pnl REAL, reason TEXT);
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

def get_positions(conn):
    cols = ["symbol", "qty", "entry", "stop", "outlay", "entry_time"]
    return {r[0]: dict(zip(cols, r))
            for r in conn.execute("SELECT * FROM positions").fetchall()}

# ------------------------------------------------------------------ alerts ---
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

# ------------------------------------------------------------------ engine ---
def close_position(conn, broker, pos, exit_px, ts, reason, log):
    sym, qty = pos["symbol"], pos["qty"]
    if CFG.get("mode") == "live":
        broker.place_order(sym, "SELL", qty, product="MIS", variety="regular")
    proceeds = exit_px * qty * (1 - COST_PER_SIDE)
    pnl = proceeds - pos["outlay"]
    conn.execute(
        "INSERT INTO closed_trades(symbol,qty,entry,exit_px,entry_time,"
        "exit_time,pnl,reason) VALUES(?,?,?,?,?,?,?,?)",
        (sym, qty, pos["entry"], exit_px, pos["entry_time"],
         ts.isoformat(), pnl, reason))
    conn.execute("DELETE FROM positions WHERE symbol=?", (sym,))
    meta_set(conn, "cash", cash(conn) + proceeds)
    log.append(f"CLOSED {sym} x{qty} @ {exit_px:.2f} ({reason}) "
               f"P&L Rs.{pnl:,.0f}")

def open_position(conn, broker, sym, row, ts, log):
    entry = float(row["Close"])
    stop_dist = TRAIL_MULT * float(row["atr"])
    if stop_dist <= 0 or np.isnan(stop_dist):
        return
    equity = cash(conn)  # sizing off free cash keeps it conservative
    qty = int(equity * RISK_PCT / stop_dist)
    qty = min(qty, int(MAX_PER_TRADE / entry),
              int(cash(conn) / (entry * (1 + COST_PER_SIDE))))
    if qty <= 0:
        return
    if CFG.get("mode") == "live":
        broker.place_order(sym, "BUY", qty, product="MIS", variety="regular")
    outlay = entry * qty * (1 + COST_PER_SIDE)
    conn.execute("INSERT INTO positions VALUES(?,?,?,?,?,?)",
                 (sym, qty, entry, entry - stop_dist, outlay, ts.isoformat()))
    meta_set(conn, "cash", cash(conn) - outlay)
    log.append(f"OPENED {sym} x{qty} @ {entry:.2f} "
               f"stop {entry - stop_dist:.2f} (Rs.{entry * qty:,.0f})")

def day_pnl_today(conn, today):
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM closed_trades WHERE exit_time >= ?",
        (today.isoformat(),)).fetchone()
    return row[0]

def entries_allowed(conn, today, log):
    if meta_get(conn, f"blocked:{today}"):
        return False
    if len(get_positions(conn)) >= MAX_POSITIONS:
        return False
    start_eq = float(meta_get(conn, f"day_start_eq:{today}", CAPITAL))
    pnl = day_pnl_today(conn, today)
    if pnl <= -MAX_DAY_LOSS * start_eq:
        meta_set(conn, f"blocked:{today}", "daily loss limit")
        log.append(f"DAILY LOSS LIMIT hit (Rs.{pnl:,.0f}) - trading stopped")
        return False
    if pnl >= MAX_DAY_PROFIT * start_eq:
        meta_set(conn, f"blocked:{today}", "daily profit target")
        log.append(f"DAILY PROFIT TARGET hit (Rs.{pnl:,.0f}) - trading stopped")
        return False
    return True

def process(conn, broker, log):
    today = date.today()
    if meta_get(conn, f"day_start_eq:{today}") is None:
        meta_set(conn, f"day_start_eq:{today}", cash(conn))

    # NIFTY regime
    ndf = yf.download(NIFTY, period="5d", interval=INTERVAL, auto_adjust=True,
                      progress=False, multi_level_index=False)
    regime = nifty_bull(ndf) if ndf is not None and not ndf.empty else None

    now = datetime.now().astimezone()
    prices = {}

    for sym in SYMBOLS:
        try:
            df = yf.download(sym, period="5d", interval=INTERVAL,
                             auto_adjust=True, progress=False,
                             multi_level_index=False)
        except Exception as e:
            print(f"  {sym}: download failed ({e})")
            continue
        if df is None or df.empty:
            continue
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        # drop the still-forming bar
        df = df[df.index + timedelta(minutes=5) <= now]
        if df.empty:
            continue
        df = prepare(df)
        if regime is not None:
            nb = regime.reindex(df.index, method="ffill").fillna(False)
        else:
            nb = df["cond_raw"] & False
        full = df["cond_raw"] & nb
        df["entry_sig"] = full & ~full.shift(fill_value=False)
        prices[sym] = float(df["Close"].iloc[-1])

        last_key = f"last:{sym}"
        last = meta_get(conn, last_key, "1970-01-01")
        new_bars = df[df.index > last]

        for ts, row in new_bars.iterrows():
            if ts.date() != today:          # never act on stale days
                meta_set(conn, last_key, ts.isoformat())
                continue
            minutes = bar_minutes(ts)
            pos = get_positions(conn).get(sym)

            # 1. square-off window: exit, take nothing else
            if minutes >= T_SQUARE_OFF:
                if pos:
                    close_position(conn, broker, pos, float(row["Close"]),
                                   ts, "Square-off 15:15", log)
                meta_set(conn, last_key, ts.isoformat())
                continue

            # 2. stop / trailing stop
            if pos:
                if float(row["Low"]) <= pos["stop"]:
                    close_position(conn, broker, pos, pos["stop"], ts,
                                   "Trailing stop", log)
                    pos = None
                else:
                    new_stop = float(row["High"]) - TRAIL_MULT * float(row["atr"])
                    if not np.isnan(new_stop) and new_stop > pos["stop"]:
                        conn.execute(
                            "UPDATE positions SET stop=? WHERE symbol=?",
                            (new_stop, sym))
                        pos["stop"] = new_stop

            # 3. exit signal on close
            if pos and bool(row["exit_sig"]):
                close_position(conn, broker, pos, float(row["Close"]), ts,
                               "Exit signal", log)
                pos = None

            # 4. entry signal on close
            if (pos is None and bool(row["entry_sig"])
                    and T_ENTRY_START <= minutes <= T_ENTRY_END
                    and entries_allowed(conn, today, log)):
                open_position(conn, broker, sym, row, ts, log)

            meta_set(conn, last_key, ts.isoformat())

    # wall-clock square-off safety net (works even if bars are missing)
    now_min = now.hour * 60 + now.minute
    if now_min >= T_SQUARE_OFF:
        for sym, pos in get_positions(conn).items():
            px = prices.get(sym, pos["entry"])
            close_position(conn, broker, pos, px,
                           datetime.now(), "Square-off 15:15", log)

    conn.commit()
    return prices

# ------------------------------------------------------------------ status ---
def show_status(conn, prices=None):
    today = date.today()
    print(f"\n=== Intraday portfolio ({CFG.get('mode', 'paper')} mode) ===")
    print(f"Cash: Rs.{cash(conn):,.0f}")
    positions = get_positions(conn)
    if positions:
        print("\nOpen positions:")
        for sym, p in positions.items():
            line = (f"  {sym}: {p['qty']} @ {p['entry']:.2f} | "
                    f"stop {p['stop']:.2f} | since {p['entry_time'][11:16]}")
            if prices and sym in prices:
                mtm = (prices[sym] - p["entry"]) * p["qty"]
                line += f" | last {prices[sym]:.2f} | MTM Rs.{mtm:,.0f}"
            print(line)
    else:
        print("No open positions.")
    blocked = meta_get(conn, f"blocked:{today}")
    if blocked:
        print(f"\nNew entries BLOCKED today: {blocked}")
    pnl = day_pnl_today(conn, today)
    n, total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM closed_trades").fetchone()
    print(f"\nToday's realized P&L: Rs.{pnl:,.0f}")
    print(f"All-time: {n} trades | Rs.{total:,.0f}")

def eod_report(conn):
    today = date.today()
    rows = conn.execute(
        "SELECT symbol,qty,entry,exit_px,pnl,reason FROM closed_trades "
        "WHERE exit_time >= ?", (today.isoformat(),)).fetchall()
    if not rows:
        return None
    lines = [f"EOD report {today}:"]
    for sym, qty, entry, exit_px, pnl, reason in rows:
        lines.append(f"  {sym} x{qty} {entry:.2f}->{exit_px:.2f} "
                     f"Rs.{pnl:,.0f} ({reason})")
    total = sum(r[4] for r in rows)
    lines.append(f"Day total: Rs.{total:,.0f} | Cash: Rs.{cash(conn):,.0f}")
    return "\n".join(lines)

# -------------------------------------------------------------------- main ---
def run_once(conn, broker):
    log = []
    prices = process(conn, broker, log)
    if log:
        print("\n".join(log))
        telegram(f"Intraday bot ({CFG.get('mode', 'paper')}):\n" + "\n".join(log))
    else:
        print(f"[{datetime.now():%H:%M}] no new signals")
    return prices

def main():
    conn = db()
    broker = get_broker(CFG)

    if "--status" in sys.argv:
        show_status(conn)
        return

    if CFG.get("mode") == "live":
        print("*** LIVE MODE - real MIS orders will be placed ***")

    if "--loop" in sys.argv:
        print("Loop mode: processing every 5 minutes until 15:35 IST. Ctrl+C to stop.")
        reported_eod = False
        while True:
            now = datetime.now()
            now_min = now.hour * 60 + now.minute
            if 9 * 60 + 15 <= now_min <= 15 * 60 + 35:
                run_once(conn, broker)
                if now_min >= 15 * 60 + 20 and not reported_eod:
                    msg = eod_report(conn)
                    if msg:
                        print(msg)
                        telegram(msg)
                    reported_eod = True
            elif now_min > 15 * 60 + 35:
                print("Market closed. Exiting loop.")
                break
            # sleep to the next 5-minute boundary (+15s for bar to settle)
            wait = 300 - (now.minute % 5) * 60 - now.second + 15
            time_mod.sleep(max(wait, 30))
    else:
        prices = run_once(conn, broker)
        show_status(conn, prices)

    conn.close()

if __name__ == "__main__":
    main()
