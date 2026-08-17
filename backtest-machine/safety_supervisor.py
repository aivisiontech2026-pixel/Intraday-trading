"""
Independent safety supervisor (S-04 / S-06).
===========================================

Sits ABOVE the strategy and answers exactly one question:

    may this book open a NEW position right now?

    SAFETY SUPERVISOR
           |
           v
    ENTRY PERMISSION
           |
           v
       STRATEGY

It never closes a position, never sizes one, never picks one. Existing
positions remain the strategy's responsibility and are unaffected by any
decision made here - a halt blocks NEW ENTRIES only, and stops, trailing
stops, reversal exits and the 15:15 square-off all continue to run.

DAILY-LOSS LATCH - the only ENFORCED control
--------------------------------------------
Contract (already declared in intraday_config.json, not invented here):

    capital               = 100000
    max_daily_loss_percent = 2          ->  limit = Rs.2,000

Scope is PER BOOK. The options book and the stock book each carry their
own Rs.100,000 and their own Rs.2,000 limit. A loss in one MUST NOT halt
the other, so every call names its book and reads only that book's ledger.

The latch is LATCHING for the session: once the running realized P&L
reaches -Rs.2,000 at ANY point, entry permission is false for the rest of
the day even if later trades bring the total back above the line.

RESTART SAFETY WITHOUT NEW STATE
--------------------------------
The latch is DERIVED, not stored. It is the LOW-WATER MARK of today's
running realized P&L, computed by replaying the session's closed trades
in exit order:

    running = 0; low = 0
    for each trade closed today, ordered by exit_time:
        running += pnl
        low = min(low, running)
    halted = low <= -limit

Because the authoritative trade ledger is what persists (and is restored
by state_sync every cycle), the latch survives a process restart, a
workflow restart, and an ephemeral runner - with no flag to lose, no
migration, and no dependency on observability.db. Section 4 of the
authorization requires the calculation be reproducible from the ledger;
deriving the latch the same way makes that true of the halt as well.

FAIL-SAFE
---------
If permission cannot be evaluated - unreadable ledger, bad schema,
anything - the supervisor returns BLOCKED. The strategy cannot override
it, cannot disable it, and cannot reach the entry path without asking.
An unavailable supervisor stops new risk; it never silently permits it.

WHAT IS DELIBERATELY *NOT* ENFORCED
-----------------------------------
Drawdown, consecutive losses, spread, latency and outage counts have no
declared threshold anywhere in the contract. Inventing one would be a
strategy change. They are recorded as OBSERVED STATE only, so the
Monday-Friday window produces the evidence a future threshold would need.
"""

import sqlite3
from datetime import date

# Reasons are stable strings so telemetry and tests can assert on them.
ALLOW = "ALLOWED"
HALT_DAILY_LOSS = "HALTED_DAILY_LOSS"
BLOCK_UNAVAILABLE = "BLOCKED_SUPERVISOR_UNAVAILABLE"

OPTIONS_BOOK = "options"
STOCK_BOOK = "stock"

# Ledger shape per book: (table, pnl column, exit-time column).
# Only these two books exist; an unknown book fails safe.
_LEDGER = {
    OPTIONS_BOOK: ("options_trades", "pnl", "exit_time"),
    STOCK_BOOK: ("trades", "pnl", "exit_time"),
}


def daily_realized(conn, book, today):
    """(running_total, low_water) of today's realized P&L for one book.

    low_water is the most negative the running total ever reached during
    the session - which is what makes the halt latching. Returns
    (None, None) if it cannot be computed; callers must treat that as
    unavailable, never as zero.
    """
    spec = _LEDGER.get(book)
    if spec is None:
        return None, None
    table, pnl_col, ts_col = spec
    day = today.isoformat() if isinstance(today, date) else str(today)
    try:
        # ORDER BY exit_time ALONE is not a total order. The stock book
        # stamps one timestamp per cycle, so a cycle closing four positions
        # writes four byte-identical exit_time values - 13 such groups exist
        # in the live ledger, up to 4 rows each. SQL leaves ties unordered,
        # and the low-water mark is order-dependent: across the orderings of
        # one real tied set the mark ranged -2,600 to -1,800, which straddles
        # the -2,000 limit. Adding rowid makes the replay a total order,
        # matching insertion order - the true sequence in which those exits
        # were written. This changes no threshold and no policy; it fixes the
        # determinism of a SAFETY control.
        rows = conn.execute(
            f"SELECT {pnl_col} FROM {table} "
            f"WHERE {ts_col} LIKE ? ORDER BY {ts_col}, rowid", (f"{day}%",)
        ).fetchall()
    except sqlite3.Error:
        return None, None
    running = 0.0
    low = 0.0
    for (p,) in rows:
        running += (p or 0.0)
        low = min(low, running)
    return round(running, 2), round(low, 2)


def daily_loss_limit(capital, max_daily_loss_percent):
    """Rs. limit from the DECLARED contract. No default, no invention."""
    if capital is None or max_daily_loss_percent is None:
        return None
    return abs(float(capital) * float(max_daily_loss_percent) / 100.0)


def entry_permission(conn, book, today, capital, max_daily_loss_percent):
    """May `book` open a NEW position?  -> (allowed: bool, state: dict)

    Fails SAFE: any inability to evaluate returns allowed=False.
    Never consults or affects the other book.
    """
    state = {
        "book": book,
        "session": today.isoformat() if isinstance(today, date) else str(today),
        "limit": None,
        "realized_today": None,
        "realized_low_water": None,
        "allowed": False,
        "reason": BLOCK_UNAVAILABLE,
    }
    try:
        limit = daily_loss_limit(capital, max_daily_loss_percent)
        if limit is None:
            return False, state
        state["limit"] = limit

        running, low = daily_realized(conn, book, today)
        if running is None:
            return False, state
        state["realized_today"] = running
        state["realized_low_water"] = low

        # LATCHING: the low-water mark decides, not the current total.
        if low <= -limit:
            state["allowed"] = False
            state["reason"] = HALT_DAILY_LOSS
            return False, state

        state["allowed"] = True
        state["reason"] = ALLOW
        return True, state
    except Exception:
        # Fail safe. An unavailable supervisor blocks new risk.
        state["allowed"] = False
        state["reason"] = BLOCK_UNAVAILABLE
        return False, state


def observe_health(smart_ok=None, master_ok=None, signals_ok=None,
                   quotes_ok=None, quote_age_s=None, session_status=None):
    """Health/outage state recorded for evidence ONLY.

    No threshold is applied to any of these. The contract declares none,
    so applying one would be an invented trading gate. They exist so the
    observation window can establish what a threshold should be.
    """
    return {
        "api_health": "OK" if smart_ok else ("DOWN" if smart_ok is not None else "UNKNOWN"),
        "instrument_master": "OK" if master_ok else ("DOWN" if master_ok is not None else "UNKNOWN"),
        "signal_data": "OK" if signals_ok else ("DOWN" if signals_ok is not None else "UNKNOWN"),
        "quote_data": "OK" if quotes_ok else ("DOWN" if quotes_ok is not None else "UNKNOWN"),
        "quote_age_s": quote_age_s,          # None = UNAVAILABLE, never 0
        "session_status": session_status,
        "enforced": False,                    # explicit: observation only
    }
