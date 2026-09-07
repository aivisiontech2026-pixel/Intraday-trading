"""Task B: can an EXPIRED contract be addressed at all? READ ONLY.

This decides whether a real OI backfill is ever possible. Phase 3B-1 found
6,921 of 140,040 contracts addressable, entirely because expired tokens
cannot be named. If any route resolves them, a 4.7-day fetch replaces a
1.9-year wait.

THE DECISIVE TEST NEEDS NO SYMBOL LOOKUP AT ALL. The production trade book
holds 80 tokens for contracts that have since expired, each with the exact
dates it was entered and exited and the prices it filled at. The trade record
itself proves the contract existed AND traded on those dates, so causes A
(not listed) and C (no open interest) are excluded by evidence rather than by
argument. Calling getOIData on one of those tokens, on a date it demonstrably
traded, answers post-expiry addressability directly.

THE VERIFICATION RULE. A resolved token is not a solution until it returns
data. Any token searchScrip hands back is immediately put to getOIData AND
getCandleData on a date the contract was active. A token that resolves but
serves nothing is the same dead end wearing a different mask.

PAIRED CONTROL throughout, as in the retention probe: OI and candle on the
same token and window, reporting divergence rather than depth.

This tests no signal, fetches no backfill, stores nothing, and concludes
nothing about tradeability.

CREDENTIAL HYGIENE. install_log_scrubber() is armed before login and re-armed
after. searchScrip logs its results through the same logzero logger that
prints request headers - the API key on every AB1012, the Bearer JWT on
network errors. See the comment block in angel_research_io.
"""
import datetime as dt
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import angel_research_io as aio
import angelone_client as angel

aio.install_log_scrubber()

IV = "FIVE_MINUTE"
LOG = []

# Captured from the PRODUCTION trade book (options_trades.db on the
# trading-state branch). Each row is a contract this system actually traded:
# the token, the symbol, its expiry, and a session it was open. Nothing is
# guessed or constructed - a wrong token returns the same AB1012 as a
# retention failure, and that confusion is what this probe exists to avoid.
EXPIRED = [
    # token,   trading_symbol,           expiry,       a session it TRADED
    ("41014", "NIFTY11AUG2624550PE", "2026-08-11", "2026-08-10"),
    ("41015", "NIFTY11AUG2624600CE", "2026-08-11", "2026-08-04"),
    ("45103", "NIFTY18AUG2624300PE", "2026-08-18", "2026-08-14"),
    ("45109", "NIFTY18AUG2624450PE", "2026-08-18", "2026-08-12"),
    ("103613", "ICICIBANK25AUG261420CE", "2026-08-25", "2026-08-17"),
    ("103615", "ICICIBANK25AUG261440CE", "2026-08-25", "2026-07-31"),
    ("46992", "NIFTY01SEP2624150PE", "2026-09-01", "2026-08-25"),
    ("46999", "NIFTY01SEP2624350CE", "2026-09-01", "2026-08-26"),
]

# Symbols built from the naming convention observed in the LIVE master
# (NAME + DDMMMYY + STRIKE + CE/PE), for contracts that have expired. These
# test whether searchScrip resolves a symbol it can no longer trade.
SEARCH_PROBES = [
    ("NFO", "NIFTY11AUG2624550PE"),
    ("NFO", "NIFTY18AUG2624300PE"),
    ("NFO", "NIFTY01SEP2624150PE"),
    ("NFO", "ICICIBANK25AUG261420CE"),
    ("NFO", "NIFTY25AUG26FUT"),
    ("NFO", "NIFTY"),                 # control: a live underlying must resolve
    ("NSE", "RELIANCE"),              # control: a live equity must resolve
]


def safe(x, n=200):
    return aio.safe(x, n)


def call(smart, kind, token, frm, to, exch="NFO", note=""):
    aio._throttle()
    fn = smart.getOIData if kind == "OI" else smart.getCandleData
    p = {"exchange": exch, "symboltoken": str(token), "interval": IV,
         "fromdate": frm, "todate": to}
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
    LOG.append({"kind": kind, "token": str(token), "from": frm, "to": to,
                "rows": n, "error": err, "note": note})
    return data, err, n


def paired(smart, token, day, exch, note):
    f, t = f"{day} 09:15", f"{day} 15:30"
    _, oe, on = call(smart, "OI", token, f, t, exch, note)
    _, ce, cn = call(smart, "CANDLE", token, f, t, exch, note)
    return on, oe, cn, ce


def verdict_row(on, cn):
    if on > 0 and cn > 0:
        return "ADDRESSABLE", "both served"
    if on == 0 and cn > 0:
        return "CANDLE ONLY", "candles served, OI not"
    if on > 0 and cn == 0:
        return "OI ONLY", "anomaly - OI without candles"
    return "DEAD", "neither served"


def main():
    print("=" * 92)
    print("EXPIRED-TOKEN ADDRESSABILITY PROBE - READ ONLY, INVENTORY ONLY")
    print(f"run {dt.datetime.now():%Y-%m-%d %H:%M} IST")
    print("=" * 92)

    smart = angel.login()
    if smart is None:
        print("\nLOGIN FAILED. (No credential value is printed.)")
        return 2
    n = aio.install_log_scrubber(smart)
    print(f"  authenticated OK; scrubber armed over {n} secret values")

    today = dt.date.today()

    # ---- 1. THE DECISIVE TEST -----------------------------------------
    print("\n" + "-" * 92)
    print("1. ALREADY-EXPIRED TOKENS FROM THE PRODUCTION TRADE BOOK")
    print("-" * 92)
    print("  Every token below is one this system actually traded, on the date")
    print("  shown, at a recorded fill price. The contract provably existed and")
    print("  traded, so an empty response can only mean post-expiry")
    print("  unaddressability - not 'not listed yet' and not 'no open interest'.")
    print(f"\n  {'token':<8} {'symbol':<24} {'expiry':<12} {'traded':<12} "
          f"{'OI':>5} {'cand':>5}  verdict")
    live_any = False
    for tok, sym, exp, day in EXPIRED:
        e = dt.date.fromisoformat(exp)
        if e >= today:
            print(f"  {tok:<8} {sym:<24} {exp:<12} {'NOT YET EXPIRED':<12} "
                  f"{'-':>5} {'-':>5}  skipped")
            continue
        exch = "NFO"
        on, oe, cn, ce = paired(smart, tok, day, exch, f"expired {sym}")
        v, why = verdict_row(on, cn)
        if on > 0 or cn > 0:
            live_any = True
        print(f"  {tok:<8} {sym:<24} {exp:<12} {day:<12} {on:>5} {cn:>5}  "
              f"{v} ({(oe or ce or why)[:34]})")

    # ---- 2. searchScrip ------------------------------------------------
    print("\n" + "-" * 92)
    print("2. searchScrip - the ONLY symbol-lookup route in the SDK")
    print("-" * 92)
    print("  Route: /rest/secure/angelbroking/order/v1/searchScrip - note it")
    print("  lives under order/v1, not historical/v1, so it is an order-entry")
    print("  helper. The prior is that it returns only tradeable contracts.")
    print(f"\n  {'exch':<5} {'query':<26} {'status':>8} {'hits':>5}  first result")
    resolved = []
    for exch, q in SEARCH_PROBES:
        aio._throttle()
        try:
            r = smart.searchScrip(exch, q)
            ok = isinstance(r, dict) and r.get("status")
            data = (r.get("data") or []) if ok else []
            msg = "" if ok else safe(r.get("message") if isinstance(r, dict) else r)
        except Exception as e:
            ok, data, msg = False, [], safe(e)
        first = ""
        if data:
            d0 = data[0]
            first = f"{d0.get('tradingsymbol')} tok={d0.get('symboltoken')}"
            for d in data:
                ts = str(d.get("tradingsymbol") or "")
                if ts.upper() == q.upper():
                    resolved.append((q, exch, str(d.get("symboltoken"))))
        print(f"  {exch:<5} {q:<26} {'OK' if ok else 'ERR':>8} {len(data):>5}  "
              f"{(first or msg)[:44]}")

    # ---- 3. VERIFY ANYTHING searchScrip RESOLVED ----------------------
    print("\n" + "-" * 92)
    print("3. VERIFICATION - a resolved token is not a solution until it serves")
    print("-" * 92)
    if not resolved:
        print("  searchScrip returned no exact match for any expired symbol.")
        print("  Nothing to verify - and that is itself the finding.")
    else:
        byq = {q: (e, t) for q, e, t in resolved}
        for tok, sym, exp, day in EXPIRED:
            if sym not in byq:
                continue
            exch, rtok = byq[sym]
            print(f"\n  {sym}: searchScrip -> token {rtok}"
                  f"   (trade book recorded {tok})")
            if rtok != tok:
                print("    NOTE: differs from the token this system actually")
                print("    traded. Testing BOTH - a plausible-looking token that")
                print("    serves nothing is the failure mode this guards against.")
            for label, t in (("searchScrip token", rtok), ("trade-book token", tok)):
                on, oe, cn, ce = paired(smart, t, day, exch, f"verify {sym} {label}")
                v, why = verdict_row(on, cn)
                print(f"    {label:<20} token {t:<9} OI={on:<5} candles={cn:<5} "
                      f"{v}  {(oe or ce or '')[:30]}")

    # ---- 4. REQUEST LOG ------------------------------------------------
    print("\n" + "-" * 92)
    print("4. FULL REQUEST LOG")
    print("-" * 92)
    print(f"  {'kind':<7} {'token':<9} {'from':<17} {'to':<17} {'rows':>6}  error")
    for r in LOG:
        print(f"  {r['kind']:<7} {r['token']:<9} {r['from']:<17} {r['to']:<17} "
              f"{r['rows']:>6}  {(r['error'] or '')[:30]}")
    print(f"\n  total requests: {len(LOG)}")

    # ---- VERDICT -------------------------------------------------------
    print("\n" + "=" * 92)
    print("VERDICT")
    print("=" * 92)
    oi_ok = [r for r in LOG if r["kind"] == "OI" and r["rows"] > 0
             and r["note"].startswith("expired")]
    cd_ok = [r for r in LOG if r["kind"] == "CANDLE" and r["rows"] > 0
             and r["note"].startswith("expired")]
    tested = len({r["token"] for r in LOG if r["note"].startswith("expired")})
    print(f"  expired tokens tested: {tested}")
    print(f"  served OI     : {len(oi_ok)}")
    print(f"  served candles: {len(cd_ok)}")
    if tested == 0:
        print("\n  INDETERMINATE - no captured contract had expired at run time.")
        print("  Re-dispatch after the nearest expiry in the list above.")
    elif oi_ok:
        print("\n  HISTORICAL TOKENS ADDRESSABLE, by direct token reuse.")
        print("  An expired contract still serves OI when its token is known.")
        print("  Preserving tokens (Task A) is therefore sufficient, and a real")
        print("  backfill becomes possible for every contract archived from now.")
        if resolved:
            print("  searchScrip additionally resolved expired symbols, which")
            print("  would extend this BACKWARDS to contracts never archived.")
        else:
            print("  searchScrip resolved nothing, so this covers only contracts")
            print("  whose tokens were captured while they were still listed.")
    elif cd_ok:
        print("\n  CANDLES ADDRESSABLE, OI NOT - divergence on expired contracts.")
        print("  Price history survives expiry; open interest does not.")
    else:
        print("\n  NOT ADDRESSABLE - an expired token serves neither OI nor")
        print("  candles. Record-forward is the only path, and Task A is what")
        print("  starts the clock.")

    print("\n" + "=" * 92)
    print("PROBE COMPLETE - nothing written, nothing stored, no order placed")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
