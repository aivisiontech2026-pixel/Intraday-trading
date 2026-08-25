# OPEN-ISSUE-REGISTER

**Version:** 1.2
**Production baseline:** `baseline-v1.9-s34b-refresh-throttle-2026-08-22`
**HEAD / origin/main:** `e240b45892ba1d67d1da6a8517dc4fbdf9c49d00`
**Status:** AUTHORITATIVE working inventory. Documentation only — this file authorizes nothing.

---

## 0. How to use this register

Future analysis **starts here**, not from chat history or memory.

- A new issue → **ADD** it with an ID (`UNREGISTERED-NNN` / `U-NNN` if it has none).
- Evidence changes → **UPDATE** the row; never overwrite the evidence class silently.
- Issue fixed → **move it through the lifecycle**; never jump straight to CLOSED.
- Issue superseded or duplicated → **MARK it and keep the row**. Never delete.
- Identifier reused for a different problem → **separate rows** (see `S-03h` / `S-03r`).

**Evidence classes:** VERIFIED (direct code/test/repo/production evidence) · INFERRED (strongly supported, not directly observed) · UNKNOWN (insufficient evidence — never invent a problem statement).

**Lifecycle states:** `CLOSED — PRODUCTION VERIFIED` · `IMPLEMENTED/FROZEN — NOT PRODUCTION OBSERVED` · `OPEN — ROOT CAUSE VERIFIED` · `OPEN — REMEDIATION CONTRAINDICATED` · `OPEN — POLICY/STRATEGY DECISION` · `OPEN — ASSOCIATION VERIFIED, CAUSATION NOT PROVEN` · `OPEN — MORE EVIDENCE REQUIRED` · `UNKNOWN` · `SUPERSEDED` · `DUPLICATE`

### The rule that matters most

> **CLOSED requires production evidence.**
> Code existing, tests passing, a design approved, a commit made, a tag created, or a branch merged are **none of them** sufficient. `IMPLEMENTED / FROZEN / NOT PRODUCTION OBSERVED` is a distinct state and is **not** CLOSED.

### The rule added in v1.2

> **A verified defect does not imply an authorized fix, and a tested fix that performs worse must be recorded as CONTRAINDICATED — not quietly dropped and not re-proposed later.**
> See S-44. Both of these remain simultaneously true: the selector architecture is defective, **and** the only tested replacement was materially worse.

### What must never be converted

test evidence → production evidence · historical behaviour → future behaviour · projection → observation · inference → fact · association → causation.

---

## 1. Reconciliation totals (v1.2)

| | v1.1 | v1.2 |
|---|---|---|
| Raw candidates reconciled | 76 | **77** (S-44 added) |
| Unreconciled | 0 | **0** |
| Closed — production verified | 21 | **22** (P0-B promoted) |
| Implemented/frozen — not production observed | 4 | **3** (P0-B left) |
| Open — root cause verified | 7 | **8** (S-44 added) |
| Open — policy/strategy decision | 4 (+5 unregistered) | 4 (+5) |
| Open — more evidence required | 5 | 5 |
| Unknown | 16 | 16 |
| Superseded | 1 | 1 |
| Duplicate | 4 | 4 |
| Unregistered issues | 18 | 18 |

---

## 2. MONDAY 2026-08-24 PRODUCTION EVIDENCE (preserved verbatim)

**VERIFIED.** Monday executed baseline `e240b458`. **315 of 315 cycles carried `code_sha = e240b45892ba1d67d1da6a8517dc4fbdf9c49d00`**; all 834 prior recorded cycles carried NULL. This is the first production evidence any post-v1.5 change has ever produced.

**Ground truth — the two loss figures are DISTINCT and must not be conflated:**

- **Rs.10,133** — options-book arithmetic from `options_trades` (4 trades).
- **Rs.10,908** — full reported day total, **including** the four stock-book losses.

| Contract | Universe pos | Ranking rank | Score | Entry | Result |
|---|---|---|---|---|---|
| NIFTY 24300 CE | 1 | 7 | 0.6222 | 72.90 | −14.99% Initial stop |
| BANKNIFTY 57800 CE | 2 | 8 | 0.6175 | 214.70 | −15.00% Initial stop |
| RELIANCE 1320 CE | 3 | 10 | 0.6161 | 2.75 | −0.80% Trailing stop |
| HDFCBANK 730 CE | 4 | 12 | 0.5728 | 4.45 | −15.06% Initial stop |

4 option trades, 4 stock trades, **0 wins**. All four options entered 09:31:05–09:31:08 in a single cycle. All DTE=1.

**Market state at entry:** 14 BULL, 2 BEAR, 4 None; all 20 signals `bar_status=COMPLETED`.

---

## 3. CLOSED — PRODUCTION VERIFIED (22)

| ID | Issue | Commit | Production evidence |
|---|---|---|---|
| **P0-B** | **Runtime provenance (`code_sha`)** | `eff0b7bb` / v1.6 | **315/315 Monday cycles stamped `e240b458` — PROMOTED in v1.2** |
| S-02 | EMA `adjust=False` parity with backtest | `a549d3cb` | Ran Aug-18 to Aug-21 |
| **S-03h** | *(historical meaning)* Single authoritative config contract | `a549d3cb` | Ran |
| S-04 / S-06 | Per-book Rs.2,000 latching daily-loss supervisor | `a549d3cb` | Latched Aug-21 14:19:40 |
| S-05 | Entry-side dependencies no longer gate exits | `a549d3cb` | 4h12m master outage Aug-21; square-off still executed |
| S-09 | Completed-bar signal boundary | `94ec220e` | FORMING-bar disagreement 4.7% to 0.0% (0/968) |
| S-25 / S-26 | Shadow-path integrity; dead ranking feature | `a549d3cb` | Ran, `ranking.mode=shadow` |
| S-28 | Stock entry window bounded at 14:30 | `a549d3cb` | Aug-21 entries 09:31:43 |
| S-29 | Gross/net P&L reconciliation in reporting | `a549d3cb` | Ran |
| O-1 | Telemetry persisted per cycle, not EOD | `41f5d342` | 208 cycles recorded Aug-21 |
| P0-D / F-1 | Quote age unparsed (Angel One timestamp format) | `41f5d342` | Verified |
| P0-D / F-2 | `candidate_snapshot` had no production call site | `41f5d342` | Verified |
| P0-D / F-3 | Daily-loss replay tie ordering not a total order | `41f5d342` | `rowid` tiebreak, `safety_supervisor.py:116` |
| P0-E / T-1 | Telemetry simulation context | `c3e12215` | Ran |
| P0-E / T-2 | Telemetry explicit `init()` | `c3e12215` | Ran |
| P0-W | ENTRY_GATE zero-candidate ambiguity | `9ed1f910` | 208 gate rows Aug-21 |
| V2-R1, V2-R2, V2-R3, V2-R4, V2-R5 | Five analytics reporting defects | `eccdab10` | Ran |

> **Identifier reuse — do not merge.** `S-03h` (config contract) is **CLOSED**. `S-03r` (risk envelope) is **OPEN** — section 6.

---

## 4. IMPLEMENTED / FROZEN — NOT PRODUCTION OBSERVED (3)

**Still NOT closed.** Monday's instrument-master path resolved normally and **did not exercise** the fallback, deadline or throttle branches. Running successfully is not the same as exercising the code.

| ID | Commit / tag | Tests | Production evidence | Closure condition |
|---|---|---|---|---|
| S-33 | `6f67b4c1` / v1.7 | 76 | NONE | A stale-fallback branch observed |
| S-34a | `cf40e99f` / v1.8 | 56 | NONE | A bounded refresh (deadline) observed |
| S-34b | `e240b458` / v1.9 | 98 | NONE | A throttle decision observed |

---

## 5. OPEN — ROOT CAUSE VERIFIED (8)

| ID | Root cause | Evidence | Isolable | Change ID |
|---|---|---|---|---|
| **S-44** | **Live selection by candidate/universe order — see section 5.1** | VERIFIED (code + Monday) | Yes | **REMEDIATION CONTRAINDICATED** |
| U-001 | Ranking calls unguarded at `options_trader.py:955-963`; position loop at `:966`; `main()` has no try/except | VERIFIED (ordering); all raise sites guarded | Yes | S-42 |
| Poisoned cache key | Daily cache key created by the day's first run regardless of download success; keys immutable | VERIFIED (+2-byte artifact) | Yes (workflow-only) | S-37 |
| S-20 | `emit_post_exit` has no production call site; `post_exit_path` = 0 rows | VERIFIED (grep) | Yes | S-38 |
| S-30 | `generate_dashboard.py:377` minute-resolution stamp; publish skip rarely fires | VERIFIED (315 cycles → 288 commits) | Yes | S-39 |
| S-36 | `research_signal_quality.py:48-55` pre-S-02 EMA + S-01 VWAP; `ranking_engine.py:19` cites it for production weights | VERIFIED | Yes | S-40 |
| U-002 | `observability.db` not in `.gitignore` | VERIFIED | Yes | S-39 |
| U-020 | Workflow has no `timeout-minutes`; GitHub default 360 min applies | VERIFIED | Yes | S-43 |

### 5.1 S-44 — LIVE SELECTION BY CONFIG/UNIVERSE ORDER

**Lifecycle: `OPEN — ROOT CAUSE VERIFIED` + `REMEDIATION CONTRAINDICATED / STRATEGY DESIGN REQUIRED`.**
**S-44 IS NOT FIXED. S-44 MUST NOT BE CLOSED.**

**The defect (VERIFIED).** `ranking.mode = shadow`. `ranking.rank()` and `ranking.select()` both run and compute `picks`. At `options_trader.py:1187-1190`:

```
if rank_mode == "active" and ranked:  entry_list = picks
else:                                 entry_list = candidates
```

`candidates` is built by iterating `data.items()`, which preserves `UNIVERSE` order. With `MAX_POSITIONS = 4`, **the first four qualifying candidates in universe order consume every slot.** Ranking observes; it does not select.

This explains the long-standing observation that the same early-universe instruments repeatedly receive positions.

**Monday illustration (VERIFIED).** Executed NIFTY/BANKNIFTY/RELIANCE/HDFCBANK = universe positions 1,2,3,4 = ranking ranks **7, 8, 10, 12**. Ranking's `would_trade=1` set was INFY, TCS, **MARUTI PE** — entirely disjoint.

**Two facts that are simultaneously true and must both be preserved:**

- **FACT 1 (VERIFIED):** the selector architecture is defective and informationally blind.
- **FACT 2 (REFUTED as a remedy):** the existing ranking implementation is **not** a proven better replacement.

Neither of the following may be written into this register: *"ranking would have saved Monday"* (unsupported), or *"there is no selection defect"* (false).

---

## 6. S-44 HISTORICAL COUNTERFACTUAL (preserved evidence)

Aug-04 → Aug-18 replay. Identical entry timing, −15% initial stop, +10% arm / 12% trail, 15:15 square-off. **Only the selector varied.**

| Selector | n | win rate | mean return | median | initial-stop rate |
|---|---|---|---|---|---|
| **ACTUAL — universe order** | **111** | **46.8%** | **+0.13%** | −1.35% | **19%** |
| **COUNTERFACTUAL — ranking top-N** | **78** | **26.9%** | **−5.23%** | −8.93% | **45%** |

- Ranking counterfactual was **worse on 9 of 11 reconstructed days**.
- Second variant using ranking's own `would_trade` set (n=25): **−5.00%** versus **+0.13%** actual.
- Methodology note: the counterfactual samples prices every 5–7 minutes versus production's ~1 minute, so it **under-detects** stop hits — a bias **in ranking's favour**. It underperformed regardless.

**Therefore: remediating S-44 by activating ranking is CONTRAINDICATED by current evidence.**

### 6.1 INFY counterexample (Monday) — VERIFIED

INFY was ranking's **#1** candidate (score 0.835). Its `ranking_log` quote moved **10.30 → 7.70 in under five minutes** (09:31:04 → 09:35:54), which would have triggered approximately the −15% initial stop almost immediately — a **larger** loss than three of the four contracts actually traded.

This finding exists specifically to prevent hindsight-driven strategy change. It is preserved as a guard against re-proposing ranking activation from intuition.

### 6.2 Counterfactual limitation — TELEMETRY GAP, not a data absence

- **Aug-04 → Aug-18:** counterfactual substantially reconstructed via `ranking_log.quote` (available from Aug-04 onward).
- **Aug-19, Aug-20, Aug-21, Aug-24:** **incomplete**. Ranking/candidate observation collapses once `MAX_POSITIONS` fills — ranking cycles per day fall to 5 / 7 / 7 / **3**.
- **Monday's full counterfactual is therefore unavailable beyond the observable first minutes.**

This is a **TELEMETRY / REPLAY GAP**, not evidence that historical data does not exist. It is the precise requirement that rescopes S-41 (section 11).

### 6.3 Ranking performance characteristics — preserved

n ≈ 128 scored live trades · Spearman ≈ **+0.158 / +0.159** · PF ≈ **0.88** · expectancy ≈ **−Rs.133**. Score quartiles showed a monotonic relationship (Q1 −Rs.24,042 → Q4 +Rs.24,447), **but this is conditioned on execution and does not establish suitability as the live selector.**

**Correct classification:** ranking has measurable signal characteristics but **insufficient evidence to replace the current selector**. Do not claim it is useless. Do not claim it is profitable.

---

## 7. OPEN — POLICY / STRATEGY DECISION (4)

| ID | Verified engineering fact | Policy question | Must NOT be assumed |
|---|---|---|---|
| **S-01** | Documented contract is a session VWAP; production and research use a whole-window close-weighted scalar. Measured: **~30.60% direction changes, 0 CE/PE reversals, ~+9.6% signals** | Is the contract or the implementation authoritative? | **That zero CE/PE reversals proves zero selection impact.** Under universe-order selection, changing *which* symbols carry a direction changes *which* land in the four slots. Remains unresolved / evidence-gated |
| **PQ-1** | See section 9 | Premium / underlying / delta / ATR / hybrid / absolute-rupee? | That the stop is "wrong" — it executes exactly as specified |
| **PQ-2** | See section 10 | A (stop entries) / B (flatten) / C (entries + protection) / D (other)? | **That Policy B is safer.** Aug-19 evidence contradicts it |
| **S-03r** | Declared envelope ~Rs.2,000 vs structurally attainable ~Rs.15,000 = **~7.5x**. **Monday's realized option loss Rs.10,133 substantially exceeded the declared envelope** | Which number is the risk envelope? | That it is a bug — both are correct answers to different questions. Downstream of the entry/position architecture. **Do not silently close** |

---

## 8. S-21 — DTE (OPEN — ASSOCIATION VERIFIED, CAUSATION NOT PROVEN)

**Promoted in v1.2 from UNKNOWN.** Historical evidence:

| DTE band | n | win rate | total P&L | mean return | initial-stop rate |
|---|---|---|---|---|---|
| **≤5** | 18 | **11%** | **−Rs.35,233** | −8.77% | **44%** |
| **6–8** | 31 | **61%** | **+Rs.25,473** | +3.48% | **10%** |

Within-day evidence also supported a strong association (Aug-17: the DTE-8 basket returned +Rs.4,000 at 70% win while the same day's DTE-1 trade lost).

**Classification, stated precisely:**

- **VERIFIED:** the DTE regime changed materially during the deterioration (8 → 7 → 6 → 5 → 4 → 1 as the 25-Aug expiry approached).
- **VERIFIED:** low-DTE trades have substantially worse historical outcomes **in this dataset**.
- **INFERRED:** near-expiry theta/gamma behaviour is a plausible mechanism.
- **UNKNOWN:** whether DTE is itself causal independently of other correlated variables.

**Do not rewrite this as "DTE was proven root cause." Do not change `min_dte` on this evidence.**

### 8.1 Tuesday 2026-08-25 DTE regime — preserved

25-Aug was expiry day; `min_dte=1` excluded 0DTE. Nearest selectable expiry: **NIFTY → 2026-09-01 (DTE 7)**; **19 other instruments → 2026-09-29 (DTE 35)**. Monday's DTE=1 regime disappeared.

**DTE 35 is outside the historical live-traded maximum of ~DTE 25 → UNKNOWN REGIME.** Do not claim it is good. Do not claim it is bad. Do not optimize around it without evidence.

---

## 9. PQ-1 — INITIAL STOP (REAL CONTRIBUTOR, DOWNSTREAM OF DTE)

- Initial-stop rate **44% at DTE ≤5 versus 10% at DTE 6–8** under the identical −15% rule.
- **Monday HDFCBANK: underlying rose ~+0.28% and the CE still lost ~−15%.** Option premium behaviour and contract characteristics matter **independently of underlying direction**.
- Stop distance measured in microstructure terms: **8.0 spreads on WIPRO, 179.4 on NIFTY** (~20x disparity); cheapest stop ~**8 ticks**.

**Status: REAL CONTRIBUTOR · DOWNSTREAM INTERACTION WITH DTE · POLICY DECISION STILL OPEN.**
**Do not change the stop parameter on this evidence alone.** Tuning the stop now would mask a time-decay exposure rather than fix it.

---

## 10. PQ-2 — DAILY-LOSS ARCHITECTURE (DID NOT CAUSE MONDAY)

- All four Monday option entries occurred **09:31:05–09:31:08**. No realized loss existed yet, so **the latch was structurally incapable of preventing them**.
- Aug-19 counter-evidence preserved: latch fired ~09:51:51 at ~−Rs.2,991 and **the day closed ~+Rs.5,026** — an over-restriction cost.
- Historical after-latch shares: Aug-20 46.1%, Aug-21 62.2% of the day's loss landed after the latch, with 3 positions open each time.

**Do not claim PQ-2 caused Monday. Do not automatically implement Policy B.**

---

## 11. Proposed change IDs — DESIGN ONLY (all PREPARE ONLY)

| ID | Purpose | Files | Touches frozen code? | Status |
|---|---|---|---|---|
| **S-37** | Poisoned cache-key remediation | `.github/workflows/intraday.yml` | No | PREPARE ONLY |
| **S-38** | Post-exit instrumentation (S-20) | `options_trader.py`, `telemetry.py` | **Yes** | PREPARE ONLY |
| **S-39** | Dashboard stamp / build volume (S-30, U-002) | `generate_dashboard.py`, `.gitignore` | No | PREPARE ONLY |
| **S-40** | Research alignment (S-36) | `research_signal_quality.py` | No | PREPARE ONLY |
| **S-41** | **Level-2 replay — RESCOPED, see 11.1** | new module + telemetry | **Yes (telemetry)** | PREPARE ONLY |
| **S-42** | Ranking-path guard (U-001) | `options_trader.py` | **Yes** | PREPARE ONLY — P2 defence-in-depth |
| **S-43** | Workflow `timeout-minutes` (U-020) | `.github/workflows/intraday.yml` | No | PREPARE ONLY |

### 11.1 S-41 — RESCOPED in v1.2

S-41 is **not** simply "build Level-2 replay". It **must additionally provide continued observation / candidate quoting for non-selected candidates after `MAX_POSITIONS` fills.**

Purpose: enable complete sequential counterfactual replay · Aug-19/20/21/24 and Monday reconstruction · comparison of alternative selectors · evaluation of DTE policies · evidence for S-01 / PQ-1 / PQ-2 / S-03r decisions.

Without that continued quoting, ranking cycles collapse to 3–7 per day once slots fill and no counterfactual is possible for the most recent — and most important — sessions.

---

## 12. SUPERSEDED / DUPLICATE / ANALYSIS-ONLY (6)

| ID | Disposition |
|---|---|
| **K** | **SUPERSEDED by O-1.** Designed an EOD-only observability sync; O-1 moved it per-cycle after proving EOD *destroyed* telemetry |
| **PQ-3** | **DUPLICATE of S-01.** Governance record only |
| **PQ-4** | **DUPLICATE of S-36.** Governance record only |
| **P0-A** | Analysis, not a change — EMA parity replay of 87 entries. Cited at `options_trader.py:378` |
| **S-34** | Umbrella label; split into **S-34a** and **S-34b** |
| EXIT_ENGINE_AUDIT finding 1 | **DUPLICATE of S-05**, closed |

---

## 13. P2 — STRATEGY RESEARCH (13)

Membership corroborated by `a549d3cb`.

| ID | Label | Status |
|---|---|---|
| S-20 | trend reversal | **OPEN — ROOT CAUSE VERIFIED** (section 5) |
| S-21 | DTE | **OPEN — ASSOCIATION VERIFIED, CAUSATION NOT PROVEN** (section 8) |
| S-12 | trade-count escalation | OPEN — INFERRED from `c3e12215`; content UNKNOWN |
| S-32 | path-order limitation | OPEN — VERIFIED at `replay_engine.py:283` |
| S-07, S-13, S-14, S-16, S-17, S-18, S-19, S-22, S-23 | signal invalidation · cross-book awareness · market gate · daily trade cap · portfolio exposure · flat/low-volatility regime · initial stop performance · ATM/liquidity · ranking predictive power | **UNKNOWN — label only. No invention.** |

---

## 14. REGISTER GAPS (8)

| ID | Verdict |
|---|---|
| S-08, S-10, S-24, S-27, S-31 | UNKNOWN — governance label only; zero repository evidence |
| **S-11, S-15** | **Cannot establish these identifiers were ever assigned** |
| S-35 | **ANSWERED BY FORENSICS** — cache used per-day, not per-cycle. No change proposed under this ID |

---

## 15. UNREGISTERED ISSUES (18)

| ID | Issue | Status | Class |
|---|---|---|---|
| **U-001** | Ranking path unguarded upstream of exits | OPEN — **P2 defence-in-depth** (section 16) | Risk-control |
| U-002 | `observability.db` not gitignored | OPEN | Infrastructure |
| U-003 | Rs.144.05 accounting discrepancy | OPEN (historical data) | Risk-control |
| U-004 | `cost_per_side` declared but not applied | OPEN — policy | Strategy |
| U-005 | `slippage` declared but not applied | OPEN — policy | Strategy |
| U-006 | `risk_per_trade_percent` declared but not applied | OPEN — policy | Strategy |
| U-007 | `max_daily_profit_percent` declared but not applied | OPEN — policy | Strategy |
| U-008 | Stock daily-loss limit reachability | OPEN | Risk-control |
| U-009 | Gross-versus-net limit basis | OPEN | Risk-control |
| U-010 | Square-off fallback reduction | OPEN | Availability |
| U-011 | Absent-gh-pages retry edge | OPEN | Infrastructure |
| U-012 | True concurrent GitHub race | OPEN | Infrastructure |
| U-013 | Shadow tier recalibration | OPEN — policy | Strategy |
| **U-014** | **Trailing dead zone — PRODUCTION CONFIRMED (section 17)** | **OPEN** | Strategy |
| U-015 | Reversal check may read a 285-second-old signal at 1-minute polling | OPEN — conditional | Risk-control |
| U-016 | Comparison mismatch mislabels an exact-boundary trailing exit | INFERRED CLOSED — both sites now use the same comparison | Observability |
| **U-017** | Historical Telegram credential in public git history | **OPEN — security (section 18)** | Security |
| U-018 | Repository public: code, logic, live trade databases and cash balance anonymously readable *(absorbs U-019: Actions-minutes billing exposure if made private)* | OPEN — policy | Strategy |
| U-020 | No `timeout-minutes` on the job | OPEN | Infrastructure |

---

## 16. U-001 — corrected assessment (P2, NOT P0)

**Structurally real (VERIFIED):** ranking calls are unguarded, `main()` has no outer exception boundary, and the call sequence sits upstream of every exit. An exception there **would** structurally bypass position management and exits.

**But reachability is narrow — four candidate raise sites checked, all guarded:** `load_history` (`try/except sqlite3.Error`); the `None`-quote path (`if q and q.get("ltp")` at `ranking_engine.py:174-175`); the weighted-score division (`total_w = sum(weights.values()) or 1.0`); the NIFTY direction lookup (guarded by construction via `in_window`).

**Production evidence: none.** Monday added **315 more clean cycles** with zero ranking exceptions. The only two failed runs on record (Aug-19) failed at `Install dependencies` with the trader step **skipped**.

**Status: P2 defence-in-depth / robustness. NOT P0. NOT a proven cause of Monday's losses. Do not lose the issue.**

---

## 17. U-014 — TRAILING DEAD ZONE (PRODUCTION CONFIRMED)

**Monday production evidence.** RELIANCE 1320 CE: entry 2.75, **peak high-water 3.10 = +12.7%**, trail level at peak = 2.728, **booked exit 2.73 = −0.73%**.

The trail arms at **+10%** (3.025) but break-even requires **+13.64%** (3.125). A profitable excursion inside that band decays back into a loss before the trail can protect it.

**Status: OPEN — PRODUCTION CONFIRMED. Do not modify trailing parameters without a separate authorized change.**

---

## 18. U-017 — SECURITY (verified; credential never reproduced)

**No credential value appears anywhere in this register. Liveness was deliberately not tested.**

| Question | Finding |
|---|---|
| Where exposed | `backtest-machine/config.json` in commits `c9e1323b` and `8bcaa500` |
| Still in current tracked files? | **No** — tracked config has empty Telegram and broker fields |
| Still in git history? | **Yes** |
| Publicly accessible? | **Yes** — anonymous fetch returns HTTP 200 |
| Rotation status | **UNKNOWN** — deliberately untested |
| Privilege | Telegram Bot API only: send/read messages |
| Trading impact | **None established.** Broker credentials were never committed |
| Independent of v1.9? | **Yes** — BotFather rotation plus secret update; no code change |

**Status: OPEN — SECURITY REMEDIATION REQUIRED. Independent of all trading root-cause analysis.**

---

## 19. Monday attribution — what was and was not the cause

| Candidate cause | Verdict |
|---|---|
| **Market direction** | **Contributing but NOT sufficient.** 14 BULL / 2 BEAR / 4 None; market fell modestly. But **HDFCBANK's underlying rose ~+0.28% and its CE still lost ~−15%** — a pure direction error cannot produce that |
| **DTE** | Strongest change-point association (all four at DTE=1); causation not proven |
| **Selection (S-44)** | Chronic, present in profitable sessions too — **not established as the change-point** |
| **PQ-1 stop** | Real contributor, substantially downstream of DTE |
| **PQ-2 daily loss** | **No causal role** — all entries preceded any possible latch |
| **Cache / instrument master** | **EXONERATED** — see section 20 |
| **Exposure / S-03r** | Loss exceeded the declared envelope; downstream of entry architecture |

**Monday is not attributable to a single cause. Do not force one.**

---

## 20. Cache / data quality — MONDAY EXONERATION

Instrument master resolved correctly. Correct expiries and strikes. **16 of 20 symbols produced candidates; the 4 omissions had `direction=None`** and were correctly excluded. All 20 signals carried `bar_status=COMPLETED`.

**The poisoned-cache issue did NOT explain Monday's selection.** Do not falsely attribute Monday to cache corruption. The cache-key issue (section 5) remains open independently.

---

## 21. MAX_POSITIONS — structural constraint

`MAX_POSITIONS = 4`. Monday had **16 candidates**; everything after the first four qualifying entries was **unreachable regardless of quality**. This binds on every entry day.

Executed rank bands across n=128: rank 1–6 → +Rs.19,372; rank 7+ → −Rs.36,375.
**Do not claim rank 1–6 is proven optimal.** That split is confounded by symbol/universe composition — executed rank 7+ also means later universe names, a different symbol mix.

---

## 22. Change-point — the key distinction

**S-44 selection-by-universe-order is CHRONIC.** It was present during both profitable and unprofitable sessions. It is therefore **NOT established as the change-point** for the recent deterioration.

**The DTE regime changed materially** as the 25-Aug expiry approached (8 → 1). DTE is the **strongest current change-point association**. **Causality remains unproven.**

---

## 23. Issue relationship map (documentation, NOT authorization)

```
UNIVERSE ORDER ─────> chronic selection architecture defect (S-44)
                        └─> four slots consumed by first qualifying universe entries
                             └─> ranking alternatives ignored (ranking = shadow only)
                                  └─> activation CONTRAINDICATED by counterfactual

DTE CALENDAR ───────> changing option regime (S-21)
                        └─> higher observed low-DTE initial-stop rate
                             └─> possible theta/gamma vulnerability [INFERRED]
                                  └─> PQ-1 stop interaction [downstream]

PQ-2 ───────────────> did NOT cause Monday's entry losses
U-014 ──────────────> confirmed downstream exit/protection defect
S-03r ──────────────> risk-envelope mismatch, downstream of entry architecture
S-36 ───────────────> ranking provenance issue; ZERO live impact while ranking is shadow
U-001 ──────────────> robustness/exception-handling; not demonstrated as a cause
U-017 ──────────────> independent security exposure
CACHE ──────────────> exonerated for Monday
```

---

## 24. Priority

- **P0 — immediate:** none.
- **P1:** S-44 (design) · S-37 · S-01 · PQ-1 · PQ-2 · S-03r · S-21 · U-003 · U-008 · U-009 · U-017 · U-018
- **P2:** S-40 · S-41 · S-42 (U-001) · S-12 · S-32 · U-013 · U-014 · U-004 to U-007
- **P3:** S-38 · S-39 · U-002 · U-010 · U-011 · U-012 · U-015 · U-020
- **UNKNOWN:** S-07, S-08, S-10, S-11, S-13, S-14, S-15, S-16, S-17, S-18, S-19, S-22, S-23, S-24, S-27, S-31

---

## 25. What must NOT change without new evidence

`ranking.mode` (stays `shadow` — activation contraindicated) · the selector / universe order / `MAX_POSITIONS` · `min_dte` / DTE policy · `INITIAL_STOP_PCT` · `TRAIL_PCT` / `TRAIL_ACTIVATE_PCT` · PQ-2 daily-loss semantics · S-01 VWAP · S-03r sizing · ranking weights · candidate filters · signal thresholds · cache key / workflow · S-09 · P0-B · S-33 · S-34a · S-34b.

---

## 26. Change log

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-08-22 | Initial exhaustive forensic reconciliation. 76 raw candidates, 0 unreconciled. |
| 1.1 | 2026-08-22 | U-001 severity corrected to defence-in-depth after all four raise sites verified guarded. U-017 verified without reproducing the credential. Noted `intraday_config.json` gitignored yet still tracked. |
| **1.2** | **2026-08-25** | **Reconciled against Monday 2026-08-24 production evidence, Tuesday 2026-08-25 pre-market analysis, and the S-44 historical counterfactual.** Corrections to prior conclusions: (1) **S-44 confirmed architecturally** — live selection follows candidate/universe order. (2) **Activating existing ranking was TESTED and found materially worse** (n=78: 26.9% win, −5.23% mean, 45% initial-stop, versus actual n=111: 46.8%, +0.13%, 19%) — remediation **CONTRAINDICATED**. (3) **DTE (S-21)** promoted from UNKNOWN to a strong association / change-point hypothesis; **causality remains unproven**. (4) **S-41 rescoped** from generic replay to **continued candidate observation/quoting after slots fill**. (5) **P0-B promoted to CLOSED / PRODUCTION VERIFIED** (315/315 `code_sha`). (6) **U-014 became PRODUCTION CONFIRMED** (RELIANCE +12.7% peak → −0.73% booked). (7) **U-001 remained P2**, not safety-critical. (8) **S-36 confirmed** but with **zero current runtime impact** while ranking is shadow. (9) **PQ-1 confirmed** a real contributor but **downstream of DTE**. (10) **PQ-2 shown not to have caused** Monday's entry losses. (11) **Cache / instrument master exonerated** for Monday. (12) **U-017 remains independently open.** |
