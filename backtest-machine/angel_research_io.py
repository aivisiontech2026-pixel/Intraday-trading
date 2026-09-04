"""Shared READ-ONLY Angel One candle I/O for the signal-research corpus.

Used by angel_gate_direction.py and angel_backfill_5m.py. It exists so the
truncation-handling logic is written ONCE - that is the part most likely to
be silently wrong, and a silently wrong version produces a corpus with holes
that every later result inherits.

MEASURED MECHANICS (from the 2026-09-04 lookback probe, not documentation)
  * record cap 1,611 rows per response, applied by SILENT TRUNCATION - the
    response is a success with fewer rows than the window contains, and
    nothing in the payload says so. The documented cap is 500; it is wrong.
    1,611 rows / 75 bars per session = 21.5 sessions, so any request wider
    than ~21 sessions WILL come back short and look fine.
  * 5-minute lookback reaches at least 1,095 days; the true cutoff is deeper
    than the probe swept.
  * 12 consecutive requests at 1.5 req/s drew no throttling.

WHAT THIS MODULE DOES NOT DO
  no order, no trading call, no state write, no production DB access.
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta

SCRIP_URL = ("https://margincalculator.angelone.in/OpenAPI_File/files/"
             "OpenAPIScripMaster.json")

# Anything credential-shaped is removed before any string is printed.
_SCRUB = re.compile(r"[A-Za-z0-9_\-]{20,}")

# Conservative: the probe measured 1.5 req/s with no throttling and Angel
# documents 3 req/s. Sitting BELOW the measured rate leaves headroom for the
# case where the limiter is bursty rather than absent.
MIN_REQUEST_INTERVAL_S = 0.75

# Silent truncation was measured at 1,611. Treat anything at or above a
# safety margin below it as "possibly truncated" and verify coverage; never
# rely on the number itself, only on whether the RANGE came back.
OBSERVED_RECORD_CAP = 1611
BARS_PER_SESSION = 75
# 18 sessions of 5-minute bars = 1,350 rows, comfortably under the cap even
# if a session ever carries more bars than the standard 75.
CHUNK_SESSIONS = 18

_last_request_at = [0.0]


def safe(e, n=200):
    """Exception or message -> printable text, credential-shaped runs removed."""
    s = str(e) if not isinstance(e, BaseException) else \
        f"{type(e).__name__}: {e}"
    return _SCRUB.sub("<redacted>", s)[:n]


def _throttle():
    dt = time.time() - _last_request_at[0]
    if dt < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - dt)
    _last_request_at[0] = time.time()


# ------------------------------------------------------------- universe ---
def resolve_tokens(stock_names, index_symbols):
    """Resolve NSE tokens from the public scrip master.

    Returns (resolved, unresolved) where resolved is
    {name: {"token","exch","scrip_symbol","kind"}}.

    Nothing is invented. A symbol that does not resolve is REPORTED and
    dropped - it is never replaced by a guess, and index tokens are never
    hardcoded, because a wrong token returns plausible candles for the
    wrong instrument and no later check would catch it.
    """
    with urllib.request.urlopen(SCRIP_URL, timeout=180) as r:
        raw = json.loads(r.read().decode("utf-8", "replace"))
    nse = [x for x in raw if x.get("exch_seg") == "NSE"]

    resolved, unresolved = {}, []
    for name in stock_names:
        want = f"{name}-EQ"
        hits = [x for x in nse if x.get("symbol") == want]
        if len(hits) == 1:
            resolved[name] = {"token": str(hits[0]["token"]), "exch": "NSE",
                              "scrip_symbol": want, "kind": "EQ"}
        else:
            unresolved.append((name, f"{len(hits)} matches for {want}"))

    for name, scrip in index_symbols.items():
        hits = [x for x in nse if x.get("symbol") == scrip]
        if len(hits) == 1:
            resolved[name] = {"token": str(hits[0]["token"]), "exch": "NSE",
                              "scrip_symbol": scrip, "kind": "INDEX"}
        else:
            unresolved.append((name, f"{len(hits)} matches for '{scrip}'"))
    return resolved, unresolved


# ---------------------------------------------------------------- fetch ---
def _one_request(smart, exch, token, interval, frm, to):
    """A single getCandleData call. Returns (rows, error_text)."""
    _throttle()
    try:
        r = smart.getCandleData({
            "exchange": exch, "symboltoken": token, "interval": interval,
            "fromdate": frm.strftime("%Y-%m-%d %H:%M"),
            "todate": to.strftime("%Y-%m-%d %H:%M")})
    except Exception as e:
        return None, safe(e)
    if not isinstance(r, dict):
        return None, f"non-dict response {type(r).__name__}"
    if not r.get("status"):
        return None, safe(r.get("message"))
    return (r.get("data") or []), None


def _ts(row):
    """Angel stamp '2026-08-25T09:15:00+05:30' -> naive IST datetime.

    Angel returns IST with an explicit +05:30 offset; the research corpus
    stores naive IST. Dropping the offset AFTER parsing (rather than string
    slicing) means a change of offset representation cannot silently shift
    every bar by hours.
    """
    d = datetime.fromisoformat(row[0])
    return d.replace(tzinfo=None) if d.tzinfo is None else \
        d.astimezone(d.tzinfo).replace(tzinfo=None)


def fetch_range(smart, exch, token, interval, start, end, bar_minutes=5,
                max_requests=400, log=None):
    """Fetch [start, end] COMPLETELY, defeating silent truncation.

    Returns (rows, spans) where rows is the deduplicated, sorted candle list
    and spans is a per-request audit trail:
        {"from","to","n","last_ts","truncated","error"}

    HOW TRUNCATION IS HANDLED. The response carries no truncation flag, so
    after every request the LAST RETURNED STAMP is compared against the
    requested end. If the response stops short, the remainder is re-requested
    from one bar past the last stamp. That loop is guarded three ways:
      * max_requests, so a pathological server cannot spin forever;
      * a strict no-progress check - if the cursor would not advance, the
        loop stops and records it rather than repeating the same request;
      * an empty-but-successful response advances the cursor by one chunk and
        records a span with n=0, so a holiday run in the middle of the window
        cannot terminate the fetch early and truncate everything after it.
    """
    rows, spans, seen = [], [], set()
    cursor = start
    bar = timedelta(minutes=bar_minutes)
    chunk = timedelta(days=int(CHUNK_SESSIONS * 1.45) + 1)   # calendar pad
    n_req = 0

    while cursor <= end and n_req < max_requests:
        n_req += 1
        req_to = min(cursor + chunk, end)
        got, err = None, None
        for attempt in range(4):
            got, err = _one_request(smart, exch, token, interval,
                                    cursor, req_to)
            if err is None:
                break
            time.sleep(1.5 * (2 ** attempt))       # 1.5, 3, 6, 12 s
        span = {"from": cursor.isoformat(timespec="minutes"),
                "to": req_to.isoformat(timespec="minutes"),
                "n": 0, "last_ts": None, "truncated": False,
                "exhausted": False, "error": err}

        if err is not None:
            # Four attempts failed. Record the hole and step past it - one
            # bad span must not abort the remaining years.
            spans.append(span)
            if log:
                log(f"      {cursor:%Y-%m-%d}..{req_to:%Y-%m-%d} ERROR {err}")
            cursor = req_to + bar
            continue

        span["n"] = len(got)
        for r in got:
            t = _ts(r)
            if t not in seen:
                seen.add(t)
                rows.append((t, float(r[1]), float(r[2]), float(r[3]),
                             float(r[4]), int(r[5])))

        if not got:
            # Successful and empty: a genuine data-free span (holidays, or
            # pre-listing). Recorded, not interpolated, and NOT treated as
            # end-of-data.
            spans.append(span)
            cursor = req_to + bar
            continue

        last = _ts(got[-1])
        span["last_ts"] = last.isoformat(timespec="minutes")
        # Covered iff the response reaches the last bar the window could
        # contain. One session of slack absorbs a final holiday.
        span["truncated"] = last < (req_to - timedelta(days=1))
        spans.append(span)
        if log and span["truncated"]:
            log(f"      TRUNCATED {cursor:%Y-%m-%d}..{req_to:%Y-%m-%d} "
                f"-> {len(got)} rows, stopped at {last:%Y-%m-%d %H:%M}; "
                f"re-requesting the remainder")

        # EXHAUSTION vs A STUCK SERVER. These look identical from the cursor
        # (neither advances) but mean opposite things, and the first run
        # conflated them: the final chunk of every symbol reaches the current
        # incomplete session, the response carries nothing after the cursor,
        # and that was logged as "NO PROGRESS" - an error span for what is
        # simply the end of the data. It cost nothing (744/744 sessions were
        # still complete) but it recurs on every run and it makes a clean run
        # look like a damaged one.
        #
        # The two are separable by asking whether the response contained any
        # bar STRICTLY AFTER the cursor. None at all, having already returned
        # data, is exhaustion. Genuine pathology - a server repeating one bar
        # forever - is still caught, by the request budget and by the coverage
        # report, which lists missing sessions from the data rather than from
        # this loop's opinion of it.
        if last <= cursor:
            span["exhausted"] = True
            # Exhaustion is benign ONLY if it happens at the end of the
            # requested window. Stopping far short is the signature of a
            # server repeating one bar, and that must not be filed as "no
            # more data" - it would hand back a corpus of one session and
            # call it complete.
            short_by = end - last
            if short_by > timedelta(days=7):
                span["error"] = (f"EXHAUSTED {short_by.days} DAYS SHORT of the "
                                 f"requested end - not end-of-data")
            if log:
                log(f"      end of data at {last:%Y-%m-%d %H:%M} "
                    f"(requested to {end:%Y-%m-%d %H:%M})"
                    f"{' - SHORT, flagged' if span['error'] else ' - not an error'}")
            break
        nxt = last + bar
        if nxt <= cursor:
            spans.append({"from": cursor.isoformat(timespec="minutes"),
                          "to": req_to.isoformat(timespec="minutes"),
                          "n": 0, "last_ts": span["last_ts"],
                          "truncated": True, "exhausted": False,
                          "error": "NO PROGRESS - cursor would not advance"})
            break
        cursor = nxt

    if n_req >= max_requests and cursor <= end:
        spans.append({"from": cursor.isoformat(timespec="minutes"),
                      "to": end.isoformat(timespec="minutes"), "n": 0,
                      "last_ts": None, "truncated": True, "exhausted": False,
                      "error": f"REQUEST BUDGET EXHAUSTED after {n_req}"})
    rows.sort(key=lambda x: x[0])
    return rows, spans
