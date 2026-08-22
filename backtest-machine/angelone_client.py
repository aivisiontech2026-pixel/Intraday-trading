"""
Angel One SmartAPI client - LIVE options market data.
=====================================================
This module is the single source of truth for real option market data:
instrument tokens, real listed expiries, and live quotes (LTP, bid, ask,
volume, open interest).

Design contract
---------------
Every function fails SOFT (returns None / {} / []) rather than raising, so
a credentials/network/API problem degrades the caller gracefully instead of
crashing the trading run. Callers MUST check for empty results - see
options_trader.py, which refuses to trade an option at all when no live
quote is available (it never substitutes a synthetic price).

Verified facts (checked against the live instrument master, 2026-07-30):
  - Instrument master: 152,299 records, ~35 MB JSON, refreshed daily.
  - Option records carry: token, symbol, name, expiry, strike, lotsize,
    instrumenttype ("OPTSTK" stocks / "OPTIDX" indices), exch_seg ("NFO").
  - STRIKE IS STORED IN PAISE: "1390000.000000" means a Rs.13,900 strike.
    Divide by 100.
  - Expiry format is DDMMMYYYY, e.g. "25AUG2026".
  - All NSE F&O expiries are TUESDAYS (NSE moved off Thursday).
  - NIFTY has weekly expiries; BANKNIFTY and single stocks are MONTHLY only.
  - Lot sizes are large (RELIANCE 500, MARUTI 50, WIPRO 3000, NIFTY 65,
    BANKNIFTY 30) - options must be sized in whole lots.

Why REST snapshots and not the WebSocket feed
---------------------------------------------
SmartWebSocketV2 exists and would stream ticks, but this bot runs as a
short-lived batch process fired by an external cron every ~15 minutes: it
starts, evaluates, trades, and exits. A streaming socket only pays off in a
long-lived process that holds state between ticks. For a batch run, the
correct primitive is a point-in-time snapshot, which is exactly what the
quote endpoint returns - including the same LTP/depth/volume/OI the socket
would deliver. Switching to WebSocket would require changing the execution
model to a persistent daemon, which is a separate architectural decision.
"""

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import pyotp
    import requests
    from SmartApi import SmartConnect
    AVAILABLE = True
except ImportError:
    AVAILABLE = False

HERE = Path(__file__).parent
SCRIP_MASTER_URL = ("https://margincalculator.angelone.in/OpenAPI_File/files/"
                    "OpenAPIScripMaster.json")
SCRIP_CACHE = HERE / "scripmaster_cache.json"
SCRIP_CACHE_MAX_AGE_H = 20  # refreshed daily by Angel One; re-pull each day
# S-34a: TOTAL budget for one instrument-master refresh, covering connect,
# TLS, response wait and body download. See _fetch_master for why a plain
# requests timeout cannot express this and for the exact bound claimed.
SCRIP_REFRESH_BUDGET_S = 20
# Ceiling on any single socket operation. The actual value passed to
# requests is derived from the REMAINING budget, so it can only ever be
# smaller than this. It also fixes the worst-case overshoot: a stall that
# begins just after a deadline check can run one socket timeout long.
SCRIP_REFRESH_SOCKET_CAP_S = 5
# Body is consumed in chunks THIS size, and the deadline is re-checked
# between them - so the chunk size IS the checking granularity. It must
# stay small: iter_content blocks until a whole chunk has accumulated, so
# a 1 MB chunk on a degraded link can block for a long time before the
# first check ever runs. That was measured: with a 1 MB chunk the bound
# did not hold at all against a real trickling socket. At 32 KB the check
# fires roughly every 32 KB / bandwidth - well under a second even on a
# badly degraded link - and the ~1,200 iterations needed for a 37 MB
# payload cost nothing measurable.
SCRIP_REFRESH_CHUNK_B = 32 * 1024
# S-33: how old a cached master may be and still be served as a FALLBACK
# after a refresh has failed. This is NOT the refresh interval above - a
# cache older than SCRIP_CACHE_MAX_AGE_H is still re-downloaded first, and
# this bound only decides whether the existing copy may stand in when that
# download fails. See load_instrument_master for why staleness is safe.
SCRIP_CACHE_MAX_STALE_H = 72

# Tuesday (Mon=0). NSE moved F&O expiry off Thursday. Used only as a
# fallback when the instrument master is unavailable - when it IS
# available we read the real listed expiry dates instead of computing them.
EXPIRY_WEEKDAY = 1

QUOTE_BATCH = 50   # API hard limit: max 50 tokens per quote request
NFO = "NFO"


# ----------------------------------------------------------------- auth ---
def login():
    """Returns an authenticated SmartConnect session, or None on failure."""
    if not AVAILABLE:
        print("  Angel One: smartapi-python/pyotp not installed")
        return None
    creds = {
        "ANGEL_API_KEY": os.environ.get("ANGEL_API_KEY"),
        "ANGEL_CLIENT_CODE": os.environ.get("ANGEL_CLIENT_CODE"),
        "ANGEL_PIN": os.environ.get("ANGEL_PIN"),
        "ANGEL_TOTP_SECRET": os.environ.get("ANGEL_TOTP_SECRET"),
    }
    missing = [k for k, v in creds.items() if not v]
    if missing:
        print(f"  Angel One: credential(s) not set - {', '.join(missing)}")
        return None
    try:
        totp = pyotp.TOTP(creds["ANGEL_TOTP_SECRET"]).now()
        smart = SmartConnect(api_key=creds["ANGEL_API_KEY"])
        data = smart.generateSession(creds["ANGEL_CLIENT_CODE"],
                                     creds["ANGEL_PIN"], totp)
        if not isinstance(data, dict) or not data.get("status"):
            msg = data.get("message") if isinstance(data, dict) else data
            print(f"  Angel One login failed: {msg}")
            return None
        return smart
    except Exception as e:
        print(f"  Angel One login error: {type(e).__name__}: {e}")
        return None


# ----------------------------------------------------- instrument master ---
_MASTER_CACHE = None   # in-process memo: {name: [option records]}

# S-33: how the master was obtained this process. Diagnostic only - no
# trading branch reads it. `status` is one of OK / STALE / DOWN / CORRUPT /
# EMPTY / MISSING / UNIVERSE_MISMATCH / STALE_EXPIRED / UNKNOWN.
_MASTER_HEALTH = {"status": "UNKNOWN", "age_h": None, "reason": None}


def master_health():
    """Copy of how the instrument master resolved this process.

    Exposed so a caller can record it. Nothing reads it today: wiring it
    into telemetry would mean editing options_trader.py, which is outside
    the authorized surface for S-33, so the classification is also printed
    to stdout - the channel this module already uses and the one the
    workflow captures in run_output.txt and the Actions log.
    """
    return dict(_MASTER_HEALTH)


def _set_health(status, age_h=None, reason=None):
    _MASTER_HEALTH.update(status=status, age_h=age_h, reason=reason)


def _cache_age_h():
    """Age of the cached CONTENT in hours, or None when there is no cache.

    mtime is the content age because the file has exactly one writer -
    the successful-download branch below. A failed download never touches
    it, so failure cannot walk the age forward, and P0.6 established that
    actions/cache restores mtime from the tar header rather than stamping
    it with the restore time.
    """
    if not SCRIP_CACHE.exists():
        return None
    return (time.time() - SCRIP_CACHE.stat().st_mtime) / 3600


def _cache_is_fresh():
    age_h = _cache_age_h()
    return age_h is not None and age_h < SCRIP_CACHE_MAX_AGE_H


class MasterRefreshTimeout(Exception):
    """The refresh exceeded its total budget. Subclasses Exception so the
    existing handler in load_instrument_master routes it to S-33 exactly
    like any other refresh failure."""


def _fetch_master(url, budget_s=None, now=None, session_factory=None):
    """Download and parse the instrument master within a TOTAL budget.

    WHY NOT requests.get(timeout=N)
    -------------------------------
    `timeout` is a per-socket-operation timeout: connect, then the gap
    BETWEEN reads. It says nothing about total elapsed time. A body
    delivered as a slow but steady trickle never exceeds the inter-byte
    gap, so the call runs as long as the server wants. Measured in a
    controlled local experiment: a body streamed in 20 chunks 0.8s apart
    completed in 16.0s against timeout=2 - an 8x overshoot. That is the
    mechanism behind the 219.5s cycle observed on 2026-08-21 against a
    180s setting, and it is why the socket timeout is a floor on the
    failure cost, not a ceiling.

    Redirects compound it further: each hop is granted a FRESH timeout, so
    the total is the sum over hops. `allow_redirects=False` removes that
    entirely - this URL serves 200 directly, and a redirect appearing in
    future is treated as a refresh failure, which fails closed into S-33.

    THE BOUND THIS ACTUALLY CLAIMS
    ------------------------------
    Not an exact wall-clock guarantee. The deadline is monotonic and is
    checked between chunks, so a stall starting just after a check runs
    for at most one socket timeout before the read itself gives up:

        total <= budget + SCRIP_REFRESH_SOCKET_CAP_S + parse

    with parse measured at ~0.2s for a 37 MB payload. An exact bound would
    need a watchdog thread or signal-based interruption, both larger than
    this change. The bound above is what the tests assert.

    The deadline is checked BETWEEN chunks, so SCRIP_REFRESH_CHUNK_B is
    the granularity of enforcement, not a performance knob. A body
    smaller than one chunk yields a single blocking read, bounded then
    only by the socket timeout - acceptable here because the real payload
    is ~37 MB, i.e. ~1,200 chunks.

    monotonic, not wall clock: immune to NTP steps and DST. `now` and
    `session_factory` are injectable so the deadline can be exercised
    deterministically without a network or a real clock.
    """
    budget_s = SCRIP_REFRESH_BUDGET_S if budget_s is None else budget_s
    now = now or time.monotonic
    deadline = now() + budget_s

    def remaining():
        return deadline - now()

    def socket_timeout():
        # Derived from what is LEFT, never more than the cap.
        return max(0.1, min(SCRIP_REFRESH_SOCKET_CAP_S, remaining()))

    session = (session_factory or requests.Session)()
    try:
        if remaining() <= 0:
            raise MasterRefreshTimeout(
                f"budget {budget_s}s exhausted before the request began")
        t = socket_timeout()
        resp = session.get(url, timeout=(t, t), stream=True,
                           allow_redirects=False)
        resp.raise_for_status()          # 4xx/5xx
        if resp.status_code != 200:      # 3xx: no redirect is followed
            raise MasterRefreshTimeout(
                f"unexpected status {resp.status_code} (redirects disabled)")
        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=SCRIP_REFRESH_CHUNK_B):
            if remaining() <= 0:
                raise MasterRefreshTimeout(
                    f"total budget {budget_s}s exceeded after "
                    f"{len(buf):,} bytes")
            if chunk:
                buf.extend(chunk)
        return json.loads(bytes(buf))
    finally:
        try:
            session.close()
        except Exception:
            pass


def _read_cache():
    """(raw, reason). reason is None on success, else EMPTY/CORRUPT/MISSING.

    A truncated file fails json.loads and is reported CORRUPT - which is
    the point: a partial write leaves a FRESH mtime on BAD content, so age
    alone must never be taken as evidence of validity.
    """
    try:
        text = SCRIP_CACHE.read_text(encoding="utf-8")
    except Exception:
        return None, "MISSING"
    if not text.strip():
        return None, "EMPTY"
    try:
        return json.loads(text), None
    except Exception:
        return None, "CORRUPT"


def _covers_universe(out):
    """Can this master actually support candidate generation?

    The functional requirement, taken from the entry path rather than
    invented: consider() needs nearest_expiry(min_dte=1) to return an
    expiry and find_option to return a contract on it. So at least one
    underlying must offer BOTH a CE and a PE on some expiry at or after
    today + min_dte. A master whose every expiry has passed satisfies no
    caller and is not a usable master, however well-formed it is.

    Deliberately NOT a record-count or "N of 20 symbols" threshold - no
    such threshold exists in this repository and inventing one would be
    arbitrary. Per-underlying gaps are already handled downstream, where
    consider() traces entry_skipped/no_listed_expiry.
    """
    floor = date.today() + timedelta(days=1)
    for recs in out.values():
        by_expiry = {}
        for r in recs:
            if r["expiry"] >= floor:
                by_expiry.setdefault(r["expiry"], set()).add(r["opt_type"])
        if any(t == {"CE", "PE"} for t in by_expiry.values()):
            return True
    return False


def _index_master(raw, universe):
    """(records, reason) - structural validation plus the existing index.

    The indexing loop is unchanged, so a healthy master produces exactly
    the mapping it always did. Validation only ever REJECTS; it can never
    admit a record the previous code would have dropped.
    """
    if not isinstance(raw, list):
        return {}, "CORRUPT"
    if not raw:
        return {}, "EMPTY"

    wanted = set(universe) if universe else None
    out = {}
    for r in raw:
        if not isinstance(r, dict):     # records must be mappings
            continue
        if r.get("exch_seg") != NFO:
            continue
        if r.get("instrumenttype") not in ("OPTSTK", "OPTIDX"):
            continue
        name = r.get("name")
        if wanted is not None and name not in wanted:
            continue
        sym = r.get("symbol", "")
        opt_type = sym[-2:]
        if opt_type not in ("CE", "PE"):
            continue
        try:
            rec = {
                "token": str(r["token"]),
                "symbol": sym,
                "name": name,
                # expiry: "25AUG2026" -> date
                "expiry": datetime.strptime(r["expiry"], "%d%b%Y").date(),
                # STRIKE IS IN PAISE in the master file - convert to rupees
                "strike": float(r["strike"]) / 100.0,
                "lotsize": int(float(r["lotsize"])),
                "opt_type": opt_type,
            }
        except (KeyError, ValueError, TypeError):
            continue                     # malformed record: skip, never serve
        out.setdefault(name, []).append(rec)

    if not out or not _covers_universe(out):
        return {}, "UNIVERSE_MISMATCH"
    return out, None


def load_instrument_master(universe=None):
    """Download (or reuse a <20h cache of) the instrument master and index
    the NFO option contracts we care about.

    Returns {name: [record, ...]} where each record is normalised to:
        {token, symbol, name, expiry (date), strike (rupees),
         lotsize (int), opt_type ("CE"/"PE")}
    Returns {} when no usable master can be obtained.

    Only option rows for `universe` are kept - the raw file is ~35 MB /
    152k records, and we need at most a few hundred of them.

    S-33 STALE FALLBACK
    -------------------
    A refresh failure no longer discards a usable local copy. On
    2026-08-21 a valid Aug-20 cache was restored at 09:15:46 IST, the
    refresh failed, and this function returned {} anyway - so the options
    book had no candidates for 4h12m while the data it needed sat on
    disk. _cache_is_fresh() was being used as an ADMISSION test; it is
    now only a PREFERENCE test.

    Order is unchanged for every healthy case: fresh cache first, then
    download. Only the failure path is new - the on-disk copy is
    re-examined and may be served for up to SCRIP_CACHE_MAX_STALE_H.

    TWO INDEPENDENT GATES. Age and validity never substitute for one
    another. Young does not imply valid: an interrupted cache write
    leaves fresh mtime on truncated content. Valid does not imply young
    enough: a well-formed master from last month is still refused. Both
    must hold before anything is served as STALE.

    WHY STALENESS IS SAFE HERE, bounded rather than assumed. Selection
    filters expiries against TODAY's date (list_expiries via
    nearest_expiry), not against the master's vintage, so an old file can
    only ever offer FEWER valid expiries - never an expired contract.
    New expiries are appended at the far end of the ladder, so the
    nearest one cannot be missing. find_option picks from the listed
    strike ladder and never invents a strike. And every entry still
    requires a live quote and fills at LIVE_ASK, so a contract this file
    got wrong yields no quote and therefore no trade. Measured over
    Aug-18..21: 95 of 95 contracts production selected resolve
    identically days later - same token, lot, strike and expiry.
    """
    global _MASTER_CACHE
    if _MASTER_CACHE is not None:
        return _MASTER_CACHE
    if not AVAILABLE:
        _set_health("DOWN", reason="smartapi-python/pyotp not installed")
        return {}

    # 1. FRESH CACHE - preferred, and unchanged from before S-33.
    if _cache_is_fresh():
        age_h = _cache_age_h()
        raw, why = _read_cache()
        if raw is not None:
            out, reason = _index_master(raw, universe)
            if out:
                print(f"  Instrument master: using cache ({len(raw):,} records)")
                _MASTER_CACHE = out
                _set_health("OK", age_h)
                print(f"  Instrument master: indexed "
                      f"{sum(len(v) for v in out.values()):,} option contracts "
                      f"across {len(out)} underlyings")
                return out
            why = reason
        # A fresh copy that is unusable is NOT served. Fall through and
        # refresh, exactly as before - a corrupt cache has always been
        # recoverable by downloading, and that stays true.
        print(f"  Instrument master: cached copy unusable ({why}) - refreshing")

    # 2. REFRESH.
    try:
        print(f"  Instrument master: downloading (~35 MB, "
              f"{SCRIP_REFRESH_BUDGET_S}s budget)...")
        raw = _fetch_master(SCRIP_MASTER_URL)
        print(f"  Instrument master: downloaded {len(raw):,} records")
        out, reason = _index_master(raw, universe)
        if not out:
            # Downloaded something unusable. Do not cache it - overwriting
            # a good cache with this would destroy the fallback.
            print(f"  Instrument master: downloaded copy REJECTED ({reason})")
            _MASTER_CACHE = out
            _set_health(reason, 0.0, "download validated as unusable")
            return out
        try:
            SCRIP_CACHE.write_text(json.dumps(raw), encoding="utf-8")
        except Exception:
            pass  # cache write is best-effort
        _MASTER_CACHE = out
        _set_health("OK", 0.0)
        print(f"  Instrument master: indexed "
              f"{sum(len(v) for v in out.values()):,} option contracts "
              f"across {len(out)} underlyings")
        return out
    except Exception as e:
        dl_err = f"{type(e).__name__}: {e}"
        print(f"  Instrument master download FAILED: {dl_err}")

    # 3. S-33 FALLBACK. The refresh failed; the last known-good copy may
    #    still be serviceable. Age is checked first because it is the
    #    cheap policy gate - there is no reason to parse ~35 MB we have
    #    already decided we may not use. Validity is then checked
    #    independently, and BOTH must pass.
    age_h = _cache_age_h()
    if age_h is None:
        print("  Instrument master: no local cache to fall back on")
        _set_health("MISSING", reason=dl_err)
        return {}
    if age_h > SCRIP_CACHE_MAX_STALE_H:
        print(f"  Instrument master: cache is {age_h:.1f}h old, beyond the "
              f"{SCRIP_CACHE_MAX_STALE_H}h fallback bound - NOT used")
        _set_health("STALE_EXPIRED", age_h, dl_err)
        return {}
    raw, why = _read_cache()
    if raw is None:
        print(f"  Instrument master: cache unusable ({why}) - no master")
        _set_health(why, age_h, dl_err)
        return {}
    out, reason = _index_master(raw, universe)
    if not out:
        print(f"  Instrument master: cache unusable ({reason}) - no master")
        _set_health(reason, age_h, dl_err)
        return {}
    _MASTER_CACHE = out
    _set_health("STALE", age_h, dl_err)
    print(f"  Instrument master: refresh failed - using STALE cache "
          f"({age_h:.1f}h old, within {SCRIP_CACHE_MAX_STALE_H}h): "
          f"{sum(len(v) for v in out.values()):,} option contracts "
          f"across {len(out)} underlyings")
    return out


def list_expiries(master, name, on_or_after: date):
    """Real listed expiry dates for `name`, sorted ascending.

    This is ground truth from the exchange's own instrument file - strictly
    better than computing "last Tuesday of month", which cannot know about
    holiday shifts or which underlyings actually have weekly contracts.
    """
    recs = master.get(name) or []
    return sorted({r["expiry"] for r in recs if r["expiry"] >= on_or_after})


def nearest_expiry(master, name, today: date, min_dte=1):
    """Nearest real expiry at least `min_dte` days out.

    min_dte=1 deliberately excludes same-day (0DTE) contracts: near-zero
    time value, and a fundamentally different instrument from what a
    trend-following strategy targets.
    """
    exps = list_expiries(master, name, today + timedelta(days=min_dte))
    return exps[0] if exps else None


def find_option(master, name, expiry: date, spot: float, opt_type: str):
    """The listed contract for `name` whose strike is nearest to `spot`.

    Returns the normalised record (token/symbol/strike/lotsize/...) or None
    if this underlying has no listed contracts for that expiry. Selecting
    from the real listed strike ladder means we can never invent a strike
    that the exchange doesn't actually trade.
    """
    recs = [r for r in (master.get(name) or [])
            if r["expiry"] == expiry and r["opt_type"] == opt_type]
    if not recs:
        return None
    return min(recs, key=lambda r: abs(r["strike"] - spot))


# ------------------------------------------------------------- live data ---
def get_quotes(smart, tokens):
    """Live NFO quotes for a list of instrument tokens.

    Returns {token: {ltp, bid, ask, volume, oi, open, high, low, close}}.
    Missing/failed tokens are simply absent from the result - callers must
    treat "absent" as "no live price", never as zero.

    Uses the FULL mode of the Live Market Data API, which returns LTP plus
    the best-5 depth ladder, traded volume and open interest in one call.
    Batched at 50 tokens (the documented per-request maximum).
    """
    if smart is None or not tokens:
        return {}
    out = {}
    tokens = [str(t) for t in tokens]
    for i in range(0, len(tokens), QUOTE_BATCH):
        batch = tokens[i:i + QUOTE_BATCH]
        # Receive time, recorded for latency measurement ONLY. It is never
        # used as a substitute for the exchange feed time - that would
        # measure our own latency and mislabel it market staleness.
        received_at = datetime.now().isoformat(timespec="microseconds")
        try:
            resp = smart.getMarketData("FULL", {NFO: batch})
        except Exception as e:
            print(f"  Angel One quote fetch failed ({len(batch)} tokens): "
                  f"{type(e).__name__}: {e}")
            continue
        if not isinstance(resp, dict) or not resp.get("status"):
            msg = resp.get("message") if isinstance(resp, dict) else resp
            print(f"  Angel One quote error: {msg}")
            continue
        for row in (resp.get("data") or {}).get("fetched", []) or []:
            try:
                token = str(row["symbolToken"])
                depth = row.get("depth") or {}
                buys = depth.get("buy") or []
                sells = depth.get("sell") or []
                ltp = float(row.get("ltp") or 0)
                out[token] = {
                    "ltp": ltp,
                    "bid": float(buys[0]["price"]) if buys else 0.0,
                    "ask": float(sells[0]["price"]) if sells else 0.0,
                    "volume": int(float(row.get("tradeVolume") or 0)),
                    "oi": int(float(row.get("opnInterest") or 0)),
                    "open": float(row.get("open") or 0),
                    "high": float(row.get("high") or 0),
                    "low": float(row.get("low") or 0),
                    "close": float(row.get("close") or 0),
                    "trading_symbol": row.get("tradingSymbol", ""),
                    # ---- temporal observability (P0-E), ADDITIVE ----------
                    # The FULL response carries exchange-side timestamps
                    # that were previously parsed away, which is why quote
                    # age has never been measurable. Preserved verbatim -
                    # None when the broker omits them. Nothing reads these
                    # for a trading decision; they exist so staleness can
                    # be MEASURED instead of assumed.
                    "exch_feed_time": row.get("exchFeedTime"),
                    "exch_trade_time": row.get("exchTradeTime"),
                    "received_at": received_at,
                }
            except (KeyError, ValueError, TypeError):
                continue
        unfetched = (resp.get("data") or {}).get("unfetched") or []
        if unfetched:
            print(f"  Angel One: {len(unfetched)} token(s) unfetched")
    return out


def fetch_option_iv(smart, symbol, expiry_date: date):
    """Real market implied volatility per strike, for ANALYTICS ONLY.

    Returns {(strike, "CE"/"PE"): iv_fraction} or {}.

    NOT used for execution pricing - execution uses the live quote from
    get_quotes(). This feeds fair-value/mispricing analytics where a
    model price is genuinely wanted.
    """
    if smart is None:
        return {}
    try:
        resp = smart.optionGreek({
            "name": symbol,
            "expirydate": expiry_date.strftime("%d%b%Y").upper(),
        })
        if not isinstance(resp, dict) or not resp.get("status"):
            return {}
        out = {}
        for row in resp.get("data", []):
            try:
                iv_pct = float(row["impliedVolatility"])
                if iv_pct > 0:
                    out[(float(row["strikePrice"]), row["optionType"])] = iv_pct / 100.0
            except (KeyError, ValueError, TypeError):
                continue
        return out
    except Exception as e:
        print(f"  Angel One optionGreek fetch failed for {symbol}: "
              f"{type(e).__name__}: {e}")
        return {}


# ------------------------------------------- fallback expiry computation ---
def last_expiry_weekday_of_month(year, month):
    """Last Tuesday of a month. FALLBACK ONLY - prefer list_expiries()."""
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = first_of_next - timedelta(days=1)
    offset = (last_day.weekday() - EXPIRY_WEEKDAY) % 7
    return last_day - timedelta(days=offset)


def next_monthly_expiry(today: date) -> date:
    """Nearest monthly expiry strictly after today. FALLBACK ONLY."""
    candidate = last_expiry_weekday_of_month(today.year, today.month)
    if candidate <= today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        candidate = last_expiry_weekday_of_month(y, m)
    return candidate
