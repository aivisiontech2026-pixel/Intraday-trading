"""
Configuration contract (S-03).
==============================

ONE authoritative source for the values the engines run on, and an
explicit statement of what every declared key actually does.

The audit found that options_trader.py read exactly three keys -
experiments, symbols, telegram - while thirteen others sat in
intraday_config.json looking authoritative and doing nothing. Two of
those (risk_per_trade_percent, max_daily_loss_percent) described a risk
envelope that had never existed in code.

This module does not change any value. Every number below is read from
the config file, and each was verified equal to the constant the engine
previously hardcoded, so adopting it is value-neutral:

    capital               100000  == CAPITAL
    max_capital_per_trade  25000  == MAX_PER_TRADE
    max_open_positions         4  == MAX_POSITIONS
    entry_start            09:30  == T_ENTRY_START (570)
    entry_end              14:30  == T_ENTRY_END   (870)
    square_off             15:15  == T_SQUARE_OFF  (915)
    interval                  5m  == INTERVAL

CLASSIFICATION OF EVERY DECLARED KEY
------------------------------------
READ + ENFORCED
    capital                  basis for the daily-loss limit; book size
    max_capital_per_trade    per-position premium budget
    max_open_positions       concurrent position cap
    entry_start / entry_end  entry window
    square_off               mandatory flat-by time
    interval                 signal bar size
    max_daily_loss_percent   ENFORCED as of this change (S-04), per book
    symbols                  trading universe
    telegram                 alert routing
    experiments              feature flags
    ranking                  scoring config (mode stays "shadow")

READ + OBSERVATIONAL
    (none - anything read here is enforced)

CONTRACT REQUIRES IMPLEMENTATION - DEFERRED
    cost_per_side   0.0003   declared but NOT applied in the options
    slippage        0.0002   book. Applying either changes realized and
                             simulated P&L, which is a STRATEGY BEHAVIOUR
                             change and is explicitly out of scope for
                             stabilization. Keys are retained, unmodified
                             and unreinterpreted. NOTE: simple_trader.py
                             separately hardcodes 1.0003/0.9997 on the
                             cash path - numerically equal to
                             cost_per_side but not read from it. That
                             duplication is documented, not "fixed",
                             because consolidating it would alter stock
                             P&L accounting.
    risk_per_trade_percent 1 declared but not enforced. Measured actual
                             risk is ~2.94% of book per trade (max 3.75%),
                             a consequence of the -15% premium stop and
                             the Rs.25,000 budget. Enforcing 1% would
                             change position sizing - STRATEGY BEHAVIOUR,
                             out of scope.
    max_daily_profit_percent 5 declared, no counterpart in code. A profit
                             halt is a strategy decision, not a safety
                             control, and no authorization exists.

LEGACY / NOT APPLICABLE TO THE OPTIONS ENGINE
    atr_trail_mult  8.0      the options trail is percentage-based
                             (TRAIL_PCT); ATR is used only by the
                             equity backtest.
    period          60d      the live engine requests "5d"; 60d is the
                             research/backtest window. Documented as a
                             genuine contradiction, NOT silently changed -
                             altering the request window would change the
                             signal (S-01 territory, frozen).
    mode            paper    no code branches on it; both engines are
                             paper-only by construction.

Nothing here is deleted. Nothing is reinterpreted. Where the contract and
the implementation disagree, the disagreement is recorded rather than
resolved by assumption.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
CFG_FILE = HERE / "intraday_config.json"

ENFORCED = "READ + ENFORCED"
DEFERRED = "CONTRACT REQUIRES IMPLEMENTATION - DEFERRED"
LEGACY = "LEGACY / NOT APPLICABLE"

CLASSIFICATION = {
    "capital": ENFORCED,
    "max_capital_per_trade": ENFORCED,
    "max_open_positions": ENFORCED,
    "entry_start": ENFORCED,
    "entry_end": ENFORCED,
    "square_off": ENFORCED,
    "interval": ENFORCED,
    "max_daily_loss_percent": ENFORCED,
    "symbols": ENFORCED,
    "telegram": ENFORCED,
    "experiments": ENFORCED,
    "ranking": ENFORCED,
    "cost_per_side": DEFERRED,
    "slippage": DEFERRED,
    "risk_per_trade_percent": DEFERRED,
    "max_daily_profit_percent": DEFERRED,
    "atr_trail_mult": LEGACY,
    "period": LEGACY,
    "mode": LEGACY,
}


def load():
    if CFG_FILE.exists():
        return json.loads(CFG_FILE.read_text())
    return {}


def _hhmm(s, fallback):
    """'09:30' -> 570 minutes past midnight. Falls back rather than raising
    so a malformed value can never take the engine down."""
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return fallback


class Contract:
    """Resolved, validated values. Fallbacks equal the previously
    hardcoded constants, so a missing key changes nothing."""

    def __init__(self, cfg=None):
        c = cfg if cfg is not None else load()
        self.raw = c
        self.capital = float(c.get("capital", 100_000))
        self.max_per_trade = float(c.get("max_capital_per_trade", 25_000))
        self.max_positions = int(c.get("max_open_positions", 4))
        self.entry_start = _hhmm(c.get("entry_start", "09:30"), 9 * 60 + 30)
        self.entry_end = _hhmm(c.get("entry_end", "14:30"), 14 * 60 + 30)
        self.square_off = _hhmm(c.get("square_off", "15:15"), 15 * 60 + 15)
        self.interval = str(c.get("interval", "5m"))
        # Enforced by safety_supervisor as of S-04. None disables the
        # limit rather than substituting a guessed number.
        v = c.get("max_daily_loss_percent")
        self.max_daily_loss_percent = float(v) if v is not None else None

    def daily_loss_limit(self):
        if self.max_daily_loss_percent is None:
            return None
        return abs(self.capital * self.max_daily_loss_percent / 100.0)

    def describe(self):
        return [(k, self.raw.get(k), CLASSIFICATION.get(k, "UNCLASSIFIED"))
                for k in CLASSIFICATION]
