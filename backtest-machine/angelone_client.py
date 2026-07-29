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
        return None
    api_key = os.environ.get("ANGEL_API_KEY")
    client_code = os.environ.get("ANGEL_CLIENT_CODE")
    pin = os.environ.get("ANGEL_PIN")
    totp_secret = os.environ.get("ANGEL_TOTP_SECRET")
    if not all([api_key, client_code, pin, totp_secret]):
        return None

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


def last_thursday_of_month(year, month):
    """The real NSE monthly stock-option expiry: last Thursday of the month."""
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = first_of_next - timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7  # 3 = Thursday
    return last_day - timedelta(days=offset)


def next_monthly_expiry(today: date) -> date:
    candidate = last_thursday_of_month(today.year, today.month)
    if candidate < today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        candidate = last_thursday_of_month(y, m)
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
