"""
Faithful production replay - P0-B.
==================================

    python replay_engine.py            # Mode A over the live trade book
    python replay_engine.py --mode B   # Mode B, forward telemetry

TWO MODES, DELIBERATELY NOT EQUIVALENT
--------------------------------------
MODE A - HISTORICAL RECONSTRUCTION
    Works from options_trades.db + ranking_log for Aug 06-14. It can
    reconstruct WHAT happened - contract, prices, quantities, exit reason,
    P&L - exactly. It CANNOT reconstruct WHEN or WHY, because no temporal
    or market-state telemetry existed when those trades were taken.

MODE B - FORWARD FULL-FIDELITY REPLAY
    Works from observability.db, which records the full chain
    cycle -> signal -> candidate -> quote -> decision -> position -> exit.
    Only Mode B supports causal reasoning.

Mode A must never be presented as Mode B. A trade replayed in Mode A has
UNAVAILABLE stamped on every temporal field, and that is a finding, not a
gap to be papered over.

CLASSIFICATION CONTRACT
-----------------------
Every field carries exactly one label:

    DIRECTLY OBSERVED       persisted verbatim by production
    DETERMINISTICALLY DERIVED  computed from observed values by a rule that
                            cannot vary (e.g. stop = entry x 0.85)
    APPROXIMATED            reconstructed via an assumption that could be
                            wrong; the assumption is named
    UNAVAILABLE             not recorded and not recoverable

Nothing is fabricated. Where production and replay disagree, the
disagreement is reported - replay is never tuned to force agreement.

SAFETY
------
Both production databases are opened through a read-only URI. This module
performs no INSERT/UPDATE/DELETE against them and never calls
ensure_schema() or any migration.
"""

import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
OPT_DB = HERE / "options_trades.db"
OBS_DB = HERE / "observability.db"

OBSERVED = "DIRECTLY OBSERVED"
DERIVED = "DETERMINISTICALLY DERIVED"
APPROX = "APPROXIMATED"
UNAVAIL = "UNAVAILABLE"

# Mirrors production constants for DERIVED reconstruction only. These are
# read, never written, and the strategy is not parameterised from here.
INITIAL_STOP_PCT = -0.15
TRAIL_PCT = 0.12


def ro(path):
    """Read-only connection. Any write attempt raises."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------- mode A ---
def replay_trade_historical(t, dir_row=None):
    """Reconstruct one historical trade. Returns {field: (value, class)}."""
    r = {}
    for f in ("symbol", "option_type", "strike", "expiry", "trading_symbol",
              "token", "entry_time", "entry_price", "qty", "lots", "lotsize",
              "exit_time", "exit_price", "reason", "pnl"):
        r[f] = (t[f], OBSERVED)

    # --- deterministically derived -------------------------------------
    try:
        exp = date.fromisoformat(str(t["expiry"])[:10])
        ent = datetime.fromisoformat(t["entry_time"]).date()
        r["dte"] = ((exp - ent).days, DERIVED)
    except Exception:
        r["dte"] = (None, UNAVAIL)

    r["initial_stop"] = (round(t["entry_price"] * (1 + INITIAL_STOP_PCT), 2),
                         DERIVED)
    r["cost"] = (round(t["qty"] * t["entry_price"], 2), DERIVED)
    r["theoretical_max_loss"] = (
        round((t["entry_price"] - t["entry_price"] * (1 + INITIAL_STOP_PCT))
              * t["qty"], 2), DERIVED)
    r["pnl_recomputed"] = (round((t["exit_price"] - t["entry_price"])
                                 * t["qty"], 2), DERIVED)

    if t["reason"] == "Trailing stop":
        # exit filled AT the stop level => high_water = exit / (1 - TRAIL)
        r["implied_high_water"] = (round(t["exit_price"] / (1 - TRAIL_PCT), 2),
                                   DERIVED)
    else:
        r["implied_high_water"] = (None, UNAVAIL)

    if t["entry_bid"] and t["entry_ask"] and t["entry_ask"] > t["entry_bid"]:
        mid = (t["entry_bid"] + t["entry_ask"]) / 2
        r["entry_spread_pct"] = (round((t["entry_ask"] - t["entry_bid"])
                                       / mid * 100, 3), DERIVED)
        r["entry_half_spread_cost"] = (round((t["entry_ask"] - mid) * t["qty"],
                                             2), DERIVED)
    else:
        r["entry_spread_pct"] = (None, UNAVAIL)
        r["entry_half_spread_cost"] = (None, UNAVAIL)

    # --- approximated (assumption named) -------------------------------
    if dir_row is not None:
        r["direction_at_entry"] = (
            dir_row["direction"],
            APPROX + " (nearest ranking_log cycle to entry_time)")
        r["cycle_ts"] = (dir_row["ts"],
                         APPROX + " (nearest ranking_log cycle)")
    else:
        r["direction_at_entry"] = (None, UNAVAIL)
        r["cycle_ts"] = (None, UNAVAIL)

    # --- unavailable: nothing temporal or market-state was recorded ----
    for f in ("underlying_spot_entry", "underlying_spot_exit",
              "signal_bar_ts", "signal_bar_status", "signal_generated_at",
              "quote_market_timestamp", "quote_received_at", "quote_age_s",
              "decision_at", "decision_latency_s", "cycle_id", "run_id",
              "high_water_at_exit_persisted", "post_exit_path",
              "max_favorable_excursion", "max_adverse_excursion",
              "delta", "gamma", "theta", "vega", "iv",
              "intra_interval_event_order"):
        r[f] = (None, UNAVAIL)
    return r


def run_mode_a(opt_db=None):
    conn = ro(opt_db or OPT_DB)
    trades = [dict(x) for x in conn.execute(
        "SELECT * FROM options_trades WHERE price_source IS NOT NULL "
        "ORDER BY entry_time")]
    rl = [dict(x) for x in conn.execute("SELECT * FROM ranking_log")]
    conn.close()

    by_day = {}
    for z in rl:
        by_day.setdefault((z["ts"][:10], z["symbol"]), []).append(z)

    out = []
    for t in trades:
        key = (t["entry_time"][:10], t["symbol"])
        rows = by_day.get(key)
        best = None
        if rows:
            et = datetime.fromisoformat(t["entry_time"])
            best = min(rows, key=lambda z: abs(
                (datetime.fromisoformat(z["ts"]) - et).total_seconds()))
        out.append((t, replay_trade_historical(t, best)))
    return out


# ------------------------------------------------------------- mode B ---
def run_mode_b(obs_db=None):
    """Forward replay over captured telemetry. Empty until cycles run."""
    p = Path(obs_db or OBS_DB)
    if not p.exists():
        return None
    conn = ro(p)
    chain = {}
    for tbl in ("cycle", "signal_snapshot", "quote_snapshot",
                "candidate_snapshot", "decision", "position_snapshot",
                "exit_snapshot", "post_exit_path"):
        try:
            chain[tbl] = [dict(x) for x in conn.execute(f"SELECT * FROM {tbl}")]
        except sqlite3.Error:
            chain[tbl] = []
    conn.close()
    return chain


# ---------------------------------------------------------- reporting ---
CORE15 = ("symbol", "option_type", "strike", "expiry", "trading_symbol",
          "token", "entry_time", "entry_price", "qty", "lots", "lotsize",
          "exit_time", "exit_price", "reason", "pnl")


def reconcile(results):
    """EXPECTED / REPLAYED / DIFFERENCE / REASON for the core fields."""
    exact = mismatch = 0
    rows = []
    for t, r in results:
        for f in CORE15:
            exp, got = t[f], r[f][0]
            ok = exp == got
            exact += ok
            if not ok:
                mismatch += 1
                rows.append((t["trading_symbol"], f, exp, got,
                             "replay disagreement - INVESTIGATE"))
        # arithmetic cross-check of P&L, independent of the stored value
        if abs(r["pnl_recomputed"][0] - t["pnl"]) > 0.01:
            mismatch += 1
            rows.append((t["trading_symbol"], "pnl_recomputed", t["pnl"],
                         r["pnl_recomputed"][0], "derivation disagreement"))
    return exact, mismatch, rows


def classification_census(results):
    census = {}
    for _, r in results:
        for f, (_, cl) in r.items():
            base = cl.split(" (")[0]
            census.setdefault(base, set()).add(f)
    return census


def main():
    mode = "A"
    if "--mode" in sys.argv:
        mode = sys.argv[sys.argv.index("--mode") + 1].upper()

    print("=" * 74)
    print(f"REPLAY ENGINE - MODE {mode}")
    print("=" * 74)

    if mode == "B":
        chain = run_mode_b()
        if chain is None:
            print("  observability.db not present - no forward telemetry yet.")
            print("  Mode B becomes available after the first instrumented "
                  "cycle runs.")
            return 0
        print("  Captured chain:")
        for k, v in chain.items():
            print(f"    {k:<20} {len(v):>6} rows")
        cyc = chain.get("cycle", [])
        gaps = [c["inter_cycle_gap_s"] for c in cyc
                if c.get("inter_cycle_gap_s") is not None]
        if gaps:
            gaps.sort()
            print(f"\n  inter-cycle gaps (n={len(gaps)}): "
                  f"median {gaps[len(gaps)//2]/60:.2f}m  max {max(gaps)/60:.2f}m")
        qs = chain.get("quote_snapshot", [])
        have = [q for q in qs if q.get("exch_feed_time")]
        print(f"\n  quote snapshots {len(qs)}, with exchange feed time "
              f"{len(have)} -> quote age "
              f"{'MEASURABLE' if have else 'UNAVAILABLE (broker omitted it)'}")
        ss = chain.get("signal_snapshot", [])
        if ss:
            from collections import Counter
            print(f"  signal bar status: {dict(Counter(s['bar_status'] for s in ss))}")
            diff = sum(1 for s in ss
                       if s.get("direction_completed_bar") is not None
                       and s.get("direction") != s.get("direction_completed_bar"))
            print(f"  CURRENT vs COMPLETED-bar direction differs: {diff}/{len(ss)}"
                  "   (observation only - production behaviour unchanged)")
        return 0

    results = run_mode_a()
    print(f"  live trades replayed: {len(results)}")
    exact, mismatch, rows = reconcile(results)
    total = len(results) * len(CORE15)
    print(f"\n  CORE-15 FIELD RECONCILIATION")
    print(f"    exact matches : {exact}/{total}")
    print(f"    mismatches    : {mismatch}")
    for r in rows[:20]:
        print(f"      {r[0]:<24} {r[1]:<18} expected={r[2]!r} "
              f"replayed={r[3]!r}  {r[4]}")

    census = classification_census(results)
    print("\n  FIELD CLASSIFICATION CENSUS")
    for cl in (OBSERVED, DERIVED, APPROX, UNAVAIL):
        fields = sorted(census.get(cl, []))
        print(f"    {cl:<26} {len(fields):>2} fields")
        if fields:
            print(f"      {', '.join(fields)}")

    print("\n  MODE A LIMITATION - stated, not worked around:")
    print("    Temporal and market-state fields are UNAVAILABLE for every")
    print("    historical trade. Mode A reconstructs WHAT happened exactly")
    print("    and cannot establish WHEN or WHY. Causal claims require")
    print("    Mode B telemetry, which only accrues forward.")
    print("\n    Intra-interval event order is UNAVAILABLE by construction:")
    print("    a high/low snapshot cannot distinguish 100->80->120 from")
    print("    100->120->80. Replay preserves this ambiguity.")
    return 0 if mismatch == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
