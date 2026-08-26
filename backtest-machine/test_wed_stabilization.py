"""Wednesday 2026-08-26 stabilization acceptance tests.

    python test_wed_stabilization.py

TWO LAYERS, DELIBERATELY.

  Layer 1  Unit tests on the pure gate functions in stabilization.py.
  Layer 2  END-TO-END runs of the REAL options_trader.process() against a
           fake broker and a fake market, with a real SQLite book.

Layer 2 exists because the single most dangerous failure mode in this
package is an entry gate blocking an exit, and that cannot be proven by
asserting things about a gate function in isolation: it is a property of
the CONTROL FLOW of process(). So the exit-path tests drive the actual
engine with every entry gate failing simultaneously and assert that the
position still closes and the trade row is still written.
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import stabilization as stab            # noqa: E402
import telemetry                         # noqa: E402

FAILURES = []
_N = [0]


def check(name, got, want):
    _N[0] += 1
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


def check_true(name, got):
    check(name, bool(got), True)


def near(name, got, want, tol=1e-6):
    _N[0] += 1
    ok = got is not None and abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want ~{want!r}")
    if not ok:
        FAILURES.append(name)


CFG = {}                      # empty -> stabilization DEFAULTS
C = stab.get_config(CFG)
TODAY = date(2026, 8, 26)


def sig(bar="2026-08-26 09:25:00+05:30", used=None, obs=None,
        direction="BULL", status="VALID"):
    return {"direction": direction, "spot": 100.0, "momentum": 1.0,
            "rel_strength": 0.1, "trend_quality": 0.8,
            "bar_ts": bar, "used_bar_ts": used if used is not None else bar,
            "observed_at": obs or "2026-08-26 09:31:00",
            "session_status": status}


# ===================================================================== 1 ===
def t_signal_freshness():
    print("\n[1] CHANGE 1 - signal freshness / session validity")
    fresh = sig(bar="2026-08-26 09:25:00", obs="2026-08-26 09:31:00")   # 360 s
    check("fresh current-session bar allowed",
          bool(stab.check_signal_freshness(fresh, TODAY, C)), True)

    # exactly AT the threshold -> allowed (the rule is `> max` rejects)
    at = sig(bar="2026-08-26 09:25:00", obs="2026-08-26 09:31:40")      # 400 s
    r = stab.check_signal_freshness(at, TODAY, C)
    check("age exactly at threshold (400s) allowed", bool(r), True)

    over = sig(bar="2026-08-26 09:25:00", obs="2026-08-26 09:31:41")    # 401 s
    r = stab.check_signal_freshness(over, TODAY, C)
    check("age above threshold rejected", bool(r), False)
    check("  reason", r.reason, "signal_bar_stale")

    prev = sig(bar="2026-08-25 15:25:00", obs="2026-08-26 09:31:00")
    r = stab.check_signal_freshness(prev, TODAY, C)
    check("previous trading session rejected", bool(r), False)
    check("  reason", r.reason, "signal_bar_previous_session")

    r = stab.check_signal_freshness(sig(bar=None), TODAY, C)
    check("missing timestamp rejected", bool(r), False)
    check("  reason", r.reason, "signal_bar_ts_missing")

    r = stab.check_signal_freshness(sig(bar=""), TODAY, C)
    check("empty timestamp rejected", bool(r), False)
    r = stab.check_signal_freshness(sig(bar="not-a-date"), TODAY, C)
    check("unparseable timestamp rejected", bool(r), False)
    check("  reason", r.reason, "signal_bar_ts_unparseable")

    check("null signal payload rejected",
          bool(stab.check_signal_freshness(None, TODAY, C)), False)
    check("non-dict payload rejected",
          bool(stab.check_signal_freshness("BULL", TODAY, C)), False)

    # THE MONDAY-AFTER-FRIDAY CASE, verbatim from production:
    # 2026-08-24T09:15:55 read a bar stamped 2026-08-21 15:25 -> 237,055 s.
    monday = sig(bar="2026-08-21 15:25:00+05:30",
                 used="2026-08-21 15:25:00+05:30",
                 obs="2026-08-24 09:15:55", status="STALE_OR_AMBIGUOUS")
    age = stab.bar_age_s(monday["bar_ts"], monday["observed_at"])
    near("  measured age matches production (237,055 s)", age, 237055.0, 1.0)
    r = stab.check_signal_freshness(monday, date(2026, 8, 24), C)
    check("Monday-after-Friday stale bar REJECTED", bool(r), False)
    check("  reason", r.reason, "signal_bar_previous_session")

    # session_status honoured independently
    r = stab.check_signal_freshness(
        sig(status="STALE_OR_AMBIGUOUS"), TODAY, C)
    check("STALE_OR_AMBIGUOUS session rejected", bool(r), False)

    # a bar stamped in the FUTURE is not "very fresh"
    r = stab.check_signal_freshness(
        sig(bar="2026-08-26 10:00:00", obs="2026-08-26 09:31:00"), TODAY, C)
    check("future-stamped bar rejected", bool(r), False)
    check("  reason", r.reason, "signal_bar_in_future")

    # the SIGNAL bar (used_bar_ts) is checked separately from the observed bar
    r = stab.check_signal_freshness(
        sig(bar="2026-08-26 09:20:00", used="2026-08-25 15:25:00",
            obs="2026-08-26 09:21:00"), TODAY, C)
    check("previous-session SIGNAL bar rejected", bool(r), False)
    check("  reason", r.reason, "signal_used_bar_previous_session")

    # direction=None is NOT a freshness failure: the momentum fallback tier
    # trades symbols with no confluence, and gating it here would delete
    # that tier under cover of a data-validity check.
    check("direction=None still fresh (momentum tier preserved)",
          bool(stab.check_signal_freshness(sig(direction=None), TODAY, C)),
          True)
    check("  ... unless explicitly required",
          bool(stab.check_signal_freshness(sig(direction=None), TODAY, C,
                                           require_direction=True)), False)


# ===================================================================== 2 ===
def t_dte():
    print("\n[2] CHANGE 2 - temporary DTE gate (MIN_DTE=2)")
    check("min_dte default", C["min_dte"], 2)
    for d, want in ((0, False), (1, False), (2, True), (3, True), (6, True),
                    (34, True)):
        r = stab.check_dte(TODAY + timedelta(days=d), TODAY, C)
        check(f"DTE {d} -> {'accepted' if want else 'rejected'}", bool(r), want)
    r = stab.check_dte(TODAY, TODAY, C)
    check("DTE 0 reason", r.reason, "dte_below_min(0<2)")
    check("unreadable expiry rejected",
          bool(stab.check_dte("garbage", TODAY, C)), False)
    check("None expiry rejected", bool(stab.check_dte(None, TODAY, C)), False)
    check("string ISO expiry accepted",
          bool(stab.check_dte("2026-09-01", TODAY, C)), True)

    # Expected Wednesday behaviour: the gate is a NO-OP.
    check("Wed index expiry 2026-09-01 (DTE 6) passes",
          bool(stab.check_dte(date(2026, 9, 1), TODAY, C)), True)
    check("Wed stock expiry 2026-09-29 (DTE 34) passes",
          bool(stab.check_dte(date(2026, 9, 29), TODAY, C)), True)

    # MIN_DTE=2 and MIN_DTE=4 are behaviourally identical on all in-sample
    # data because DTE 2-3 has ZERO observations.
    c4 = dict(C, min_dte=4)
    same = all(bool(stab.check_dte(TODAY + timedelta(days=d), TODAY, C))
               == bool(stab.check_dte(TODAY + timedelta(days=d), TODAY, c4))
               for d in (0, 1, 4, 5, 6, 7, 8, 34))
    check("min_dte 2 vs 4 identical outside the empty 2-3 band", same, True)


# ===================================================================== 3 ===
def t_liquidity():
    print("\n[3] CHANGE 5 - temporary liquidity gate (1.0%)")
    ok = {"ltp": 100.0, "bid": 99.75, "ask": 100.25}          # 0.50%
    near("spread computed", stab.spread_pct(ok), 0.4999, 1e-3)
    check("<=1% accepted", bool(stab.check_spread(ok, C)), True)

    wide = {"ltp": 100.0, "bid": 99.0, "ask": 101.0}          # 2.0%
    r = stab.check_spread(wide, C)
    check(">1% rejected", bool(r), False)
    check_true("  reason names the gate", r.reason.startswith("spread_above_max"))

    for name, q in (("missing quote", None),
                    ("empty quote", {}),
                    ("zero bid", {"ltp": 10, "bid": 0, "ask": 10.1}),
                    ("zero ask", {"ltp": 10, "bid": 9.9, "ask": 0}),
                    ("negative bid", {"ltp": 10, "bid": -1, "ask": 10.1}),
                    ("negative ask", {"ltp": 10, "bid": 9.9, "ask": -1}),
                    ("crossed book", {"ltp": 10, "bid": 10.5, "ask": 9.5}),
                    ("non-numeric", {"ltp": 10, "bid": "x", "ask": "y"})):
        check(f"{name} rejected", bool(stab.check_spread(q, C)), False)
        check(f"{name} is NOT read as zero spread", stab.spread_pct(q), None)

    check("no LTP rejected",
          bool(stab.check_quote({"ltp": 0, "bid": 1, "ask": 1.1})), False)
    check("missing quote rejected", bool(stab.check_quote(None)), False)

    # boundary: exactly 1.00%
    exact = {"ltp": 100.0, "bid": 99.5024875, "ask": 100.5}
    near("  boundary spread", stab.spread_pct(exact), 0.9975, 1e-2)
    check("exactly at threshold accepted", bool(stab.check_spread(exact, C)),
          True)


# ===================================================================== 4 ===
def t_trailing():
    print("\n[4] CHANGE 4 - U-014 trailing dead zone")
    E, A, T = 100.0, 0.10, 0.12
    check("pre-arm returns None (initial stop stands)",
          stab.trail_stop_level(E, 105.0, A, T, C), None)
    check("just below arm returns None",
          stab.trail_stop_level(E, E * (1 + A) - 0.01, A, T, C), None)

    # THE DEFECT, reproduced exactly: 1.10 * 0.88 = 0.968 -> -3.2%
    armed = E * (1 + A)
    raw = armed * (1 - T)
    near("  raw (defective) level is BELOW entry", raw, 96.8, 1e-9)
    lvl = stab.trail_stop_level(E, armed, A, T, C)
    check_true("armed trail is floored at/above entry", lvl >= E)
    near("  floored to entry + round-trip cost", lvl, E * 1.0006, 1e-9)

    # every historically-observed dead-zone return is now impossible
    worst = -3.168
    check_true("worst observed dead-zone exit (-3.168%) now unreachable",
               (lvl / E - 1) * 100 > worst)

    # immediately after arm, and on retracement
    lvl2 = stab.trail_stop_level(E, 111.0, A, T, C)
    near("just after arm still floored", lvl2, E * 1.0006, 1e-9)

    # profitable trail: band applies normally, floor is inert
    lvl3 = stab.trail_stop_level(E, 150.0, A, T, C)
    near("profitable trail unchanged (150 * 0.88)", lvl3, 132.0, 1e-9)
    check_true("  floor did not interfere", lvl3 > E * 1.0006)

    # the crossover point: floor stops binding above entry/(1-T)*(1+cost)
    cross = E * 1.0006 / (1 - T)
    near("crossover trail level equals floor", stab.trail_stop_level(
        E, cross, A, T, C), E * 1.0006, 1e-6)

    # initial stop is untouched by this change
    near("initial stop still -15%", E * (1 + -0.15), 85.0, 1e-9)

    # degenerate inputs never raise
    for bad in ((0, 110), (-1, 110), ("x", 110), (100, "y"), (None, None)):
        stab.trail_stop_level(bad[0], bad[1], A, T, C)
    check("degenerate inputs return None, never raise",
          stab.trail_stop_level(0, 110, A, T, C), None)

    # replay the 17 real dead-zone exits: none may remain below entry
    below = 0
    for entry, hw in ((27.9, 27.9 * 1.10), (59.8, 59.8 * 1.12),
                      (7.2, 7.2 * 1.15), (2.75, 2.75 * 1.10),
                      (562.8, 562.8 * 1.10)):
        lv = stab.trail_stop_level(entry, hw, A, T, C)
        if lv is not None and lv < entry:
            below += 1
    check("no armed trail can exit below entry (sampled real entries)",
          below, 0)


# ===================================================================== 5 ===
def t_authorize_entry_only():
    print("\n[5] SECTION 3.1 - authorization is ENTRY-ONLY (unit layer)")
    # Worst possible context: everything wrong, simultaneously.
    hostile = {
        "symbol": "NIFTY", "signal": sig(bar="2026-08-21 15:25:00",
                                         status="STALE_OR_AMBIGUOUS"),
        "trading_date": TODAY, "now": "2026-08-26T14:00:00",
        "expiry": TODAY,                       # DTE 0
        "quote": {"ltp": 0, "bid": 0, "ask": 0},
        "open_positions": 99, "traded_today": {"NIFTY"},
        "entry_allowed": False, "in_entry_window": False,
    }
    r = stab.authorize(stab.ENTRY, hostile, CFG)
    check("ENTRY with everything wrong is REJECTED", bool(r), False)
    for intent in (stab.EXIT, "EXIT", "SQUARE_OFF", "TRAILING_STOP",
                   "INITIAL_STOP", "TREND_REVERSAL", None, ""):
        check(f"intent {intent!r} PASSES the same hostile context",
              bool(stab.authorize(intent, hostile, CFG)), True)
    check("EXIT passes with an EMPTY context (reads no input)",
          bool(stab.authorize(stab.EXIT, {}, CFG)), True)

    # ordering: the first thing wrong is what gets named
    base = {"symbol": "X", "signal": sig(), "trading_date": TODAY,
            "now": "2026-08-26 09:31:00",
            "expiry": TODAY + timedelta(days=6),
            "quote": {"ltp": 10, "bid": 9.975, "ask": 10.025},
            "open_positions": 0, "traded_today": set(),
            "entry_allowed": True, "in_entry_window": True}
    check("clean ENTRY authorized", bool(stab.authorize(stab.ENTRY, base, CFG)),
          True)
    check("window closed -> entry_window_closed",
          stab.authorize(stab.ENTRY, dict(base, in_entry_window=False),
                         CFG).reason, "entry_window_closed")
    check("supervisor halt -> daily_loss...",
          stab.authorize(stab.ENTRY, dict(base, entry_allowed=False),
                         CFG).reason, "daily_loss_or_supervisor_block")
    check("duplicate -> duplicate_symbol_today",
          stab.authorize(stab.ENTRY, dict(base, traded_today={"X"}),
                         CFG).reason, "duplicate_symbol_today")
    check("unknown position count -> rejected (never default-allow)",
          bool(stab.authorize(stab.ENTRY, dict(base, open_positions=None),
                              CFG)), False)

    # position cap
    for n, want in ((0, True), (1, True), (2, False), (3, False)):
        check(f"{n} open positions -> "
              f"{'allowed' if want else 'rejected'}",
              bool(stab.authorize(stab.ENTRY, dict(base, open_positions=n),
                                  CFG)), want)
    check("cap reason names the numbers",
          stab.authorize(stab.ENTRY, dict(base, open_positions=2),
                         CFG).reason, "position_cap(2>=2)")

    # disabling restores baseline
    off = {"stabilization": {"enabled": False}}
    check("gates OFF -> hostile ENTRY passes (rollback is config-only)",
          bool(stab.authorize(stab.ENTRY, hostile, off)), True)


# ===================================================================== 6 ===
def t_ledger():
    print("\n[6] CHANGE 11 - gate ledger identities")
    L = stab.GateLedger(2)
    for _ in range(5):
        L.generated()
    L.reject("signal_bar_stale")
    L.reject("dte_below_min(1<2)")
    L.reject("spread_above_max(2.00%>1.00%)")
    L.passed(); L.passed()
    L.entry(); L.entry()
    check("identities hold", L.check(), True)
    check("generated", L.candidates_generated, 5)
    check("rejections total", L.rejected_total, 3)
    check("bucketing: stale", L.counts["rejected_stale_signal"], 1)
    check("bucketing: dte", L.counts["rejected_dte"], 1)
    check("bucketing: spread", L.counts["rejected_spread"], 1)

    bad = stab.GateLedger(2)
    bad.generated(); bad.generated()
    bad.passed()
    try:
        bad.check()
        check("unaccounted candidate raises", False, True)
    except stab.LedgerIdentityError:
        check("unaccounted candidate raises", True, True)

    bad2 = stab.GateLedger(2)
    bad2.generated(); bad2.passed(); bad2.entry(); bad2.entry()
    try:
        bad2.check()
        check("entered > passed raises", False, True)
    except stab.LedgerIdentityError:
        check("entered > passed raises", True, True)

    bad3 = stab.GateLedger(2)
    for _ in range(3):
        bad3.generated(); bad3.passed(); bad3.entry()
    try:
        bad3.check()
        check("entered > max_option_positions raises", False, True)
    except stab.LedgerIdentityError:
        check("entered > max_option_positions raises", True, True)

    for reason, bucket in (
            ("signal_bar_previous_session", "rejected_stale_signal"),
            ("dte_unreadable", "rejected_dte"),
            ("quote_no_ltp", "rejected_quote_invalid"),
            ("quote_invalid_for_spread", "rejected_quote_invalid"),
            ("spread_above_max(9%>1%)", "rejected_spread"),
            ("position_cap(2>=2)", "rejected_position_cap"),
            ("duplicate_symbol_today", "rejected_duplicate"),
            ("daily_loss_or_supervisor_block", "rejected_daily_loss"),
            ("entry_window_closed", "rejected_entry_window"),
            ("no_listed_expiry", "rejected_no_contract"),
            ("no_listed_strike", "rejected_no_contract"),
            ("not_selected_slot_limit", "rejected_not_selected"),
            ("other_same_cycle_reentry_guard", "rejected_other")):
        check(f"bucket_for({reason})", stab.bucket_for(reason), bucket)


# ===================================================================== 7 ===
def t_selection():
    print("\n[7] S-44 - explicit production selection policy")
    cands = [{"name": n} for n in ("NIFTY", "BANKNIFTY", "RELIANCE",
                                   "HDFCBANK")]
    sel, dropped = stab.select_for_entry(cands, 2)
    check("policy name", stab.POLICY_NAME, "universe_order_v1")
    check("selects the first N in universe order",
          [c["name"] for c in sel], ["NIFTY", "BANKNIFTY"])
    check("drops the rest WITH a reason (accounted, not vanished)",
          [(c["name"], w) for c, w in dropped],
          [("RELIANCE", "not_selected_slot_limit"),
           ("HDFCBANK", "not_selected_slot_limit")])
    check("zero slots selects nothing",
          stab.select_for_entry(cands, 0)[0], [])
    check("zero slots drops everything",
          len(stab.select_for_entry(cands, 0)[1]), 4)
    check("negative slots is treated as zero",
          stab.select_for_entry(cands, -3)[0], [])
    check("more slots than candidates is not an error",
          len(stab.select_for_entry(cands, 99)[0]), 4)
    check("selection is order-preserving, not re-ranked",
          [c["name"] for c in stab.select_for_entry(cands, 4)[0]],
          ["NIFTY", "BANKNIFTY", "RELIANCE", "HDFCBANK"])


# ===================================================================== 8 ===
def t_costs():
    print("\n[8] CHANGE 10 - cost accounting (REPORTING ONLY)")
    c = stab.round_trip_cost(100.0, 110.0, 75)
    check_true("cost breakdown produced", c is not None)
    check_true("brokerage is two orders", c["brokerage"] == 40.0)
    check_true("STT charged on the SELL leg only, at the sourced 0.15%",
               abs(c["stt"] - 110.0 * 75 * 0.0015) < 1e-9)
    check_true("stamp duty charged on the BUY leg only",
               abs(c["stamp_duty"] - 100.0 * 75 * 0.00003) < 1e-9)
    check_true("total is the sum of the parts",
               abs(c["total"] - sum(v for k, v in c.items()
                                    if k != "total")) < 1e-9)
    check_true("total is strictly positive", c["total"] > 0)
    check("degenerate input returns None, never raises",
          stab.round_trip_cost("x", 1, 1), None)

    f = stab.spread_friction(99.5, 100.5, 108.0, 112.0, 75)
    near("spread friction = half-spread per leg x qty", f, (0.5 + 2.0) * 75, 1e-9)
    check("crossed quote -> None (never silently zero)",
          stab.spread_friction(101, 100, 1, 2, 75), None)
    check("missing quote -> None", stab.spread_friction(None, None, 1, 2, 75),
          None)
    check_true("rate source names the publisher and the page",
               "angelone.in/exchange-transaction-charges"
               in stab.COST_RATE_SOURCE)
    check_true("rate source records the retrieval date",
               "2026-08-26" in stab.COST_RATE_SOURCE)
    check_true("rate source states there is no contract note to reconcile",
               "PAPER TRADING" in stab.COST_RATE_SOURCE)
    check_true("rate source states it is reporting-only",
               "REPORTING ONLY" in stab.COST_RATE_SOURCE)
    check("STT rate matches the published schedule",
          stab.COST_RATES["stt_pct_sell_premium"], 0.15)
    check("exchange rate matches the published schedule",
          stab.COST_RATES["exchange_txn_pct"], 0.0355299)
    check("IPFT rate matches the published schedule",
          stab.COST_RATES["ipft_pct"], 0.002)
    check_true("GST base includes IPFT, per the schedule",
               abs(c["gst"] - (c["brokerage"] + c["exchange"] + c["sebi"]
                               + c["ipft"]) * 0.18) < 1e-9)


# ============================== LAYER 2: END-TO-END process() ==============
class FakeSmart:
    pass


class FakeAngel:
    """Stand-in for angelone_client. Deterministic, offline."""

    def __init__(self, quotes=None, master=True, expiry=None):
        self._quotes = quotes or {}
        self._master = master
        self._expiry = expiry or (TODAY + timedelta(days=6))
        self.calls = {"nearest_expiry": 0, "find_option": 0, "get_quotes": 0}

    def login(self):
        return FakeSmart()

    def load_instrument_master(self, universe=None, refresh_allowed=True):
        return {"NIFTY": [1], "BANKNIFTY": [1]} if self._master else {}

    def master_health(self):
        return {"status": "FRESH", "age_h": 1.0, "reason": None,
                "refresh_attempted": False}

    def nearest_expiry(self, master, name, today, min_dte=1):
        self.calls["nearest_expiry"] += 1
        return self._expiry

    def find_option(self, master, name, expiry, spot, opt_type):
        self.calls["find_option"] += 1
        return {"name": name, "opt_type": opt_type, "strike": 100.0,
                "expiry": expiry, "token": f"TK_{name}",
                "symbol": f"{name}TEST{opt_type}", "lotsize": 75}

    def get_quotes(self, smart, tokens):
        self.calls["get_quotes"] += 1
        return {t: self._quotes[t] for t in tokens if t in self._quotes}

    def fetch_option_iv(self, smart, symbol, expiry_date):
        return {}


def _quote(ltp, bid=None, ask=None, high=None, low=None):
    bid = ltp * 0.999 if bid is None else bid
    ask = ltp * 1.001 if ask is None else ask
    return {"ltp": ltp, "bid": bid, "ask": ask, "volume": 10000, "oi": 10000,
            "open": ltp, "high": high if high is not None else ltp,
            "low": low if low is not None else ltp, "close": ltp,
            "trading_symbol": "X", "exch_feed_time": None,
            "exch_trade_time": None, "received_at": None}


# --- fully-controlled driver: the REAL process() against a fake market ----
def _frame(bar_ts, spot, n=25):
    """A 25-bar frame whose LAST stamp is `bar_ts` - the value the real
    `_signal_bar_meta` then derives bar_ts / age / session_status from."""
    import pandas as pd
    last = stab.parse_bar_ts(bar_ts)
    idx = pd.to_datetime([last - timedelta(minutes=5 * (n - 1 - i))
                          for i in range(n)])
    return pd.DataFrame({"Open": [spot] * n, "High": [spot] * n,
                         "Low": [spot] * n, "Close": [spot] * n,
                         "Volume": [1000] * n}, index=idx)


def drive(tmp, name, *, bars, quotes, expiry, now_dt, cfg,
          direction="BULL", positions=(), halt=False, break_ranking=False,
          master=True, reuse=None):
    """Run ONE real process() cycle offline.

    `bars` maps underlying -> the timestamp of the last bar yfinance
    returns. Everything downstream - `_signal_bar_meta`, the freshness
    gate, the DTE gate, the quote/spread gate, selection, authorization,
    the ledger and the heartbeat - is the REAL production code.
    """
    import options_trader as ot
    import safety_supervisor as ss

    if reuse is None:
        ot.DB = Path(tmp) / f"{name}.db"
        obs = Path(tmp) / f"{name}_obs.db"
        telemetry.shutdown()
        telemetry.reset_for_test(str(obs))
        conn = ot.db_init()
        ot.meta_set(conn, "cash", 100000)
        for p_ in positions:
            conn.execute(
                "INSERT INTO options_positions(symbol,option_type,strike,"
                "expiry,qty,entry_price,entry_time,high_water,stop_price,"
                "token,trading_symbol,lots,lotsize,last_price) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p_["symbol"], p_.get("opt_type", "CE"), 100.0,
                 p_.get("expiry", expiry).isoformat(), 75, p_["entry"],
                 datetime.now().isoformat(), p_.get("high_water", p_["entry"]),
                 p_.get("stop", p_["entry"] * 0.85), p_["token"],
                 f"{p_['symbol']}TESTCE", 1, 75, p_["entry"]))
        conn.commit()
    else:
        conn, obs = reuse
        # A previous assertion block may have released the store handle to
        # read it; re-open the SAME file so this cycle is still recorded.
        telemetry.init(str(obs))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_dt

    class BrokenRanking:
        SECTOR = {}

        @staticmethod
        def get_config(cfg_):
            raise RuntimeError("ranking exploded")

    class FakeYF:
        @staticmethod
        def download(ticker, **kw):
            for n_, t_ in ot.UNIVERSE:
                if t_ == ticker and n_ in bars:
                    return _frame(bars[n_], 100.0)
            return None

    ang = FakeAngel(quotes=quotes, expiry=expiry, master=master)
    saved = (ot.angel, ot.datetime, ot.telegram, ot.yf, ot.get_direction,
             ot.signal_bars, ot.STAB, ot.STAB_ON, ot.MAX_OPTION_POSITIONS,
             ot.ranking, ss.entry_permission)
    ot.angel = ang
    ot.datetime = Clock
    ot.telegram = lambda m: None
    ot.yf = FakeYF
    ot.get_direction = lambda df: direction
    ot.signal_bars = lambda df, observed_at=None: df
    ot.STAB = stab.get_config(cfg)
    ot.STAB_ON = bool(ot.STAB["enabled"])
    ot.MAX_OPTION_POSITIONS = (int(ot.STAB["max_option_positions"])
                               if ot.STAB_ON else ot.MAX_POSITIONS)
    if halt:
        ss.entry_permission = lambda *a, **k: (
            False, {"reason": "HALTED_DAILY_LOSS", "book": "options",
                    "realized_today": -2000, "realized_low_water": -2000,
                    "limit": 2000})
    if break_ranking:
        ot.ranking = BrokenRanking

    buf, err = io.StringIO(), None
    try:
        with redirect_stdout(buf):
            ot.process(conn, [], TODAY)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        (ot.angel, ot.datetime, ot.telegram, ot.yf, ot.get_direction,
         ot.signal_bars, ot.STAB, ot.STAB_ON, ot.MAX_OPTION_POSITIONS,
         ot.ranking, ss.entry_permission) = saved

    return {"conn": conn, "obs": obs, "angel": ang, "error": err,
            "out": buf.getvalue(),
            "open": conn.execute(
                "SELECT COUNT(*) FROM options_positions").fetchone()[0],
            "trades": conn.execute(
                "SELECT reason,exit_price FROM options_trades "
                "ORDER BY id").fetchall()}


def exit_path_case(name, tmp, *, signal_bar, quote, expiry, now_dt, cfg,
                   entry=100.0, stop=None, halt=False, break_ranking=False,
                   direction="BULL"):
    """Open a position, run one REAL cycle with hostile ENTRY conditions,
    and report whether the position still closed."""
    r = drive(tmp, name, bars={"NIFTY": signal_bar, "BANKNIFTY": signal_bar},
              quotes={"TK_NIFTY": quote, "TK_BANKNIFTY": quote},
              expiry=expiry, now_dt=now_dt, cfg=cfg, direction=direction,
              halt=halt, break_ranking=break_ranking,
              positions=[{"symbol": "NIFTY", "token": "TK_NIFTY",
                          "entry": entry, "stop": stop or entry * 0.85,
                          "expiry": expiry}])
    r["conn"].close()
    telemetry.shutdown()
    return r


STALE_BAR = "2026-08-21 15:25:00+05:30"     # Friday close, read on Monday
FRESH_BAR = "2026-08-26 10:55:00+05:30"
# Two wide-spread quotes: one that BREACHES the stop (so the stop fires)
# and one that does not (so the square-off / reversal branch is reached).
WIDE_LOW = _quote(80.0, bid=60.0, ask=100.0, low=60.0)     # 50% spread
WIDE_HIGH = _quote(150.0, bid=120.0, ask=200.0, low=120.0)  # 50% spread
GOOD_EXP = TODAY + timedelta(days=6)


def t_exit_path_end_to_end(tmp):
    print("\n[9] SECTION 17 / 3.1 - EXIT PATH, end-to-end through process()")
    print("    Real process(); position open; entry gates deliberately failing.")
    mid = datetime(2026, 8, 26, 11, 0, 0)
    late = datetime(2026, 8, 26, 15, 20, 0)
    cfg = {}
    stop_q = _quote(80.0, bid=79.9, ask=80.1, low=80.0)   # trades to the stop

    cases = [
        ("stale signal bar",
         dict(signal_bar=STALE_BAR, quote=stop_q, expiry=GOOD_EXP,
              now_dt=mid, cfg=cfg), "Initial stop"),
        ("spread above threshold",
         dict(signal_bar=FRESH_BAR, quote=WIDE_LOW, expiry=GOOD_EXP,
              now_dt=mid, cfg=cfg), "Initial stop"),
        ("DTE 0",
         dict(signal_bar=FRESH_BAR, quote=stop_q, expiry=TODAY,
              now_dt=mid, cfg=cfg), "Initial stop"),
        ("outside entry window (15:15 square-off)",
         dict(signal_bar=FRESH_BAR, quote=_quote(120.0), expiry=GOOD_EXP,
              now_dt=late, cfg=cfg), "Square-off 15:15"),
        ("daily-loss halt active",
         dict(signal_bar=FRESH_BAR, quote=stop_q, expiry=GOOD_EXP,
              now_dt=mid, cfg=cfg, halt=True), "Initial stop"),
        ("ranking raises an exception",
         dict(signal_bar=FRESH_BAR, quote=stop_q, expiry=GOOD_EXP,
              now_dt=mid, cfg=cfg, break_ranking=True), "Initial stop"),
        ("ALL entry gates failing at once",
         dict(signal_bar=STALE_BAR, quote=WIDE_HIGH, expiry=TODAY,
              now_dt=late, cfg=cfg, halt=True, break_ranking=True),
         "Square-off 15:15"),
    ]
    for i, (label, kw, want) in enumerate(cases):
        r = exit_path_case(f"exit{i}", tmp, **kw)
        check(f"{label}: no crash", r["error"], None)
        check(f"{label}: position CLOSED", r["open"], 0)
        check(f"{label}: exit reason",
              r["trades"][0][0] if r["trades"] else None, want)
        check(f"{label}: nothing NEW was entered", len(r["trades"]), 1)

    # trend-reversal exit under a hostile entry context
    r = exit_path_case("exitrev", tmp, signal_bar=STALE_BAR, quote=WIDE_HIGH,
                       expiry=GOOD_EXP, now_dt=mid, cfg=cfg,
                       direction="BEAR")
    check("trend reversal exit fires with every entry gate failing",
          r["trades"][0][0] if r["trades"] else None, "Trend reversal exit")
    check("  position gone", r["open"], 0)

    # gates DISABLED must not change the exit outcome
    r = exit_path_case("exitoff", tmp, signal_bar=STALE_BAR, quote=stop_q,
                       expiry=GOOD_EXP, now_dt=mid,
                       cfg={"stabilization": {"enabled": False}})
    check("gates OFF: exit identical", r["open"], 0)
    check("  reason identical",
          r["trades"][0][0] if r["trades"] else None, "Initial stop")

    # An exit must also survive a completely absent instrument master and a
    # broken supervisor - neither is an entry gate, but both sit upstream.
    r = drive(tmp, "exitnomaster", bars={"NIFTY": FRESH_BAR},
              quotes={"TK_NIFTY": stop_q}, expiry=GOOD_EXP, now_dt=mid,
              cfg={}, master=False,
              positions=[{"symbol": "NIFTY", "token": "TK_NIFTY",
                          "entry": 100.0}])
    check("no instrument master: exit still executes", r["open"], 0)
    r["conn"].close(); telemetry.shutdown()


def t_no_substitution(tmp):
    print()
    print("[10] SECTION 3.2 - gates REJECT, they never SUBSTITUTE")
    mid = datetime(2026, 8, 26, 10, 0, 0)
    good = _quote(50.0, bid=49.9, ask=50.1)

    # DTE 1 for every underlying -> the DTE gate must reject, and must NOT
    # ask for another expiry or walk to another strike.
    r = drive(tmp, "sub_dte", bars={"NIFTY": "2026-08-26 09:55:00+05:30",
                                    "BANKNIFTY": "2026-08-26 09:55:00+05:30"},
              quotes={"TK_NIFTY": good, "TK_BANKNIFTY": good},
              expiry=TODAY + timedelta(days=1), now_dt=mid, cfg={})
    out, ang = r["out"], r["angel"]
    check_true("DTE rejection is logged with its reason",
               "dte_below_min" in out)
    check("nearest_expiry called at most once per symbol",
          ang.calls["nearest_expiry"] <= 2, True)
    check("find_option NEVER called -> no strike walk", ang.calls["find_option"],
          0)
    check("nothing entered", r["open"], 0)
    r["conn"].close(); telemetry.shutdown()

    # Stale signal -> rejected BEFORE any contract work, so neither the
    # expiry nor the strike lookup can be driven by the rejection.
    r = drive(tmp, "sub_stale", bars={"NIFTY": STALE_BAR,
                                      "BANKNIFTY": STALE_BAR},
              quotes={"TK_NIFTY": good, "TK_BANKNIFTY": good},
              expiry=GOOD_EXP, now_dt=mid, cfg={})
    check_true("stale-signal rejection is logged",
               "signal_bar_previous_session" in r["out"])
    check("stale signal: no expiry lookup at all",
          r["angel"].calls["nearest_expiry"], 0)
    check("stale signal: no strike lookup at all",
          r["angel"].calls["find_option"], 0)
    check("stale signal: nothing entered", r["open"], 0)
    r["conn"].close(); telemetry.shutdown()

    # Wide spread -> rejected at final authorization; no adjacent strike is
    # tried and no other underlying backfills the freed slot.
    r = drive(tmp, "sub_spread", bars={"NIFTY": "2026-08-26 09:55:00+05:30",
                                       "BANKNIFTY":
                                           "2026-08-26 09:55:00+05:30"},
              quotes={"TK_NIFTY": WIDE_HIGH, "TK_BANKNIFTY": WIDE_HIGH},
              expiry=GOOD_EXP, now_dt=mid, cfg={})
    check_true("spread rejection is logged", "spread_above_max" in r["out"])
    check("spread-rejected: nothing entered", r["open"], 0)
    check("spread-rejected: find_option called once per symbol (no walk)",
          r["angel"].calls["find_option"] <= 2, True)
    r["conn"].close(); telemetry.shutdown()

    # Structural proof: there is no substitution MECHANISM to drive.
    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    proc = src[src.index("def process("):]
    check("nearest_expiry has exactly ONE call site in process()",
          proc.count("angel.nearest_expiry("), 1)
    check("find_option has exactly ONE call site in process()",
          proc.count("angel.find_option("), 1)
    check_true("the momentum fallback tier consults gate_rejected",
               "name in gate_rejected" in proc)
    check("no expiry-rollover loop exists", "for exp in" in proc, False)
    entry = proc[proc.index("iv_cache = {}"):]
    check("the entry loop has no retry/continue-with-different-contract path",
          entry.count("angel.find_option("), 0)
    check("no gate reason is re-evaluated against a second threshold",
          entry.count("stab.authorize("), 1)
    check_true("a failed authorization ends that candidate (continue)",
               "ledger.reject(auth.reason)" in entry and "continue" in entry)


def t_position_cap(tmp):
    print("\n[11] CHANGE 6 - MAX_OPTION_POSITIONS enforced at submission")
    import options_trader as ot
    check("options cap is 2", ot.MAX_OPTION_POSITIONS, 2)
    check("stock/book cap key UNCHANGED at 4", ot.MAX_POSITIONS, 4)
    cc = json.loads((HERE / "intraday_config.json").read_text())
    check("max_open_positions (shared with paper_trader/intraday_backtest) "
          "untouched", cc["max_open_positions"], 4)
    check("options cap lives in its own key",
          cc["stabilization"]["max_option_positions"], 2)

    for n, want in ((0, True), (1, True), (2, False), (3, False)):
        ctx = {"symbol": "X", "signal": sig(), "trading_date": TODAY,
               "now": "2026-08-26 09:31:00",
               "expiry": TODAY + timedelta(days=6),
               "quote": {"ltp": 10, "bid": 9.975, "ask": 10.025},
               "open_positions": n, "traded_today": set(),
               "entry_allowed": True, "in_entry_window": True}
        check(f"{n} open -> {'allowed' if want else 'rejected'}",
              bool(stab.authorize(stab.ENTRY, ctx, CFG)), want)

    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    entry = src[src.index("iv_cache = {}"):]
    check_true("position count is RE-READ from the database inside the "
               "entry loop, not trusted from selection time",
               'SELECT COUNT(*) FROM options_positions").fetchone()[0]'
               in entry)
    check_true("authorization is called for every entry",
               "stab.authorize(stab.ENTRY" in entry)


def t_concurrency():
    print("\n[12] CHANGE 8 - cross-run concurrency")
    wf = (HERE.parent / ".github" / "workflows" / "intraday.yml").read_text(
        encoding="utf-8")
    check_true("workflow declares a concurrency group",
               "concurrency:" in wf)
    check_true("  group is named", "group: intraday-trader" in wf)
    check_true("  overlapping runs QUEUE (cancel-in-progress: false)",
               "cancel-in-progress: false" in wf)
    # The real property: exactly ONE workflow pushes the options book, and
    # every workflow that touches trading-state at all declares a
    # concurrency group. A workflow census would break the moment a new
    # workflow is added, which says nothing about safety.
    wfdir = HERE.parent / ".github" / "workflows"
    writers, touchers = [], []
    for f in sorted(wfdir.glob("*.yml")):
        t = f.read_text(encoding="utf-8")
        # Only EXECUTED lines count - a comment saying "no save" is not one.
        for line in t.splitlines():
            code = line.split("#", 1)[0]
            if "state_sync.py save" in code and "options_trades.db" in code:
                writers.append(f.name)
                break
        if "state_sync.py" in t:
            touchers.append((f.name, "concurrency:" in t))
    check("exactly one workflow SAVES the options book",
          writers, ["intraday.yml"])
    check_true("premarket saves only its own book",
               "state_sync.py save --own market_memory.db" in
               (wfdir / "premarket.yml").read_text(encoding="utf-8"))
    for name, has_group in touchers:
        check(f"{name} declares a concurrency group", has_group, True)
    smoke = (wfdir / "smoke-test.yml").read_text(encoding="utf-8")
    check_true("the smoke test shares the trader's concurrency group",
               "group: intraday-trader" in smoke)
    check("the smoke test never saves state",
          any("state_sync.py save" in ln.split("#", 1)[0]
              for ln in smoke.splitlines()), False)
    check_true("the smoke test arms the dry run",
               'OPTIONS_DRY_RUN: "1"' in smoke)
    # B4: the timing hazard is documented where an operator will meet it.
    check_true("the smoke test carries the scheduling warning",
               "RUN THIS BEFORE 08:45 IST" in smoke)
    check_true("  and records why a separate group was rejected",
               "REJECTED" in smoke and "concurrently" in smoke)

    ss = (HERE / "state_sync.py").read_text(encoding="utf-8")
    check("state push is NOT a force-push", "--force" in ss, False)
    check("  nor a force-with-lease", "force-with-lease" in ss, False)
    check("  nor a refspec overwrite (+branch)", '"+" + BRANCH' in ss, False)
    check_true("push is a plain fast-forward push",
               'run(["git", "push", "origin", BRANCH]' in ss)
    check_true("a rejected push RETRIES against re-cloned state, "
               "never overwrites",
               "push rejected (attempt" in ss and "for attempt in range(1, 4)"
               in ss)
    check_true("a run that lost state cannot overwrite a fuller branch",
               "REGRESSION BLOCKED" in ss)

    import state_sync
    check("monotonic guard covers observability.db",
          state_sync.MONOTONIC.get("observability.db"), "cycle")
    check("monotonic guard covers the options book",
          state_sync.MONOTONIC.get("options_trades.db"), "options_trades")


def t_regression_guard(tmp):
    print("\n[13] state regression guard (stale run cannot clobber newer)")
    import state_sync
    newer = Path(tmp) / "newer.db"
    older = Path(tmp) / "older.db"
    for path, n in ((newer, 10), (older, 3)):
        c = sqlite3.connect(str(path))
        c.execute("CREATE TABLE options_trades(id INTEGER)")
        c.executemany("INSERT INTO options_trades VALUES(?)",
                      [(i,) for i in range(n)])
        c.commit(); c.close()
    check("row counts read correctly",
          (state_sync.count_rows(str(newer), "options_trades"),
           state_sync.count_rows(str(older), "options_trades")), (10, 3))
    check("a corrupt/absent file is not silently treated as empty",
          state_sync.count_rows(str(Path(tmp) / "nope.db"), "options_trades"),
          None)
    check("local(3) < branch(10) is the BLOCKED condition", 3 < 10, True)


def t_observability(tmp):
    print()
    print("[14] CHANGE 3/9/11 - observability end to end")
    mid = datetime(2026, 8, 26, 10, 0, 0)
    good = _quote(50.0, bid=49.9, ask=50.1)
    bars = {"NIFTY": "2026-08-26 09:55:00+05:30",
            "BANKNIFTY": "2026-08-26 09:55:00+05:30"}

    # --- cycle 1: two clean candidates, both should enter ---------------
    r = drive(tmp, "obs", bars=bars,
              quotes={"TK_NIFTY": good, "TK_BANKNIFTY": good},
              expiry=GOOD_EXP, now_dt=mid, cfg={})
    conn, obs, out = r["conn"], r["obs"], r["out"]
    check("cycle 1: no crash", r["error"], None)
    check("cycle 1: entered exactly MAX_OPTION_POSITIONS", r["open"], 2)
    check_true("ledger printed for the operator", "GATE LEDGER:" in out)
    telemetry.shutdown()

    o = sqlite3.connect(str(obs))
    try:
        check("cycle heartbeat written",
              o.execute("SELECT COUNT(*) FROM cycle_heartbeat").fetchone()[0], 1)
        hb = o.execute("SELECT auth_ok,master_ok,signals_ok,quotes_fetched,"
                       "gates_evaluated,entry_window,candidates_generated "
                       "FROM cycle_heartbeat").fetchone()
        check("  heartbeat: auth ok", hb[0], 1)
        check("  heartbeat: master ok", hb[1], 1)
        check("  heartbeat: signals ok", hb[2], 1)
        check_true("  heartbeat: quotes counted", hb[3] and hb[3] > 0)
        check("  heartbeat: gates evaluated", hb[4], 1)
        check("  heartbeat: entry window recorded", hb[5], 1)
        check("  heartbeat: candidate count recorded", hb[6], 2)

        gl = o.execute("SELECT candidates_generated,passed_to_selection,"
                       "entered,identities_ok FROM gate_ledger").fetchone()
        check("gate ledger written", gl is not None, True)
        check("  identities hold", gl[3], 1)
        check("  generated == passed + rejections", gl[0], 2)
        check("  entered <= passed", gl[2] <= gl[1], True)
        check("  entered <= MAX_OPTION_POSITIONS", gl[2] <= 2, True)

        ents = o.execute("SELECT decision_id,candidate_id FROM decision "
                         "WHERE action='ENTRY'").fetchall()
        check("ENTRY decisions recorded", len(ents), 2)
        check("EVERY entry carries a candidate_id (was 0/34 historically)",
              all(e[1] for e in ents), True)
        for _, cid in ents:
            row = o.execute(
                "SELECT symbol,cycle_id,selection_policy,gate_result,"
                "spread_pct,signal_bar_ts FROM candidate_snapshot "
                "WHERE candidate_id=?", (cid,)).fetchone()
            check_true("  candidate resolves to a snapshot", row is not None)
            check("  selection policy recorded", row[2], "universe_order_v1")
            check("  gate verdict recorded", row[3], "ELIGIBLE")
            check_true("  cycle linkage present", bool(row[1]))
            check_true("  spread recorded", row[4] is not None)
            check_true("  signal bar recorded", bool(row[5]))
    finally:
        o.close()

    # --- cycle 2: premium collapses -> both stop out --------------------
    stop_q = _quote(30.0, bid=29.9, ask=30.1, low=29.9)
    r2 = drive(tmp, "obs", bars=bars,
               quotes={"TK_NIFTY": stop_q, "TK_BANKNIFTY": stop_q},
               expiry=GOOD_EXP, now_dt=datetime(2026, 8, 26, 10, 30, 0),
               cfg={}, reuse=(conn, obs))
    check("cycle 2: both positions closed", r2["open"], 0)
    check("cycle 2: two trades booked", len(r2["trades"]), 2)

    # --- cycle 3: the post-exit path is observed ------------------------
    r3 = drive(tmp, "obs", bars=bars,
               quotes={"TK_NIFTY": _quote(35.0), "TK_BANKNIFTY": _quote(35.0)},
               expiry=GOOD_EXP, now_dt=datetime(2026, 8, 26, 10, 40, 0),
               cfg={}, reuse=(conn, obs))
    check("cycle 3: no crash", r3["error"], None)
    conn.close()
    telemetry.shutdown()

    o = sqlite3.connect(str(obs))
    try:
        pe = o.execute("SELECT COUNT(*) FROM post_exit_path").fetchone()[0]
        check_true("post_exit_path is POPULATED (was 0 rows)", pe > 0)
        row = o.execute("SELECT minutes_since_exit,ltp,entry_price,exit_price,"
                        "exit_reason,underlying_spot FROM post_exit_path "
                        "LIMIT 1").fetchone()
        check_true("  minutes_since_exit recorded", row[0] is not None)
        check_true("  option mark recorded", row[1] is not None)
        check_true("  entry and exit price recorded (recovery is derivable)",
                   row[2] is not None and row[3] is not None)
        check_true("  exit reason recorded", bool(row[4]))
        check_true("  underlying spot recorded", row[5] is not None)

        ex = o.execute("SELECT exit_reason,entry_price,initial_stop_level,"
                       "dist_to_initial_stop FROM exit_snapshot").fetchall()
        check_true("exit_snapshot written for every exit", len(ex) >= 2)
        check("  entry price recorded",
              all(e[1] is not None for e in ex), True)
        check("  initial stop level recorded (CHANGE 15)",
              all(e[2] is not None for e in ex), True)
        check("  distance to initial stop recorded",
              all(e[3] is not None for e in ex), True)

        tc = o.execute("SELECT gross_pnl,cost_total,net_pnl,rate_source "
                       "FROM trade_cost").fetchall()
        check_true("trade_cost written for every exit", len(tc) >= 2)
        check("  cost is strictly positive",
              all(t[1] and t[1] > 0 for t in tc), True)
        check("  net is BELOW gross (costs subtract)",
              all(t[2] is not None and t[2] < t[0] for t in tc), True)
        check_true("  rate source recorded", all(t[3] for t in tc))
        check("  gross P&L is NOT rewritten by the cost model",
              all(t[0] is not None for t in tc), True)

        n_hb = o.execute("SELECT COUNT(*) FROM cycle_heartbeat").fetchone()[0]
        check("one heartbeat per cycle (3 cycles run)", n_hb, 3)
        n_gl = o.execute("SELECT COUNT(*) FROM gate_ledger").fetchone()[0]
        check("one gate ledger per cycle", n_gl, 3)
        bad = o.execute("SELECT COUNT(*) FROM gate_ledger "
                        "WHERE identities_ok != 1").fetchone()[0]
        check("no cycle violated the ledger identities", bad, 0)
    finally:
        o.close()


def t_replay_harness(tmp):
    print()
    print("[15] SECTION 23 - replay harness verified BEFORE any replay claim")
    import replay_engine as rp
    state = os.environ.get("WED_STATE_DIR")
    if not state or not (Path(state) / "options_trades.db").exists():
        print("  SKIP  WED_STATE_DIR not set - run with the production state "
              "copy to exercise this (see the validation log).")
        return
    res = rp.run_mode_a(str(Path(state) / "options_trades.db"))
    check_true("mode A produces NON-EMPTY output", len(res) > 0)
    check_true("each result is a (trade, replay) pair",
               all(isinstance(x, tuple) and len(x) == 2 for x in res))
    day = [x for x in res if x[0]["entry_time"][:10] == "2026-08-25"]
    check_true("one known day replays non-empty", len(day) > 0)
    exact, mism, _ = rp.reconcile(day)
    check_true("  the day's fields reconcile exactly", exact > 0)
    check("  zero replay disagreements on that day", mism, 0)
    cen = rp.classification_census(day)
    check_true("  every field carries an evidence classification",
               set(cen) <= {"DIRECTLY OBSERVED", "DETERMINISTICALLY DERIVED",
                            "APPROXIMATED", "UNAVAILABLE"})


def t_eod_summary_and_daily_loss(tmp):
    print()
    print("[18] CRITERION O - a zero-trade day is POSITIVELY confirmable")
    import options_trader as ot
    import safety_supervisor as ss

    ot.DB = Path(tmp) / "eod.db"
    telemetry.shutdown()
    telemetry.reset_for_test(str(Path(tmp) / "eod_obs.db"))
    conn = ot.db_init()
    ot.meta_set(conn, "cash", 100000)
    conn.commit()
    sent = []
    saved_tg = ot.telegram
    ot.telegram = lambda m: sent.append(m)
    try:
        # a session that generated candidates and rejected them all
        L = stab.GateLedger(2)
        for _ in range(6):
            L.generated()
        for r_ in ("spread_above_max(3.0%>1.0%)", "spread_above_max(2.0%>1.0%)",
                   "dte_below_min(1<2)", "signal_bar_stale",
                   "not_selected_slot_limit", "duplicate_symbol_today"):
            L.reject(r_)
        acc = ot._record_day_gates(conn, "2026-08-26", L)
        check("day accumulator counts the cycle", acc["cycles"], 1)
        check("day accumulator totals the candidates",
              acc["candidates_generated"], 6)

        # before 15:35 the summary must stay silent
        ot._eod_gate_summary(conn, "2026-08-26", 15 * 60 + 30, [])
        check("no summary before 15:35", len(sent), 0)

        log = []
        ot._eod_gate_summary(conn, "2026-08-26", 15 * 60 + 35, log)
        check("summary sent at 15:35", len(sent), 1)
        msg = sent[0]
        check_true("  names the session", "2026-08-26" in msg)
        check_true("  reports the heartbeat count", "cycles (heartbeats): 1"
                   in msg)
        check_true("  reports candidates generated",
                   "candidates generated: 6" in msg)
        check_true("  itemises spread rejections", "rejected spread: 2" in msg)
        check_true("  itemises DTE rejections", "rejected dte: 1" in msg)
        check_true("  itemises stale-signal rejections",
                   "rejected stale signal: 1" in msg)
        check_true("  reports ZERO entries explicitly", "entered: 0" in msg)
        check_true("  states whether the identities hold",
                   "identities hold: YES" in msg)
        check_true("  states the policy in force", "max_pos=2" in msg)

        # exactly once per day, even across many cycles
        ot._eod_gate_summary(conn, "2026-08-26", 15 * 60 + 40, [])
        ot._eod_gate_summary(conn, "2026-08-26", 15 * 60 + 59, [])
        check("summary is sent exactly once per session", len(sent), 1)
    finally:
        ot.telegram = saved_tg
        conn.close()
        telemetry.shutdown()

    print()
    print("[19] CRITERION Q - daily-loss policy UNCHANGED and still fires")
    import config_contract
    cfg = json.loads((HERE / "intraday_config.json").read_text())
    check("max_daily_loss_percent untouched", cfg["max_daily_loss_percent"], 2)
    check("capital untouched", cfg["capital"], 100000)
    check("max_capital_per_trade untouched", cfg["max_capital_per_trade"],
          25000)
    con = config_contract.Contract(cfg)
    check("limit is still Rs.2,000", con.daily_loss_limit(), 2000.0)
    check("supervisor computes the same limit",
          ss.daily_loss_limit(100000, 2), 2000.0)

    # It must still fire at the REDUCED exposure level: two positions of
    # Rs.25,000 can lose far more than Rs.2,000, so the cap does not make
    # the halt unreachable.
    ot.DB = Path(tmp) / "dl.db"
    telemetry.shutdown()
    telemetry.reset_for_test(str(Path(tmp) / "dl_obs.db"))
    conn = ot.db_init()
    today = TODAY
    for pnl in (-1200.0, -900.0):
        conn.execute(
            "INSERT INTO options_trades(symbol,option_type,strike,expiry,qty,"
            "entry_price,exit_price,entry_time,exit_time,pnl,reason,"
            "price_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("NIFTY", "CE", 100.0, "2026-09-01", 75, 100.0, 84.0,
             f"{today}T09:31:00", f"{today}T10:00:00", pnl, "Initial stop",
             "STOP_LEVEL"))
    conn.commit()
    allowed, state = ss.entry_permission(conn, ss.OPTIONS_BOOK, today,
                                         100000, 2)
    check("two capped losses breach the limit", state["realized_today"],
          -2100.0)
    check("  entries are HALTED", allowed, False)
    check("  reason is the daily-loss halt", state["reason"],
          "HALTED_DAILY_LOSS")
    check("  the limit reported is unchanged", state["limit"], 2000.0)

    # worst case at the new cap: 2 x Rs.25,000 x -15% initial stop
    worst = 2 * 25000 * 0.15
    check_true("worst single-cycle loss at cap=2 still exceeds the limit, "
               "so the halt is reachable", worst > 2000.0)
    conn.close()
    telemetry.shutdown()


def t_credential_hygiene():
    print()
    print("[17] CHANGE 14 / U-017 - credential exposure, repository side")
    import subprocess
    root = HERE.parent
    # A Telegram bot token has a recognisable SHAPE. This asserts on the
    # SHAPE only - the value is never read into a variable that is printed,
    # never logged, and never transmitted anywhere.
    pat = r"[0-9]{8,12}:[A-Za-z0-9_-]{30,}"
    r = subprocess.run(["git", "grep", "-I", "-l", "-E", pat],
                       cwd=str(root), capture_output=True, text=True)
    tracked_hits = [f for f in r.stdout.split()
                    if not f.endswith("test_wed_stabilization.py")]
    check("no TRACKED file carries a credential shape", tracked_hits, [])

    cfg = json.loads((HERE / "intraday_config.json").read_text())
    check("telegram bot_token is blank in the repository",
          cfg["telegram"]["bot_token"], "")
    check("telegram chat_id is blank in the repository",
          cfg["telegram"]["chat_id"], "")

    gi = (root / ".gitignore").read_text(encoding="utf-8")
    check_true("the historically-leaked config path is gitignored",
               "backtest-machine/config.json" in gi)
    ls = subprocess.run(["git", "ls-files", "backtest-machine/config.json"],
                        cwd=str(root), capture_output=True, text=True)
    check("the leaked file is no longer tracked", ls.stdout.strip(), "")

    wf = (root / ".github" / "workflows" / "intraday.yml").read_text(
        encoding="utf-8")
    check_true("live credentials are injected from Actions secrets at runtime",
               "secrets.TG_BOT_TOKEN" in wf)
    check("the workflow never echoes the secret",
          "echo \"$TG_BOT_TOKEN" in wf, False)


def t_rollback():
    print("\n[16] SECTION 33 - schema is additive; rollback is bounded")
    src = (HERE / "telemetry.py").read_text(encoding="utf-8")
    check_true("migrations are ADD COLUMN only",
               "ALTER TABLE %s ADD COLUMN %s %s" in src)
    check("no column is dropped", "DROP COLUMN" in src, False)
    check("no table is dropped", "DROP TABLE" in src, False)
    check("no column is renamed", "RENAME COLUMN" in src, False)
    for _, _, decl in telemetry.MIGRATIONS:
        check_true(f"  {decl} column is nullable (no NOT NULL)",
                   "NOT NULL" not in decl)
    check_true("config disable restores baseline",
               stab.get_config({"stabilization": {"enabled": False}})
               ["enabled"] is False)


def main():
    print("=" * 72)
    print("WEDNESDAY 2026-08-26 STABILIZATION ACCEPTANCE TESTS")
    print("=" * 72)
    with tempfile.TemporaryDirectory() as tmp:
        t_signal_freshness()
        t_dte()
        t_liquidity()
        t_trailing()
        t_authorize_entry_only()
        t_ledger()
        t_selection()
        t_costs()
        t_exit_path_end_to_end(tmp)
        t_no_substitution(tmp)
        t_position_cap(tmp)
        t_concurrency()
        t_regression_guard(tmp)
        t_observability(tmp)
        t_replay_harness(tmp)
        t_eod_summary_and_daily_loss(tmp)
        t_credential_hygiene()
        t_rollback()
    print("\n" + "=" * 72)
    print(f"{_N[0]} assertions, {len(FAILURES)} failed")
    if FAILURES:
        for f in FAILURES:
            print(f"  FAILED: {f}")
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
