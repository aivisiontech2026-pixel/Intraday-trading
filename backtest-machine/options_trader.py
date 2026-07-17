"""
Intraday options paper trader for NIFTY 50 and BANKNIFTY
===============================================
Simulates realistic options prices based on Black-Scholes model.

Strategy:
  - Bullish signal -> Buy ATM call
  - Bearish signal -> Buy ATM put
  - Exit: +20% profit or -10% stop loss (or square-off at 15:15)

Runs every 5 minutes during market hours. State persists in options_trades.db.
"""

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
import math

import numpy as np
import yfinance as yf

HERE = Path(__file__).parent
DB = HERE / "options_trades.db"
CFG_FILE = HERE / "intraday_config.json"
CFG = json.loads(CFG_FILE.read_text()) if CFG_FILE.exists() else {}

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

CAPITAL = 100_000  # shared capital pool
MAX_PER_TRADE = 5_000  # options premium exposure per trade
PROFIT_TARGET = 0.20  # exit at +20%
STOP_LOSS = -0.10  # exit at -10%
T_SQUARE_OFF = 15 * 60 + 15  # 15:15 IST
INTERVAL = "5m"
NIFTY = "^NSEI"
BANKNIFTY = "^NSEBANK"

# ----------------------------------------------------------------- db ---
def db_init():
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS options_positions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, option_type TEXT, strike REAL, expiry TEXT,
        qty INTEGER, entry_price REAL, entry_time TEXT);
    CREATE TABLE IF NOT EXISTS options_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, option_type TEXT, strike REAL, expiry TEXT,
        qty INTEGER, entry_price REAL, exit_price REAL,
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

# ----------------------------------------------------------------- pricing ---
def days_to_expiry(expiry_str, ref_date):
    """Days remaining until option expiry (Thursday for NIFTY/BANKNIFTY)."""
    exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    return max(1, (exp - ref_date).days)

def implied_vol(spot, atm_iv=0.20):
    """Simple IV: lower for high spots (low volatility in uptrends)."""
    return atm_iv * (1 - 0.05 * math.log(max(1, spot / 50000)))

def black_scholes_call(spot, strike, dte, rate, iv):
    """Simplified Black-Scholes call price."""
    if dte <= 0:
        return max(0, spot - strike)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * dte/365) / (iv * math.sqrt(dte/365))
    d2 = d1 - iv * math.sqrt(dte/365)
    nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    nd2 = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    call_price = spot * nd1 - strike * math.exp(-rate * dte/365) * nd2
    return max(0.5, call_price)  # min price 0.50

def black_scholes_put(spot, strike, dte, rate, iv):
    """Simplified Black-Scholes put price."""
    if dte <= 0:
        return max(0, strike - spot)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * dte/365) / (iv * math.sqrt(dte/365))
    d2 = d1 - iv * math.sqrt(dte/365)
    nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    nd2 = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    put_price = strike * math.exp(-rate * dte/365) * (1 - nd2) - spot * (1 - nd1)
    return max(0.5, put_price)

def next_expiry(today):
    """Next Thursday (NIFTY/BANKNIFTY weekly expiry)."""
    days_ahead = 3 - today.weekday()  # 3 = Thursday
    if days_ahead <= 0:
        days_ahead += 7
    return (today + timedelta(days=days_ahead)).isoformat()

def get_atm_option_price(spot, option_type, today):
    """Get realistic ATM option price."""
    dte = days_to_expiry(next_expiry(today), today)
    iv = implied_vol(spot)
    rate = 0.05  # 5% risk-free rate

    # Find ATM strike (round to nearest 100/500)
    strike_unit = 500 if spot > 50000 else 100
    atm_strike = (spot // strike_unit) * strike_unit

    if option_type == "CALL":
        price = black_scholes_call(spot, atm_strike, dte, rate, iv)
    else:  # PUT
        price = black_scholes_put(spot, atm_strike, dte, rate, iv)

    return atm_strike, price, dte

# ----------------------------------------------------------------- signals ---
def get_nifty_direction(df):
    """Returns 'BULL', 'BEAR', or None based on EMA crossover."""
    if df is None or df.empty or len(df) < 21:
        return None
    ema9 = df["Close"].ewm(span=9).mean().iloc[-1]
    ema21 = df["Close"].ewm(span=21).mean().iloc[-1]
    vwap = (df["Close"] * df["Volume"]).sum() / df["Volume"].sum()

    if ema9 > ema21 and df["Close"].iloc[-1] > vwap:
        return "BULL"
    elif ema9 < ema21 and df["Close"].iloc[-1] < vwap:
        return "BEAR"
    return None

def open_option(conn, spot, option_type, symbol, today, log):
    """Open an options position."""
    expiry_str = next_expiry(today)
    strike, premium, dte = get_atm_option_price(spot, option_type, today)

    qty = max(1, int(MAX_PER_TRADE / premium))  # quantity based on premium
    cost = qty * premium

    if cost > cash(conn):
        return False

    conn.execute(
        "INSERT INTO options_positions(symbol,option_type,strike,expiry,qty,entry_price,entry_time) "
        "VALUES(?,?,?,?,?,?,?)",
        (symbol, option_type, strike, expiry_str, qty, premium, datetime.now().isoformat()))
    meta_set(conn, "cash", cash(conn) - cost)

    msg = f"📊 OPTIONS: BOUGHT {qty} {symbol} {option_type} {strike} @ Rs.{premium:.2f} (DTE={dte})"
    log.append(msg)
    telegram(msg)
    return True

def close_option(conn, pos, exit_price, reason, log):
    """Close an options position."""
    qty = pos["qty"]
    proceeds = qty * exit_price
    pnl = proceeds - (qty * pos["entry_price"])
    pnl_pct = (pnl / (qty * pos["entry_price"])) * 100 if pos["entry_price"] > 0 else 0

    conn.execute(
        "INSERT INTO options_trades(symbol,option_type,strike,expiry,qty,entry_price,exit_price,"
        "entry_time,exit_time,pnl,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pos["symbol"], pos["option_type"], pos["strike"], pos["expiry"],
         qty, pos["entry_price"], exit_price, pos["entry_time"],
         datetime.now().isoformat(), pnl, reason))
    conn.execute("DELETE FROM options_positions WHERE id=?", (pos["id"],))
    meta_set(conn, "cash", cash(conn) + proceeds)

    emoji = "✅" if pnl > 0 else "❌"
    msg = f"{emoji} OPTIONS: SOLD {qty} {pos['symbol']} {pos['option_type']} {pos['strike']} @ Rs.{exit_price:.2f} | P&L Rs.{pnl:,.0f} ({pnl_pct:+.1f}%) | {reason}"
    log.append(msg)
    telegram(msg)
    return pnl

def process(conn, log, today):
    """Process options signals and manage positions."""
    # Send market open message once per day
    if not meta_get(conn, f"market_open_msg:{today}"):
        msg = f"🔔 OPTIONS MARKET OPENED | {today} 09:15 IST\n💰 Capital: Rs.{cash(conn):,.0f}"
        log.append(msg)
        telegram(msg)
        meta_set(conn, f"market_open_msg:{today}", "1")

    # Fetch NIFTY and BANKNIFTY
    try:
        nifty_df = yf.download(NIFTY, period="5d", interval=INTERVAL,
                               auto_adjust=True, progress=False, multi_level_index=False)
    except:
        return

    if nifty_df is None or nifty_df.empty:
        return

    nifty_spot = float(nifty_df["Close"].iloc[-1])
    nifty_dir = get_nifty_direction(nifty_df)

    try:
        bank_df = yf.download(BANKNIFTY, period="5d", interval=INTERVAL,
                              auto_adjust=True, progress=False, multi_level_index=False)
    except:
        bank_df = None

    bank_spot = float(bank_df["Close"].iloc[-1]) if bank_df is not None and not bank_df.empty else None
    bank_dir = get_nifty_direction(bank_df) if bank_df is not None and not bank_df.empty else None

    now = datetime.now().astimezone()
    now_min = now.hour * 60 + now.minute

    # Get positions
    positions = conn.execute("SELECT id,symbol,option_type,strike,expiry,qty,entry_price "
                            "FROM options_positions").fetchall()

    # Manage existing positions
    for pos_row in positions:
        pos = dict(zip(["id", "symbol", "option_type", "strike", "expiry", "qty", "entry_price"], pos_row))

        # Update current price
        if pos["symbol"] == "NIFTY":
            current_price = nifty_spot
        else:
            current_price = bank_spot

        if current_price is None:
            continue

        # Time decay (theta): lose 2% per day closer to expiry
        dte = days_to_expiry(pos["expiry"], today)
        theta_decay = 1 - (0.02 / max(1, dte))
        current_price *= theta_decay

        pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) if pos["entry_price"] > 0 else 0

        # Exit on profit target, stop loss, or time
        if pnl_pct >= PROFIT_TARGET:
            close_option(conn, pos, current_price, f"+{PROFIT_TARGET*100:.0f}% profit", log)
        elif pnl_pct <= STOP_LOSS:
            close_option(conn, pos, current_price, f"{STOP_LOSS*100:.0f}% stop", log)
        elif dte <= 1:  # last day before expiry
            close_option(conn, pos, current_price, "Expiry close-out", log)
        elif now_min >= T_SQUARE_OFF:
            close_option(conn, pos, current_price, "Market close 15:15", log)

    # Entry signals (after 09:30, before 14:30)
    if 9*60+30 <= now_min <= 14*60+30:
        if nifty_dir == "BULL" and len(positions) < 2:
            open_option(conn, nifty_spot, "CALL", "NIFTY", today, log)
        elif nifty_dir == "BEAR" and len(positions) < 2:
            open_option(conn, nifty_spot, "PUT", "NIFTY", today, log)

        if bank_spot and bank_dir == "BULL" and len(positions) < 4:
            open_option(conn, bank_spot, "CALL", "BANKNIFTY", today, log)
        elif bank_spot and bank_dir == "BEAR" and len(positions) < 4:
            open_option(conn, bank_spot, "PUT", "BANKNIFTY", today, log)

    conn.commit()

# ----------------------------------------------------------------- main ---
def main():
    conn = db_init()
    today = date.today()
    log = []

    # Show status
    positions = conn.execute("SELECT symbol,option_type,strike,qty,entry_price "
                            "FROM options_positions").fetchall()
    if not positions:
        status = f"[{datetime.now():%H:%M}] Options: no open positions | Cash: Rs.{cash(conn):,.0f}"
    else:
        status = f"[{datetime.now():%H:%M}] Options: {len(positions)} open position(s)"

    print(status)
    for sym, opt_type, strike, qty, entry in positions:
        print(f"  {sym} {opt_type} {strike} x{qty} @ {entry:.2f}")

    # Process
    process(conn, log, today)
    if log:
        print("\n".join(log))
        # Send summary to telegram
        telegram(f"📊 Options Summary:\n" + "\n".join(log))

    conn.close()

if __name__ == "__main__":
    main()
