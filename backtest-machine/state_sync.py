"""
Safe persistence of trading state to the `trading-state` git branch.
====================================================================

Replaces the inline shell in both workflows, which lost the entire trade
history on 2026-08-03. Three defects combined to cause that:

  1. `git show "FETCH_HEAD:$f" > "$f" || true` - the shell CREATES the
     redirect target before git runs, so a failed extract left a 0-byte
     database that was then committed and force-pushed over good data.
  2. Each save did `git init` + `git push -f`, so the branch held exactly
     one commit. Once bad data was pushed there was nothing to recover.
  3. Both workflows force-pushed the same branch under different
     concurrency groups, so they could overwrite each other.

Guarantees provided here:

  RESTORE  Extract to a temp path, verify the file is non-empty AND a
           valid SQLite database with the expected table, and only then
           move it into place. A failed restore leaves NO file, so the
           caller starts clean rather than reading a corrupt one.

  SAVE     Refuses to push a REGRESSION. Each database has a monotonic
           table (closed trades / daily memory) that can only grow; if
           the local count is lower than what is already on the branch,
           the save is rejected. This is the guard that would have
           stopped the 2026-08-03 wipe: a freshly-initialised 0-trade
           database can never overwrite a 39-trade one.

  HISTORY  Commits are appended to the existing branch instead of
           force-pushing a fresh repo, so every prior state stays
           recoverable via git.

  RACE     Pushes are non-forced with pull-rebase-retry, so two
           concurrent workflows merge instead of clobbering.

Usage
-----
    python state_sync.py restore
    python state_sync.py save --own options_trades.db,simple_trades.db
    python state_sync.py save --own market_memory.db --allow-regression

`--own` lists the files this workflow is responsible for; everything else
on the branch is left untouched, so the pre-market and trading workflows
cannot overwrite each other's databases.
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
BRANCH = "trading-state"

# database -> the table that only ever grows (used as the regression guard)
MONOTONIC = {
    "options_trades.db": "options_trades",
    "simple_trades.db": "trades",
    "market_memory.db": "daily_memory",
    "intraday_trades.db": "closed_trades",
    # Observability. `cycle` gets exactly one row per run and is never
    # deleted, so it is the natural monotonic guard - a run that lost the
    # database cannot push an emptier one over a full one.
    "observability.db": "cycle",
}
ALL_FILES = list(MONOTONIC)

# Databases deferred to an end-of-session save instead of the per-cycle one.
#
# O-1: this is now EMPTY, and observability.db rides the ordinary per-cycle
# save with the trading books.
#
# It previously held observability.db, on the reasoning that a per-cycle
# push would add ~45 commits/hour. That reasoning was wrong on both halves:
#
#   * The push already happens. The intraday workflow saves the three
#     trading books EVERY cycle - 362 commits landed on trading-state on
#     2026-08-17 alone. observability.db joins that SAME commit and the
#     SAME push, so the marginal cost is zero. After square-off it is
#     strictly cheaper: the old design produced TWO commits per cycle under
#     one GITHUB_RUN_ID (one for the books, one for observability); now
#     there is one.
#
#   * Deferring did not delay the data, it DESTROYED it. Runners are
#     ephemeral. A cycle wrote its telemetry to the checkout, the EOD save
#     declined to push it, and the runner was deleted. Reproduced over a
#     simulated session: 8 cycles ran, 2 rows survived. Monday retained
#     only 15:15-15:59 - the trading session itself was lost every day.
#
# An empty set keeps the --eod code path harmless rather than removing it:
# a save requested with --eod now selects nothing and returns success
# without pushing, so no second observability-only push can occur.
EOD_ONLY = set()


def run(cmd, cwd=None, check=True, quiet=False):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0 and not quiet:
        print(f"  ! {' '.join(cmd[:4])}... -> rc={r.returncode} {r.stderr.strip()[:160]}")
    return r


def count_rows(path, table):
    """Monotonic row count, or None when the file is missing/unreadable."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return None


def is_valid_db(path, table):
    return count_rows(path, table) is not None


def clone_branch(dest):
    """Shallow clone of the state branch, or an empty repo on first run."""
    url = os.environ.get("STATE_REPO_URL")
    if not url:
        print("  ! STATE_REPO_URL not set")
        return False
    r = run(["git", "clone", "--depth", "5", "--branch", BRANCH, url, str(dest)],
            check=False, quiet=True)
    if r.returncode == 0:
        return True
    print(f"  branch '{BRANCH}' not found - initialising it")
    run(["git", "clone", "--depth", "1", url, str(dest)], check=False, quiet=True)
    if not (dest / ".git").exists():
        run(["git", "init", "-q", str(dest)])
        run(["git", "remote", "add", "origin", url], cwd=dest)
    run(["git", "checkout", "-q", "--orphan", BRANCH], cwd=dest, check=False)
    for f in dest.iterdir():
        if f.name != ".git":
            f.unlink() if f.is_file() else shutil.rmtree(f)
    return True


# --------------------------------------------------------- session timing ---
def past_square_off(now=None):
    """True once the trading session's square-off time has passed.

    Read from the SAME configuration contract the engines run on, so the
    persistence boundary cannot drift away from the trading boundary.
    Falls back to 15:15 IST - the hardcoded engine constant - if the
    contract cannot be loaded, rather than guessing a different time.
    """
    from datetime import datetime
    now = now or datetime.now()
    try:
        import config_contract
        cutoff = config_contract.Contract().square_off
    except Exception:
        cutoff = 15 * 60 + 15
    return (now.hour * 60 + now.minute) >= cutoff


# ------------------------------------------------------------------ restore ---
def restore(skip=()):
    """Restore databases from the branch.

    `skip` lets a workflow decline files it does not own. The pre-market
    workflow uses it for observability.db: that database belongs to the
    intraday workflow alone, and pre-market must neither write nor
    synchronise it.
    """
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "state"
        if not clone_branch(repo):
            print("  No prior state - starting fresh.")
            return 0
        restored, skipped = [], []
        for fname, table in MONOTONIC.items():
            if fname in skip:
                print(f"  skipping {fname} (not owned by this workflow)")
                continue
            src = repo / fname
            if not src.exists() or src.stat().st_size == 0:
                skipped.append(f"{fname} (absent/empty on branch)")
                continue
            tmp = HERE / f"{fname}.restore.tmp"
            shutil.copy2(src, tmp)
            if is_valid_db(tmp, table):
                tmp.replace(HERE / fname)
                restored.append(f"{fname} ({count_rows(HERE / fname, table)} rows)")
            else:
                tmp.unlink(missing_ok=True)
                skipped.append(f"{fname} (INVALID - not a readable database)")
        for r_ in restored:
            print(f"  restored {r_}")
        for s in skipped:
            print(f"  WARNING: could not restore {s} - this book starts fresh")
        return 0


# --------------------------------------------------------------------- save ---
def save(own, allow_regression=False, eod=False):
    own = [f.strip() for f in own.split(",") if f.strip()]
    unknown = [f for f in own if f not in MONOTONIC]
    if unknown:
        print(f"  ! unknown file(s) in --own: {unknown}")
        return 1

    # END-OF-SESSION POLICY.
    #
    # observability.db is appended to on every cycle but is only useful as
    # a complete session, and nothing reads it back intraday. Synchronising
    # it per cycle would mean ~45 pushes/hour to the state branch for data
    # no decision depends on. It is therefore saved once, after square-off.
    #
    # Before square-off the request is a NO-OP, not an error: the same
    # workflow step runs on every cycle and must stay silent until the
    # session ends. The database is not lost by skipping - it is rebuilt
    # from the branch on the next restore and appended to.
    if not eod:
        deferred = [f for f in own if f in EOD_ONLY]
        if deferred:
            print(f"  deferred to end-of-session: {', '.join(deferred)}")
        own = [f for f in own if f not in EOD_ONLY]
    else:
        own = [f for f in own if f in EOD_ONLY]
        if not past_square_off():
            print("  end-of-session save requested before square-off "
                  "- nothing to do yet.")
            return 0
    if not own:
        return 0

    for attempt in range(1, 4):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "state"
            if not clone_branch(repo):
                return 1

            staged, rejected = [], []
            for fname in own:
                table = MONOTONIC[fname]
                local = HERE / fname
                if not local.exists():
                    continue
                local_n = count_rows(local, table)
                if local_n is None:
                    rejected.append(f"{fname}: local file is not a valid database")
                    continue
                remote_n = count_rows(repo / fname, table)
                if (remote_n is not None and local_n < remote_n
                        and not allow_regression):
                    rejected.append(
                        f"{fname}: REGRESSION BLOCKED - local has {local_n} "
                        f"{table} rows, branch has {remote_n}. Refusing to "
                        f"overwrite. (Restore likely failed this run.)")
                    continue
                shutil.copy2(local, repo / fname)
                staged.append(f"{fname} ({local_n} {table} rows"
                              + (f", was {remote_n}" if remote_n is not None else "")
                              + ")")

            for r_ in rejected:
                print(f"  {r_}")
            if not staged:
                print("  Nothing safe to save.")
                return 1 if rejected else 0

            run(["git", "config", "user.name", "intraday-ci"], cwd=repo)
            run(["git", "config", "user.email",
                 "ci@users.noreply.github.com"], cwd=repo)
            run(["git", "add", "-A"], cwd=repo)
            st = run(["git", "status", "--porcelain"], cwd=repo)
            if not st.stdout.strip():
                print("  No changes to commit.")
                return 0
            run(["git", "commit", "-q", "-m",
                 f"state {os.environ.get('GITHUB_RUN_ID', 'local')}: "
                 + "; ".join(staged)], cwd=repo)

            push = run(["git", "push", "origin", BRANCH], cwd=repo,
                       check=False, quiet=True)
            if push.returncode == 0:
                for s in staged:
                    print(f"  saved {s}")
                return 0
            print(f"  push rejected (attempt {attempt}/3) - "
                  f"concurrent update, retrying")
    print("  ! could not push after 3 attempts")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["restore", "save"])
    ap.add_argument("--own", default=",".join(ALL_FILES))
    ap.add_argument("--skip", default="",
                    help="restore: files this workflow does not own")
    ap.add_argument("--allow-regression", action="store_true",
                    help="override the regression guard (recovery only)")
    ap.add_argument("--eod", action="store_true",
                    help="save: end-of-session databases only, and only "
                         "once square-off has passed")
    a = ap.parse_args()
    if a.action == "restore":
        return restore(skip={f.strip() for f in a.skip.split(",") if f.strip()})
    return save(a.own, a.allow_regression, a.eod)


if __name__ == "__main__":
    sys.exit(main())
