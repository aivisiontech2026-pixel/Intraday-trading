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
  - The high-water mark includes peaks that occur BETWEEN polls, taken
    from the session high in each quote, so a spike that fades before the
    next cycle still ratchets the stop (a resting stop order would have
    caught it). Symmetrically, a session low through the stop triggers the
    exit. Baselined at entry so pre-entry extremes are excluded.
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
import ranking_engine as ranking

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

# How stale the UNDERLYING signal may be before it is refetched.
#
# The signal (EMA9/21 + VWAP) is computed on 5-MINUTE bars, so it cannot
# change more often than a new bar closes. Refetching 20 symbols from
# yfinance every minute would therefore recompute an identical answer
# ~4 times out of 5 while quintupling the request rate - and yfinance
# already rate-limits us at the current cadence (all 18 pre-market
# symbols failed on 2026-08-04).
#
# So: signals refresh on the bar cadence, while option QUOTES are pulled
# every cycle. That is what makes sub-5-minute polling worthwhile - the
# gain is faster stop-loss reaction, not fresher signals.
SIGNAL_MAX_AGE_SEC = 285    # just under 5 min, so a 5-min bar is never missed

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
        # ranking-engine instrumentation (shadow mode stamps every trade)
        ("options_positions", "entry_bid", "REAL"),
        ("options_positions", "entry_ask", "REAL"),
        ("options_positions", "entry_oi", "INTEGER"),
        ("options_positions", "entry_volume", "INTEGER"),
        ("options_positions", "entry_score", "REAL"),
        ("options_positions", "entry_rank", "INTEGER"),
        ("options_positions", "entry_tier", "TEXT"),
        ("options_trades", "entry_score", "REAL"),
        ("options_trades", "entry_rank", "INTEGER"),
        ("options_trades", "entry_tier", "TEXT"),
        # intra-interval extremes: the day's high/low as last observed, so a
        # peak or trough BETWEEN two polls is not lost - see process()
        ("options_positions", "day_high_seen", "REAL"),
        ("options_positions", "day_low_seen", "REAL"),
        ("options_positions", "peak_source", "TEXT"),
        ("options_trades", "peak_source", "TEXT"),
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
            "entry_fair_value", "entry_bid", "entry_ask", "entry_oi",
            "entry_volume", "entry_score", "entry_rank", "entry_tier",
            "day_high_seen", "day_low_seen", "peak_source"]
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
    """BULL / BEAR / None from EMA9-21 + VWAP confluence on the UNDERLYING.

    Indices report ZERO volume on yfinance, so a volume-weighted VWAP is
    undefined there - previously this returned None unconditionally for
    NIFTY/BANKNIFTY, silently disabling their confluence signal for the
    entire life of the bot (found by the signal-quality study, where the
    market-alignment bucket came back n=0). Falls back to a volume-less
    proxy (mean close) for that case, same approach as intraday_backtest.
    """
    if df is None or df.empty or len(df) < 21:
        return None
    ema9 = df["Close"].ewm(span=9).mean().iloc[-1]
    ema21 = df["Close"].ewm(span=21).mean().iloc[-1]
    vol = df["Volume"].sum()
    if vol:
        vwap = (df["Close"] * df["Volume"]).sum() / vol
    else:
        vwap = df["Close"].mean()
    last = df["Close"].iloc[-1]
    if ema9 > ema21 and last > vwap:
        return "BULL"
    if ema9 < ema21 and last < vwap:
        return "BEAR"
    return None


def get_trend_quality(df):
    """Fraction of the last 12 bars whose EMA9/21 relationship matches the
    current one. NOTE (evidence): the 60d study found FRESH trends
    (tq < 1.0) outperformed fully-established ones intraday, so the
    ranking engine scores freshness higher - see ranking_engine.py."""
    if df is None or df.empty or len(df) < 21:
        return 0.5
    ema9 = df["Close"].ewm(span=9).mean()
    ema21 = df["Close"].ewm(span=21).mean()
    up_now = ema9.iloc[-1] > ema21.iloc[-1]
    tail = (ema9 > ema21).tail(12)
    return float(tail.mean()) if up_now else float((~tail).mean())


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
                fair, tag="", score=None, rank_pos=None, tier=None):
    """Open a position. Entry fills at the live ASK (crossing the spread),
    falling back to live LTP when there is no depth. Never a model price.
    The candidate's ranking-engine score/rank/tier and the full entry
    quote snapshot are stamped on the position for later attribution."""
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

    # Baseline the session extremes AT ENTRY. The day's high/low are
    # cumulative from 09:15, so they may already contain a spike that
    # happened before we bought. Only a LATER rise above this baseline can
    # have occurred while we held the position - see process().
    conn.execute(
        "INSERT INTO options_positions(symbol,option_type,strike,expiry,qty,"
        "entry_price,entry_time,high_water,stop_price,token,trading_symbol,"
        "lots,lotsize,last_price,entry_fair_value,entry_bid,entry_ask,"
        "entry_oi,entry_volume,entry_score,entry_rank,entry_tier,"
        "day_high_seen,day_low_seen) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["name"], rec["opt_type"], rec["strike"], rec["expiry"].isoformat(),
         qty, entry_px, datetime.now().isoformat(), entry_px, initial_stop,
         rec["token"], rec["symbol"], lots, lotsize, entry_px, fair,
         bid, ask, quote.get("oi"), quote.get("volume"),
         score, rank_pos, tier,
         quote.get("high") or entry_px, quote.get("low") or entry_px))
    meta_set(conn, "cash", cash(conn) - cost)

    edge = f"{entry_px - fair:+.2f}" if fair else "n/a"
    trace(event="ENTRY", ts=datetime.now().isoformat(timespec="seconds"),
          symbol=rec["symbol"], token=rec["token"], ltp=f"{ltp:.2f}",
          bid=f"{bid:.2f}", ask=f"{ask:.2f}", fill=f"{entry_px:.2f}",
          volume=quote["volume"], oi=quote["oi"], spot=f"{spot:.2f}",
          signal=underlying_dir, lots=lots, lotsize=lotsize, qty=qty,
          cost=f"{cost:.0f}", stop=f"{initial_stop:.2f}",
          fair_value=f"{fair:.2f}" if fair else "n/a", edge=edge,
          rank_score=score if score is not None else "n/a",
          rank=rank_pos if rank_pos is not None else "n/a", tier=tier or "n/a",
          price_source="LIVE_ASK" if ask > 0 else "LIVE_LTP")

    msg = (f"📊 OPTIONS: BOUGHT {lots} lot(s) ({qty}) {rec['symbol']} "
           f"@ Rs.{entry_px:.2f}{tag} | cost Rs.{cost:,.0f} | "
           f"stop Rs.{initial_stop:.2f}")
    log.append(msg)
    telegram(msg)
    return True


def close_option(conn, pos, exit_px, reason, log, quote=None, price_source=None,
                 peak_source=None):
    """Close a position.

    `exit_px` is normally the live market price. For stop-triggered exits
    the caller passes the STOP LEVEL instead and sets
    price_source="STOP_LEVEL" - see the stop branch in process() for why.

    `peak_source` records whether the high-water mark that set the trailing
    stop came from a polled price (POLL) or from the session high observed
    between polls (INTRA_INTERVAL_HIGH), so the two can be compared in
    attribution.
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
        "trading_symbol,lots,lotsize,exit_bid,exit_ask,exit_oi,price_source,"
        "entry_bid,entry_ask,entry_oi,entry_volume,entry_score,entry_rank,"
        "entry_tier,peak_source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pos["symbol"], pos["option_type"], pos["strike"], pos["expiry"], qty,
         pos["entry_price"], exit_px, pos["entry_time"],
         datetime.now().isoformat(), pnl, reason, pos.get("token"),
         pos.get("trading_symbol"), pos.get("lots"), pos.get("lotsize"),
         quote.get("bid") if quote else None,
         quote.get("ask") if quote else None,
         quote.get("oi") if quote else None, src,
         pos.get("entry_bid"), pos.get("entry_ask"), pos.get("entry_oi"),
         pos.get("entry_volume"), pos.get("entry_score"),
         pos.get("entry_rank"), pos.get("entry_tier"), peak_source))
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
          reason=reason, price_source=src, peak_source=peak_source or "n/a",
          high_water=f"{pos.get('high_water') or pos['entry_price']:.2f}")

    emoji = "✅" if pnl > 0 else "❌"
    msg = (f"{emoji} OPTIONS: SOLD {pos.get('lots')} lot(s) ({qty}) "
           f"{pos.get('trading_symbol')} @ Rs.{exit_px:.2f} | "
           f"P&L Rs.{pnl:,.0f} ({pnl_pct:+.1f}%) | {reason}")
    log.append(msg)
    telegram(msg)
    return pnl


# ----------------------------------------------------------------- engine ---
def load_cached_signals(conn, now):
    """Underlying signals from the last refresh, if still within the bar
    cadence. Returns (data, age_seconds) or (None, age_or_None).

    Timestamps are stored and compared as NAIVE local time. process()
    passes an AWARE `now` (datetime.now().astimezone()), so it is
    normalised here - mixing the two raises TypeError, which was
    previously swallowed and made the cache silently never hit.
    """
    raw = meta_get(conn, "signal_cache")
    if not raw:
        return None, None
    try:
        blob = json.loads(raw)
        naive_now = now.replace(tzinfo=None)
        age = (naive_now - datetime.fromisoformat(blob["ts"])).total_seconds()
    except (ValueError, KeyError, TypeError) as e:
        print(f"  Signals: cache unreadable ({type(e).__name__}) - refetching")
        return None, None
    if age < 0 or age > SIGNAL_MAX_AGE_SEC:
        return None, age
    return blob["data"], age


def save_cached_signals(conn, data, now):
    """Persist signals with a NAIVE timestamp - see load_cached_signals."""
    meta_set(conn, "signal_cache",
             json.dumps({"ts": now.replace(tzinfo=None).isoformat(),
                         "data": data}))


def square_off_net(conn, log, now_min, degraded_reason):
    """Unconditional 15:15 square-off, usable in DEGRADED mode.

    The strategy's core guarantee is that no position is ever carried
    overnight. That guarantee must not depend on Angel One being
    reachable, on the instrument master downloading, or on yfinance
    returning bars - all of which are external services that can fail.

    Uses the LAST OBSERVED LIVE price stored on the position each cycle
    (falling back to the entry price if the position never got a mark).
    That is a real, previously-observed market price - stale, and
    labelled as such - never a model price.

    No-op before 15:15: during the session a position with no quote is
    correctly held and retried, not force-closed on a data hiccup.
    """
    if now_min < T_SQUARE_OFF:
        return
    stranded = load_positions(conn)
    if not stranded:
        return
    print(f"  SQUARE-OFF NET: {len(stranded)} position(s) still open past "
          f"15:15 while degraded ({degraded_reason}) - closing at last "
          f"observed price rather than carrying overnight.")
    for pos in stranded:
        px = pos.get("last_price") or pos.get("entry_price")
        close_option(conn, pos, px,
                     f"Square-off 15:15 (degraded: {degraded_reason} - "
                     f"last observed price)", log, None,
                     price_source="LAST_OBSERVED_LIVE")


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
    # NOTE: every early return below MUST first run the square-off net.
    # On 2026-08-04 four positions were carried overnight because a
    # degraded-data return skipped position management entirely - which
    # silently broke the strategy's core promise that nothing is ever
    # held past 15:15.
    smart = angel.login()
    if smart is None:
        print("  Angel One unavailable -> no NEW option trades this cycle "
              "(execution requires live quotes; synthetic pricing is not "
              "permitted).")
        square_off_net(conn, log, now_min, "Angel One unavailable")
        conn.commit()
        return
    master = angel.load_instrument_master(UNIVERSE_NAMES)
    if not master:
        print("  Instrument master unavailable -> no NEW option trades.")
        square_off_net(conn, log, now_min, "instrument master unavailable")
        conn.commit()
        return

    # ---- underlying signals (yfinance 5-min bars, refreshed on the bar
    # cadence rather than every cycle - see SIGNAL_MAX_AGE_SEC) ----------
    data, age = load_cached_signals(conn, now)
    signals_fresh = data is None
    if data is not None:
        print(f"  Signals: reusing cache ({age:.0f}s old, 5-min bar unchanged)"
              f" - {len(data)} symbols, no yfinance calls this cycle")
    else:
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
                          "momentum": get_momentum(df, today),
                          "trend_quality": get_trend_quality(df)}
        if data:
            print(f"  Signals: refreshed {len(data)} symbols from yfinance")
    if "NIFTY" not in data:
        print("  No underlying data -> no NEW option trades this cycle.")
        square_off_net(conn, log, now_min, "no underlying data")
        conn.commit()
        return

    # relative strength: today's move vs NIFTY's (evidence: the strongest
    # measured ranking feature - see ranking_engine.py header)
    nifty_mom = data["NIFTY"]["momentum"]
    for name, info in data.items():
        info["rel_strength"] = round(info["momentum"] - nifty_mom, 3)
    if signals_fresh:
        save_cached_signals(conn, data, now)

    positions = load_positions(conn)

    # ---- decide which contracts we may enter, so quotes can be batched --
    traded_today = symbols_traded_today(conn, today_str)
    open_count = len(positions)
    # Entries require FRESH signals. On a cached-signal cycle the bot is
    # here purely to mark positions and enforce stops against live option
    # quotes - opening a new position off a signal it already acted on
    # would just re-trade the same bar.
    in_window = (T_ENTRY_START <= now_min <= T_ENTRY_END) and signals_fresh

    candidates = []   # dicts: name/rec/direction/tier/tag + score features
    if in_window:
        def consider(name, info, direction, tier, tag=""):
            exp = angel.nearest_expiry(master, name, today, min_dte=1)
            if exp is None:
                trace(event="entry_skipped", symbol=name, reason="no_listed_expiry")
                return
            opt_type = "CE" if direction == "BULL" else "PE"
            rec = angel.find_option(master, name, exp, info["spot"], opt_type)
            if rec is None:
                trace(event="entry_skipped", symbol=name, reason="no_listed_strike")
                return
            candidates.append({
                "name": name, "rec": rec, "direction": direction,
                "tier": tier, "tag": tag,
                "momentum": info["momentum"],
                "rel_strength": info["rel_strength"],
                "trend_quality": info["trend_quality"],
            })

        for name, info in data.items():
            if name in traded_today or info["direction"] is None:
                continue
            consider(name, info, info["direction"], "confluence")

        if now_min >= FALLBACK_START_MIN and len(traded_today) < MIN_DAILY_TRADES:
            picked = {c["name"] for c in candidates}
            for name, info in sorted(data.items(),
                                     key=lambda kv: -abs(kv[1]["momentum"])):
                if name in traded_today or name in picked:
                    continue
                d = "BULL" if info["momentum"] >= 0 else "BEAR"
                consider(name, info, d, "momentum",
                         tag=f" [momentum {info['momentum']:+.1f}%]")

    # ---- ONE batched live-quote call for positions + candidates --------
    tokens = [p["token"] for p in positions if p.get("token")]
    tokens += [c["rec"]["token"] for c in candidates]
    quotes = angel.get_quotes(smart, list(dict.fromkeys(tokens)))
    print(f"  Live quotes: {len(quotes)}/{len(set(tokens))} tokens fetched")

    # ---- ranking engine: score every candidate, every cycle ------------
    # In "shadow" (default) this ONLY logs and stamps scores - the
    # baseline keeps deciding. In "active" the ranked selection decides.
    rcfg = ranking.get_config(CFG)
    rank_mode = rcfg.get("mode", "shadow")
    ranked = []
    if candidates and rank_mode != "off":
        for c in candidates:
            c["quote"] = quotes.get(c["rec"]["token"])
        history = ranking.load_history(conn, rcfg["min_history_n"])
        ranked = ranking.rank(candidates, data["NIFTY"]["direction"],
                              rcfg, history)
        open_sectors = {}
        for p in positions:
            sec = ranking.SECTOR.get(p["symbol"], "Other")
            open_sectors[sec] = open_sectors.get(sec, 0) + 1
        slots = max(0, MAX_POSITIONS - open_count)
        picks, rejections = ranking.select(ranked, rcfg, open_sectors, slots)
        ranking.log_cycle(conn, rank_mode, ranked, picks)
        for c in ranked:
            trace(event="rank", symbol=c["name"], rank=c["rank"],
                  score=c["score"], tier=c["tier"],
                  would_trade=1 if c in picks else 0)
        if rank_mode == "active":
            for c, why in rejections:
                trace(event="rank_rejected", symbol=c["name"],
                      score=c["score"], reason=why)

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

        # ---- INTRA-INTERVAL PEAK CAPTURE ---------------------------------
        # The bot only sees prices at poll instants. A premium that spikes
        # and fades between two polls was invisible, so the high-water mark
        # - and therefore the trailing stop - was built from a SAMPLED
        # series while a real broker's stop watches every tick.
        #
        # This cost a real trade: NIFTY11AUG2624600CE on 2026-08-05 traded
        # to ~193 but the bot's sampled peak was 183.25, putting the trail
        # at 161.26 (BELOW the 164.80 entry) instead of 169.84 (above it).
        # A +Rs.655 winner was booked as a -Rs.460 loss. The evidence was
        # already in the quote: Angel One returns the session high/low on
        # every call, and this code discarded them.
        #
        # The day's high is cumulative from 09:15, so it can contain a spike
        # that predates our entry - ratcheting off it directly would invent
        # profit we never had. Only a rise ABOVE the value seen on the
        # previous poll can have happened during the interval just elapsed,
        # i.e. while we held. That delta is what we ratchet on.
        #
        # This mirrors a resting trailing-stop order: if the premium ran
        # 183 -> 193 -> 161 inside one interval, a real stop would have
        # ratcheted to 169.84 on the way up and filled there on the way
        # down. Same answer. Consistent with the STOP_LEVEL fill contract.
        day_high, day_low = q.get("high") or 0.0, q.get("low") or 0.0
        prev_high = pos.get("day_high_seen") or day_high
        prev_low = pos.get("day_low_seen") or day_low

        # peak_source is STICKY: it records how the high-water mark that set
        # the current stop was obtained, so it must persist on the position.
        # Recomputing it at exit would report the last cycle's source, not
        # the cycle that actually captured the peak.
        high_water = max(pos["high_water"] or pos["entry_price"], mark)
        peak_source = pos.get("peak_source") or "POLL"
        if day_high > prev_high and day_high > high_water:
            high_water = day_high
            peak_source = "INTRA_INTERVAL_HIGH"

        stop_price = pos["stop_price"] or pos["entry_price"] * (1 + INITIAL_STOP_PCT)
        if high_water >= pos["entry_price"] * (1 + TRAIL_ACTIVATE_PCT):
            stop_price = max(stop_price, high_water * (1 - TRAIL_PCT))

        # A new session LOW below the stop means the premium traded through
        # the stop between polls - a resting order would already have
        # filled. Evaluate the exit against that trough, not just the
        # wake-up price, so a spike-down through the stop is not missed.
        trough = day_low if (day_low > 0 and day_low < prev_low) else mark
        trigger = min(mark, trough)

        conn.execute("UPDATE options_positions SET high_water=?, stop_price=?, "
                     "last_price=?, day_high_seen=?, day_low_seen=?, "
                     "peak_source=? WHERE id=?",
                     (high_water, stop_price, mark,
                      max(day_high, prev_high), min(day_low, prev_low) if day_low
                      else prev_low, peak_source, pos["id"]))
        pos["high_water"], pos["stop_price"] = high_water, stop_price

        trace(event="mark", ts=datetime.now().isoformat(timespec="seconds"),
              symbol=pos.get("trading_symbol"), token=token,
              ltp=f"{q['ltp']:.2f}", bid=f"{q['bid']:.2f}", ask=f"{q['ask']:.2f}",
              day_high=f"{day_high:.2f}", day_low=f"{day_low:.2f}",
              volume=q["volume"], oi=q["oi"], mark=f"{mark:.2f}",
              entry=f"{pos['entry_price']:.2f}", high_water=f"{high_water:.2f}",
              peak_source=peak_source, trigger=f"{trigger:.2f}",
              stop=f"{stop_price:.2f}",
              unrealised=f"{(mark - pos['entry_price']) * pos['qty']:.0f}")

        reversed_ = ((pos["option_type"] == "CE" and direction == "BEAR") or
                     (pos["option_type"] == "PE" and direction == "BULL"))
        expiry = datetime.strptime(pos["expiry"], "%Y-%m-%d").date()

        if trigger <= stop_price:
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
            # >= matches the activation test above. With > , a peak landing
            # exactly on the activation threshold trailed the stop but was
            # still recorded as "Initial stop", mislabelling the exit in
            # every attribution report.
            trailing = high_water >= pos["entry_price"] * (1 + TRAIL_ACTIVATE_PCT)
            close_option(conn, pos, stop_price,
                         "Trailing stop" if trailing else "Initial stop",
                         log, q, price_source="STOP_LEVEL",
                         peak_source=peak_source)
        elif reversed_:
            close_option(conn, pos, mark, "Trend reversal exit", log, q,
                         peak_source=peak_source)
        elif expiry < today:
            close_option(conn, pos, mark, "Expired contract", log, q,
                         peak_source=peak_source)
        elif now_min >= T_SQUARE_OFF:
            close_option(conn, pos, mark, "Square-off 15:15", log, q,
                         peak_source=peak_source)

    if not in_window:
        conn.commit()
        return

    # ---- entries, on LIVE quotes only ----------------------------------
    # active mode: trade the ranked picks. shadow/off: baseline order,
    # with the candidate's score stamped on the trade for attribution.
    open_count = conn.execute(
        "SELECT COUNT(*) FROM options_positions").fetchone()[0]
    if rank_mode == "active" and ranked:
        entry_list = picks
    else:
        entry_list = candidates
    score_by_name = {c["name"]: c for c in ranked}

    iv_cache = {}
    for cand in entry_list:
        name, rec = cand["name"], cand["rec"]
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

        sc = score_by_name.get(name, {})
        if open_option(conn, rec, q, data[name]["spot"], cand["direction"],
                       today, log, fair, cand.get("tag", ""),
                       score=sc.get("score"), rank_pos=sc.get("rank"),
                       tier=cand.get("tier")):
            traded_today.add(name)
            open_count += 1

    conn.commit()


# ----------------------------------------------------------------- main ---
def selfcheck(conn):
    """Fail LOUD on a broken write path, before any trading happens.

    A 26-column / 25-placeholder mismatch in close_option()'s INSERT
    shipped undetected and crashed EVERY option close - positions were
    opened and then carried overnight because the exit threw. The
    workflow pipes stdout through `tee`, which discards the exit code,
    so the step still reported success. Exercising the write path here
    against a throwaway transaction turns that class of failure into an
    immediate, visible error.
    """
    probe = {"id": -1, "symbol": "_SELFCHECK", "option_type": "CE",
             "strike": 0.0, "expiry": date.today().isoformat(), "qty": 1,
             "entry_price": 1.0, "entry_time": "1970-01-01T00:00:00",
             "high_water": 1.0, "stop_price": 1.0, "token": "0",
             "trading_symbol": "_SELFCHECK", "lots": 1, "lotsize": 1,
             "last_price": 1.0, "entry_fair_value": None,
             "entry_bid": None, "entry_ask": None, "entry_oi": None,
             "entry_volume": None, "entry_score": None,
             "entry_rank": None, "entry_tier": None}
    # Silence BOTH sinks. close_option() sends a Telegram alert on every
    # exit, and the probe drives close_option() - so the check was pushing
    # a fake "SOLD _SELFCHECK" message to the phone on every single cycle.
    # The trade itself was always correctly rolled back; only the
    # notification escaped, because the rollback cannot recall it.
    global trace, telegram
    real_trace, trace = trace, lambda **kw: None
    real_tg, telegram = telegram, lambda msg: None
    try:
        conn.execute("SAVEPOINT selfcheck")
        conn.execute(
            "INSERT INTO options_positions(id,symbol,option_type,strike,"
            "expiry,qty,entry_price,entry_time,high_water,stop_price,token,"
            "trading_symbol,lots,lotsize,last_price) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (probe["id"], probe["symbol"], probe["option_type"],
             probe["strike"], probe["expiry"], probe["qty"],
             probe["entry_price"], probe["entry_time"], probe["high_water"],
             probe["stop_price"], probe["token"], probe["trading_symbol"],
             probe["lots"], probe["lotsize"], probe["last_price"]))
        close_option(conn, probe, 1.0, "selfcheck", [], None,
                     price_source="SELFCHECK")
    except Exception as e:
        print(f"  FATAL: trade write path is broken - {type(e).__name__}: {e}")
        print("  Refusing to trade. Fix the schema/SQL before running again.")
        raise
    finally:
        trace, telegram = real_trace, real_tg
        conn.execute("ROLLBACK TO selfcheck")
        conn.execute("RELEASE selfcheck")
    return True


def main():
    conn = db_init()
    today = date.today()
    log = []

    selfcheck(conn)   # aborts loudly if open/close SQL is broken

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
