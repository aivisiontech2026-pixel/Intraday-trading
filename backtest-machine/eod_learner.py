"""
After-market learner (runs ~4:15 PM IST, after market close)
============================================================
Records how the day actually went, grades the morning call, updates the
historical memory used for future similarity searches, and sends a
learning summary to Telegram. Never forgets previous market days.
"""

import json
import os
import sys
from datetime import date

import requests
import yfinance as yf

from market_memory import db_init, update_eod, win_rate

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "intraday_config.json")))


def telegram(msg):
    tg = CFG.get("telegram", {})
    if not (tg.get("bot_token") and tg.get("chat_id")):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
            json={"chat_id": tg["chat_id"], "text": msg}, timeout=10)
    except Exception as e:
        print(f"  (telegram failed: {e})")


def main():
    today = date.today().isoformat()
    conn = db_init()
    row = conn.execute(
        "SELECT decision, confidence, nifty_prev_close FROM daily_memory "
        "WHERE date=?", (today,)).fetchone()
    if not row:
        print(f"No morning snapshot for {today} - nothing to learn.")
        return
    decision, confidence, prev_close = row

    df = yf.download("^NSEI", period="2d", interval="1d", auto_adjust=True,
                     progress=False, multi_level_index=False)
    if df is None or df.empty or str(df.index[-1].date()) != today:
        print("No Nifty data for today (holiday?) - skipping.")
        return

    nifty_open = float(df["Open"].iloc[-1])
    nifty_close = float(df["Close"].iloc[-1])
    ref = prev_close or nifty_open
    day_chg = round((nifty_close - ref) / ref * 100, 2)
    intraday = round((nifty_close - nifty_open) / nifty_open * 100, 2)

    if day_chg > 0.15:
        result = "BULL"
    elif day_chg < -0.15:
        result = "BEAR"
    else:
        result = "FLAT"

    correct = None
    if decision == "BUY CALL":
        correct = 1 if intraday > 0 else 0
    elif decision == "BUY PUT":
        correct = 1 if intraday < 0 else 0

    update_eod(conn, today, {
        "result": result,
        "nifty_open": round(nifty_open, 2),
        "nifty_close": round(nifty_close, 2),
        "day_change_pct": day_chg,
        "call_correct": correct,
        "notes": f"intraday {intraday:+.2f}%",
    })

    n, wr = win_rate(conn)
    total_days = conn.execute(
        "SELECT COUNT(*) FROM daily_memory WHERE result IS NOT NULL").fetchone()[0]

    verdict = ("SKIPPED (no trade)" if correct is None
               else "CORRECT" if correct else "WRONG")
    msg = "\n".join([
        f"MARKET CLOSE LEARNING | {today}",
        f"Nifty: {nifty_open:,.0f} -> {nifty_close:,.0f} "
        f"({day_chg:+.2f}% vs prev close, {intraday:+.2f}% intraday)",
        f"Day result: {result}",
        f"Morning call: {decision} ({confidence:.0f}%) -> {verdict}",
        f"Memory: {total_days} days stored | call accuracy {wr:.0f}% over {n} trades",
    ])
    print(msg)
    telegram(msg)
    conn.close()


if __name__ == "__main__":
    main()
