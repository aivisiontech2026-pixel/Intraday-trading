"""Characterization tests for the reporting/analytics module.

    python test_trade_analytics.py

Exits non-zero on any failure, matching test_exit_engine.py's convention.

WHY THIS EXISTS
---------------
trade_analytics.py had no test coverage, and compare_strategies() indexes
its query result POSITIONALLY (r[0] = score, r[1] = pnl). Widening that
SELECT - which V2-R1 requires, because the query carried no timestamp -
can silently shift those indices: the values stay numeric, the report
still renders, and every statistic comes out wrong with no exception
raised. These tests pin the numeric contract so that failure is loud.

SAFETY
------
Every test builds its own in-memory SQLite fixture. No production or
historical database is opened, read, or written. ensure_schema() is
never called, so no schema migration can occur - which is also why
build_report() is not executed here; the rendering assertions below
inspect the module source instead.
"""

import math
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import trade_analytics as ta

FAILURES = []


def check(name, got, want, tol=1e-6):
    if isinstance(want, float) and isinstance(got, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


# --------------------------------------------------------------- fixture ---
# Chronological order is the INSERT order below. Scores are deliberately
# NOT monotonic with time, so score-ordering and time-ordering produce
# different equity paths - which is exactly what V2-R1 is about.
#
#   exit_time            pnl    score
#   2026-08-01T10:00   +100.0   0.90
#   2026-08-02T10:00   -500.0   0.50
#   2026-08-03T10:00    +50.0   0.80
#   2026-08-04T10:00   -300.0   0.60
#   2026-08-05T10:00   +200.0   0.70
FIXTURE = [
    # sym, tsym, qty, entry, exit, pnl, reason, src, ebid, eask, eoi, evol,
    # score, rank, tier, exit_time
    ("AAA", "AAA-CE", 100, 10.0, 11.0, 100.0, "Square-off 15:15", "LIVE_BID",
     9.9, 10.1, 500, 1000, 0.90, 1, "confluence", "2026-08-01T10:00:00"),
    ("BBB", "BBB-PE", 100, 20.0, 15.0, -500.0, "Initial stop", "STOP_LEVEL",
     19.8, 20.2, 500, 1000, 0.50, 5, "confluence", "2026-08-02T10:00:00"),
    ("CCC", "CCC-CE", 100, 30.0, 30.5, 50.0, "Trailing stop", "STOP_LEVEL",
     29.7, 30.3, 500, 1000, 0.80, 2, "confluence", "2026-08-03T10:00:00"),
    ("DDD", "DDD-PE", 100, 40.0, 37.0, -300.0, "Trend reversal exit", "LIVE_BID",
     39.6, 40.4, 500, 1000, 0.60, 4, "confluence", "2026-08-04T10:00:00"),
    ("EEE", "EEE-CE", 100, 50.0, 52.0, 200.0, "Square-off 15:15", "LIVE_BID",
     49.5, 50.5, 500, 1000, 0.70, 3, "confluence", "2026-08-05T10:00:00"),
]

# One synthetic-era row (price_source NULL) that every live query must
# exclude, and one live-but-unscored row that compare_strategies() must
# exclude while the portfolio statistics still count it.
EXTRA = [
    ("XXX", "XXX-CE", 100, 10.0, 99.0, 8900.0, "Square-off 15:15", None,
     9.9, 10.1, 500, 1000, 0.99, 1, "confluence", "2026-07-01T10:00:00"),
    ("YYY", "YYY-PE", 100, 10.0, 12.0, 200.0, "Square-off 15:15", "LIVE_BID",
     9.9, 10.1, 500, 1000, None, None, "confluence", "2026-07-31T10:00:00"),
]


def fixture_conn():
    """In-memory database shaped like the real options_trades table."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE options_trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, trading_symbol TEXT, qty INTEGER,
            entry_price REAL, exit_price REAL, pnl REAL, reason TEXT,
            price_source TEXT, entry_bid REAL, entry_ask REAL,
            entry_oi INTEGER, entry_volume INTEGER,
            entry_score REAL, entry_rank INTEGER, entry_tier TEXT,
            exit_time TEXT)""")
    cols = ("symbol,trading_symbol,qty,entry_price,exit_price,pnl,reason,"
            "price_source,entry_bid,entry_ask,entry_oi,entry_volume,"
            "entry_score,entry_rank,entry_tier,exit_time")
    ph = ",".join("?" * 16)
    for row in FIXTURE + EXTRA:
        conn.execute(f"INSERT INTO options_trades({cols}) VALUES({ph})", row)
    conn.commit()
    return conn


def drawdown(pnls):
    """Reference implementation, deliberately independent of stats()."""
    eq = pk = dd = 0.0
    for p in pnls:
        eq += p
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    return round(dd, 2)


# ----------------------------------------------------------------- tests ---
def test_stats_numeric_contract():
    """stats() must keep its order-invariant numbers exactly."""
    print("\n[1] stats() order-invariant contract")
    pnls = [r[5] for r in FIXTURE]          # +100 -500 +50 -300 +200
    s = ta.stats(pnls)
    check("n", s["n"], 5)
    check("net_pnl", s["net_pnl"], -450.0)
    check("win_rate", s["win_rate"], 60.0)
    check("avg_winner", s["avg_winner"], round(350 / 3, 2))
    check("avg_loser", s["avg_loser"], -400.0)
    check("profit_factor", s["profit_factor"], round(350 / 800, 3))
    check("expectancy", s["expectancy"], -90.0)
    check("sufficient (n < 30)", s["sufficient"], False)


def test_drawdown_is_order_dependent():
    """Pins the exact difference V2-R1 corrects."""
    print("\n[2] drawdown depends on ordering")
    chrono = [r[5] for r in FIXTURE]
    by_score = [r[5] for r in sorted(FIXTURE, key=lambda r: -r[12])]
    check("chronological drawdown", drawdown(chrono), 750.0)
    check("score-ordered drawdown", drawdown(by_score), 800.0)
    check("the two differ", drawdown(chrono) != drawdown(by_score), True)
    check("stats() honours the order it is given",
          ta.stats(chrono)["max_drawdown"], 750.0)


def test_compare_strategies_population_and_spearman():
    """Population selection and Spearman must not change."""
    print("\n[3] compare_strategies(): population + Spearman")
    conn = fixture_conn()
    cmp_ = ta.compare_strategies(conn)
    check("n (live AND scored only)", cmp_["n"], 5)
    check("net_pnl", cmp_["baseline_all"]["net_pnl"], -450.0)
    check("win_rate", cmp_["baseline_all"]["win_rate"], 60.0)
    check("profit_factor", cmp_["baseline_all"]["profit_factor"],
          round(350 / 800, 3))
    check("expectancy", cmp_["baseline_all"]["expectancy"], -90.0)
    # score desc: .90(+100) .80(+50) .70(+200) .60(-300) .50(-500)
    # pnl  desc: +200 +100 +50 -300 -500 -> ranks 1,2,0,3,4
    # d^2 = 1+1+4+0+0 = 6 -> rho = 1 - 36/120 = 0.7
    check("spearman", cmp_["spearman"], 0.7)
    check("comparison_sufficient (n < 100)",
          cmp_["comparison_sufficient"], False)
    conn.close()


def test_live_trades_excludes_synthetic():
    """price_source IS NULL rows must never enter live statistics."""
    print("\n[4] synthetic-era exclusion")
    conn = fixture_conn()
    rows = ta.live_trades(conn)
    check("live rows (5 scored + 1 unscored)", len(rows), 6)
    check("synthetic row excluded",
          all(r[0] != "XXX" for r in rows), True)
    check("unscored live row included",
          any(r[0] == "YYY" for r in rows), True)
    conn.close()


def test_spread_cost_and_direction_pnl():
    """V2-R5 must not alter these numbers - label only."""
    print("\n[5] spread cost + direction P&L")
    conn = fixture_conn()
    rows = [r for r in ta.live_trades(conn) if r[0] == "BBB"]
    a = ta.attribute(rows[0])
    # bid 19.8 ask 20.2 -> mid 20.0 -> half-spread 0.2 x 100 = 20.0
    check("spread_cost (entry half-spread x qty)", a["spread_cost"], 20.0)
    check("spread_pct", a["spread_pct"], 2.0)
    check("direction_pnl = pnl + spread_cost", a["direction_pnl"], -480.0)
    check("spread_cost key name preserved", "spread_cost" in a, True)
    # half-spreads x qty: AAA 10 + BBB 20 + CCC 30 + DDD 40 + EEE 50 + YYY 10
    total = sum(ta.attribute(r)["spread_cost"] for r in ta.live_trades(conn)
                if ta.attribute(r)["spread_cost"] is not None)
    check("total spread across live rows", round(total, 2), 160.0)
    conn.close()


def test_no_top_k_in_output():
    """V2-R3: the historical top-k comparison must be gone entirely."""
    print("\n[6] top-k comparison removed")
    conn = fixture_conn()
    cmp_ = ta.compare_strategies(conn)
    for k in (1, 2, 3):
        check(f"top_{k} absent from result", f"top_{k}" in cmp_, False)
    src = (HERE / "trade_analytics.py").read_text(encoding="utf-8")
    check("no 'ranked top-' rendering in source",
          "ranked top-" in src, False)
    conn.close()


def test_report_labels():
    """V2-R2 / V2-R4 / V2-R5 are rendering changes.

    Asserted against the module source rather than by executing
    build_report(), because build_report() calls ensure_schema(), which
    performs a schema migration - explicitly out of scope here.
    """
    print("\n[7] report labelling")
    src = (HERE / "trade_analytics.py").read_text(encoding="utf-8")
    check("V2-R2: misleading 'shadow evidence' header gone",
          "BASELINE vs RANKED (shadow evidence)" in src, False)
    check("V2-R2: ranking_log exclusion stated",
          "ranking_log" in src, True)
    check("V2-R4: CUMULATIVE scope labelled", "CUMULATIVE" in src, True)
    check("V2-R5: entry-side half-spread named",
          "ENTRY-SIDE HALF-SPREAD" in src.upper(), True)
    check("V2-R5: exclusions noted",
          "excludes" in src.lower(), True)


if __name__ == "__main__":
    for fn in (test_stats_numeric_contract,
               test_drawdown_is_order_dependent,
               test_compare_strategies_population_and_spearman,
               test_live_trades_excludes_synthetic,
               test_spread_cost_and_direction_pnl,
               test_no_top_k_in_output,
               test_report_labels):
        fn()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All trade-analytics characterization tests passed.")
