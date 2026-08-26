"""SECTION 34 pre-open smoke test: one full cycle, entries DISABLED.

    OPTIONS_DRY_RUN=1 python smoke_test.py

Fifteen changes landed across the safety-critical path at once, and a green
unit-test suite does not establish that the process BOOTS in the production
environment. This runs the real `options_trader.main()` against live data
with entries refused at the final authorization, then inspects the
observability store and reports PASS/FAIL per section 34's checklist.

It refuses to run unless the dry run is actually armed, so it can never
place an order by accident.

EXITS ARE NOT DISABLED. `stabilization.authorize` returns PASS for any
non-ENTRY intent before the dry-run flag is read, so a position open when
the smoke test runs is still risk-managed and still squared off. That is
deliberate: a "safe" mode that stranded a position would be the exact
failure this package exists to prevent.
"""

import os
import sqlite3
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

RESULTS = []


def step(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail else ""))


def main():
    import stabilization as stab
    cfg = stab.get_config(__import__("json").loads(
        (HERE / "intraday_config.json").read_text(encoding="utf-8")))
    if not cfg["dry_run"]:
        print("REFUSING TO RUN: dry run is not armed.")
        print("Set OPTIONS_DRY_RUN=1 (or stabilization.dry_run=true) first.")
        print("This guard exists so the smoke test can never submit an order.")
        return 2

    print("=" * 70)
    print("SECTION 34 PRE-OPEN SMOKE TEST - entries DISABLED, exits ACTIVE")
    print("=" * 70)
    print(f"  policy: gates={'ON' if cfg['enabled'] else 'OFF'} "
          f"max_pos={cfg['max_option_positions']} min_dte={cfg['min_dte']} "
          f"max_spread={cfg['max_entry_spread_pct']}% "
          f"max_bar_age={cfg['max_signal_bar_age_s']}s dry_run=True")
    print()

    import telemetry
    import options_trader as ot

    obs = telemetry.DB
    before = _counts(obs)
    trades_before = _trade_count(ot.DB)

    crashed = None
    try:
        ot.main()
    except Exception:
        crashed = traceback.format_exc()

    print()
    print("-" * 70)
    print("CHECKLIST")
    print("-" * 70)
    step("process starts and completes", crashed is None,
         "" if crashed is None else crashed.strip().splitlines()[-1])

    after = _counts(obs)
    step("state restored (a trading book was readable)",
         trades_before is not None,
         f"{trades_before} closed option trades" if trades_before is not None
         else "options_trades.db unreadable")
    step("cycle heartbeat recorded",
         after.get("cycle_heartbeat", 0) > before.get("cycle_heartbeat", 0))
    step("gate ledger recorded",
         after.get("gate_ledger", 0) > before.get("gate_ledger", 0))

    hb = _last(obs, "SELECT auth_ok,master_ok,signals_ok,quotes_fetched,"
                    "candidates_generated,gates_evaluated,open_positions,"
                    "entry_window FROM cycle_heartbeat ORDER BY id DESC "
                    "LIMIT 1")
    if hb:
        step("Angel One authentication succeeded", hb[0] == 1)
        step("instrument master available", hb[1] == 1)
        step("signal data fetched", hb[2] == 1)
        step("live quotes fetched", (hb[3] or 0) > 0, f"{hb[3]} tokens")
        step("candidates generated and counted", hb[4] is not None,
             f"{hb[4]} candidates")
        step("all gates evaluated", hb[5] == 1)
    else:
        step("heartbeat readable", False, "no heartbeat row")

    gl = _last(obs, "SELECT candidates_generated,passed_to_selection,entered,"
                    "identities_ok,detail FROM gate_ledger ORDER BY id DESC "
                    "LIMIT 1")
    if gl:
        step("gate ledger identities hold", gl[3] == 1, gl[4] or "")
        step("NO entry was submitted", gl[2] == 0,
             f"entered={gl[2]}")
    else:
        step("gate ledger readable", False, "no ledger row")

    trades_after = _trade_count(ot.DB)
    step("no new trade row was written by the dry run",
         trades_after == trades_before,
         f"{trades_before} -> {trades_after}")
    pos = _positions(ot.DB)
    step("open-position count is readable", pos is not None,
         f"{pos} open" if pos is not None else "")

    print()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("=" * 70)
    if failed:
        print(f"SMOKE TEST: FAIL ({len(failed)} of {len(RESULTS)})")
        for n in failed:
            print(f"   FAILED: {n}")
        if crashed:
            print()
            print(crashed)
        print("SECTION 34: NO-GO")
        return 1
    print(f"SMOKE TEST: PASS ({len(RESULTS)} checks)")
    print("SECTION 34: satisfied")
    return 0


def _counts(db):
    out = {}
    if not Path(db).exists():
        return out
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for t in ("cycle", "cycle_heartbeat", "gate_ledger", "decision",
                  "candidate_snapshot", "post_exit_path", "trade_cost"):
            try:
                out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                out[t] = 0
        c.close()
    except sqlite3.Error:
        pass
    return out


def _last(db, sql):
    if not Path(db).exists():
        return None
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return c.execute(sql).fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return None


def _trade_count(db):
    r = _last(db, "SELECT COUNT(*) FROM options_trades")
    return r[0] if r else None


def _positions(db):
    r = _last(db, "SELECT COUNT(*) FROM options_positions")
    return r[0] if r else None


if __name__ == "__main__":
    sys.exit(main())
