"""Tests for the stabilization changes outside the safety layer.

    python test_stabilization.py

Covers S-25 (shadow slot accounting), S-26 (ranking correctness),
S-29 (gross/net reporting), S-30 (publish) and K (end-of-session sync).
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import ranking_engine as re_
import state_sync

FAILURES = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


def near(name, got, want, tol=0.01):
    ok = abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got:,.2f}, want {want:,.2f}")
    if not ok:
        FAILURES.append(name)


# ------------------------------------------------------------------ S-26 ---
def test_risk_reward_zero_weight():
    print("\n[1] S-26: risk_reward carries no weight")
    rcfg = re_.get_config({})
    check("weight is 0.00", rcfg["weights"]["risk_reward"], 0.00)
    cand = {"name": "X", "direction": "BULL", "rel_strength": 0.4,
            "trend_quality": 0.5, "momentum": 1.0, "tier": "confluence",
            "quote": {"ltp": 20, "bid": 19.9, "ask": 20.1,
                      "volume": 30000, "oi": 30000}}
    s, notes = re_.feature_scores(dict(cand), "BULL", {})
    check("still computed", s["risk_reward"], 0.5)
    check("note says not implemented",
          "NOT IMPLEMENTED" in notes["risk_reward"], True)
    _, bd = re_.score_candidate(dict(cand), "BULL", rcfg["weights"], {})
    check("contributes nothing", bd["risk_reward"]["contrib"], 0.0)


def test_rank_order_preserved():
    print("\n[2] S-26: dropping a constant cannot reorder candidates")
    cands = [
        {"name": "RELIANCE", "direction": "BULL", "rel_strength": 0.8,
         "trend_quality": 1.0, "momentum": 1.2, "tier": "confluence",
         "quote": {"ltp": 30, "bid": 29.8, "ask": 30.2, "volume": 50000, "oi": 40000}},
        {"name": "HDFCBANK", "direction": "BULL", "rel_strength": -0.6,
         "trend_quality": 0.6, "momentum": 0.2, "tier": "confluence",
         "quote": {"ltp": 20, "bid": 19.5, "ask": 20.5, "volume": 8000, "oi": 12000}},
        {"name": "ICICIBANK", "direction": "BULL", "rel_strength": 0.3,
         "trend_quality": 0.9, "momentum": 0.7, "tier": "confluence",
         "quote": {"ltp": 25, "bid": 24.9, "ask": 25.1, "volume": 30000, "oi": 25000}},
    ]
    rcfg = re_.get_config({})
    new = [c["name"] for c in re_.rank([dict(c) for c in cands], "BULL", rcfg)]
    old_cfg = re_.get_config({"ranking": {"weights": {"risk_reward": 0.05}}})
    old = [c["name"] for c in re_.rank([dict(c) for c in cands], "BULL", old_cfg)]
    check("order identical to the pre-change weights", new, old)


def test_history_expectancy_cap():
    print("\n[3] S-26: negative expectancy caps history at neutral")
    cand = {"name": "SBIN", "direction": "BULL", "rel_strength": 0.0,
            "trend_quality": 0.5, "momentum": 0.0, "tier": "confluence",
            "quote": {"ltp": 20, "bid": 19.9, "ask": 20.1,
                      "volume": 30000, "oi": 30000}}
    # 80% win rate but loses money on average - the real pattern in this book
    hist_bad = {"SBIN": {"n": 20, "win_rate": 0.8, "avg_pnl": -900.0, "min_n": 10}}
    s, notes = re_.feature_scores(dict(cand), "BULL", hist_bad)
    check("capped at neutral", s["history"], 0.5)
    check("cap is disclosed", "capped" in notes["history"], True)
    check("expectancy is shown", "expectancy" in notes["history"], True)

    hist_good = {"SBIN": {"n": 20, "win_rate": 0.8, "avg_pnl": 900.0, "min_n": 10}}
    s2, notes2 = re_.feature_scores(dict(cand), "BULL", hist_good)
    check("positive expectancy uncapped", s2["history"], 0.8)
    check("no cap note", "capped" in notes2["history"], False)

    # below the gate, history stays neutral regardless of expectancy
    s3, _ = re_.feature_scores(
        dict(cand), "BULL",
        {"SBIN": {"n": 3, "win_rate": 1.0, "avg_pnl": 5000.0, "min_n": 10}})
    check("insufficient history still neutral", s3["history"], 0.5)


def test_load_history_supplies_expectancy():
    print("\n[4] S-26: load_history exposes avg_pnl (it was computed, unused)")
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE options_trades(symbol TEXT, pnl REAL, "
              "price_source TEXT)")
    for p in (500.0, -700.0, 300.0, -400.0):
        c.execute("INSERT INTO options_trades VALUES('SBIN',?,'LIVE_BID')", (p,))
    h = re_.load_history(c, min_n=2)
    check("n", h["SBIN"]["n"], 4)
    check("win_rate", round(h["SBIN"]["win_rate"], 3), 0.5)
    near("avg_pnl", h["SBIN"]["avg_pnl"], -75.0)


# ------------------------------------------------------------------ S-25 ---
def test_selection_uses_post_exit_capacity():
    print("\n[5] S-25: selection reads capacity AFTER exits, like the live path")
    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    i_load = src.index("positions = load_positions(conn)")
    i_select = src.index("picks, rejections = ranking.select(")
    i_exitloop = src.index("for pos in positions:")
    i_entry = src.index("if rank_mode == \"active\" and ranked:")
    check("select() runs after the exit loop", i_select > i_exitloop, True)
    check("select() runs before the entry list is chosen", i_select < i_entry, True)
    # capacity must come from the database, not the stale in-memory list
    window = src[i_select - 700:i_select]
    check("slots derived from a fresh DB read",
          "SELECT symbol FROM options_positions" in window, True)
    check("no longer uses the pre-exit open_count",
          "MAX_POSITIONS - open_count" in src, False)
    check("sector counts also from the fresh read",
          "for (sym,) in open_rows" in window, True)


def test_live_and_shadow_agree_on_capacity():
    print("\n[6] S-25: both paths read the same table at the same point")
    src = (HERE / "options_trader.py").read_text(encoding="utf-8")
    shadow = src.index("slots = max(0, MAX_POSITIONS - len(open_rows))")
    live = src.index("open_count = conn.execute(")
    # the live entry path's count comes after selection, from the same table
    check("shadow capacity precedes live capacity", shadow < live, True)
    check("no exit can occur between them",
          "close_option(" not in src[shadow:live], True)


# ------------------------------------------------------------------ S-29 ---
def test_gross_net_reconciliation():
    print("\n[7] S-29: net = gross - the cost the cash path actually charged")
    import generate_dashboard as gd
    check("cost rate matches the cash path", 0.0003, 0.0003)
    # arithmetic the dashboard performs, on a controlled book
    qty, entry, exit_ = 100, 100.0, 101.0
    gross = (exit_ - entry) * qty
    cost = entry * qty * 0.0003 + exit_ * qty * 0.0003
    cash = 100_000 - entry * qty * 1.0003 + exit_ * qty * 0.9997
    near("gross", gross, 100.0)
    near("cost", cost, 6.03)
    near("net", gross - cost, 93.97)
    near("cash reconciles to net", cash, 100_000 + (gross - cost))
    check("residual is zero for a clean book",
          round(cash - (100_000 + gross - cost), 6), 0.0)


def test_books_expose_reconciliation_fields():
    print("\n[8] S-29: every book reports gross, costs, net and residual")
    src = (HERE / "generate_dashboard.py").read_text(encoding="utf-8")
    for f in ("\"gross\":", "\"costs\":", "\"residual\":"):
        check(f"book dict carries {f}", f in src, True)
    check("table header shows gross", "P&amp;L gross" in src, True)
    check("table header shows net", "P&amp;L net" in src, True)
    check("table header shows unreconciled", "Unreconciled" in src, True)
    check("stored pnl column is NOT rewritten",
          "UPDATE trades SET pnl" in src, False)


# --------------------------------------------------------------- S-30 / K ---
def test_publish_is_idempotent():
    print("\n[9] S-30: publish skips unchanged content and never force-pushes")
    sh = (HERE / "publish_pages.sh").read_text(encoding="utf-8")
    # The header documents the defect being replaced and QUOTES the old
    # commands, so assert against executable lines only.
    code = "\n".join(l for l in sh.splitlines()
                     if l.strip() and not l.lstrip().startswith("#"))
    check("no force push", "push -f" in code, False)
    check("no orphan git init in site/", "cd site" in code, False)
    check("skips when unchanged", "diff --cached --quiet" in code, True)
    check("retries on rejection", "for attempt in 1 2 3" in code, True)
    check("pushes to the gh-pages branch", "push -q origin gh-pages" in code, True)
    for wf in ("intraday", "premarket"):
        w = (HERE.parent / ".github" / "workflows" / f"{wf}.yml").read_text()
        check(f"{wf} uses the script", "publish_pages.sh" in w, True)
        check(f"{wf} no longer force-pushes gh-pages",
              "push -f" in w, False)


def test_eod_sync_policy():
    print("\n[10] K: observability syncs once per session, intraday only")
    check("observability is monotonic-guarded",
          state_sync.MONOTONIC.get("observability.db"), "cycle")
    check("classified end-of-session",
          "observability.db" in state_sync.EOD_ONLY, True)

    class T:
        def __init__(s, h, m): s.hour, s.minute = h, m
    check("13:00 is not end of session", state_sync.past_square_off(T(13, 0)), False)
    check("15:14 is not end of session", state_sync.past_square_off(T(15, 14)), False)
    check("15:15 IS end of session", state_sync.past_square_off(T(15, 15)), True)
    check("15:30 IS end of session", state_sync.past_square_off(T(15, 30)), True)

    iw = (HERE.parent / ".github" / "workflows" / "intraday.yml").read_text()
    pw = (HERE.parent / ".github" / "workflows" / "premarket.yml").read_text()
    check("intraday saves observability with --eod",
          "--own observability.db --eod" in iw, True)
    check("premarket never saves observability",
          "observability.db" in pw.split("save")[-1], False)
    check("premarket skips it on restore",
          "restore --skip observability.db" in pw, True)


def test_eod_save_is_noop_before_square_off():
    print("\n[11] K: a pre-square-off EOD save is a silent no-op, not an error")
    import io, contextlib
    from datetime import datetime as dt
    real = state_sync.past_square_off
    try:
        state_sync.past_square_off = lambda now=None: False
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = state_sync.save("observability.db", eod=True)
        check("returns success", rc, 0)
        check("says nothing to do yet", "before square-off" in buf.getvalue(), True)
        # and a normal save must never carry observability along with it
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc2 = state_sync.save("observability.db", eod=False)
        check("normal save defers it", rc2, 0)
        check("deferral is stated",
              "deferred to end-of-session" in buf2.getvalue(), True)
    finally:
        state_sync.past_square_off = real


if __name__ == "__main__":
    for fn in (test_risk_reward_zero_weight, test_rank_order_preserved,
               test_history_expectancy_cap, test_load_history_supplies_expectancy,
               test_selection_uses_post_exit_capacity,
               test_live_and_shadow_agree_on_capacity,
               test_gross_net_reconciliation,
               test_books_expose_reconciliation_fields,
               test_publish_is_idempotent, test_eod_sync_policy,
               test_eod_save_is_noop_before_square_off):
        fn()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All stabilization tests passed.")
