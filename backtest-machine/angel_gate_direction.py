"""GATE: do Angel One and yfinance produce the SAME signal direction?

This runs BEFORE any backfill. The question it answers is not "are the prices
close" - it is "would the bot have made the same decision". Two feeds can
agree to four decimal places on price and still disagree on direction at the
instants that matter, because EMA9/EMA21/VWAP crossings are threshold events:
arbitrarily small price differences flip the output whenever the signal is
near a crossing, and near-crossing instants are exactly where trades happen.

PRE-STATED DECISION RULE - fixed before the number is known, and printed
before it is computed so it cannot be adjusted afterwards:

    >= 99%   feeds are interchangeable; Angel may extend the yfinance corpus
    95 - 99% Angel-only corpus. Do NOT mix feeds in one series.
    <  95%   STOP. Neither the backfill nor Phase 2 proceeds.

WHAT IS COMPARED. The overlap is bounded by yfinance's hard 60-day cap on
5-minute data; Angel is pulled over the same window so both sides are the
same sessions. direction_at() below is a byte-for-byte copy of the function
used in horizon_test.py and the Phase 1 harness - copied rather than
imported because the harness lives outside the repository, and a gate that
tested a REIMPLEMENTED signal would be measuring the reimplementation.

READ ONLY: no order, no trading call, no state write, no production DB.
"""
import json
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
import yfinance as yf

import angelone_client as angel
import angel_research_io as aio

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "intraday_config.json")))
STOCKS = [s.replace(".NS", "") for s in CFG.get("symbols", [])]
INDEX_SCRIP = {"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank"}
YF_TICKER = dict({s: f"{s}.NS" for s in STOCKS},
                 NIFTY="^NSEI", BANKNIFTY="^NSEBANK")

DECISIONS = [dtime(9, 30), dtime(10, 30), dtime(11, 30),
             dtime(12, 30), dtime(13, 30)]
LOOKBACK_DAYS = 7
OUT_DB = os.path.join(os.environ.get("RESEARCH_OUT", HERE),
                      "angel_5m_overlap.db")

THRESHOLD_INTERCHANGEABLE = 99.0
THRESHOLD_ANGEL_ONLY = 95.0


def direction_at(bars):
    """The production confluence signal, on bars up to the cutoff."""
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


def to_frame(rows):
    """[(ts,o,h,l,c,v)] -> the OHLCV frame direction_at expects."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low",
                                     "Close", "Volume"])
    df.index = pd.to_datetime(df["ts"])
    return df[["Open", "High", "Low", "Close", "Volume"]].sort_index()


def decisions_for(df, days):
    """{(day, decision) -> direction} using the harness's exact windowing."""
    out = {}
    if df.empty:
        return out
    for day in days:
        d0 = df[df.index.date == day]
        if d0.empty:
            continue
        for dec in DECISIONS:
            cut = pd.Timestamp.combine(pd.Timestamp(day), dec)
            hist = df[(df.index < cut) &
                      (df.index >= cut - pd.Timedelta(days=LOOKBACK_DAYS))]
            if len(hist) < 21:
                out[(day, dec)] = "NO_HISTORY"
                continue
            d, _ = direction_at(hist)
            out[(day, dec)] = d or "FLAT"
    return out


def save(rows_by_symbol, spans_by_symbol, window):
    conn = sqlite3.connect(OUT_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bar(
            symbol TEXT, interval TEXT, ts TEXT,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY(symbol, interval, ts));
        CREATE TABLE IF NOT EXISTS batch(
            symbol TEXT, interval TEXT, source TEXT, fetched_at_utc TEXT,
            req_from TEXT, req_to TEXT, n_bars INTEGER,
            first_ts TEXT, last_ts TEXT, ts_semantics TEXT,
            adjustment TEXT, spans_json TEXT);
    """)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for sym, rows in rows_by_symbol.items():
        conn.executemany(
            "INSERT OR REPLACE INTO bar VALUES(?,?,?,?,?,?,?,?)",
            [(sym, "5m", t.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c, v)
             for t, o, h, l, c, v in rows])
        conn.execute(
            "INSERT INTO batch VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sym, "5m", "angelone.getCandleData", now,
             window[0].isoformat(), window[1].isoformat(), len(rows),
             rows[0][0].isoformat() if rows else None,
             rows[-1][0].isoformat() if rows else None,
             "BAR OPEN (verified: grid is exactly 09:15..15:25, 75 stamps)",
             "UNVERIFIED at gate stage - see the backfill validation",
             json.dumps(spans_by_symbol.get(sym, []))))
    conn.commit()
    conn.close()


def main():
    print("=" * 78)
    print("ANGEL vs YFINANCE - SIGNAL DIRECTION AGREEMENT GATE")
    print(f"run at {datetime.now():%Y-%m-%d %H:%M} IST")
    print("=" * 78)
    print("PRE-STATED RULE (fixed before the number is known):")
    print(f"  >= {THRESHOLD_INTERCHANGEABLE}%      feeds interchangeable; "
          f"Angel may extend the yfinance corpus")
    print(f"  {THRESHOLD_ANGEL_ONLY} - {THRESHOLD_INTERCHANGEABLE}%   "
          f"Angel-ONLY corpus; do not mix feeds in one series")
    print(f"  <  {THRESHOLD_ANGEL_ONLY}%      STOP - no backfill, no Phase 2")
    print("=" * 78)

    smart = angel.login()
    if smart is None:
        print("\nLOGIN FAILED - gate cannot run. (No credential is printed.)")
        return 2
    print("  authenticated OK\n")

    print("resolving NSE tokens from the public scrip master ...")
    resolved, unresolved = aio.resolve_tokens(STOCKS, INDEX_SCRIP)
    for name, why in unresolved:
        print(f"  UNRESOLVED {name}: {why}  -> DROPPED, not guessed")
    print(f"  resolved {len(resolved)}/{len(STOCKS) + len(INDEX_SCRIP)}")
    for n in sorted(resolved):
        r = resolved[n]
        print(f"    {n:<11} token {r['token']:<8} {r['kind']:<5} "
              f"{r['scrip_symbol']}")

    # ---------------------------------------------------------- window ---
    # yfinance's 60-day cap on 5-minute data defines the overlap; Angel is
    # pulled over exactly the same span so neither side has sessions the
    # other lacks for reasons unrelated to the feeds.
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=60)
    end = today.replace(hour=15, minute=30)
    print(f"\noverlap window: {start:%Y-%m-%d} .. {end:%Y-%m-%d} "
          f"(bounded by yfinance's 60-day 5-minute cap)")

    ang_rows, ang_spans, yf_frames = {}, {}, {}
    print("\n" + "-" * 78)
    print("PULLING BOTH FEEDS")
    print("-" * 78)
    print(f"  {'symbol':<12} {'angel':>7} {'yfinance':>9} {'sessions A/Y':>14}"
          f"  truncation events")
    for name in sorted(resolved):
        r = resolved[name]
        rows, spans = aio.fetch_range(
            smart, r["exch"], r["token"], "FIVE_MINUTE", start, end,
            log=lambda m: print(m))
        ang_rows[name], ang_spans[name] = rows, spans
        try:
            y = yf.download(YF_TICKER[name], period="60d", interval="5m",
                            auto_adjust=True, progress=False,
                            multi_level_index=False)
            if y is not None and not y.empty and y.index.tz is not None:
                y.index = y.index.tz_convert("Asia/Kolkata").tz_localize(None)
            yf_frames[name] = y if y is not None else pd.DataFrame()
        except Exception as e:
            print(f"  {name}: yfinance FAILED {aio.safe(e)}")
            yf_frames[name] = pd.DataFrame()
        ntr = sum(1 for s in spans if s["truncated"])
        nse = len({t.date() for t, *_ in rows})
        nsy = len(set(yf_frames[name].index.date)) if not yf_frames[name].empty else 0
        print(f"  {name:<12} {len(rows):>7,} {len(yf_frames[name]):>9,} "
              f"{nse:>6}/{nsy:<7} {ntr}")

    save(ang_rows, ang_spans, (start, end))
    print(f"\n  Angel bars written to {OUT_DB} (uploaded as the run artifact)")

    # ------------------------------------------------------- agreement ---
    print("\n" + "-" * 78)
    print("DIRECTION AGREEMENT AT HARNESS DECISION INSTANTS")
    print("  decision times 09:30/10:30/11:30/12:30/13:30, 7-day trailing")
    print("  window, bars strictly BEFORE the cut - identical on both feeds")
    print("-" * 78)
    by_sym, by_dec = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    agree = total = 0
    both_agree = both_total = 0
    conf = defaultdict(int)
    disagreements = []

    for name in sorted(resolved):
        adf, ydf = to_frame(ang_rows[name]), yf_frames[name]
        if adf.empty or ydf.empty:
            print(f"  {name:<12} SKIPPED - one feed returned nothing")
            continue
        days = sorted(set(adf.index.date) & set(ydf.index.date))
        A, Y = decisions_for(adf, days), decisions_for(ydf, days)
        for k in sorted(set(A) & set(Y)):
            a, y = A[k], Y[k]
            total += 1
            ok = (a == y)
            agree += ok
            by_sym[name][0] += ok
            by_sym[name][1] += 1
            by_dec[k[1]][0] += ok
            by_dec[k[1]][1] += 1
            conf[(a, y)] += 1
            if a in ("BULL", "BEAR") and y in ("BULL", "BEAR"):
                both_total += 1
                both_agree += ok
            if not ok and len(disagreements) < 40:
                disagreements.append((name, k[0], k[1].strftime("%H:%M"), a, y))

    if not total:
        print("  NO COMPARABLE INSTANTS - gate cannot be evaluated")
        return 4

    strict = 100.0 * agree / total
    lenient = 100.0 * both_agree / both_total if both_total else float("nan")

    print(f"\n  {'symbol':<12} {'instants':>9} {'agree':>7} {'pct':>8}")
    for n in sorted(by_sym):
        a, t = by_sym[n]
        print(f"  {n:<12} {t:>9} {a:>7} {100.0*a/t:>7.2f}%")
    print(f"\n  {'decision':<12} {'instants':>9} {'agree':>7} {'pct':>8}")
    for d in sorted(by_dec):
        a, t = by_dec[d]
        print(f"  {d.strftime('%H:%M'):<12} {t:>9} {a:>7} {100.0*a/t:>7.2f}%")

    print("\n  confusion (Angel -> yfinance), most frequent first:")
    for (a, y), n in sorted(conf.items(), key=lambda x: -x[1])[:10]:
        print(f"    {a:<12} -> {y:<12} {n:>7}  "
              f"{'MATCH' if a == y else 'differ'}")
    if disagreements:
        print("\n  first disagreements:")
        for d in disagreements[:15]:
            print(f"    {d[0]:<11} {d[1]} {d[2]}  Angel={d[3]:<10} yf={d[4]}")

    # ---------------------------------------------- price reconciliation ---
    print("\n" + "-" * 78)
    print("PRICE RECONCILIATION - locating the largest per-bar differences")
    print("-" * 78)
    worst = []
    for name in sorted(resolved):
        adf, ydf = to_frame(ang_rows[name]), yf_frames[name]
        if adf.empty or ydf.empty:
            continue
        j = adf.join(ydf, how="inner", lsuffix="_a", rsuffix="_y")
        if j.empty:
            continue
        d = (j["Close_a"] - j["Close_y"]).abs()
        rel = d / j["Close_y"] * 10000.0
        print(f"  {name:<12} common {len(j):>6}  |dClose| max {d.max():>8.2f} "
              f"mean {d.mean():>7.4f}   rel max {rel.max():>7.1f}bps "
              f"mean {rel.mean():>6.2f}bps")
        for ts in d.nlargest(3).index:
            worst.append((float(d.loc[ts]), name, ts, float(j.loc[ts, "Close_a"]),
                          float(j.loc[ts, "Close_y"]),
                          float(j.loc[ts, "Volume_a"]),
                          float(j.loc[ts, "Volume_y"])))
    worst.sort(reverse=True)
    print("\n  THE OUTLIER BARS (the probe saw a 4.30 max on one session):")
    print(f"    {'diff':>8} {'symbol':<11} {'stamp':<17} {'angel':>10} "
          f"{'yfinance':>10} {'volA':>12} {'volY':>12}")
    for w in worst[:12]:
        print(f"    {w[0]:>8.2f} {w[1]:<11} {str(w[2])[:16]:<17} "
              f"{w[3]:>10.2f} {w[4]:>10.2f} {w[5]:>12,.0f} {w[6]:>12,.0f}")

    # ------------------------------------------------------------ verdict ---
    print("\n" + "=" * 78)
    print("GATE RESULT")
    print("=" * 78)
    print(f"  decision instants compared          : {total:,}")
    print(f"  IDENTICAL OUTCOME (strict, incl. FLAT/NO_HISTORY): "
          f"{agree:,} = {strict:.2f}%")
    print(f"  agreement where BOTH gave a tradable direction    : "
          f"{both_agree:,}/{both_total:,} = {lenient:.2f}%")
    print("  The STRICT number is the one the rule is applied to. A FLAT on "
          "one feed\n  and a BULL on the other is a different decision, not a "
          "missing one.")
    verdict = ("INTERCHANGEABLE" if strict >= THRESHOLD_INTERCHANGEABLE else
               "ANGEL_ONLY" if strict >= THRESHOLD_ANGEL_ONLY else "STOP")
    print(f"\n  {strict:.2f}%  ->  VERDICT: {verdict}")
    print({
        "INTERCHANGEABLE": "  Angel may extend the yfinance corpus. Backfill "
                           "authorised.",
        "ANGEL_ONLY": "  Backfill authorised, but the corpus must be "
                      "ANGEL-ONLY.\n  Do NOT mix feeds within one series; "
                      "rebuild the overlap from Angel too.",
        "STOP": "  STOP. Do not backfill. Do not write Phase 2. The two "
                "feeds do not\n  agree on what the signal said, so neither "
                "can be trusted to describe it.",
    }[verdict])
    print("=" * 78)
    return 0 if verdict != "STOP" else 5


if __name__ == "__main__":
    sys.exit(main())
