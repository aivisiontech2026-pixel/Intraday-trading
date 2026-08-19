"""Tests for the observability layer (P0-E).

    python test_telemetry.py

Exits non-zero on failure, matching the other suites' convention.

The central property under test is NEGATIVE: telemetry must be incapable
of affecting trading. Several tests therefore assert that a broken or
disabled telemetry layer changes nothing at all.
"""

import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import telemetry as tm

FAILURES = []
TMP = Path(tempfile.gettempdir()) / "obs_test.db"


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


def fresh():
    """Close any open handle BEFORE unlinking - Windows keeps the file
    locked while a sqlite connection is alive."""
    if tm._conn is not None:
        try:
            tm._conn.close()
        except Exception:
            pass
        tm._conn = None
    if TMP.exists():
        try:
            TMP.unlink()
        except PermissionError:
            pass
    tm.reset_for_test(TMP)


def test_cycle_identity():
    print("\n[1] cycle id uniqueness + gap arithmetic")
    fresh()
    a = tm.new_cycle(trading_date="2026-08-14")
    tm.close_cycle()
    b = tm.new_cycle(trading_date="2026-08-14")
    tm.close_cycle()
    check("two cycles get distinct ids", a != b, True)
    c = sqlite3.connect(TMP)
    n = c.execute("SELECT COUNT(DISTINCT cycle_id) FROM cycle").fetchone()[0]
    check("both persisted, ids distinct", n, 2)
    first = c.execute("SELECT inter_cycle_gap_s FROM cycle "
                      "WHERE cycle_id=?", (a,)).fetchone()[0]
    second = c.execute("SELECT inter_cycle_gap_s FROM cycle "
                       "WHERE cycle_id=?", (b,)).fetchone()[0]
    check("first cycle gap is NULL (no predecessor)", first, None)
    check("second cycle gap computed", second is not None, True)
    c.close()


def test_timestamp_ordering():
    print("\n[2] timestamp ordering invariant")
    fresh()
    tm.new_cycle()
    t0 = datetime.now()
    req = t0.isoformat()
    recv = (t0 + timedelta(seconds=2)).isoformat()
    tm.emit_quotes({"1": {"ltp": 10, "bid": 9.9, "ask": 10.1,
                          "exch_feed_time": (t0 - timedelta(seconds=1)).isoformat()}},
                   req, recv)
    c = sqlite3.connect(TMP)
    r = c.execute("SELECT requested_at,received_at,exch_feed_time,quote_age_s "
                  "FROM quote_snapshot").fetchone()
    check("requested <= received", r[0] <= r[1], True)
    check("quote age computed from FEED time", round(r[3], 1), 3.0)
    c.close()


def test_quote_age_null_when_no_feed_time():
    print("\n[3] no feed time => quote age UNAVAILABLE (never substituted)")
    fresh()
    tm.new_cycle()
    now = datetime.now()
    tm.emit_quotes({"9": {"ltp": 5, "bid": 4.9, "ask": 5.1}},
                   now.isoformat(), (now + timedelta(seconds=4)).isoformat())
    c = sqlite3.connect(TMP)
    r = c.execute("SELECT exch_feed_time,received_at,quote_age_s "
                  "FROM quote_snapshot").fetchone()
    check("exch_feed_time NULL", r[0], None)
    check("received_at still recorded", r[1] is not None, True)
    check("quote_age_s NULL - receive time NOT substituted", r[2], None)
    c.close()


def test_quote_age_parses_broker_timestamp_format():
    """P0-D F-1: Angel One stamps `17-Aug-2026 15:15:57`, not ISO.

    fromisoformat() alone raises on it, the exception was swallowed, and
    quote_age_s was NULL on every row of Monday's production run while the
    raw timestamps were captured correctly.
    """
    print("\n[18] broker timestamp format yields a real quote age")
    check("ISO still parses",
          tm._parse_ts("2026-08-17T15:15:59.162813").second, 59)
    check("Angel One format parses",
          tm._parse_ts("17-Aug-2026 15:15:57").second, 57)
    check("fromisoformat alone would have failed here",
          _iso_only("17-Aug-2026 15:15:57"), None)
    check("garbage still yields None", tm._parse_ts("not a timestamp"), None)
    check("None yields None", tm._parse_ts(None), None)
    check("empty string yields None", tm._parse_ts("   "), None)

    fresh()
    tm.new_cycle()
    # exactly Monday's observed pair
    tm.emit_quotes({"9": {"ltp": 5, "bid": 4.9, "ask": 5.1,
                          "exch_feed_time": "17-Aug-2026 15:15:57"}},
                   "2026-08-17T15:15:58.000000",
                   "2026-08-17T15:15:59.162813")
    c = sqlite3.connect(TMP)
    r = c.execute("SELECT exch_feed_time,received_at,quote_age_s "
                  "FROM quote_snapshot").fetchone()
    check("raw feed time still stored verbatim", r[0], "17-Aug-2026 15:15:57")
    check("quote_age_s is now COMPUTED", round(r[2], 4), 2.1628)
    c.close()

    # an unparseable feed time must still leave NULL, never a guess
    fresh()
    tm.new_cycle()
    tm.emit_quotes({"9": {"ltp": 5, "bid": 4.9, "ask": 5.1,
                          "exch_feed_time": "garbage"}},
                   "2026-08-17T15:15:58", "2026-08-17T15:15:59")
    c = sqlite3.connect(TMP)
    r = c.execute("SELECT exch_feed_time,quote_age_s FROM quote_snapshot").fetchone()
    check("unparseable feed time stored verbatim", r[0], "garbage")
    check("age stays NULL - no substitution", r[1], None)
    c.close()

    # MIXED tz-awareness: both parse, but subtracting raises TypeError. The
    # broker stamp is naive; simple_trader stamps AWARE times. The failure
    # must stay LOCAL - losing the whole emit would drop every quote row for
    # the cycle and every quote_snapshot_id link with it.
    fresh()
    tm.new_cycle()
    res = tm.emit_quotes({"9": {"ltp": 5, "bid": 4.9, "ask": 5.1,
                                "exch_feed_time": "17-Aug-2026 15:15:57"}},
                         "2026-08-17T15:15:58+05:30",
                         "2026-08-17T15:15:59.162813+05:30")
    check("emit did NOT abort", isinstance(res, dict) and len(res) == 1, True)
    c = sqlite3.connect(TMP)
    r = c.execute("SELECT exch_feed_time,received_at,quote_age_s,ltp "
                  "FROM quote_snapshot").fetchone()
    check("quote row still inserted", r[3], 5)
    check("both raw timestamps preserved", (r[0], r[1]),
          ("17-Aug-2026 15:15:57", "2026-08-17T15:15:59.162813+05:30"))
    check("age NULL - no offset assumed", r[2], None)
    c.close()

    # every format the production paths actually emit
    for label, s, want_tz in (
            ("broker", "17-Aug-2026 15:15:57", False),
            ("broker+micros", "17-Aug-2026 15:15:57.123456", False),
            ("iso naive", "2026-08-17T15:15:59.162813", False),
            ("iso aware", "2026-08-17T15:15:59.162813+05:30", True),
            ("space sep", "2026-08-17 15:15:59", False)):
        d = tm._parse_ts(s)
        check(f"{label} parses", d is not None, True)
        check(f"{label} tz-aware={want_tz}", d.tzinfo is not None, want_tz)
    check("date-only does NOT become a midnight age",
          tm._parse_ts("17-Aug-2026"), None)
    check("epoch int rejected", tm._parse_ts(1755432957), None)


def _iso_only(s):
    """What the pre-fix code did, for contrast."""
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def test_candidate_snapshot_has_production_call_site():
    """P0-D F-2: candidate_snapshot was empty for Monday's entire run."""
    print("\n[19] emit_candidate is wired to a real production event")
    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    check("emit_candidate called in production",
          "telemetry.emit_candidate(" in src, True)
    # it must sit in the ranking-selection block, after position management
    i_call = src.index("telemetry.emit_candidate(")
    i_sel = src.index("picks, rejections = ranking.select(")
    i_exit = src.index("for pos in positions:")
    check("emitted at the ranking-selection event", i_call > i_sel, True)
    check("which is after position management", i_sel > i_exit, True)
    check("no trading branch reads its return value",
          "= telemetry.emit_candidate(" in src, False)

    # and it records what it is given
    fresh()
    tm.new_cycle()
    cid = tm.emit_candidate(
        "BANKNIFTY", "BEAR",
        {"token": "12345", "symbol": "BANKNIFTY25AUG2657300PE",
         "strike": 57300.0, "expiry": date(2026, 8, 25)},
        quote_snapshot_id="q1", score=0.61, rank=2, would_trade=1,
        tier="confluence", today=date(2026, 8, 17))
    check("returns an id", isinstance(cid, str), True)
    c = sqlite3.connect(TMP)
    r = c.execute("SELECT symbol,direction,token,trading_symbol,strike,dte,"
                  "score,rank,would_trade,tier FROM candidate_snapshot").fetchone()
    check("symbol", r[0], "BANKNIFTY")
    check("direction", r[1], "BEAR")
    check("token", r[2], "12345")
    check("trading_symbol", r[3], "BANKNIFTY25AUG2657300PE")
    check("strike", r[4], 57300.0)
    check("dte derived from expiry - today", r[5], 8)
    check("score", r[6], 0.61)
    check("rank", r[7], 2)
    check("would_trade", r[8], 1)
    c.close()


def test_entry_gate_disambiguates_zero_candidate_cycles():
    """P0-W: a cycle with zero candidate rows was ambiguous.

    Either the gate was CLOSED (candidates were never evaluated, so no
    entry was possible) or it was OPEN and the set was genuinely EMPTY.
    Portfolio replay must treat those oppositely. 17 such cycles on
    2026-08-18, 58 on 2026-08-19.
    """
    print("\n[20] ENTRY_GATE records the already-computed gate state")
    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    check("ENTRY_GATE emitted in production", '"ENTRY_GATE"' in src, True)
    # must fire on EVERY cycle that reaches the gate - not inside `if in_window`
    i_gate = src.index('"ENTRY_GATE"')
    i_win = src.index("candidates = []   # dicts")
    i_quotes = src.index("# ---- ONE batched live-quote call")
    check("emitted after candidate construction", i_gate > i_win, True)
    check("emitted before the quote fetch", i_gate < i_quotes, True)
    # 4-space indent means module-level-of-function, i.e. NOT inside `if in_window`
    gate_line = [l for l in src.splitlines()
                 if l.rstrip().endswith('"ENTRY_GATE",')][0]
    check("emitted at function scope, not inside a conditional block",
          len(gate_line) - len(gate_line.lstrip()), 8)
    check("return value unused", "= telemetry.emit_decision" in src, False)
    # every constituent it records must be a value production already computed
    for v in ("in_window", "signals_fresh", "signals_available",
              "entry_allowed", "len(candidates)"):
        check(f"records already-computed {v}", v in src, True)
    check("no new evaluation added for telemetry",
          src.count("if in_window:"), 1)

    # the recorded string must be machine-parseable both ways
    fresh()
    tm.new_cycle()
    tm.emit_decision("ENTRY_GATE", reason="in_window=0 time=1 fresh=1 "
                     "signals=1 master=1 allowed=0 candidates=0")
    tm.emit_decision("ENTRY_GATE", reason="in_window=1 time=1 fresh=1 "
                     "signals=1 master=1 allowed=1 candidates=7")
    c = sqlite3.connect(TMP)
    rows = [r[0] for r in c.execute(
        "SELECT reason FROM decision WHERE action='ENTRY_GATE' ORDER BY rowid")]
    c.close()
    check("both rows stored", len(rows), 2)
    closed = dict(kv.split("=") for kv in rows[0].split())
    openr = dict(kv.split("=") for kv in rows[1].split())
    check("closed gate parses", closed["in_window"], "0")
    check("closed gate names the blocker", closed["allowed"], "0")
    check("open gate parses", openr["in_window"], "1")
    check("open gate carries candidate count", openr["candidates"], "7")
    check("zero candidates + open gate is distinguishable",
          (closed["in_window"], closed["candidates"]) !=
          (openr["in_window"], openr["candidates"]), True)


def test_bar_status_and_signal_age():
    print("\n[4] completed vs forming bar detection")
    fresh()
    tm.new_cycle()
    import pandas as pd
    base = datetime(2026, 8, 14, 10, 0, 0)
    # observed 6 min after the bar opened -> that bar has closed
    idx = pd.date_range(base, periods=3, freq="5min")
    df = pd.DataFrame({"Close": [1, 2, 3], "Volume": [1, 1, 1]}, index=idx)
    tm.emit_signal("X", base.isoformat(),
                   (idx[-1] + timedelta(minutes=6)).isoformat(), df=df)
    tm.emit_signal("Y", base.isoformat(),
                   (idx[-1] + timedelta(minutes=1)).isoformat(), df=df)
    c = sqlite3.connect(TMP)
    rows = dict(c.execute("SELECT symbol,bar_status FROM signal_snapshot"))
    check("observed after bar close -> COMPLETED", rows["X"], "COMPLETED")
    check("observed mid-bar -> FORMING", rows["Y"], "FORMING")
    r = c.execute("SELECT bar_ts,bar_age_s FROM signal_snapshot "
                  "WHERE symbol='X'").fetchone()
    check("bar_ts recorded", r[0] is not None, True)
    check("bar age computed", round(r[1]), 360)
    c.close()


def test_missing_bar_timestamp():
    print("\n[5] missing bar timestamp -> UNKNOWN, not fabricated")
    fresh()
    tm.new_cycle()
    tm.emit_signal("Z", None, None, df=None, direction="BULL")
    c = sqlite3.connect(TMP)
    r = c.execute("SELECT bar_ts,bar_status,bar_age_s,direction "
                  "FROM signal_snapshot").fetchone()
    check("bar_ts NULL", r[0], None)
    check("status UNKNOWN", r[1], "UNKNOWN")
    check("age NULL", r[2], None)
    check("production direction preserved verbatim", r[3], "BULL")
    c.close()


def test_completed_bar_observation_is_parallel_only():
    print("\n[6] completed-bar direction recorded but never returned")
    fresh()
    tm.new_cycle()
    import pandas as pd
    idx = pd.date_range(datetime(2026, 8, 14, 10, 0), periods=4, freq="5min")
    df = pd.DataFrame({"Close": [1, 2, 3, 99], "Volume": [1, 1, 1, 1]},
                      index=idx)
    calls = []

    def fake_dir(d):
        calls.append(len(d))
        return "BULL" if len(d) == 4 else "BEAR"

    ret = tm.emit_signal("W", None, None, df=df, direction="BULL",
                         direction_fn=fake_dir)
    check("emit_signal returns an id, not a direction", isinstance(ret, str), True)
    check("completed-bar fn saw the series MINUS the last bar", calls, [3])
    c = sqlite3.connect(TMP)
    r = c.execute("SELECT direction,direction_completed_bar "
                  "FROM signal_snapshot").fetchone()
    check("production direction unchanged", r[0], "BULL")
    check("completed-bar direction recorded separately", r[1], "BEAR")
    c.close()


def test_session_validation():
    print("\n[7] session validity classification")
    fresh()
    tm.new_cycle()
    import pandas as pd
    idx = pd.date_range(datetime(2026, 8, 14, 10, 0), periods=2, freq="5min")
    df = pd.DataFrame({"Close": [1, 2], "Volume": [1, 1]}, index=idx)
    tm.emit_signal("A", None, None, df=df, session_date="2026-08-14")
    tm.emit_signal("B", None, None, df=df, session_date="2026-08-13")
    c = sqlite3.connect(TMP)
    rows = dict(c.execute("SELECT symbol,session_status FROM signal_snapshot"))
    check("bar matches session -> VALID", rows["A"], "VALID")
    check("bar from another session -> flagged", rows["B"], "STALE_OR_AMBIGUOUS")
    c.close()


def test_fail_open_on_broken_store():
    print("\n[8] FAIL-OPEN: broken telemetry never raises")
    fresh()
    tm.new_cycle()
    real = tm._conn
    class Exploding:
        def execute(self, *a, **k): raise RuntimeError("disk gone")
        def commit(self): raise RuntimeError("disk gone")
    tm._conn = Exploding()
    ok = True
    try:
        tm.emit_signal("A", None, None)
        tm.emit_quotes({"1": {"ltp": 1}}, None, None)
        tm.emit_decision("ENTRY")
        tm.emit_position("1", "X")
        tm.emit_exit("1", "X", "Initial stop")
        tm.emit_post_exit("1", "X", None, {})
        tm.close_cycle()
    except Exception as e:
        ok = False
        print(f"      raised: {type(e).__name__}: {e}")
    tm._conn = real
    check("no exception escaped to the caller", ok, True)


def test_disabled_telemetry_is_inert():
    print("\n[9] disabled telemetry writes nothing and returns None")
    fresh()
    tm.disable()
    check("new_cycle returns None", tm.new_cycle(), None)
    check("emit_signal returns None", tm.emit_signal("A", None, None), None)
    check("emit_quotes returns None", tm.emit_quotes({"1": {}}, None, None), None)
    tm.enable()
    c = sqlite3.connect(TMP)
    check("nothing persisted while disabled",
          c.execute("SELECT COUNT(*) FROM signal_snapshot").fetchone()[0], 0)
    c.close()


def test_exit_captures_high_water():
    print("\n[10] exit snapshot preserves high-water (row is deleted upstream)")
    fresh()
    tm.new_cycle()
    tm.emit_exit("55", "FOO25AUG26CE", "Trailing stop", exit_price=88.0,
                 high_water_at_exit=100.0, peak_source="POLL", pnl=-1200.0)
    c = sqlite3.connect(TMP)
    r = c.execute("SELECT exit_reason,high_water_at_exit,pnl "
                  "FROM exit_snapshot").fetchone()
    check("reason stored", r[0], "Trailing stop")
    check("high water preserved", r[1], 100.0)
    check("pnl stored", r[2], -1200.0)
    c.close()


def test_post_exit_path_captured():
    print("\n[11] post-exit path is recordable (unblocks S-20)")
    fresh()
    tm.new_cycle()
    tm.emit_post_exit("77", "BAR25AUG26PE", "2026-08-14T11:00:00",
                      {"ltp": 12.5, "bid": 12.4, "ask": 12.6})
    c = sqlite3.connect(TMP)
    r = c.execute("SELECT token,ltp,bid,ask FROM post_exit_path").fetchone()
    check("token recorded", r[0], "77")
    check("post-exit ltp recorded", r[1], 12.5)
    c.close()


def test_production_db_untouched():
    print("\n[12] telemetry never opens the production books")
    fresh()
    prod = HERE / "options_trades.db"
    before = prod.stat().st_mtime if prod.exists() else None
    tm.new_cycle()
    tm.emit_signal("A", None, None)
    tm.emit_decision("ENTRY")
    tm.close_cycle()
    after = prod.stat().st_mtime if prod.exists() else None
    check("options_trades.db mtime unchanged", before, after)
    check("telemetry store is a separate file",
          str(TMP) != str(prod), True)


def test_t1_simulation_context_suppresses_sink():
    """T-1: simulated actions must not reach the sink at all."""
    print("\n[13] T-1 simulation context suppresses recording")
    fresh()
    tm.new_cycle()
    tm.emit_exit("1", "REAL-BEFORE", "Initial stop", pnl=-100.0)
    with tm.simulation():
        tm.emit_exit("2", "_SELFCHECK", "selfcheck", pnl=0.0)
        tm.emit_decision("ENTRY", trading_symbol="_SELFCHECK")
        tm.emit_position("2", "_SELFCHECK")
        tm.emit_signal("_SELFCHECK", None, None)
    tm.emit_exit("3", "REAL-AFTER", "Trailing stop", pnl=250.0)
    c = sqlite3.connect(TMP)
    syms = [r[0] for r in c.execute("SELECT trading_symbol FROM exit_snapshot")]
    check("real exits recorded either side", syms, ["REAL-BEFORE", "REAL-AFTER"])
    check("zero _SELFCHECK exit rows",
          sum(1 for s in syms if s == "_SELFCHECK"), 0)
    check("no simulated decision rows",
          c.execute("SELECT COUNT(*) FROM decision").fetchone()[0], 0)
    check("no simulated position rows",
          c.execute("SELECT COUNT(*) FROM position_snapshot").fetchone()[0], 0)
    check("no simulated signal rows",
          c.execute("SELECT COUNT(*) FROM signal_snapshot").fetchone()[0], 0)
    check("flag restored after context", tm.is_simulating(), False)
    c.close()


def test_t1_context_restores_on_exception():
    print("\n[14] T-1 context is exception-safe and re-entrant")
    fresh()
    try:
        with tm.simulation():
            with tm.simulation():
                check("nested still suppressing", tm.is_simulating(), True)
            check("inner exit keeps outer suppression", tm.is_simulating(), True)
            raise RuntimeError("probe blew up")
    except RuntimeError:
        pass
    check("flag restored after exception", tm.is_simulating(), False)
    tm.emit_exit("9", "AFTER", "Initial stop")
    c = sqlite3.connect(TMP)
    check("recording resumed", c.execute(
        "SELECT COUNT(*) FROM exit_snapshot").fetchone()[0], 1)
    c.close()


def test_t2_no_store_without_explicit_init():
    """T-2: emitting without init() must not create a file."""
    print("\n[15] T-2 no store is created without explicit init()")
    tm.shutdown()
    tm._initialised = False
    tm._warned_uninitialised = False
    stray = HERE / "observability.db"
    existed = stray.exists()
    tm.new_cycle()
    tm.emit_signal("A", None, None)
    tm.emit_exit("1", "X", "Initial stop")
    check("no observability.db materialised",
          stray.exists(), existed)
    check("emits return None when uninitialised", tm.emit_decision("ENTRY"), None)


def test_t2_explicit_init_still_works():
    print("\n[16] T-2 explicit init still creates and uses a store")
    fresh()
    tm.new_cycle()
    tm.emit_exit("1", "X", "Trailing stop", pnl=10.0)
    check("store exists after explicit init", TMP.exists(), True)
    c = sqlite3.connect(TMP)
    check("row written", c.execute(
        "SELECT COUNT(*) FROM exit_snapshot").fetchone()[0], 1)
    c.close()


def test_t2_in_memory_store():
    print("\n[17] T-2 in-memory store leaves no artifact")
    tm.reset_for_test(":memory:")
    tm.new_cycle()
    tm.emit_exit("1", "MEM", "Initial stop", pnl=-5.0)
    n = tm._conn.execute("SELECT COUNT(*) FROM exit_snapshot").fetchone()[0]
    check("in-memory recording works", n, 1)
    check("no file created", (HERE / ":memory:").exists(), False)
    tm.shutdown()


if __name__ == "__main__":
    for fn in (test_cycle_identity, test_timestamp_ordering,
               test_quote_age_null_when_no_feed_time,
               test_bar_status_and_signal_age, test_missing_bar_timestamp,
               test_completed_bar_observation_is_parallel_only,
               test_session_validation, test_fail_open_on_broken_store,
               test_disabled_telemetry_is_inert, test_exit_captures_high_water,
               test_post_exit_path_captured, test_production_db_untouched,
               test_t1_simulation_context_suppresses_sink,
               test_t1_context_restores_on_exception,
               test_t2_no_store_without_explicit_init,
               test_t2_explicit_init_still_works,
               test_t2_in_memory_store,
               test_quote_age_parses_broker_timestamp_format,
               test_candidate_snapshot_has_production_call_site,
               test_entry_gate_disambiguates_zero_candidate_cycles):
        fn()
    tm.shutdown()
    if TMP.exists():
        try:
            TMP.unlink()
        except Exception:
            pass
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All telemetry tests passed.")
