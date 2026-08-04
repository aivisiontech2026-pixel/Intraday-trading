"""
Live market data verification probe.
====================================
Proves - with real credentials, in CI - that the options pipeline fetches
genuine live market data, by printing every field Angel One returns for a
single contract so you can compare it against your broker app.

    python verify_live_data.py                    # defaults to TCS 1840 CE
    python verify_live_data.py TCS 1840 CE
    python verify_live_data.py MARUTI 14200 CE

Verifies each stage independently, so a failure tells you exactly which
one broke:
    1. login              - credentials + TOTP + IP allowed
    2. instrument master   - contract exists, token/lot size resolved
    3. live quote          - LTP / bid / ask / volume / OI actually returned

Exits non-zero if any stage fails, so CI shows a red run rather than a
green one with a quiet problem.
"""

import sys
from datetime import date, datetime

import angelone_client as angel

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def telegram(msg):
    """Send the report to Telegram so it can be compared on the same phone."""
    try:
        import json
        from pathlib import Path
        cfg = json.loads((Path(__file__).parent / "intraday_config.json")
                         .read_text())
        tg = cfg.get("telegram", {})
        if not (tg.get("bot_token") and tg.get("chat_id")):
            return
        import requests
        requests.post(
            f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
            json={"chat_id": tg["chat_id"], "text": msg}, timeout=10)
    except Exception as e:
        print(f"  (telegram send failed: {e})")


def main():
    name = (sys.argv[1] if len(sys.argv) > 1 else "TCS").upper()
    strike = float(sys.argv[2]) if len(sys.argv) > 2 else 1840.0
    opt_type = (sys.argv[3] if len(sys.argv) > 3 else "CE").upper()
    today = date.today()

    print("=" * 62)
    print(f"LIVE DATA VERIFICATION  |  {name} {strike:.0f} {opt_type}")
    print(f"run at {datetime.now():%Y-%m-%d %H:%M:%S} IST")
    print("=" * 62)

    # --- stage 1: authentication -------------------------------------
    print("\n[1/3] Angel One login")
    smart = angel.login()
    if smart is None:
        print("  FAILED - see the reason above.")
        return 1
    print("  OK - authenticated")

    # --- stage 2: instrument master ----------------------------------
    print("\n[2/3] Instrument master / contract resolution")
    master = angel.load_instrument_master([name])
    if not master:
        print("  FAILED - could not load instrument master")
        return 1

    expiries = angel.list_expiries(master, name, today)
    print(f"  Listed expiries: {', '.join(str(e) for e in expiries[:4])}")

    recs = [r for r in master.get(name, [])
            if abs(r["strike"] - strike) < 0.01 and r["opt_type"] == opt_type]
    if not recs:
        print(f"  FAILED - no listed {name} {strike:.0f} {opt_type} contract")
        return 1
    rec = min(recs, key=lambda r: r["expiry"])
    print(f"  Contract : {rec['symbol']}")
    print(f"  Token    : {rec['token']}")
    print(f"  Expiry   : {rec['expiry']}   Lot size: {rec['lotsize']}")

    # --- stage 3: live quote -----------------------------------------
    print("\n[3/3] Live quote (Angel One Live Market Data API, FULL mode)")
    quotes = angel.get_quotes(smart, [rec["token"]])
    q = quotes.get(rec["token"])
    if not q:
        print("  FAILED - no quote returned for this token")
        return 1

    spread = q["ask"] - q["bid"] if q["bid"] and q["ask"] else 0
    print(f"  LTP            : Rs.{q['ltp']:,.2f}")
    print(f"  Bid            : Rs.{q['bid']:,.2f}")
    print(f"  Ask            : Rs.{q['ask']:,.2f}")
    print(f"  Spread         : Rs.{spread:,.2f}")
    print(f"  Open           : Rs.{q['open']:,.2f}")
    print(f"  High           : Rs.{q['high']:,.2f}")
    print(f"  Low            : Rs.{q['low']:,.2f}")
    print(f"  Prev close     : Rs.{q['close']:,.2f}")
    # Angel One reports volume/OI in SHARES; broker apps almost always
    # display them in LOTS. Show both so the two can be compared without
    # the reader having to know the convention.
    lot = rec["lotsize"]
    print(f"  Volume         : {q['volume']:,} qty   = {q['volume']//lot:,} lots")
    print(f"  Open interest  : {q['oi']:,} qty   = {q['oi']//lot:,} lots")
    print(f"  (lot size {lot} - your broker app most likely shows the LOTS figure)")

    one_lot = q["ask"] * rec["lotsize"] if q["ask"] else q["ltp"] * rec["lotsize"]
    print(f"\n  1 lot at ask   : Rs.{one_lot:,.0f} "
          f"({rec['lotsize']} x Rs.{q['ask'] or q['ltp']:,.2f})")
    print(f"  Per-trade budget: Rs.25,000 -> "
          f"{'TRADEABLE' if one_lot <= 25000 else 'SKIPPED (one lot exceeds budget)'}")

    print("\n" + "=" * 62)
    print("RESULT: live data pipeline VERIFIED end to end.")
    print("Compare LTP/bid/ask/OI above against your broker app.")
    print("Outside market hours these are the last traded values -")
    print("still real exchange data, just not changing.")
    print("=" * 62)

    telegram(
        f"✅ LIVE DATA VERIFIED | {rec['symbol']}\n"
        f"Token: {rec['token']}  Lot: {lot}\n"
        f"Expiry: {rec['expiry']}\n\n"
        f"LTP: Rs.{q['ltp']:,.2f}\n"
        f"Bid: Rs.{q['bid']:,.2f}   Ask: Rs.{q['ask']:,.2f}\n"
        f"Open: Rs.{q['open']:,.2f}  High: Rs.{q['high']:,.2f}  "
        f"Low: Rs.{q['low']:,.2f}\n"
        f"Volume: {q['volume']//lot:,} lots ({q['volume']:,} qty)\n"
        f"OI: {q['oi']//lot:,} lots ({q['oi']:,} qty)\n\n"
        f"1 lot at ask = Rs.{one_lot:,.0f}\n\n"
        f"NOTE: broker apps usually show volume/OI in LOTS.\n"
        f"Prices are last-traded - outside market hours they are the\n"
        f"previous session's close, not a live tick.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
