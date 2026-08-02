"""
Signal-quality research: which entry features actually predict intraday
follow-through on the UNDERLYING?
=======================================================================
Grounds the ranking engine's weights in measurable evidence instead of
intuition. For every (day, symbol) over the lookback window it computes,
using ONLY bars up to 09:30 of that day (no look-ahead):

  direction   : EMA9/21 + session-VWAP confluence (the bot's Tier-1 signal)
  rel_strength: symbol's day-open->09:30 return minus NIFTY's same return
  aligned     : does the symbol's direction agree with NIFTY's direction?
  trend_qual  : fraction of the last 12 bars where EMA9/21 agreed with the
                final direction (how established the trend is, 0..1)

Outcome measured: the underlying's forward return 09:30 -> 15:15 the same
day, SIGNED by direction (so "signal return" > 0 means the signal was
right). Options are levered instruments on this move, so underlying
follow-through is the honest, data-rich proxy - option-level history
does not exist for these dates (contracts expire and drop off feeds).

    python research_signal_quality.py            # ~60d x 20 symbols

Output: bucket tables of hit-rate and mean signal return per feature,
printed and saved to signal_research_results.json for the record.
"""

import json
import sys
from datetime import time as dtime

import pandas as pd
import yfinance as yf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CFG = json.load(open("intraday_config.json"))
UNIVERSE = [("NIFTY", "^NSEI"), ("BANKNIFTY", "^NSEBANK")] + \
    [(s.replace(".NS", ""), s) for s in CFG.get("symbols", [])]

CUT = dtime(9, 30)
EXIT_T = dtime(15, 15)


def direction_at(bars):
    """The bot's confluence signal, computed on bars up to the cutoff."""
    if len(bars) < 21:
        return None, 0.0
    ema9 = bars["Close"].ewm(span=9).mean()
    ema21 = bars["Close"].ewm(span=21).mean()
    vol = bars["Volume"].sum()
    if vol:
        vwap = (bars["Close"] * bars["Volume"]).sum() / vol
    else:
        # indices report zero volume on yfinance - volume-less proxy
        vwap = bars["Close"].mean()
    last = bars["Close"].iloc[-1]
    d = None
    if ema9.iloc[-1] > ema21.iloc[-1] and last > vwap:
        d = "BULL"
    elif ema9.iloc[-1] < ema21.iloc[-1] and last < vwap:
        d = "BEAR"
    if d is None:
        return None, 0.0
    tail = (ema9 > ema21).tail(12)
    tq = float(tail.mean()) if d == "BULL" else float((~tail).mean())
    return d, tq


def main():
    frames = {}
    for name, ticker in UNIVERSE:
        try:
            df = yf.download(ticker, period="60d", interval="5m",
                             auto_adjust=True, progress=False,
                             multi_level_index=False)
            if df is not None and not df.empty:
                frames[name] = df.dropna(subset=["Close"])
                print(f"  {name}: {len(df)} bars")
        except Exception as e:
            print(f"  {name}: FAILED {e}")
    if "NIFTY" not in frames:
        print("No NIFTY data; abort")
        return

    rows = []
    nifty = frames["NIFTY"]
    days = sorted({ts.date() for ts in nifty.index})
    for day in days:
        nd = nifty[nifty.index.date == day]
        if nd.empty:
            continue
        n_pre = nifty[(nifty.index.date < day) |
                      ((nifty.index.date == day) & (nifty.index.time <= CUT))]
        n_dir, _ = direction_at(n_pre.tail(400))
        n_open = float(nd["Open"].iloc[0])
        n_930 = n_pre[n_pre.index.date == day]
        n_930_px = float(n_930["Close"].iloc[-1]) if not n_930.empty else n_open
        n_r930 = (n_930_px / n_open - 1) * 100

        for name, df in frames.items():
            if name == "NIFTY":
                continue
            dd = df[df.index.date == day]
            if dd.empty:
                continue
            pre = df[(df.index.date < day) |
                     ((df.index.date == day) & (df.index.time <= CUT))]
            d, tq = direction_at(pre.tail(400))
            if d is None:
                continue
            s_open = float(dd["Open"].iloc[0])
            s_930b = pre[pre.index.date == day]
            if s_930b.empty:
                continue
            s_930 = float(s_930b["Close"].iloc[-1])
            later = dd[dd.index.time >= EXIT_T]
            exit_px = float(later["Close"].iloc[0]) if not later.empty \
                else float(dd["Close"].iloc[-1])
            fwd = (exit_px / s_930 - 1) * 100
            sig_ret = fwd if d == "BULL" else -fwd
            rel = (s_930 / s_open - 1) * 100 - n_r930
            rows.append({
                "day": str(day), "symbol": name, "direction": d,
                "signal_return": sig_ret,
                "rel_strength": rel,
                "rel_aligned": (rel > 0) == (d == "BULL"),
                "mkt_aligned": (n_dir == d) if n_dir else None,
                "trend_qual": tq,
            })

    r = pd.DataFrame(rows)
    print(f"\nSamples: {len(r)} signal-days across {r['symbol'].nunique()} symbols, "
          f"{r['day'].nunique()} days")

    def bucket(df, label):
        if df.empty:
            return {"label": label, "n": 0}
        return {"label": label, "n": len(df),
                "hit_rate": round(float((df.signal_return > 0).mean()) * 100, 1),
                "mean_ret": round(float(df.signal_return.mean()), 3),
                "median_ret": round(float(df.signal_return.median()), 3)}

    out = []
    out.append(bucket(r, "ALL confluence signals"))
    out.append(bucket(r[r.mkt_aligned == True], "market-ALIGNED (dir == NIFTY dir)"))
    out.append(bucket(r[r.mkt_aligned == False], "market-OPPOSED (dir != NIFTY dir)"))
    out.append(bucket(r[r.rel_aligned], "rel-strength CONFIRMS direction"))
    out.append(bucket(r[~r.rel_aligned], "rel-strength CONTRADICTS direction"))
    out.append(bucket(r[r.trend_qual >= 0.99], "trend fully established (tq=1.0)"))
    out.append(bucket(r[r.trend_qual < 0.99], "trend fresh/unstable (tq<1.0)"))
    both = r[(r.mkt_aligned == True) & (r.rel_aligned)]
    out.append(bucket(both, "ALIGNED + rel-strength CONFIRMS (stacked)"))
    neither = r[(r.mkt_aligned == False) & (~r.rel_aligned)]
    out.append(bucket(neither, "OPPOSED + rel-strength CONTRADICTS (stacked)"))

    print(f"\n{'bucket':45s} {'n':>5s} {'hit%':>6s} {'mean%':>7s} {'med%':>7s}")
    for b in out:
        if b["n"]:
            print(f"{b['label']:45s} {b['n']:>5d} {b['hit_rate']:>6.1f} "
                  f"{b['mean_ret']:>7.3f} {b['median_ret']:>7.3f}")
        else:
            print(f"{b['label']:45s} {b['n']:>5d}")

    json.dump({"samples": len(r), "buckets": out},
              open("signal_research_results.json", "w"), indent=2)
    print("\nSaved signal_research_results.json")


if __name__ == "__main__":
    main()
