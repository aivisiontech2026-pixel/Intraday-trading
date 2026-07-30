"""
Intraday options paper trader - LIVE market execution pricing.
==============================================================

EXECUTION PRICING CONTRACT
--------------------------
Every price that affects money derives from LIVE Angel One market data.
There is no synthetic/model pricing anywhere in the execution path.

Fills come from one of two places, and every trade records which:
  * LIVE_ASK / LIVE_BID / LIVE_LTP - the live quote (entries, trend
    reversal exits, square-off).
  * STOP_LEVEL - stop-triggered exits fill AT the stop level rather than
    at the price observed on waking. A resting stop order triggers the
    instant price touches the level; it does not wait for our next poll,
    so filling at the wake-up price charged the strategy twice (once for
    the market move, again for polling latency). The stop level is itself
    derived from live prices - entry fill and the live high-water mark.
    The observed market price is recorded alongside it for audit.

No synthetic fallback exists:

  * No live quote for a contract  -> the bot REFUSES to open it.
  * No live quote while a position is open -> that position is held and
    retried next cycle (its stop is evaluated only against real prices).
  * At 15:15 square-off, if a live quote is unavailable, the position is
    closed at the LAST OBSERVED LIVE price (stored on the position), and
    the exit reason says so explicitly. That is a stale real price, never
    a model price.

Black-Scholes is retained ONLY for analytics: fair value, and the
mispricing edge (live - fair) recorded on each trade for research. It is
never used to fill, stop, or value a position.

Contract selection is likewise real: strikes, expiries, tokens and lot
sizes all come from Angel One's instrument master, so the bot can only
trade contracts the exchange actually lists.

Strategy (trend-following, no fixed profit target)
--------------------------------------------------
  - Universe: NIFTY, BANKNIFTY + the stocks in intraday_config.json
  - Signals are computed on the UNDERLYING (EMA9/21 + VWAP on 5-min bars);
    the option is the instrument used to express that view.
  - Bullish -> buy ATM call; bearish -> buy ATM put
  - Initial stop: -15% of entry premium
  - Once +10%, a trailing stop ratchets 12% below the high-water premium
  - Trend reversal on the underlying exits immediately
  - Everything is squared off at 15:15

Sizing is in WHOLE LOTS (real lot sizes: RELIANCE 500, MARUTI 50,
WIPRO 3000, NIFTY 65, BANKNIFTY 30). A symbol whose single lot costs more
than the per-trade budget or available cash is skipped, with the reason
logged.
"""

import json
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

import angelone_client as angel

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
DB = HERE / "options_trades.db"
CFG_FILE = HERE / "intraday_config.json"
CFG = json.loads(CFG_FILE.read_text()) if CFG_FILE.exists() else {}

CAPITAL = 100_000
MAX_PER_TRADE = 25_000      # per-position premium budget (>=1 real lot)
INITIAL_STOP_PCT = -0.15
TRAIL_ACTIVATE_PCT = 0.10
TRAIL_PCT = 0.12
T_SQUARE_OFF = 15 * 60 + 15
INTERVAL = "5m"

MIN_DAILY_TRADES = 5
MAX_POSITIONS = 4           # 4 x Rs.25,000 = the full Rs.100,000 book
FALLBACK_START_MIN = 11 * 60
T_ENTRY_START, T_ENTRY_END = 9 * 60 + 30, 14 * 60 + 30

UNIVERSE = [("NIFTY", "^NSEI"), ("BANKNIFTY", "^NSEBANK")] + \
    [(s.replace(".NS", ""), s) for s in CFG.get("symbols", [])]
UNIVERSE_NAMES = [n for n, _ in UNIVERSE]


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
    # additive migrations - live-data columns added by the live-pricing refactor
    for table, col, decl in [
        ("options_positions", "token", "TEXT"),
        ("options_positions", "trading_symbol", "TEXT"),
        ("options_positions", "lots", "INTEGER"),
        ("options_positions", "lotsize", "INTEGER"),
        ("options_positions", "last_price", "REAL"),
        ("options_positions", "entry_fair_value", "REAL"),
        ("options_trades", "token", "TEXT"),
        ("options_trades", "trading_symbol", "TEXT"),
        ("options_trades", "lots", "INTEGER"),
        ("options_trades", "lotsize", "INTEGER"),
        ("options_trades", "entry_bid", "REAL"),
        ("options_trades", "entry_ask", "REAL"),
        ("options_trades", "exit_bid", "REAL"),
        ("options_trades", "exit_ask", "REAL"),
        ("options_trades", "entry_oi", "INTEGER"),
        ("options_trades", "exit_oi", "INTEGER"),
        ("options_trades", "entry_volume", "INTEGER"),
        ("options_trades", "price_source", "TEXT"),
    ]:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return conn


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))


def cash(conn):
    return float(meta_get(conn, "cash", CAPITAL))


POS_COLS = ["id", "symbol", "option_type", "strike", "expiry", "qty",
            "entry_price", "entry_time", "high_water", "stop_price",
            "token", "trading_symbol", "lots", "lotsize", "last_price",
            "entry_fair_value"]
POS_SELECT = "SELECT " + ",".join(POS_COLS) + " FROM options_positions"


def load_positions(conn):
    return [dict(zip(POS_COLS, row)) for row in conn.execute(POS_SELECT)]


# ------------------------------------------------- analytics (NOT pricing) ---
def black_scholes(spot, strike, dte, rate, iv, opt_type):
    """Theoretical fair value. ANALYTICS ONLY - never an execution price."""
    if iv <= 0 or spot <= 0 or strike <= 0:
        return None
    if dte <= 0:
        return max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
    t = dte / 365.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv ** 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    nd2 = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    if opt_type == "CE":
        return spot * nd1 - strike * math.exp(-rate * t) * nd2
    return strike * math.exp(-rate * t) * (1 - nd2) - spot * (1 - nd1)


def fair_value(spot, rec, today, iv_map):
    """Model fair value for mispricing analytics. None when IV unknown."""
    if not iv_map:
        return None
    iv = iv_map.get((rec["strike"], rec["opt_type"]))
    if not iv:
        return None
    return black_scholes(spot, rec["strike"], (rec["expiry"] - today).days,
                         0.05, iv, rec["opt_type"])


# ----------------------------------------------------------------- signals ---
def get_direction(df):
    """BULL / BEAR / None from EMA9-21 + VWAP confluence on the UNDERLYING."""
    if df is None or df.empty or len(df) < 21:
        return None
    ema9 = df["Close"].ewm(span=9).mean().iloc[-1]
    ema21 = df["Close"].ewm(span=21).mean().iloc[-1]
    vol = df["Volume"].sum()
    if not vol:
        return None
    vwap = (df["Close"] * df["Volume"]).sum() / vol
    last = df["Close"].iloc[-1]
    if ema9 > ema21 and last > vwap:
        return "BULL"
    if ema9 < ema21 and last < vwap:
        return "BEAR"
    return None


def get_momentum(df, today):
    if df is None or df.empty:
        return 0.0
    todays = df[df.index.date == today]
    if len(todays) >= 2:
        o, c = float(todays["Open"].iloc[0]), float(todays["Close"].iloc[-1])
    else:
        w = df.tail(12)
        if len(w) < 2:
            return 0.0
        o, c = float(w["Close"].iloc[0]), float(w["Close"].iloc[-1])
    return (c - o) / o * 100 if o else 0.0


def symbols_traded_today(conn, today_str):
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM options_positions WHERE entry_time LIKE ?",
        (f"{today_str}%",)).fetchall()
    rows += conn.execute(
        "SELECT DISTINCT symbol FROM options_trades WHERE entry_time LIKE ?",
        (f"{today_str}%",)).fetchall()
    return {r[0] for r in rows}


# ----------------------------------------------------------------- trace ---
def trace(**fields):
    """One structured audit line per option decision."""
    print("  TRACE " + " ".join(f"{k}={v}" for k, v in fields.items()))


# ----------------------------------------------------------------- orders ---
def open_option(conn, rec, quote, spot, underlying_dir, today, log,
                fair, tag=""):
    """Open a position. Entry fills at the live ASK (crossing the spread),
    falling back to live LTP when there is no depth. Never a model price."""
    ltp, bid, ask = quote["ltp"], quote["bid"], quote["ask"]
    entry_px = ask if ask > 0 else ltp
    if entry_px <= 0:
        trace(event="entry_rejected", symbol=rec["symbol"], reason="no_live_price")
        return False

    lotsize = rec["lotsize"]
    lot_cost = entry_px * lotsize
    budget = min(MAX_PER_TRADE, cash(conn))
    lots = int(budget // lot_cost)
    if lots < 1:
        trace(event="entry_rejected", symbol=rec["symbol"],
              reason="one_lot_exceeds_budget", lot_cost=f"{lot_cost:.0f}",
              budget=f"{budget:.0f}")
        return False

    qty = lots * lotsize
    cost = qty * entry_px
    initial_stop = round(entry_px * (1 + INITIAL_STOP_PCT), 2)

    conn.execute(
        "INSERT INTO options_positions(symbol,option_type,strike,expiry,qty,"
        "entry_price,entry_time,high_water,stop_price,token,trading_symbol,"
        "lots,lotsize,last_price,entry_fair_value) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["name"], rec["opt_type"], rec["strike"], rec["expiry"].isoformat(),
         qty, entry_px, datetime.now().isoformat(), entry_px, initial_stop,
         rec["token"], rec["symbol"], lots, lotsize, entry_px, fair))
    meta_set(conn, "cash", cash(conn) - cost)

    edge = f"{entry_px - fair:+.2f}" if fair else "n/a"
    trace(event="ENTRY", ts=datetime.now().isoformat(timespec="seconds"),
          symbol=rec["symbol"], token=rec["token"], ltp=f"{ltp:.2f}",
          bid=f"{bid:.2f}", ask=f"{ask:.2f}", fill=f"{entry_px:.2f}",
          volume=quote["volume"], oi=quote["oi"], spot=f"{spot:.2f}",
          signal=underlying_dir, lots=lots, lotsize=lotsize, qty=qty,
          cost=f"{cost:.0f}", stop=f"{initial_stop:.2f}",
          fair_value=f"{fair:.2f}" if fair else "n/a", edge=edge,
          price_source="LIVE_ASK" if ask > 0 else "LIVE_LTP")

    msg = (f"📊 OPTIONS: BOUGHT {lots} lot(s) ({qty}) {rec['symbol']} "
           f"@ Rs.{entry_px:.2f}{tag} | cost Rs.{cost:,.0f} | "
           f"stop Rs.{initial_stop:.2f}")
    log.append(msg)
    telegram(msg)
    return True


def close_option(conn, pos, exit_px, reason, log, quote=None, price_source=None):
    """Close a position.

    `exit_px` is normally the live market price. For stop-triggered exits
    the caller passes the STOP LEVEL instead and sets
    price_source="STOP_LEVEL" - see the stop branch in process() for why.
    """
    qty = pos["qty"]
    proceeds = qty * exit_px
    pnl = proceeds - qty * pos["entry_price"]
    pnl_pct = (exit_px / pos["entry_price"] - 1) * 100 if pos["entry_price"] else 0

    src = price_source or ("LIVE_BID" if quote and quote.get("bid", 0) > 0 else (
        "LIVE_LTP" if quote else "LAST_OBSERVED_LIVE"))
    conn.execute(
        "INSERT INTO options_trades(symbol,option_type,strike,expiry,qty,"
        "entry_price,exit_price,entry_time,exit_time,pnl,reason,token,"
        "trading_symbol,lots,lotsize,exit_bid,exit_ask,exit_oi,price_source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pos["symbol"], pos["option_type"], pos["strike"], pos["expiry"], qty,
         pos["entry_price"], exit_px, pos["entry_time"],
         datetime.now().isoformat(), pnl, reason, pos.get("token"),
         pos.get("trading_symbol"), pos.get("lots"), pos.get("lotsize"),
         quote.get("bid") if quote else None,
         quote.get("ask") if quote else None,
         quote.get("oi") if quote else None, src))
    conn.execute("DELETE FROM options_positions WHERE id=?", (pos["id"],))
    meta_set(conn, "cash", cash(conn) + proceeds)

    trace(event="EXIT", ts=datetime.now().isoformat(timespec="seconds"),
          symbol=pos.get("trading_symbol"), token=pos.get("token"),
          ltp=f"{quote['ltp']:.2f}" if quote else "n/a",
          bid=f"{quote['bid']:.2f}" if quote else "n/a",
          ask=f"{quote['ask']:.2f}" if quote else "n/a",
          volume=quote["volume"] if quote else "n/a",
          oi=quote["oi"] if quote else "n/a",
          fill=f"{exit_px:.2f}", entry=f"{pos['entry_price']:.2f}",
          market=f"{quote['bid'] or quote['ltp']:.2f}" if quote else "n/a",
          qty=qty, pnl=f"{pnl:.0f}", pnl_pct=f"{pnl_pct:+.1f}%",
          reason=reason, price_source=src)

    emoji = "✅" if pnl > 0 else "❌"
    msg = (f"{emoji} OPTIONS: SOLD {pos.get('lots')} lot(s) ({qty}) "
           f"{pos.get('trading_symbol')} @ Rs.{exit_px:.2f} | "
           f"P&L Rs.{pnl:,.0f} ({pnl_pct:+.1f}%) | {reason}")
    log.append(msg)
    telegram(msg)
    return pnl


# ----------------------------------------------------------------- engine ---
def process(conn, log, today):
    today_str = today.isoformat()

    if not meta_get(conn, f"market_open_msg:{today_str}"):
        msg = (f"🔔 OPTIONS MARKET OPENED | {today_str} 09:15 IST\n"
               f"💰 Capital: Rs.{cash(conn):,.0f}")
        log.append(msg)
        telegram(msg)
        meta_set(conn, f"market_open_msg:{today_str}", "1")

    now = datetime.now().astimezone()
    now_min = now.hour * 60 + now.minute

    # ---- live market data plumbing -------------------------------------
    smart = angel.login()
    if smart is None:
        print("  Angel One unavailable -> NO option trading this cycle "
              "(execution requires live quotes; synthetic pricing is not "
              "permitted). Open positions are held.")
        return
    master = angel.load_instrument_master(UNIVERSE_NAMES)
    if not master:
        print("  Instrument master unavailable -> NO option trading this cycle.")
        return

    # ---- underlying signals (yfinance 5-min bars) ----------------------
    data = {}
    for name, ticker in UNIVERSE:
        try:
            df = yf.download(ticker, period="5d", interval=INTERVAL,
                             auto_adjust=True, progress=False,
                             multi_level_index=False)
        except Exception as e:
            print(f"  {name}: underlying download failed ({e})")
            continue
        if df is None or df.empty:
            continue
        data[name] = {"spot": float(df["Close"].iloc[-1]),
                      "direction": get_direction(df),
                      "momentum": get_momentum(df, today)}
    if "NIFTY" not in data:
        print("  No underlying data -> skipping cycle.")
        return

    positions = load_positions(conn)

    # ---- decide which contracts we may enter, so quotes can be batched --
    traded_today = symbols_traded_today(conn, today_str)
    open_count = len(positions)
    in_window = T_ENTRY_START <= now_min <= T_ENTRY_END

    candidates = []   # (name, rec, direction, tag)
    if in_window:
        def consider(name, info, direction, tag=""):
            exp = angel.nearest_expiry(master, name, today, min_dte=1)
            if exp is None:
                trace(event="entry_skipped", symbol=name, reason="no_listed_expiry")
                return
            opt_type = "CE" if direction == "BULL" else "PE"
            rec = angel.find_option(master, name, exp, info["spot"], opt_type)
            if rec is None:
                trace(event="entry_skipped", symbol=name, reason="no_listed_strike")
                return
            candidates.append((name, rec, direction, tag))

        for name, info in data.items():
            if name in traded_today or info["direction"] is None:
                continue
            consider(name, info, info["direction"])

        if now_min >= FALLBACK_START_MIN and len(traded_today) < MIN_DAILY_TRADES:
            picked = {c[0] for c in candidates}
            for name, info in sorted(data.items(),
                                     key=lambda kv: -abs(kv[1]["momentum"])):
                if name in traded_today or name in picked:
                    continue
                d = "BULL" if info["momentum"] >= 0 else "BEAR"
                consider(name, info, d,
                         tag=f" [momentum {info['momentum']:+.1f}%]")

    # ---- ONE batched live-quote call for positions + candidates --------
    tokens = [p["token"] for p in positions if p.get("token")]
    tokens += [rec["token"] for _, rec, _, _ in candidates]
    quotes = angel.get_quotes(smart, list(dict.fromkeys(tokens)))
    print(f"  Live quotes: {len(quotes)}/{len(set(tokens))} tokens fetched")

    # ---- manage open positions on LIVE prices --------------------------
    for pos in positions:
        token = pos.get("token")
        q = quotes.get(str(token)) if token else None
        info = data.get(pos["symbol"])
        direction = info["direction"] if info else None

        if q is None or q["ltp"] <= 0:
            # No live price: do NOT invent one.
            if now_min >= T_SQUARE_OFF and pos.get("last_price"):
                close_option(conn, pos, pos["last_price"],
                             "Square-off 15:15 (no live quote - last observed price)",
                             log, None)
            else:
                trace(event="hold_no_quote", symbol=pos.get("trading_symbol"),
                      token=token, reason="no_live_quote_this_cycle")
            continue

        # exit fills at the live BID (crossing the spread), else live LTP
        mark = q["bid"] if q["bid"] > 0 else q["ltp"]

        high_water = max(pos["high_water"] or pos["entry_price"], mark)
        stop_price = pos["stop_price"] or pos["entry_price"] * (1 + INITIAL_STOP_PCT)
        if high_water >= pos["entry_price"] * (1 + TRAIL_ACTIVATE_PCT):
            stop_price = max(stop_price, high_water * (1 - TRAIL_PCT))
        conn.execute("UPDATE options_positions SET high_water=?, stop_price=?, "
                     "last_price=? WHERE id=?",
                     (high_water, stop_price, mark, pos["id"]))
        pos["high_water"], pos["stop_price"] = high_water, stop_price

        trace(event="mark", ts=datetime.now().isoformat(timespec="seconds"),
              symbol=pos.get("trading_symbol"), token=token,
              ltp=f"{q['ltp']:.2f}", bid=f"{q['bid']:.2f}", ask=f"{q['ask']:.2f}",
              volume=q["volume"], oi=q["oi"], mark=f"{mark:.2f}",
              entry=f"{pos['entry_price']:.2f}", high_water=f"{high_water:.2f}",
              stop=f"{stop_price:.2f}",
              unrealised=f"{(mark - pos['entry_price']) * pos['qty']:.0f}")

        reversed_ = ((pos["option_type"] == "CE" and direction == "BEAR") or
                     (pos["option_type"] == "PE" and direction == "BULL"))
        expiry = datetime.strptime(pos["expiry"], "%Y-%m-%d").date()

        if mark <= stop_price:
            # STOP-PRICE FILL: book the exit AT the stop level, not at the
            # price we happen to observe on waking. A resting stop order
            # sitting with the broker triggers the instant price touches
            # the level; it does not wait for our next poll. Filling at
            # `mark` charged the strategy twice - once for the market move
            # and again for our polling latency - which understated gains
            # on trailing-stop exits exactly as it overstated losses on
            # initial-stop exits.
            #
            # Applied identically to winners and losers, so neither side is
            # flattered. Labelled STOP_LEVEL (not LIVE_*) so the audit trail
            # never claims this fill came off the live quote; the observed
            # market price is still recorded alongside it in the trace.
            #
            # Assumes an ideal fill at the level. If the premium genuinely
            # gapped through the stop with no trades in between, a real
            # order would have filled worse than this.
            trailing = high_water > pos["entry_price"] * (1 + TRAIL_ACTIVATE_PCT)
            close_option(conn, pos, stop_price,
                         "Trailing stop" if trailing else "Initial stop",
                         log, q, price_source="STOP_LEVEL")
        elif reversed_:
            close_option(conn, pos, mark, "Trend reversal exit", log, q)
        elif expiry < today:
            close_option(conn, pos, mark, "Expired contract", log, q)
        elif now_min >= T_SQUARE_OFF:
            close_option(conn, pos, mark, "Square-off 15:15", log, q)

    if not in_window:
        conn.commit()
        return

    # ---- entries, on LIVE quotes only ----------------------------------
    open_count = conn.execute(
        "SELECT COUNT(*) FROM options_positions").fetchone()[0]
    iv_cache = {}
    for name, rec, direction, tag in candidates:
        if open_count >= MAX_POSITIONS:
            break
        if name in traded_today:
            continue
        q = quotes.get(rec["token"])
        if q is None or q["ltp"] <= 0:
            trace(event="entry_rejected", symbol=rec["symbol"],
                  token=rec["token"], reason="no_live_quote")
            continue

        # analytics only - fair value / mispricing edge, never the fill
        key = (name, rec["expiry"])
        if key not in iv_cache:
            iv_cache[key] = angel.fetch_option_iv(smart, name, rec["expiry"])
        fair = fair_value(data[name]["spot"], rec, today, iv_cache[key])

        if open_option(conn, rec, q, data[name]["spot"], direction, today,
                       log, fair, tag):
            traded_today.add(name)
            open_count += 1

    conn.commit()


# ----------------------------------------------------------------- main ---
def main():
    conn = db_init()
    today = date.today()
    log = []

    positions = load_positions(conn)
    if not positions:
        print(f"[{datetime.now():%H:%M}] Options: no open positions | "
              f"Cash: Rs.{cash(conn):,.0f}")
    else:
        print(f"[{datetime.now():%H:%M}] Options: {len(positions)} open position(s)")
        for p in positions:
            print(f"  {p.get('trading_symbol') or p['symbol']} "
                  f"x{p['qty']} @ {p['entry_price']:.2f} "
                  f"(high {p['high_water']:.2f}, stop {p['stop_price']:.2f})")

    process(conn, log, today)
    if log:
        print("\n".join(log))
    conn.close()


if __name__ == "__main__":
    main()
