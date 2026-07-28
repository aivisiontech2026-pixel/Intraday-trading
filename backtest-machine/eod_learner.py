"""
After-market learner (runs ~4:15 PM IST, after market close)
============================================================
Records how the day actually went, grades the morning call, updates the
historical memory used for future similarity searches, and sends a
learning summary to Telegram. Never forgets previous market days.
"""

import json
import os
import sqlite3
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


def _q(dbfile, sql, args=()):
    p = os.path.join(HERE, dbfile)
    if not os.path.exists(p):
        return []
    c = sqlite3.connect(p)
    try:
        return c.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        c.close()


def trading_summary(today_str):
    """Every buy and sell today across both books, plus day totals."""
    lines = ["📊 TRADES TODAY"]
    total_pnl, total_closed, total_wins = 0.0, 0, 0
    any_trades = False

    books = [
        ("STOCKS", "simple_trades.db", "trades", "positions",
         lambda r: f"  {r[0].replace('.NS','')} x{r[1]} @ {r[2]:.2f}"),
        ("OPTIONS", "options_trades.db", "options_trades", "options_positions",
         lambda r: f"  {r[0]} {r[1]} {r[2]:.0f} x{r[3]} @ Rs.{r[4]:.2f}"),
    ]

    for label, dbfile, ttable, ptable, buy_fmt in books:
        if dbfile == "simple_trades.db":
            buys = _q(dbfile, "SELECT symbol,qty,entry FROM positions "
                             "WHERE entry_time LIKE ?", (f"{today_str}%",))
            buys += _q(dbfile, "SELECT symbol,qty,entry FROM trades "
                              "WHERE entry_time LIKE ?", (f"{today_str}%",))
            sells = _q(dbfile, "SELECT symbol,qty,exit,pnl,reason FROM trades "
                              "WHERE exit_time LIKE ?", (f"{today_str}%",))
        else:
            buys = _q(dbfile, "SELECT symbol,option_type,strike,qty,entry_price "
                             "FROM options_positions WHERE entry_time LIKE ?",
                             (f"{today_str}%",))
            buys += _q(dbfile, "SELECT symbol,option_type,strike,qty,entry_price "
                              "FROM options_trades WHERE entry_time LIKE ?",
                              (f"{today_str}%",))
            sells = _q(dbfile, "SELECT symbol,option_type,strike,qty,exit_price,pnl,reason "
                              "FROM options_trades WHERE exit_time LIKE ?",
                              (f"{today_str}%",))

        if not buys and not sells:
            continue
        any_trades = True
        lines.append(f"\n{label}")
        lines.append(f"BUY ({len(buys)}):")
        for r in buys:
            lines.append(buy_fmt(r))

        lines.append(f"SELL ({len(sells)}):")
        for r in sells:
            pnl, reason = r[-2], r[-1]
            emoji = "✅" if pnl > 0 else "❌"
            if dbfile == "simple_trades.db":
                sym, qty, exit_px = r[0], r[1], r[2]
                lines.append(f"  {emoji} {sym.replace('.NS','')} x{qty} @ {exit_px:.2f} "
                            f"| Rs.{pnl:,.0f} | {reason}")
            else:
                sym, opt, strike, qty, exit_px = r[0], r[1], r[2], r[3], r[4]
                lines.append(f"  {emoji} {sym} {opt} {strike:.0f} x{qty} @ Rs.{exit_px:.2f} "
                            f"| Rs.{pnl:,.0f} | {reason}")
            total_pnl += pnl
            total_closed += 1
            total_wins += 1 if pnl > 0 else 0

        # anomaly: still open after square-off (shouldn't normally happen)
        still_open = _q(dbfile, f"SELECT COUNT(*) FROM {ptable}")
        if still_open and still_open[0][0] > 0:
            lines.append(f"⚠️ {still_open[0][0]} position(s) still open in {label} - check dashboard")

    if not any_trades:
        return "📊 TRADES TODAY: none"

    wr = (total_wins / total_closed * 100) if total_closed else 0
    lines.append(f"\nDAY TOTAL: Rs.{total_pnl:,.0f} | {total_closed} closed | "
                f"win rate {wr:.0f}%")
    return "\n".join(lines)


def main():
    today = date.today().isoformat()
    conn = db_init()

    # Trades always get reported, regardless of whether AI-call grading
    # below succeeds - the user wants every buy/sell, not just the AI verdict.
    sections = [f"📈 END OF DAY SUMMARY | {today}", "", trading_summary(today)]

    row = conn.execute(
        "SELECT decision, confidence, nifty_prev_close FROM daily_memory "
        "WHERE date=?", (today,)).fetchone()
    if not row:
        sections.append("\nNo morning AI snapshot for today - nothing to grade.")
    else:
        decision, confidence, prev_close = row

        # Use 5-min intraday bars, not the daily candle - Yahoo often hasn't
        # published today's daily bar yet this soon after close (16:15 IST),
        # which previously caused this script to silently skip with no alert.
        df = yf.download("^NSEI", period="1d", interval="5m", auto_adjust=True,
                         progress=False, multi_level_index=False)
        todays = df[df.index.date == date.today()] if df is not None and not df.empty else df

        if df is None or df.empty:
            print("No Nifty intraday data available.")
            sections.append("\n⚠️ No Nifty intraday data available - AI call not graded.")
        elif todays.empty:
            print("No Nifty data for today (holiday?).")
            sections.append("\nNo Nifty data for today (holiday?) - AI call not graded.")
        else:
            nifty_open = float(todays["Open"].iloc[0])
            nifty_close = float(todays["Close"].iloc[-1])
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
            sections.append("\n📉 MARKET & AI CALL")
            sections.append(
                f"Nifty: {nifty_open:,.0f} -> {nifty_close:,.0f} "
                f"({day_chg:+.2f}% vs prev close, {intraday:+.2f}% intraday)\n"
                f"Day result: {result}\n"
                f"Morning call: {decision} ({confidence:.0f}%) -> {verdict}\n"
                f"Memory: {total_days} days stored | call accuracy {wr:.0f}% over {n} trades")

    msg = "\n".join(sections)
    print(msg)
    telegram(msg)
    conn.close()


if __name__ == "__main__":
    main()
