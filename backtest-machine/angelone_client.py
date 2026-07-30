"""
Angel One SmartAPI client - fetches real market-implied volatility so
options_trader.py can price options against real market conditions
instead of a flat guessed IV.

Fully automated: logs in fresh on every run using a TOTP secret (no
daily token caching/refresh needed). Every function fails soft - on any
error (bad credentials, network issue, IP not whitelisted, symbol not
covered) it returns None/{} so options_trader.py falls back to its own
IV estimate rather than crashing.
"""

import os
from datetime import date, timedelta

try:
    import pyotp
    from SmartApi import SmartConnect
    AVAILABLE = True
except ImportError:
    AVAILABLE = False


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
    api_key, client_code, pin, totp_secret = (
        creds["ANGEL_API_KEY"], creds["ANGEL_CLIENT_CODE"],
        creds["ANGEL_PIN"], creds["ANGEL_TOTP_SECRET"])

    try:
        totp = pyotp.TOTP(totp_secret).now()
        smart = SmartConnect(api_key=api_key)
        data = smart.generateSession(client_code, pin, totp)
        if not isinstance(data, dict) or not data.get("status"):
            msg = data.get("message") if isinstance(data, dict) else data
            print(f"  Angel One login failed: {msg}")
            return None
        return smart
    except Exception as e:
        print(f"  Angel One login error: {type(e).__name__}: {e}")
        return None


def _to_ddmmmyyyy(d: date) -> str:
    return d.strftime("%d%b%Y").upper()


EXPIRY_WEEKDAY = 1  # Tuesday (Mon=0). NSE moved F&O expiry off Thursday;
                    # verified against a live MARUTI option chain showing
                    # 25 Aug / 29 Sep / 27 Oct 2026 - all Tuesdays, and all
                    # exactly the last Tuesday of their month.


def last_expiry_weekday_of_month(year, month):
    """The real NSE monthly stock-option expiry: last Tuesday of the month."""
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = first_of_next - timedelta(days=1)
    offset = (last_day.weekday() - EXPIRY_WEEKDAY) % 7
    return last_day - timedelta(days=offset)


def next_monthly_expiry(today: date) -> date:
    """Nearest monthly expiry strictly AFTER today.

    Deliberately excludes today itself: a contract expiring today has
    essentially no time value left and is a fundamentally different
    (0DTE) instrument than what this trend-following strategy is built
    for - and on the real expiry day the contract may no longer be
    listed/tradeable at all.
    """
    candidate = last_expiry_weekday_of_month(today.year, today.month)
    if candidate <= today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        candidate = last_expiry_weekday_of_month(y, m)
    return candidate


def fetch_option_iv(smart, symbol, expiry_date: date):
    """Real market-implied volatility for every strike of symbol+expiry.

    Returns {(strike, "CE"/"PE"): iv_fraction}, or {} on any failure
    (including "this exact expiry isn't listed" - e.g. querying a weekly
    date for a stock that only has monthly contracts).
    """
    if smart is None:
        return {}
    try:
        resp = smart.optionGreek({
            "name": symbol,
            "expirydate": _to_ddmmmyyyy(expiry_date),
        })
        if not isinstance(resp, dict) or not resp.get("status"):
            return {}
        out = {}
        for row in resp.get("data", []):
            try:
                strike = float(row["strikePrice"])
                opt_type = row["optionType"]  # "CE" or "PE"
                iv_pct = float(row["impliedVolatility"])
                if iv_pct > 0:
                    out[(strike, opt_type)] = iv_pct / 100.0
            except (KeyError, ValueError, TypeError):
                continue
        return out
    except Exception as e:
        print(f"  Angel One optionGreek fetch failed for {symbol}: "
              f"{type(e).__name__}: {e}")
        return {}
