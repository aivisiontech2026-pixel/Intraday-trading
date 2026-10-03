> **PROVENANCE — preserved document**
>
> - Document: Phase 6B design review report (ORIGINAL)
> - Origin: Claude Code session output, extracted exactly as recorded from the local Claude Code session transcript (path withheld), line 14091: assistant message uuid `6332e5c4-55f3-4edb-9377-daf9a11c57ab`, timestamp `2026-10-02T15:32:01.538Z`. It is the only candidate: no repository commit on any branch, and no working-tree file, contains it.
> - Extracted text: 26295 characters, 26522 bytes, sha256 `84a8bedd622c03e044a169aadc259f22c7386daf26ec1a155ec87aa7341e5fb2`
> - Date preserved: 2026-10-03
> - Changes below the marker: none. The text includes the message's one-line opening preamble as recorded.
> - This report is CORRECTED and QUALIFIED by the amendment, errata and E4 check in this directory; read it through INDEX.md.

<!-- END PROVENANCE HEADER -->
Analysis is complete — all evidence gathered read-only, no changes made. Writing up the report now.

# PHASE 6B — FINAL DESIGN REVIEW

**Read-only. No code, configuration, schema, workflow, state, ledger (71) or archive change. No P&L or win rate computed for any threshold. No commits.**

## 1. Executive conclusion

- **Is current trailing logic fully understood?** **Yes**, traced through the code and checked against data. 51 of 52 trailing exits that have a recorded high-water reproduce the formula to within ₹0.01. The one mismatch (Δ₹0.11) is **NOT ESTABLISHED**.
- **Is activation coupled to retracement?** **Yes, in the raw formula.** The stop at activation is `(1+a)(1−r)` × entry. **Since 2026-08-26, though, the production stop is not the raw formula.** A floor at entry × 1.0006 decouples them in the band from +10% to +13.705%.
- **Can the current 10%/12% configuration leave the stop below entry?** **Not under current production** (`stabilization.enabled = true`), because the floor holds an armed stop at entry + 0.06%. **It did** for every trade from 2026-07-25 to 2026-08-25, and **it would again** if stabilization were disabled.
- **Is milestone ratcheting genuinely distinct?** **Yes.** One thing worth knowing: production already contains *one* discrete milestone. At +10% the stop jumps from 0.85 × entry to 1.0006 × entry, and only then does it trail continuously.
- **Is historical data sufficient?** **No — C.** This confirms Phase 6A.
- **Is prospective instrumentation required?** **Yes, materially.**
- **Is collection feasible at the required resolution?** **It depends on one capability that hasn't been verified.** The number of requests is not the problem: the measured worst case is 43 tokens per cycle, which fits in a single 50-token request. **What decides it is resolution.** Minute-level crossings are achievable if Angel serves `ONE_MINUTE` candles for NFO option contracts, and that is **NOT ESTABLISHED**. Within-minute ordering would need tick streaming, which the current batch architecture cannot host.

## 2. Current trailing implementation

| element | location | what the code does |
|---|---|---|
| Constants | [options_trader.py:105-107](backtest-machine/options_trader.py#L105-L107) | `INITIAL_STOP_PCT = -0.15`, `TRAIL_ACTIVATE_PCT = 0.10`, `TRAIL_PCT = 0.12`. These values haven't changed since `88010ac6` (2026-07-25). |
| Initial stop | [options_trader.py:608](backtest-machine/options_trader.py#L608) | `round(entry_px * (1 + INITIAL_STOP_PCT), 2)`, set at entry |
| Mark | [:1374](backtest-machine/options_trader.py#L1374) | `mark = bid if bid > 0 else ltp` |
| High-water | [:1407-1411](backtest-machine/options_trader.py#L1407-L1411) | `max(prior hw or entry, mark)`. It is raised to the quote's session `high` **only if** that high rose above the previous poll's value (`peak_source = INTRA_INTERVAL_HIGH`). |
| Trail level | [:1429-1436](backtest-machine/options_trader.py#L1429-L1436) → [stabilization.py:352-383](backtest-machine/stabilization.py#L352-L383) | `trail_stop_level`: returns `None` if `hw < entry × 1.10`, otherwise `max(hw × 0.88, entry × (1 + 0.06/100))` |
| Stop update | [:1435-1436](backtest-machine/options_trader.py#L1435-L1436) | `stop_price = max(stop_price, _trail)`, so the stop can only rise |
| Trigger | [:1442-1443](backtest-machine/options_trader.py#L1442-L1443) | `trough = day_low if it fell below the previous poll's value, else mark`, then `trigger = min(mark, trough)` |
| Exit + label | [:1476-1504](backtest-machine/options_trader.py#L1476-L1504) | If `trigger <= stop_price`, the trade closes **at `stop_price`** with `price_source = "STOP_LEVEL"`. The reason is `"Trailing stop"` if `hw >= entry × 1.10`, otherwise `"Initial stop"`. |
| Other exits | [:1506-1520](backtest-machine/options_trader.py#L1506-L1520), [:927](backtest-machine/options_trader.py#L927), [:1365](backtest-machine/options_trader.py#L1365) | Trend reversal, expired contract, 15:15 square-off at `mark`, plus two degraded-mode square-offs at the last observed price |
| Persistence | [:1445-1460](backtest-machine/options_trader.py#L1445-L1460), [:825](backtest-machine/options_trader.py#L825) | `options_positions` (hw, stop, day_high_seen, day_low_seen, peak_source), `position_snapshot` every cycle, `exit_snapshot.high_water_at_exit` |
| Quote source | [angelone_client.py:564-631](backtest-machine/angelone_client.py#L564-L631) | `smart.getMarketData("FULL", {NFO: batch})`, 50 tokens per request |

**Two facts the brief didn't anticipate:**

- **`day_low` is used.** It feeds the exit trigger through the same "new session extreme since last poll" logic that `day_high` uses for the high-water.
- **The price basis is mixed.** Polled updates use the **bid**, but an intra-interval update uses the session **traded** high or low.

## 3. Current mathematical behavior

**Raw formula (confirmed in code):**

```
armed iff hw >= entry × (1 + a)
raw stop = hw × (1 − r)
stop_at_activation / entry = (1 + a)(1 − r) = 1.10 × 0.88 = 0.968   (3.2% below entry)
breakeven activation:  (1 + a)(1 − r) = 1  →  a = r / (1 − r) = 0.12 / 0.88 ≈ 13.636%
```

**Effective formula in current production (the floor, since 2026-08-26):**

```
stop = max( hw × 0.88 , entry × 1.0006 )      once armed
raw trail first exceeds the floor at hw > entry × 1.0006 / 0.88 = entry × 1.13705  (+13.705%)

  +10.000% → +13.705%   stop = entry × 1.0006   (the floor binds)
  above +13.705%        stop = hw × 0.88        (the raw trail binds)
```

In 8 of the 52 checked trailing exits, the floor was the level that bound.

**Boundary behaviour for hypothetical activations.** These are arithmetic only: no history, no P&L, no ranking.

```
 a      raw stop/entry   raw vs entry   with current floor
 +5%    0.9240           −7.60%         1.0006
 +7%    0.9416           −5.84%         1.0006
 +8%    0.9504           −4.96%         1.0006
 +10%   0.9680           −3.20%         1.0006
 +12%   0.9856           −1.44%         1.0006
 +15%   1.0120           +1.20%         1.0120
```

These are mathematical properties of the implementation, not recommendations.

**§6 stop-floor classification: B — floored at entry**, precisely entry × (1 + `trail_min_exit_pct`/100) = entry + 0.06%. Three conditions attach to that:

- It applies only when armed and only while `stabilization.enabled = true`.
- Behaviour was **A** (the trail could sit below entry) for trades before 2026-08-26.
- In addition, the stop never decreases (`max`) and never falls below the initial stop, because it starts there and only rises.

## 4. Current state machine

```
ENTRY ─► stop = round(entry × 0.85, 2); hw = entry
  │
  ▼  every cycle (~59 s median; one quote snapshot per held position)
HIGH-WATER UPDATE ─ hw = max(hw, bid-or-ltp); if session high rose since the last poll → hw = session high
  │
  ▼  same cycle
ACTIVATION ─ hw ≥ entry × 1.10 ?   (re-evaluated every cycle; effectively one-way because hw is monotone)
  │ yes                               │ no
  ▼                                   ▼
STOP UPDATE ─ stop = max(stop, max(hw × 0.88, entry × 1.0006))      stop unchanged
  │
  ▼  same cycle
TRIGGER ─ min(bid-or-ltp, session low if it fell since the last poll) ≤ stop ?
  │ yes → EXIT at stop_price, STOP_LEVEL, "Trailing stop" if hw ≥ 1.10 × entry, else "Initial stop"
  │ no  → trend reversal / expired / 15:15 → EXIT at mark (bid-or-ltp)
```

- **Activation, stop movement and exit can all happen within one observation.**
- **The code embeds an intrabar ordering assumption.** Within a cycle it applies the new high first, then tests the new low against the stop that high produced. This is "high before low", and the comment at [:1394-1398](backtest-machine/options_trader.py#L1394-L1398) describes it as such (`183 → 193 → 161`). If the low actually came first, the stop in force at that moment was lower. **The ordering is assumed, not observed.**
- **Gaps are not modelled.** A gap is just a longer interval. Only new session extremes reveal what happened inside it.
- **Timestamps:** decisions use the local cycle time. `exch_feed_time` and `exch_trade_time` are stored in all 20,883 quote snapshots but are never used for decisions.

## 5. Historical data limitations

This confirms Phase 6A on every point, with one correction.

| item | evidence |
|---|---|
| Path coverage | 121 of 255 trades (47.5%). `position_snapshot` starts 2026-08-17. |
| Polling gaps | Median 59.3 s, p99 332.6 s, maximum 991.5 s |
| Interval lows | A new session low was captured in 1.4% of intervals, a new session high in 3.5% |
| Ordering | The assumption is built into production (§4) and cannot be checked from the data |
| Fills | 153 of 255 exits are `STOP_LEVEL`, i.e. modelled fills |
| Expired tokens | Expired contracts are largely unaddressable for later candle backfill |
| Reconstructible subset | 51 trades, of which 50 are losers and 41 never reached +5%. It is selected on outcome. |
| `replay_result` | 0 rows |

**Correction to Phase 6A.** Candidates blocked by capacity **are** labelled, as `not_selected_slot_limit` ([stabilization.py:680](backtest-machine/stabilization.py#L680)). Capacity is applied as the slot count *inside* selection, which is why `rejected_position_cap = 0`. However, that label only reaches the ledger's pooled `rejected_not_selected` bucket (5,720 in total, mixed with ranking drops) and a trace log. **It is persisted per candidate nowhere**: zero rows in `decision` or `candidate_snapshot` match `%slot%`.

**§12 classification: C — INSUFFICIENT.**

## 6. Descriptive milestone-reach distribution

This section is descriptive only. It is not a basis for choosing thresholds and contains no P&L.

**A1. Whole book, from exit return alone.** This is an exact lower bound and covers every trade.

```
+5% 74 (29.0%)  +7% 62 (24.3%)  +8% 59 (23.1%)  +10% 51 (20.0%)  +12% 47 (18.4%)  +15% 39 (15.3%)  +20% 30 (11.8%)   of 255
```

**A2. Whole book, adding the `Trailing stop` label and the recorded `high_water_at_exit`:**

```
+5% 146 (57.3%)  +7% 135 (52.9%)  +8% 129 (50.6%)  +10% 118 (46.3%)  +12% 78 (30.6%)  +15% 60 (23.5%)  +20% 46 (18.0%)   of 255
```

The drop between +10% and +12% is partly an artifact. The label proves +10% and nothing higher.

**B. Live-price subset, same sources:**

```
+5% 127 (57.5%)  +7% 116 (52.5%)  +8% 110 (49.8%)  +10% 99 (44.8%)  +12% 63 (28.5%)  +15% 47 (21.3%)  +20% 35 (15.8%)   of 221
```

**C. Path-observed subset (121 trades; selection-biased, do not extrapolate):**

```
            polled marks only (bid)        persisted high_water (incl. intra-interval)
 +5%     74 = 61.2% path / 33.5% live       79 = 65.3% / 35.7%
 +10%    49 = 40.5% / 22.2%                 56 = 46.3% / 25.3%
 +15%    32 = 26.4% / 14.5%                 37 = 30.6% / 16.7%
 +20%    24 = 19.8% / 10.9%                 27 = 22.3% / 12.2%
```

The other levels are in the run log. Seven trades reach +10% only through intra-interval capture.

**§12A.1 exit-reason lower bound.** The contingency on §5 is **satisfied**. `"Trailing stop"` is written only at [options_trader.py:1500](backtest-machine/options_trader.py#L1500), conditioned on `hw ≥ entry × 1.10`, and `hw` is monotone. The label test was `>` until `25c65b84` (2026-08-06) and `>=` after. Exact-threshold cases before that date were therefore labelled `Initial stop`, which only makes the bound more conservative.

```
"Trailing stop": 96 of 255 = 37.6%   (the expected figure, verified)
live subset:     88 of 221 = 39.8%    model-priced era: 8 of 34
```

**The bound depends on how the high-water was measured, not just on the parameter.** The parameter never changed, but the result shifts when the measurement method did:

```
before 2026-08-06              9 of  51  (17.6%)
2026-08-06 .. 08-25           52 of 121  (43.0%)
2026-08-26 onward             35 of  83  (42.2%)
```

The jump lines up with `25c65b84`, which both changed the label test and introduced intra-interval high capture. That is a sequence, not an established cause.

Limits on what this shows:

- It covers **one level only**, +10%.
- It is **censored**: trades that reached +10% and exited some other way are not counted, so the true rate is higher.
- It cannot be interpolated to any other level.
- **It must not be used to select or rank any threshold**, now or later.

## 7. Milestone-ratchet mechanism

**Definition.** A set of discrete favourable levels M₁ < M₂ < … . When Mₖ is reached, the stop rises to a lock level L(k). Once raised, the stop never falls back. The lock depends only on *which milestones have been reached*, not on how far price ran beyond the last one.

**How it differs from current production:**

- The current mechanism, after its single milestone at +10%, trails continuously at `hw × 0.88`. The stop moves with every new high.
- A ratchet is discrete. It ignores excursion between milestones, its pullback tolerance depends on distance to the lock level rather than a fixed fraction of the peak, and it handles large jumps differently.
- Both are monotone.
- The two mechanisms coincide only in degenerate parameterisations.

## 8. Policy-definition questions (before Phase 6C)

These must all be settled before Phase 6C. None is selected here.

- **A** — milestone spacing
- **B** — lock-in function or offset
- **C** — the initial stop, and whether it interacts with L(k)
- **D** — the monotonic rule
- **E** — the multiple-milestone jump rule
- **F** — same-observation crossing handling
- **G** — gap handling
- **H** — the quote and fill rule (bid, LTP or traded price; `STOP_LEVEL` or observed)
- **I** — which price field counts as reaching a milestone (polled bid, interval high, traded high)
- **J** — interaction with the existing floor, the 12% trail and other exits
- **K** — the evaluation metric, statistical method and multiplicity (ledger 71 + k)

**Gap semantics, §10:**

- **Case A** (100 → 105 → 110): each milestone is observed in sequence, so there is no ambiguity.
- **Case B** (100 → 110) and **Case C** (100 → 120): several milestones are crossed between observations. A rule is required: highest milestone only, all crossed in sequence, or another deterministic rule. If a low also occurred inside the interval: **INTRABAR ORDER NOT IDENTIFIABLE.**
- **Case D** (100 → 110 → 106): the order across observations is known, and the outcome depends only on L(k).
- **Case E** (100 → 112 → 103): identifiable if 112 and 103 are separate observations. If both fall inside one interval, known only as its high and low: **INTRABAR ORDER NOT IDENTIFIABLE.**

## 9. Required prospective fields

| FIELD | REQUIRED? | PURPOSE | SOURCE | FREQUENCY |
|---|---|---|---|---|
| token + exchange | REQUIRED | Join key | instrument master | per record |
| trading symbol, underlying, expiry, strike, CE/PE | REQUIRED | Identity, rollover checks | master or scrip archive | per contract |
| entry ts / price; exit ts / price / reason | REQUIRED | Endpoints | trader | per trade |
| LTP, bid, ask | REQUIRED | Mark and fill basis | `getMarketData FULL` | per poll |
| exchange ts + local receive ts | REQUIRED | Staleness and ordering | FULL response / local clock | per poll |
| **interval high + interval low** | **REQUIRED** | Crossings | **NOT AVAILABLE from snapshots** (session OHLC only); needs minute candles | per minute |
| quote snapshot ID | REQUIRED | Traceability | telemetry | per poll |
| high-water, current stop, initial stop | REQUIRED | Production state | trader | per poll |
| activation state + activation timestamp | REQUIRED | Not persisted today | derived in trader | on transition |
| milestone state | NOT ESTABLISHED | Depends on undefined policy | n/a | n/a |
| position state (open / closed / held-no-quote) | REQUIRED | Gap diagnosis | trader | per poll |
| day high / day low (session) | OPTIONAL | Production replay only; **not** a substitute for interval OHLC | FULL | per poll |
| OI, volume | OPTIONAL | Liquidity context | FULL | per poll |

## 10. Required temporal resolution

**What the evidence shows:**

- The API is REST `getMarketData("FULL")`, batched at 50 tokens ([angelone_client.py:90](backtest-machine/angelone_client.py#L90)). The code describes that cap as documented.
- Polling is one GitHub Actions run per minute, triggered by an external cron. In-process cycle time is a median of 2.7 s; the gap between cycles is a median of 59.2 s and a p99 of 135 s.
- The snapshot carries **session** OHLC, not interval OHLC.
- `SmartWebSocketV2` exists but is unused. [angelone_client.py:27-37](backtest-machine/angelone_client.py#L27-L37) defers streaming as "a separate architectural decision".

**Rate limits:**

- The quote API's per-second limit is **NOT ESTABLISHED**.
- The historical candle API was measured at 1.5 req/s with no throttling, against a documented 3 req/s ([angel_research_io.py:35-36](backtest-machine/angel_research_io.py#L35-L36)).
- **Whether Angel serves `ONE_MINUTE` candles for NFO option tokens is NOT ESTABLISHED.** `angel_oi_probe.py` tests that interval, but its output was never persisted.

**What resolution is needed:**

- **To know *in which minute* a milestone or stop was crossed:** interval high and low per minute, per contract.
- **To know *the order within a minute*:** tick data, which this architecture cannot host.

## 11. Candidate / capacity observability

Each candidate needs to be recorded at every step of the funnel:

- generated
- eligible or ineligible (with gate reason, as today)
- selected or not, with a **persisted** reason that separates `slot_limit` from `ranking` (today they are pooled and only kept in logs)
- open-position count and available slots at decision time
- `HALTED_DAILY_LOSS` state — this blocks entries in 6,330 decision rows
- the contract it would have bought (already present on 6,208 of 6,227 full-book candidate rows)
- its path from first eligibility to the end of the counterfactual window
- the realised outcome if it was selected

## 12. Prospective collection feasibility

**Held positions.** These are already polled every cycle: a median of 2 per cycle, at most 4 historically, and 2 under the cap. Collecting their paths adds no requests.

**Unselected candidates.**

- Measured over 20 sessions: 22 new contracts per session at the median, 35 at p90, 41 at the maximum.
- Even if every one were retained until 15:15, the peak would be 41 tracked contracts plus 2 held = **43 tokens, which is one quote request per cycle.** Request count is **not** the binding constraint at this cadence.
- Headroom is 7 tokens. A day above 48 would need a second request.

**What does bind:**

1. **Snapshots lack interval OHLC.** Polling more candidates does not fix this.
2. **Minute candles (getting interval OHLC)** cost one request per contract per day, because historical candles are not batched. About 45 requests at ≤1.5 req/s is roughly 30 s. **If this ran inside the trader's 1-minute cycle it would multiply the median 2.7 s cycle time**, so it must run outside the trading cycle.
3. The quote API's rate limit and the availability of `ONE_MINUTE` NFO candles are both **NOT ESTABLISHED**.

**Failure modes to plan for:**

- A missed cron firing produces a gap (p99 135 s already).
- No quote means the position is held unmarked ([:1366](backtest-machine/options_trader.py#L1366)).
- Candle backfill for a contract fails once it expires (the token problem).
- `observability.db` (35.4 MB) is committed to `trading-state` on every run. Growth multiplies the bytes pushed per run.

## 13. Data-quality controls

Checks needed for prospective data:

- Duplicate or missing timestamps, and per-contract monotonic ordering
- Exchange time against receive time: staleness, and the sign of the gap
- A stale quote (exchange time unchanged across polls)
- `bid > ask`; prices ≤ 0 or outside the circuit band
- `high < low`; interval high below the mark, or interval low above it
- Implausible jumps flagged, never silently dropped
- Interval high or low missing
- Token-to-identity change; expiry rollover
- API error codes logged per request
- Reconnect or missed-cycle gaps (more than 1.5× the cadence)
- Duplicate quote IDs
- A candidate's token disappearing before the end of its window

## 14. Contract / token continuity

**Identity:** `(exchange, token)` is the primary key. Symbol, underlying, expiry, strike and type are attributes, checked for consistency. First-seen and last-seen come from the scrip archive's `token_registry`, which already keys on `token` with exactly those fields.

**Joins:**

- candidate → quote via `quote_snapshot_id` and `token`
- candidate → position via `token` and entry time
- position → path via `token` within the trade's time window
- position → exit via `token` and exit time
- every one of these → scrip archive via `token`

The archive itself is not modified.

## 15. Storage / performance

**MEASURED:**

- `observability.db` is 35.4 MB across 31 sessions, about 1.14 MB per session for all tables combined
- `position_snapshot` 9,636 rows; `quote_snapshot` 20,883; `candidate_snapshot` 9,700
- 418 cycles per session at the median
- 48 snapshots per held trade at the median
- 14 quotes per never-traded contract at the median

The per-table byte split is **NOT MEASURED** (`dbstat` is unavailable).

**ESTIMATED:**

- Minute-candle backfill: about 45 contracts × up to 375 one-minute bars ≈ 17,000 rows per day. At an assumed 60–100 bytes per row that is roughly 1.0–1.7 MB per day, or about 21–36 MB a month.
- Write load: the trader writes as today; the backfill is one batch after the close.
- Latency: none, if it runs outside the trading cycle.

## 16. Architecture options

These are not ranked.

**OPTION A — MINIMAL.**

- *Fields:* add to the existing per-cycle rows — activation timestamp, initial stop, position state, the raw session high and low before and after each poll, and a persisted per-candidate selection reason with slot count.
- *Resolution:* the current one-minute snapshots.
- *Load:* no extra requests; storage growth negligible.
- *Capability:* fixes capacity attribution and the activation record.
- *Limitation:* **does not fix the central gap.** Interval lows stay unobserved.

**OPTION B — BALANCED.**

- *Fields:* A, plus a separate job after 15:30 that fetches `ONE_MINUTE` `getCandleData` for every held contract and every eligible-but-unselected contract touched that day. These contracts are still listed on the day, so their tokens are addressable.
- *Resolution:* one minute, with interval high **and** low.
- *Load:* about 45 historical requests per day at ≤1.5 req/s, outside market hours, with no effect on the trader.
- *Capability:* identifies which minute each crossing happened in. Within-minute order remains **NOT IDENTIFIABLE**.
- *Limitation:* **depends on `ONE_MINUTE` NFO candles, which are NOT ESTABLISHED**. It is also a new job holding Angel credentials, so it must keep the rule that no job holds both a write credential and Angel credentials.

**OPTION C — HIGH-RESOLUTION.**

- *Fields:* tick streaming over `SmartWebSocketV2` for held and candidate contracts.
- *Capability:* order becomes identifiable at tick level.
- *Feasibility:* **blocked under the current architecture.** It needs a long-lived daemon, which the batch model from external cron to Actions cannot host. Subscription and rate limits are **NOT ESTABLISHED**, and tick storage is **NOT ESTABLISHED**.

## 17. Phase 6C data-readiness criteria

These are criteria for the data, not for the strategy. Before Phase 6C:

1. Every held position has a complete interval-OHLC path from entry to exit. Any gap is flagged, not filled.
2. Every eligible, unselected candidate has a path from first eligibility to the end of the counterfactual window, with a persisted selection reason and slot state.
3. The §13 integrity checks pass, or their failures are documented.
4. Identity joins resolve for all records.
5. The milestone, lock, jump, ordering, gap and fill rules (§8) are pre-registered before any collected data is examined for outcomes.
6. The evaluation metric, statistical method and multiplicity are pre-registered.
7. The `ONE_MINUTE` NFO capability is verified, or Option C is resolved.

No sample size, minimum detectable effect or threshold is set here.

## 18. Calendar review point

**Review date: 2027-01-04.** That is about three months of collection, counted from 2026-10-02.

The review looks at:

- data accumulated
- informative-event coverage
- position-path and candidate-path coverage
- storage, API and write burden
- unresolved data-quality problems
- whether the research question still justifies collection

At that point the operator chooses **A — continue**, **B — modify the design**, or **C — stop and close the direction**.

**Profitability is not the test in either direction.** Negative P&L is not a reason to stop, and failing to reach readiness is not a reason to continue automatically.

## 19. Risks / unresolved questions

- `ONE_MINUTE` NFO candle availability is **NOT ESTABLISHED**. It decides whether Option B is viable.
- The quote API's rate limit is **NOT ESTABLISHED**.
- Production's own "high before low" ordering assumption (§4) is untestable without ticks. That limit also applies to any replay of production.
- The mixed bid / traded-price basis must be resolved in pre-registration (§8 I).
- Growth of `observability.db` adds to the per-minute pushes to `trading-state`.
- The slot-limit reason has been lost per candidate for everything to date.
- The single formula mismatch (Δ₹0.11) is unexplained.
- The workflow header says "every 15 min", but the observed cadence is one minute. The documentation is stale.
- The daily-loss halt, recorded in 6,330 decision rows, suppresses candidates for reasons unrelated to capacity and has to be modelled.

## 20. FINAL DISPOSITION

```
C — MATERIAL INSTRUMENTATION REQUIRED
```

This is a judgement on data and design, not on the strategy. The material work is per-contract interval OHLC for held positions and unselected candidates, plus persisted capacity attribution.

**One API capability that hasn't been verified separates C from D.** If Angel does not serve `ONE_MINUTE` candles for NFO option tokens, the only route left is tick streaming. That is architecture-blocked, and the disposition would become **D**.

**STOP.**