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
| **Spread rejections** | **the dominant bucket — roughly 30–45% of candidates that reach the quote stage** | near-zero rejections (gate not wired) or ~100% (threshold or spread computation wrong) |
| **Trades entered** | **0 to 2** | 3 or more — would mean the cap failed |
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
| `rejected_spread` high **and** `quote_snapshot` shows genuinely wide books | **CORRECT REJECTION** |
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
