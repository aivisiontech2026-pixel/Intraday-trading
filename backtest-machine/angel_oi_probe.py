"""Probe Angel One's getOIData. READ ONLY. Inventory only.

Phase 3A left one question unresolved, and it was the highest-value one in
the survey: open interest is the only candidate that could be genuinely
non-price AND per-symbol AND intraday, which is the only combination that
reaches the certification bar in ~2 years rather than ~35. The route exists
(/rest/secure/angelbroking/historical/v1/getOIData, the same historical/v1
family as getCandleData) and the SDK parameter is named
`historicOIDataParams`, but the SDK is a pass-through - it deletes None
values and posts whatever dict it is given. Nothing about the request shape,
the retention, the granularity or the stamp convention is established.

This establishes them. It tests no signal, stores nothing, and concludes
nothing about tradeability.

THE TRAP THIS PROBE IS BUILT AROUND. Option contracts expire, and the scrip
master lists only live ones. Bisecting the lookback on a near-expiry
contract measures WHEN THAT CONTRACT WAS LISTED, not how far back the API
retains data - and it would report a shallow, confident, wrong answer. NIFTY
lists options out to 2031, so a long-dated contract has existed for years.
Section 5 bisects on the FARTHEST expiry and repeats the sweep on the
nearest one, so the two interpretations are separated by construction rather
than by assumption.

The timestamp section repeats the fix already applied to the candle probe:
judge the convention from a FULL session's whole stamp SET, never from the
endpoints of a session that may be short.

CREDENTIAL HYGIENE
  Credentials are read by angelone_client.login() from the environment and
  are never referenced here. Every printed string passes through safe(),
  which removes anything credential-shaped. No credential value can reach
  stdout on any path, including error paths.
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

# The SDK's own logger prints request headers - the API key on every
# AB1012, the Bearer JWT on network errors - through logzero, which this
# module does not own. Arm the redactor before anything can fail.
import angel_research_io as aio
aio.install_log_scrubber()

SCRIP_URL = ("https://margincalculator.angelone.in/OpenAPI_File/files/"
             "OpenAPIScripMaster.json")
_SCRUB = re.compile(r"[A-Za-z0-9_\-]{20,}")
INTERVALS = ["ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "TEN_MINUTE",
             "FIFTEEN_MINUTE", "THIRTY_MINUTE", "ONE_HOUR", "ONE_DAY"]
OFFSETS = [3, 7, 30, 60, 90, 180, 365, 545, 730, 1095]


def safe(x, n=300):
    s = f"{type(x).__name__}: {x}" if isinstance(x, BaseException) else str(x)
    return _SCRUB.sub("<redacted>", s)[:n]


def load_master():
    print("  fetching the public scrip master ...")
    with urllib.request.urlopen(SCRIP_URL, timeout=180) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def parse_expiry(s):
    try:
        return datetime.strptime(s, "%d%b%Y").date()
    except Exception:
        return None


def pick_contracts(raw, spot):
    """Choose the tokens the probe needs, from the live master only."""
    opts = []
    for r in raw:
        if r.get("exch_seg") != "NFO" or r.get("name") != "NIFTY":
            continue
        if r.get("instrumenttype") != "OPTIDX":
            continue
        e = parse_expiry(r.get("expiry") or "")
        if not e:
            continue
        try:
            k = float(r["strike"]) / 100.0
        except Exception:
            continue
        opts.append({"token": str(r["token"]), "symbol": r["symbol"],
                     "expiry": e, "strike": k,
                     "opt": r["symbol"][-2:]})
    if not opts:
        return {}
    exp = sorted({o["expiry"] for o in opts})
    near, far = exp[0], exp[-1]

    def atm(e, typ):
        c = [o for o in opts if o["expiry"] == e and o["opt"] == typ]
        return min(c, key=lambda o: abs(o["strike"] - spot)) if c else None

    def offstrike(e, typ, step):
        c = sorted({o["strike"] for o in opts if o["expiry"] == e
                    and o["opt"] == typ})
        if not c:
            return None
        i = min(range(len(c)), key=lambda j: abs(c[j] - spot))
        j = max(0, min(len(c) - 1, i + step))
        for o in opts:
            if o["expiry"] == e and o["opt"] == typ and o["strike"] == c[j]:
                return o
        return None

    out = {"near_ce": atm(near, "CE"), "near_pe": atm(near, "PE"),
           "near_ce_otm": offstrike(near, "CE", 4),
           "far_ce": atm(far, "CE"),
           "near_expiry": near, "far_expiry": far,
           "n_expiries": len(exp)}
    for r in raw:
        if r.get("exch_seg") == "NFO" and r.get("name") == "NIFTY" \
                and r.get("instrumenttype") == "FUTIDX":
            e = parse_expiry(r.get("expiry") or "")
            if e == exp[0] or out.get("fut") is None:
                out["fut"] = {"token": str(r["token"]), "symbol": r["symbol"],
                              "expiry": e, "strike": 0.0, "opt": "FUT"}
        if r.get("exch_seg") == "NSE" and r.get("symbol") == "RELIANCE-EQ":
            out["eq"] = {"token": str(r["token"]), "symbol": "RELIANCE-EQ",
                         "expiry": None, "strike": 0.0, "opt": "EQ"}
    return out


def call(smart, fn, params):
    t0 = time.time()
    try:
        r = fn(dict(params))
    except Exception as e:
        return None, safe(e), time.time() - t0
    el = time.time() - t0
    if not isinstance(r, dict):
        return None, f"non-dict response {type(r).__name__}", el
    if not r.get("status"):
        return None, safe(r.get("message") or r.get("errorcode") or r), el
    return (r.get("data") if r.get("data") is not None else []), None, el


def oi(smart, token, interval, frm, to, exch="NFO"):
    return call(smart, smart.getOIData, {
        "exchange": exch, "symboltoken": token, "interval": interval,
        "fromdate": frm.strftime("%Y-%m-%d %H:%M"),
        "todate": to.strftime("%Y-%m-%d %H:%M")})


def main():
    print("=" * 78)
    print("ANGEL ONE getOIData PROBE - READ ONLY, INVENTORY ONLY")
    print(f"today {datetime.now():%Y-%m-%d %H:%M} IST")
    print("=" * 78)

    smart = angel.login()
    aio.install_log_scrubber(smart)
    if smart is None:
        print("\nLOGIN FAILED. (No credential value is printed.)")
        return 2
    print("  authenticated OK")

    raw = load_master()
    idx = [r for r in raw if r.get("exch_seg") == "NSE"
           and r.get("symbol") == "Nifty 50"]
    if not idx:
        print("  Nifty 50 index token not found - cannot locate ATM")
        return 3
    itok = str(idx[0]["token"])
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    d = today - timedelta(days=4)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    rows, err, _ = call(smart, smart.getCandleData, {
        "exchange": "NSE", "symboltoken": itok, "interval": "ONE_DAY",
        "fromdate": (d - timedelta(days=10)).strftime("%Y-%m-%d %H:%M"),
        "todate": d.strftime("%Y-%m-%d %H:%M")})
    if not rows:
        print(f"  could not read NIFTY spot: {err}")
        return 4
    spot = float(rows[-1][4])
    print(f"  NIFTY spot {spot:,.2f} (from {rows[-1][0][:10]})")

    C = pick_contracts(raw, spot)
    if not C.get("near_ce"):
        print("  no NIFTY option contracts resolved")
        return 5
    print(f"  expiries listed: {C['n_expiries']}   nearest {C['near_expiry']}"
          f"   farthest {C['far_expiry']}")
    for k in ("near_ce", "near_pe", "near_ce_otm", "far_ce", "fut", "eq"):
        c = C.get(k)
        if c:
            print(f"    {k:<12} {c['symbol']:<26} token {c['token']}")

    # ---- 1. REQUEST SHAPE --------------------------------------------
    print("\n" + "-" * 78)
    print("1. REQUEST SHAPE - is getOIData shaped like getCandleData?")
    print("-" * 78)
    tok = C["near_ce"]["token"]
    variants = [
        ("candle-shaped (exchange/symboltoken/interval/from/to)",
         {"exchange": "NFO", "symboltoken": tok, "interval": "ONE_DAY",
          "fromdate": (d - timedelta(days=10)).strftime("%Y-%m-%d %H:%M"),
          "todate": d.strftime("%Y-%m-%d %H:%M")}),
        ("no interval key",
         {"exchange": "NFO", "symboltoken": tok,
          "fromdate": (d - timedelta(days=10)).strftime("%Y-%m-%d %H:%M"),
          "todate": d.strftime("%Y-%m-%d %H:%M")}),
        ("date-only stamps",
         {"exchange": "NFO", "symboltoken": tok, "interval": "ONE_DAY",
          "fromdate": (d - timedelta(days=10)).strftime("%Y-%m-%d"),
          "todate": d.strftime("%Y-%m-%d")}),
    ]
    working = None
    for name, p in variants:
        data, err, el = call(smart, smart.getOIData, p)
        n = len(data) if data is not None else 0
        print(f"  {name:<52} {'OK' if err is None else 'ERR'} "
              f"rows={n:<5} {el:.2f}s")
        if err:
            print(f"      -> {err}")
        elif working is None and n:
            working = p
        time.sleep(0.4)
    if working is None:
        print("\n  NO REQUEST SHAPE RETURNED DATA. Everything below would be")
        print("  guesswork, so the probe stops here and reports that honestly.")
        return 0

    # ---- 2. RESPONSE SHAPE -------------------------------------------
    print("\n" + "-" * 78)
    print("2. RESPONSE SHAPE - what fields come back?")
    print("-" * 78)
    data, err, _ = call(smart, smart.getOIData, working)
    if data:
        print(f"  {len(data)} records; type of a record: "
              f"{type(data[0]).__name__}")
        if isinstance(data[0], dict):
            print(f"  keys: {sorted(data[0].keys())}")
        for rec in data[:3]:
            print(f"    {safe(rec, 200)}")
        print("  (records are printed through the scrubber like everything else)")

    # ---- 3. INTERVALS -------------------------------------------------
    print("\n" + "-" * 78)
    print("3. INTERVALS - which are accepted, and at what density?")
    print("-" * 78)
    print(f"  {'interval':<16} {'rows':>6}  note")
    ok_intervals = []
    for iv in INTERVALS:
        data, err, _ = oi(smart, tok, iv, d.replace(hour=9, minute=15),
                          d.replace(hour=15, minute=30))
        n = len(data) if data is not None else 0
        if err:
            print(f"  {iv:<16} {'-':>6}  {err[:60]}")
        else:
            ok_intervals.append((iv, n))
            print(f"  {iv:<16} {n:>6}  one session")
        time.sleep(0.4)

    # ---- 4. PER-CONTRACT OR PER-UNDERLYING ---------------------------
    print("\n" + "-" * 78)
    print("4. GRANULARITY - per CONTRACT, or aggregated per UNDERLYING?")
    print("-" * 78)
    print("  Decisive test: two DIFFERENT strikes of the same underlying and")
    print("  expiry. Identical series => per-underlying. Different => per-")
    print("  contract. Same for CE vs PE, and for the future.")
    iv = ok_intervals[0][0] if ok_intervals else "ONE_DAY"
    sigs = {}
    for k in ("near_ce", "near_ce_otm", "near_pe", "far_ce", "fut"):
        c = C.get(k)
        if not c:
            continue
        data, err, _ = oi(smart, c["token"], iv,
                          d.replace(hour=9, minute=15),
                          d.replace(hour=15, minute=30))
        if err:
            print(f"  {k:<12} {c['symbol']:<26} ERR {err[:44]}")
        else:
            last = safe((data or [])[-1], 60) if data else ""
            sigs[k] = (len(data or []), last)
            print(f"  {k:<12} {c['symbol']:<26} rows={len(data or []):<5} "
                  f"last={last[:56]}")
        time.sleep(0.4)

    def compare(a, b, label):
        """Only adjudicate when BOTH sides actually returned records.

        Two empty responses have identical (empty) signatures, and comparing
        them would print a confident 'PER-UNDERLYING' verdict from no data at
        all - the same failure the candle probe made when it judged a short
        session as a convention mismatch.
        """
        if a not in sigs or b not in sigs:
            print(f"  {label:<38} -> one side missing, not adjudicating")
            return
        if not sigs[a][0] or not sigs[b][0]:
            print(f"  {label:<38} -> {sigs[a][0]} and {sigs[b][0]} rows; "
                  f"an empty response cannot adjudicate")
            return
        same = sigs[a][1] == sigs[b][1]
        print(f"  {label:<38} -> "
              f"{'IDENTICAL' if same else 'DIFFERENT'}"
              f"{': PER-UNDERLYING' if same and a == 'near_ce' and b == 'near_ce_otm' else ''}"
              f"{': PER-CONTRACT' if not same and a == 'near_ce' and b == 'near_ce_otm' else ''}")

    print()
    compare("near_ce", "near_ce_otm", "two strikes, same expiry")
    compare("near_ce", "near_pe", "CE vs PE, same strike and expiry")
    compare("near_ce", "far_ce", "same strike, different expiry")
    c = C.get("eq")
    if c:
        data, err, _ = oi(smart, c["token"], iv,
                          d.replace(hour=9, minute=15),
                          d.replace(hour=15, minute=30), exch="NSE")
        print(f"  cash equity (has no OI) -> "
              f"{'rows=%d' % len(data or []) if err is None else 'ERR ' + err[:50]}")

    # ---- 5. LOOKBACK DEPTH -------------------------------------------
    print("\n" + "-" * 78)
    print("5. LOOKBACK DEPTH - retention, separated from contract life")
    print("-" * 78)
    print("  A near-expiry contract cannot answer this: it did not exist a year")
    print("  ago, so an empty response says nothing about retention. The far-")
    print("  dated contract has been listed for years, so IT bounds retention.")
    for label, key in (("FAR-dated  %s" % C["far_expiry"], "far_ce"),
                       ("NEAR       %s" % C["near_expiry"], "near_ce")):
        c = C.get(key)
        if not c:
            continue
        print(f"\n  {label}   {c['symbol']}")
        print(f"    {'offset':>8} {'date':<12} {'rows':>6}  error")
        ok, bad = [], []
        for off in OFFSETS:
            dd = today - timedelta(days=off)
            while dd.weekday() >= 5:
                dd -= timedelta(days=1)
            data, err, _ = oi(smart, c["token"], iv,
                              dd.replace(hour=9, minute=15),
                              dd.replace(hour=15, minute=30))
            n = len(data) if data is not None else 0
            print(f"    {off:>7}d {dd:%Y-%m-%d} {n:>6}  {err or ''}")
            (ok if n > 0 else bad).append(off)
            time.sleep(0.4)
        if key != "far_ce":
            continue
        deeper = [b for b in bad if b > max(ok)] if ok else []
        if not ok:
            print("    no offset returned data - retention cannot be bisected")
        elif not deeper:
            print(f"    every probed offset returned data; retention is DEEPER "
                  f"than {max(OFFSETS)}d")
        else:
            lo, hi = max(ok), min(deeper)
            print(f"    bracket: {lo}d works, {hi}d does not - bisecting")
            for _ in range(9):
                if hi - lo <= 1:
                    break
                mid = (lo + hi) // 2
                dd = today - timedelta(days=mid)
                while dd.weekday() >= 5:
                    dd -= timedelta(days=1)
                data, err, _ = oi(smart, c["token"], iv,
                                  dd.replace(hour=9, minute=15),
                                  dd.replace(hour=15, minute=30))
                n = len(data) if data is not None else 0
                print(f"      {mid:>5}d {dd:%Y-%m-%d}  rows={n:<5} {err or ''}")
                if n > 0:
                    lo = mid
                else:
                    hi = mid
                time.sleep(0.4)
            print(f"    CUTOFF between {lo}d and {hi}d "
                  f"-> approx {(today - timedelta(days=lo)):%Y-%m-%d}")
            print("    NOTE: this is min(API retention, contract listing date).")
            print("    If it coincides with the contract's listing it bounds")
            print("    nothing about retention, and a longer-dated contract or")
            print("    an expired-contract token would be needed to go deeper.")

    # ---- 6. RECORD CAP ------------------------------------------------
    print("\n" + "-" * 78)
    print("6. RECORD CAP - does a wide window truncate SILENTLY?")
    print("-" * 78)
    print("  getCandleData truncates at 1,611 rows with nothing in the payload")
    print("  saying so. Checking whether getOIData does the same.")
    c = C.get("far_ce") or C["near_ce"]
    for span, iv2 in ((40, iv), (400, "ONE_DAY")):
        frm = (today - timedelta(days=span)).replace(hour=9, minute=15)
        to = (today - timedelta(days=2)).replace(hour=15, minute=30)
        data, err, _ = oi(smart, c["token"], iv2, frm, to)
        if err:
            print(f"  {span:>4}d @ {iv2:<15} ERROR (so it errors, not truncates)"
                  f": {err[:50]}")
        else:
            n = len(data or [])
            first = safe(data[0], 40) if data else ""
            last = safe(data[-1], 40) if data else ""
            print(f"  {span:>4}d @ {iv2:<15} rows={n:<6} first={first[:34]} "
                  f"last={last[:34]}")
            print(f"       -> {'possible SILENT TRUNCATION' if n in (500, 1611) else 'no cap hit at this width'}")
        time.sleep(0.5)

    # ---- 7. TIMESTAMP SEMANTICS --------------------------------------
    print("\n" + "-" * 78)
    print("7. TIMESTAMP SEMANTICS - bar OPEN or bar CLOSE?")
    print("-" * 78)
    print("  Judged from a FULL session's whole stamp SET, never from the")
    print("  endpoints of a session that may be short - the mistake the candle")
    print("  probe made on 2026-08-25 and that section 7 there disproved.")
    if not ok_intervals:
        print("  no interval accepted; cannot establish the convention")
    else:
        iv5 = "FIVE_MINUTE" if any(x[0] == "FIVE_MINUTE" for x in ok_intervals) \
            else ok_intervals[0][0]
        step = 5 if iv5 == "FIVE_MINUTE" else None
        OPEN_G = [(datetime(2000, 1, 1, 9, 15) + timedelta(minutes=5 * i)
                   ).strftime("%H:%M") for i in range(75)]
        CLOSE_G = [(datetime(2000, 1, 1, 9, 20) + timedelta(minutes=5 * i)
                    ).strftime("%H:%M") for i in range(75)]
        c = C["near_ce"]
        verdict, tried = None, 0
        pd_ = today - timedelta(days=3)
        while tried < 10 and verdict is None:
            while pd_.weekday() >= 5:
                pd_ -= timedelta(days=1)
            data, err, _ = oi(smart, c["token"], iv5,
                              pd_.replace(hour=9, minute=15),
                              pd_.replace(hour=15, minute=30))
            tried += 1
            n = len(data or [])
            stamps = []
            for rec in (data or []):
                s = rec.get("time") if isinstance(rec, dict) else (
                    rec[0] if isinstance(rec, (list, tuple)) else "")
                stamps.append(str(s)[11:16])
            if step and n == 75:
                verdict = ("BAR OPEN" if stamps == OPEN_G else
                           "BAR CLOSE" if stamps == CLOSE_G else
                           "FULL SESSION BUT NEITHER GRID - inspect")
                print(f"  {pd_:%Y-%m-%d}: {n} rows  FULL -> {verdict}")
            else:
                print(f"  {pd_:%Y-%m-%d}: {n} rows  "
                      f"{'SHORT - not adjudicating' if n else (err or 'no data')}")
            pd_ -= timedelta(days=1)
            time.sleep(0.4)
        if verdict is None:
            print("\n  No full session found - convention UNDETERMINED.")
            print("  That is an honest unknown, not a mismatch.")
        else:
            print(f"\n  OI stamps: {verdict}")
            print("  getCandleData is BAR OPEN (grid exactly 09:15..15:25).")
            print(f"  JOIN: {'aligns directly' if verdict == 'BAR OPEN' else 'WOULD BE OFF BY ONE BAR'}")

    print("\n" + "=" * 78)
    print("PROBE COMPLETE - nothing written, nothing stored, no order placed")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
