"""Task A: preserve scrip-master tokens before they expire. NO CREDENTIALS.

WHY THIS EXISTS. Phase 3B-1 established that Angel retains historical data for
365+ days, but retention only means the API will serve a contract you can
NAME. The scrip master is a live snapshot; once a contract expires its token
vanishes from it, and a token is an opaque exchange integer that cannot be
derived from a symbol. 6,921 of 140,040 contracts are addressable, and every
day this does not run, another few hundred become permanently unreachable.

WHAT IS STORED - AND WHY NOT A DAILY SNAPSHOT. The obvious design is one file
per day. Measured on the real master, that is:

    full master, as fetched          38.8 MB/day  ->  14.15 GB/year
    8 fields, all rows               25.3 MB/day  ->   0.75 GB/year gzipped
    8 fields, NFO+NSE only            8.2 MB/day  ->   0.24 GB/year gzipped

All three store the same ~49,000 unchanged rows over and over. What a future
backfill actually needs is not "the master on day X" but "the token for
NIFTY11AUG2624550PE, and the window it was listed". That is a CUMULATIVE
REGISTRY, one row per token ever seen, and it grows only by the genuinely new
contracts each day - a few hundred, not fifty thousand.

    registry, one row per token ever seen   ~30 MB TOTAL after a year,
                                            not per year

The disappearance log falls out of it for free: any token whose last_seen is
before today has expired, and last_seen says when. That log is the record of
exactly which tokens became unaddressable and on what date.

IDEMPOTENT BY CONSTRUCTION. The registry is keyed on token; a re-run on the
same day rewrites last_seen with the same value. Running twice is
indistinguishable from running once.

NO CREDENTIALS. The master is a public unauthenticated endpoint. This module
never reads an ANGEL_* variable and the workflow never passes one.
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.request
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIP_URL = ("https://margincalculator.angelone.in/OpenAPI_File/files/"
             "OpenAPIScripMaster.json")
# Kept segments. NFO carries the F&O contracts whose tokens expire; NSE carries
# the cash and index rows a backfill needs to resolve spot. BSE/BFO/CDS/MCX are
# dropped - this project trades none of them, and they are 60% of the file.
KEEP_SEGMENTS = ("NFO", "NSE")
FIELDS = ("token", "symbol", "name", "expiry", "strike", "instrumenttype",
          "exch_seg", "lotsize")

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_registry(
    token TEXT PRIMARY KEY,
    symbol TEXT, name TEXT, expiry TEXT, strike TEXT,
    instrumenttype TEXT, exch_seg TEXT, lotsize TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_reg_symbol ON token_registry(symbol);
CREATE INDEX IF NOT EXISTS ix_reg_name   ON token_registry(name, expiry);
CREATE INDEX IF NOT EXISTS ix_reg_last   ON token_registry(last_seen);

-- One row per run. The audit trail for whether the archive has holes.
CREATE TABLE IF NOT EXISTS snapshot_run(
    run_date TEXT PRIMARY KEY,
    fetched_at_utc TEXT, rows_in_master INTEGER, rows_kept INTEGER,
    new_tokens INTEGER, disappeared_tokens INTEGER, note TEXT);

-- Append-only: the record of exactly which tokens became unaddressable.
CREATE TABLE IF NOT EXISTS disappeared(
    token TEXT PRIMARY KEY,
    symbol TEXT, name TEXT, expiry TEXT, instrumenttype TEXT,
    first_seen TEXT, last_seen TEXT, noticed_on TEXT);
"""


def fetch_master(path=None):
    """The public master, or a local file for offline testing."""
    if path:
        print(f"  reading local master {path}")
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    print(f"  fetching {SCRIP_URL}")
    req = urllib.request.Request(SCRIP_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def update(db, raw, run_date):
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    kept = [r for r in raw if r.get("exch_seg") in KEEP_SEGMENTS]
    seen = {str(r.get("token")): r for r in kept if r.get("token") is not None}

    before = {t for (t,) in conn.execute("SELECT token FROM token_registry")}
    prev_live = {t for (t,) in conn.execute(
        "SELECT token FROM token_registry WHERE last_seen=("
        "SELECT MAX(last_seen) FROM token_registry WHERE last_seen<?)",
        (run_date,))}

    rows = [(t, str(r.get("symbol") or ""), str(r.get("name") or ""),
             str(r.get("expiry") or ""), str(r.get("strike") or ""),
             str(r.get("instrumenttype") or ""), str(r.get("exch_seg") or ""),
             str(r.get("lotsize") or ""), run_date, run_date)
            for t, r in seen.items()]
    # first_seen is preserved on conflict; only last_seen advances. That is
    # what makes a same-day re-run a no-op rather than a corruption.
    conn.executemany(
        "INSERT INTO token_registry(token,symbol,name,expiry,strike,"
        "instrumenttype,exch_seg,lotsize,first_seen,last_seen) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(token) DO UPDATE SET last_seen=excluded.last_seen",
        rows)

    new = sorted(set(seen) - before)
    gone = sorted(prev_live - set(seen)) if prev_live else []
    for t in gone:
        r = conn.execute(
            "SELECT symbol,name,expiry,instrumenttype,first_seen,last_seen "
            "FROM token_registry WHERE token=?", (t,)).fetchone()
        if r:
            conn.execute(
                "INSERT OR IGNORE INTO disappeared(token,symbol,name,expiry,"
                "instrumenttype,first_seen,last_seen,noticed_on) "
                "VALUES(?,?,?,?,?,?,?,?)", (t,) + tuple(r) + (run_date,))
    conn.execute(
        "INSERT INTO snapshot_run(run_date,fetched_at_utc,rows_in_master,"
        "rows_kept,new_tokens,disappeared_tokens,note) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(run_date) DO UPDATE SET fetched_at_utc=excluded.fetched_at_utc,"
        "rows_in_master=excluded.rows_in_master,rows_kept=excluded.rows_kept,"
        "new_tokens=excluded.new_tokens,disappeared_tokens=excluded.disappeared_tokens",
        (run_date, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         len(raw), len(seen), len(new), len(gone), f"segments={KEEP_SEGMENTS}"))
    conn.commit()
    return conn, seen, new, gone


def report(conn, seen, new, gone, run_date):
    print(f"\n  rows in master : {len(seen):,} kept (segments {KEEP_SEGMENTS})")
    tot = conn.execute("SELECT COUNT(*) FROM token_registry").fetchone()[0]
    dis = conn.execute("SELECT COUNT(*) FROM disappeared").fetchone()[0]
    runs = conn.execute("SELECT COUNT(*) FROM snapshot_run").fetchone()[0]
    print(f"  registry total : {tot:,} tokens ever seen, across {runs} run(s)")
    print(f"  disappeared    : {dis:,} tokens recorded as no longer listed")

    print(f"\n  NEW since the previous run: {len(new):,}")
    if new:
        c = Counter(seen[t].get("instrumenttype") or "(blank)" for t in new)
        print(f"    by type: {dict(c)}")
        for t in new[:5]:
            r = seen[t]
            print(f"    + {t:<9} {str(r.get('symbol')):<26} "
                  f"exp {r.get('expiry') or '-'}")

    print(f"\n  DISAPPEARED (now permanently unaddressable): {len(gone):,}")
    for t in gone[:8]:
        r = conn.execute("SELECT symbol,expiry,last_seen FROM disappeared "
                         "WHERE token=?", (t,)).fetchone()
        if r:
            print(f"    - {t:<9} {str(r[0]):<26} expiry {r[1] or '-':<12} "
                  f"last listed {r[2]}")
    if len(gone) > 8:
        print(f"    ... and {len(gone)-8:,} more")

    print("\n  universe coverage (the 20 names this project trades):")
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        cfg = json.load(open(os.path.join(here, "intraday_config.json")))
        uni = [s.replace(".NS", "") for s in cfg.get("symbols", [])] + \
              ["NIFTY", "BANKNIFTY"]
        q = ",".join("?" * len(uni))
        n = conn.execute(f"SELECT COUNT(*) FROM token_registry WHERE name IN ({q})",
                         uni).fetchone()[0]
        liv = conn.execute(f"SELECT COUNT(*) FROM token_registry WHERE name IN ({q})"
                           " AND last_seen=?", uni + [run_date]).fetchone()[0]
        print(f"    {n:,} tokens ever seen, {liv:,} listed today")
    except Exception as e:
        print(f"    (config unavailable: {type(e).__name__})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get(
        "ARCHIVE_DB", "scrip_token_registry.db"))
    ap.add_argument("--local-master", default=None,
                    help="read a local master file instead of the network")
    ap.add_argument("--date", default=None, help="override the run date")
    a = ap.parse_args()
    run_date = a.date or dt.date.today().isoformat()

    print("=" * 78)
    print("SCRIP MASTER TOKEN ARCHIVE - no credentials, public endpoint only")
    print(f"run date {run_date}   db {a.db}")
    print("=" * 78)
    raw = fetch_master(a.local_master)
    print(f"  master rows: {len(raw):,}")
    conn, seen, new, gone = update(a.db, raw, run_date)
    report(conn, seen, new, gone, run_date)
    size = os.path.getsize(a.db) if os.path.exists(a.db) else 0
    print(f"\n  registry file: {size/1e6:.2f} MB")
    conn.close()
    print("\n" + "=" * 78)
    print("ARCHIVE UPDATED")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
