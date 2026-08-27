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

# Third state, alongside PASS and FAIL. A check whose PRECONDITION does not
# hold has not been answered, and answering it either way is a lie: FAIL
# blames the build for the environment, PASS claims evidence that was never
# gathered. NOT_EXERCISED records that the question was not put.
NOT_EXERCISED = None


def step(name, ok, detail=""):
    """Record one checklist result.

    `ok` is True (PASS), False (FAIL), or NOT_EXERCISED (the precondition
    did not hold). NOT_EXERCISED is NON-FAILING and is counted separately -
    it is never folded into the pass count, so it cannot become a silent
    pass.
    """
    RESULTS.append((name, ok, detail))
    label = "PASS" if ok is True else ("FAIL" if ok is False else "N/EX")
    print(f"  {label}  {name}" + (f"  [{detail}]" if detail else ""))


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
        # Answerable only if the cycle actually asked the broker for
        # something. Outside 09:30-14:30 with a flat book there are no
        # positions, no candidates and no post-exit watches, so the token
        # list is EMPTY BY CONSTRUCTION and "0 quotes" is the correct
        # result, not a failure.
        #
        # The precondition is read from WHAT WAS REQUESTED, never from the
        # clock: a clock test would pass vacuously whenever the window is
        # open but the book still happens to be empty, which is the same
        # defect wearing a different hat.
        _req = _tokens_requested(ot.DB, hb)
        if _req == 0:
            step("live quotes fetched", NOT_EXERCISED,
                 "no tokens requested - nothing to fetch")
        else:
            step("live quotes fetched", (hb[3] or 0) > 0,
                 f"{hb[3]} of {_req} tokens")
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
    failed = [n for n, ok, _ in RESULTS if ok is False]
    skipped = [n for n, ok, _ in RESULTS if ok is NOT_EXERCISED]
    passed = [n for n, ok, _ in RESULTS if ok is True]
    print("=" * 70)
    if failed:
        print(f"SMOKE TEST: FAIL ({len(failed)} of {len(RESULTS)}"
              + (f", {len(skipped)} not exercised" if skipped else "") + ")")
        for n in failed:
            print(f"   FAILED: {n}")
        for n in skipped:
            print(f"   NOT EXERCISED: {n}")
        if crashed:
            print()
            print(crashed)
        print("SECTION 34: NO-GO")
        return 1
    # `satisfied` requires ZERO FAILURES. It does NOT require zero
    # not-exercised items: outside the entry window several checks have no
    # answerable precondition, and that is the architecture working.
    print(f"SMOKE TEST: PASS ({len(passed)} of {len(RESULTS)}"
          + (f", {len(skipped)} not exercised" if skipped else "") + ")")
    for n in skipped:
        print(f"   NOT EXERCISED: {n}")
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


def _tokens_requested(trade_db, hb):
    """How many tokens the cycle would have asked the broker for.

    Mirrors the token list options_trader.process() builds, from recorded
    state rather than from the clock:

        open positions carrying a token
      + this cycle's candidates              (heartbeat.candidates_generated)
      + contracts still inside their 60-minute post-exit window

    Only zero-versus-non-zero is used, so overlap between the three sources
    does not matter and no double counting can mislead. Unreadable state
    returns a positive count, so an unanswerable precondition can never be
    manufactured by a failure to look.
    """
    n = 0
    r = _last(trade_db, "SELECT COUNT(*) FROM options_positions "
                        "WHERE token IS NOT NULL AND token != ''")
    n += r[0] if r else 1
    try:
        n += int(hb[4] or 0)
    except (TypeError, ValueError, IndexError):
        n += 1
    r = _last(trade_db, "SELECT value FROM meta WHERE key='post_exit_watch'")
    if r and r[0]:
        try:
            n += len(__import__("json").loads(r[0]))
        except (ValueError, TypeError):
            n += 1
    return n


if __name__ == "__main__":
    sys.exit(main())
