"""
Candidate ranking & dynamic trade selection engine.
===================================================
Replaces first-come-in-list-order candidate selection with a weighted,
explainable score. Runs in one of three modes (config: ranking.mode):

  "off"    - engine not consulted at all.
  "shadow" - DEFAULT. Scores are computed and logged every cycle, and
             stamped onto every executed trade, but the BASELINE strategy
             keeps making all decisions. This accumulates true forward
             out-of-sample evidence (score vs realized P&L) without
             changing behaviour.
  "active" - entries are chosen by rank, with liquidity gates, sector
             caps and a dynamic trade count by confidence tier.

Every score comes with a full per-feature breakdown so any trade can be
attributed after the fact ("why was this selected over that").

FEATURE EVIDENCE (research_signal_quality.py, 57 trading days x 19
symbols, 785 signal-days, forward return 09:30->15:15 signed by signal):

  ALL confluence signals            n=785  hit 47.3%  mean -0.068%
  rel-strength CONFIRMS             n=562  hit 48.8%  mean -0.042%
  rel-strength CONTRADICTS          n=223  hit 43.5%  mean -0.134%
  trend fresh (tq<1.0)              n=411  hit 49.1%  mean -0.010%
  trend established (tq=1.0)        n=374  hit 45.2%  mean -0.133%
  market-ALIGNED with NIFTY         n=492  hit 44.1%  mean -0.150%
  market-OPPOSED                    n=152  hit 48.0%  mean +0.059%

Consequences, encoded in the default weights:
  * rel_strength is the best-supported separator -> highest weight.
  * trend_quality favors FRESH trends (the measured direction), small
    weight - provisional, single-window evidence.
  * market_alignment gets WEIGHT 0 by default: the naive "trade with the
    index" assumption was contradicted in this (mean-reverting) window,
    and flipping it contrarian on one window would be overfitting. The
    feature is still computed and logged so shadow data keeps measuring
    it; revisit when several regimes of evidence exist.
  * The base signal itself has ~no intraday edge (47.3%), so the
    engine's largest immediate value is NOT picking winners - it is
    (a) mechanically cutting cost via liquidity gates, (b) cutting the
    NUMBER of edgeless trades via conservative tiers, and (c) stamping
    scores on every trade to build the evidence base safely.

Historical-performance feature is GATED: a symbol's own realized stats
only contribute once it has >= min_history_n closed trades under the
live-pricing engine; before that its contribution is neutral (0.5).
This prevents overfitting to a handful of early trades. Only LIVE-era
trades count (price_source set); synthetic-era records are excluded.

S-26 CORRECTIONS (shadow scoring only - see feature_scores)
-----------------------------------------------------------
  * risk_reward was a hardcoded constant, never computed. Weight 0.05
    -> 0.00.
  * history scored win rate while discarding realized expectancy; it is
    now capped at neutral for symbols with non-positive expectancy.

Both change SHADOW SCORES. Neither changes a trading decision: mode is
"shadow", so no score has ever selected a trade. Rank ORDER is also
unchanged by the weight change - dropping a constant-valued feature is a
monotone transform of the score - but the absolute scale shifts, because
total weight falls from 1.00 to 0.95 and the constant +0.025 offset every
candidate carried is gone.

  CONSEQUENCE, RECORDED NOT RESOLVED: the `tiers` thresholds (0.75 /
  0.62 / 0.52) were set against the OLD scale. They are only consulted in
  "active" mode, which is not enabled, so nothing is broken today. They
  MUST be recalibrated against observed shadow scores before mode is ever
  set to "active". Recalibrating them now would be guessing at numbers
  the observation window has not yet produced.
"""

import json
import sqlite3
from datetime import datetime

# ------------------------------------------------------------- sectors ---
SECTOR = {
    "NIFTY": "Index", "BANKNIFTY": "Index",
    "HDFCBANK": "Banking", "ICICIBANK": "Banking", "SBIN": "Banking",
    "AXISBANK": "Banking", "BAJAJFINSV": "Financials",
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT",
    "MARUTI": "Auto",
    "RELIANCE": "Energy", "ONGC": "Energy",
    "NTPC": "Power", "POWERGRID": "Power",
    "SUNPHARMA": "Pharma",
    "JSWSTEEL": "Metals", "LT": "Infra",
    "ASIANPAINT": "Consumer", "BHARTIARTL": "Telecom",
}

DEFAULTS = {
    "mode": "shadow",
    "weights": {
        # evidence-annotated; see module docstring for the measurements
        "rel_strength": 0.30,       # best-supported separator (785 samples)
        "option_liquidity": 0.25,   # mechanical cost - arithmetic, not statistics
        "trend_quality": 0.10,      # favors FRESH trends (measured); provisional
        "momentum": 0.10,           # untested by study - shadow-validating
        "signal_tier": 0.10,        # confluence > momentum fallback (structural)
        "history": 0.10,            # gated on min_history_n live-era trades
        "risk_reward": 0.00,        # S-26: constant 0.5, never measured - see below
        "market_alignment": 0.00,   # contradicted in current window; logged only
    },
    # dynamic trade count by top-candidate confidence (score 0..1).
    # Conservative on purpose: the base signal is edgeless, so every
    # marginal trade costs ~frictions; fewer, better-ranked trades.
    "tiers": [
        {"min_score": 0.75, "max_trades": 3},
        {"min_score": 0.62, "max_trades": 2},
        {"min_score": 0.52, "max_trades": 1},
    ],  # below the last tier: no trades
    "max_per_sector": 2,
    "liquidity": {
        "max_spread_pct": 5.0,   # (ask-bid)/mid, entry-killing beyond this
        "min_oi": 500,
        "min_volume": 100,
    },
    "min_history_n": 10,
}


def get_config(cfg):
    """Merge the config file's `ranking` block over defaults."""
    user = (cfg or {}).get("ranking", {})
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


# ------------------------------------------------------------- features ---
def clamp01(x):
    return max(0.0, min(1.0, x))


def feature_scores(cand, nifty_dir, history):
    """Each feature mapped to 0..1. Returns (scores dict, notes dict)."""
    s, notes = {}, {}

    d = cand["direction"]
    if nifty_dir is None:
        s["market_alignment"] = 0.5
        notes["market_alignment"] = "NIFTY neutral"
    elif d == nifty_dir:
        s["market_alignment"] = 1.0
        notes["market_alignment"] = f"aligned with NIFTY {nifty_dir}"
    else:
        s["market_alignment"] = 0.0
        notes["market_alignment"] = f"OPPOSES NIFTY {nifty_dir}"

    rel = cand.get("rel_strength", 0.0)
    signed = rel if d == "BULL" else -rel
    s["rel_strength"] = clamp01(0.5 + signed / 2.0)   # +-1% rel move = +-0.5
    notes["rel_strength"] = f"{rel:+.2f}% vs NIFTY ({'confirms' if signed > 0 else 'contradicts'})"

    tq = cand.get("trend_quality", 0.5)
    # Evidence-directed: FRESH trends (tq<1) outperformed fully-established
    # ones intraday in the 57d study, so freshness scores higher.
    # tq=1.0 -> 0.30, tq=0.5 -> 0.65, tq=0.0 -> 1.00 (gentle slope).
    s["trend_quality"] = clamp01(1.0 - 0.7 * tq)
    notes["trend_quality"] = (f"EMA alignment {tq:.0%} of last 12 bars "
                              f"(fresh scores higher - see evidence)")

    mom = abs(cand.get("momentum", 0.0))
    s["momentum"] = clamp01(mom / 2.0)                # 2% day move = 1.0
    notes["momentum"] = f"{cand.get('momentum', 0.0):+.2f}% today"

    s["signal_tier"] = 1.0 if cand.get("tier") == "confluence" else 0.3
    notes["signal_tier"] = cand.get("tier", "?")

    q = cand.get("quote")
    if q and q.get("ltp"):
        mid = (q["bid"] + q["ask"]) / 2 if q["bid"] and q["ask"] else q["ltp"]
        spread_pct = ((q["ask"] - q["bid"]) / mid * 100) if (q["bid"] and q["ask"] and mid) else 10.0
        spread_s = clamp01(1 - spread_pct / 6.0)      # 0% spread=1, 6%+=0
        oi_s = clamp01(q.get("oi", 0) / 20000)
        vol_s = clamp01(q.get("volume", 0) / 20000)
        s["option_liquidity"] = 0.5 * spread_s + 0.3 * oi_s + 0.2 * vol_s
        notes["option_liquidity"] = (f"spread {spread_pct:.1f}%, "
                                     f"OI {q.get('oi', 0):,}, vol {q.get('volume', 0):,}")
        cand["spread_pct"] = round(spread_pct, 2)
    else:
        s["option_liquidity"] = 0.0
        notes["option_liquidity"] = "no live quote"

    # S-26: risk_reward was DOCUMENTED as "cheaper premium relative to the
    # underlying's day range" but was never computed - it returned the
    # constant 0.5 for every candidate on every cycle. A constant cannot
    # separate candidates; all it did was carry 5% of the weight budget and
    # dilute the three features that ARE measured. Weight is now 0.00, the
    # same treatment market_alignment already gets: still computed, still
    # logged, contributing nothing until something real is behind it.
    # Kept rather than deleted so the breakdown schema is stable.
    s["risk_reward"] = 0.5
    notes["risk_reward"] = "NOT IMPLEMENTED - constant, weight 0"

    # S-26: this scored win RATE while load_history also computed average
    # P&L and threw it away. The two are decoupled in this book's realized
    # data - trailing-stop exits are 21 wins for +Rs.35,079 while initial
    # stops are 0 wins for -Rs.56,037 - so a symbol can carry a flattering
    # win rate and still have lost money. Feeding win rate alone into a
    # feature named "history" asserts an equivalence the ledger contradicts.
    #
    # Win rate still drives the score (no P&L->score scale is invented
    # here; there is no evidence for one). It is now CAPPED AT NEUTRAL when
    # realized expectancy is non-positive: a symbol that has lost money on
    # average may not count as evidence in its own favour. Expectancy is
    # surfaced in the note either way, so the observation window measures
    # which of the two actually predicts.
    h = history.get(cand["name"]) if history else None
    if h and h["n"] >= h.get("min_n", 10):
        wr = h["win_rate"]
        exp_pnl = h.get("avg_pnl", 0.0)
        raw = clamp01(wr)
        s["history"] = min(raw, 0.5) if exp_pnl <= 0 else raw
        capped = " [capped: negative expectancy]" if exp_pnl <= 0 else ""
        notes["history"] = (f"{h['n']} trades, {wr:.0%} win rate, "
                            f"expectancy Rs.{exp_pnl:,.0f}{capped}")
    else:
        s["history"] = 0.5
        n = h["n"] if h else 0
        notes["history"] = f"insufficient history (n={n}) - neutral"

    return s, notes


def score_candidate(cand, nifty_dir, weights, history):
    s, notes = feature_scores(cand, nifty_dir, history)
    total_w = sum(weights.values()) or 1.0
    score = sum(weights[k] * s.get(k, 0.5) for k in weights) / total_w
    breakdown = {k: {"score": round(s.get(k, 0.5), 3),
                     "weight": weights[k],
                     "contrib": round(weights[k] * s.get(k, 0.5) / total_w, 4),
                     "note": notes.get(k, "")} for k in weights}
    return round(score, 4), breakdown


def rank(candidates, nifty_dir, rcfg, history=None):
    """Score and sort all candidates, best first."""
    ranked = []
    for cand in candidates:
        score, breakdown = score_candidate(cand, nifty_dir,
                                           rcfg["weights"], history or {})
        ranked.append({**cand, "score": score, "breakdown": breakdown})
    ranked.sort(key=lambda c: -c["score"])
    for i, c in enumerate(ranked):
        c["rank"] = i + 1
    return ranked


def max_trades_for(top_score, tiers):
    for t in tiers:
        if top_score >= t["min_score"]:
            return t["max_trades"]
    return 0


def select(ranked, rcfg, open_sector_counts=None, slots_available=99):
    """Active-mode selection: liquidity gates -> sector caps -> tier count.
    Returns (picks, rejections) with a reason on every rejection."""
    picks, rejections = [], []
    sector_counts = dict(open_sector_counts or {})
    liq = rcfg["liquidity"]
    budget = max_trades_for(ranked[0]["score"], rcfg["tiers"]) if ranked else 0
    budget = min(budget, slots_available)

    for c in ranked:
        if len(picks) >= budget:
            rejections.append((c, f"tier budget reached ({budget})"))
            continue
        q = c.get("quote") or {}
        if not q.get("ltp"):
            rejections.append((c, "no live quote"))
            continue
        if c.get("spread_pct", 99) > liq["max_spread_pct"]:
            rejections.append((c, f"spread {c.get('spread_pct')}% > {liq['max_spread_pct']}%"))
            continue
        if q.get("oi", 0) < liq["min_oi"]:
            rejections.append((c, f"OI {q.get('oi', 0)} < {liq['min_oi']}"))
            continue
        if q.get("volume", 0) < liq["min_volume"]:
            rejections.append((c, f"volume {q.get('volume', 0)} < {liq['min_volume']}"))
            continue
        sec = SECTOR.get(c["name"], "Other")
        if sector_counts.get(sec, 0) >= rcfg["max_per_sector"]:
            rejections.append((c, f"sector cap {sec} ({rcfg['max_per_sector']})"))
            continue
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
        picks.append(c)
    return picks, rejections


# ------------------------------------------------------- learning stats ---
def load_history(conn, min_n=10):
    """Per-symbol realized stats from LIVE-era closed trades only.
    Pre-live trades (price_source NULL) were executed under synthetic
    pricing and are excluded as evidence."""
    out = {}
    try:
        rows = conn.execute(
            "SELECT symbol, COUNT(*), AVG(pnl > 0), AVG(pnl) "
            "FROM options_trades WHERE price_source IS NOT NULL "
            "GROUP BY symbol").fetchall()
    except sqlite3.Error:
        return out
    for sym, n, wr, avg_pnl in rows:
        out[sym] = {"n": n, "win_rate": wr or 0.0,
                    "avg_pnl": avg_pnl or 0.0, "min_n": min_n}
    return out


# ------------------------------------------------------------- logging ---
def ensure_tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ranking_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, mode TEXT, symbol TEXT, direction TEXT, tier TEXT,
        rank INTEGER, score REAL, would_trade INTEGER,
        breakdown TEXT, quote TEXT);
    """)


def log_cycle(conn, mode, ranked, picks):
    ensure_tables(conn)
    pick_names = {c["name"] for c in picks}
    ts = datetime.now().isoformat(timespec="seconds")
    for c in ranked:
        q = c.get("quote") or {}
        conn.execute(
            "INSERT INTO ranking_log(ts,mode,symbol,direction,tier,rank,score,"
            "would_trade,breakdown,quote) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, mode, c["name"], c["direction"], c.get("tier"),
             c["rank"], c["score"], 1 if c["name"] in pick_names else 0,
             json.dumps(c["breakdown"]),
             json.dumps({k: q.get(k) for k in
                         ("ltp", "bid", "ask", "volume", "oi")})))


# ------------------------------------------------------------- selftest ---
def _selftest():
    cands = [
        {"name": "RELIANCE", "direction": "BULL", "rel_strength": 0.8,
         "trend_quality": 1.0, "momentum": 1.2, "tier": "confluence",
         "quote": {"ltp": 30, "bid": 29.8, "ask": 30.2, "volume": 50000, "oi": 40000}},
        {"name": "HDFCBANK", "direction": "BULL", "rel_strength": -0.6,
         "trend_quality": 0.6, "momentum": 0.2, "tier": "confluence",
         "quote": {"ltp": 20, "bid": 19.5, "ask": 20.5, "volume": 8000, "oi": 12000}},
        {"name": "TCS", "direction": "BEAR", "rel_strength": 0.5,
         "trend_quality": 0.7, "momentum": 0.9, "tier": "momentum",
         "quote": {"ltp": 55, "bid": 52, "ask": 58, "volume": 300, "oi": 300}},
        {"name": "ICICIBANK", "direction": "BULL", "rel_strength": 0.3,
         "trend_quality": 0.9, "momentum": 0.7, "tier": "confluence",
         "quote": {"ltp": 25, "bid": 24.9, "ask": 25.1, "volume": 30000, "oi": 25000}},
    ]
    rcfg = get_config({})
    ranked = rank(cands, "BULL", rcfg)
    assert [c["name"] for c in ranked] == \
        ["RELIANCE", "ICICIBANK", "HDFCBANK", "TCS"], \
        f"unexpected order {[c['name'] for c in ranked]}"
    picks, rej = select(ranked, rcfg, {}, slots_available=4)
    names = [p["name"] for p in picks]
    assert "TCS" not in names, "TCS (illiquid, wide spread) must never be picked"
    assert names[0] == "RELIANCE" and names[1] == "ICICIBANK"
    # sector cap: 3rd Banking name must be rejected even with budget room
    wide = dict(rcfg, tiers=[{"min_score": 0.0, "max_trades": 9}])
    extra = ranked + [dict(ranked[2], name="AXISBANK")]
    p2, r2 = select(rank([dict(c) for c in cands] +
                         [dict(cands[1], name="AXISBANK")], "BULL", wide),
                    wide, {}, slots_available=9)
    banking = [p["name"] for p in p2 if SECTOR.get(p["name"]) == "Banking"]
    assert len(banking) <= 2, f"sector cap violated: {banking}"
    # liquidity gate fires when budget is not the binding constraint
    tcs_rej = [why for c, why in r2 if c["name"] == "TCS"]
    assert tcs_rej and ("spread" in tcs_rej[0] or "OI" in tcs_rej[0]), tcs_rej
    total_contrib = sum(v["contrib"] for v in ranked[0]["breakdown"].values())
    assert abs(total_contrib - ranked[0]["score"]) < 1e-6, "breakdown must sum to score"
    assert max_trades_for(0.80, rcfg["tiers"]) == 3
    assert max_trades_for(0.65, rcfg["tiers"]) == 2
    assert max_trades_for(0.55, rcfg["tiers"]) == 1
    assert max_trades_for(0.40, rcfg["tiers"]) == 0
    print("ranking_engine selftest: ALL PASS")
    print(f"  ranked order: {[c['name'] for c in ranked]}")
    print(f"  scores      : {[c['score'] for c in ranked]}")
    print(f"  picks       : {names}")
    for c, why in rej:
        print(f"  rejected    : {c['name']} - {why}")


if __name__ == "__main__":
    _selftest()
