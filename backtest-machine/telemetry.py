"""
Observability capture - P0-E temporal foundation.
=================================================

Records the production decision chain so it can later be replayed and
reasoned about causally. It records; it never decides.

    cycle -> signal_snapshot -> quote_snapshot -> candidate_snapshot
          -> decision -> position_snapshot -> exit_snapshot
          -> post_exit_path

ARCHITECTURAL CONTRACT - the whole point of this module
-------------------------------------------------------
1. FAIL-OPEN. Every public function is wrapped so that NOTHING it does can
   raise into the caller. A telemetry failure prints one line and trading
   continues untouched. Risk management must never depend on observability
   succeeding - that inversion is exactly the class of coupling the audit
   flagged as S-05.
2. NO RETURN VALUE IS CONSUMED BY TRADING. `new_cycle()` returns an id
   used only as a correlation key in later emit() calls. Every other
   function returns None.
3. SEPARATE STORE. Writes go to observability.db. The production books
   (options_trades.db, simple_trades.db) are never opened here.

NULL MEANS UNAVAILABLE - AND ONLY THAT
--------------------------------------
Every timestamp column is nullable. A NULL is a factual statement that the
value could not be observed. It is never a default, never coalesced, and
receive-time is NEVER substituted for market-time. Downstream code must
propagate UNAVAILABLE rather than approximate it. test_telemetry.py
enforces this.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not gate, filter, delay, or alter any trading decision. It reads
values the strategy has already computed and writes them down. The
completed-bar direction it records (see emit_signal) is an OBSERVATION
ONLY - computed for future comparison and never returned to the caller.
"""

import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "observability.db"

# Bar cadence used only to classify COMPLETED vs FORMING. Mirrors the
# strategy's INTERVAL; it does not influence any strategy calculation.
BAR_SECONDS = 300

_conn = None
_cycle_id = None
_enabled = True
_initialised = False       # T-2: no store is created until init() is called
_warned_uninitialised = False
_simulating = False        # T-1: suppresses recording of simulated actions


def _now():
    return datetime.now().isoformat(timespec="microseconds")


def _safe(fn):
    """Fail-open wrapper. Telemetry must never propagate an exception.

    Also enforces the two suppression gates:
      * _enabled    - global off switch (tests only)
      * _simulating - T-1: inside a simulation context nothing is recorded
    `init` is exempt from the simulation gate so a store can still be
    opened while a probe is running.
    """
    def wrapper(*a, **kw):
        if not _enabled:
            return None
        if _simulating and fn.__name__ != "init":
            return None
        try:
            return fn(*a, **kw)
        except Exception as e:
            print(f"  telemetry: {fn.__name__} failed "
                  f"({type(e).__name__}: {e}) - continuing, trading unaffected")
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class simulation:
    """Context in which simulated actions are NOT recorded (T-1).

    selfcheck() proves the trade write path by driving a probe through
    close_option() inside a rolled-back SAVEPOINT. The rollback undoes the
    row in options_trades.db - it cannot undo a write to a DIFFERENT
    database, so the probe was minting a phantom `_SELFCHECK` exit into
    observability.db on every cycle.

    Filtering those rows out downstream would be the wrong fix: the sink
    itself must not record them. This context makes that explicit, and it
    is re-entrant and exception-safe so a failing probe still restores the
    flag. It suppresses ONLY telemetry - close_option()'s trading
    behaviour, the savepoint, and the Telegram path are untouched.
    """

    def __enter__(self):
        global _simulating
        self._prev = _simulating
        _simulating = True
        return self

    def __exit__(self, *exc):
        global _simulating
        _simulating = self._prev
        return False


def is_simulating():
    return _simulating


SCHEMA = """
CREATE TABLE IF NOT EXISTS cycle(
    cycle_id TEXT PRIMARY KEY, run_id TEXT, trading_date TEXT,
    scheduled_at TEXT, workflow_started_at TEXT, process_started_at TEXT,
    cycle_completed_at TEXT, prev_cycle_completed_at TEXT,
    inter_cycle_gap_s REAL, experiment_flags TEXT, code_sha TEXT);

CREATE TABLE IF NOT EXISTS signal_snapshot(
    signal_snapshot_id TEXT PRIMARY KEY, cycle_id TEXT, symbol TEXT,
    data_requested_at TEXT, data_received_at TEXT,
    bar_ts TEXT, bar_status TEXT, bar_age_s REAL,
    direction TEXT, momentum REAL, trend_quality REAL, spot REAL,
    direction_completed_bar TEXT, session_date TEXT, session_status TEXT);

CREATE TABLE IF NOT EXISTS quote_snapshot(
    quote_snapshot_id TEXT PRIMARY KEY, cycle_id TEXT, token TEXT,
    trading_symbol TEXT, requested_at TEXT, received_at TEXT,
    exch_feed_time TEXT, exch_trade_time TEXT, quote_age_s REAL,
    ltp REAL, bid REAL, ask REAL, volume INTEGER, oi INTEGER,
    day_high REAL, day_low REAL);

CREATE TABLE IF NOT EXISTS candidate_snapshot(
    candidate_id TEXT PRIMARY KEY, cycle_id TEXT, symbol TEXT,
    direction TEXT, token TEXT, trading_symbol TEXT, strike REAL,
    expiry TEXT, dte INTEGER, quote_snapshot_id TEXT,
    score REAL, rank INTEGER, would_trade INTEGER, tier TEXT);

CREATE TABLE IF NOT EXISTS decision(
    decision_id TEXT PRIMARY KEY, cycle_id TEXT, candidate_id TEXT,
    decided_at TEXT, action TEXT, reason TEXT, trade_token TEXT,
    trading_symbol TEXT, entry_price REAL, qty INTEGER);

CREATE TABLE IF NOT EXISTS position_snapshot(
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, token TEXT,
    trading_symbol TEXT, evaluated_at TEXT, mark REAL, high_water REAL,
    stop_price REAL, day_high_seen REAL, day_low_seen REAL,
    peak_source TEXT, trigger_value REAL, quote_snapshot_id TEXT);

CREATE TABLE IF NOT EXISTS exit_snapshot(
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT, token TEXT,
    trading_symbol TEXT, decided_at TEXT, exit_reason TEXT,
    exit_price REAL, trigger_value REAL, high_water_at_exit REAL,
    peak_source TEXT, quote_snapshot_id TEXT, pnl REAL);

CREATE TABLE IF NOT EXISTS post_exit_path(
    id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, trading_symbol TEXT,
    exited_at TEXT, observed_at TEXT, quote_snapshot_id TEXT,
    ltp REAL, bid REAL, ask REAL);

CREATE TABLE IF NOT EXISTS replay_result(
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, mode TEXT,
    trade_ref TEXT, field TEXT, expected TEXT, replayed TEXT,
    difference TEXT, classification TEXT);
"""


@_safe
def init(db_path=None):
    """Open (creating if needed) the observability store.

    T-2: this is the ONLY place a store is created. Nothing auto-creates
    one on first emit, so importing or exercising the trading engine
    leaves no filesystem artifact. Production opts in explicitly from
    main(); tests pass their own path.
    """
    global _conn, _initialised
    if _conn is not None:
        return _conn
    _conn = sqlite3.connect(str(db_path or DB))
    _conn.executescript(SCHEMA)
    _conn.commit()
    _initialised = True
    return _conn


def _c():
    """Current store, or None if telemetry was never initialised.

    Returns None rather than lazily creating a database - see T-2. The
    first uninitialised emit says so once, so production telemetry can
    never be dropped SILENTLY; it just cannot spring a file into
    existence as a side effect of an import.
    """
    global _warned_uninitialised
    if _conn is not None:
        return _conn
    if not _warned_uninitialised:
        _warned_uninitialised = True
        print("  telemetry: not initialised (telemetry.init() was never "
              "called) - observability skipped this run, trading unaffected")
    return None


@_safe
def new_cycle(run_id=None, scheduled_at=None, workflow_started_at=None,
              trading_date=None, experiment_flags=None, code_sha=None):
    """Open a cycle and return its correlation id.

    The returned value is a correlation key only. No trading branch reads
    it; if telemetry is disabled it is None and every later emit() no-ops.
    """
    global _cycle_id
    c = _c()
    if c is None:
        return None
    prev = c.execute("SELECT cycle_completed_at FROM cycle "
                     "WHERE cycle_completed_at IS NOT NULL "
                     "ORDER BY process_started_at DESC LIMIT 1").fetchone()
    prev_done = prev[0] if prev else None
    started = _now()
    gap = None
    if prev_done:
        gap = (datetime.fromisoformat(started)
               - datetime.fromisoformat(prev_done)).total_seconds()
    _cycle_id = uuid.uuid4().hex
    c.execute("INSERT INTO cycle(cycle_id,run_id,trading_date,scheduled_at,"
              "workflow_started_at,process_started_at,prev_cycle_completed_at,"
              "inter_cycle_gap_s,experiment_flags,code_sha) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)",
              (_cycle_id, run_id or os.environ.get("GITHUB_RUN_ID"),
               trading_date, scheduled_at, workflow_started_at, started,
               prev_done, gap, experiment_flags, code_sha))
    c.commit()
    return _cycle_id


@_safe
def close_cycle():
    c = _c()
    if c is None or _cycle_id is None:
        return None
    c.execute("UPDATE cycle SET cycle_completed_at=? WHERE cycle_id=?",
              (_now(), _cycle_id))
    c.commit()


def _parse_ts(value):
    """Best-effort timestamp parse -> datetime, or None.

    Two producers feed the observability store and they do NOT agree on a
    format:

        our own clock      2026-08-17T15:15:59.162813        (ISO)
        Angel One feed     17-Aug-2026 15:15:57              (broker format)

    quote_age_s was computed with fromisoformat() alone, which raises on
    the broker format. The exception was swallowed, so `quote_age_s` was
    NULL on EVERY row of Monday's production run even though both
    timestamps were captured correctly - the entire point of the freshness
    capture produced no measurement.

    Returns None on anything unrecognised, so the caller still records
    NULL rather than a guessed age. No threshold is applied anywhere; this
    only makes an already-captured quantity computable.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _bar_status(bar_ts, observed_at):
    """COMPLETED / FORMING / UNKNOWN for the bar the signal was built on.

    A bar stamped T covers [T, T+BAR_SECONDS). If we observed the series
    before that window closed, the last bar was still forming. Recorded
    for measurement only - the strategy's use of .iloc[-1] is untouched.
    """
    if not bar_ts or not observed_at:
        return "UNKNOWN", None
    try:
        b = datetime.fromisoformat(str(bar_ts))
        o = datetime.fromisoformat(str(observed_at))
        if b.tzinfo is not None:
            b = b.replace(tzinfo=None)
        if o.tzinfo is not None:
            o = o.replace(tzinfo=None)
        age = (o - b).total_seconds()
        return ("COMPLETED" if age >= BAR_SECONDS else "FORMING"), age
    except Exception:
        return "UNKNOWN", None


@_safe
def emit_signal(symbol, requested_at, received_at, df=None, direction=None,
                momentum=None, trend_quality=None, spot=None,
                direction_fn=None, session_date=None):
    """Record the signal input and, separately, a completed-bar observation.

    `direction` is whatever production computed - copied verbatim, never
    recomputed. `direction_completed_bar` re-runs the SAME direction_fn on
    the series with the final (possibly forming) bar dropped. It is written
    down and NEVER returned, so it cannot influence anything. It exists so
    the CURRENT-vs-COMPLETED comparison can be made later without changing
    today's strategy.
    """
    c = _c()
    if c is None:
        return None
    bar_ts = None
    if df is not None and len(df):
        try:
            bar_ts = str(df.index[-1])
        except Exception:
            bar_ts = None
    status, age = _bar_status(bar_ts, received_at)
    completed_dir = None
    if direction_fn is not None and df is not None and len(df) > 1:
        try:
            completed_dir = direction_fn(df.iloc[:-1])
        except Exception:
            completed_dir = None
    sid = uuid.uuid4().hex
    sess = "VALID" if (session_date and bar_ts
                       and str(bar_ts)[:10] == str(session_date)) else \
           ("STALE_OR_AMBIGUOUS" if bar_ts else "UNKNOWN")
    c.execute("INSERT INTO signal_snapshot(signal_snapshot_id,cycle_id,symbol,"
              "data_requested_at,data_received_at,bar_ts,bar_status,bar_age_s,"
              "direction,momentum,trend_quality,spot,direction_completed_bar,"
              "session_date,session_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (sid, _cycle_id, symbol, requested_at, received_at, bar_ts,
               status, age, direction, momentum, trend_quality, spot,
               completed_dir, str(session_date) if session_date else None, sess))
    c.commit()
    return sid


@_safe
def emit_quotes(quotes, requested_at, received_at):
    """Record one quote snapshot per token. Returns {token: snapshot_id}.

    quote_age_s is decided/received minus the EXCHANGE feed time. When the
    broker supplies no feed time the age is NULL - receive time is not
    substituted, because that would measure our latency and label it market
    staleness.

    The age is DERIVED from two timestamps that are also stored verbatim
    alongside it, so a parsing change can never invent a value: if either
    timestamp cannot be read the age stays NULL and the raw strings remain
    available for offline reconstruction.
    """
    c = _c()
    if c is None or not quotes:
        return {}
    out = {}
    for token, q in quotes.items():
        feed = q.get("exch_feed_time")
        age = None
        if feed and received_at:
            f, r = _parse_ts(feed), _parse_ts(received_at)
            if f is not None and r is not None:
                # The subtraction is guarded SEPARATELY from the parse. Two
                # datetimes can both parse and still not be subtractable:
                # the broker stamp is naive while simple_trader stamps
                # tz-AWARE times, and mixing them raises TypeError. Without
                # a local guard that escapes to @_safe and aborts the whole
                # emit - losing every quote row for the cycle plus the
                # quote_snapshot_id links - which is strictly worse than the
                # NULL age this replaces. No offset is assumed: an
                # incomparable pair yields NULL, never a guessed age.
                try:
                    age = (r - f).total_seconds()
                except TypeError:
                    age = None
        qid = uuid.uuid4().hex
        out[str(token)] = qid
        c.execute("INSERT INTO quote_snapshot(quote_snapshot_id,cycle_id,token,"
                  "trading_symbol,requested_at,received_at,exch_feed_time,"
                  "exch_trade_time,quote_age_s,ltp,bid,ask,volume,oi,day_high,"
                  "day_low) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (qid, _cycle_id, str(token), q.get("trading_symbol"),
                   requested_at, received_at, feed, q.get("exch_trade_time"),
                   age, q.get("ltp"), q.get("bid"), q.get("ask"),
                   q.get("volume"), q.get("oi"), q.get("high"), q.get("low")))
    c.commit()
    return out


@_safe
def emit_candidate(symbol, direction, rec, quote_snapshot_id=None, score=None,
                   rank=None, would_trade=None, tier=None, today=None):
    c = _c()
    if c is None:
        return None
    cid = uuid.uuid4().hex
    exp = rec.get("expiry") if isinstance(rec, dict) else None
    dte = None
    if exp is not None and today is not None:
        try:
            dte = (exp - today).days
        except Exception:
            dte = None
    c.execute("INSERT INTO candidate_snapshot(candidate_id,cycle_id,symbol,"
              "direction,token,trading_symbol,strike,expiry,dte,"
              "quote_snapshot_id,score,rank,would_trade,tier) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (cid, _cycle_id, symbol, direction,
               str(rec.get("token")) if isinstance(rec, dict) else None,
               rec.get("symbol") if isinstance(rec, dict) else None,
               rec.get("strike") if isinstance(rec, dict) else None,
               str(exp) if exp else None, dte, quote_snapshot_id,
               score, rank, would_trade, tier))
    c.commit()
    return cid


@_safe
def emit_decision(action, reason=None, candidate_id=None, token=None,
                  trading_symbol=None, entry_price=None, qty=None):
    c = _c()
    if c is None:
        return None
    did = uuid.uuid4().hex
    c.execute("INSERT INTO decision(decision_id,cycle_id,candidate_id,"
              "decided_at,action,reason,trade_token,trading_symbol,"
              "entry_price,qty) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (did, _cycle_id, candidate_id, _now(), action, reason,
               str(token) if token else None, trading_symbol,
               entry_price, qty))
    c.commit()
    return did


@_safe
def emit_position(token, trading_symbol, mark=None, high_water=None,
                  stop_price=None, day_high_seen=None, day_low_seen=None,
                  peak_source=None, trigger_value=None,
                  quote_snapshot_id=None):
    c = _c()
    if c is None:
        return None
    c.execute("INSERT INTO position_snapshot(cycle_id,token,trading_symbol,"
              "evaluated_at,mark,high_water,stop_price,day_high_seen,"
              "day_low_seen,peak_source,trigger_value,quote_snapshot_id) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (_cycle_id, str(token) if token else None, trading_symbol,
               _now(), mark, high_water, stop_price, day_high_seen,
               day_low_seen, peak_source, trigger_value, quote_snapshot_id))
    c.commit()


@_safe
def emit_exit(token, trading_symbol, exit_reason, exit_price=None,
              trigger_value=None, high_water_at_exit=None, peak_source=None,
              quote_snapshot_id=None, pnl=None):
    """Record the exit AND the high-water mark that produced it.

    options_positions rows are DELETEd on close, so high_water is otherwise
    lost the instant a trade ends. Capturing it here is what makes the
    trailing-stop path reconstructable after the fact.
    """
    c = _c()
    if c is None:
        return None
    c.execute("INSERT INTO exit_snapshot(cycle_id,token,trading_symbol,"
              "decided_at,exit_reason,exit_price,trigger_value,"
              "high_water_at_exit,peak_source,quote_snapshot_id,pnl) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (_cycle_id, str(token) if token else None, trading_symbol,
               _now(), exit_reason, exit_price, trigger_value,
               high_water_at_exit, peak_source, quote_snapshot_id, pnl))
    c.commit()


@_safe
def emit_post_exit(token, trading_symbol, exited_at, quote,
                   quote_snapshot_id=None):
    """Price path AFTER an exit - the evidence S-20 needs.

    Without this, a trend-reversal exit can never be evaluated: we see the
    loss booked and nothing about what the contract did next.
    """
    c = _c()
    if c is None:
        return None
    c.execute("INSERT INTO post_exit_path(token,trading_symbol,exited_at,"
              "observed_at,quote_snapshot_id,ltp,bid,ask) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (str(token) if token else None, trading_symbol, exited_at,
               _now(), quote_snapshot_id,
               (quote or {}).get("ltp"), (quote or {}).get("bid"),
               (quote or {}).get("ask")))
    c.commit()


def current_cycle_id():
    return _cycle_id


def disable():
    """Used by tests to prove the trading path is unaffected when telemetry
    is completely inert."""
    global _enabled
    _enabled = False


def enable():
    global _enabled
    _enabled = True


def reset_for_test(db_path):
    """Test-only: point the module at a throwaway store.

    `db_path` may be ":memory:" for a store that never touches disk.
    """
    global _conn, _cycle_id, _enabled, _initialised
    global _warned_uninitialised, _simulating
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn, _cycle_id, _enabled = None, None, True
    _initialised, _warned_uninitialised, _simulating = False, False, False
    init(db_path)


def shutdown():
    """Release the store handle. Optional; the interpreter does this at
    exit. Provided so tests can unlink the file on Windows."""
    global _conn, _initialised
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn, _initialised = None, False
