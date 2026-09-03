"""Probe Angel One's historical-candle lookback depth. READ ONLY.

Answers one question: how far back does getCandleData actually serve
5-minute bars? That determines whether the signal-research corpus can be
built at the production timeframe or must fall back to hourly.

WHAT THIS DOES NOT DO
  * no backfill, no corpus write, no state write, no order, no trading call
  * it only READS candles and prints counts and timestamps

CREDENTIAL HYGIENE
  Credentials are read by angelone_client.login() from the environment and
  are never referenced here. Every error path prints `type(e).__name__` and
  a truncated message only, and the message is scrubbed for anything that
  looks like a token before printing. No credential value can reach stdout.
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import angelone_client as angel

SCRIP_URL = ("https://margincalculator.angelone.in/OpenAPI_File/files/"
             "OpenAPIScripMaster.json")
PROBE_SYMBOL = "RELIANCE-EQ"
OFFSETS = [7, 30, 60, 90, 180, 365, 545, 730, 1095]

# Anything shaped like a credential is removed before any message is printed.
_SCRUB = re.compile(r"[A-Za-z0-9_\-]{20,}")


def safe(e, n=160):
    """Exception -> printable text with credential-shaped strings removed."""
    return f"{type(e).__name__}: {_SCRUB.sub('<redacted>', str(e))[:n]}"


def nse_token(sym):
    """NSE equity token for `sym`, from the public scrip master."""
    print(f"  fetching scrip master for the {sym} token ...")
    with urllib.request.urlopen(SCRIP_URL, timeout=120) as r:
        raw = json.loads(r.read().decode("utf-8", "replace"))
    for rec in raw:
        if rec.get("exch_seg") == "NSE" and rec.get("symbol") == sym:
            print(f"  {sym} -> token {rec['token']}  ({rec.get('name')})")
            return str(rec["token"])
    return None


def candles(smart, token, interval, frm, to):
    """One getCandleData call. Returns (rows, error_text, elapsed_seconds)."""
    t0 = time.time()
    try:
        r = smart.getCandleData({
            "exchange": "NSE", "symboltoken": token, "interval": interval,
            "fromdate": frm.strftime("%Y-%m-%d %H:%M"),
            "todate": to.strftime("%Y-%m-%d %H:%M")})
    except Exception as e:
        return None, safe(e), time.time() - t0
    el = time.time() - t0
    if not isinstance(r, dict):
        return None, f"non-dict response {type(r).__name__}", el
    if not r.get("status"):
        return None, _SCRUB.sub("<redacted>", str(r.get("message")))[:160], el
    return (r.get("data") or []), None, el


def main():
    print("=" * 74)
    print("ANGEL ONE HISTORICAL LOOKBACK PROBE - READ ONLY")
    print(f"probe symbol {PROBE_SYMBOL} | today {datetime.now():%Y-%m-%d %H:%M} IST")
    print("=" * 74)

    smart = angel.login()
    if smart is None:
        print("\nLOGIN FAILED - cannot probe. (No credential value is printed.)")
        return 2
    print("  authenticated OK")

    token = nse_token(PROBE_SYMBOL)
    if not token:
        print(f"  {PROBE_SYMBOL} not found in the scrip master")
        return 3

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # ---- 1. LOOKBACK SWEEP -------------------------------------------
    print("\n" + "-" * 74)
    print("1. LOOKBACK SWEEP - FIVE_MINUTE, one session per offset")
    print("-" * 74)
    print(f"  {'offset':>7} {'date':<12} {'bars':>6} {'first':<17} {'last':<17} error")
    ok_off, bad_off = [], []
    for off in OFFSETS:
        d = today - timedelta(days=off)
        while d.weekday() >= 5:                    # step back to a weekday
            d -= timedelta(days=1)
        rows, err, _ = candles(smart, token, "FIVE_MINUTE",
                               d.replace(hour=9, minute=15),
                               d.replace(hour=15, minute=30))
        n = len(rows) if rows is not None else 0
        f = rows[0][0][:16] if rows else ""
        l = rows[-1][0][:16] if rows else ""
        print(f"  {off:>6}d {d:%Y-%m-%d} {n:>6} {f:<17} {l:<17} {err or ''}")
        (ok_off if n > 0 else bad_off).append(off)
        time.sleep(0.4)

    # ---- 2. BISECT THE EXACT CUTOFF ----------------------------------
    print("\n" + "-" * 74)
    print("2. EXACT CUTOFF - bisection between deepest success and first failure")
    print("-" * 74)
    if not ok_off:
        print("  no offset returned data; cannot bisect")
    elif not bad_off:
        print(f"  every probed offset returned data (deepest {max(OFFSETS)}d)."
              f"  Cutoff is DEEPER than the sweep.")
    else:
        lo, hi = max(ok_off), min(b for b in bad_off if b > max(ok_off)) \
            if any(b > max(ok_off) for b in bad_off) else (max(ok_off), None)
        if hi is None:
            print(f"  no failure deeper than {lo}d; nothing to bisect")
        else:
            print(f"  bracket: {lo}d works, {hi}d does not")
            for _ in range(9):
                if hi - lo <= 1:
                    break
                mid = (lo + hi) // 2
                d = today - timedelta(days=mid)
                while d.weekday() >= 5:
                    d -= timedelta(days=1)
                rows, err, _ = candles(smart, token, "FIVE_MINUTE",
                                       d.replace(hour=9, minute=15),
                                       d.replace(hour=15, minute=30))
                n = len(rows) if rows is not None else 0
                print(f"    {mid:>5}d {d:%Y-%m-%d}  bars={n:<5} {err or ''}")
                if n > 0:
                    lo = mid
                else:
                    hi = mid
                time.sleep(0.4)
            print(f"  CUTOFF between {lo}d and {hi}d ago "
                  f"-> approx {(today - timedelta(days=lo)):%Y-%m-%d}")

    # ---- 3. RECORD CAP ------------------------------------------------
    print("\n" + "-" * 74)
    print("3. RECORD CAP - request a window far larger than 500 bars")
    print("-" * 74)
    d = today - timedelta(days=10)
    rows, err, _ = candles(smart, token, "FIVE_MINUTE",
                           (d - timedelta(days=30)).replace(hour=9, minute=15),
                           d.replace(hour=15, minute=30))
    if rows is None:
        print(f"  ERROR (so the cap ERRORS rather than truncating): {err}")
    else:
        print(f"  requested ~30 sessions (~2,250 bars) -> got {len(rows)}")
        if rows:
            print(f"  first {rows[0][0][:16]}   last {rows[-1][0][:16]}")
        print(f"  -> cap behaviour: "
              f"{'SILENT TRUNCATION at %d' % len(rows) if len(rows) < 2000 else 'no truncation observed'}")

    # ---- 4. RATE LIMIT ------------------------------------------------
    print("\n" + "-" * 74)
    print("4. RATE LIMIT - 12 rapid identical requests, no sleep")
    print("-" * 74)
    d = today - timedelta(days=10)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    okc = 0
    t0 = time.time()
    for i in range(12):
        rows, err, el = candles(smart, token, "FIVE_MINUTE",
                                d.replace(hour=9, minute=15),
                                d.replace(hour=10, minute=15))
        if err:
            print(f"    req {i+1:>2}: THROTTLED/ERROR after {okc} ok - {err}")
            break
        okc += 1
        print(f"    req {i+1:>2}: {len(rows):>3} bars  {el:.2f}s")
    span = time.time() - t0
    print(f"  {okc} consecutive requests succeeded in {span:.1f}s "
          f"({okc/span if span else 0:.1f} req/s)")

    # ---- 5. TIMESTAMP SEMANTICS --------------------------------------
    #
    # HEURISTIC FIX (2026-09-04). The previous version probed ONE session,
    # read only its first and last stamp, and declared
    #   "MATCH: NO - JOINS WOULD BE OFF BY ONE BAR"
    # whenever the endpoints were not exactly 09:15/15:25. It landed on
    # 2026-08-25, a SHORT session (that session is 72 bars in the yfinance
    # corpus too, ending 15:10), and so contradicted section 7 - which
    # reconciled the same session and found 72 common stamps with ZERO
    # unique to either feed. Endpoints cannot adjudicate a convention when
    # the session may be truncated; a STAMP SET can.
    #
    # This version therefore (a) searches for a FULL session before judging,
    # and (b) compares the whole stamp set against both candidate grids, so
    # a short session is reported as non-adjudicating instead of being
    # scored as a mismatch.
    print("\n" + "-" * 74)
    print("5. TIMESTAMP SEMANTICS - bar OPEN or bar CLOSE?")
    print("-" * 74)
    OPEN_GRID = [(datetime(2000, 1, 1, 9, 15) + timedelta(minutes=5 * i)
                  ).strftime("%H:%M") for i in range(75)]
    CLOSE_GRID = [(datetime(2000, 1, 1, 9, 20) + timedelta(minutes=5 * i)
                   ).strftime("%H:%M") for i in range(75)]
    print("  NSE continuous session 09:15-15:30 = 375 min = 75 five-minute bars.")
    print(f"  BAR OPEN grid  : {OPEN_GRID[0]}..{OPEN_GRID[-1]}")
    print(f"  BAR CLOSE grid : {CLOSE_GRID[0]}..{CLOSE_GRID[-1]}")
    print("  Searching back for a FULL (75-bar) session - a short session")
    print("  cannot distinguish the two conventions and must not be judged.\n")
    verdict, examined = None, 0
    probe_day = today - timedelta(days=7)
    while examined < 12 and verdict is None:
        while probe_day.weekday() >= 5:
            probe_day -= timedelta(days=1)
        rows, err, _ = candles(smart, token, "FIVE_MINUTE",
                               probe_day.replace(hour=9, minute=15),
                               probe_day.replace(hour=15, minute=30))
        examined += 1
        n = len(rows) if rows else 0
        stamps = [r[0][11:16] for r in rows] if rows else []
        if n == 75:
            if stamps == OPEN_GRID:
                verdict = "BAR OPEN"
            elif stamps == CLOSE_GRID:
                verdict = "BAR CLOSE"
            else:
                verdict = "FULL SESSION BUT NEITHER GRID - inspect manually"
            print(f"  {probe_day:%Y-%m-%d}: {n} bars  FULL -> {verdict}")
        else:
            print(f"  {probe_day:%Y-%m-%d}: {n} bars  "
                  f"{'SHORT - not adjudicating' if n else (err or 'no data')}")
        probe_day -= timedelta(days=1)
        time.sleep(0.4)

    if verdict is None:
        print("\n  NO FULL SESSION FOUND in 12 attempts - semantics UNDETERMINED.")
        print("  This is an honest 'unknown', not a mismatch.")
    else:
        print(f"\n  ANGEL: {verdict}")
        print("  yfinance corpus is BAR OPEN - verified independently: across")
        print("  87,589 5-minute bars there are exactly 75 distinct stamps,")
        print("  09:15..15:25, with NO 15:30 stamp and no 09:10 stamp. A")
        print("  close-labelled 375-minute session would have to run 09:20..15:30.")
        print(f"  MATCH: {'YES - stamps join directly' if verdict == 'BAR OPEN' else 'NO - JOINS WOULD BE OFF BY ONE BAR'}")

    # ---- 6. ONE_MINUTE lookback --------------------------------------
    print("\n" + "-" * 74)
    print("6. DOES ONE_MINUTE HAVE THE SAME LOOKBACK?")
    print("-" * 74)
    for off in (7, 90, 365, 730):
        dd = today - timedelta(days=off)
        while dd.weekday() >= 5:
            dd -= timedelta(days=1)
        rows, err, _ = candles(smart, token, "ONE_MINUTE",
                               dd.replace(hour=9, minute=15),
                               dd.replace(hour=10, minute=15))
        print(f"  {off:>5}d {dd:%Y-%m-%d}  bars={len(rows) if rows else 0:<5} {err or ''}")
        time.sleep(0.4)

    # ---- 7. RECONCILIATION vs yfinance --------------------------------
    print("\n" + "-" * 74)
    print("7. RECONCILIATION - Angel vs yfinance, same session, bar by bar")
    print("-" * 74)
    try:
        import yfinance as yf
        rec_day = today - timedelta(days=10)
        while rec_day.weekday() >= 5:
            rec_day -= timedelta(days=1)
        a_rows, err, _ = candles(smart, token, "FIVE_MINUTE",
                                 rec_day.replace(hour=9, minute=15),
                                 rec_day.replace(hour=15, minute=30))
        y = yf.download("RELIANCE.NS", period="60d", interval="5m",
                        auto_adjust=True, progress=False,
                        multi_level_index=False)
        if y.index.tz is not None:
            y.index = y.index.tz_convert("Asia/Kolkata").tz_localize(None)
        y = y[y.index.date == rec_day.date()]
        print(f"  session {rec_day:%Y-%m-%d}   Angel {len(a_rows or [])} bars"
              f"   yfinance {len(y)} bars")
        amap = {r[0][11:16]: r for r in (a_rows or [])}
        ymap = {i.strftime("%H:%M"): r for i, r in y.iterrows()}
        common = sorted(set(amap) & set(ymap))
        print(f"  common stamps: {len(common)}   "
              f"Angel-only {len(set(amap)-set(ymap))}   "
              f"yf-only {len(set(ymap)-set(amap))}")
        if common:
            dc = [abs(amap[t][4] - ymap[t].Close) for t in common]
            do = [abs(amap[t][1] - ymap[t].Open) for t in common]
            sc = sum(amap[t][4] - ymap[t].Close for t in common) / len(common)
            print(f"  |close| diff: max {max(dc):.4f}  mean {sum(dc)/len(dc):.4f}")
            print(f"  |open|  diff: max {max(do):.4f}  mean {sum(do)/len(do):.4f}")
            print(f"  SIGNED mean close diff (Angel - yf): {sc:+.4f}  "
                  f"-> {'no systematic offset' if abs(sc) < 0.05 else 'SYSTEMATIC OFFSET'}")
            print("  sample (time, Angel close, yf close):")
            for t in common[:5]:
                print(f"    {t}  {amap[t][4]:>10.2f}  {ymap[t].Close:>10.2f}")
    except Exception as e:
        print(f"  reconciliation failed: {safe(e)}")

    print("\n" + "=" * 74)
    print("PROBE COMPLETE - no data was written, no order placed")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
