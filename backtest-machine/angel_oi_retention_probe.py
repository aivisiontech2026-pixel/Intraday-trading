"""Phase 3B-0.2: OI retention, corrected. READ ONLY. Inventory only.

WHAT THE PREVIOUS RUN GOT WRONG. It swept token 42635
(NIFTY08SEP2623900CE), watched rows fall 75 -> 74 -> 49 -> 18 -> 22 -> 0,
and called it "RETENTION: weeks". That contract expires 2026-09-08, so it
was listed in mid-August - exactly where its data stops. The declining counts
are a contract coming into existence, not an API forgetting. Its own
attribution column said so: at 21d, 25d and 30d the paired CANDLE call
returned nothing either, which cannot separate a listing wall from
retention. The classifier printed that sentence and emitted the other label.

The classifier now lives in angel_oi_classify.py, cannot see rows whose
paired candle call failed, and is covered by test_oi_classifier.py using the
exact rows from that run.

THE CORRECTED PROBE. Sweep a LIQUID, LONG-LISTED contract and report, at
every offset, whether OI stops BEFORE candles do:

    OI stops before candles      -> retention is real; report the boundary
    they stop together, deep     -> OI availability tracks contract
                                    availability; retention is not binding
    neither stops in range       -> retention exceeds the sweep

ESCALATION. A December 2026 expiry may hit its own listing wall around
mid-2026, which would be contract age one layer deeper. The ladder below runs
longer- and longer-listed contracts until one either diverges or reaches the
deepest offset, with NIFTY futures as the fallback: quarterly, listed well in
advance, and always liquid. Each candidate is checked for NON-ZERO OI in a
recent window first, so a silent zero-OI strike is never read as retention.

This tests no signal, computes no correlation, hit rate or return, stores
nothing, and concludes nothing about tradeability.

CREDENTIAL HYGIENE. install_log_scrubber() is armed before login and re-armed
after. The SDK's own logger prints request headers - the API key on every
AB1012, the Bearer JWT on network errors - through logzero, which this module
does not own. See the comment block in angel_research_io.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import angel_research_io as aio
import angelone_client as angel
from angel_oi_classify import classify, INDETERMINATE, TRACKS

aio.install_log_scrubber()          # before login: env credentials

# Captured from the LIVE scrip master on 2026-09-04 and written here then.
# Angel's master lists live contracts only, so this is the only honest way to
# hold a token that may since have expired. Nothing here is guessed - a wrong
# token returns the same AB1012 as a retention failure, which is exactly the
# confusion this probe exists to avoid.
#
# Ordered by listing age: a longer-dated expiry was listed earlier, so it can
# be queried further back before hitting its own contract-age wall.
LADDER = [
    ("opt_29dec26", "71501", "NIFTY29DEC2624000CE", "2026-12-29", "OPTIDX"),
    ("opt_30mar27", "80272", "NIFTY30MAR2724000CE", "2027-03-30", "OPTIDX"),
    ("opt_29jun27", "71560", "NIFTY29JUN2724000CE", "2027-06-29", "OPTIDX"),
    ("opt_28dec27", "53624", "NIFTY28DEC2724000CE", "2027-12-28", "OPTIDX"),
    ("opt_27jun28", "86151", "NIFTY27JUN2824000CE", "2028-06-27", "OPTIDX"),
    ("fut_29sep26", "68407", "NIFTY29SEP26FUT",     "2026-09-29", "FUTIDX"),
    ("fut_27oct26", "48704", "NIFTY27OCT26FUT",     "2026-10-27", "FUTIDX"),
    ("fut_23nov26", "61471", "NIFTY23NOV26FUT",     "2026-11-23", "FUTIDX"),
]
OFFSETS = [60, 90, 120, 180, 270, 365]
IV = "FIVE_MINUTE"
LOG = []


def safe(x, n=180):
    return aio.safe(x, n)


def _win(d):
    return d.replace(hour=9, minute=15), d.replace(hour=15, minute=30)


def _weekday(d):
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def call(smart, kind, token, interval, frm, to, note=""):
    aio._throttle()
    fn = smart.getOIData if kind == "OI" else smart.getCandleData
    p = {"exchange": "NFO", "symboltoken": token, "interval": interval,
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


def paired(smart, token, d, note):
    """One offset: OI and candles, same token, same window, same interval."""
    f, t = _win(d)
    _, oe, on = call(smart, "OI", token, IV, f, t, note)
    _, ce, cn = call(smart, "CANDLE", token, IV, f, t, note)
    return on, oe, cn, ce


def sweep(smart, key, token, symbol, today):
    """Offsets 60..365 with a paired candle control at every point."""
    rows = []
    print(f"    {'back':>5} {'date':<12} {'OI':>7} {'candles':>8} "
          f"{'diverged':>9}  note")
    for off in OFFSETS:
        d = _weekday(today - timedelta(days=off))
        on, oe, cn, ce = paired(smart, token, d, f"{key} -{off}d")
        div = "YES" if (cn > 0 and on == 0) else ("no" if cn > 0 else "-")
        rows.append({"offset": off, "date": d.strftime("%Y-%m-%d"),
                     "oi_rows": on, "candle_rows": cn})
        why = oe or ce or ("both served" if on and cn else
                           "stopped together" if not on and not cn else "")
        print(f"    {off:>4}d {d:%Y-%m-%d} {on:>7} {cn:>8} {div:>9}  {why[:38]}")
    return rows


def bisect(smart, key, token, today, lo, hi):
    """Narrow a divergence boundary. lo: OI served. hi: OI absent, candles present."""
    print(f"\n    bisecting the divergence between -{lo}d and -{hi}d")
    extra = []
    for _ in range(8):
        if hi - lo <= 2:
            break
        mid = (lo + hi) // 2
        d = _weekday(today - timedelta(days=mid))
        on, oe, cn, ce = paired(smart, token, d, f"{key} bisect -{mid}d")
        extra.append({"offset": mid, "date": d.strftime("%Y-%m-%d"),
                      "oi_rows": on, "candle_rows": cn})
        print(f"      {mid:>4}d {d:%Y-%m-%d}  OI={on:<6} candles={cn:<6} "
              f"{(oe or ce or '')[:34]}")
        if cn == 0:
            print("        candles gone too - this point cannot bound anything")
            hi = mid
            continue
        if on > 0:
            lo = mid
        else:
            hi = mid
    print(f"    -> OI stops between -{lo}d and -{hi}d while candles continue")
    return extra


def main():
    print("=" * 90)
    print("getOIData RETENTION - CORRECTED PROBE (Phase 3B-0.2)")
    print(f"run {datetime.now():%Y-%m-%d %H:%M} IST")
    print("=" * 90)
    print("  Previous label 'weeks' is withdrawn: it came from a contract whose")
    print("  candles stopped where its OI stopped, which is a listing wall, not")
    print("  retention. Classifier rewritten and regression-tested.")

    smart = angel.login()
    if smart is None:
        print("\nLOGIN FAILED. (No credential value is printed.)")
        return 2
    n = aio.install_log_scrubber(smart)
    print(f"\n  authenticated OK; scrubber armed over {n} secret values")

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    recent = _weekday(today - timedelta(days=3))

    live = set()
    try:
        with urllib.request.urlopen(aio.SCRIP_URL, timeout=180) as r:
            raw = json.loads(r.read().decode("utf-8", "replace"))
        live = {str(x.get("token")) for x in raw if x.get("exch_seg") == "NFO"}
        print(f"  current master: {len(live):,} NFO tokens")
    except Exception as e:
        print(f"  could not fetch the master: {safe(e)}")

    print("\n" + "-" * 90)
    print("ESCALATION LADDER - ordered by listing age, longest-listed last")
    print("-" * 90)
    print(f"  {'key':<12} {'token':<7} {'symbol':<24} {'expiry':<12} "
          f"{'in master':<10}")
    for key, tok, sym, exp, kind in LADDER:
        print(f"  {key:<12} {tok:<7} {sym:<24} {exp:<12} "
              f"{('yes' if tok in live else 'NO') if live else '?':<10}")

    verdicts = []
    settled = None
    for key, tok, sym, exp, kind in LADDER:
        print("\n" + "-" * 90)
        print(f"{sym}   token {tok}   expiry {exp}   ({kind})")
        print("-" * 90)

        # LIQUIDITY GATE. A zero-OI strike returns nothing for cause C, and
        # reading that as retention is the error this probe is correcting.
        f, t = _win(recent)
        _, oe, on = call(smart, "OI", tok, IV, f, t, f"{key} recent")
        print(f"  recent liquidity {recent:%Y-%m-%d}: OI rows={on} {oe or ''}")
        if on == 0:
            print("  -> no recent OI. Cause C is not excluded, so this contract")
            print("     cannot test retention. SKIPPED, not counted as evidence.")
            verdicts.append((sym, "SKIPPED", "no recent OI (cause C)"))
            continue

        rows = sweep(smart, key, tok, sym, today)
        label, reason, ev = classify(rows)
        print(f"\n  classify -> {label}")
        print(f"    {reason}")

        if label not in (INDETERMINATE, TRACKS):
            served = [r for r in rows if r["candle_rows"] > 0 and r["oi_rows"] > 0]
            div = [r for r in rows if r["candle_rows"] > 0 and r["oi_rows"] == 0]
            # Bisect only when there is a served point SHALLOWER than the
            # divergence to bracket against. Without one the boundary is at or
            # above the shallowest offset tested, classify() already flags that
            # as an upper bound, and there is nothing to narrow.
            shallower = [r["offset"] for r in served
                         if r["offset"] < min(x["offset"] for x in div)] if div else []
            if shallower:
                rows += bisect(smart, key, tok, today, max(shallower),
                               min(x["offset"] for x in div))
                label, reason, ev = classify(rows)
                print(f"    refined -> {label}: {reason[:120]}")
            else:
                print("    no served offset shallower than the divergence - "
                      "nothing to bracket; the label stands as an upper bound.")
            settled = (sym, label, reason, rows)
            verdicts.append((sym, label, reason))
            print("\n  DIVERGENCE FOUND - retention is measurable here; "
                  "ladder stops.")
            break

        verdicts.append((sym, label, reason))
        if label == TRACKS:
            settled = (sym, label, reason, rows)
            print("\n  OI tracked candles to the deepest offset tested; "
                  "ladder stops.")
            break
        print("  -> INDETERMINATE on this contract; escalating to a "
              "longer-listed one.")

    # ---- REQUEST LOG -------------------------------------------------
    print("\n" + "-" * 90)
    print("FULL REQUEST LOG")
    print("-" * 90)
    print(f"  {'kind':<7} {'token':<7} {'from':<17} {'to':<17} {'rows':>6}  error")
    for r in LOG:
        print(f"  {r['kind']:<7} {r['token']:<7} {r['from']:<17} "
              f"{r['to']:<17} {r['rows']:>6}  {(r['error'] or '')[:32]}")
    print(f"\n  total requests: {len(LOG)}")

    # ---- CLASSIFICATION ----------------------------------------------
    print("\n" + "=" * 90)
    print("CORRECTED CLASSIFICATION")
    print("=" * 90)
    print(f"  {'contract':<26} {'label':<32} why")
    for sym, lab, why in verdicts:
        print(f"  {sym:<26} {lab:<32} {why[:34]}")
    if settled is None:
        print("\n  RETENTION: INDETERMINATE")
        print("  Every contract on the ladder either lacked recent OI or stopped")
        print("  on both sides together. No divergence was observed anywhere, so")
        print("  retention was not measured. The longest continuously-listed")
        print("  liquid instrument reachable would be needed to settle it.")
    else:
        sym, lab, reason, rows = settled
        print(f"\n  RETENTION: {lab}")
        print(f"  settled on {sym}")
        print(f"  {reason}")
        if lab == TRACKS:
            deepest = max(r["offset"] for r in rows if r["oi_rows"] > 0)
            print(f"\n  CONSEQUENCE: OI availability tracks candle availability")
            print(f"  to at least {deepest} days. Retention is not the binding")
            print(f"  constraint - contract listing is. A backfill is therefore")
            print(f"  possible for any contract over the window it existed.")
            print(f"  This report does not design that backfill.")

    print("\n" + "=" * 90)
    print("PROBE COMPLETE - nothing written, nothing stored, no order placed")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    sys.exit(main())
