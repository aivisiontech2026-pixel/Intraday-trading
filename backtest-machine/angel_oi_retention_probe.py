"""Phase 3B-0.1: does getOIData RETAIN history? READ ONLY. Inventory only.

The first probe established that Angel serves intraday OI, per contract, at
ONE_MINUTE..ONE_HOUR, bar-open. Retention was left unknown, because an empty
response has three causes that look identical:

    A  contract lifetime - it did not exist or trade then
    B  API retention     - it did exist and traded, and the API won't serve it
    C  liquidity         - it existed but carried no open interest

THE DISCRIMINATOR. For every historical OI request, this probe fires the
SAME window at getCandleData on the SAME token. Candles and OI come from the
same historical/v1 family, so:

    candles > 0 and OI > 0   -> data is served
    candles > 0 and OI = 0   -> it existed AND TRADED; only OI is missing
                                => A and C are excluded, retention is B
    candles = 0 and OI = 0   -> cannot separate A from C. INDETERMINATE.
    candles = 0 and OI > 0   -> anomaly, reported as such

That is why no conclusion here rests on argument about whether a strike was
liquid. It rests on whether the exchange printed trades in that contract in
that window, which the candle endpoint answers directly.

EXPIRED TOKENS - PROVENANCE. Angel's scrip master lists only live contracts,
so expired tokens cannot be resolved once they expire. The tokens in
CAPTURED below were read from the LIVE master on 2026-09-04 and written into
this file while they were still listed. That is the only honest way to hold
an expired token: capture it before expiry, and say so. No token here is
guessed or constructed - a wrong token returns the same AB1012 as a
retention failure, and that confusion is precisely what this probe exists to
avoid.

This tests no signal, computes no correlation, hit rate or return, stores
nothing, and concludes nothing about tradeability.

CREDENTIAL HYGIENE. angel_research_io.install_log_scrubber() is called
before login and again after, because the SDK's own logger prints request
headers - including the API key on every AB1012 and the Bearer JWT on
network errors - through logzero, which this code does not own. See the
comment block there.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import angel_research_io as aio
import angelone_client as angel

aio.install_log_scrubber()          # before login: env credentials

SCRIP_URL = aio.SCRIP_URL

# Captured from the LIVE scrip master on 2026-09-04. Expiries are recorded so
# the probe can state, at run time, which of these have since expired.
CAPTURED = [
    # key            token     symbol                 expiry        strike  type
    ("priority",    "42635", "NIFTY08SEP2623900CE", "2026-09-08", 23900, "CE"),
    ("near_atm",    "42641", "NIFTY08SEP2624050CE", "2026-09-08", 24050, "CE"),
    ("wk_15sep",    "47321", "NIFTY15SEP2624050CE", "2026-09-15", 24050, "CE"),
    ("mo_29sep",    "74060", "NIFTY29SEP2624050CE", "2026-09-29", 24050, "CE"),
    ("mo_27oct",    "51396", "NIFTY27OCT2624050CE", "2026-10-27", 24050, "CE"),
    ("mo_23nov",    "64399", "NIFTY23NOV2624050CE", "2026-11-23", 24050, "CE"),
    ("mo_29dec",    "71501", "NIFTY29DEC2624000CE", "2026-12-29", 24000, "CE"),
]
PRIORITY_OFFSETS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 18, 21, 25, 30]
INTERVALS = ["ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "TEN_MINUTE",
             "FIFTEEN_MINUTE", "THIRTY_MINUTE", "ONE_HOUR", "ONE_DAY"]
IV = "FIVE_MINUTE"
LOG = []


def safe(x, n=180):
    return aio.safe(x, n)


def _win(d, h1=9, m1=15, h2=15, m2=30):
    return d.replace(hour=h1, minute=m1), d.replace(hour=h2, minute=m2)


def req(smart, kind, token, interval, frm, to, exch="NFO", note=""):
    """One getOIData or getCandleData call. Everything is logged."""
    aio._throttle()
    fn = smart.getOIData if kind == "OI" else smart.getCandleData
    p = {"exchange": exch, "symboltoken": token, "interval": interval,
         "fromdate": frm.strftime("%Y-%m-%d %H:%M"),
         "todate": to.strftime("%Y-%m-%d %H:%M")}
    err, data = None, None
    try:
        r = fn(dict(p))
        if not isinstance(r, dict):
            err = f"non-dict {type(r).__name__}"
        elif not r.get("status"):
            err = safe(r.get("message") or r.get("errorcode") or "status false")
        else:
            data = r.get("data") or []
    except Exception as e:
        err = safe(e)
    n = len(data) if data is not None else 0
    LOG.append({"kind": kind, "token": token, "interval": interval,
                "from": p["fromdate"], "to": p["todate"], "rows": n,
                "error": err, "note": note})
    return data, err, n


def stamps(data):
    out = []
    for rec in data or []:
        s = rec.get("time") if isinstance(rec, dict) else (
            rec[0] if isinstance(rec, (list, tuple)) else "")
        out.append(str(s))
    return out


def attribute(oi_rows, oi_err, c_rows, c_err):
    """A / B / C attribution from the paired candle result."""
    if oi_rows > 0:
        return "SERVED", "OI returned"
    if c_rows > 0:
        return "B", "candles exist -> contract traded; only OI missing"
    if c_err and not oi_err:
        return "INDET", "candle call itself failed"
    return "INDET", "no candles either -> cannot separate A (not listed) from C (no OI)"


def main():
    print("=" * 88)
    print("getOIData HISTORICAL RETENTION PROBE - READ ONLY, INVENTORY ONLY")
    print(f"run {datetime.now():%Y-%m-%d %H:%M} IST")
    print("=" * 88)

    smart = angel.login()
    if smart is None:
        print("\nLOGIN FAILED. (No credential value is printed.)")
        return 2
    n = aio.install_log_scrubber(smart)      # after login: runtime tokens too
    print(f"  authenticated OK; log scrubber armed over {n} secret values")

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # ---- 0. WHICH CAPTURED CONTRACTS ARE STILL LISTED? ----------------
    print("\n" + "-" * 88)
    print("0. CONTRACT REGISTRY - provenance and live/expired status")
    print("-" * 88)
    print("  tokens below were captured from the LIVE scrip master on 2026-09-04")
    print("  and written into this file then. None is guessed or constructed.")
    live = set()
    try:
        with urllib.request.urlopen(SCRIP_URL, timeout=180) as r:
            raw = json.loads(r.read().decode("utf-8", "replace"))
        live = {str(x.get("token")) for x in raw if x.get("exch_seg") == "NFO"}
        print(f"  current master: {len(live):,} NFO tokens")
    except Exception as e:
        print(f"  could not fetch the master: {safe(e)}")
    print(f"\n  {'key':<11} {'token':<8} {'symbol':<24} {'expiry':<12} "
          f"{'expired?':<9} {'in master?':<10}")
    for key, tok, sym, exp, strike, typ in CAPTURED:
        e = datetime.strptime(exp, "%Y-%m-%d").date()
        gone = e < today.date()
        inm = ("yes" if tok in live else "NO") if live else "?"
        print(f"  {key:<11} {tok:<8} {sym:<24} {exp:<12} "
              f"{('EXPIRED' if gone else 'live'):<9} {inm:<10}")

    P = CAPTURED[0]
    print(f"\n  priority contract: {P[2]} token {P[1]}")
    print("  A NIFTY near-month strike close to the money is among the most")
    print("  heavily traded instruments listed, so if candles exist for a window")
    print("  the contract unambiguously existed and traded in it.")

    # Establish a WORKING interval before the boundary sweep. Running the
    # sweep on an interval the endpoint happens to reject would return zeros
    # everywhere and read exactly like a retention wall - the whole failure
    # mode this probe exists to avoid. Interval support is mapped properly in
    # section 5; this only picks one that demonstrably returns rows today.
    global IV
    recent0 = today - timedelta(days=3)
    while recent0.weekday() >= 5:
        recent0 -= timedelta(days=1)
    f0, t0 = _win(recent0)
    for cand in (IV, "ONE_MINUTE", "THREE_MINUTE", "FIFTEEN_MINUTE", "ONE_HOUR"):
        _, e0, n0 = req(smart, "OI", P[1], cand, f0, t0, note="interval probe")
        print(f"  interval check {cand:<15} rows={n0:<6} {e0 or ''}")
        if n0 > 0:
            IV = cand
            break
    else:
        print("  NO interval returned rows on a recent session. The sweep below")
        print("  cannot distinguish retention from an unusable request, so every")
        print("  result must be read as INDETERMINATE.")
    print(f"  -> sweeps below use {IV}")

    # ---- 1. PRIORITY SWEEP -------------------------------------------
    print("\n" + "-" * 88)
    print("1. PRIORITY TEST - day-by-day boundary, with paired candle control")
    print("-" * 88)
    print(f"  {'back':>5} {'date':<12} {'OI rows':>8} {'candle rows':>12} "
          f"{'attribution':<10} note")
    boundary = None
    prev_ok = None
    for off in PRIORITY_OFFSETS:
        d = today - timedelta(days=off)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        f, t = _win(d)
        o, oe, on = req(smart, "OI", P[1], IV, f, t, note=f"priority -{off}d")
        c, ce, cn = req(smart, "CANDLE", P[1], IV, f, t, note=f"priority -{off}d")
        tag, why = attribute(on, oe, cn, ce)
        print(f"  {off:>4}d {d:%Y-%m-%d} {on:>8} {cn:>12} {tag:<10} "
              f"{(oe or why)[:44]}")
        if prev_ok is not None and prev_ok > 0 and on == 0 and boundary is None:
            boundary = (prev_off, off)
        prev_ok, prev_off = on, off
    if boundary:
        print(f"\n  OI falls to zero between -{boundary[0]}d and -{boundary[1]}d")
    else:
        print("\n  no zero-crossing inside the probed range")

    # ---- 2. FAR-DATED LIQUID CONTROL ---------------------------------
    print("\n" + "-" * 88)
    print("2. FAR-DATED LIQUID CONTROL - confirm RECENT OI before judging old")
    print("-" * 88)
    print("  A 2031 strike proves nothing: zero OI there is cause C. These are")
    print("  monthly expiries 1-4 months out, at the money, and each is checked")
    print("  for non-zero OI in a RECENT window before any older window counts.")
    recent = today - timedelta(days=3)
    while recent.weekday() >= 5:
        recent -= timedelta(days=1)
    for key, tok, sym, exp, strike, typ in CAPTURED:
        if not key.startswith("mo_"):
            continue
        f, t = _win(recent)
        o, oe, on = req(smart, "OI", tok, IV, f, t, note=f"{key} recent")
        print(f"\n  {sym} (expiry {exp})")
        print(f"    recent {recent:%Y-%m-%d}: OI rows={on} {oe or ''}")
        if on == 0:
            print("    -> no recent OI, so this contract cannot test retention"
                  " (cause C not excluded)")
            continue
        for off in (14, 30, 45, 60):
            d = today - timedelta(days=off)
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            f, t = _win(d)
            o, oe, on2 = req(smart, "OI", tok, IV, f, t, note=f"{key} -{off}d")
            c, ce, cn = req(smart, "CANDLE", tok, IV, f, t, note=f"{key} -{off}d")
            tag, why = attribute(on2, oe, cn, ce)
            print(f"    -{off:>3}d {d:%Y-%m-%d}: OI={on2:<6} candles={cn:<6} "
                  f"{tag:<8} {(oe or why)[:38]}")

    # ---- 3. EXPIRED CONTRACTS ----------------------------------------
    print("\n" + "-" * 88)
    print("3. EXPIRED CONTRACTS - options")
    print("-" * 88)
    exp_any = False
    for key, tok, sym, exp, strike, typ in CAPTURED:
        e = datetime.strptime(exp, "%Y-%m-%d").date()
        if e >= today.date():
            continue
        exp_any = True
        d = e - timedelta(days=3)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        f, t = _win(d)
        o, oe, on = req(smart, "OI", tok, IV, f, t, note=f"expired {key}")
        c, ce, cn = req(smart, "CANDLE", tok, IV, f, t, note=f"expired {key}")
        print(f"  {sym} expired {exp}; querying {d:%Y-%m-%d} (inside its life)")
        print(f"    token still in master: {'yes' if tok in live else 'NO'}")
        print(f"    OI rows={on} {oe or ''}   candle rows={cn} {ce or ''}")
        tag, why = attribute(on, oe, cn, ce)
        print(f"    -> {tag}: {why}")
    if not exp_any:
        print("  None of the captured contracts has expired as of this run.")
        print("  Expired-contract behaviour is therefore NOT ADDRESSABLE today;")
        print("  re-dispatch after the nearest captured expiry to settle it.")

    # ---- 5. INTERVALS AND THE ONE_DAY ANOMALY ------------------------
    print("\n" + "-" * 88)
    print("5. INTERVALS - and resolving the ONE_DAY anomaly")
    print("-" * 88)
    d = today - timedelta(days=3)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    f, t = _win(d)
    print(f"  single session {d:%Y-%m-%d}")
    print(f"    {'interval':<16} {'rows':>6}  error")
    for iv in INTERVALS:
        o, oe, on = req(smart, "OI", P[1], iv, f, t, note="interval sweep")
        print(f"    {iv:<16} {on:>6}  {oe or ''}")
    print("\n  ONE_DAY variants - the first probe saw it succeed once (8 rows on")
    print("  a multi-day window) then fail on single-session windows:")
    wide_f = (today - timedelta(days=12)).replace(hour=9, minute=15)
    wide_t = d.replace(hour=15, minute=30)
    variants = [
        ("multi-day window, timed stamps", wide_f, wide_t, False),
        ("multi-day window, date-only stamps", wide_f, wide_t, True),
        ("single session, timed stamps", f, t, False),
    ]
    for label, a, b, dateonly in variants:
        aio._throttle()
        p = {"exchange": "NFO", "symboltoken": P[1], "interval": "ONE_DAY",
             "fromdate": a.strftime("%Y-%m-%d" if dateonly else "%Y-%m-%d %H:%M"),
             "todate": b.strftime("%Y-%m-%d" if dateonly else "%Y-%m-%d %H:%M")}
        try:
            r = smart.getOIData(dict(p))
            ok = isinstance(r, dict) and r.get("status")
            nn = len(r.get("data") or []) if ok else 0
            msg = "" if ok else safe(r.get("message") if isinstance(r, dict) else r)
        except Exception as e:
            nn, msg = 0, safe(e)
        LOG.append({"kind": "OI", "token": P[1], "interval": "ONE_DAY",
                    "from": p["fromdate"], "to": p["todate"], "rows": nn,
                    "error": msg or None, "note": "ONE_DAY " + label})
        print(f"    {label:<38} rows={nn:<5} {msg}")

    # ---- 6. RECORD CAP vs WINDOW LIMIT -------------------------------
    print("\n" + "-" * 88)
    print("6. RECORD CAP vs WINDOW WIDTH - two different failures")
    print("-" * 88)
    print("  A wide OLD window failing could be either. A wide RECENT window")
    print("  isolates width, because retention cannot be the cause there.")
    for days in (2, 5, 10, 20, 40):
        a = (today - timedelta(days=days)).replace(hour=9, minute=15)
        b = d.replace(hour=15, minute=30)
        o, oe, on = req(smart, "OI", P[1], IV, a, b, note=f"width {days}d recent")
        st = stamps(o)
        print(f"  recent window {days:>3}d  rows={on:<6} "
              f"{(st[0][:16] + ' .. ' + st[-1][:16]) if st else (oe or '')}")
        if on in (500, 1611):
            print(f"       -> sits exactly on a known cap ({on}); "
                  f"SILENT TRUNCATION suspected")

    # ---- 7. REPRODUCIBILITY ------------------------------------------
    print("\n" + "-" * 88)
    print("7. REPRODUCIBILITY - repeat one success and one failure")
    print("-" * 88)
    succ = next((x for x in LOG if x["kind"] == "OI" and x["rows"] > 0), None)
    fail = next((x for x in LOG if x["kind"] == "OI" and x["rows"] == 0
                 and x["error"]), None)
    for label, rec in (("SUCCESS", succ), ("FAILURE", fail)):
        if not rec:
            print(f"  no {label.lower()} to repeat")
            continue
        a = datetime.strptime(rec["from"], "%Y-%m-%d %H:%M")
        b = datetime.strptime(rec["to"], "%Y-%m-%d %H:%M")
        o, oe, on = req(smart, "OI", rec["token"], rec["interval"], a, b,
                        note="reproducibility")
        same = (on == rec["rows"])
        print(f"  {label}: {rec['from']} .. {rec['to']} @ {rec['interval']}")
        print(f"    first run rows={rec['rows']} {rec['error'] or ''}")
        print(f"    repeat    rows={on} {oe or ''}   -> "
              f"{'REPRODUCES' if same else 'DOES NOT REPRODUCE'}")
        if on and o:
            st = stamps(o)
            print(f"    stamps {st[0][:16]} .. {st[-1][:16]}   "
                  f"sample record {safe(o[0], 90)}")

    # ---- REQUEST LOG -------------------------------------------------
    print("\n" + "-" * 88)
    print("4. FULL REQUEST LOG - one row per call")
    print("-" * 88)
    print(f"  {'kind':<7} {'token':<8} {'interval':<15} {'from':<17} "
          f"{'to':<17} {'rows':>6}  error")
    for r in LOG:
        print(f"  {r['kind']:<7} {r['token']:<8} {r['interval']:<15} "
              f"{r['from']:<17} {r['to']:<17} {r['rows']:>6}  "
              f"{(r['error'] or '')[:34]}")
    print(f"\n  total requests: {len(LOG)}")

    # ---- CLASSIFICATION ----------------------------------------------
    print("\n" + "=" * 88)
    print("9. RETENTION CLASSIFICATION")
    print("=" * 88)
    ois = [r for r in LOG if r["kind"] == "OI" and r["note"].startswith("priority")]
    served = [r for r in ois if r["rows"] > 0]
    print(f"  priority-contract OI requests: {len(ois)}, served: {len(served)}")
    print("  Attribution counts are printed per row above; the classification")
    print("  below uses only rows where a paired candle call SUCCEEDED, because")
    print("  only those exclude A and C.")
    if boundary:
        lo, hi = boundary
        label = ("~7 days" if hi <= 10 else "weeks" if hi <= 45 else
                 "months" if hi <= 200 else ">=1 year")
        print(f"\n  OI stops between -{lo}d and -{hi}d on a contract whose")
        print(f"  candles were still served -> RETENTION: {label}")
    else:
        print("\n  No boundary observed inside the probed range. If every probed")
        print("  offset was served, retention is deeper than the sweep; if none")
        print("  was, classify INDETERMINATE - not 'unavailable'.")
    print("\n  If the paired candle call failed wherever OI failed, the correct")
    print("  label is INDETERMINATE: A and C were not excluded and retention was")
    print("  not measured.")

    print("\n" + "=" * 88)
    print("PROBE COMPLETE - nothing written, nothing stored, no order placed")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
