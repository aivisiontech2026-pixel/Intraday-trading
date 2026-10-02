"""ONE_MINUTE NFO candle feasibility probe. READ ONLY. Inventory only.

Settles ONE fact: does Angel One serve ONE_MINUTE historical candles
(getCandleData) for NFO option tokens? Phase 6B's disposition is C if yes;
its D contingency applies if no.

It tests no signal, computes no P&L, places no order and writes nothing to
the repository. The only Angel calls are login and getCandleData.

PRE-REGISTERED CONTRACTS - fixed BEFORE any request was made, from
trading-state ce5606921cf5ab2344a078523a39be25fc7b44f5 read anonymously on
2026-10-02. Do not change these after seeing data.

  A  BANKNIFTY27OCT2654200PE  token 49420   NFO  expiry 2026-10-27
     rule: index option quoted on 2026-10-01, expiry after 2026-10-03, most
     quote_snapshot rows on 2026-10-01 (91 rows; runner-up 87).
  B  RELIANCE27OCT261180PE    token 106673  NFO  expiry 2026-10-27
     rule: the same, restricted to stock options (314 rows; runner-up 196).
  X  NOT ESTABLISHED. No contract expiring 2026-09-29 or 2026-09-30 was
     quoted on its own expiry day: the trader's nearest_expiry(min_dte=1)
     excludes same-day contracts, and 29SEP26 contracts were last quoted
     2026-09-25 15:12. Requests #5 and #6 are therefore NOT RUN. No
     substitute rule is applied.
  E  RELIANCE-EQ              token 2885    NSE  (B's underlying; agreed by
     the corpus `batch` table and the cached scrip master).
  W  NIFTY06OCT2622550PE      token 40700   NFO  expiry 2026-10-06  (WEEKLY)
     rule: NFO index options quoted on 2026-10-01 with expiry after
     2026-10-03; "weekly" = expiry is NOT the last expiry of that
     underlying in that calendar month, per the instrument master's expiry
     list (backtest-machine/scripmaster_cache.json, dated 2026-09-07:
     NIFTY Oct-2026 = 2026-10-06, 2026-10-27; BANKNIFTY Oct-2026 =
     2026-10-27). Of the 11 contracts in the population, W is the ONLY
     weekly (87 rows on 2026-10-01); every other is a BANKNIFTY 27OCT26
     monthly. No runner-up, no tie. Token matches the master entry.

REQUEST PLAN (fixed order; controls first)
  #1  E NSE ONE_MINUTE  2026-10-01 09:15 -> 15:30    control
  #2  A NFO FIVE_MINUTE 2026-10-01 09:15 -> 15:30    control
  #3  A NFO ONE_MINUTE  2026-10-01 09:15 -> 15:30    decides the verdict
  #4  B NFO ONE_MINUTE  2026-10-01 09:15 -> 15:30
  #5  X                                               NOT RUN (X not established)
  #6  X                                               NOT RUN (X not established)
  #7' W NFO ONE_MINUTE  2026-10-01 09:15 -> 15:30    weekly-contract result
  If #1 AND #2 both fail (API status false or an exception), the probe
  stops: nothing about ONE_MINUTE NFO could be concluded.

PRE-REGISTERED INTERPRETATION
  * Rule 1.7 is applied to "bars inside the 2026-10-01 session AND with
    OHLC" jointly; `bars_in_session_with_ohlc` records that joint count.
  * The overall verdict (AVAILABLE / NOT AVAILABLE / NOT ESTABLISHED)
    rests on #3 ALONE and covers MONTHLY contracts.
  * #7' is reported under its own heading as a WEEKLY-contract result, by
    the same rule 1.7 conditions and the same controls (#1, #2).
  * If #3 is AVAILABLE and #7' is not, both are reported. They are never
    merged into one verdict, and the disposition is not revised in this run.

LIMITS
  At most 12 getCandleData calls in total, retries included, spaced at
  least 1.0 s apart. One retry per request, and only on a transient network
  exception (connection error or timeout). No rate-limit testing.

CREDENTIAL HYGIENE
  Credentials are read from the environment only to log in and are never
  printed. Login failure records only "login failed" plus the API error
  code (or the exception TYPE) - never the message, which is why
  angelone_client.login() is not reused: it prints the API message and
  discards the code. The SDK's own credential-printing logger is silenced
  and both streams are redacted by angel_research_io.install_log_scrubber().
  The result file is scanned for every credential value (except an
  all-digit value under 8 characters - the PIN; see _secrets), every
  session token, JWT shape and Bearer header before it is written; raw
  response bodies are withheld if the scan finds anything.
"""
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import angel_research_io as aio
aio.install_log_scrubber()          # armed before anything can fail

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "candle_probe_out")
MAX_REQUESTS = 12
MIN_SPACING_S = 1.0
SESSION_DAY = "2026-10-01"

CONTRACTS = {
    "A": {"symbol": "BANKNIFTY27OCT2654200PE", "token": "49420", "exch": "NFO"},
    "B": {"symbol": "RELIANCE27OCT261180PE", "token": "106673", "exch": "NFO"},
    "E": {"symbol": "RELIANCE-EQ", "token": "2885", "exch": "NSE"},
    "W": {"symbol": "NIFTY06OCT2622550PE", "token": "40700", "exch": "NFO"},
}
PLAN = [
    ("1", "E", "ONE_MINUTE", "2026-10-01 09:15", "2026-10-01 15:30"),
    ("2", "A", "FIVE_MINUTE", "2026-10-01 09:15", "2026-10-01 15:30"),
    ("3", "A", "ONE_MINUTE", "2026-10-01 09:15", "2026-10-01 15:30"),
    ("4", "B", "ONE_MINUTE", "2026-10-01 09:15", "2026-10-01 15:30"),
    ("5", "X", None, None, None),
    ("6", "X", None, None, None),
    ("7'", "W", "ONE_MINUTE", "2026-10-01 09:15", "2026-10-01 15:30"),
]

_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,}")
_BEARER = re.compile(r"(?i)Bearer\s+\S+")
_calls = [0]
_last = [0.0]


def _transient_types():
    try:
        import requests
        return (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
    except Exception:
        return (ConnectionError, TimeoutError)


def probe_login():
    """(smart, None) on success; (None, 'errorcode ...' or exception TYPE)."""
    try:
        import pyotp
        from SmartApi import SmartConnect
    except Exception as e:
        return None, f"sdk import failed ({type(e).__name__})"
    keys = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN",
            "ANGEL_TOTP_SECRET")
    if any(not os.environ.get(k) for k in keys):
        return None, "credential environment variable missing"
    try:
        smart = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
        data = smart.generateSession(
            os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PIN"],
            pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now())
    except Exception as e:
        return None, f"exception {type(e).__name__}"
    if not isinstance(data, dict) or not data.get("status"):
        code = data.get("errorcode") if isinstance(data, dict) else None
        return None, f"errorcode {code!r}"
    return smart, None


def _space():
    wait = MIN_SPACING_S - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


def _in_session(ts):
    """True for a bar stamped on SESSION_DAY between 09:15 and 15:29."""
    try:
        d = datetime.fromisoformat(ts)
    except Exception:
        return False
    hm = d.hour * 60 + d.minute
    return d.strftime("%Y-%m-%d") == SESSION_DAY and 555 <= hm < 930


def _has_ohlc(bar):
    try:
        return len(bar) >= 5 and all(float(x) == float(x) for x in bar[1:5])
    except Exception:
        return False


def candle_request(smart, rid, key, interval, frm, to):
    c = CONTRACTS[key]
    rec = {"request": rid, "contract": key, "symbol": c["symbol"],
           "token": c["token"], "exchange": c["exch"], "interval": interval,
           "fromdate": frm, "todate": to, "attempts": []}
    transient = _transient_types()
    for attempt in (1, 2):
        if _calls[0] >= MAX_REQUESTS:
            rec["attempts"].append({"skipped": "request cap reached"})
            break
        _space()
        _calls[0] += 1
        t0 = time.time()
        try:
            r = smart.getCandleData({
                "exchange": c["exch"], "symboltoken": c["token"],
                "interval": interval, "fromdate": frm, "todate": to})
        except transient as e:
            rec["attempts"].append({"exception": type(e).__name__,
                                    "latency_s": round(time.time() - t0, 3)})
            if attempt == 1:
                continue                     # one retry, transient only
            break
        except Exception as e:
            rec["attempts"].append({"exception": type(e).__name__,
                                    "latency_s": round(time.time() - t0, 3)})
            break
        lat = round(time.time() - t0, 3)
        if not isinstance(r, dict):
            rec["attempts"].append({"latency_s": lat,
                                    "non_dict": type(r).__name__})
            break
        data = r.get("data") or []
        rec["attempts"].append({"latency_s": lat})
        rec.update({
            "api_status": bool(r.get("status")),
            "errorcode": aio.safe(r.get("errorcode") or ""),
            "message": aio.safe(r.get("message") or ""),
            "bar_count": len(data),
            "first_bar_ts": data[0][0] if data else None,
            "last_bar_ts": data[-1][0] if data else None,
            "fields_per_bar": sorted({len(b) for b in data}) if data else [],
            "first_three_bars": data[:3],
            "bars_in_session_day": sum(1 for b in data if _in_session(b[0])),
            "bars_with_ohlc": sum(1 for b in data if _has_ohlc(b)),
            "bars_in_session_with_ohlc": sum(
                1 for b in data if _in_session(b[0]) and _has_ohlc(b)),
            "raw_body": r,
        })
        if interval == "ONE_MINUTE" and frm.startswith(SESSION_DAY):
            seen = {datetime.fromisoformat(b[0]).strftime("%H:%M")
                    for b in data if _in_session(b[0])}
            grid = [f"{m // 60:02d}:{m % 60:02d}" for m in range(555, 930)]
            missing = [g for g in grid if g not in seen]
            rec["minutes_expected"] = len(grid)
            rec["minutes_missing"] = len(missing)
            rec["missing_minutes_first_10"] = missing[:10]
        break
    rec["succeeded"] = bool(rec.get("api_status"))
    return rec


def _secrets(smart):
    vals = set()
    for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN",
              "ANGEL_TOTP_SECRET"):
        v = os.environ.get(k)
        if not v or len(v) < 4:
            continue
        # All-digit values shorter than 8 characters (the PIN) are left out
        # of the SUBSTRING scan. The PIN is used only at login and is never
        # returned by any API, so it cannot appear in a response body - but
        # a short digit run occurs naturally in prices, volumes and
        # timestamps, and scanning for it would withhold every clean
        # response. The JWT, Bearer and session-token scans are unchanged.
        if v.isdigit() and len(v) < 8:
            continue
        vals.add(v)
    for attr in ("access_token", "refresh_token", "feed_token"):
        v = getattr(smart, attr, None) if smart is not None else None
        if isinstance(v, str) and len(v) >= 8:
            vals.add(v)
    return vals


def _scan(text, secrets):
    hits = [len(v) for v in secrets if v in text]
    return {"secret_values_found": len(hits),
            "jwt_shapes_found": len(_JWT.findall(text)),
            "bearer_found": len(_BEARER.findall(text))}


def _clean(scan):
    return not any(scan.values())


def write_result(result, smart):
    """Scan, withhold raw bodies on any hit, rescan, then write."""
    os.makedirs(OUT_DIR, exist_ok=True)
    secrets = _secrets(smart)
    text = json.dumps(result, indent=1, default=str)
    scan = _scan(text, secrets)
    result["credential_scan_full"] = scan
    if not _clean(scan):
        for rec in result.get("requests", []):
            rec.pop("raw_body", None)
        result["raw_bodies"] = "WITHHELD - credential scan hit"
        text = json.dumps(result, indent=1, default=str)
        scan2 = _scan(text, secrets)
        result["credential_scan_after_withholding"] = scan2
        if not _clean(scan2):
            with open(os.path.join(OUT_DIR, "SCAN_FAILED.txt"), "w") as f:
                f.write("Credential scan failed twice. Nothing written.\n")
            return False
        text = json.dumps(result, indent=1, default=str)
    else:
        text = json.dumps(result, indent=1, default=str)
    with open(os.path.join(OUT_DIR, "probe_result.json"), "w",
              encoding="utf-8") as f:
        f.write(text)
    return True


def main():
    started = datetime.now().isoformat(timespec="seconds")
    print("=" * 72)
    print("ONE_MINUTE NFO CANDLE PROBE - READ ONLY")
    print(f"started {started} (process TZ: {time.tzname[0]})")
    print("=" * 72)
    result = {"started": started, "contracts": CONTRACTS,
              "contract_X": "NOT ESTABLISHED - see module docstring",
              "requests": [], "stopped": None}

    smart, err = probe_login()
    aio.install_log_scrubber(smart)          # runtime tokens join the set
    if smart is None:
        result["login"] = f"login failed - {err}"
        result["stopped"] = "login failed"
        print(f"  {result['login']}")
        write_result(result, None)
        return 2
    result["login"] = "ok"
    print("  login ok")

    for rid, key, interval, frm, to in PLAN:
        if key == "X":
            result["requests"].append({
                "request": rid, "contract": "X",
                "status": "NOT RUN - contract X not established"})
            print(f"  #{rid}  NOT RUN - contract X not established")
            continue
        rec = candle_request(smart, rid, key, interval, frm, to)
        result["requests"].append(rec)
        print(f"  #{rid}  {key} {rec['exchange']:<3} {interval:<11} "
              f"{frm} -> {to}  status={rec.get('api_status')} "
              f"code={rec.get('errorcode') or '-'} "
              f"bars={rec.get('bar_count')} "
              f"in-session={rec.get('bars_in_session_day')} "
              f"in-session+ohlc={rec.get('bars_in_session_with_ohlc')}")
        if rid == "2":
            r1, r2 = result["requests"][0], result["requests"][1]
            if not r1.get("succeeded") and not r2.get("succeeded"):
                result["stopped"] = "controls #1 and #2 both failed"
                print("  STOP - both controls failed; probe is broken.")
                break

    result["calls_made"] = _calls[0]
    result["finished"] = datetime.now().isoformat(timespec="seconds")
    ok = write_result(result, smart)
    print(f"  getCandleData calls made: {_calls[0]} (cap {MAX_REQUESTS})")
    print(f"  result written: {ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
