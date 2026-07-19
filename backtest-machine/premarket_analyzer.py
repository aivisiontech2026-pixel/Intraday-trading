"""
Pre-market AI analyzer (runs ~8:15 AM IST, before market open)
==============================================================
Pipeline:  global markets -> India technicals -> news sentiment
        -> option chain (best-effort) -> historical similarity
        -> rule-based decision engine (+optional Claude reasoning)
        -> Telegram report -> store snapshot in market_memory.db

Decision rules:
  - 6 signals: global cues, Asia, volatility, news sentiment,
    Nifty trend, historical similarity
  - trade only if >= 4 of 6 non-neutral signals agree
  - confidence < 75%  -> NO TRADE
  - output: BUY CALL / BUY PUT / NO TRADE with entry, SL, targets

Objective is NOT to predict the market — it is to maximize probability
while minimizing risk. NSE option-chain/FII-DII endpoints usually block
cloud IPs; those signals degrade gracefully to "unavailable".
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime

import requests
import yfinance as yf

from market_memory import db_init, save_morning, find_similar, win_rate

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "intraday_config.json")))

STOCKS = CFG.get("symbols", [])

# ------------------------------------------------------------------ telegram ---
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

# ------------------------------------------------------------------ data ---
def pct_change(ticker, period="5d"):
    """Last close vs previous close, in %. None on failure."""
    try:
        df = yf.download(ticker, period=period, interval="1d",
                         auto_adjust=True, progress=False,
                         multi_level_index=False)
        if df is None or len(df) < 2:
            return None, None
        last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
        return round((last - prev) / prev * 100, 2), last
    except Exception:
        return None, None


def fetch_global():
    g = {}
    for key, tkr in [("sp500", "^GSPC"), ("nasdaq", "^IXIC"), ("dow", "^DJI"),
                     ("nikkei", "^N225"), ("hangseng", "^HSI"),
                     ("ftse", "^FTSE"), ("crude", "CL=F"), ("gold", "GC=F"),
                     ("dxy", "DX-Y.NYB")]:
        chg, last = pct_change(tkr)
        g[f"{key}_chg"], g[f"{key}_last"] = chg, last
    _, vix = pct_change("^VIX")
    g["vix"] = round(vix, 2) if vix else None
    _, us10y = pct_change("^TNX")
    g["us10y"] = round(us10y / 10, 2) if us10y else None  # ^TNX is yield*10
    asia = [v for v in (g["nikkei_chg"], g["hangseng_chg"]) if v is not None]
    g["asia_chg"] = round(sum(asia) / len(asia), 2) if asia else None
    return g


def fetch_india():
    d = {}
    try:
        ndf = yf.download("^NSEI", period="1y", interval="1d",
                          auto_adjust=True, progress=False,
                          multi_level_index=False)
        close = ndf["Close"]
        d["nifty_prev_close"] = round(float(close.iloc[-1]), 2)
        d["ema20"] = round(float(close.ewm(span=20).mean().iloc[-1]), 2)
        d["ema50"] = round(float(close.ewm(span=50).mean().iloc[-1]), 2)
        d["ema200"] = round(float(close.ewm(span=200).mean().iloc[-1]), 2)
        tr = (ndf["High"] - ndf["Low"]).rolling(14).mean()
        d["atr"] = round(float(tr.iloc[-1]), 1)
        d["support"] = round(float(ndf["Low"].tail(20).min()), 1)
        d["resistance"] = round(float(ndf["High"].tail(20).max()), 1)
        # trend score: +1 per EMA the close is above, -1 per EMA below
        c = d["nifty_prev_close"]
        d["trend_score"] = sum(1 if c > e else -1
                               for e in (d["ema20"], d["ema50"], d["ema200"]))
    except Exception as e:
        print(f"  nifty daily failed: {e}")
    _, bn = pct_change("^NSEBANK")
    d["banknifty_prev_close"] = round(bn, 2) if bn else None
    _, ivix = pct_change("^INDIAVIX")
    d["india_vix"] = round(ivix, 2) if ivix else None
    return d


def fetch_option_chain():
    """Best-effort NSE option chain -> PCR + max pain. Cloud IPs are
    usually blocked; returns (None, None) then."""
    try:
        s = requests.Session()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                   "Accept-Language": "en-US,en;q=0.9",
                   "Referer": "https://www.nseindia.com/option-chain"}
        s.get("https://www.nseindia.com", headers=headers, timeout=10)
        r = s.get("https://www.nseindia.com/api/option-chain-indices"
                  "?symbol=NIFTY", headers=headers, timeout=10)
        data = r.json()["records"]["data"]
        ce_oi = sum(x["CE"]["openInterest"] for x in data if "CE" in x)
        pe_oi = sum(x["PE"]["openInterest"] for x in data if "PE" in x)
        pcr = round(pe_oi / ce_oi, 2) if ce_oi else None
        strikes = sorted({x["strikePrice"] for x in data})
        best, best_pain = None, float("inf")
        for k in strikes:
            pain = sum(x["CE"]["openInterest"] * max(0, k - x["strikePrice"])
                       for x in data if "CE" in x)
            pain += sum(x["PE"]["openInterest"] * max(0, x["strikePrice"] - k)
                        for x in data if "PE" in x)
            if pain < best_pain:
                best, best_pain = k, pain
        return pcr, best
    except Exception as e:
        print(f"  option chain unavailable ({type(e).__name__}) - expected on cloud IPs")
        return None, None


# ------------------------------------------------------------------ news ---
BULL_WORDS = ["surge", "rally", "gain", "record high", "jump", "soar", "boost",
              "upgrade", "beats", "strong", "growth", "buy", "bullish",
              "rate cut", "stimulus", "profit rise", "optimis"]
BEAR_WORDS = ["fall", "crash", "drop", "plunge", "slump", "selloff", "sell-off",
              "fear", "downgrade", "miss", "weak", "recession", "bearish",
              "rate hike", "inflation worry", "tension", "war", "sanction",
              "tumble", "loss", "decline"]


def fetch_news():
    """Headlines from Google News RSS + keyword sentiment in [-5, +5]."""
    queries = ["Nifty OR Sensex stock market",
               "RBI OR SEBI India markets",
               "US Fed OR crude oil impact India"]
    heads = []
    for q in queries:
        try:
            url = ("https://news.google.com/rss/search?q="
                   + requests.utils.quote(q) + "&hl=en-IN&gl=IN&ceid=IN:en")
            r = requests.get(url, timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                t = item.findtext("title") or ""
                if t:
                    heads.append(t)
        except Exception as e:
            print(f"  news query failed: {e}")
    heads = list(dict.fromkeys(heads))[:30]

    score = 0
    for h in heads:
        hl = h.lower()
        score += sum(1 for w in BULL_WORDS if w in hl)
        score -= sum(1 for w in BEAR_WORDS if w in hl)
    sentiment = max(-5, min(5, round(score / max(len(heads), 1) * 10, 1)))
    return sentiment, heads[:8]


def stock_candidates():
    """Momentum ranking of the trading universe -> call/put candidates."""
    mom = []
    for sym in STOCKS:
        try:
            df = yf.download(sym, period="10d", interval="1d",
                             auto_adjust=True, progress=False,
                             multi_level_index=False)
            if df is None or len(df) < 6:
                continue
            r5 = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-6]) - 1) * 100
            mom.append((sym.replace(".NS", ""), round(r5, 2)))
        except Exception:
            continue
    mom.sort(key=lambda x: -x[1])
    return mom[:3], mom[-3:]


# ------------------------------------------------------------------ engine ---
def decide(g, ind, sentiment, pcr, similar):
    """6 signals -> direction votes -> confidence -> decision."""
    signals = {}

    def vote(v):  # +1 bull, -1 bear, 0 neutral
        return 1 if v > 0 else (-1 if v < 0 else 0)

    us = [v for v in (g["sp500_chg"], g["nasdaq_chg"], g["dow_chg"]) if v is not None]
    us_avg = sum(us) / len(us) if us else 0
    signals["global"] = vote(us_avg if abs(us_avg) >= 0.25 else 0)
    signals["asia"] = vote(g["asia_chg"] if g["asia_chg"] and abs(g["asia_chg"]) >= 0.25 else 0)

    vix, ivix = g.get("vix"), ind.get("india_vix")
    if vix and vix > 25 or (ivix and ivix > 20):
        signals["volatility"] = -1            # high vol -> bearish caution
    elif vix and vix < 16 and (ivix or 99) < 15:
        signals["volatility"] = 1
    else:
        signals["volatility"] = 0

    signals["news"] = vote(sentiment if abs(sentiment) >= 1 else 0)
    ts = ind.get("trend_score", 0)
    signals["trend"] = vote(ts if abs(ts) >= 2 else 0)

    hist_note = "no history yet"
    if similar:
        chgs = [s["day_change_pct"] for s in similar if s["day_change_pct"] is not None]
        if chgs:
            avg = sum(chgs) / len(chgs)
            signals["history"] = vote(avg if abs(avg) >= 0.15 else 0)
            hist_note = f"{len(similar)} similar days, avg move {avg:+.2f}%"
        else:
            signals["history"] = 0
    else:
        signals["history"] = 0

    bulls = sum(1 for v in signals.values() if v == 1)
    bears = sum(1 for v in signals.values() if v == -1)
    direction = "BULL" if bulls > bears else ("BEAR" if bears > bulls else "FLAT")
    agree, oppose = max(bulls, bears), min(bulls, bears)

    confidence = 50 + agree * 9 - oppose * 6
    if pcr is not None:  # option-chain confirmation nudges confidence
        if (direction == "BULL" and pcr > 1.1) or (direction == "BEAR" and pcr < 0.9):
            confidence += 4
    confidence = max(5, min(95, confidence))

    if agree >= 4 and confidence >= 75 and direction != "FLAT":
        decision = "BUY CALL" if direction == "BULL" else "BUY PUT"
    else:
        decision = "NO TRADE"

    if ivix and ivix > 20 or (vix and vix > 28):
        regime = "High Volatility"
    elif direction == "BULL" and ts >= 2:
        regime = "Trending Bullish"
    elif direction == "BEAR" and ts <= -2:
        regime = "Trending Bearish"
    elif ivix and ivix < 13:
        regime = "Low Volatility"
    else:
        regime = "Range-bound"

    risk = "High" if regime == "High Volatility" else ("Low" if confidence >= 85 else "Medium")
    return decision, confidence, signals, regime, risk, hist_note


def claude_view(payload):
    """Optional second opinion from Claude (needs ANTHROPIC_API_KEY secret)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1500,
            thinking={"type": "adaptive"},
            system=("You are an institutional options trader specializing in the "
                    "Indian stock market. Your objective is NOT to predict the "
                    "market; it is to maximize probability while minimizing risk. "
                    "Given the pre-market data JSON, reply in under 150 words: "
                    "your read of the regime, whether you agree with the "
                    "rule-engine's call, and the single biggest risk today. "
                    "If confidence is below 75%, prefer NO TRADE."),
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        )
        if resp.stop_reason == "refusal":
            return None
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        print(f"  claude analysis skipped: {e}")
        return None


# ------------------------------------------------------------------ main ---
def main():
    today = date.today().isoformat()
    print(f"=== Pre-market analysis {today} {datetime.now():%H:%M} ===")

    g = fetch_global()
    ind = fetch_india()
    sentiment, headlines = fetch_news()
    pcr, max_pain = fetch_option_chain()
    calls, puts = stock_candidates()

    # gap estimate proxy: weighted overnight cues (SGX/GIFT not on free feeds)
    cues = [(g.get("sp500_chg"), 0.5), (g.get("asia_chg"), 0.35),
            (sentiment / 5 if sentiment else None, 0.15)]
    known = [(v, w) for v, w in cues if v is not None]
    gap_pct = round(sum(v * w for v, w in known) / sum(w for _, w in known), 2) if known else None

    conn = db_init()
    feats = {"sp500_chg": g["sp500_chg"], "nasdaq_chg": g["nasdaq_chg"],
             "dow_chg": g["dow_chg"], "vix": g["vix"], "asia_chg": g["asia_chg"],
             "india_vix": ind.get("india_vix"), "gap_pct": gap_pct,
             "sentiment": sentiment, "pcr": pcr,
             "trend_score": ind.get("trend_score")}
    similar = find_similar(conn, feats, exclude_date=today)

    decision, confidence, signals, regime, risk, hist_note = decide(
        g, ind, sentiment, pcr, similar)

    spot = ind.get("nifty_prev_close")
    atr = ind.get("atr") or 100
    trade_lines = []
    if decision != "NO TRADE" and spot:
        est_open = spot * (1 + (gap_pct or 0) / 100)
        strike = round(est_open / 50) * 50
        sign = 1 if decision == "BUY CALL" else -1
        sl = round(est_open - sign * 0.5 * atr)
        t1 = round(est_open + sign * 0.75 * atr)
        t2 = round(est_open + sign * 1.25 * atr)
        rr = round(1.25 * atr / (0.5 * atr), 1)
        trade_lines = [
            f"Strike (ATM): {strike} {'CE' if sign == 1 else 'PE'}",
            f"Spot ref: {est_open:,.0f} | SL: {sl:,} | T1: {t1:,} | T2: {t2:,}",
            f"Risk:Reward 1:{rr}",
        ]

    n_hist, wr = win_rate(conn)

    # ---- report
    fmt = lambda v, suf="": "n/a" if v is None else f"{v:+.2f}{suf}" if isinstance(v, float) else str(v)
    lines = [
        f"PRE-MARKET AI REPORT | {today}",
        "",
        f"GLOBAL: S&P {fmt(g['sp500_chg'],'%')} | Nasdaq {fmt(g['nasdaq_chg'],'%')} | "
        f"Dow {fmt(g['dow_chg'],'%')} | VIX {g['vix'] or 'n/a'}",
        f"Asia {fmt(g['asia_chg'],'%')} | Crude {fmt(g['crude_chg'],'%')} | "
        f"Gold {fmt(g['gold_chg'],'%')} | DXY {fmt(g['dxy_chg'],'%')}",
        f"INDIA: Nifty prev {spot:,.0f} | IndiaVIX {ind.get('india_vix') or 'n/a'} | "
        f"Est.gap {fmt(gap_pct,'%')}" if spot else "INDIA: data unavailable",
        f"Trend: EMA20 {ind.get('ema20')} / EMA50 {ind.get('ema50')} / "
        f"EMA200 {ind.get('ema200')} (score {ind.get('trend_score')})",
        f"S/R: {ind.get('support')} / {ind.get('resistance')} | ATR {atr}",
        f"Options: PCR {pcr or 'n/a'} | MaxPain {max_pain or 'n/a'}",
        f"News sentiment: {sentiment:+.1f}/5",
        f"History: {hist_note} | All-time call accuracy: {wr:.0f}% ({n_hist} days)",
        "",
        f"REGIME: {regime}",
        f"SIGNALS: " + ", ".join(f"{k}:{'+' if v==1 else '-' if v==-1 else '0'}"
                                 for k, v in signals.items()),
        "",
        f">>> {decision} | Confidence {confidence}% | Risk: {risk}",
        *trade_lines,
        "",
        "CALL candidates: " + ", ".join(f"{s}({r:+.1f}%)" for s, r in calls),
        "PUT candidates: " + ", ".join(f"{s}({r:+.1f}%)" for s, r in puts),
    ]
    report = "\n".join(lines)
    print(report)

    ai = claude_view({"global": g, "india": ind, "sentiment": sentiment,
                      "pcr": pcr, "max_pain": max_pain, "similar_days": similar,
                      "rule_decision": decision, "confidence": confidence,
                      "regime": regime})
    if ai:
        report += f"\n\nAI VIEW:\n{ai}"
        print(f"\nAI VIEW:\n{ai}")

    telegram(report)

    save_morning(conn, {
        "date": today, **feats,
        "europe_chg": g["ftse_chg"], "crude_chg": g["crude_chg"],
        "gold_chg": g["gold_chg"], "dxy_chg": g["dxy_chg"], "us10y": g["us10y"],
        "nifty_prev_close": spot,
        "banknifty_prev_close": ind.get("banknifty_prev_close"),
        "max_pain": max_pain, "atr": ind.get("atr"),
        "support": ind.get("support"), "resistance": ind.get("resistance"),
        "regime": regime, "news_summary": " | ".join(headlines),
        "decision": decision, "confidence": confidence,
        "reason": json.dumps(signals),
    })
    conn.close()
    print("\nSnapshot stored in market_memory.db")


if __name__ == "__main__":
    main()
