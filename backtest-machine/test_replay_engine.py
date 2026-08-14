"""Tests for the replay engine (P0-B).

    python test_replay_engine.py

The properties under test are mostly NEGATIVE: replay must not mutate
production state, must not fabricate what was never recorded, and must not
claim an intra-interval ordering the data cannot support.
"""

import hashlib
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import replay_engine as re_

FAILURES = []
FIX = Path(tempfile.gettempdir()) / "replay_fixture.db"


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


def build_fixture():
    if FIX.exists():
        try:
            FIX.unlink()
        except PermissionError:
            pass
    c = sqlite3.connect(FIX)
    c.execute("""CREATE TABLE options_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, option_type TEXT,
        strike REAL, expiry TEXT, qty INTEGER, entry_price REAL,
        exit_price REAL, entry_time TEXT, exit_time TEXT, pnl REAL,
        reason TEXT, token TEXT, trading_symbol TEXT, lots INTEGER,
        lotsize INTEGER, entry_bid REAL, entry_ask REAL, exit_bid REAL,
        exit_ask REAL, price_source TEXT, entry_score REAL, entry_rank INTEGER,
        entry_tier TEXT, peak_source TEXT)""")
    c.execute("""CREATE TABLE ranking_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, mode TEXT, symbol TEXT,
        direction TEXT, tier TEXT, rank INTEGER, score REAL,
        would_trade INTEGER, breakdown TEXT, quote TEXT)""")
    # one trailing-stop trade: entry 100 -> exit 88 means high water 100
    c.execute("INSERT INTO options_trades(symbol,option_type,strike,expiry,"
              "qty,entry_price,exit_price,entry_time,exit_time,pnl,reason,"
              "token,trading_symbol,lots,lotsize,entry_bid,entry_ask,"
              "price_source) VALUES('AAA','CE',100,'2026-08-25',100,100.0,"
              "88.0,'2026-08-14T10:00:00','2026-08-14T11:00:00',-1200.0,"
              "'Trailing stop','999','AAA25AUG26100CE',1,100,99.5,100.0,"
              "'STOP_LEVEL')")
    c.execute("INSERT INTO ranking_log(ts,mode,symbol,direction,tier,rank,"
              "score,would_trade) VALUES('2026-08-14T09:59:58','shadow','AAA',"
              "'BULL','confluence',1,0.8,1)")
    c.commit()
    c.close()


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_core_fields_exact():
    print("\n[1] core-15 fields reconcile exactly")
    build_fixture()
    res = re_.run_mode_a(FIX)
    check("one trade replayed", len(res), 1)
    exact, mismatch, rows = re_.reconcile(res)
    check("all core fields exact", exact, len(re_.CORE15))
    check("zero mismatches", mismatch, 0)


def test_derived_fields():
    print("\n[2] deterministically derived fields")
    res = re_.run_mode_a(FIX)
    _, r = res[0]
    check("dte derived", r["dte"][0], 11)
    check("dte classified DERIVED", r["dte"][1], re_.DERIVED)
    check("initial stop = entry x 0.85", r["initial_stop"][0], 85.0)
    check("implied high water = exit / 0.88", r["implied_high_water"][0], 100.0)
    check("cost = qty x entry", r["cost"][0], 10000.0)
    check("pnl recomputed independently", r["pnl_recomputed"][0], -1200.0)
    check("entry half-spread cost", r["entry_half_spread_cost"][0], 25.0)


def test_unavailable_never_fabricated():
    print("\n[3] unavailable fields are NULL, never invented")
    res = re_.run_mode_a(FIX)
    _, r = res[0]
    for f in ("underlying_spot_entry", "signal_bar_ts", "quote_market_timestamp",
              "quote_received_at", "quote_age_s", "decision_at", "cycle_id",
              "run_id", "delta", "iv", "post_exit_path",
              "max_favorable_excursion", "max_adverse_excursion"):
        if r[f][0] is not None or r[f][1] != re_.UNAVAIL:
            FAILURES.append(f"fabricated {f}")
            print(f"  FAIL  {f} fabricated: {r[f]}")
    print(f"  PASS  13 temporal/market fields all NULL + UNAVAILABLE")


def test_path_order_ambiguity_preserved():
    print("\n[4] intra-interval event order preserved as UNAVAILABLE")
    res = re_.run_mode_a(FIX)
    _, r = res[0]
    check("event order not claimed", r["intra_interval_event_order"][0], None)
    check("classified UNAVAILABLE", r["intra_interval_event_order"][1],
          re_.UNAVAIL)


def test_approximation_is_labelled():
    print("\n[5] approximations name their assumption")
    res = re_.run_mode_a(FIX)
    _, r = res[0]
    check("direction reconstructed", r["direction_at_entry"][0], "BULL")
    check("labelled APPROXIMATED with reason",
          r["direction_at_entry"][1].startswith(re_.APPROX)
          and "nearest ranking_log" in r["direction_at_entry"][1], True)


def test_determinism():
    print("\n[6] replay is deterministic")
    a = re_.run_mode_a(FIX)
    b = re_.run_mode_a(FIX)
    check("two runs identical",
          [x[1] for x in a] == [x[1] for x in b], True)


def test_does_not_mutate_production():
    print("\n[7] replay does not mutate the source database")
    before = sha(FIX)
    re_.run_mode_a(FIX)
    re_.run_mode_a(FIX)
    check("byte-identical after two replays", sha(FIX), before)


def test_readonly_connection_rejects_writes():
    print("\n[8] source opened read-only")
    conn = re_.ro(FIX)
    blocked = False
    try:
        conn.execute("UPDATE options_trades SET pnl=0")
        conn.commit()
    except sqlite3.OperationalError:
        blocked = True
    conn.close()
    check("write attempt rejected", blocked, True)


def test_strategy_params_untouched():
    print("\n[9] replay does not alter strategy parameters")
    import options_trader as ot
    check("MAX_POSITIONS", ot.MAX_POSITIONS, 4)
    check("MAX_PER_TRADE", ot.MAX_PER_TRADE, 25000)
    check("INITIAL_STOP_PCT", ot.INITIAL_STOP_PCT, -0.15)
    check("TRAIL_ACTIVATE_PCT", ot.TRAIL_ACTIVATE_PCT, 0.10)
    check("TRAIL_PCT", ot.TRAIL_PCT, 0.12)
    check("SIGNAL_MAX_AGE_SEC", ot.SIGNAL_MAX_AGE_SEC, 285)
    check("experiment still enabled", ot.EXPERIMENT_NO_SAME_CYCLE_REENTRY, True)


def test_mode_b_absent_is_not_faked():
    print("\n[10] Mode B reports absence rather than inventing a chain")
    missing = Path(tempfile.gettempdir()) / "no_such_obs.db"
    if missing.exists():
        missing.unlink()
    check("returns None when no telemetry exists", re_.run_mode_b(missing), None)


if __name__ == "__main__":
    for fn in (test_core_fields_exact, test_derived_fields,
               test_unavailable_never_fabricated,
               test_path_order_ambiguity_preserved,
               test_approximation_is_labelled, test_determinism,
               test_does_not_mutate_production,
               test_readonly_connection_rejects_writes,
               test_strategy_params_untouched, test_mode_b_absent_is_not_faked):
        fn()
    if FIX.exists():
        try:
            FIX.unlink()
        except Exception:
            pass
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All replay-engine tests passed.")
