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
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

import angelone_client as angel
import ranking_engine as ranking
import config_contract
# Independent safety layer (S-04/S-06). Sits ABOVE the strategy: it can
# withhold NEW-ENTRY permission and can do nothing else. It never closes,
# sizes or selects a position, and the strategy cannot disable it.
import safety_supervisor
# Observability only (P0-E). Every call is fail-open and returns a value
# no trading branch reads. See telemetry.py's architectural contract.
import telemetry
# Wednesday 2026-08-26 stabilization gates (ENTRY-ONLY), the gate ledger,
# the U-014 trail floor and the reporting-only cost model. Every rule is
# configurable and reversible from intraday_config.json without a code
# revert. See stabilization.py's header for the exit-path invariant.
import stabilization as stab

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
DB = HERE / "options_trades.db"
CFG_FILE = HERE / "intraday_config.json"
CFG = json.loads(CFG_FILE.read_text()) if CFG_FILE.exists() else {}

# S-03: one authoritative configuration source. Each value below was
# verified equal to the constant previously hardcoded here, so adopting
# the contract is value-neutral. See config_contract.py for the full
# classification of every declared key, including the ones that remain
# DEFERRED (cost_per_side, slippage, risk_per_trade_percent) because
# enforcing them would change P&L or sizing - strategy behaviour, out of
# scope for stabilization.
CONTRACT = config_contract.Contract(CFG)

CAPITAL = CONTRACT.capital                  # 100_000
MAX_PER_TRADE = CONTRACT.max_per_trade      # 25_000
INITIAL_STOP_PCT = -0.15
TRAIL_ACTIVATE_PCT = 0.10
TRAIL_PCT = 0.12
T_SQUARE_OFF = CONTRACT.square_off          # 15:15
INTERVAL = CONTRACT.interval                # "5m"

MIN_DAILY_TRADES = 5
MAX_POSITIONS = CONTRACT.max_positions      # 4 x Rs.25,000 = full book
FALLBACK_START_MIN = 11 * 60

# --- Wednesday 2026-08-26 stabilization policy ------------------------------
# TEMPORARY, CONFIGURABLE, REVERSIBLE. `"stabilization": {"enabled": false}`
# in intraday_config.json restores baseline behaviour exactly, with no code
# revert, and the ledger then records that the gates were off.
STAB = stab.get_config(CFG)
STAB_ON = bool(STAB["enabled"])

# CHANGE 6: concurrent option positions, capped for stabilization.
#
# Deliberately a SEPARATE configuration key from `max_open_positions`.
# That key is ALSO read by paper_trader.py:94 and intraday_backtest.py:68,
# so lowering it would change the STOCK book too - out of scope per the
# authorization. `max_option_positions` is options-only and defaults to 2.
#
# It is NOT claimed to be the optimal position count. Measured in-sample,
# a 2-position cap removed only ~4% of the 09:31 damage (-Rs.22,269 vs
# -Rs.23,123): position count is risk CONTAINMENT, and the stale-signal
# gate below is the independent data-validity fix.
MAX_OPTION_POSITIONS = (int(STAB["max_option_positions"]) if STAB_ON
                        else MAX_POSITIONS)

# CHANGE 11: end-of-day gate summary, sent once at/after this minute even
# when the session produced no trade at all.
T_GATE_SUMMARY = 15 * 60 + 35

# CHANGE 3 / CHANGE 15: how long after an exit the price path is followed,
# and how far apart the observations may be. Post-exit observation is
# PASSIVE - the tokens ride the quote batch that already runs every cycle,
# so it adds no market-data request.
POST_EXIT_WINDOW_MIN = 60
POST_EXIT_KEY = "post_exit_watch"
T_ENTRY_START, T_ENTRY_END = CONTRACT.entry_start, CONTRACT.entry_end

# EXPERIMENT (default OFF): same-cycle candidate-reuse guard.
#
# The candidate list is built once per cycle, BEFORE the position-
# management loop runs. When an exit frees a slot mid-cycle, the entry
# loop then refills it from that PRE-EXIT list. Measured over Aug 10-13:
# 14 such same-cycle re-entries, 0 winners, -Rs.26,180.
#
# NOT a time-based cooldown: it does not delay by a fixed interval. It
# ends the current entry evaluation so the next entry-eligible cycle
# rebuilds candidates from freshly downloaded signals.
#
# Counter-evidence on record: in the profitable window the same mechanism
# produced 2 winners from 3 attempts (+Rs.11,347), including the largest
# trade in the book's history (SBIN +Rs.13,125 on 2026-08-06, entered one
# second after a loss). This flag would have skipped that trade.
#
# Absent from config => False => behaviour byte-identical to baseline.
EXPERIMENT_NO_SAME_CYCLE_REENTRY = bool(
    CFG.get("experiments", {}).get("no_same_cycle_reentry", False))

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
# S-09: seconds a bar covers, matching telemetry._bar_status. Used only to
# tell a FORMING bar from a CLOSED one - it is not a cadence.
BAR_SECONDS = 300

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


# ------------------------------------------- S-34b refresh throttle ---
# S-34a bounds how LONG one instrument-master refresh may block. It does
# not change how OFTEN one is attempted, and while the cache is stale
# every cycle attempts another: on 2026-08-21 that was 208 attempts
# across 208 cycles, 89 of which failed, for 129.6 minutes of blocked
# time in a single session. Position management sits downstream of the
# master load, so every one of those seconds delayed stop evaluation.
#
# S-33 already made frequent refresh unnecessary for CORRECTNESS: a
# validated cache up to 72h old is authorized to trade. So after a
# failure it is safe to wait before trying again.
#
# THE THROTTLE IS AN OPTIMIZATION, NEVER A SAFETY AUTHORITY. Every way
# of failing to read it - absent, empty, malformed, future-dated,
# unreadable database - permits the refresh. Bad state can only ever
# cause one extra bounded attempt; trusting bad state could suppress an
# attempt that was needed.
MASTER_REFRESH_RETRY_S = 30 * 60
MASTER_REFRESH_KEY = "master_refresh_failed_at"


def master_refresh_allowed(conn, now_epoch=None):
    """May a network refresh be attempted this cycle?

    Epoch seconds, deliberately the same clock basis as the cache mtime
    S-33 measures age with, so the two are directly comparable and
    neither depends on the runner's timezone.

    Returns True on every anomaly. `now_epoch` is injectable so the
    boundary can be tested without wall-clock timing.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    try:
        raw = meta_get(conn, MASTER_REFRESH_KEY)
    except Exception as e:                      # database unreadable
        print(f"  Master refresh throttle: state unreadable "
              f"({type(e).__name__}) - allowing refresh")
        return True
    if raw is None or str(raw).strip() == "":
        return True                             # never failed, or cleared
    try:
        failed_at = float(raw)
    except (TypeError, ValueError):
        print("  Master refresh throttle: state malformed - allowing refresh")
        return True
    elapsed = now_epoch - failed_at
    if elapsed < 0:
        # Future timestamp: a backwards clock step, or state written by a
        # runner whose clock ran ahead. Waiting it out could suppress
        # refreshes for an unbounded time, so it is treated as no state.
        print("  Master refresh throttle: timestamp is in the future "
              "- allowing refresh")
        return True
    return elapsed >= MASTER_REFRESH_RETRY_S


def record_master_refresh(conn, health, now_epoch=None):
    """Persist throttle state from how the loader actually resolved.

    A THROTTLED SKIP must never stamp the timestamp. If it did, the
    interval would restart on every cycle and the suppression would
    extend itself indefinitely - which is why this keys off
    `refresh_attempted` rather than off the status alone.

    Persistence failure is swallowed: the throttle must never become a
    new way for trading to break.
    """
    if not health.get("refresh_attempted"):
        return                                  # skipped: leave state alone
    try:
        if health.get("status") == "OK":
            meta_set(conn, MASTER_REFRESH_KEY, "")          # cleared
        else:
            # Includes a download that succeeded but validated as
            # unusable - no new master was obtained, so it is a failure.
            meta_set(conn, MASTER_REFRESH_KEY,
                     time.time() if now_epoch is None else now_epoch)
    except Exception as e:
        print(f"  Master refresh throttle: state not persisted "
              f"({type(e).__name__}) - trading unaffected")


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
    # S-02: adjust=False matches backtest.py::ema() and the documented
    # definition. Live previously used the pandas default adjust=True,
    # which is a different estimator. P0-A replayed all 87 reconstructable
    # entries under both and found ZERO direction flips - so this is a
    # parity correction with no observed decision impact in that window.
    # It is NOT claimed to be universally behaviour-neutral: the two
    # estimators differ numerically and could diverge on future data.
    ema9 = df["Close"].ewm(span=9, adjust=False).mean().iloc[-1]
    ema21 = df["Close"].ewm(span=21, adjust=False).mean().iloc[-1]
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


def _final_bar_proven_closed(df, observed_at):
    """True ONLY when the final bar can be PROVEN complete.

    Returns False for every form of doubt. Never raises.

    A bar stamped T covers [T, T+BAR_SECONDS). Proving it closed needs
    two unambiguous instants, so both sides are normalised to absolute
    (tz-aware) time before subtracting:

      bar    yfinance returns tz-AWARE stamps for NSE symbols. A NAIVE
             index carries no offset, so its instant is unknowable and
             completion cannot be proven.
      obs    `_recv_at` is naive local time. `.astimezone()` interprets a
             naive datetime as local and yields an aware one, so the
             comparison is in absolute time and no longer depends on the
             runner's TZ matching the exchange's. Under the previous
             wall-clock subtraction a UTC runner against IST bars gave
             age = -19800s, which silently read as "closed".
    """
    try:
        bar_ts = df.index[-1]
        # NaT (like NaN) is not equal to itself - detects it without
        # importing pandas here.
        if bar_ts is None or bar_ts != bar_ts:       # NaT / missing
            return False
        bar_ts = bar_ts.to_pydatetime()
        if bar_ts.tzinfo is None:                    # ambiguous offset
            return False
        obs = (datetime.fromisoformat(str(observed_at))
               if observed_at else datetime.now())
        obs = obs.astimezone()                       # naive -> local-aware
        age = (obs - bar_ts).total_seconds()
    except Exception:
        return False                                 # unreadable -> unproven
    if age < 0:                                      # clock skew / future bar
        return False
    return age >= BAR_SECONDS


def signal_bars(df, observed_at=None):
    """The bars a signal may legitimately be built on.

    A bar stamped T covers [T, T+BAR_SECONDS). Observed before that
    window closes it is still FORMING and its OHLC can still change, so
    a signal built on it can flip on data that had not happened yet.
    During market hours yfinance returns exactly that bar as the last row
    and get_direction() reads .iloc[-1].

    COMPLETION-POSITIVE RULE: the final bar is retained ONLY when it is
    PROVEN closed. Every form of doubt - unreadable timestamp, NaT, naive
    index, negative age, clock skew, timezone ambiguity - drops it.

    The earlier revision inverted this: it fell back to the untouched
    frame whenever completion could not be established, so four separate
    degraded-input paths silently reinstated the very defect this exists
    to remove, with no error and no telemetry. Dropping instead costs at
    most one bar of staleness on data that is definitely complete.

    Pure selection on an ALREADY-FETCHED frame: no request, no cadence
    change, no I/O.

    Empty frame -> returned unchanged (nothing to drop; get_direction and
    get_trend_quality return None/0.5 on empty, get_momentum returns 0.0,
    so no caller can be surprised).
    """
    if df is None or len(df) == 0:
        return df
    if _final_bar_proven_closed(df, observed_at):
        return df                       # proven complete -> keep it
    return df.iloc[:-1]                 # unproven or forming -> drop it


def _signal_bar_meta(df, sig_df, observed_at, today_str):
    """Bar identity for the freshness gate. Never raises; unknown -> None.

    Pure inspection of an ALREADY-FETCHED frame: no request, no cadence
    change, no I/O. A field that cannot be read stays None, and None is a
    REJECTION at the gate - never a pass.
    """
    meta = {"bar_ts": None, "used_bar_ts": None,
            "observed_at": str(observed_at) if observed_at else None,
            "bar_age_s": None, "session_status": "UNKNOWN"}
    try:
        if df is not None and len(df):
            meta["bar_ts"] = str(df.index[-1])
    except Exception:
        meta["bar_ts"] = None
    try:
        if sig_df is not None and len(sig_df):
            meta["used_bar_ts"] = str(sig_df.index[-1])
    except Exception:
        meta["used_bar_ts"] = None
    meta["bar_age_s"] = stab.bar_age_s(meta["bar_ts"], meta["observed_at"])
    # Same rule as telemetry.emit_signal, so the gate verdict and the
    # recorded session_status can never disagree.
    if meta["bar_ts"]:
        meta["session_status"] = ("VALID"
                                  if str(meta["bar_ts"])[:10] == str(today_str)
                                  else "STALE_OR_AMBIGUOUS")
    return meta


def get_trend_quality(df):
    """Fraction of the last 12 bars whose EMA9/21 relationship matches the
    current one. NOTE (evidence): the 60d study found FRESH trends
    (tq < 1.0) outperformed fully-established ones intraday, so the
    ranking engine scores freshness higher - see ranking_engine.py."""
    if df is None or df.empty or len(df) < 21:
        return 0.5
    # S-02: same estimator as get_direction, for internal consistency.
    ema9 = df["Close"].ewm(span=9, adjust=False).mean()
    ema21 = df["Close"].ewm(span=21, adjust=False).mean()
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
                fair, tag="", score=None, rank_pos=None, tier=None,
                candidate_id=None):
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

    # observability: entry decision with full contract identity.
    # CHANGE 9: `candidate_id` closes the chain
    #   cycle -> candidate -> gates -> selection -> decision -> trade.
    # It was NULL on all 34 historical ENTRY rows.
    telemetry.emit_decision(
        "ENTRY", reason=underlying_dir, candidate_id=candidate_id,
        token=rec.get("token"), trading_symbol=rec.get("symbol"),
        entry_price=entry_px, qty=qty)

    msg = (f"📊 OPTIONS: BOUGHT {lots} lot(s) ({qty}) {rec['symbol']} "
           f"@ Rs.{entry_px:.2f}{tag} | cost Rs.{cost:,.0f} | "
           f"stop Rs.{initial_stop:.2f}")
    log.append(msg)
    telegram(msg)
    return True


DAY_GATES_KEY = "gate_ledger_day"
DAY_HEARTBEAT_KEY = "cycle_heartbeat_count"


def _record_day_gates(conn, today_str, ledger):
    """Accumulate the session's gate totals and heartbeat count.

    Kept in `meta` (which already rides the per-cycle state push) so the
    15:35 summary survives the ephemeral runner without a second store.
    """
    try:
        raw = meta_get(conn, f"{DAY_GATES_KEY}:{today_str}")
        acc = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        acc = {}
    for k, v in ledger.as_dict().items():
        acc[k] = int(acc.get(k, 0)) + int(v)
    acc["cycles"] = int(acc.get("cycles", 0)) + 1
    meta_set(conn, f"{DAY_GATES_KEY}:{today_str}", json.dumps(acc))
    return acc


def _eod_gate_summary(conn, today_str, now_min, log):
    """CHANGE 11: ONE Telegram message at 15:35 with the day's gate ledger
    and heartbeat count - sent whether or not any trade occurred.

    Section 22: a zero-trade session must be POSITIVELY CONFIRMED. Silence
    is indistinguishable from a crash, an auth failure, a failed state
    restore, a scheduler that never fired, or a gate rejecting everything
    because the GATE is broken.
    """
    if now_min < T_GATE_SUMMARY:
        return
    flag = f"gate_summary_sent:{today_str}"
    if meta_get(conn, flag):
        return
    try:
        raw = meta_get(conn, f"{DAY_GATES_KEY}:{today_str}")
        acc = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        acc = {}
    gen = int(acc.get("candidates_generated", 0))
    rej = sum(int(acc.get(b, 0)) for b in stab.REJECT_BUCKETS)
    passed = int(acc.get("passed_to_selection", 0))
    entered = int(acc.get("entered", 0))
    ok = (gen == rej + passed) and entered <= passed
    lines = [f"🧾 OPTIONS GATE SUMMARY | {today_str}",
             f"cycles (heartbeats): {int(acc.get('cycles', 0))}",
             f"candidates generated: {gen}"]
    for b in stab.REJECT_BUCKETS:
        n = int(acc.get(b, 0))
        if n:
            lines.append(f"  {b.replace('rejected_', 'rejected ')}: {n}")
    lines += [f"passed to selection: {passed}", f"entered: {entered}",
              f"identities hold: {'YES' if ok else 'NO'}",
              f"gates: {'ON' if STAB_ON else 'OFF'} "
              f"(max_pos={MAX_OPTION_POSITIONS}, min_dte={STAB['min_dte']}, "
              f"max_spread={STAB['max_entry_spread_pct']}%, "
              f"max_bar_age={STAB['max_signal_bar_age_s']}s)"]
    msg = "\n".join(lines)
    log.append(msg)
    telegram(msg)
    meta_set(conn, flag, "1")


def _watch_minutes(w, now):
    """Minutes since a watched contract was closed, or None if unreadable."""
    try:
        t = datetime.fromisoformat(str(w.get("exited_at")))
        n = now.replace(tzinfo=None) if now.tzinfo else now
        if t.tzinfo:
            t = t.replace(tzinfo=None)
        m = (n - t).total_seconds() / 60.0
        return m if m >= 0 else None
    except Exception:
        return None


def post_exit_watchlist(conn):
    """Contracts whose price path is still being followed after an exit."""
    try:
        return json.loads(meta_get(conn, POST_EXIT_KEY) or "[]")
    except (ValueError, TypeError):
        return []


def post_exit_watch(conn, entry):
    """CHANGE 3: start following a contract after it is closed.

    PASSIVE. The token joins the quote batch that already runs every
    cycle, so no additional market-data request is made and no cadence
    changes. Persisted in `meta` because the runner is ephemeral.
    """
    lst = [w for w in post_exit_watchlist(conn)
           if str(w.get("token")) != str(entry.get("token"))]
    lst.append(entry)
    meta_set(conn, POST_EXIT_KEY, json.dumps(lst[-32:]))


def close_option(conn, pos, exit_px, reason, log, quote=None, price_source=None,
                 peak_source=None, spot=None, underlying_dir=None):
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

    # observability: exit decision + the high-water mark that produced it.
    # Captured here because the options_positions row is deleted above.
    #
    # CHANGE 15 - TREND REVERSAL: INSTRUMENT, DO NOT CHANGE.
    # 12 trend-reversal exits, 0 winners, -Rs.23,147 on the full book.
    # Concerning, but not proof of causality - so the exit rule is
    # UNTOUCHED and the missing evidence is recorded instead: how far the
    # premium still was from its INITIAL stop when the reversal fired, and
    # what the underlying was doing at that instant. Combined with the
    # post-exit path below, that makes the decision answerable next week
    # without changing anything today.
    _initial_stop = (pos["entry_price"] * (1 + INITIAL_STOP_PCT)
                     if pos.get("entry_price") else None)
    telemetry.emit_exit(
        pos.get("token"), pos.get("trading_symbol"), reason,
        exit_price=exit_px, high_water_at_exit=pos.get("high_water"),
        peak_source=peak_source, pnl=pnl,
        entry_price=pos.get("entry_price"), initial_stop_level=_initial_stop,
        underlying_spot=spot, underlying_direction=underlying_dir)

    # CHANGE 10 - REAL TRANSACTION COST ACCOUNTING (REPORTING ONLY).
    # The options book has always reported Costs = Rs.0. Gross P&L above is
    # unchanged and no historical row is rewritten; the modeled cost and
    # the net figure are recorded ALONGSIDE it. Nothing in execution,
    # stops, trail arming, ranking or selection reads this.
    try:
        _costs = stab.round_trip_cost(pos["entry_price"], exit_px, qty)
        _fric = stab.spread_friction(pos.get("entry_bid"), pos.get("entry_ask"),
                                     (quote or {}).get("bid"),
                                     (quote or {}).get("ask"), qty)
        telemetry.emit_trade_cost(
            pos.get("token"), pos.get("trading_symbol"), qty,
            pos["entry_price"], exit_px, pnl, _costs, friction=_fric,
            rate_source=stab.COST_RATE_SOURCE)
    except Exception as e:                       # accounting must never trade
        print(f"  cost accounting skipped ({type(e).__name__}: {e})")

    # CHANGE 3 - POST-EXIT PATH. The table had ZERO rows, so an
    # initial-stop exit could never be classified as "correct", "too
    # tight", "option noise" or "wrong direction". Registering the
    # contract here starts a 60-minute passive observation.
    try:
        post_exit_watch(conn, {
            "token": str(pos.get("token")), "symbol": pos.get("trading_symbol"),
            "exited_at": datetime.now().isoformat(timespec="seconds"),
            "exit_price": exit_px, "entry_price": pos.get("entry_price"),
            "reason": reason, "underlying": pos.get("symbol")})
    except Exception as e:
        print(f"  post-exit watch skipped ({type(e).__name__}: {e})")

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
    # Naive wall clock, matching how telemetry normalises bar stamps, so
    # the freshness gate measures the same age the evidence records.
    now_iso = now.replace(tzinfo=None).isoformat(timespec="microseconds")

    # --- observability: open a correlation cycle (additive, fail-open) ---
    telemetry.new_cycle(
        trading_date=today_str,
        experiment_flags=("no_same_cycle_reentry="
                          f"{EXPERIMENT_NO_SAME_CYCLE_REENTRY}"
                          f" stabilization={STAB_ON}"))

    # CHANGE 11: every generated candidate gets exactly one terminal
    # disposition, so a zero-trade cycle is POSITIVELY explained instead of
    # being inferred from an empty candidate list. Created here, before the
    # first early return, so EVERY cycle emits a ledger and a heartbeat -
    # including the degraded ones. A missing heartbeat is then a genuine
    # failure signal rather than an artefact of which branch we took.
    ledger = stab.GateLedger(MAX_OPTION_POSITIONS if STAB_ON else None)
    hb = {"trading_date": today_str, "state_restored": None, "auth_ok": None,
          "master_ok": None, "signals_ok": None, "quotes_fetched": None,
          "gates_evaluated": False, "entry_window": None,
          "open_positions": None}

    def finish(note=None):
        """Close the cycle: heartbeat + gate ledger + EOD summary + commit.

        Called on EVERY exit path from process(), degraded ones included.
        Fail-open throughout - observability may never prevent a commit.
        """
        try:
            ok = True
            try:
                ledger.check()
            except stab.LedgerIdentityError as e:
                ok = False
                print(f"  GATE LEDGER IDENTITY VIOLATED: {e}")
                trace(event="gate_ledger_identity_violated", detail=str(e))
            hb["candidates_generated"] = ledger.candidates_generated
            if hb.get("open_positions") is None:
                hb["open_positions"] = conn.execute(
                    "SELECT COUNT(*) FROM options_positions").fetchone()[0]
            telemetry.emit_heartbeat(note=note, **hb)
            telemetry.emit_gate_ledger(ledger, trading_date=today_str,
                                       identities_ok=ok,
                                       detail=ledger.summary())
            print(f"  GATE LEDGER: {ledger.summary()} identities_ok={ok}")
            _record_day_gates(conn, today_str, ledger)
            _eod_gate_summary(conn, today_str, now_min, log)
        except Exception as e:
            print(f"  cycle close-out observability skipped "
                  f"({type(e).__name__}: {e})")
        conn.commit()

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
        hb["auth_ok"] = False
        finish("angel_one_unavailable")
        return
    # S-05 RISK-MANAGEMENT ISOLATION.
    # The instrument master is an ENTRY-side dependency: open positions
    # already carry their own token and need nothing from it. Previously
    # its failure returned here, skipping the position-management loop
    # entirely - so a data outage could leave a position past its stop
    # with live quotes available and unused. It no longer gates exits;
    # it only disables NEW candidate construction below.
    # S-34b: skip a network attempt when one failed recently. The loader
    # honours this ONLY after proving a usable <=72h fallback exists, so
    # it cannot suppress a refresh the system actually needs.
    _refresh_ok = master_refresh_allowed(conn)
    master = angel.load_instrument_master(UNIVERSE_NAMES,
                                          refresh_allowed=_refresh_ok)
    record_master_refresh(conn, angel.master_health())
    hb["auth_ok"] = True
    hb["state_restored"] = True   # db_init() opened a restored book above
    hb["master_ok"] = bool(master)
    if not master:
        print("  Instrument master unavailable -> no NEW option trades "
              "(existing positions ARE still risk-managed this cycle).")

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
            _req_at = datetime.now().isoformat(timespec="microseconds")
            try:
                df = yf.download(ticker, period="5d", interval=INTERVAL,
                                 auto_adjust=True, progress=False,
                                 multi_level_index=False)
            except Exception as e:
                print(f"  {name}: underlying download failed ({e})")
                continue
            if df is None or df.empty:
                continue
            _recv_at = datetime.now().isoformat(timespec="microseconds")
            # S-09: yfinance returns the CURRENTLY FORMING bar as the last row
            # during market hours, and get_direction() reads .iloc[-1]. So
            # 83.6% of production signals (3,308 of 3,959 recorded) were
            # built on an incomplete bar whose OHLC can still change.
            #
            # `signal_bars` retains the final row ONLY when it is PROVEN
            # closed; any doubt drops it. Dropping unconditionally instead
            # would discard valid completed bars and add a bar of staleness -
            # that variant altered 80 further signals needlessly.
            #
            # This uses the SAME dataframe already fetched above. No new
            # market-data request, no new cadence, no change to batching,
            # ordering, retries or rate limits. It changes bar SELECTION and
            # nothing else.
            _sig_df = signal_bars(df, _recv_at)
            data[name] = {"spot": float(df["Close"].iloc[-1]),
                          "direction": get_direction(_sig_df),
                          "momentum": get_momentum(_sig_df, today),
                          "trend_quality": get_trend_quality(_sig_df)}
            # CHANGE 1: carry the bar identity forward so the freshness
            # gate can be evaluated WITHOUT refetching anything.
            #
            #   bar_ts        latest observed bar - the same quantity
            #                 telemetry.bar_age_s and session_status are
            #                 already computed from, so gate and evidence
            #                 cannot disagree.
            #   used_bar_ts   the bar the DIRECTION was actually built on
            #                 (last proven-closed bar). On 2026-08-24 the
            #                 09:15:55 observation carried Friday 15:25 in
            #                 BOTH fields, direction BULL, session_status
            #                 STALE_OR_AMBIGUOUS - and nothing in the
            #                 engine read any of it.
            #
            # These are strings, so they survive the JSON signal cache.
            data[name].update(_signal_bar_meta(df, _sig_df, _recv_at,
                                               today_str))
            # observability: bar identity/status + a COMPLETED-BAR direction
            # recorded for later comparison. Never read back, never used.
            telemetry.emit_signal(
                name, _req_at, _recv_at, df=df,
                direction=data[name]["direction"],
                momentum=data[name]["momentum"],
                trend_quality=data[name]["trend_quality"],
                spot=data[name]["spot"],
                direction_fn=get_direction, session_date=today_str)
        if data:
            print(f"  Signals: refreshed {len(data)} symbols from yfinance")
    # S-05 RISK-MANAGEMENT ISOLATION.
    # Underlying signals are an ENTRY-side dependency. Open positions need
    # them only for the trend-reversal check, which correctly degrades to
    # "no reversal signal" when direction is None. Stops, trailing stops,
    # expiry and square-off need only the option quote. Previously a
    # yfinance failure returned here and skipped position management
    # entirely - the single highest-severity coupling the audit found.
    # It now disables NEW candidate construction and nothing else.
    signals_available = "NIFTY" in data
    hb["signals_ok"] = bool(signals_available)
    if not signals_available:
        print("  No underlying data -> no NEW option trades this cycle "
              "(existing positions ARE still risk-managed).")

    # relative strength: today's move vs NIFTY's (evidence: the strongest
    # measured ranking feature - see ranking_engine.py header)
    if signals_available:
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
    #
    # S-04/S-06: the supervisor is consulted BEFORE any candidate work and
    # can only ever REMOVE entry permission. It is asked here, above the
    # strategy, and the strategy has no path that bypasses it. A halt
    # blocks NEW ENTRIES only - the position-management loop below is
    # deliberately outside this gate.
    #
    # S-05: master/signals are ENTRY-side requirements. Their absence
    # suppresses candidates; it never reaches position management.
    entry_allowed, safety_state = safety_supervisor.entry_permission(
        conn, safety_supervisor.OPTIONS_BOOK, today,
        CONTRACT.capital, CONTRACT.max_daily_loss_percent)
    if not entry_allowed:
        print(f"  SAFETY SUPERVISOR: new entries BLOCKED "
              f"({safety_state['reason']}) | realized today "
              f"Rs.{safety_state['realized_today']} | session low "
              f"Rs.{safety_state['realized_low_water']} | limit "
              f"Rs.{safety_state['limit']}. Existing positions REMAIN "
              f"risk-managed.")
    telemetry.emit_decision(
        "ENTRY_PERMISSION", reason=safety_state["reason"],
        trading_symbol=safety_state["book"])

    in_window = (T_ENTRY_START <= now_min <= T_ENTRY_END
                 and signals_fresh and signals_available and bool(master)
                 and entry_allowed)
    hb["entry_window"] = bool(in_window)
    hb["gates_evaluated"] = True

    candidates = []   # dicts: name/rec/direction/tier/tag + score features
    # Symbols dropped by a gate. Section 3.2: a rejection must DROP the
    # candidate, never substitute. The confluence pass and the momentum
    # fallback pass both consult this, so a symbol rejected by a gate in
    # the first pass cannot be re-admitted with a different direction in
    # the second - which would be "selecting a different option on the
    # same underlying" by side effect.
    gate_rejected = set()
    if in_window:
        def consider(name, info, direction, tier, tag=""):
            """Build at most ONE candidate for `name`, or drop it.

            Section 3.2 - NO SUBSTITUTION. `nearest_expiry()` and
            `find_option()` are each called exactly once. There is no
            retry loop, no expiry rollover, no strike walk, and no
            relaxation of any threshold. Every failure path below ends in
            `return` with nothing appended.
            """
            ledger.generated()

            # CHANGE 1 - signal / session validity, BEFORE any contract
            # work. Cheapest gate first, and it drops the symbol for both
            # passes rather than just this one.
            if STAB_ON:
                r = stab.check_signal_freshness(info, today, STAB, now=now_iso)
                if not r:
                    ledger.reject(r.reason)
                    gate_rejected.add(name)
                    trace(event="entry_skipped", symbol=name, reason=r.reason,
                          bar_ts=info.get("bar_ts"),
                          used_bar_ts=info.get("used_bar_ts"),
                          bar_age_s=info.get("bar_age_s"),
                          session_status=info.get("session_status"),
                          threshold_s=STAB["max_signal_bar_age_s"],
                          trading_date=today_str, gate="signal_freshness")
                    telemetry.emit_candidate(
                        name, direction, {}, today=today,
                        gate_result="REJECTED", gate_reason=r.reason,
                        selection_policy=stab.POLICY_NAME,
                        signal_bar_ts=info.get("bar_ts"),
                        signal_bar_age_s=info.get("bar_age_s"))
                    return

            exp = angel.nearest_expiry(master, name, today, min_dte=1)
            if exp is None:
                ledger.reject("no_listed_expiry")
                gate_rejected.add(name)
                trace(event="entry_skipped", symbol=name, reason="no_listed_expiry")
                return

            # CHANGE 2 - temporary DTE floor. A rejection DROPS the
            # candidate: `nearest_expiry` is NOT called again with a later
            # date, so no expiry rollover can occur.
            if STAB_ON:
                r = stab.check_dte(exp, today, STAB)
                if not r:
                    ledger.reject(r.reason)
                    gate_rejected.add(name)
                    trace(event="entry_skipped", symbol=name, reason=r.reason,
                          expiry=exp.isoformat(), min_dte=STAB["min_dte"],
                          policy="temporary_stabilization_policy", gate="dte")
                    telemetry.emit_candidate(
                        name, direction, {"expiry": exp}, today=today,
                        gate_result="REJECTED", gate_reason=r.reason,
                        selection_policy=stab.POLICY_NAME)
                    return

            opt_type = "CE" if direction == "BULL" else "PE"
            rec = angel.find_option(master, name, exp, info["spot"], opt_type)
            if rec is None:
                ledger.reject("no_listed_strike")
                gate_rejected.add(name)
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
                # `gate_rejected` is what stops a gate rejection from
                # turning into a substitution - see section 3.2.
                if (name in traded_today or name in picked
                        or name in gate_rejected):
                    continue
                d = "BULL" if info["momentum"] >= 0 else "BEAR"
                consider(name, info, d, "momentum",
                         tag=f" [momentum {info['momentum']:+.1f}%]")

    # Observability: record the ENTRY GATE and every input that produced it.
    #
    # Without this a cycle with zero candidate_snapshot rows is ambiguous:
    # either the gate was CLOSED (no evaluation happened, so production
    # could not have entered anything) or it was OPEN and the candidate set
    # was genuinely EMPTY. Those demand opposite treatment in a portfolio
    # replay - the first means "look forward to the next open cycle", the
    # second means "no entry was possible here" - and they were
    # indistinguishable. 17 such cycles on 2026-08-18, 58 on 2026-08-19.
    #
    # Every value below was ALREADY computed above for the trading
    # decision; nothing is evaluated, fetched or derived for telemetry.
    # The candidate cadence is untouched: this only records what the gate
    # already decided. Fail-open, return value unused.
    telemetry.emit_decision(
        "ENTRY_GATE",
        reason=(f"in_window={int(in_window)} "
                f"time={int(T_ENTRY_START <= now_min <= T_ENTRY_END)} "
                f"fresh={int(signals_fresh)} "
                f"signals={int(signals_available)} "
                f"master={int(bool(master))} "
                f"allowed={int(entry_allowed)} "
                f"candidates={len(candidates)}"),
        trading_symbol=safety_supervisor.OPTIONS_BOOK)

    # ---- ONE batched live-quote call for positions + candidates --------
    tokens = [p["token"] for p in positions if p.get("token")]
    tokens += [c["rec"]["token"] for c in candidates]
    # CHANGE 3: contracts still inside their 60-minute post-exit window
    # ride the SAME batch. Purely passive - no extra request, no cadence
    # change, and nothing here can affect a trading decision.
    _watch = [w for w in post_exit_watchlist(conn)
              if _watch_minutes(w, now) is not None
              and _watch_minutes(w, now) <= POST_EXIT_WINDOW_MIN]
    tokens += [str(w["token"]) for w in _watch if w.get("token")]
    _q_req_at = datetime.now().isoformat(timespec="microseconds")
    quotes = angel.get_quotes(smart, list(dict.fromkeys(tokens)))
    _q_recv_at = datetime.now().isoformat(timespec="microseconds")
    print(f"  Live quotes: {len(quotes)}/{len(set(tokens))} tokens fetched")
    hb["quotes_fetched"] = len(quotes)
    # observability: one snapshot per token, keyed for correlation
    _qsnap = telemetry.emit_quotes(quotes, _q_req_at, _q_recv_at) or {}

    # CHANGE 3: record one post-exit observation per watched contract.
    # Maximum recovery, minimum adverse move, time-to-recovery and
    # "recovered above entry" are exact aggregates over these rows - no
    # separate telemetry framework is needed to answer them.
    for w in _watch:
        wq = quotes.get(str(w.get("token")))
        if wq is None:
            continue
        telemetry.emit_post_exit(
            w.get("token"), w.get("symbol"), w.get("exited_at"), wq,
            quote_snapshot_id=_qsnap.get(str(w.get("token"))),
            minutes_since_exit=_watch_minutes(w, now),
            exit_price=w.get("exit_price"), entry_price=w.get("entry_price"),
            underlying_spot=(data.get(w.get("underlying")) or {}).get("spot"),
            exit_reason=w.get("reason"))
    meta_set(conn, POST_EXIT_KEY, json.dumps(_watch))

    # ---- ranking engine: SCORING ---------------------------------------
    # In "shadow" (default) this ONLY logs and stamps scores - the
    # baseline keeps deciding. In "active" the ranked selection decides.
    # Scores depend only on the candidate and its quote, so they are
    # computed here, next to the quote fetch. SELECTION is deliberately
    # deferred until after position management (see S-25 below).
    #
    # CHANGE 12 / U-001: ranking is RESEARCH. An exception raised inside
    # it must not terminate the trader, because the position-management
    # loop - every stop, every trailing stop, every square-off - runs
    # BELOW this point. Containment here keeps the exit path reachable
    # when ranking is broken. The calculation itself is untouched.
    rcfg, rank_mode = {}, "off"
    ranked, picks, rejections = [], [], []
    try:
        rcfg = ranking.get_config(CFG)
        rank_mode = rcfg.get("mode", "shadow")
        if candidates and rank_mode != "off":
            for c in candidates:
                c["quote"] = quotes.get(c["rec"]["token"])
            history = ranking.load_history(conn, rcfg["min_history_n"])
            ranked = ranking.rank(candidates, data["NIFTY"]["direction"],
                                  rcfg, history)
    except Exception as e:
        ranked, picks, rejections = [], [], []
        print(f"  RANKING FAILED ({type(e).__name__}: {e}) - scores "
              f"unavailable this cycle. Exits, stops and square-off are "
              f"UNAFFECTED; entries fall back to the explicit production "
              f"selection policy.")
        trace(event="ranking_exception", error=type(e).__name__,
              contained="yes", exits_affected="no")

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
        # CHANGE 4 - U-014 TRAILING DEAD ZONE.
        #
        # The trail band (12%) is WIDER than the arm threshold (10%), so an
        # armed trail computed 1.10 * 0.88 = 0.968 of entry - a stop 3.2%
        # BELOW the entry price. 17 of 53 live-price trailing exits closed
        # inside that band for -Rs.6,358, and the worst (-3.168%) landed
        # within 0.001 of the arithmetic floor. That is an arithmetic
        # defect, not a strategy preference.
        #
        # The fix is a FLOOR at entry + round-trip cost. The arm threshold
        # and the band are UNCHANGED, so every trail that already sat above
        # entry produces the identical level; only the mathematically
        # impossible region is removed. `trail_stop_level` returns None
        # when the trail is not armed, and the initial stop then stands
        # exactly as before.
        _trail = (stab.trail_stop_level(pos["entry_price"], high_water,
                                        TRAIL_ACTIVATE_PCT, TRAIL_PCT, STAB)
                  if STAB_ON else
                  (high_water * (1 - TRAIL_PCT)
                   if high_water >= pos["entry_price"] * (1 + TRAIL_ACTIVATE_PCT)
                   else None))
        if _trail is not None:
            stop_price = max(stop_price, _trail)

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

        # observability: per-cycle position state, incl. the high-water mark
        # that is otherwise lost when the position row is DELETEd on close.
        telemetry.emit_position(
            token, pos.get("trading_symbol"), mark=mark,
            high_water=high_water, stop_price=stop_price,
            day_high_seen=day_high, day_low_seen=day_low,
            peak_source=peak_source, trigger_value=trigger,
            quote_snapshot_id=_qsnap.get(str(token)))

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
                         peak_source=peak_source,
                         spot=(info or {}).get("spot"),
                         underlying_dir=direction)
        elif reversed_:
            close_option(conn, pos, mark, "Trend reversal exit", log, q,
                         peak_source=peak_source,
                         spot=(info or {}).get("spot"),
                         underlying_dir=direction)
        elif expiry < today:
            close_option(conn, pos, mark, "Expired contract", log, q,
                         peak_source=peak_source,
                         spot=(info or {}).get("spot"),
                         underlying_dir=direction)
        elif now_min >= T_SQUARE_OFF:
            close_option(conn, pos, mark, "Square-off 15:15", log, q,
                         peak_source=peak_source,
                         spot=(info or {}).get("spot"),
                         underlying_dir=direction)

    # ---- ranking engine: SELECTION (S-25) ------------------------------
    # Selection runs HERE, after the position-management loop, because
    # both of its capacity inputs are only correct post-exit:
    #
    #   slots        MAX_POSITIONS - open positions
    #   open_sectors per-sector counts feeding max_per_sector
    #
    # Both were previously derived from `positions`, the list loaded
    # BEFORE any exit ran. A cycle that closed two positions still logged
    # slots as if those positions were open, so candidates the baseline
    # then went on to enter were recorded `would_trade=0` with reason
    # "tier budget reached (0)". The live entry path re-reads the count
    # from the database at this point (see below); the shadow path did
    # not, so shadow and live disagreed on capacity by construction and
    # the shadow evidence understated what ranking would have done.
    #
    # Reading both from the database - the same source, at the same point
    # in the cycle, as the live path - removes the divergence. Scores and
    # rank order are computed above and are unaffected; only the
    # capacity-dependent `would_trade` flag changes.
    #
    # CHANGE 12 / U-001: contained for the same reason as scoring above -
    # the selection call is RESEARCH in shadow mode, and an exception here
    # must not terminate the trader or bypass any risk control.
    pick_names = set()
    if ranked:
        try:
            open_rows = conn.execute(
                "SELECT symbol FROM options_positions").fetchall()
            open_sectors = {}
            for (sym,) in open_rows:
                sec = ranking.SECTOR.get(sym, "Other")
                open_sectors[sec] = open_sectors.get(sec, 0) + 1
            # Shadow capacity must match LIVE capacity or the shadow
            # evidence measures a book the production path cannot open.
            slots = max(0, MAX_OPTION_POSITIONS - len(open_rows))
            picks, rejections = ranking.select(ranked, rcfg, open_sectors,
                                               slots)
            ranking.log_cycle(conn, rank_mode, ranked, picks)
            pick_names = {c["name"] for c in picks}
            for c in ranked:
                trace(event="rank", symbol=c["name"], rank=c["rank"],
                      score=c["score"], tier=c["tier"],
                      would_trade=1 if c["name"] in pick_names else 0)
            if rank_mode == "active":
                for c, why in rejections:
                    trace(event="rank_rejected", symbol=c["name"],
                          score=c["score"], reason=why)
        except Exception as e:
            picks, rejections, pick_names = [], [], set()
            print(f"  RANKING SELECTION FAILED ({type(e).__name__}: {e}) - "
                  f"contained. Exits and risk controls UNAFFECTED.")
            trace(event="ranking_exception", stage="select",
                  error=type(e).__name__, contained="yes", exits_affected="no")

    # ---- CHANGE 9 / S-41: candidate -> trade traceability --------------
    # candidate_snapshot was written ONLY for candidates that reached
    # ranking, and `decision.candidate_id` was NULL on all 34 production
    # ENTRY rows - candidate linkage was effectively 0%. Every surviving
    # candidate now gets a row here, before any entry can be attempted, and
    # its id is carried into the ENTRY decision. Gate-rejected candidates
    # were already recorded at the point of rejection.
    _score_by_name = {c["name"]: c for c in ranked}
    cand_ids = {}
    for c in candidates:
        sc = _score_by_name.get(c["name"], {})
        cand_ids[c["name"]] = telemetry.emit_candidate(
            c["name"], c["direction"], c["rec"],
            quote_snapshot_id=_qsnap.get(str(c["rec"]["token"])),
            score=sc.get("score"), rank=sc.get("rank"),
            would_trade=1 if c["name"] in pick_names else 0,
            tier=c.get("tier"), today=today,
            gate_result="ELIGIBLE", selection_policy=stab.POLICY_NAME,
            spread_pct=stab.spread_pct(quotes.get(c["rec"]["token"])),
            signal_bar_ts=(data.get(c["name"]) or {}).get("bar_ts"),
            signal_bar_age_s=(data.get(c["name"]) or {}).get("bar_age_s"))

    if not in_window:
        finish("entry_window_closed")
        return

    # ---- EXPERIMENT: do not refill a slot from a PRE-EXIT candidate list -
    # `candidates` was built above, before the position-management loop.
    # If that loop closed anything, the list predates the exit, so acting
    # on it now would re-enter on information gathered before the market
    # told us to leave. End the entry evaluation instead; the next
    # entry-eligible cycle rebuilds candidates from fresh signals.
    #
    # Exits, sizing, ranking, MAX_POSITIONS and the first BUY of the day
    # are all untouched - the book is empty at 09:15, so the opening cycle
    # closes nothing and this branch cannot fire there.
    if EXPERIMENT_NO_SAME_CYCLE_REENTRY:
        still_open = conn.execute(
            "SELECT COUNT(*) FROM options_positions").fetchone()[0]
        closed_this_cycle = len(positions) - still_open
        if closed_this_cycle > 0:
            print(f"  EXPERIMENT no_same_cycle_reentry: {closed_this_cycle} "
                  f"position(s) closed this cycle - entry evaluation ended, "
                  f"candidate list ({len(candidates)}) NOT reused. Next "
                  f"entry-eligible cycle will rebuild from fresh signals.")
            trace(event="entry_evaluation_ended",
                  reason="same_cycle_exit_invalidates_candidates",
                  closed_this_cycle=closed_this_cycle,
                  candidates_discarded=len(candidates))
            for _c in candidates:
                ledger.reject("other_same_cycle_reentry_guard")
            finish("same_cycle_reentry_guard")
            return

    # ---- entries, on LIVE quotes only ----------------------------------
    # active mode: trade the ranked picks. shadow/off: baseline order,
    # with the candidate's score stamped on the trade for attribution.
    open_count = conn.execute(
        "SELECT COUNT(*) FROM options_positions").fetchone()[0]

    # ---- S-44 / CHANGE 9: EXPLICIT PRODUCTION SELECTION STAGE ----------
    #
    # Production selection used to be the unnamed `else` branch of the
    # ranking-mode test, so "which candidates may consume the slots" was
    # decided by the ORDER OF `symbols` IN intraday_config.json and was
    # never named, logged, or testable. That is S-44.
    #
    # The policy is now explicit, named (`universe_order_v1`), logged on
    # every candidate row, and unit-tested - and it is DELIBERATELY THE
    # SAME POLICY. The historical counterfactual measured ranking top-N at
    # 26.9% win / -5.23% mean (n=78) against universe order at 46.8% /
    # +0.13% (n=111), so swapping it in today would be an unvalidated
    # strategy change wearing a bug fix's clothes. Ranking stays SHADOW.
    if rank_mode == "active" and ranked:
        entry_list = picks
        _not_picked = [c for c in candidates if c["name"] not in pick_names]
        for _c in _not_picked:
            ledger.reject("not_selected_ranking_active")
    else:
        entry_list = candidates

    slots = max(0, MAX_OPTION_POSITIONS - open_count)
    selected, dropped = stab.select_for_entry(entry_list, slots)
    for _c, _why in dropped:
        ledger.reject(_why)
        trace(event="entry_rejected", symbol=_c["rec"]["symbol"],
              reason=_why, slots=slots, open_positions=open_count,
              policy=stab.POLICY_NAME)
    trace(event="selection", policy=stab.POLICY_NAME, mode=rank_mode,
          eligible=len(entry_list), slots=slots, selected=len(selected))

    score_by_name = {c["name"]: c for c in ranked}

    iv_cache = {}
    for cand in selected:
        name, rec = cand["name"], cand["rec"]
        q = quotes.get(rec["token"])

        # ---- CHANGE 7: FINAL EXECUTION-TIME SAFETY AUTHORIZATION -------
        #
        # ENTRY INTENT ONLY. `stab.authorize` returns PASS for any other
        # intent before reading a single input, and no exit path in this
        # file calls it at all - the position-management loop above ran to
        # completion before this point and reads only `positions` and
        # `quotes`, and square_off_net() is outside even that. An entry
        # gate therefore cannot block, delay or reject an exit.
        #
        # Every value is re-read AT THIS INSTANT rather than trusted from
        # candidate-selection time: `open_positions` comes from the
        # database (so exits earlier in this cycle are reflected), the
        # clock is re-checked, the supervisor verdict, the duplicate set,
        # the signal, the DTE and the live quote are all re-evaluated.
        _open_now = conn.execute(
            "SELECT COUNT(*) FROM options_positions").fetchone()[0]
        auth = stab.authorize(stab.ENTRY, {
            "symbol": name,
            "signal": data.get(name),
            "trading_date": today,
            "now": now_iso,
            "expiry": rec["expiry"],
            "quote": q,
            "open_positions": _open_now,
            "traded_today": traded_today,
            "entry_allowed": entry_allowed,
            "in_entry_window": T_ENTRY_START <= now_min <= T_ENTRY_END,
        }, CFG)
        if not auth:
            ledger.reject(auth.reason)
            trace(event="entry_rejected", symbol=rec["symbol"],
                  token=rec["token"], reason=auth.reason, gate="final_auth",
                  open_positions=_open_now, max_positions=MAX_OPTION_POSITIONS,
                  spread_pct=(f"{stab.spread_pct(q):.3f}"
                              if stab.spread_pct(q) is not None else "n/a"))
            telemetry.emit_decision(
                "ENTRY_REJECTED", reason=auth.reason,
                candidate_id=cand_ids.get(name), token=rec.get("token"),
                trading_symbol=rec.get("symbol"))
            continue

        # Baseline quote guard, retained so behaviour is unchanged when
        # the stabilization gates are configured OFF.
        if q is None or q["ltp"] <= 0:
            ledger.reject("quote_no_ltp")
            trace(event="entry_rejected", symbol=rec["symbol"],
                  token=rec["token"], reason="no_live_quote")
            continue

        ledger.passed()

        # analytics only - fair value / mispricing edge, never the fill
        key = (name, rec["expiry"])
        if key not in iv_cache:
            iv_cache[key] = angel.fetch_option_iv(smart, name, rec["expiry"])
        fair = fair_value(data[name]["spot"], rec, today, iv_cache[key])

        sc = score_by_name.get(name, {})
        if open_option(conn, rec, q, data[name]["spot"], cand["direction"],
                       today, log, fair, cand.get("tag", ""),
                       score=sc.get("score"), rank_pos=sc.get("rank"),
                       tier=cand.get("tier"),
                       candidate_id=cand_ids.get(name)):
            traded_today.add(name)
            open_count += 1
            ledger.entry()

    finish("entry_evaluation_complete")


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
    # THIRD sink: close_option() also emits observability. The savepoint
    # below rolls back the trade row but cannot roll back a write to a
    # different database, so the probe was minting a phantom _SELFCHECK
    # exit every cycle. Suppress at the sink, not downstream.
    _sim = telemetry.simulation()
    _sim.__enter__()
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
        _sim.__exit__(None, None, None)
        conn.execute("ROLLBACK TO selfcheck")
        conn.execute("RELEASE selfcheck")
    return True


def main():
    conn = db_init()
    today = date.today()
    log = []

    # Observability store: a SEPARATE file. Production books are never
    # opened by telemetry. Per-run only - it is deliberately NOT in
    # state_sync's owned list, so nothing about state persistence changes.
    telemetry.init()

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
    telemetry.close_cycle()   # observability only; fail-open
    if log:
        print("\n".join(log))
    conn.close()


if __name__ == "__main__":
    main()
