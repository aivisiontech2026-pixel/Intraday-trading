"""
Intraday options paper trader for NIFTY 50 and BANKNIFTY
===============================================
Simulates realistic options prices based on Black-Scholes model.

Strategy (trend-following, no fixed profit target):
  - Universe: NIFTY, BANKNIFTY, and the stock symbols in
    intraday_config.json (all real NSE F&O names)
  - Bullish signal (EMA9/21 + VWAP) -> Buy ATM call; bearish -> Buy ATM put
  - Daily quota: if confluence signals alone haven't produced
    MIN_DAILY_TRADES distinct symbols by mid-session, the shortfall is
    filled with the strongest-momentum names among the untraded rest
    (still one option trade per symbol per day, direction from momentum
    sign) - guarantees at least ~5 option trades most days
  - Initial stop-loss: -15% of premium, set at entry
  - Once a trade is up 10%+, a trailing stop activates and ratchets up
    with the premium's high-water mark (never loosens)
  - Real-time exit signal: underlying trend reverses against the position
    (EMA9/21 + VWAP flips) -> exit immediately regardless of P&L
  - Final exit: trailing/initial stop hit, trend reversal, last day before
    expiry, or square-off at 15:15 - whichever comes first

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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
MAX_PER_TRADE = 5_000  # options premium exposure per trade, sized off Rs.100,000 capital
INITIAL_STOP_PCT = -0.15   # hard stop at entry: -15% of premium
TRAIL_ACTIVATE_PCT = 0.10  # once up 10%+, start trailing
TRAIL_PCT = 0.12           # trail 12% below the highest premium seen
T_SQUARE_OFF = 15 * 60 + 15  # 15:15 IST
INTERVAL = "5m"

MIN_DAILY_TRADES = 5   # guarantee at least this many distinct option trades/day
MAX_POSITIONS = 8      # concurrent open positions cap (8 x Rs.5,000 = Rs.40,000 of Rs.100,000)
FALLBACK_START_MIN = 11 * 60  # 11:00 IST - top up the daily quota from here on
T_ENTRY_START, T_ENTRY_END = 9 * 60 + 30, 14 * 60 + 30

# trading universe: index symbols + every stock in intraday_config.json
UNIVERSE = [("NIFTY", "^NSEI"), ("BANKNIFTY", "^NSEBANK")] + \
    [(sym.replace(".NS", ""), sym) for sym in CFG.get("symbols", [])]

# ----------------------------------------------------------------- db ---
def db_init():
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS options_positions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, option_type TEXT, strike REAL, expiry TEXT,
        qty INTEGER, entry_price REAL, entry_time TEXT,
        high_water REAL, stop_price REAL);
    CREATE TABLE IF NOT EXISTS options_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, option_type TEXT, strike REAL, expiry TEXT,
        qty INTEGER, entry_price REAL, exit_price REAL,
        entry_time TEXT, exit_time TEXT, pnl REAL, reason TEXT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    # tolerate DBs created before high_water/stop_price existed
    cols = {r[1] for r in conn.execute("PRAGMA table_info(options_positions)")}
    if "high_water" not in cols:
        conn.execute("ALTER TABLE options_positions ADD COLUMN high_water REAL")
    if "stop_price" not in cols:
        conn.execute("ALTER TABLE options_positions ADD COLUMN stop_price REAL")
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

    # Find ATM strike (finer rounding for lower-priced stock underlyings)
    if spot > 50000:
        strike_unit = 500
    elif spot > 2000:
        strike_unit = 100
    elif spot > 500:
        strike_unit = 50
    elif spot > 100:
        strike_unit = 20
    else:
        strike_unit = 10
    atm_strike = (spot // strike_unit) * strike_unit

    if option_type == "CALL":
        price = black_scholes_call(spot, atm_strike, dte, rate, iv)
    else:  # PUT
        price = black_scholes_put(spot, atm_strike, dte, rate, iv)

    return atm_strike, price, dte

# ----------------------------------------------------------------- signals ---
def get_direction(df):
    """Returns 'BULL', 'BEAR', or None based on EMA9/21 + VWAP confluence."""
    if df is None or df.empty or len(df) < 21:
        return None
    ema9 = df["Close"].ewm(span=9).mean().iloc[-1]
    ema21 = df["Close"].ewm(span=21).mean().iloc[-1]
    vol = df["Volume"].sum()
    if not vol:
        return None
    vwap = (df["Close"] * df["Volume"]).sum() / vol

    if ema9 > ema21 and df["Close"].iloc[-1] > vwap:
        return "BULL"
    elif ema9 < ema21 and df["Close"].iloc[-1] < vwap:
        return "BEAR"
    return None

def get_momentum(df, today):
    """% move today (or over the last dozen bars if today has too few)."""
    if df is None or df.empty:
        return 0.0
    todays = df[df.index.date == today]
    if len(todays) >= 2:
        o, c = float(todays["Open"].iloc[0]), float(todays["Close"].iloc[-1])
    else:
        window = df.tail(12)
        if len(window) < 2:
            return 0.0
        o, c = float(window["Close"].iloc[0]), float(window["Close"].iloc[-1])
    return (c - o) / o * 100 if o else 0.0

def symbols_traded_today(conn, today_str):
    """Distinct symbols already opened today (open or already closed)."""
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM options_positions WHERE entry_time LIKE ?",
        (f"{today_str}%",)).fetchall()
    rows += conn.execute(
        "SELECT DISTINCT symbol FROM options_trades WHERE entry_time LIKE ?",
        (f"{today_str}%",)).fetchall()
    return {r[0] for r in rows}

def open_option(conn, spot, option_type, symbol, today, log, tag=""):
    """Open an options position."""
    expiry_str = next_expiry(today)
    strike, premium, dte = get_atm_option_price(spot, option_type, today)

    qty = max(1, int(MAX_PER_TRADE / premium))  # quantity based on premium
    cost = qty * premium

    if cost > cash(conn):
        return False

    initial_stop = round(premium * (1 + INITIAL_STOP_PCT), 2)
    conn.execute(
        "INSERT INTO options_positions(symbol,option_type,strike,expiry,qty,entry_price,"
        "entry_time,high_water,stop_price) VALUES(?,?,?,?,?,?,?,?,?)",
        (symbol, option_type, strike, expiry_str, qty, premium, datetime.now().isoformat(),
         premium, initial_stop))
    meta_set(conn, "cash", cash(conn) - cost)

    msg = (f"📊 OPTIONS: BOUGHT {qty} {symbol} {option_type} {strike} @ Rs.{premium:.2f}"
           f"{tag} (DTE={dte}, initial stop Rs.{initial_stop:.2f})")
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
    today_str = today.isoformat()

    # Send market open message once per day
    if not meta_get(conn, f"market_open_msg:{today_str}"):
        msg = f"🔔 OPTIONS MARKET OPENED | {today_str} 09:15 IST\n💰 Capital: Rs.{cash(conn):,.0f}"
        log.append(msg)
        telegram(msg)
        meta_set(conn, f"market_open_msg:{today_str}", "1")

    # Fetch the whole universe: spot, trend direction, today's momentum
    data = {}
    for symbol, ticker in UNIVERSE:
        try:
            df = yf.download(ticker, period="5d", interval=INTERVAL,
                             auto_adjust=True, progress=False, multi_level_index=False)
        except Exception as e:
            print(f"  {symbol}: download failed ({e})")
            continue
        if df is None or df.empty:
            continue
        data[symbol] = {
            "spot": float(df["Close"].iloc[-1]),
            "direction": get_direction(df),
            "momentum": get_momentum(df, today),
        }

    if "NIFTY" not in data:
        return  # can't proceed without at least the index

    now = datetime.now().astimezone()
    now_min = now.hour * 60 + now.minute

    # Manage existing positions
    positions = conn.execute("SELECT id,symbol,option_type,strike,expiry,qty,entry_price,"
                            "high_water,stop_price FROM options_positions").fetchall()

    for pos_row in positions:
        pos = dict(zip(["id", "symbol", "option_type", "strike", "expiry", "qty",
                       "entry_price", "high_water", "stop_price"], pos_row))

        info = data.get(pos["symbol"])
        if info is None:
            continue
        current_price, direction = info["spot"], info["direction"]

        # Time decay (theta): lose 2% per day closer to expiry
        dte = days_to_expiry(pos["expiry"], today)
        theta_decay = 1 - (0.02 / max(1, dte))
        current_price *= theta_decay

        # Ratchet the trailing stop up (never down) once the trade is in profit
        high_water = max(pos["high_water"] or pos["entry_price"], current_price)
        stop_price = pos["stop_price"] or pos["entry_price"] * (1 + INITIAL_STOP_PCT)
        if high_water >= pos["entry_price"] * (1 + TRAIL_ACTIVATE_PCT):
            trail_stop = high_water * (1 - TRAIL_PCT)
            stop_price = max(stop_price, trail_stop)
        if high_water != pos["high_water"] or stop_price != pos["stop_price"]:
            conn.execute("UPDATE options_positions SET high_water=?, stop_price=? WHERE id=?",
                        (high_water, stop_price, pos["id"]))
            pos["high_water"], pos["stop_price"] = high_water, stop_price

        # Real-time exit signal: underlying trend has reversed against the position
        reversed_ = ((pos["option_type"] == "CALL" and direction == "BEAR") or
                    (pos["option_type"] == "PUT" and direction == "BULL"))

        if current_price <= stop_price:
            trailing = high_water > pos["entry_price"] * (1 + TRAIL_ACTIVATE_PCT)
            reason = "Trailing stop" if trailing else "Initial stop"
            close_option(conn, pos, current_price, reason, log)
        elif reversed_:
            close_option(conn, pos, current_price, "Trend reversal exit", log)
        elif dte <= 1:  # last day before expiry
            close_option(conn, pos, current_price, "Expiry close-out", log)
        elif now_min >= T_SQUARE_OFF:
            close_option(conn, pos, current_price, "Market close 15:15", log)

    if not (T_ENTRY_START <= now_min <= T_ENTRY_END):
        conn.commit()
        return

    traded_today = symbols_traded_today(conn, today_str)
    open_count = conn.execute("SELECT COUNT(*) FROM options_positions").fetchone()[0]

    # Tier 1: confluence signal entries (EMA9/21 + VWAP)
    for symbol, info in data.items():
        if open_count >= MAX_POSITIONS:
            break
        if symbol in traded_today or info["direction"] is None:
            continue
        option_type = "CALL" if info["direction"] == "BULL" else "PUT"
        if open_option(conn, info["spot"], option_type, symbol, today, log):
            traded_today.add(symbol)
            open_count += 1

    # Tier 2: guarantee at least MIN_DAILY_TRADES/day - fill the shortfall from
    # the strongest-momentum untraded names once we're past FALLBACK_START_MIN
    if now_min >= FALLBACK_START_MIN and len(traded_today) < MIN_DAILY_TRADES:
        candidates = sorted(
            (item for item in data.items() if item[0] not in traded_today),
            key=lambda item: -abs(item[1]["momentum"]))
        for symbol, info in candidates:
            if len(traded_today) >= MIN_DAILY_TRADES or open_count >= MAX_POSITIONS:
                break
            option_type = "CALL" if info["momentum"] >= 0 else "PUT"
            if open_option(conn, info["spot"], option_type, symbol, today, log,
                          tag=f" [momentum pick {info['momentum']:+.1f}%]"):
                traded_today.add(symbol)
                open_count += 1

    conn.commit()

# ----------------------------------------------------------------- main ---
def main():
    conn = db_init()
    today = date.today()
    log = []

    # Show status
    positions = conn.execute("SELECT symbol,option_type,strike,qty,entry_price,"
                            "high_water,stop_price FROM options_positions").fetchall()
    if not positions:
        status = f"[{datetime.now():%H:%M}] Options: no open positions | Cash: Rs.{cash(conn):,.0f}"
    else:
        status = f"[{datetime.now():%H:%M}] Options: {len(positions)} open position(s)"

    print(status)
    for sym, opt_type, strike, qty, entry, hw, stop in positions:
        print(f"  {sym} {opt_type} {strike} x{qty} @ {entry:.2f} "
              f"(high {hw:.2f}, stop {stop:.2f})")

    # Process
    process(conn, log, today)
    if log:
        print("\n".join(log))
        # Send summary to telegram
        telegram(f"📊 Options Summary:\n" + "\n".join(log))

    conn.close()

if __name__ == "__main__":
    main()
