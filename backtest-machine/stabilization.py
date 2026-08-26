"""
Wednesday 2026-08-26 stabilization: entry gates, ledger, trail floor, costs.
============================================================================

WHY A SEPARATE MODULE
---------------------
Every rule here is a TEMPORARY, CONFIGURABLE, REVERSIBLE risk control or a
data-validity check. Keeping them in one file that the trading engine calls
means:

  * each rule is a pure function and can be unit-tested without a broker,
    a database, or a market;
  * `stabilization.enabled(CFG) is False` restores baseline behaviour
    exactly, so rollback is a one-line configuration change and does not
    need a code revert;
  * a reviewer can read the entire safety surface in one place.

THE INVARIANT THAT MATTERS MOST
-------------------------------
An entry gate must never be able to block an EXIT.

Every gate in this module is reachable only through `authorize(ENTRY, ...)`.
`authorize(EXIT, ...)` returns PASS unconditionally and reads none of the
gate inputs - it cannot raise, cannot consult a threshold, and cannot
consult risk state. That is asserted directly in the tests, including with
every gate input deliberately set to its worst possible value.

The trading engine reinforces this structurally: exits are executed by the
position-management loop, which runs BEFORE any gate is consulted and
reads only `positions` and `quotes`. `square_off_net()` is further outside
even that. Neither path calls anything in this module.

GATES REJECT; THEY NEVER SUBSTITUTE
-----------------------------------
A rejected candidate is DROPPED. No gate here returns an alternative, and
no gate result is used by the caller to pick a different expiry, strike,
option or underlying. `nearest_expiry()` and `find_option()` are each
called exactly once per candidate and have no retry loop, so there is no
mechanism a rejection could drive. The caller additionally records every
gate-rejected symbol so the momentum fallback tier cannot re-admit it.

GATES REJECT ON MISSING DATA
----------------------------
Missing, null, unparseable, crossed, zero or negative input is a
REJECTION. There is no default-allow anywhere in this file.
"""

import os
from collections import OrderedDict
from datetime import date, datetime

# ---------------------------------------------------------------- intents ---
ENTRY = "ENTRY"
EXIT = "EXIT"

# -------------------------------------------------------- default policy ----
# Every value below is a TEMPORARY STABILIZATION POLICY for the
# 2026-08-26 session. None of them is claimed to be an optimized or
# validated strategy threshold, and none was tuned against the days it is
# validated on.
DEFAULTS = OrderedDict((
    ("enabled", True),
    # CHANGE 1 - signal / session validity.
    # Applied to the age of the LATEST OBSERVED bar in the fetched frame,
    # which is the quantity `telemetry.bar_age_s` already records and the
    # one the 2026-08-24 09:15 evidence is expressed in (237,055 s).
    ("max_signal_bar_age_s", 400),
    # CHANGE 2 - minimum days to expiry. 2 is the least restrictive value
    # that removes every observed DTE 0-1 trade; DTE 2-3 has ZERO
    # in-sample observations, so 2 and 4 are behaviourally identical on
    # all historical data. It is a floor, not a tuned threshold.
    ("min_dte", 2),
    # CHANGE 5 - maximum quoted spread as a percentage of mid.
    ("max_entry_spread_pct", 1.0),
    # CHANGE 6 - concurrent option positions. Deliberately a SEPARATE key
    # from `max_open_positions`, which paper_trader.py and
    # intraday_backtest.py also read: changing that key would alter the
    # stock book, which is out of scope.
    ("max_option_positions", 2),
    # CHANGE 4 - round-trip cost floor for the trailing stop, in percent
    # of entry. An armed trail may never produce an exit level below
    # entry * (1 + this/100). 0.06 = 0.03% per side, the rate already
    # declared as `cost_per_side` in intraday_config.json.
    ("trail_min_exit_pct", 0.06),
    # SECTION 34 - pre-open smoke test. When true, every ENTRY is refused
    # at the final authorization and recorded in the ledger as
    # `other_dry_run`. Exits are untouched: `authorize` returns PASS for
    # any non-ENTRY intent before this (or any other) value is read, so a
    # dry run can never strand an open position. Also settable per-run
    # with OPTIONS_DRY_RUN=1 so the smoke test needs no config edit.
    ("dry_run", False),
))


def get_config(cfg=None):
    """Merged policy. Unknown keys are ignored; missing keys take DEFAULTS."""
    out = dict(DEFAULTS)
    blk = (cfg or {}).get("stabilization") or {}
    for k in DEFAULTS:
        if k in blk and blk[k] is not None:
            if isinstance(DEFAULTS[k], bool):
                out[k] = bool(blk[k])
            else:
                out[k] = type(DEFAULTS[k])(blk[k])
    # OPTIONS_DRY_RUN is a one-way switch: the environment may TURN ON the
    # dry run, never turn one off. A smoke test can therefore be requested
    # without editing configuration, and an unset/garbage variable cannot
    # silently re-enable live entries.
    if str(os.environ.get("OPTIONS_DRY_RUN", "")).strip() in ("1", "true",
                                                              "TRUE", "yes"):
        out["dry_run"] = True
    return out


def enabled(cfg=None):
    return bool(get_config(cfg)["enabled"])


# ---------------------------------------------------------------- results ---
class GateResult(tuple):
    """(ok, reason). A truthy result carries reason None."""
    __slots__ = ()

    def __new__(cls, ok, reason=None):
        return tuple.__new__(cls, (bool(ok), reason))

    ok = property(lambda self: self[0])
    reason = property(lambda self: self[1])

    def __bool__(self):
        return self[0]

    def __repr__(self):
        return "GateResult(ok=%r, reason=%r)" % (self[0], self[1])


PASS = GateResult(True, None)


def _reject(reason):
    return GateResult(False, reason)


# ------------------------------------------------------------- CHANGE 1 ----
def parse_bar_ts(value):
    """Bar timestamp -> naive datetime, or None when unusable.

    Mirrors telemetry._parse_ts / _bar_status normalisation so the gate and
    the recorded evidence cannot disagree. Timezone-aware stamps are
    reduced to their own wall clock, which is what `bar_age_s` has always
    been measured against.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    s = str(value).strip()
    if not s or s.lower() in ("nat", "nan", "none"):
        return None
    d = None
    try:
        d = datetime.fromisoformat(s)
    except Exception:
        d = None
    if d is None:
        for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M:%S.%f", "%d-%b-%Y %H:%M:%S"):
            try:
                d = datetime.strptime(s, fmt)
                break
            except Exception:
                continue
    if d is None:
        return None
    return d.replace(tzinfo=None) if d.tzinfo is not None else d


def bar_age_s(bar_ts, observed_at):
    """Seconds between a bar stamp and an observation instant, or None.

    None on anything unreadable - and None is a REJECTION upstream, never
    a pass.
    """
    b, o = parse_bar_ts(bar_ts), parse_bar_ts(observed_at)
    if b is None or o is None:
        return None
    return (o - b).total_seconds()


def check_signal_freshness(sig, trading_date, cfg_or_max_age, now=None,
                           require_direction=False):
    """May the signal for one underlying authorize an ENTRY?

    `sig` is the per-symbol signal dict the engine already builds. The
    fields consulted (`bar_ts`, `used_bar_ts`, `observed_at`,
    `session_status`) are recorded at fetch time from the SAME dataframe
    the direction was computed on - nothing is refetched and no market
    call is made here.

    Rejects, in order, on: missing payload, unparseable payload, no
    direction, missing bar stamp, unreadable bar stamp, a bar whose
    session date is not the current trading date, a SIGNAL bar whose
    session date is not the current trading date, an unknown age, a bar
    stamped in the future, an age above the configured threshold, and a
    session status the telemetry layer already marked not VALID.
    """
    max_age = (cfg_or_max_age["max_signal_bar_age_s"]
               if isinstance(cfg_or_max_age, dict) else cfg_or_max_age)
    if sig is None:
        return _reject("signal_missing")
    if not isinstance(sig, dict):
        return _reject("signal_unparseable")
    # `direction is None` is NOT a freshness failure. The momentum
    # fallback tier deliberately trades symbols with no EMA/VWAP
    # confluence, and the confluence tier already guards on direction
    # itself. Gating on it here would silently delete the entire momentum
    # tier under the guise of a data-validity check - a strategy change.
    if require_direction and sig.get("direction") is None:
        return _reject("signal_no_direction")

    bar_ts = sig.get("bar_ts")
    if bar_ts is None or (isinstance(bar_ts, str) and not bar_ts.strip()):
        return _reject("signal_bar_ts_missing")
    b = parse_bar_ts(bar_ts)
    if b is None:
        return _reject("signal_bar_ts_unparseable")

    td = (trading_date.isoformat() if isinstance(trading_date, date)
          and not isinstance(trading_date, datetime)
          else str(trading_date)[:10])
    if b.date().isoformat() != td:
        return _reject("signal_bar_previous_session")

    # The bar the DIRECTION was actually computed on (the last proven-closed
    # bar) must also belong to the current session. On 2026-08-24 the first
    # observation of the day carried Friday 15:25 in BOTH fields.
    used = sig.get("used_bar_ts")
    if used is not None:
        u = parse_bar_ts(used)
        if u is None:
            return _reject("signal_used_bar_unparseable")
        if u.date().isoformat() != td:
            return _reject("signal_used_bar_previous_session")

    observed = now if now is not None else sig.get("observed_at")
    age = bar_age_s(bar_ts, observed)
    if age is None:
        return _reject("signal_age_unknown")
    if age < 0:
        return _reject("signal_bar_in_future")
    if age > float(max_age):
        return _reject("signal_bar_stale")

    status = sig.get("session_status")
    if status is not None and status != "VALID":
        return _reject("signal_session_" + str(status).lower())
    return PASS


# ------------------------------------------------------------- CHANGE 2 ----
def days_to_expiry(expiry, today):
    """Whole calendar days from `today` to `expiry`, or None if unreadable."""
    try:
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        elif not isinstance(expiry, date):
            expiry = datetime.fromisoformat(str(expiry)[:10]).date()
        if isinstance(today, datetime):
            today = today.date()
        elif not isinstance(today, date):
            today = datetime.fromisoformat(str(today)[:10]).date()
        return (expiry - today).days
    except Exception:
        return None


def check_dte(expiry, today, cfg_or_min_dte):
    """Reject contracts expiring sooner than the configured floor.

    A rejection DROPS the candidate. It must never roll the selection to
    the next listed expiry - the caller calls `nearest_expiry()` once and
    has no retry path, and the rejected symbol is recorded so the momentum
    fallback tier cannot re-consider it either.
    """
    min_dte = (cfg_or_min_dte["min_dte"]
               if isinstance(cfg_or_min_dte, dict) else cfg_or_min_dte)
    d = days_to_expiry(expiry, today)
    if d is None:
        return _reject("dte_unreadable")
    if d < int(min_dte):
        return _reject("dte_below_min(%d<%d)" % (d, int(min_dte)))
    return PASS


# ------------------------------------------------------------- CHANGE 5 ----
def spread_pct(quote):
    """Quoted spread as a percentage of mid, or None when unquotable.

    None for a missing, non-numeric, zero, negative or CROSSED quote.
    None is a rejection upstream - never treated as a zero spread.
    """
    if not isinstance(quote, dict):
        return None
    try:
        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    if ask < bid:                      # crossed book
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 100.0


def check_quote(quote):
    """Is there a usable live quote at all?"""
    if not isinstance(quote, dict):
        return _reject("quote_missing")
    try:
        ltp = float(quote.get("ltp") or 0.0)
    except (TypeError, ValueError):
        return _reject("quote_unparseable")
    if ltp <= 0:
        return _reject("quote_no_ltp")
    return PASS


# Binary floating point cannot represent most decimal quotes exactly, so a
# spread that is EXACTLY at the threshold can compute as 1.0000000000000142.
# Without this tolerance the gate would reject a quote the policy admits,
# on rounding noise alone. 1e-9 percentage points is far below any
# meaningful spread and changes no genuine verdict.
_SPREAD_EPS = 1e-9


def check_spread(quote, cfg_or_max_pct):
    max_pct = (cfg_or_max_pct["max_entry_spread_pct"]
               if isinstance(cfg_or_max_pct, dict) else cfg_or_max_pct)
    sp = spread_pct(quote)
    if sp is None:
        return _reject("quote_invalid_for_spread")
    if sp > float(max_pct) + _SPREAD_EPS:
        return _reject("spread_above_max(%.4f%%>%.4f%%)" % (sp, float(max_pct)))
    return PASS


# ------------------------------------------------------------- CHANGE 4 ----
def trail_stop_level(entry_price, high_water, activate_pct, trail_pct,
                     cfg_or_min_exit_pct):
    """Trailing stop level once armed, floored at entry + round-trip cost.

    U-014: the trail band (12%) is WIDER than the arm threshold (10%), so
    an armed trail sat at entry * 1.10 * 0.88 = 0.968 - i.e. 3.2% BELOW
    entry. 17 of 53 live-price trailing exits closed inside that band, for
    -Rs.6,358, and the worst was -3.168%, within 0.001 of the arithmetic
    floor.

    The correction is a FLOOR, not a redesign: the band and the arm
    threshold are unchanged, so every trail that was already above entry
    behaves identically. Only the mathematically-impossible region is
    removed.

    Returns None when the trail is not armed - the caller then keeps the
    initial stop, exactly as before.
    """
    try:
        entry_price = float(entry_price)
        high_water = float(high_water)
    except (TypeError, ValueError):
        return None
    if entry_price <= 0:
        return None
    if high_water < entry_price * (1 + float(activate_pct)):
        return None                       # not armed
    min_pct = (cfg_or_min_exit_pct["trail_min_exit_pct"]
               if isinstance(cfg_or_min_exit_pct, dict) else cfg_or_min_exit_pct)
    raw = high_water * (1 - float(trail_pct))
    floor = entry_price * (1 + float(min_pct) / 100.0)
    return max(raw, floor)


# ------------------------------------------------------------ CHANGE 10 ----
# Indian F&O transaction costs for OPTIONS. REPORTING ONLY - nothing here
# reaches execution, stop levels, trail arming, ranking or selection.
# Rates are DECLARED here, not inferred, so a correction is a one-line
# edit with a visible source (see COST_RATE_SOURCE).
COST_RATES = OrderedDict((
    ("brokerage_per_order", 20.0),          # flat, per executed order
    ("stt_pct_sell_premium", 0.15),         # sell side only, on premium
    ("exchange_txn_pct", 0.0355299),        # NSE equity options, buy + sell
    ("sebi_pct", 0.0001),                   # Rs.10 / crore
    ("ipft_pct", 0.002),
    ("stamp_duty_pct_buy", 0.003),          # buy side only, on premium
    ("gst_pct_on_charges", 18.0),           # brokerage + txn + SEBI + IPFT
))
COST_RATE_SOURCE = (
    "Angel One published schedule for EQUITY OPTIONS (NSE F&O), retrieved "
    "2026-08-26 from https://www.angelone.in/exchange-transaction-charges : "
    "brokerage Rs.20/executed order; STT 0.15% sell side on premium; "
    "exchange transaction 0.0355299% buy+sell; SEBI turnover Rs.10/crore; "
    "IPFT 0.002%; stamp duty 0.003% buy side on premium; GST 18% on "
    "brokerage + transaction + SEBI + IPFT. "
    "This is PAPER TRADING, so there is no contract note to reconcile "
    "against - these are declared rates, not observed ones. NOT read from "
    "intraday_config.json's cost_per_side (0.0003), a different "
    "percentage-of-turnover model used by the stock backtest and "
    "classified DEFERRED in config_contract.py. REPORTING ONLY: no "
    "execution, stop, trail, ranking or selection path reads these.")


def round_trip_cost(entry_price, exit_price, qty, rates=None):
    """Modeled round-trip transaction cost in rupees. REPORTING ONLY."""
    r = dict(COST_RATES)
    r.update(rates or {})
    try:
        buy_val = float(entry_price) * float(qty)
        sell_val = float(exit_price) * float(qty)
    except (TypeError, ValueError):
        return None
    if buy_val < 0 or sell_val < 0:
        return None
    turnover = buy_val + sell_val
    brokerage = 2 * r["brokerage_per_order"]
    stt = sell_val * r["stt_pct_sell_premium"] / 100.0
    exch = turnover * r["exchange_txn_pct"] / 100.0
    sebi = turnover * r["sebi_pct"] / 100.0
    ipft = turnover * r["ipft_pct"] / 100.0
    stamp = buy_val * r["stamp_duty_pct_buy"] / 100.0
    gst = (brokerage + exch + sebi + ipft) * r["gst_pct_on_charges"] / 100.0
    return OrderedDict((
        ("brokerage", brokerage), ("stt", stt), ("exchange", exch),
        ("sebi", sebi), ("ipft", ipft), ("stamp_duty", stamp), ("gst", gst),
        ("total", brokerage + stt + exch + sebi + ipft + stamp + gst),
    ))


def spread_friction(entry_bid, entry_ask, exit_bid, exit_ask, qty):
    """Half-spread paid on each leg, in rupees. REPORTING ONLY.

    Entries fill at the ASK and exits at the BID, so the friction versus
    mid is (ask-mid) going in and (mid-bid) coming out - one half-spread
    per leg.
    """
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return None
    total = 0.0
    for bid, ask in ((entry_bid, entry_ask), (exit_bid, exit_ask)):
        try:
            b, a = float(bid or 0), float(ask or 0)
        except (TypeError, ValueError):
            return None
        if b <= 0 or a <= 0 or a < b:
            return None
        total += (a - b) / 2.0
    return total * qty


# ------------------------------------------------------------- CHANGE 7 ----
# Terminal dispositions. Every generated candidate lands in EXACTLY ONE of
# these, which is what makes the gate-ledger identity assertable.
REJECT_BUCKETS = (
    "rejected_no_contract",
    "rejected_stale_signal",
    "rejected_dte",
    "rejected_quote_invalid",
    "rejected_spread",
    "rejected_position_cap",
    "rejected_duplicate",
    "rejected_daily_loss",
    "rejected_entry_window",
    "rejected_not_selected",
    "rejected_other",
)

# reason prefix -> ledger bucket
_BUCKET_OF = (
    ("signal_", "rejected_stale_signal"),
    ("dte_", "rejected_dte"),
    ("spread_", "rejected_spread"),
    ("quote_", "rejected_quote_invalid"),
    ("position_cap", "rejected_position_cap"),
    ("duplicate", "rejected_duplicate"),
    ("daily_loss", "rejected_daily_loss"),
    ("entry_window", "rejected_entry_window"),
    ("no_listed_", "rejected_no_contract"),
    ("not_selected", "rejected_not_selected"),
)


def bucket_for(reason):
    for prefix, bucket in _BUCKET_OF:
        if str(reason).startswith(prefix):
            return bucket
    return "rejected_other"


def authorize(intent, ctx, cfg=None):
    """FINAL execution-time authorization, immediately before submission.

    *** ENTRY-ONLY. ***  `intent != ENTRY` returns PASS before a single
    gate input is read. An exit can therefore never be blocked, delayed or
    rejected by anything in this module, no matter what state the market,
    the quote, the clock, the risk supervisor or the configuration is in.
    That is asserted directly, with every input set adversarially, in
    test_wed_stabilization.py.

    `ctx` carries only values the engine has ALREADY computed for the
    trading decision this cycle:

        signal            per-symbol signal dict
        trading_date      today
        expiry            the candidate contract's expiry
        quote             the live quote for the candidate's token
        open_positions    count re-read from the database at this instant
        traded_today      set of symbols already traded this session
        symbol            underlying name
        entry_allowed     safety-supervisor verdict
        in_entry_window   clock check

    Ordering is deliberate: cheap, state-only checks first, so a rejection
    reason names the FIRST thing that was wrong rather than an incidental
    later one.
    """
    if intent != ENTRY:
        return PASS
    c = get_config(cfg)
    if not c["enabled"]:
        return PASS

    if c["dry_run"]:
        return _reject("other_dry_run")
    if not ctx.get("in_entry_window", True):
        return _reject("entry_window_closed")
    if not ctx.get("entry_allowed", True):
        return _reject("daily_loss_or_supervisor_block")
    if ctx.get("symbol") in (ctx.get("traded_today") or ()):
        return _reject("duplicate_symbol_today")
    try:
        open_n = int(ctx.get("open_positions"))
    except (TypeError, ValueError):
        return _reject("position_cap_unknown")
    cap = int(c["max_option_positions"])
    if open_n >= cap:
        return _reject("position_cap(%d>=%d)" % (open_n, cap))

    r = check_signal_freshness(ctx.get("signal"), ctx.get("trading_date"), c,
                               now=ctx.get("now"))
    if not r:
        return r
    r = check_dte(ctx.get("expiry"), ctx.get("trading_date"), c)
    if not r:
        return r
    r = check_quote(ctx.get("quote"))
    if not r:
        return r
    r = check_spread(ctx.get("quote"), c)
    if not r:
        return r
    return PASS


# ------------------------------------------------------------ CHANGE 11 ----
class LedgerIdentityError(AssertionError):
    """The gate ledger did not account for every generated candidate."""


class GateLedger:
    """Per-cycle census of what happened to every generated candidate.

    Section 22: a zero-trade session must be POSITIVELY CONFIRMED, not
    inferred from silence. Silence is indistinguishable from a crash, an
    auth failure, a failed state restore, a scheduler that never fired, or
    a gate that rejects everything because the GATE is broken. The ledger
    plus the cycle heartbeat is what separates those.

    Every generated candidate gets exactly one terminal disposition, so:

        candidates_generated == sum(rejections) + passed_to_selection
        entered              <= passed_to_selection
        entered              <= max_option_positions
    """

    def __init__(self, max_positions=None):
        self.max_positions = max_positions
        self.candidates_generated = 0
        self.passed_to_selection = 0
        self.entered = 0
        self.counts = OrderedDict((b, 0) for b in REJECT_BUCKETS)
        self.reasons = OrderedDict()

    # -- recording ---------------------------------------------------
    def generated(self, n=1):
        self.candidates_generated += n

    def reject(self, reason):
        bucket = bucket_for(reason)
        self.counts[bucket] += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        return bucket

    def passed(self, n=1):
        self.passed_to_selection += n

    def entry(self, n=1):
        self.entered += n

    # -- identities --------------------------------------------------
    @property
    def rejected_total(self):
        return sum(self.counts.values())

    def check(self):
        """Raise LedgerIdentityError unless every identity holds."""
        if self.candidates_generated != (self.rejected_total
                                         + self.passed_to_selection):
            raise LedgerIdentityError(
                "generated=%d != rejections=%d + passed=%d"
                % (self.candidates_generated, self.rejected_total,
                   self.passed_to_selection))
        if self.entered > self.passed_to_selection:
            raise LedgerIdentityError(
                "entered=%d > passed=%d"
                % (self.entered, self.passed_to_selection))
        if (self.max_positions is not None
                and self.entered > int(self.max_positions)):
            raise LedgerIdentityError(
                "entered=%d > max_option_positions=%d"
                % (self.entered, int(self.max_positions)))
        return True

    def as_dict(self):
        d = OrderedDict()
        d["candidates_generated"] = self.candidates_generated
        d.update(self.counts)
        d["passed_to_selection"] = self.passed_to_selection
        d["entered"] = self.entered
        return d

    def summary(self):
        parts = ["generated=%d" % self.candidates_generated]
        parts += ["%s=%d" % (k, v) for k, v in self.counts.items() if v]
        parts += ["passed=%d" % self.passed_to_selection,
                  "entered=%d" % self.entered]
        return " ".join(parts)


# ------------------------------------------------------------- CHANGE 9 ----
POLICY_NAME = "universe_order_v1"


def select_for_entry(candidates, slots):
    """EXPLICIT production selection policy.

    S-44: production selection was an implicit `else` branch of the
    ranking-mode test, so "which candidates may consume the position
    slots" was decided by the order of `symbols` in intraday_config.json
    and was never named, logged or tested.

    This makes the SAME policy explicit and testable. It is deliberately
    NOT a behaviour change: the historical counterfactual measured ranking
    top-N at 26.9% win / -5.23% mean against universe order at 46.8% /
    +0.13% (n=78 / n=111), so replacing the policy today would be an
    unvalidated strategy change dressed up as a fix.

    Returns (selected, dropped) where `dropped` carries a reason, so
    slot-limited candidates are ACCOUNTED FOR in the ledger instead of
    silently vanishing when the entry loop breaks out.
    """
    ordered = list(candidates)
    try:
        n = max(0, int(slots))
    except (TypeError, ValueError):
        n = 0
    return ordered[:n], [(c, "not_selected_slot_limit") for c in ordered[n:]]
