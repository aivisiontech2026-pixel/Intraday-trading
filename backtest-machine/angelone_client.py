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


def _cache_is_fresh():
    if not SCRIP_CACHE.exists():
        return False
    age_h = (time.time() - SCRIP_CACHE.stat().st_mtime) / 3600
    return age_h < SCRIP_CACHE_MAX_AGE_H


def load_instrument_master(universe=None):
    """Download (or reuse a <20h cache of) the instrument master and index
    the NFO option contracts we care about.

    Returns {name: [record, ...]} where each record is normalised to:
        {token, symbol, name, expiry (date), strike (rupees),
         lotsize (int), opt_type ("CE"/"PE")}
    Returns {} on any failure.

    Only option rows for `universe` are kept - the raw file is ~35 MB /
    152k records, and we need at most a few hundred of them.
    """
    global _MASTER_CACHE
    if _MASTER_CACHE is not None:
        return _MASTER_CACHE
    if not AVAILABLE:
        return {}

    raw = None
    if _cache_is_fresh():
        try:
            raw = json.loads(SCRIP_CACHE.read_text(encoding="utf-8"))
            print(f"  Instrument master: using cache ({len(raw):,} records)")
        except Exception:
            raw = None
    if raw is None:
        try:
            print("  Instrument master: downloading (~35 MB)...")
            resp = requests.get(SCRIP_MASTER_URL, timeout=180)
            resp.raise_for_status()
            raw = resp.json()
            print(f"  Instrument master: downloaded {len(raw):,} records")
            try:
                SCRIP_CACHE.write_text(json.dumps(raw), encoding="utf-8")
            except Exception:
                pass  # cache write is best-effort
        except Exception as e:
            print(f"  Instrument master download FAILED: "
                  f"{type(e).__name__}: {e}")
            return {}

    wanted = set(universe) if universe else None
    out = {}
    for r in raw:
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
            continue
        out.setdefault(name, []).append(rec)

    _MASTER_CACHE = out
    if out:
        print(f"  Instrument master: indexed {sum(len(v) for v in out.values()):,} "
              f"option contracts across {len(out)} underlyings")
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
