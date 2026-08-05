"""Regression tests for the options exit engine.

Runs the REAL options_trader.process() against a scripted quote feed.
Only the two external services (yfinance bars, Angel One quotes) are
stubbed; every exit decision is made by production code.

    python test_exit_engine.py

Exits non-zero on any failure so CI shows a red run.

Covers the intra-interval peak-capture fix: before it, the high-water mark
was built from a SAMPLED series of poll prices, so a premium that spiked
and faded between two cycles never ratcheted the trailing stop. That cost
NIFTY11AUG2624600CE on 2026-08-05 - traded to ~193, sampled peak 183.25,
booked -Rs.460 where the true peak implied +Rs.655.
"""

import os
import sqlite3
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

import pandas as pd

import angelone_client as angel
import options_trader as ot

TODAY = date.today()
REAL_DT = datetime
FAILURES = []


def stub_bars(ticker, **kw):
    idx = pd.date_range(REAL_DT.combine(TODAY, dtime(9, 15)), periods=60,
                        freq="5min")
    base = {"^NSEI": 24300, "^NSEBANK": 52000}.get(ticker, 1300)
    closes = [base * (1 + 0.01 * i / 60) for i in range(60)]
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": [1_000_000] * 60}, index=idx)


ot.yf.download = stub_bars
angel.login = lambda: object()
angel.fetch_option_iv = lambda *a, **k: {}
angel.load_instrument_master(ot.UNIVERSE_NAMES)

QUOTE = {"ltp": 0.0, "high": 0.0, "low": 0.0}


def stub_quotes(smart, tokens):
    p, hi, lo = QUOTE["ltp"], QUOTE["high"], QUOTE["low"]
    return {str(t): {"ltp": p, "bid": round(p - 0.05, 2),
                     "ask": round(p + 0.05, 2), "volume": 400000, "oi": 300000,
                     "open": p, "high": hi, "low": lo, "close": p,
                     "trading_symbol": f"T{t}"} for t in map(str, tokens)}


angel.get_quotes = stub_quotes

TRACKED = "TRACED-CONTRACT"


def seed(db, entry_px, day_high=None, day_low=None):
    """One open position, priced as open_option() would have written it."""
    if os.path.exists(db):
        os.remove(db)
    ot.DB = db
    conn = ot.db_init()
    conn.execute("INSERT OR REPLACE INTO meta VALUES('cash','100000')")
    conn.execute(
        "INSERT INTO options_positions(symbol,option_type,strike,expiry,qty,"
        "entry_price,entry_time,token,trading_symbol,lots,lotsize,high_water,"
        "stop_price,last_price,day_high_seen,day_low_seen) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("RELIANCE", "CE", 1290, "2026-08-25", 100, entry_px,
         f"{TODAY}T09:31:00", "99999", TRACKED, 1, 100, entry_px,
         round(entry_px * 0.85, 2), entry_px,
         day_high if day_high is not None else entry_px,
         day_low if day_low is not None else entry_px))
    conn.commit()
    conn.close()


def cycle(db, hh, mm, ltp, high=None, low=None):
    """Run one production cycle. Returns the closed trade row, or None."""
    QUOTE.update(ltp=ltp, high=high if high is not None else ltp,
                 low=low if low is not None else ltp)

    class FrozenClock(REAL_DT):
        @classmethod
        def now(cls, tz=None):
            b = REAL_DT.combine(TODAY, dtime(hh, mm))
            return b if tz is None else b.replace(tzinfo=tz)

    ot.datetime = FrozenClock
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ot.process(conn, [], TODAY)
    row = conn.execute(
        "SELECT * FROM options_trades WHERE trading_symbol=?", (TRACKED,)
    ).fetchone()
    pos = conn.execute("SELECT * FROM options_positions WHERE trading_symbol=?",
                       (TRACKED,)).fetchone()
    conn.close()
    return (dict(row) if row else None, dict(pos) if pos else None)


def check(name, got, want, tol=0.01):
    ok = (abs(got - want) <= tol if isinstance(want, (int, float))
          and isinstance(got, (int, float)) else got == want)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


def test_intra_interval_peak_ratchets_the_stop():
    """The 2026-08-05 NIFTY trade, with the session high now visible.

    Poll prices never exceed 183.25, but the session high reaches 193 -
    exactly the case the old sampled-only high-water missed.
    """
    print("\n[1] intra-interval peak ratchets the trailing stop")
    db = "t_peak.db"
    seed(db, 164.80, day_high=164.80, day_low=164.80)
    cycle(db, 10, 0, 170.00, high=170.00)
    _, pos = cycle(db, 11, 0, 183.30, high=193.00)   # spike between polls
    check("high_water picked up the session high", pos["high_water"], 193.00)
    check("trail stop = 193 x 0.88", pos["stop_price"], 169.84)
    trade, _ = cycle(db, 11, 30, 169.00, high=193.00, low=169.00)
    check("exit fired at the ratcheted stop", trade["exit_price"], 169.84)
    check("exit is a PROFIT", round(trade["pnl"], 2), 504.00)
    check("labelled a trailing stop", trade["reason"], "Trailing stop")
    check("peak attributed to the interval", trade["peak_source"],
          "INTRA_INTERVAL_HIGH")
    os.remove(db)


def test_pre_entry_high_is_excluded():
    """A session high set BEFORE entry must not ratchet anything.

    Guards the obvious wrong fix - using q["high"] directly would invent
    profit from a spike the position never held through.
    """
    print("\n[2] pre-entry session high is excluded")
    db = "t_pre.db"
    seed(db, 100.00, day_high=180.00, day_low=100.00)   # 180 happened at 09:20
    _, pos = cycle(db, 10, 0, 101.00, high=180.00)      # unchanged since entry
    # high_water tracks the BID (100.95), not the 180 spike we never held
    check("high_water ignores the pre-entry spike", pos["high_water"], 100.95)
    check("stop still the initial -15%", pos["stop_price"], 85.00)
    os.remove(db)


def test_intra_interval_trough_triggers_stop():
    """A session low through the stop fills, even if price recovered."""
    print("\n[3] intra-interval trough triggers the stop")
    db = "t_trough.db"
    seed(db, 100.00, day_high=100.00, day_low=100.00)
    trade, _ = cycle(db, 11, 0, 95.00, high=100.00, low=80.00)
    check("exit fired despite recovery to 95", trade is not None, True)
    check("filled AT the stop level", trade["exit_price"], 85.00)
    check("tagged STOP_LEVEL", trade["price_source"], "STOP_LEVEL")
    check("labelled an initial stop", trade["reason"], "Initial stop")
    os.remove(db)


def test_trail_label_boundary():
    """Whenever the stop is trailed, the exit must SAY trailing.

    Activation (high_water >= entry x 1.10) and the label once used
    different operators (>= vs >), so a peak landing exactly on the
    threshold trailed the stop but was recorded as "Initial stop".

    Note: the exact boundary is unreachable in practice - 100.0 * 1.1 is
    110.00000000000001 in IEEE-754, so a high-water of exactly 110.00 does
    NOT activate. The operators are aligned as defensive consistency; what
    this test enforces is the invariant that matters, that a trailed stop
    is never labelled an initial one.
    """
    print("\n[4] label matches behaviour just past the activation threshold")
    db = "t_label.db"
    seed(db, 100.00, day_high=100.00, day_low=100.00)
    _, pos = cycle(db, 10, 0, 110.05, high=110.05)     # just above +10%
    check("stop was trailed", pos["stop_price"], 96.844)
    check("trailed above the initial stop", pos["stop_price"] > 85.00, True)
    trade, _ = cycle(db, 10, 30, 96.00, low=96.00)
    check("reason says Trailing, not Initial", trade["reason"], "Trailing stop")
    os.remove(db)


def test_ratchet_is_monotonic():
    """The stop must never loosen when price falls."""
    print("\n[5] stop ratchet is monotonic")
    db = "t_mono.db"
    seed(db, 100.00, day_high=100.00, day_low=100.00)
    stops = []
    for (hh, mm), px in [((10, 0), 115.05), ((10, 30), 130.05),
                         ((11, 0), 120.05), ((11, 30), 118.05)]:
        _, pos = cycle(db, hh, mm, px, high=px, low=px)
        if pos:
            stops.append(round(pos["stop_price"], 2))
    print(f"       stop path: {stops}")
    check("never decreases", all(b >= a for a, b in zip(stops, stops[1:])), True)
    check("peaked at 130.05 x 0.88", max(stops), 114.44)
    os.remove(db)


def test_winner_survives_to_square_off():
    """The RELIANCE case: a winner that never retraces 12% rides to 15:15."""
    print("\n[6] untriggered winner reaches the 15:15 square-off")
    db = "t_win.db"
    seed(db, 20.60, day_high=20.60, day_low=20.60)
    for (hh, mm), px in [((11, 30), 24.50), ((13, 30), 28.50), ((14, 30), 27.20)]:
        cycle(db, hh, mm, px, high=px, low=px)
    trade, _ = cycle(db, 15, 16, 27.75, high=28.50, low=27.20)
    check("closed at square-off", trade["reason"], "Square-off 15:15")
    check("filled at the live bid", trade["price_source"], "LIVE_BID")
    check("profit booked", round(trade["pnl"], 2), 710.00)
    os.remove(db)


def test_no_overnight_carry_when_degraded():
    """15:15 must flatten the book even with every data source down."""
    print("\n[7] degraded 15:15 leaves nothing open")
    db = "t_deg.db"
    seed(db, 100.00, day_high=100.00, day_low=100.00)
    cycle(db, 14, 0, 105.00, high=105.00, low=105.00)

    saved = angel.login
    angel.login = lambda: None                     # Angel One unreachable

    class FrozenClock(REAL_DT):
        @classmethod
        def now(cls, tz=None):
            b = REAL_DT.combine(TODAY, dtime(15, 16))
            return b if tz is None else b.replace(tzinfo=tz)

    ot.datetime = FrozenClock
    conn = sqlite3.connect(db)
    ot.process(conn, [], TODAY)
    n_open = conn.execute("SELECT COUNT(*) FROM options_positions "
                          "WHERE trading_symbol=?", (TRACKED,)).fetchone()[0]
    src = conn.execute("SELECT price_source FROM options_trades "
                       "WHERE trading_symbol=?", (TRACKED,)).fetchone()
    conn.close()
    angel.login = saved
    check("no position carried overnight", n_open, 0)
    check("closed at last observed live price", src[0], "LAST_OBSERVED_LIVE")
    os.remove(db)


def test_write_path_intact():
    """close_option()'s INSERT must match its placeholder count.

    A 26-column / 25-placeholder mismatch once crashed every exit and
    stranded four positions overnight.
    """
    print("\n[8] trade write path")
    db = "t_write.db"
    seed(db, 100.00)
    ot.DB = db
    conn = ot.db_init()
    ot.selfcheck(conn)          # raises SystemExit if the write path is broken
    conn.close()
    check("selfcheck passed", True, True)
    os.remove(db)


if __name__ == "__main__":
    for fn in (test_intra_interval_peak_ratchets_the_stop,
               test_pre_entry_high_is_excluded,
               test_intra_interval_trough_triggers_stop,
               test_trail_label_boundary,
               test_ratchet_is_monotonic,
               test_winner_survives_to_square_off,
               test_no_overnight_carry_when_degraded,
               test_write_path_intact):
        fn()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All exit-engine regression tests passed.")
