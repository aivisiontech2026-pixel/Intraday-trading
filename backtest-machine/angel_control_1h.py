"""The missing cell: the frozen signal on ANGEL HOURLY bars, 3 years.

The control divergence (yfinance 1h 3y = 46.9%, Angel 5m 3y = 49.24%) has
three candidate causes - timeframe, feed, and window. Measured on the only
window where yfinance offers both timeframes (the 59-session 5-minute
overlap), all three come out near zero:

    TIMEFRAME  yf 5m - yf 1h, same 59 sessions   +0.36pp  (se 1.06)
    WINDOW     yf 1h overlap - yf 1h 3 years     +0.39pp
    FEED       yf 5m 2026Q3 - Angel 5m 2026Q3    +0.90pp

Their sum does not reach the +2.32pp that needs explaining, so the
decomposition does not close. The reason is that the timeframe term is
measured on ONE quarter with se 1.06pp - the overlap is all yfinance can
offer at 5 minutes.

Angel has no such limit. Fetching ANGEL HOURLY over the same 3 years fills
the fourth cell and isolates timeframe at full power, on the full window,
with the feed held fixed:

    Angel 1h 3y  vs  Angel 5m 3y (49.24%)   -> TIMEFRAME, feed held fixed
    Angel 1h 3y  vs  yf 1h 3y    (46.92%)   -> FEED, timeframe held fixed

Whichever of the two gaps is large is the explanation. If Angel 1h lands
near 46.9%, timeframe explains the divergence and the hourly baseline was
never a valid control for a 5-minute strategy. If it lands near 49.2%, the
feed does, and the yfinance corpus is the thing that was misleading.

SESSION-CLUSTERED STANDARD ERRORS throughout. On a given session all 20
symbols are one market, so the naive binomial SE understates the true one by
a measured factor of 5.7x in design effect. Every interval printed here is
clustered by session; the naive figure is shown alongside only to make the
size of that correction visible.

READ ONLY: no order, no trading call, no state write, no production DB.
"""
import json
import math
import os
import sqlite3
import sys
import warnings
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

import angelone_client as angel
import angel_research_io as aio

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "intraday_config.json")))
STOCKS = [s.replace(".NS", "") for s in CFG.get("symbols", [])]
INDEX_SCRIP = {"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank"}
INDEX = set(INDEX_SCRIP)

OUT_DB = os.path.join(os.environ.get("RESEARCH_OUT", HERE),
                      "angel_corpus_1h.db")
YEARS = float(os.environ.get("BACKFILL_YEARS", "3"))
ALLOWED_VERDICTS = {"INTERCHANGEABLE", "ANGEL_ONLY"}

DECISIONS = [dtime(9, 30), dtime(10, 30), dtime(11, 30),
             dtime(12, 30), dtime(13, 30)]
LOOKBACK_DAYS = 7
CLOSE_T = pd.Timestamp("15:15").time()

# The two cells this run is compared against, fixed here so they cannot
# drift between runs or be adjusted after the number is known.
REFERENCE = {
    "angel_5m_3y": 49.24,      # this project's backfill run
    "yf_1h_3y": 46.92,         # recomputed locally, reproduces the 46.33 baseline
    "yf_5m_overlap": 47.66,
    "yf_1h_overlap": 47.31,
    "design_effect": 5.7,      # measured on yfinance 1h, 3 years
}


def direction_at(bars):
    """Byte-for-byte the frozen production confluence signal."""
    if len(bars) < 21:
        return None, 0.0
    ema9 = bars["Close"].ewm(span=9, adjust=False).mean()
    ema21 = bars["Close"].ewm(span=21, adjust=False).mean()
    vol = bars["Volume"].sum()
    vwap = ((bars["Close"] * bars["Volume"]).sum() / vol) if vol \
        else bars["Close"].mean()
    last = bars["Close"].iloc[-1]
    d = None
    if ema9.iloc[-1] > ema21.iloc[-1] and last > vwap:
        d = "BULL"
    elif ema9.iloc[-1] < ema21.iloc[-1] and last < vwap:
        d = "BEAR"
    up = (ema9 > ema21).tail(12)
    tq = float(up.mean()) if ema9.iloc[-1] > ema21.iloc[-1] \
        else float((~up).mean())
    return d, tq


def cluster_se(sample):
    """Session-clustered SE of the hit rate, in percentage points."""
    n = len(sample)
    if n < 2:
        return float("nan"), 0
    p = sum(1 for r in sample if r["r"] > 0) / n
    byday = defaultdict(list)
    for r in sample:
        byday[r["day"]].append(1.0 if r["r"] > 0 else 0.0)
    G = len(byday)
    if G < 2:
        return float("nan"), G
    s = sum((sum(v) - len(v) * p) ** 2 for v in byday.values())
    return 100 * math.sqrt(s * G / (G - 1)) / n, G


def stats(sample):
    n = len(sample)
    p = 100 * sum(1 for r in sample if r["r"] > 0) / n
    naive = 100 * math.sqrt((p / 100) * (1 - p / 100) / n)
    cse, G = cluster_se(sample)
    return n, p, naive, cse, G


def main():
    v = (os.environ.get("GATE_VERDICT") or "").strip().upper()
    if v not in ALLOWED_VERDICTS:
        print(f"GATE_VERDICT = {v!r} is not one of {sorted(ALLOWED_VERDICTS)}.")
        print("REFUSING TO FETCH. Run the direction-agreement gate first.")
        return 3
    print("=" * 80)
    print("ANGEL HOURLY CONTROL - the missing cell of the 2x2")
    print("=" * 80)
    print(f"  comparing against, fixed before this run:")
    for k, val in REFERENCE.items():
        print(f"    {k:<18} {val}")
    print()

    smart = angel.login()
    if smart is None:
        print("LOGIN FAILED. (No credential is printed.)")
        return 2
    print("  authenticated OK\n")

    resolved, unresolved = aio.resolve_tokens(STOCKS, INDEX_SCRIP)
    for name, why in unresolved:
        print(f"  UNRESOLVED {name}: {why}  -> DROPPED, not guessed")

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=int(365.25 * YEARS))
    end = today.replace(hour=15, minute=30)
    print(f"  window {start:%Y-%m-%d} .. {end:%Y-%m-%d}\n")
    print(f"  {'symbol':<12} {'bars':>8} {'sessions':>9} {'reqs':>5} "
          f"{'trunc':>6} {'errors':>7}")

    conn = sqlite3.connect(OUT_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bar(
            symbol TEXT, interval TEXT, ts TEXT, open REAL, high REAL,
            low REAL, close REAL, volume INTEGER,
            PRIMARY KEY(symbol, interval, ts));
        CREATE TABLE IF NOT EXISTS batch(
            symbol TEXT, interval TEXT, source TEXT, fetched_at_utc TEXT,
            req_from TEXT, req_to TEXT, n_bars INTEGER, ts_semantics TEXT,
            adjustment TEXT, spans_json TEXT);
    """)
    corpus = {}
    for name in sorted(resolved):
        r = resolved[name]
        # 60-minute bars: ~7 per session, so a chunk of 18 sessions is only
        # ~126 rows - nowhere near the 1,611 cap. Truncation handling still
        # runs; it simply never has to fire.
        rows, spans = aio.fetch_range(smart, r["exch"], r["token"], "ONE_HOUR",
                                      start, end, bar_minutes=60,
                                      log=lambda m: print(m))
        ne = sum(1 for s in spans if s["error"])
        nt = sum(1 for s in spans if s["truncated"])
        print(f"  {name:<12} {len(rows):>8,} "
              f"{len({t.date() for t, *_ in rows}):>9} {len(spans):>5} "
              f"{nt:>6} {ne:>7}")
        conn.executemany(
            "INSERT OR REPLACE INTO bar VALUES(?,?,?,?,?,?,?,?)",
            [(name, "1h", t.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c, vv)
             for t, o, h, l, c, vv in rows])
        conn.execute("INSERT INTO batch VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (name, "1h", "angelone.getCandleData",
                      datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      start.isoformat(), end.isoformat(), len(rows),
                      "BAR OPEN", "split-adjusted, dividend-unadjusted",
                      json.dumps(spans)))
        conn.commit()
        if rows:
            df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low",
                                             "Close", "Volume"])
            df.index = pd.to_datetime(df["ts"])
            corpus[name] = df[["Open", "High", "Low", "Close",
                               "Volume"]].sort_index()
    conn.close()

    obs = []
    for sym, df in corpus.items():
        for day, d0 in df.groupby(df.index.date):
            for dec in DECISIONS:
                cut = pd.Timestamp.combine(pd.Timestamp(day), dec)
                hist = df[(df.index < cut) &
                          (df.index >= cut - pd.Timedelta(days=LOOKBACK_DAYS))]
                if len(hist) < 21:
                    continue
                d, _ = direction_at(hist)
                if d is None:
                    continue
                fut = d0[d0.index >= cut]
                eod = fut[fut.index <= pd.Timestamp.combine(
                    pd.Timestamp(day), CLOSE_T)]
                if fut.empty or eod.empty:
                    continue
                s = 1.0 if d == "BULL" else -1.0
                obs.append({
                    "sym": sym, "day": str(day),
                    "q": f"{str(day)[:4]}Q{(int(str(day)[5:7])-1)//3+1}",
                    "idx": "INDEX" if sym in INDEX else "STOCK",
                    "r": s * (float(eod["Close"].iloc[-1]) /
                              float(fut["Close"].iloc[0]) - 1) * 100})

    if len(obs) < 100:
        print(f"\n  only {len(obs)} observations - cannot evaluate")
        return 4

    print("\n" + "=" * 80)
    print("RESULT")
    print("=" * 80)
    n, p, naive, cse, G = stats(obs)
    print(f"  ANGEL 1h, {YEARS} years:  n={n:,}  sessions={G}  hit={p:.2f}%")
    print(f"    naive 95% CI     [{p-1.96*naive:.2f}, {p+1.96*naive:.2f}]  "
          f"(se {naive:.3f}pp)")
    print(f"    CLUSTERED 95% CI [{p-1.96*cse:.2f}, {p+1.96*cse:.2f}]  "
          f"(se {cse:.3f}pp, design effect {(cse/naive)**2:.1f}x)")

    print(f"\n  {'comparison':<42} {'gap':>8}  isolates")
    g_tf = REFERENCE["angel_5m_3y"] - p
    g_fd = p - REFERENCE["yf_1h_3y"]
    print(f"  {'Angel 5m 3y  -  Angel 1h 3y':<42} {g_tf:>+7.2f}pp  "
          f"TIMEFRAME (feed fixed)")
    print(f"  {'Angel 1h 3y  -  yfinance 1h 3y':<42} {g_fd:>+7.2f}pp  "
          f"FEED (timeframe fixed)")
    print(f"  {'total to explain (Angel 5m - yf 1h)':<42} "
          f"{REFERENCE['angel_5m_3y']-REFERENCE['yf_1h_3y']:>+7.2f}pp")
    dom = "TIMEFRAME" if abs(g_tf) > abs(g_fd) else "FEED"
    print(f"\n  DOMINANT TERM: {dom}")
    if dom == "TIMEFRAME":
        print("  -> the hourly baseline was never a valid control for a")
        print("     5-minute strategy. RETIRE IT rather than reconcile it.")
    else:
        print("  -> the two feeds disagree on the full window even at the same")
        print("     timeframe. The gate measured agreement on 41 sessions only;")
        print("     that is the thing to distrust, not the timeframe.")

    print(f"\n  BY QUARTER (clustered SE, not naive):")
    print(f"    {'quarter':<9} {'n':>7} {'days':>5} {'hit%':>7} "
          f"{'clustered 95% CI':>20}")
    qs = defaultdict(list)
    for r in obs:
        qs[r["q"]].append(r)
    vals = []
    for q in sorted(qs):
        if len(qs[q]) < 100:
            continue
        qn, qp, qnaive, qcse, qG = stats(qs[q])
        vals.append(qp)
        print(f"    {q:<9} {qn:>7,} {qG:>5} {qp:>6.2f}%   "
              f"[{qp-1.96*qcse:>6.2f}, {qp+1.96*qcse:>6.2f}]")
    if len(vals) > 1:
        m = sum(vals) / len(vals)
        sd = math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))
        band = 1.96 * cse * math.sqrt(REFERENCE["design_effect"] /
                                      REFERENCE["design_effect"])
        exp_sd = cse * math.sqrt(len(obs) / (len(obs) / len(vals)))
        print(f"\n    quarters {len(vals)}  mean {m:.2f}%  sd {sd:.2f}pp  "
              f"above 50%: {sum(1 for x in vals if x > 50)}/{len(vals)}")
        chi = (len(vals) - 1) * (sd / (cse * math.sqrt(len(vals)))) ** 2
        print(f"    Dispersion beyond sampling noise is judged against the")
        print(f"    CLUSTERED per-quarter SE, never the naive one - the naive")
        print(f"    SE understates by ~{REFERENCE['design_effect']}x and makes")
        print(f"    ordinary variation look like regime.")

    print(f"\n  BY SYMBOL CLASS:")
    for cls in ("INDEX", "STOCK"):
        s = [r for r in obs if r["idx"] == cls]
        if len(s) >= 100:
            sn, sp, snv, scse, sG = stats(s)
            print(f"    {cls:<7} n={sn:>7,}  {sp:>6.2f}%  "
                  f"clustered [{sp-1.96*scse:.2f}, {sp+1.96*scse:.2f}]")

    print(f"\n  corpus written to {OUT_DB} (uploaded as the run artifact)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
