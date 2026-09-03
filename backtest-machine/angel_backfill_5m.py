"""BACKFILL: three years of 5-minute bars from Angel One, then validate.

Runs ONLY after the direction-agreement gate. The gate verdict is passed in
as an environment variable and is checked before a single candle is
requested - see require_gate(). This is a hard interlock, not a reminder:
the whole point of a pre-stated gate is that it cannot be walked past once
the backfill looks convenient.

Everything that follows the fetch is computed HERE, on the runner, and
printed to the log:
    COVERAGE    sessions per symbol per year, bars per session, gaps LISTED
    ADJUSTMENT  are Angel's prices split-adjusted or raw?
    CONTROL     the frozen confluence signal, re-run on the new corpus
The corpus is also uploaded as an artifact, but no conclusion depends on
retrieving it - a research run whose answer is trapped inside a file nobody
can download has not answered anything.

READ ONLY with respect to trading: no order, no trading call, no state
write, no production database is opened.
"""
import json
import math
import os
import sqlite3
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, time as dtime, timedelta, timezone

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import yfinance as yf

import angelone_client as angel
import angel_research_io as aio

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "intraday_config.json")))
STOCKS = [s.replace(".NS", "") for s in CFG.get("symbols", [])]
INDEX_SCRIP = {"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank"}
INDEX = set(INDEX_SCRIP)

OUT_DB = os.path.join(os.environ.get("RESEARCH_OUT", HERE),
                      "angel_corpus_5m.db")
YEARS = float(os.environ.get("BACKFILL_YEARS", "3"))
ALLOWED_VERDICTS = {"INTERCHANGEABLE", "ANGEL_ONLY"}

DECISIONS = [dtime(9, 30), dtime(10, 30), dtime(11, 30),
             dtime(12, 30), dtime(13, 30)]
HORIZONS = [15, 30, 60]
LOOKBACK_DAYS = 7

# Corporate actions inside the backfill window, taken from yfinance's own
# split record for this universe - NOT chosen by hand, and re-read at run
# time so the test cannot drift out of date.
BASELINE = {"control_hit_pct": 46.33, "quarter_mean_pct": 46.46,
            "quarter_sd_pp": 1.13, "n_obs": 38926, "quarters": 13,
            "interval": "1h", "quarters_above_50": 0}


def require_gate():
    """Refuse to fetch anything unless the gate has actually passed."""
    v = (os.environ.get("GATE_VERDICT") or "").strip().upper()
    print("-" * 78)
    print("GATE INTERLOCK")
    print("-" * 78)
    if v not in ALLOWED_VERDICTS:
        print(f"  GATE_VERDICT = {v!r}")
        print(f"  Not one of {sorted(ALLOWED_VERDICTS)}. REFUSING TO BACKFILL.")
        print("  Run the direction-agreement gate first and pass its verdict.")
        return None
    print(f"  GATE_VERDICT = {v}  -> backfill authorised")
    if v == "ANGEL_ONLY":
        print("  NOTE: corpus must be ANGEL-ONLY. The yfinance corpus may not")
        print("  be concatenated with this one; it may only be compared to it.")
    return v


# ------------------------------------------------------------- storage ---
SCHEMA = """
CREATE TABLE IF NOT EXISTS bar(
    symbol TEXT, interval TEXT, ts TEXT,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    batch_id INTEGER,
    PRIMARY KEY(symbol, interval, ts));
CREATE TABLE IF NOT EXISTS batch(
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, symbol TEXT, scrip_symbol TEXT, token TEXT, kind TEXT,
    interval TEXT, req_from TEXT, req_to TEXT, fetched_at_utc TEXT,
    n_bars INTEGER, first_ts TEXT, last_ts TEXT,
    tz_handling TEXT, ts_semantics TEXT, adjustment TEXT,
    gate_verdict TEXT, n_requests INTEGER, n_truncations INTEGER);
-- Every requested span, including the ones that returned nothing. Missing
-- sessions are RECORDED here; they are never filled in.
CREATE TABLE IF NOT EXISTS fetch_span(
    batch_id INTEGER, symbol TEXT, req_from TEXT, req_to TEXT,
    n INTEGER, last_ts TEXT, truncated INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS session_present(
    symbol TEXT, interval TEXT, session_date TEXT, n_bars INTEGER,
    first_ts TEXT, last_ts TEXT,
    PRIMARY KEY(symbol, interval, session_date));
"""


def done_symbols(conn):
    """Symbols already fetched in this database - makes a re-run resumable."""
    return {r[0] for r in conn.execute(
        "SELECT symbol FROM batch WHERE interval='5m' AND n_bars>0")}


def store(conn, name, meta, rows, spans, window, verdict):
    cur = conn.execute(
        "INSERT INTO batch(source,symbol,scrip_symbol,token,kind,interval,"
        "req_from,req_to,fetched_at_utc,n_bars,first_ts,last_ts,tz_handling,"
        "ts_semantics,adjustment,gate_verdict,n_requests,n_truncations) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("angelone.getCandleData", name, meta["scrip_symbol"], meta["token"],
         meta["kind"], "5m", window[0].isoformat(), window[1].isoformat(),
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         len(rows), rows[0][0].isoformat() if rows else None,
         rows[-1][0].isoformat() if rows else None,
         "Angel returns IST with +05:30; parsed then made naive IST",
         "BAR OPEN - grid is 09:15..15:25, verified against yfinance stamps",
         "determined empirically in the ADJUSTMENT section of this run",
         verdict, len(spans), sum(1 for s in spans if s["truncated"])))
    bid = cur.lastrowid
    conn.executemany("INSERT OR REPLACE INTO bar VALUES(?,?,?,?,?,?,?,?,?)",
                     [(name, "5m", t.strftime("%Y-%m-%d %H:%M:%S"),
                       o, h, l, c, v, bid) for t, o, h, l, c, v in rows])
    conn.executemany("INSERT INTO fetch_span VALUES(?,?,?,?,?,?,?,?)",
                     [(bid, name, s["from"], s["to"], s["n"], s["last_ts"],
                       1 if s["truncated"] else 0, s["error"]) for s in spans])
    per = defaultdict(list)
    for r in rows:
        per[r[0].date()].append(r[0])
    for d, ts in per.items():
        conn.execute("INSERT OR REPLACE INTO session_present VALUES(?,?,?,?,?,?)",
                     (name, "5m", str(d), len(ts), min(ts).isoformat(),
                      max(ts).isoformat()))
    conn.commit()
    return bid


# -------------------------------------------------------------- fetch ---
def backfill(smart, conn, verdict):
    resolved, unresolved = aio.resolve_tokens(STOCKS, INDEX_SCRIP)
    for name, why in unresolved:
        print(f"  UNRESOLVED {name}: {why}  -> DROPPED, not guessed")
    already = done_symbols(conn)
    if already:
        print(f"  resuming: {len(already)} symbol(s) already stored, skipping")

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=int(365.25 * YEARS))
    end = today.replace(hour=15, minute=30)
    print(f"  window {start:%Y-%m-%d} .. {end:%Y-%m-%d}  ({YEARS} years)")
    print(f"\n  {'symbol':<12} {'bars':>9} {'sessions':>9} {'reqs':>5} "
          f"{'trunc':>6} {'errors':>7}  range")
    for name in sorted(resolved):
        if name in already:
            print(f"  {name:<12} {'(already stored - skipped)'}")
            continue
        rows, spans = aio.fetch_range(
            smart, resolved[name]["exch"], resolved[name]["token"],
            "FIVE_MINUTE", start, end, log=lambda m: print(m))
        store(conn, name, resolved[name], rows, spans, (start, end), verdict)
        ns = len({t.date() for t, *_ in rows})
        ne = sum(1 for s in spans if s["error"])
        nt = sum(1 for s in spans if s["truncated"])
        rng = (f"{rows[0][0]:%Y-%m-%d}..{rows[-1][0]:%Y-%m-%d}"
               if rows else "NO DATA")
        print(f"  {name:<12} {len(rows):>9,} {ns:>9} {len(spans):>5} "
              f"{nt:>6} {ne:>7}  {rng}")
    return resolved


# ----------------------------------------------------------- coverage ---
def coverage(conn):
    print("\n" + "=" * 78)
    print("COVERAGE")
    print("=" * 78)
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM session_present WHERE interval='5m' "
        "ORDER BY symbol")]
    # The NSE trading calendar is not invented here. The expected session set
    # is the set of dates on which the INDICES returned bars - indices are the
    # one series that cannot be absent for issuer-specific reasons (halt,
    # suspension, listing date).
    idx = [s for s in syms if s in INDEX]
    if idx:
        exp = {r[0] for r in conn.execute(
            "SELECT DISTINCT session_date FROM session_present "
            "WHERE interval='5m' AND symbol IN (%s)" %
            ",".join("?" * len(idx)), idx)}
        basis = f"union of dates the indices returned ({', '.join(idx)})"
    else:
        exp = {r[0] for r in conn.execute(
            "SELECT DISTINCT session_date FROM session_present "
            "WHERE interval='5m'")}
        basis = "union of dates ANY symbol returned (no index available)"
    print(f"  expected-session basis: {basis}")
    print(f"  expected sessions in window: {len(exp)}\n")

    print(f"  {'symbol':<12} {'sessions':>9} {'missing':>8} " +
          " ".join(f"{y:>6}" for y in sorted({d[:4] for d in exp})))
    years = sorted({d[:4] for d in exp})
    exp_y = Counter(d[:4] for d in exp)
    allmiss = {}
    for s in syms:
        have = {r[0] for r in conn.execute(
            "SELECT session_date FROM session_present "
            "WHERE interval='5m' AND symbol=?", (s,))}
        miss = sorted(exp - have)
        allmiss[s] = miss
        hy = Counter(d[:4] for d in have)
        print(f"  {s:<12} {len(have):>9} {len(miss):>8} " +
              " ".join(f"{hy.get(y,0):>3}/{exp_y[y]:<2}" for y in years))

    print("\n  MISSING SESSIONS (listed, never interpolated):")
    any_missing = False
    for s, miss in allmiss.items():
        if miss:
            any_missing = True
            head = ", ".join(miss[:12])
            print(f"    {s:<12} {len(miss):>3}: {head}"
                  f"{' ...' if len(miss) > 12 else ''}")
    if not any_missing:
        print("    none - every symbol covers every expected session")

    print("\n  BARS PER SESSION (75 = a full 09:15-15:25 grid):")
    print(f"    {'symbol':<12} {'75':>7} {'70-74':>7} {'<70':>7} {'>75':>6}"
          f"  median")
    for s in syms:
        n = [r[0] for r in conn.execute(
            "SELECT n_bars FROM session_present WHERE interval='5m' "
            "AND symbol=?", (s,))]
        if not n:
            continue
        n.sort()
        print(f"    {s:<12} {sum(1 for x in n if x == 75):>7} "
              f"{sum(1 for x in n if 70 <= x < 75):>7} "
              f"{sum(1 for x in n if x < 70):>7} "
              f"{sum(1 for x in n if x > 75):>6}  {n[len(n)//2]}")

    tr = conn.execute("SELECT COUNT(*) FROM fetch_span WHERE truncated=1").fetchone()[0]
    er = conn.execute("SELECT COUNT(*) FROM fetch_span WHERE error IS NOT NULL").fetchone()[0]
    tot = conn.execute("SELECT COUNT(*) FROM fetch_span").fetchone()[0]
    print(f"\n  requests {tot}   truncation events {tr} (all re-requested)   "
          f"failed spans {er}")
    if er:
        print("  FAILED SPANS - these are holes, not zeros:")
        for r in conn.execute("SELECT symbol,req_from,req_to,error FROM "
                              "fetch_span WHERE error IS NOT NULL LIMIT 20"):
            print(f"    {r[0]:<12} {r[1]} .. {r[2]}  {r[3]}")


# --------------------------------------------------------- adjustment ---
def adjustment(conn):
    """Are Angel's historical prices split-adjusted, or raw as-traded?

    This decides whether a 3-year series is usable as-is. An UNADJUSTED
    series contains a 50% overnight 'move' on a bonus date that is not a
    move at all; a signal test that ate one would score it as a real event.

    Two independent tests, because either alone can mislead:
      A. LEVEL - compare Angel's close on an old session against yfinance's
         raw (auto_adjust=False) and adjusted (auto_adjust=True) daily close
         for that same session. Whichever it tracks is the answer.
      B. DISCONTINUITY - at each corporate action yfinance reports for this
         universe, measure Angel's own close-to-close ratio across the
         action date. A raw series steps by the ratio; an adjusted one does
         not move.
    """
    print("\n" + "=" * 78)
    print("ADJUSTMENT - are Angel's prices adjusted or raw?")
    print("=" * 78)
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM session_present WHERE interval='5m' "
        "AND symbol NOT IN (%s) ORDER BY symbol" %
        ",".join("?" * len(INDEX)), sorted(INDEX))]

    print("\n  A. LEVEL TEST - Angel close vs yfinance raw and adjusted")
    print(f"     {'symbol':<12} {'date':<12} {'angel':>10} {'yf raw':>10} "
          f"{'yf adj':>10}  tracks")
    votes = Counter()
    for s in syms:
        row = conn.execute(
            "SELECT ts,close FROM bar WHERE interval='5m' AND symbol=? "
            "ORDER BY ts LIMIT 1", (s,)).fetchone()
        if not row:
            continue
        day = row[0][:10]
        a = conn.execute(
            "SELECT close FROM bar WHERE interval='5m' AND symbol=? AND ts "
            "LIKE ? ORDER BY ts DESC LIMIT 1", (s, day + "%")).fetchone()[0]
        try:
            raw = yf.download(f"{s}.NS", start=day,
                              end=(pd.Timestamp(day) + pd.Timedelta(days=4)
                                   ).strftime("%Y-%m-%d"),
                              interval="1d", auto_adjust=False, progress=False,
                              multi_level_index=False)
            adj = yf.download(f"{s}.NS", start=day,
                              end=(pd.Timestamp(day) + pd.Timedelta(days=4)
                                   ).strftime("%Y-%m-%d"),
                              interval="1d", auto_adjust=True, progress=False,
                              multi_level_index=False)
        except Exception as e:
            print(f"     {s:<12} yfinance failed: {aio.safe(e)}")
            continue
        if raw is None or raw.empty or adj is None or adj.empty:
            continue
        r0, a0 = float(raw["Close"].iloc[0]), float(adj["Close"].iloc[0])
        dr, da = abs(a - r0) / r0, abs(a - a0) / a0
        who = "RAW" if dr < da else "ADJUSTED"
        if min(dr, da) > 0.02:
            who = "NEITHER(>2%)"
        votes[who] += 1
        print(f"     {s:<12} {day:<12} {a:>10.2f} {r0:>10.2f} {a0:>10.2f}"
              f"  {who}")

    print("\n  B. DISCONTINUITY TEST - Angel's own step across each action")
    print(f"     {'symbol':<12} {'action date':<12} {'ratio':>7} "
          f"{'angel step':>11}  reads as")
    for s in syms:
        try:
            sp = yf.Ticker(f"{s}.NS").splits
        except Exception:
            continue
        if sp is None or len(sp) == 0:
            continue
        for d, ratio in sp.items():
            day = str(d)[:10]
            before = conn.execute(
                "SELECT close FROM bar WHERE interval='5m' AND symbol=? AND "
                "ts < ? ORDER BY ts DESC LIMIT 1", (s, day)).fetchone()
            after = conn.execute(
                "SELECT close FROM bar WHERE interval='5m' AND symbol=? AND "
                "ts >= ? ORDER BY ts LIMIT 1", (s, day)).fetchone()
            if not before or not after or not after[0]:
                continue
            step = before[0] / after[0]
            reads = ("RAW (step matches the ratio)"
                     if abs(step - float(ratio)) < 0.15 * float(ratio)
                     else "ADJUSTED (no step)" if abs(step - 1.0) < 0.15
                     else "AMBIGUOUS")
            votes["RAW" if "RAW" in reads else
                  "ADJUSTED" if "ADJUSTED" in reads else "AMBIGUOUS"] += 1
            print(f"     {s:<12} {day:<12} {float(ratio):>7.3f} "
                  f"{step:>11.3f}  {reads}")

    print(f"\n  VOTES: {dict(votes)}")
    top = votes.most_common(1)
    print(f"  CONCLUSION: Angel historical 5-minute prices appear "
          f"{top[0][0] if top else 'UNDETERMINED'}")
    if top and top[0][0] == "RAW":
        print("  CONSEQUENCE: the series contains real split discontinuities.")
        print("  They must be adjusted out before any multi-year return study,")
        print("  or every action date contributes a fictitious 50% move.")
    conn.execute("UPDATE batch SET adjustment=? WHERE interval='5m'",
                 (f"empirical: {top[0][0] if top else 'UNDETERMINED'} "
                  f"(votes {dict(votes)})",))
    conn.commit()


# ------------------------------------------------------------ control ---
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


def control(conn):
    print("\n" + "=" * 78)
    print("CONTROL - the frozen signal, re-run on the Angel 5-minute corpus")
    print("=" * 78)
    print(f"  BASELINE to beat (yfinance {BASELINE['interval']} corpus): "
          f"{BASELINE['control_hit_pct']}% on {BASELINE['n_obs']:,} obs, "
          f"{BASELINE['quarters']} quarters,")
    print(f"  quarter mean {BASELINE['quarter_mean_pct']}%, "
          f"sd {BASELINE['quarter_sd_pp']}pp, "
          f"{BASELINE['quarters_above_50']}/{BASELINE['quarters']} above 50%.")
    print("  The 5-minute corpus reproduces the PRODUCTION timeframe, which "
          "the 1h\n  baseline could not. If the signal is dead by mechanism "
          "rather than by\n  sampling artefact, this number lands in the same "
          "place.\n")

    df_all = pd.read_sql_query(
        "SELECT symbol,ts,close,volume FROM bar WHERE interval='5m' "
        "ORDER BY symbol,ts", conn)
    rows = []
    for sym, g in df_all.groupby("symbol"):
        g = g.copy()
        g.index = pd.to_datetime(g["ts"])
        d = g[["close", "volume"]].rename(columns={"close": "Close",
                                                   "volume": "Volume"})
        for day, d0 in d.groupby(d.index.date):
            for dec in DECISIONS:
                cut = pd.Timestamp.combine(pd.Timestamp(day), dec)
                hist = d[(d.index < cut) &
                         (d.index >= cut - pd.Timedelta(days=LOOKBACK_DAYS))]
                if len(hist) < 21:
                    continue
                direction, tq = direction_at(hist)
                if direction is None:
                    continue
                fut = d0[d0.index >= cut]
                if fut.empty:
                    continue
                p0 = float(fut["Close"].iloc[0])
                sign = 1.0 if direction == "BULL" else -1.0
                rec = {"sym": sym, "day": str(day),
                       "dec": dec.strftime("%H:%M"),
                       "q": f"{str(day)[:4]}Q{(int(str(day)[5:7])-1)//3+1}",
                       "idx": "INDEX" if sym in INDEX else "STOCK"}
                for h in HORIZONS:
                    tgt = cut + pd.Timedelta(minutes=h)
                    w = fut[fut.index <= tgt]
                    tol = pd.Timedelta(minutes=max(6, h * 0.2))
                    rec[f"h{h}"] = (None if w.empty or w.index[-1] < tgt - tol
                                    else sign * (float(w["Close"].iloc[-1]) / p0 - 1) * 100)
                eod = fut[fut.index <= pd.Timestamp.combine(
                    pd.Timestamp(day), pd.Timestamp("15:15").time())]
                rec["hEOD"] = (sign * (float(eod["Close"].iloc[-1]) / p0 - 1) * 100
                               if not eod.empty else None)
                rows.append(rec)

    print(f"  observations {len(rows):,}  "
          f"({len({r['day'] for r in rows})} sessions, "
          f"{len({r['sym'] for r in rows})} symbols, "
          f"{len({r['q'] for r in rows})} quarters)\n")

    def hit(sample, key):
        v = [r[key] for r in sample if r.get(key) is not None]
        if len(v) < 30:
            return None
        p = sum(1 for x in v if x > 0) / len(v)
        se = math.sqrt(p * (1 - p) / len(v))
        return len(v), 100 * p, 100 * (p - 1.96 * se), 100 * (p + 1.96 * se), \
            sum(v) / len(v)

    keys = [f"h{h}" for h in HORIZONS] + ["hEOD"]
    print(f"  {'horizon':<10} {'n':>8} {'hit%':>8} {'95% CI':>18} {'mean%':>10}")
    for k in keys:
        r = hit(rows, k)
        if r:
            print(f"  {k:<10} {r[0]:>8,} {r[1]:>7.2f}% "
                  f"[{r[2]:>6.2f},{r[3]:>6.2f}] {r[4]:>10.4f}")

    print("\n  BY QUARTER (hEOD) - is it a regime, or is it the mechanism?")
    qs = sorted({r["q"] for r in rows})
    vals = []
    print(f"    {'quarter':<9} {'n':>7} {'hit%':>8}")
    for q in qs:
        r = hit([x for x in rows if x["q"] == q], "hEOD")
        if r:
            vals.append(r[1])
            print(f"    {q:<9} {r[0]:>7,} {r[1]:>7.2f}%")
    if vals:
        m = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / max(len(vals) - 1, 1))
        above = sum(1 for v in vals if v > 50.0)
        print(f"\n    quarters {len(vals)}   mean {m:.2f}%   sd {sd:.2f}pp   "
              f"above 50%: {above}/{len(vals)}")
        print(f"    baseline was mean {BASELINE['quarter_mean_pct']}%  "
              f"sd {BASELINE['quarter_sd_pp']}pp  above 50%: "
              f"{BASELINE['quarters_above_50']}/{BASELINE['quarters']}")

    print("\n  BY SYMBOL CLASS (hEOD):")
    for cls in ("INDEX", "STOCK"):
        r = hit([x for x in rows if x["idx"] == cls], "hEOD")
        if r:
            print(f"    {cls:<7} n={r[0]:>7,}  {r[1]:>6.2f}%  "
                  f"[{r[2]:.2f},{r[3]:.2f}]")

    print(f"\n  NOTE ON MULTIPLICITY: this run consumed "
          f"{len(keys) * (1 + len(qs) + 2)} cells. The Bonferroni correction "
          f"must be\n  applied against the PERSISTED tests-consumed ledger, "
          f"which lives with the\n  research corpus and not on this runner. "
          f"The confidence intervals above are\n  UNCORRECTED and are "
          f"therefore optimistic.")


def main():
    verdict = require_gate()
    if verdict is None:
        return 3
    smart = angel.login()
    if smart is None:
        print("\nLOGIN FAILED. (No credential is printed.)")
        return 2
    print("  authenticated OK")
    conn = sqlite3.connect(OUT_DB)
    conn.executescript(SCHEMA)
    print("\n" + "=" * 78)
    print(f"BACKFILL - {YEARS} years of 5-minute bars")
    print("=" * 78)
    backfill(smart, conn, verdict)
    coverage(conn)
    adjustment(conn)
    control(conn)
    conn.close()
    print(f"\ncorpus written to {OUT_DB} (uploaded as the run artifact)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
