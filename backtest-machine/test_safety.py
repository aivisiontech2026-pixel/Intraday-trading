"""Safety-layer tests: daily-loss latch, supervisor, risk isolation.

    python test_safety.py

Covers validation criteria A-H and the adversarial attempts in R.
The important properties here are ADVERSARIAL: the tests try to bypass
the latch, cross-contaminate the two books, and clear the halt by
restarting or by recovering P&L.
"""

import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import safety_supervisor as sup
import config_contract as cc

FAILURES = []
TODAY = date(2026, 8, 17)
LIMIT_CAPITAL = 100_000
LIMIT_PCT = 2.0          # -> Rs.2,000, the declared contract


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


def opt_db(trades, day=TODAY):
    """In-memory options ledger. trades = [(pnl, minute), ...]"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE options_trades(id INTEGER PRIMARY KEY, pnl REAL, "
              "exit_time TEXT)")
    for i, (p, mins) in enumerate(trades):
        ts = (datetime.combine(day, datetime.min.time())
              + timedelta(minutes=mins)).isoformat()
        c.execute("INSERT INTO options_trades(pnl,exit_time) VALUES(?,?)",
                  (p, ts))
    c.commit()
    return c


def stock_db(trades, day=TODAY):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE trades(id INTEGER PRIMARY KEY, pnl REAL, "
              "exit_time TEXT)")
    for p, mins in trades:
        ts = (datetime.combine(day, datetime.min.time())
              + timedelta(minutes=mins)).isoformat()
        c.execute("INSERT INTO trades(pnl,exit_time) VALUES(?,?)", (p, ts))
    c.commit()
    return c


def perm(conn, book):
    return sup.entry_permission(conn, book, TODAY, LIMIT_CAPITAL, LIMIT_PCT)


# ------------------------------------------------------------------ A-H ---
def test_limit_from_contract():
    print("\n[1] limit derives from the DECLARED contract (not invented)")
    check("limit = 2% of 100,000", sup.daily_loss_limit(100_000, 2.0), 2000.0)
    c = cc.Contract()
    check("config yields the same limit", c.daily_loss_limit(), 2000.0)
    check("capital unchanged", c.capital, 100000.0)
    check("percent unchanged", c.max_daily_loss_percent, 2.0)


def test_a_enforced_per_book():
    print("\n[2] A: enforced independently per book")
    o = opt_db([(-2100.0, 600)])
    ok, st = perm(o, sup.OPTIONS_BOOK)
    check("options halted at -2,100", ok, False)
    check("reason", st["reason"], sup.HALT_DAILY_LOSS)
    s = stock_db([(50.0, 600)])
    ok2, st2 = perm(s, sup.STOCK_BOOK)
    check("stock unaffected", ok2, True)


def test_b_options_loss_does_not_halt_stock():
    print("\n[3] B: options loss must NOT halt stock entries")
    o = opt_db([(-9000.0, 570)])
    s = stock_db([(-100.0, 570)])
    check("options halted", perm(o, sup.OPTIONS_BOOK)[0], False)
    check("stock still permitted", perm(s, sup.STOCK_BOOK)[0], True)


def test_c_stock_loss_does_not_halt_options():
    print("\n[4] C: stock loss must NOT halt options entries")
    o = opt_db([(-100.0, 570)])
    s = stock_db([(-5000.0, 570)])
    check("stock halted", perm(s, sup.STOCK_BOOK)[0], False)
    check("options still permitted", perm(o, sup.OPTIONS_BOOK)[0], True)


def test_d_latch_holds_within_session():
    print("\n[5] D: once halted, the book cannot re-enter this session")
    o = opt_db([(-2500.0, 585)])
    check("halted", perm(o, sup.OPTIONS_BOOK)[0], False)
    # re-evaluate repeatedly, as successive cycles would
    for _ in range(5):
        if perm(o, sup.OPTIONS_BOOK)[0]:
            FAILURES.append("latch released on re-evaluation")
    check("still halted after 5 re-evaluations",
          perm(o, sup.OPTIONS_BOOK)[0], False)


def test_e_recovery_cannot_clear_latch():
    print("\n[6] E: later profit must NOT reopen entry permission")
    # -2,050 at 09:45, then +250 -> running back to -1,800
    o = opt_db([(-2050.0, 585), (250.0, 720)])
    run, low = sup.daily_realized(o, sup.OPTIONS_BOOK, TODAY)
    check("running total recovered to -1,800", run, -1800.0)
    check("low-water still -2,050", low, -2050.0)
    ok, st = perm(o, sup.OPTIONS_BOOK)
    check("LATCH HOLDS despite recovery", ok, False)
    check("reason still daily loss", st["reason"], sup.HALT_DAILY_LOSS)


def test_f_restart_cannot_clear_latch():
    print("\n[7] F: restart must not silently clear the latch")
    trades = [(-2050.0, 585), (250.0, 720)]
    a = opt_db(trades)
    before = perm(a, sup.OPTIONS_BOOK)[0]
    a.close()                       # simulate process death
    b = opt_db(trades)              # fresh process, same ledger
    after = perm(b, sup.OPTIONS_BOOK)[0]
    check("halted before restart", before, False)
    check("STILL halted after restart", after, False)
    check("latch is derived from the ledger, not stored state",
          sup.daily_realized(b, sup.OPTIONS_BOOK, TODAY)[1], -2050.0)


def test_h_next_session_resets():
    print("\n[8] H: next session resets the latch")
    o = opt_db([(-3000.0, 600)], day=TODAY)
    check("halted today", perm(o, sup.OPTIONS_BOOK)[0], False)
    tomorrow = TODAY + timedelta(days=1)
    ok, st = sup.entry_permission(o, sup.OPTIONS_BOOK, tomorrow,
                                  LIMIT_CAPITAL, LIMIT_PCT)
    check("permitted next session", ok, True)
    check("next session realized = 0", st["realized_today"], 0.0)


def test_boundary_exact():
    print("\n[9] boundary: exactly -2,000 halts (>= limit)")
    check("-1,999.99 permitted", perm(opt_db([(-1999.99, 600)]),
                                     sup.OPTIONS_BOOK)[0], True)
    check("-2,000.00 halted", perm(opt_db([(-2000.0, 600)]),
                                   sup.OPTIONS_BOOK)[0], False)
    check("-2,000.01 halted", perm(opt_db([(-2000.01, 600)]),
                                   sup.OPTIONS_BOOK)[0], False)


# ------------------------------------------------------ adversarial (R) ---
def test_r_fail_safe_on_broken_ledger():
    print("\n[10] R: unreadable ledger must FAIL SAFE (block)")
    c = sqlite3.connect(":memory:")          # no options_trades table
    ok, st = perm(c, sup.OPTIONS_BOOK)
    check("blocked, not permitted", ok, False)
    check("reason names unavailability", st["reason"], sup.BLOCK_UNAVAILABLE)


def test_r_unknown_book_fails_safe():
    print("\n[11] R: unknown book fails safe")
    ok, st = perm(opt_db([]), "not_a_book")
    check("blocked", ok, False)
    check("reason", st["reason"], sup.BLOCK_UNAVAILABLE)


def test_r_missing_contract_fails_safe():
    print("\n[12] R: absent limit fails safe rather than defaulting")
    ok, st = sup.entry_permission(opt_db([]), sup.OPTIONS_BOOK, TODAY,
                                  100_000, None)
    check("no percent -> blocked", ok, False)
    ok2, _ = sup.entry_permission(opt_db([]), sup.OPTIONS_BOOK, TODAY,
                                  None, 2.0)
    check("no capital -> blocked", ok2, False)


def test_r_other_sessions_ignored():
    print("\n[13] R: yesterday's losses cannot halt today")
    y = TODAY - timedelta(days=1)
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE options_trades(id INTEGER PRIMARY KEY, pnl REAL, "
              "exit_time TEXT)")
    c.execute("INSERT INTO options_trades(pnl,exit_time) VALUES(?,?)",
              (-50000.0, datetime.combine(y, datetime.min.time()).isoformat()))
    c.commit()
    ok, st = perm(c, sup.OPTIONS_BOOK)
    check("today permitted", ok, True)
    check("today realized = 0", st["realized_today"], 0.0)


def test_r_supervisor_cannot_be_bypassed_by_strategy():
    print("\n[14] R: strategy cannot flip the decision")
    import options_trader as ot
    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    # entry_allowed must be consumed by in_window and never reassigned
    check("in_window consumes entry_allowed",
          "and entry_allowed" in src, True)
    # assigned exactly once, by the supervisor call - never re-bound after
    import re
    assigns = re.findall(r"^\s*entry_allowed\s*(?:,[^=]*)?=", src, re.M)
    check("entry_allowed assigned exactly once", len(assigns), 1)
    check("that assignment is the supervisor call",
          "safety_state" in assigns[0], True)
    check("supervisor module is imported", "import safety_supervisor" in src, True)


def test_g_halt_does_not_touch_exits():
    print("\n[15] G: halt must not disable exit/risk management")
    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    i_gate = src.index("entry_allowed, safety_state =")
    i_posloop = src.index("for pos in positions:")
    i_entryloop = src.index("for cand in entry_list:")
    check("position-management loop is NOT inside the entry gate",
          i_posloop > i_gate, True)
    check("entry loop comes after position management",
          i_entryloop > i_posloop, True)
    for fn in ("close_option(", "square_off_net("):
        check(f"{fn} still present", fn in src, True)
    st_src = (HERE / "simple_trader.py").read_text(encoding="utf-8")
    check("stock exit loop after the gate",
          st_src.index("# ---- EXIT:") >
          st_src.index("entry_allowed, safety_state ="), True)


def test_s05_entry_deps_do_not_gate_risk_management():
    print("\n[16] S-05: entry-side failures no longer return early")
    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    check("instrument-master failure no longer returns",
          "square_off_net(conn, log, now_min, \"instrument master unavailable\")"
          in src, False)
    check("underlying-data failure no longer returns",
          "square_off_net(conn, log, now_min, \"no underlying data\")"
          in src, False)
    check("master gates candidates instead", "bool(master)" in src, True)
    check("signals gate candidates instead", "signals_available" in src, True)
    check("Angel One failure still returns (no prices = cannot manage)",
          "square_off_net(conn, log, now_min, \"Angel One unavailable\")"
          in src, True)


def test_frozen_untouched():
    print("\n[17] frozen issues remain untouched")
    import options_trader as ot, json
    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    check("S-01 5-day scalar VWAP unchanged",
          '(df["Close"] * df["Volume"]).sum() / vol' in src, True)
    check("S-01 mean-close fallback unchanged",
          'vwap = df["Close"].mean()' in src, True)
    check("S-09 .iloc[-1] unchanged", 'last = df["Close"].iloc[-1]' in src, True)
    c = json.loads((HERE / "intraday_config.json").read_text())
    check("ranking.mode still shadow", c["ranking"]["mode"], "shadow")
    check("no_same_cycle_reentry still True",
          c["experiments"]["no_same_cycle_reentry"], True)
    check("MAX_POSITIONS", ot.MAX_POSITIONS, 4)
    check("INITIAL_STOP_PCT", ot.INITIAL_STOP_PCT, -0.15)
    check("TRAIL_PCT", ot.TRAIL_PCT, 0.12)


def test_s02_ema_parity():
    print("\n[18] S-02: EMA now adjust=False, matching the backtest")
    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    check("get_direction uses adjust=False",
          'ewm(span=9, adjust=False)' in src, True)
    check("no bare ewm(span=9) remains", 'ewm(span=9).mean()' in src, False)
    check("no bare ewm(span=21) remains", 'ewm(span=21).mean()' in src, False)
    import backtest
    import inspect
    check("backtest still adjust=False",
          "adjust=False" in inspect.getsource(backtest.ema), True)


if __name__ == "__main__":
    for fn in (test_limit_from_contract, test_a_enforced_per_book,
               test_b_options_loss_does_not_halt_stock,
               test_c_stock_loss_does_not_halt_options,
               test_d_latch_holds_within_session,
               test_e_recovery_cannot_clear_latch,
               test_f_restart_cannot_clear_latch,
               test_h_next_session_resets, test_boundary_exact,
               test_r_fail_safe_on_broken_ledger,
               test_r_unknown_book_fails_safe,
               test_r_missing_contract_fails_safe,
               test_r_other_sessions_ignored,
               test_r_supervisor_cannot_be_bypassed_by_strategy,
               test_g_halt_does_not_touch_exits,
               test_s05_entry_deps_do_not_gate_risk_management,
               test_frozen_untouched, test_s02_ema_parity):
        fn()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All safety tests passed.")
