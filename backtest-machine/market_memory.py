"""
Historical market memory for the pre-market AI analyzer.
=========================================================
Stores one row per trading day (global cues, India data, news sentiment,
option metrics, and the eventual market result), then finds the Top-10
most similar historical days by z-score-normalized distance over the
numeric feature vector.

State lives in market_memory.db (persisted on the trading-state branch by CI).
"""

import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "market_memory.db"

# numeric features used for similarity search
FEATURES = ["sp500_chg", "nasdaq_chg", "dow_chg", "vix", "asia_chg",
            "india_vix", "gap_pct", "sentiment", "pcr", "trend_score"]


def db_init():
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS daily_memory(
        date TEXT PRIMARY KEY,
        -- morning features
        sp500_chg REAL, nasdaq_chg REAL, dow_chg REAL, vix REAL,
        asia_chg REAL, europe_chg REAL, crude_chg REAL, gold_chg REAL,
        dxy_chg REAL, us10y REAL,
        nifty_prev_close REAL, banknifty_prev_close REAL, india_vix REAL,
        gap_pct REAL, sentiment REAL, news_summary TEXT,
        pcr REAL, max_pain REAL, trend_score REAL, atr REAL,
        support REAL, resistance REAL, regime TEXT,
        -- the morning call
        decision TEXT, confidence REAL, reason TEXT,
        -- filled in by the EOD learner
        result TEXT, nifty_open REAL, nifty_close REAL, day_change_pct REAL,
        call_correct INTEGER, notes TEXT);
    """)
    return conn


def save_morning(conn, row: dict):
    cols = ", ".join(row.keys())
    ph = ", ".join("?" * len(row))
    conn.execute(f"INSERT OR REPLACE INTO daily_memory({cols}) VALUES({ph})",
                 list(row.values()))
    conn.commit()


def update_eod(conn, date_str, updates: dict):
    sets = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE daily_memory SET {sets} WHERE date=?",
                 list(updates.values()) + [date_str])
    conn.commit()


def find_similar(conn, today_features: dict, top_n=10, exclude_date=None):
    """Top-N most similar past days by normalized euclidean distance.

    Only uses features present (non-None) both today and in the stored row,
    so a day without option-chain data still matches on the rest.
    """
    rows = conn.execute(
        "SELECT date, {}, decision, confidence, result, day_change_pct, "
        "call_correct FROM daily_memory WHERE result IS NOT NULL".format(
            ", ".join(FEATURES))).fetchall()
    if exclude_date:
        rows = [r for r in rows if r[0] != exclude_date]
    if not rows:
        return []

    # per-feature mean/std across history for z-scoring
    stats = {}
    for i, f in enumerate(FEATURES, start=1):
        vals = [r[i] for r in rows if r[i] is not None]
        if len(vals) >= 2:
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            stats[f] = (mean, max(var ** 0.5, 1e-9))

    scored = []
    for r in rows:
        dist, used = 0.0, 0
        for i, f in enumerate(FEATURES, start=1):
            tv, hv = today_features.get(f), r[i]
            if tv is None or hv is None or f not in stats:
                continue
            mean, std = stats[f]
            dist += ((tv - mean) / std - (hv - mean) / std) ** 2
            used += 1
        if used >= 3:  # need at least 3 comparable features
            scored.append((dist / used, r))
    scored.sort(key=lambda x: x[0])

    out = []
    for dist, r in scored[:top_n]:
        out.append({
            "date": r[0], "decision": r[-5], "confidence": r[-4],
            "result": r[-3], "day_change_pct": r[-2], "call_correct": r[-1],
            "distance": round(dist, 3),
        })
    return out


def win_rate(conn):
    n, wins = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(call_correct),0) FROM daily_memory "
        "WHERE call_correct IS NOT NULL AND decision != 'NO TRADE'").fetchone()
    return (n, (wins / n * 100) if n else 0.0)
