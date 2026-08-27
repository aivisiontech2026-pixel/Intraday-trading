# Wednesday 2026-08-26 Stabilization — Merge-Gate Closeout

**Branch:** `stabilization/wed-2026-08-26` · **Build:** `8428c38f` + `700b6c4f` + this closeout
**Baseline:** `e240b45892ba1d67d1da6a8517dc4fbdf9c49d00` (`main`, untouched)

This document exists so the corrections below are recorded somewhere durable
and versioned, rather than living only in a chat transcript where they cannot
be re-cited next week.

**Scope note on "fix at source."** `OPEN-ISSUE-REGISTER.md` (branch
`docs/open-issue-register-v1.1`, commit `6a93e9b3`, v1.2) was checked for all
three premises below. **It carries none of them** — they originate in the
Wednesday package prompt, not the register. So there is nothing to correct in
the register; this file is the source of record for the corrections, and the
register should absorb them at its next documentation-only revision.

---

## HEADLINE — THE CORRECTED COST OF THE OBSERVED BOOK

**Read this before any other number in this file.**

```
gross P&L as booked                                     -Rs.28,312
less measured unpaid exit half-spread on 83 stop fills  -Rs.35,070
less sourced statutory charges                          -Rs.48,302
```

**That is a 71% escalation on the figure this effort has been quoting**, and it
changes the perceived scale of the problem. It is not a new loss; it is the
same book, measured properly for the first time.

**HEALTH WARNING — this is a FLOOR, not a final figure.**

- It **excludes polling-latency slippage entirely.** The `STOP_LEVEL` contract
  assumes an ideal fill at the level; a premium that genuinely gapped through
  the stop fills worse, and that is not measured anywhere.
- The observed exit quotes come from the poll that **detected** the breach, not
  from the unobserved instant it occurred. Spreads at that earlier instant are
  unknown.
- The statutory charges are **declared rates from a published schedule**, not a
  contract note. This is paper trading; there is nothing to reconcile against.

**No historical row was rewritten, and nothing in execution, stops, trail
arming, ranking or selection reads any of these numbers.** Gross P&L as booked
is unchanged and remains the figure the trading engine acts on. See §B2 for
the measurement and §N1 for the rate schedule.

---

## R1 — The §5 stale-signal premise is WRONG

**Package claim.** *"On Monday 2026-08-24 the first NIFTY bar read by the
system was timestamped 2026-08-21 15:25 (the previous Friday's close) …
entries were still authorised against it."*

**Verdict: the first clause is true. The second is false.**

Evidence, from `observability.db` / `signal_snapshot`:

| observed at | bar_ts | age | session_status | authorised anything? |
|---|---|---|---|---|
| 2026-08-24 09:15:55 | 2026-08-21 15:25 | 237,055 s | `STALE_OR_AMBIGUOUS` | **No** — 15 min before the entry window opened |
| 2026-08-24 09:31:00 | 2026-08-24 09:25 | **361 s** | `VALID` | **Yes** — all four entries |

All four Monday entries fired 09:31:05–09:31:08 against a **current-session
bar 361 seconds old**. The stale Friday bar was observed at 09:15:55, before
the 09:30 entry window opened, and authorised nothing.

**Consequence for the record:**

- The freshness gate (CHANGE 1) is **PROPHYLACTIC, NOT REMEDIAL**. It must
  never be described as explaining the 09:31 losses.
- It is still worth keeping: it enforces an invariant nothing else in the
  engine checks, telemetry proves the condition occurs, and it costs nothing
  on healthy data (5,338 of 5,340 in-window snapshots pass at 400 s).
- What actually blocks all four Monday entries is the **DTE gate** — every one
  carried expiry 2026-08-25, DTE 1.

---

## R2 — The trend-reversal count was double-counted

**Package claim.** *"11 historical trend-reversal exits, 0 winners, ≈ −₹22,703
… plus two additional trend-reversal losses on 2026-08-25: HDFCBANK CE
−₹1,267, TCS PE −₹1,991."*

**Verdict: the "plus two" is a double-count. Those two are already inside the
11.** Confirmed directly — `HDFCBANK29SEP26730CE` and `TCS29SEP262280PE` both
appear in the live-price population.

Correct figures:

| population | exits | winners | P&L |
|---|---|---|---|
| live-price (n=138) | 11 | **0** | −₹22,703 |
| **all recorded (n=172)** | **12** | **0** | **−₹23,147** |

The twelfth is `AXISBANK`, entered 2026-07-30, `price_source = NULL` — the
Black-Scholes era, before the live-pricing contract.

**Record the all-trades basis: 12 exits, 0 winners, −₹23,147.**
Trend-reversal semantics remain frozen (§30). CHANGE 15 instruments it.

---

## R3 — Tuesday 2026-08-25 is explained, not unknown

Previously filed as REMAINING UNKNOWN #4. That understated the data.

| reason | n | P&L |
|---|---|---|
| Initial stop | 2 | −₹7,288 |
| Trend reversal exit | 2 | −₹3,259 |
| Square-off 15:15 | 1 | +₹1,575 |
| **total** | **5** | **−₹8,971** |

**Every rupee of Tuesday's loss sits in the two exit buckets the package
explicitly froze** — §9 (initial stop) and §30 (trend reversal). Zero gate
rejections is not a mystery; it is the arithmetic consequence of the scope
decision.

Also recorded: **four of five** entered at 09:31, and **four of five** carried
expiry 2026-09-29 (DTE 35). The fifth (`NIFTY01SEP2624150PE`, 11:20) carried
2026-09-01, DTE 7.

> Minor correction to the review's own figure: it said *three* carried
> 2026-09-29. It is four — HDFCBANK, TCS, RELIANCE and ICICIBANK. The single
> exception is the 11:20 NIFTY trade.

**Reclassified:** *Explained; deliberately out of scope. 100% initial-stop and
trend-reversal loss, both frozen by §9 and §30. CHANGE 3 (post-exit path) and
CHANGE 15 (reversal instrumentation) are the instruments that make it
decidable.*

---

## B2 — §24 idealisation, quantified

**Replaces:** *"Replayed P&L is optimistic by an unmeasured amount."*

83 of 138 exits (60%) are booked at `STOP_LEVEL` — filled **at the stop
price**, with no bid in the fill. For those trades the exit-side half-spread
is **not** inside gross P&L. (For the 51 `LIVE_BID` exits it is, and must not
be counted again — that was the double-count already corrected.)

| basis | figure |
|---|---|
| entry-side spread as a proxy (the review's method) | **₹6,912** |
| **exit-side spread, DIRECTLY OBSERVED, 83 / 83 rows** | **₹6,758** |

**This is better than a proxy.** `close_option()` records the observed market
quote alongside every stop-level fill, per the execution-pricing contract, so
`exit_bid`/`exit_ask` are present on **all 83** rows. The measured figure is
₹6,758; the entry-side proxy overstates it by 2%.

**Direction of the residual bias — the review's reasoning does not survive
measurement, but a smaller version of it does.** The review predicted the proxy
would be an *under*estimate because spreads widen on adverse moves. Measured
ratio observed / proxy = **0.98** — it slightly *over*estimates. However, the
observed quote is taken at the **poll instant that detected the breach**, not
at the instant the stop actually triggered between polls. That instant is
unobserved. So **₹6,758 is a floor, not a ceiling**, and the residual
uncertainty is confined to the intra-poll interval rather than to the whole
quantity.

**Separate from, and additional to, polling-latency slippage.** The
`STOP_LEVEL` contract assumes an ideal fill at the level; a premium that
genuinely gapped through the stop would fill worse still. That is not measured
here and is not included below.

| | |
|---|---|
| gross P&L as booked | −₹28,312 |
| less the unpaid exit-side half-spread on 83 stop fills | **−₹35,070** |
| less modeled statutory charges (see N1) | **−₹48,302** |

---

## B3 — Population A residual row: caveated

The row *"would still have been eligible: 91 trades, +₹8,467, 47.3% win"* is
retained **only** with this caveat attached inline, relabelled:

> **Residual of unblocked trades — NOT a simulated session outcome, NOT a
> forward expectation.**
>
> 1. **In-sample selection.** The 1.0% spread threshold was chosen from this
>    dataset. Filtering the same data by it and reporting the residual is
>    selection, not validation.
> 2. **The counterfactual is invalid.** A blocked entry frees a slot a later
>    candidate might have taken, and that replacement's outcome is not in the
>    data. (This caveat was stated under Population B and must apply equally
>    here.)
> 3. It sits near *"blocked by any gate: −₹36,779"* and invites the reading
>    *"the gates would have turned −₹28k into +₹8k."* **Nothing supports
>    that.** §37's anti-overclaim discipline applies to this row exactly as it
>    applies to a P&L projection.

---

## B4 — Smoke-test scheduling hazard

`.github/workflows/smoke-test.yml` joins the trader's concurrency group
(`intraday-trader`) with `cancel-in-progress: false`. That is **correct for
state safety and hazardous for timing**.

| question | answer |
|---|---|
| Can it run while the trader is mid-cycle? | **Yes — neither is cancelled.** `cancel-in-progress: false` queues rather than cancels. |
| What is the cost? | The **next trader run queues behind the smoke test.** |
| Worst-case delay to the next trader run | One full smoke-test job: dependency install + four suites + one live cycle ≈ **2–4 minutes**, plus GitHub runner acquisition. Against a ~1/min external trigger, that is **one to four skipped trader cycles**. |
| Can it corrupt state? | **No.** No `state_sync.py save` step and no dashboard publish; it restores read-only. |

**Operator rule: run it before 08:45 IST, or the evening before.** It is a dry
run that saves no state, so there is no reason to run it close to the open.
Triggering it near 09:15 delays the session start.

The alternative — giving it its own concurrency group — was **rejected**: it
would let a smoke test and a live cycle run simultaneously against the same
`trading-state` branch. Queueing is the safe failure mode; the fix is
scheduling discipline, not weaker isolation.

---

## N1 — Cost rates, sourced

Neither my original estimate nor the review's was right. Both are superseded by
the published schedule.

**Source:** Angel One, equity options (NSE F&O),
`https://www.angelone.in/exchange-transaction-charges`, retrieved
**2026-08-26**. Recorded verbatim in `stabilization.COST_RATE_SOURCE`.

| component | mine (was) | review | **sourced** |
|---|---|---|---|
| brokerage | ₹20/order | ₹20/order | **₹20 per executed order** |
| STT | 0.100% | 0.150% | **0.15%, sell side, on premium** |
| exchange txn | 0.0503% | 0.03504% | **0.0355299%, buy + sell** |
| SEBI | 0.0001% | — | **₹10 / crore** |
| IPFT | 0.0005% | — | **0.002%** |
| stamp duty | 0.003% buy | 0.003% buy | **0.003%, buy side, on premium** |
| GST | 18% on brokerage+txn+SEBI | 18% | **18% on brokerage + txn + SEBI + IPFT** |

Two corrections to my implementation: the GST base was missing IPFT, and IPFT
itself was a quarter of the published rate.

Recomputed over the full live-price population (n=138):

| | mine (was) | review | **sourced** |
|---|---|---|---|
| brokerage | 5,520 | 5,520 | **5,520** |
| STT | 2,773 | 4,159 | **4,159** |
| exchange | 2,804 | 1,953 | **1,980** |
| SEBI + IPFT | 34 | 33 | **117** |
| stamp duty | 84 | 84 | **84** |
| GST | 1,499 | 1,351 | **1,371** |
| **total** | 12,713 | 13,100 | **13,232** |
| gross | −28,312 | −28,312 | **−28,312** |
| **net (est.)** | −41,025 | −41,412 | **−41,544** |

The sourced STT reproduces the review's figure exactly, confirming it used
0.15%. This is paper trading, so there is no contract note to reconcile
against; these are declared rates and `COST_RATE_SOURCE` says so. Nothing in
execution, stops, trail arming, ranking or selection reads them, and no
historical row is rewritten.

---

## N2 — The 09:31 question stays re-diagnosable

**Nothing changed. Confirmed by inspection of the emitters.**

The finding survives even though its diagnosis did not: 60/138 trades,
−₹23,123, 82% of observed gross loss; 12 of 17 in the four-day window; 4 of 5
on Tuesday. Nothing in this build gates entry timing — the correct scope
decision for today.

For **every** candidate, entered and rejected alike:

| field needed | where it lands | for rejected candidates? |
|---|---|---|
| cycle timestamp | `cycle.process_started_at`, `cycle_heartbeat.observed_at` | yes — one row per cycle, degraded cycles included |
| entry timestamp | `decision.decided_at` (action `ENTRY`), `options_trades.entry_time` | n/a — entered only |
| rejection timestamp | `decision.decided_at` (action `ENTRY_REJECTED`) | yes |
| signal bar timestamp | `candidate_snapshot.signal_bar_ts` | yes — written at the gate |
| signal bar age | `candidate_snapshot.signal_bar_age_s` | yes |
| spread at decision | `candidate_snapshot.spread_pct` | **partial — see below** |
| DTE | `candidate_snapshot.dte` | **partial — see below** |
| gate verdict + reason | `candidate_snapshot.gate_result` / `gate_reason` | yes |
| per-cycle census | `gate_ledger` (11 buckets, identities asserted) | yes |

**Two honest gaps, reported and NOT fixed today** (per instruction):

1. A candidate rejected by the **freshness gate** is dropped before any
   contract lookup, so its row has **no `dte`, no `spread_pct`, no strike and
   no token** — there is no contract yet. The gate reason and the signal bar
   are recorded. This is the correct behaviour (§3.2 forbids doing contract
   work for a rejected candidate); it simply means freshness-rejected rows
   cannot contribute DTE or spread to a timing study.
2. A candidate rejected by the **DTE gate** records its expiry and DTE but not
   `spread_pct`, because quotes are batched after candidate construction.

Everything needed to re-diagnose the 09:31 concentration by entry time, bar
age, spread and DTE **for candidates that reached the quote stage** is
present, on both the entered and rejected sides. That is a strict improvement
on the current state, where rejected candidates left no trace at all.

---

## A1 — Cadence interaction, and the unit the ledger reports in

### A1.0 — The premise needs correcting first

The amendment states: *"The trader evaluates entries roughly five times per
signal bar."* **Measured, that is not what happens**, and the reason is a
precondition already in the engine.

`in_window` requires `signals_fresh`, which is true **only on a cycle that
actually refetched signals**. Candidates are therefore built only on refresh
cycles, never on the four cached cycles between them.

| measurement | value |
|---|---|
| cycles total | 1,823 |
| cycles that refreshed signals | **381 (20.9%)** |
| cycles that produced candidates | 89 |
| **candidate cycles with no signal refresh that cycle** | **0** |
| median cycle gap | 56 s |
| median refresh gap | 302 s |
| NIFTY bars seeing exactly **one** refresh | **322 of 333** |

The 11 bars with more than one refresh are all the **15:25 bar**, re-observed
between 15:25 and 15:56 after the last bar of the day has closed — outside the
09:30–14:30 entry window entirely. One is the 2026-08-21 15:25 bar carried into
Monday 09:15, i.e. R1's stale bar.

**So within the entry window the trader evaluates entries approximately ONCE
per signal bar.** The cadence ratio is real (≈5 cycles per bar), but four of
those five cycles cannot generate a candidate.

### A1.1 — The side effect, corrected and recorded

**Classification: 🟡 INFERRED side effect of CHANGE 5. Not a defect. Sign
unknown.**

The amendment's mechanism — *"enter on whichever cycle in this bar has the
tightest quote"* — **cannot occur**: there is only one quote observation per
bar at which an entry can be authorized.

What CHANGE 5 **can** do is one bar-granular thing:

> A candidate rejected for spread on bar *N* is dropped. If its direction
> persists into bar *N+1*, it is re-considered there against a **new** quote.
> The gate can therefore **defer an entry by one or more bars** rather than
> preventing it.

That is still a behaviour change and it is still unremarked, so it is recorded
here. It is **not** intra-bar quote shopping, it operates at ~5-minute
granularity, and it is bounded by the entry window closing at 14:30. Whether
deferring into a later bar is better or worse than entering on the first bar is
**unknown**, and this build does not attempt to find out.

It remains true, and is the substance of the amendment's point, that **CHANGE 5
is the first entry condition that varies between successive evaluations of a
persisting signal.** Direction, DTE and position count are stable across bars
while a trend holds; quoted spread is not.

### A1.2 — UNIT DISCIPLINE (extension of §36 population reconciliation)

**The gate ledger counts CANDIDATE EVALUATIONS. It does not count TRADE
OPPORTUNITIES, and it does not count TRADES.**

Three units, never to be compared, summed, or converted into one another
without naming which is in use:

| unit | what it is | scale per session |
|---|---|---|
| **cycle** | one trader run; one `cycle_heartbeat` and one `gate_ledger` row | ~315 |
| **candidate evaluation** | one `candidate_snapshot` row: one symbol, on one refresh cycle | ~92 median, 412 max observed |
| **trade** | one `options_trades` row | 4–5 |

A rate quoted in one unit is meaningless in another. The 43/138 spread figure
in the historical validation is **per trade taken**; the ledger's
`rejected_spread` will be **per candidate evaluation**. They differ by more
than a factor of two, in a direction that is not obvious — see A1.3.

### A1.3 — Amended spread-rejection prediction, in the ledger's own unit

**My earlier prediction of "~30–45% of candidates that reach the quote stage"
was wrong.** It was calibrated on 43/138 entered trades — a biased subset,
because universe-order selection fills its slots from the front of the list,
where the index options are.

Measured over all 922 recorded candidate evaluations, joined to the quote each
was scored against:

| | |
|---|---|
| candidate evaluations with a linked quote | 922 |
| quote-invalid | 0 |
| **spread > 1.0%** | **627 / 922 = 68.0%** |
| median candidate spread | **1.290%** |
| p75 / p90 | 1.791% / 2.941% |

**Amended prediction — per candidate evaluation: `rejected_spread` ≈ 60–75% of
candidates that reach the quote stage.**

**Per opportunity: the same number.** A1.4 shows evaluations and opportunities
are 1:1 in this system, so no separate per-opportunity prediction is needed —
which is itself the useful finding.

**Per trade attempted: ~30%**, the historical 43/138 basis. Retained only to
show the two must not be confused.

**A consequence worth predicting, because it is falsifiable:**

| | candidates | median spread | pass ≤1.0% |
|---|---|---|---|
| NIFTY + BANKNIFTY | 47 | 0.27% / 0.32% | **100%** |
| all stock options | 875 | — | **28.3%** |
| NTPC, POWERGRID | 102 | 3.02% / 3.31% | **0%** |

The index options are first in universe order and always pass; most stock
options usually do not. **Prediction: whenever NIFTY and BANKNIFTY both carry a
direction, they will consume both slots**, and Wednesday's book will skew
heavily toward index options. If Wednesday enters two stock options while both
indices carried a direction, that prediction is falsified and selection needs
re-examining. 🟡 INFERRED.

### A1.3b — The consequence of that skew (A4.1)

**🟡 INFERRED. Not a defect. NOT grounds for a change today.**

The prediction above states the *incidence*. This is the *consequence*, and it
is uncomfortable: the skew concentrates the book into the historically
**worst-performing** subset.

| live-price population (n=138) | n | P&L | win |
|---|---|---|---|
| **index** (NIFTY, BANKNIFTY) | **29** | **−₹36,502** | **24.1%** |
| stock | 109 | +₹8,190 | 46.8% |

Applying the 1.0% spread gate to entered trades carrying a usable entry quote
(5 excluded for want of one):

| | n | P&L | win |
|---|---|---|---|
| **index survivors** | **29** | **−₹36,502** | **24.1%** |
| stock survivors | 66 | +₹35,066 | 54.5% |

Every index trade survives the gate. Two thirds of stock trades do not.

**The structural chain:**

```
indices sit first in universe order
  -> they clear the spread gate 100% of the time  (median 0.28%)
  -> stocks clear it 28% of the time              (median 1.33%)
  -> MAX_OPTION_POSITIONS = 2
  -> the book is structurally biased toward the two symbols that lost
     Rs.36,502 at a 24% win rate
```

**Two things follow, both for the record only:**

1. **CHANGE 5 is a liquidity filter that is also, unintentionally, a
   symbol-class filter.** Its intended effect (exclude illiquid options) and
   its actual incidence (concentrate the book in two index symbols) are
   different things. The second was not a design decision and was not
   anticipated when the gate was specified.
2. **The 2-position cap may deliver LESS diversification than the 4-position
   book it replaced.** NIFTY and BANKNIFTY are highly correlated and a
   directional signal will usually put both on the same side. Two correlated
   index positions is a different risk shape from four positions spread across
   stocks — arguably a more concentrated one, which is the opposite of the
   cap's intent. The cap still bounds *rupee* exposure at ₹50,000; it does not
   bound *correlation* exposure, and nothing in this build does.

#### Counter-evidence, stated honestly

**The sample is thin.** 7 wins in 29. Against the book-average 42.0% win rate,
`P(X ≤ 7 | n=29, p=0.42) = 0.036` one-tailed. **Suggestive, not established** —
and it is one test chosen after looking at the data, so the nominal p-value
overstates the evidence.

**The threshold is in-sample.** 1.0% was selected on this same dataset (§B3).
Filtering it by that threshold and reporting the survivors repeats the
selection problem §B3 already flags.

**Confounds — what the data can and cannot rule out:**

| confound | verdict | evidence |
|---|---|---|
| **Notional** | **RULED OUT** | index median ₹21,424 vs stock ₹20,610 — 4% apart, both bounded by `MAX_PER_TRADE` ₹25,000 |
| **Day / session mix** | **RULED OUT** | 16 of 16 index sessions also carry stock trades. On those *same 16 sessions*: index −₹36,502, stock +₹10,529. Not different market regimes. |
| **Entry time (09:31)** | **MOSTLY RULED OUT** | index is 66% 09:31 vs stock 38%, so the concern was real. But the gap survives the split: index non-09:31 n=10, −₹18,542, 20.0% win; stock non-09:31 n=68, +₹13,354, 48.5%. **n=10 is very thin**, so this is weakened, not eliminated. |
| **DTE** | **NOT RULED OUT** | index median DTE 7 vs stock 13. DTE is itself an unresolved association (S-21) and cannot be used to control for anything. |
| **Intraday correlation / regime** | **NOT RULED OUT** | two correlated indices on the same side of the same move is one bet recorded as two. Nothing in the data separates that from a symbol-class effect. |

**Conclusion for the record:** the association is real in this sample and
survives the two confounds that could be tested, but n=29 (n=10 outside 09:31)
and an in-sample threshold are not a basis for an index-specific control.
**No change is made today.** A4.3 states in advance what evidence would be
required.

### A1.4 — Is per-opportunity derivable on Thursday? **YES. Nothing missing.**

`(symbol, bar_ts)` is sufficient, and the collapse is a no-op:

| check | result |
|---|---|
| candidate rows resolvable to a `bar_ts` | **922 / 922 = 100%** |
| `(symbol, bar_ts)` groups holding more than one evaluation | **0** |

**Evaluations and opportunities are already 1:1** across the entire recorded
history — the direct consequence of A1.0.

One wrinkle, stated rather than fixed:

- A **freshness-rejected** candidate carries `signal_bar_ts` on its own row.
- A **DTE-rejected** candidate does **not** — that emit passes the expiry but
  not the bar stamp.
- Both are recoverable by joining `candidate_snapshot.cycle_id` +
  `symbol` → `signal_snapshot.bar_ts`. That join resolves for **100%** of rows,
  guaranteed by A1.0: candidates exist only on refresh cycles, and a refresh
  cycle always writes `signal_snapshot`.

So the collapse works today for every rejection bucket. **Nothing is missing
and nothing was added.**

---

## A3 — OPERATOR ACTIONS

### Know this before the session opens

> **If both index symbols carry a direction, both slots will likely fill with
> NIFTY and BANKNIFTY — historically the worst-performing subset of the book
> (n=29, −₹36,502, 24% win). This is an expected consequence of the spread
> gate's incidence, not a defect, and no change is being made today.**

You are being told now so you meet it in the ledger rather than discovering it
in the P&L. If Wednesday's book is index-heavy and loses, that is the
**predicted** outcome of a deliberate scope decision, not evidence that the
build failed — and per the pre-registration below, **no P&L outcome changes the
assessment**. See §A1.3b for the evidence and its limits, and §A4.3 for the
standard that would have to be met before an index-specific control could be
justified.

### Actions

1. **Push the branch and run the smoke test — BEFORE 08:45 IST** (see B4; near
   09:15 it delays the session by one to four trader cycles):
   ```
   git push -u origin stabilization/wed-2026-08-26
   gh workflow run smoke-test.yml --ref stabilization/wed-2026-08-26
   ```
   `SECTION 34: satisfied` clears criterion N, the only open item.

2. **Rotate the Telegram bot token via BotFather.** Still outstanding. The only
   real remedy for U-017; repository-side remediation is already complete.

3. **Approve the temporary policy values:** `min_dte=2`,
   `max_entry_spread_pct=1.0`, `max_option_positions=2`,
   `max_signal_bar_age_s=400`.

4. **Absorb R1-R3 into `OPEN-ISSUE-REGISTER.md` at the next
   documentation-only revision.**

   *Open loop, recorded here because nothing else forces it.* The register
   (branch `docs/open-issue-register-v1.1`, `6a93e9b3`, v1.2) never carried
   the three wrong premises, so there was nothing to correct and the docs
   branch was deliberately not touched. But after this branch merges the
   register still will not contain the corrections, and R1's stale-signal
   premise in particular is the kind of claim that gets re-cited. It needs a
   separately-authorized documentation-only commit.

---

## WEDNESDAY SUCCESS CRITERIA — PRE-REGISTERED

*Written 2026-08-26 before market open. Committed so the result cannot be
rationalised after the fact.*

### 1. What counts as success

Success is **three observability facts, none of which is P&L**:

1. **Complete cycle heartbeat** — one `cycle_heartbeat` row per trader run for
   the whole session, with no gap.
2. **Gate ledger identities balance** on every cycle:
   `candidates_generated == Σ(rejections) + passed_to_selection`,
   `entered ≤ passed_to_selection`, `entered ≤ 2`.
3. **`post_exit_path` non-empty** — if and only if at least one position was
   opened and closed. If nothing traded, this criterion is **not applicable**,
   not failed.

**P&L is explicitly NOT a success criterion.** A profitable Wednesday does not
validate this build and a losing Wednesday does not invalidate it. The
four-week result is ~0.7σ from zero (per-trade t ≈ −0.88, p ≈ 0.38); one
session cannot move that. **No P&L outcome, good or bad, changes the
assessment.**

**Zero trades is a PASS**, provided 1 and 2 hold.

### 2. Falsifiable predictions

| quantity | prediction | what would falsify it |
|---|---|---|
| **DTE rejections** | **exactly 0** | any non-zero `rejected_dte`. Nearest index expiry is Tue 2026-09-01 (DTE 6); stock expiry 2026-09-29 (DTE 34). Both clear the floor of 2. |
| **Freshness rejections** | **0, or a small number confined to the 09:30–09:35 cycles** | any rejection after 10:00 on a day with no feed outage |
| **Spread rejections** *(unit: candidate evaluations — see A1.2)* | **the dominant bucket — 60–75% of candidates reaching the quote stage.** Amended from "30–45%": that was a per-trade figure. Per opportunity is the same number (A1.4). | below ~40% or above ~90%. Near-zero = gate not wired; ~100% = threshold or spread arithmetic wrong |
| **Book composition** | **index-skewed.** If NIFTY and BANKNIFTY both carry a direction, they take both slots (100% historical pass rate, first in universe order) | two stock options entered while both indices carried a direction |
| **Trades entered** | **0 to 2** | 3 or more — would mean the cap failed |
| **Candidate evaluations** | ~90–400 for the session, on ~60 refresh cycles inside the entry window — **not** ~315, and **not** 4 | evaluations on cycles that did not refresh signals (A1.0 says zero) |
| **Peak concurrent positions** | **≤ 2** | 3 or more |
| **Max deployed capital** | **≤ ₹50,000** | anything above |
| **Trailing exits below entry** | **0** | any trailing exit filling below entry × 1.0006 |
| **Positions carried overnight** | **0** | any open position after 15:15 |

### 3. Defect vs. correct rejection — decidable from the ledger alone

| symptom in the ledger | reading |
|---|---|
| `candidates_generated = 0` on cycles inside the entry window, with `signals_ok=1` and `master_ok=1` | **DEFECT** — candidate construction is broken, not a gate |
| `rejected_stale_signal` ≈ every candidate, all session | **DEFECT** — bar-age computation or timezone handling, not a stale feed. Cross-check `heartbeat.signals_ok` and `signal_snapshot.bar_age_s`. |
| `rejected_dte > 0` on Wednesday | **DEFECT** — prediction 2 falsified; expiry resolution is wrong |
| `rejected_spread` high **and** `quote_snapshot` shows genuinely wide books | **CORRECT REJECTION** — 68% is the expected rate, not an alarm |
| a rate compared across units (evaluations vs trades vs cycles) | **ANALYSIS ERROR, not a system defect.** Name the unit before drawing any conclusion (A1.2) |
| `rejected_spread` high **and** `quote_snapshot` shows tight books | **DEFECT** — spread arithmetic |
| `rejected_quote_invalid` high with `quotes_fetched > 0` | **DEFECT** — quote parsing |
| `identities_ok = 0` on any cycle | **DEFECT — unconditional.** A candidate went unaccounted; the ledger cannot be trusted for any other conclusion that day. |
| heartbeat gap | **DEFECT** — crash, auth failure, failed restore, or the scheduler not firing. Distinguish via the Actions run list. |
| `entered > 0` while `passed_to_selection = 0` | **DEFECT** — an entry bypassed authorization. Highest severity. |

The distinguishing principle: **a correct rejection names a market condition
and is corroborated by the snapshot it was computed from; a defect is a
rejection the raw data does not support, or an accounting identity that fails.**

### 4. Thursday's first analysis, named in advance

**Table:** `post_exit_path`, joined to `exit_snapshot` on `token` +
`exited_at`.

**Question:** *For each initial-stop exit, did the option's premium recover
above the stop level within 60 minutes, and did the underlying continue in the
signalled direction?*

**Decision it feeds:** whether PQ-1 (the −15% initial stop, ~0.55σ of
underlying movement, 83% of stops inside 1σ) is a **stop-width** problem or a
**direction** problem. Those need opposite remedies, and today there is no
evidence to choose between them.

- Premium recovers **and** underlying continued → stop too tight → widen-stop
  experiment is justified.
- Premium recovers, underlying did **not** → option noise / spread → an
  execution problem, not a stop problem.
- Neither recovers → the stop was correct and the entry was wrong → the
  question moves to signal quality, not the stop.

**If Wednesday trades nothing, this analysis does not run, and that is an
acceptable outcome** — the instrument is in place and accumulates from the
next session that trades. It will not be substituted with a P&L review.

### 5. Thursday's SECONDARY analysis (A4.3) — named now

The primary above stays primary. This is one additional question, no more.

**Tables:** `candidate_snapshot` (all rows, entered and rejected) joined to
`quote_snapshot` via `quote_snapshot_id`, and to `decision` via
`candidate_id`.

**Question:** *Of the candidates that passed the spread gate, what fraction
were index symbols — and how did index and stock entries perform separately?*

**Pre-stated interpretations.** Written before the data exists, so the reading
is fixed in advance:

| outcome | reading | what follows |
|---|---|---|
| Index share of gate survivors is **high (≳50%)** and index entries **lose** | The A1.3b chain reproduced out of sample, once | **One** out-of-sample observation added to n=29. Nothing else. One session is not evidence. |
| Index share **high** and index entries **win** | Incidence confirmed, consequence not | A1.3b weakens. Record and continue; do not celebrate a single session either. |
| Index share **low (≲25%)** | The A1.3 incidence prediction is **falsified** | Re-examine the spread measurement and the selection order before anything else. A falsified incidence invalidates the consequence too. |
| No trades, or no index candidates | **Not applicable.** Not a failure, not support | Wait for the next session that trades. |

**What would have to be true before an index-specific control could be
justified.** Setting this standard now, not after a bad day:

1. **Out-of-sample evidence.** The association must hold on sessions *after*
   2026-08-26 — data that did not exist when the 1.0% threshold was chosen.
   The current n=29 is entirely in-sample and cannot be reused.
2. **n ≥ 60 index trades**, roughly double today's, with the effect persisting.
   At the observed gap that is around the point where one test survives a
   correction for having gone looking.
3. **The DTE confound resolved or controlled.** Index median DTE 7 vs stock 13
   (§A1.3b). While S-21 is unresolved this may be a DTE effect wearing a
   symbol-class costume.
4. **The correlation question separated from the symbol-class question.** Two
   correlated indices on the same side of one move is *one bet recorded as
   two*. If that is the mechanism, the remedy is a correlation or
   same-direction constraint, **not** an index exclusion — different defect,
   different fix.
5. **A stated hypothesis about WHY**, testable independently of P&L. "Index
   options lost money" is an observation, not a mechanism.

Until all five hold, the finding stays 🟡 INFERRED and no control is built.
**A bad Wednesday does not lower this bar** — that is precisely what
pre-registering it is for.
