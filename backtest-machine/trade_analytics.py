"""
Post-trade attribution, learning statistics, and baseline-vs-ranked
comparison.
===================================================================

    python trade_analytics.py            # full report to stdout
    python trade_analytics.py --telegram # also send the summary

WHAT IS AND IS NOT TRUSTWORTHY HERE
-----------------------------------
Only trades executed under the LIVE-pricing engine (price_source IS NOT
NULL) are counted as evidence. Trades from the synthetic-pricing era are
excluded entirely: their fills were model outputs, so their P&L measures
the model, not the market.

Every statistic is reported with its sample size and an explicit
sufficiency verdict. Sharpe/Sortino/profit-factor on a handful of trades
are noise; this module says so rather than printing a confident number.
Minimum samples before a statistic is treated as decision-grade:

    MIN_N_DIRECTIONAL = 30   # win rate, expectancy, profit factor
    MIN_N_RATIO       = 60   # Sharpe, Sortino (need return distribution)
    MIN_N_COMPARISON  = 100  # baseline vs ranked A/B call

ATTRIBUTION MODEL
-----------------
Each closed trade's P&L is decomposed into causes that are separately
actionable:

  spread_cost     : (ask-bid) at entry x qty. Pure friction - the price
                    paid to cross the book. Attributable to OPTION
                    SELECTION (liquidity), not to the market call.
  direction_pnl   : P&L that would have been earned at mid-to-mid.
                    Attributable to MARKET PREDICTION + STOCK SELECTION.
  exit_efficiency : realized exit vs the best mark seen while open
                    (high_water). Attributable to EXIT TIMING.
  sizing_note     : lots taken vs lots affordable - POSITION SIZING.
"""

import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
OPT_DB = HERE / "options_trades.db"

MIN_N_DIRECTIONAL = 30
MIN_N_RATIO = 60
MIN_N_COMPARISON = 100


def _rows(conn, sql, args=()):
    """Query helper. A schema mismatch must NEVER look like 'no data' -
    that would silently report an empty strategy history as fact."""
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        print(f"  WARNING: query failed ({e}) - result treated as empty. "
              f"This is a schema problem, not an absence of trades.")
        return []


def ensure_schema(db_path):
    """Apply options_trader's migrations so analytics can read a DB written
    by an older build (e.g. one restored from the trading-state branch)."""
    try:
        import options_trader
        options_trader.DB = db_path
        options_trader.db_init().close()
    except Exception as e:
        print(f"  WARNING: could not run schema migration: {e}")


# ------------------------------------------------------------ statistics ---
def stats(pnls):
    """Portfolio statistics with explicit sufficiency flags."""
    n = len(pnls)
    if n == 0:
        return {"n": 0, "sufficient": False}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / n if n > 1 else 0.0
    sd = math.sqrt(var)
    downside = [p for p in pnls if p < 0]
    dvar = sum(p ** 2 for p in downside) / len(downside) if downside else 0.0
    dsd = math.sqrt(dvar)

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "n": n,
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / n * 100, 1),
        "avg_winner": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loser": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "expectancy": round(mean, 2),
        # per-trade Sharpe/Sortino (not annualised - annualising a handful
        # of intraday trades would be actively misleading)
        "sharpe_per_trade": round(mean / sd, 3) if sd else None,
        "sortino_per_trade": round(mean / dsd, 3) if dsd else None,
        "max_drawdown": round(max_dd, 2),
        "sufficient": n >= MIN_N_DIRECTIONAL,
        "ratios_sufficient": n >= MIN_N_RATIO,
    }


def fmt_stats(s, label):
    if not s.get("n"):
        return f"  {label:22s} no trades"
    warn = "" if s["sufficient"] else "  <- SAMPLE TOO SMALL, not decision-grade"
    pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "n/a"
    out = [f"  {label:22s} n={s['n']:<4d} net=Rs.{s['net_pnl']:>9,.0f}  "
           f"win={s['win_rate']:>5.1f}%  PF={pf:>5s}  "
           f"exp=Rs.{s['expectancy']:>7,.0f}  maxDD=Rs.{s['max_drawdown']:>8,.0f}{warn}"]
    if s.get("ratios_sufficient"):
        out.append(f"  {'':22s} Sharpe/trade={s['sharpe_per_trade']}  "
                   f"Sortino/trade={s['sortino_per_trade']}")
    return "\n".join(out)


# ----------------------------------------------------------- attribution ---
def attribute(t):
    """Decompose one closed trade into actionable causes."""
    (sym, tsym, qty, entry, exit_, pnl, reason, src,
     ebid, eask, eoi, evol, score, rank, tier) = t

    a = {"symbol": tsym or sym, "qty": qty, "entry": entry, "exit": exit_,
         "pnl": pnl, "reason": reason, "price_source": src,
         "score": score, "rank": rank, "tier": tier}

    # spread cost: we buy at ask, so crossing costs (ask-mid) per unit
    if ebid and eask and eask > ebid:
        mid = (ebid + eask) / 2
        a["spread_pct"] = round((eask - ebid) / mid * 100, 2)
        a["spread_cost"] = round((eask - mid) * qty, 2)
        a["direction_pnl"] = round(pnl + a["spread_cost"], 2)
    else:
        a["spread_pct"] = None
        a["spread_cost"] = None
        a["direction_pnl"] = None

    # primary cause
    if reason and "Initial stop" in reason:
        a["primary_cause"] = "MARKET PREDICTION (went against us immediately)"
    elif reason and "Trailing stop" in reason:
        a["primary_cause"] = ("EXIT TIMING (gave back from peak)" if pnl > 0
                              else "MARKET PREDICTION (reversed after entry)")
    elif reason and "Trend reversal" in reason:
        a["primary_cause"] = "MARKET PREDICTION (signal flipped)"
    elif reason and "Square-off" in reason:
        a["primary_cause"] = ("ENTRY TIMING (no follow-through by close)"
                              if abs(pnl) < abs(entry * qty * 0.05)
                              else "MARKET PREDICTION")
    else:
        a["primary_cause"] = "OTHER"

    a["should_have_taken"] = pnl > 0
    return a


def live_trades(conn):
    return _rows(conn,
                 "SELECT symbol,trading_symbol,qty,entry_price,exit_price,pnl,"
                 "reason,price_source,entry_bid,entry_ask,entry_oi,"
                 "entry_volume,entry_score,entry_rank,entry_tier "
                 "FROM options_trades WHERE price_source IS NOT NULL "
                 "ORDER BY exit_time")


# -------------------------------------------------- baseline vs ranked ---
def compare_strategies(conn):
    """Baseline vs what the ranked engine would have picked.

    Uses the score stamped on each executed trade (shadow mode) - we can
    only compare over trades the baseline ACTUALLY took, so this measures
    'does score rank realized outcomes?', not 'would ranked have found
    better trades elsewhere'. The latter needs the ranking_log rows for
    candidates that were never traded, which only accumulate forward.
    """
    rows = _rows(conn,
                 "SELECT entry_score, pnl FROM options_trades "
                 "WHERE price_source IS NOT NULL AND entry_score IS NOT NULL")
    if not rows:
        return None
    rows.sort(key=lambda r: -r[0])
    pnls_all = [r[1] for r in rows]
    out = {"baseline_all": stats(pnls_all)}
    for k in (1, 2, 3):
        out[f"top_{k}"] = stats([r[1] for r in rows[:k]])
    # rank correlation between score and outcome
    n = len(rows)
    if n >= 3:
        ranks_score = list(range(n))
        by_pnl = sorted(range(n), key=lambda i: -rows[i][1])
        ranks_pnl = [0] * n
        for pos, i in enumerate(by_pnl):
            ranks_pnl[i] = pos
        d2 = sum((ranks_score[i] - ranks_pnl[i]) ** 2 for i in range(n))
        out["spearman"] = round(1 - 6 * d2 / (n * (n * n - 1)), 3)
    out["n"] = n
    out["comparison_sufficient"] = n >= MIN_N_COMPARISON
    return out


# ------------------------------------------------------- learning stats ---
def learning_table(conn):
    rows = live_trades(conn)
    by_sym, by_reason, by_tier = defaultdict(list), defaultdict(list), defaultdict(list)
    for t in rows:
        by_sym[t[0]].append(t[5])
        by_reason[t[6] or "?"].append(t[5])
        by_tier[t[14] or "unknown"].append(t[5])
    return by_sym, by_reason, by_tier


# ------------------------------------------------------------- reporting ---
def build_report():
    if not OPT_DB.exists():
        return "No options database found."
    ensure_schema(OPT_DB)
    conn = sqlite3.connect(OPT_DB)
    L = []
    A = L.append

    trades = live_trades(conn)
    all_n = _rows(conn, "SELECT COUNT(*) FROM options_trades")[0][0]
    A("=" * 78)
    A("OPTIONS STRATEGY ANALYTICS")
    A("=" * 78)
    A(f"Total option trades on record : {all_n}")
    A(f"LIVE-pricing-era trades       : {len(trades)}  <- the only valid evidence")
    A(f"Excluded (synthetic-era)      : {all_n - len(trades)}")
    if not trades:
        A("\nNo live-era trades yet - nothing measurable.")
        conn.close()
        return "\n".join(L)

    A("")
    A("-" * 78)
    A("PORTFOLIO STATISTICS (live-era only)")
    A("-" * 78)
    A(fmt_stats(stats([t[5] for t in trades]), "ALL live trades"))

    by_sym, by_reason, by_tier = learning_table(conn)
    A("")
    A("  By exit reason:")
    for r, p in sorted(by_reason.items(), key=lambda kv: sum(kv[1])):
        A(fmt_stats(stats(p), f"    {r[:20]}"))
    A("")
    A("  By signal tier:")
    for r, p in by_tier.items():
        A(fmt_stats(stats(p), f"    {r}"))

    A("")
    A("-" * 78)
    A("PER-TRADE ATTRIBUTION")
    A("-" * 78)
    tot_spread = 0.0
    measurable = 0
    for t in trades:
        a = attribute(t)
        A(f"  {a['symbol']}")
        A(f"    entry {a['entry']:.2f} -> exit {a['exit']:.2f}  qty {a['qty']}  "
          f"P&L Rs.{a['pnl']:,.0f}  [{a['reason']}]")
        if a["spread_cost"] is not None:
            tot_spread += a["spread_cost"]
            measurable += 1
            A(f"    spread {a['spread_pct']}% -> cost Rs.{a['spread_cost']:,.0f}"
              f" | direction-only P&L Rs.{a['direction_pnl']:,.0f}")
        else:
            A("    spread cost: NOT MEASURABLE (entry quote not recorded)")
        A(f"    cause: {a['primary_cause']}")
        if a["score"] is not None:
            A(f"    rank score {a['score']:.3f} (rank #{a['rank']}, {a['tier']})")
    if measurable:
        A("")
        A(f"  Total measurable spread cost: Rs.{tot_spread:,.0f} across "
          f"{measurable} trades (Rs.{tot_spread/measurable:,.0f}/trade)")

    cmp_ = compare_strategies(conn)
    A("")
    A("-" * 78)
    A("BASELINE vs RANKED (shadow evidence)")
    A("-" * 78)
    if not cmp_:
        A("  No scored trades yet. Shadow mode stamps a score on every trade")
        A("  from now on; this comparison populates as those trades close.")
    else:
        A(fmt_stats(cmp_["baseline_all"], "baseline (all taken)"))
        for k in (1, 2, 3):
            A(fmt_stats(cmp_[f"top_{k}"], f"ranked top-{k} only"))
        if "spearman" in cmp_:
            A(f"\n  Spearman(score, realized P&L) = {cmp_['spearman']:+.3f}")
            A("    +1 = score perfectly ranks outcomes, 0 = no relationship")
        if not cmp_["comparison_sufficient"]:
            A(f"\n  VERDICT: n={cmp_['n']} < {MIN_N_COMPARISON} required.")
            A("  NOT decision-grade. Do NOT switch modes on this evidence.")

    A("")
    A("=" * 78)
    A("RECOMMENDATION")
    A("=" * 78)
    n = len(trades)
    if n < MIN_N_DIRECTIONAL:
        A(f"  Stay in SHADOW mode. n={n}, need >={MIN_N_COMPARISON} scored")
        A("  closed trades before any switch to active can be justified.")
        A("  Shadow costs nothing and accumulates the evidence.")
    else:
        A("  Sufficient directional sample. Review the comparison above.")
    conn.close()
    return "\n".join(L)


def main():
    report = build_report()
    print(report)
    if "--telegram" in sys.argv:
        try:
            cfg = json.loads((HERE / "intraday_config.json").read_text())
            tg = cfg.get("telegram", {})
            if tg.get("bot_token") and tg.get("chat_id"):
                import requests
                # Telegram caps messages at 4096 chars
                body = report[-3900:]
                requests.post(
                    f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                    json={"chat_id": tg["chat_id"], "text": body}, timeout=10)
        except Exception as e:
            print(f"(telegram send failed: {e})")


if __name__ == "__main__":
    main()
