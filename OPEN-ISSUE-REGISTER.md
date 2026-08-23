# OPEN-ISSUE-REGISTER

**Version:** 1.1
**Baseline at time of writing:** `baseline-v1.9-s34b-refresh-throttle-2026-08-22`
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

**Lifecycle states:** `CLOSED — PRODUCTION VERIFIED` · `IMPLEMENTED/FROZEN — NOT PRODUCTION OBSERVED` · `OPEN — ROOT CAUSE VERIFIED` · `OPEN — POLICY/STRATEGY DECISION` · `OPEN — MORE EVIDENCE REQUIRED` · `UNKNOWN` · `SUPERSEDED` · `DUPLICATE`

### The rule that matters most

> **CLOSED requires production evidence.**
> Code existing, tests passing, a design approved, a commit made, a tag created, or a branch merged are **none of them** sufficient. `IMPLEMENTED / FROZEN / NOT PRODUCTION OBSERVED` is a distinct state and is **not** CLOSED.

### What must never be converted

test evidence → production evidence · historical behaviour → future behaviour · projection → observation · inference → fact.

---

## 1. Reconciliation totals (v1.1)

| | Count |
|---|---|
| Raw candidates discovered | **76** |
| Reconciled | **76** |
| **Unreconciled** | **0** |
| Closed — production verified | 21 |
| Implemented/frozen — not production observed | 4 |
| Open — root cause verified | 7 |
| Open — policy/strategy decision | 4 (+5 unregistered policy items) |
| Open — more evidence required | 5 |
| Unknown | 16 |
| Superseded | 1 |
| Duplicate | 4 |
| Unregistered issues created | 18 |

Discovery method: regex sweep of all identifier shapes across `*.py/*.yml/*.sh/*.md/*.json`; the same across **all commit subjects, commit bodies and tag messages**; `TODO|FIXME|HACK|XXX|BUG|WORKAROUND|KNOWN ISSUE|LIMITATION|NOT IMPLEMENTED|DEFERRED|caveat`; the three tracked markdown documents; a targeted trading-safety class sweep; and code-ordering verification of specific claims.

---

## 2. CLOSED — PRODUCTION VERIFIED (21)

| ID | Issue | Commit | Production evidence |
|---|---|---|---|
| S-02 | EMA `adjust=False` parity with backtest | `a549d3cb` | Ran Aug-18 to Aug-21 |
| **S-03h** | *(historical meaning)* Single authoritative config contract | `a549d3cb` | Ran |
| S-04 / S-06 | Per-book Rs.2,000 latching daily-loss supervisor | `a549d3cb` | Latched Aug-21 14:19:40 |
| S-05 | Entry-side dependencies no longer gate exits | `a549d3cb` | 4h12m master outage Aug-21; square-off still executed 15:16:05 |
| S-09 | Completed-bar signal boundary | `94ec220e` | FORMING-bar disagreement 4.7% to 0.0% (0/968) |
| S-25 / S-26 | Shadow-path integrity; dead ranking feature | `a549d3cb` | Ran, `ranking.mode=shadow` |
| S-28 | Stock entry window bounded at 14:30 | `a549d3cb` | Aug-21 entries 09:31:43 |
| S-29 | Gross/net P&L reconciliation in reporting | `a549d3cb` | Ran |
| O-1 | Telemetry persisted per cycle, not EOD | `41f5d342` | 208 cycles recorded Aug-21 |
| P0-D / F-1 | Quote age unparsed (Angel One timestamp format) | `41f5d342` | Verified |
| P0-D / F-2 | `candidate_snapshot` had no production call site | `41f5d342` | Verified |
| P0-D / F-3 | Daily-loss replay tie ordering not a total order | `41f5d342` | `rowid` tiebreak at `safety_supervisor.py:116` |
| P0-E / T-1 | Telemetry simulation context | `c3e12215` | Ran |
| P0-E / T-2 | Telemetry explicit `init()` | `c3e12215` | Ran |
| P0-W | ENTRY_GATE zero-candidate ambiguity | `9ed1f910` | 208 gate rows Aug-21 |
| V2-R1..R5 | Five analytics reporting defects | `eccdab10` | Ran |

> **Identifier reuse — do not merge.** `S-03h` (config contract) is **CLOSED**. `S-03r` (risk envelope) is **OPEN** — section 5. Different problems sharing a number.

---

## 3. IMPLEMENTED / FROZEN — NOT PRODUCTION OBSERVED (4)

**These are NOT closed.** As of 2026-08-22 they have executed **zero** production cycles.

Proof of non-execution: `cycle.code_sha` is NULL on **834/834** recorded cycles, and `master_refresh_failed_at` is **absent** from production `meta`.

| ID | Commit / tag | Tests | Production evidence | Closure condition |
|---|---|---|---|---|
| P0-B | `eff0b7bb` / v1.6 | 9 | NONE | `code_sha` non-NULL in production |
| S-33 | `6f67b4c1` / v1.7 | 76 | NONE | A stale-fallback branch observed |
| S-34a | `cf40e99f` / v1.8 | 56 | NONE | A bounded refresh observed |
| S-34b | `e240b458` / v1.9 | 98 | NONE | A throttle decision observed |

Lifecycle: `DEPLOYED → NOT YET PRODUCTION OBSERVED → NOT CLOSED`.

---

## 4. OPEN — ROOT CAUSE VERIFIED (7)

| ID | Root cause | Evidence | Isolable | Change ID |
|---|---|---|---|---|
| **U-001** | Ranking calls are unguarded at `options_trader.py:955-963`; the position-management loop starts at `:966`; `main()` wraps `process()` with no try/except. An exception there would skip all exits. **Reachability is narrow — see 4.1.** | VERIFIED (line ordering) | Yes | **S-42** |
| Poisoned cache key | The daily cache key is created by the **first** run of the day regardless of whether the download succeeded; Actions cache keys are **immutable**, so a later success can never be reused | VERIFIED (+2-byte artifact vs thousands on every other day) | Yes (workflow-only) | **S-37** |
| S-20 | `emit_post_exit` has **no production call site** (defined `telemetry.py:518`; referenced only in 2 tests). `post_exit_path` = 0 rows by construction. Same class as F-2 | VERIFIED (grep) | Yes | **S-38** |
| S-30 | `generate_dashboard.py:377` stamps the current time at **minute** resolution, so content differs almost every cycle and the publish skip rarely fires | VERIFIED (Aug-20: 315 cycles produced 288 commits = 27 same-minute skips) | Yes | **S-39** |
| S-36 | `research_signal_quality.py:48-55` uses pre-S-02 EMA (no `adjust=False`) and the S-01 scalar VWAP. **`ranking_engine.py:19` cites this file as the evidence source for production ranking weights** | VERIFIED | Yes | **S-40** |
| U-002 | `observability.db` not in `.gitignore` | VERIFIED | Yes | S-39 |
| U-020 | Workflow has **no `timeout-minutes`**, so the GitHub default of 360 minutes applies | VERIFIED (0 occurrences) | Yes | **S-43** |

### 4.1 U-001 — verified severity (corrected in v1.1)

**Structurally real (VERIFIED).** The unguarded call sequence sits upstream of every exit, and there is no outer exception boundary in `main()`. If an exception occurred there, initial stops, trailing stops, trend-reversal exits, expiry exits and the 15:15 square-off would all be skipped. This is the **same failure class as S-05**, which the original audit rated the highest-severity coupling in the system.

**But reachability is narrow — four candidate raise sites were checked and all are guarded:**

| Candidate raise site | Verdict |
|---|---|
| `load_history` sqlite failure | **GUARDED** — `try/except sqlite3.Error` returns an empty dict |
| A `None` quote reaching a subscript on bid/ask | **GUARDED** — `if q and q.get("ltp")` at `ranking_engine.py:174-175` protects every subscript below it |
| ZeroDivisionError on the weighted score | **GUARDED** — `total_w = sum(weights.values()) or 1.0` |
| KeyError on the NIFTY direction lookup | **GUARDED by construction** — candidates are non-empty only when `in_window`, which itself requires NIFTY to be present in the signal data |

**Remaining raise sites are configuration-shaped, not market-data-shaped** (INFERRED) — for example a malformed `ranking` config block replacing the weights or tiers with a non-dict.

**Production evidence: none.** The only two failed runs in the record (Aug-19, `41f5d342`) failed at **`Install dependencies`** with the trader step **skipped** — not a trader crash. No ranking exception has ever been observed in 834 recorded cycles.

**Classification: OPEN — ROOT CAUSE VERIFIED. Defence-in-depth / robustness gap, priority P2.** Not an active safety defect. **Downgraded from the v1.0 assessment**, which over-stated reachability before the guards were checked.

---

## 5. OPEN — POLICY / STRATEGY DECISION (4)

Engineering cannot determine the correct behaviour. **No policy value may be changed because an alternative merely sounds safer.**

| ID | Verified engineering fact | Policy question | Must NOT be assumed |
|---|---|---|---|
| **S-01** | Documented contract is a session VWAP (typical price, daily reset). Production and research both use a whole-window, close-weighted scalar. Measured across two independent windows: **30.60% direction changes, 0 CE/PE reversals, +9.6% signals**, VWAP divergence median 0.355% of spot | Is the contract or the implementation authoritative? | That correcting it improves P&L. With **zero reversals** it is a *frequency* effect, never a *direction* effect |
| **PQ-1** | The flat -15% premium stop is **8.0 bid-ask spreads wide on WIPRO and 179.4 on NIFTY** (~20x disparity); the cheapest stop is **8 ticks**; full stops were observed on underlying moves of 0.081%, 0.172%, 0.176% and 0.448% | Premium / underlying / delta-adjusted / ATR / hybrid / absolute-rupee? | That the stop is "wrong" — it executes exactly as specified (Aug-21 HDFCBANK -14.953%) |
| **PQ-2** | Realized-only latch, per book, Rs.2,000. **Aug-19: latched 09:51:51 on -Rs.2,991, and the day closed +Rs.5,026.** Aug-20 / Aug-21: 46.1% / 62.2% of the day's loss landed after the latch; 3 positions were open at latch each time | A (stop entries) / B (flatten) / C (entries plus protection for open positions) / D (other)? | That B is safer — on the one available sample it converts Aug-19 into a loss |
| **S-03r** | Declared Rs.2,000 versus structurally attainable Rs.15,000 (4 x Rs.25,000 x 15%) = **7.5x** | Which number is the risk envelope? | That it is a bug — both are correct answers to different questions |

---

## 6. OPEN — MORE EVIDENCE REQUIRED (5)

S-01 · PQ-1 (n=6 stop events; no delta, IV or MAE/MFE recorded) · PQ-2 (n=3 latch events) · S-12 · S-21.

All are gated on **S-41 Level-2 sequential portfolio replay**, which does not exist. yfinance retains only about 5 days at 5-minute resolution, so the study window cannot be extended backwards.

---

## 7. SUPERSEDED / DUPLICATE / ANALYSIS-ONLY (6)

| ID | Disposition |
|---|---|
| **K** | **SUPERSEDED by O-1.** Designed an EOD-only observability sync; O-1 moved it per-cycle after proving the EOD design *destroyed* telemetry (8 cycles ran, 2 rows survived) |
| **PQ-3** | **DUPLICATE of S-01.** Governance record only; no repository evidence |
| **PQ-4** | **DUPLICATE of S-36.** Governance record only |
| **P0-A** | Analysis, not a change — EMA parity replay of 87 reconstructable entries. Cited at `options_trader.py:378` |
| **S-34** | Umbrella label; split into **S-34a** and **S-34b** |
| EXIT_ENGINE_AUDIT finding 1 | **DUPLICATE of S-05**, closed |

---

## 8. P2 — STRATEGY RESEARCH (13)

Membership corroborated by `a549d3cb`: *"Frozen and verified unchanged by AST comparison: S-01, S-07, S-09, S-12, S-13, S-14, S-16, S-17, S-18, S-19, S-20, S-21, S-22, S-23, S-32..."*

| ID | Label | Status |
|---|---|---|
| S-20 | trend reversal | **OPEN — ROOT CAUSE VERIFIED** (section 4) |
| S-12 | trade-count escalation | OPEN — INFERRED from `c3e12215`: *"remain OPEN — this work makes them measurable"*; content UNKNOWN |
| S-21 | DTE | OPEN — same evidence; content UNKNOWN |
| S-32 | path-order limitation | OPEN — VERIFIED at `replay_engine.py:283`: *"Intra-interval event order is UNAVAILABLE by construction"* |
| S-07, S-13, S-14, S-16, S-17, S-18, S-19, S-22, S-23 | signal invalidation · cross-book awareness · market gate · daily trade cap · portfolio exposure · flat/low-volatility regime · initial stop performance · ATM/liquidity · ranking predictive power | **UNKNOWN — label only.** No problem statement, evidence or component recoverable. **No invention.** |

None of the P2 set has a commit, a test or an implementation.

---

## 9. REGISTER GAPS (8)

| ID | Verdict |
|---|---|
| S-08 (freshness), S-10 (session/holiday validity), S-24 (replay instrumentation), S-27 (scheduler telemetry), S-31 (dashboard P&L labelling) | UNKNOWN — governance label only; zero repository evidence |
| **S-11, S-15** | **Cannot establish these identifiers were ever assigned.** Absent from every source searched |
| S-35 | **ANSWERED BY FORENSICS** — the cache is used per-day, not per-cycle (cycle 1 took 11.9 / 10.7 / 12.4 s versus about 2 s in steady state). No change was ever proposed under this ID |

---

## 10. UNREGISTERED ISSUES (18)

| ID | Issue | Source | Status | Class |
|---|---|---|---|---|
| **U-001** | Ranking path unguarded upstream of exits | `a549d3cb` follow-up; verified in code | OPEN — root cause verified (4.1) | Risk-control |
| U-002 | `observability.db` not gitignored | `a549d3cb` | OPEN | Infrastructure |
| U-003 | Rs.144.05 accounting discrepancy | `a549d3cb`; `generate_dashboard.py:154` | OPEN (historical data) | Risk-control |
| U-004 | `cost_per_side` declared but not applied | `config_contract.py:107` | OPEN — policy | Strategy |
| U-005 | `slippage` declared but not applied | `config_contract.py:108` | OPEN — policy | Strategy |
| U-006 | `risk_per_trade_percent` declared but not applied | `config_contract.py:109` | OPEN — policy | Strategy |
| U-007 | `max_daily_profit_percent` declared but not applied | `config_contract.py:110` | OPEN — policy | Strategy |
| U-008 | Stock daily-loss limit reachability | `a549d3cb` | OPEN | Risk-control |
| U-009 | Gross-versus-net limit basis | `a549d3cb` | OPEN | Risk-control |
| U-010 | Square-off fallback reduction | `a549d3cb` | OPEN | Availability |
| U-011 | Absent-gh-pages retry edge | `a549d3cb` | OPEN | Infrastructure |
| U-012 | True concurrent GitHub race | `a549d3cb` | OPEN | Infrastructure |
| U-013 | Shadow tier recalibration | `a549d3cb` | OPEN — policy | Strategy |
| U-014 | Trailing dead zone: arms at +10%, breaks even only at +13.64% | `EXIT_ENGINE_AUDIT.md` finding 3 | OPEN — policy | Strategy |
| U-015 | Reversal check may read a 285-second-old signal **at 1-minute polling** | `EXIT_ENGINE_AUDIT.md` caveat | OPEN — conditional | Risk-control |
| U-016 | Comparison mismatch mislabels an exact-boundary trailing exit | `EXIT_ENGINE_AUDIT.md` finding 2 | **INFERRED CLOSED** — both sites now use the same comparison; no closing commit found | Observability |
| **U-017** | **Historical Telegram credential exposed in public git history** | verified | **OPEN — escalate (section 11)** | Security |
| U-018 | Repository public: code, logic, **live trade databases and cash balance** anonymously readable | verified HTTP 200 | OPEN — policy | Strategy |

---

## 11. U-017 — SECURITY FINDING (verified; credential never reproduced)

**No credential value appears anywhere in this register, and none was transmitted or tested.**

| Question | Finding | Class |
|---|---|---|
| Where exposed | `backtest-machine/config.json` in commits `c9e1323b` and `8bcaa500` | VERIFIED |
| Still in current tracked files? | **No.** The tracked config has an empty Telegram bot token and chat id, and empty broker fields. No local `config.json` exists | VERIFIED |
| Still in git history? | **Yes.** Both commits carry a populated Telegram bot token (length consistent with a live-format token) and chat id | VERIFIED |
| Publicly accessible? | **Yes.** An anonymous fetch of the historical blob returns **HTTP 200** | VERIFIED |
| Revoked or rotated? | **UNKNOWN.** Deliberately not tested — verifying liveness would mean transmitting the credential | UNKNOWN |
| Privilege | Telegram Bot API only: send messages to chats the bot belongs to, read updates. Used at `options_trader.py:161`, `intraday_trader.py:90`, `eod_learner.py:32` | VERIFIED |
| Can it affect trading? | **No.** Notifications only. Broker credentials were **never committed** — they exist solely as GitHub Secrets injected at runtime | VERIFIED |
| Independent of v1.9? | **Yes.** Rotation is a BotFather action plus updating the repository secret. **No code change, no production impact, no Monday impact** | VERIFIED |

**Required remediation (authorization required; not performed):** rotate the token via BotFather and update the `TG_BOT_TOKEN` repository secret. Making the repository private would **not** undo the exposure. History rewriting is a separate, higher-risk decision and is **not** proposed here.

**Related note:** `backtest-machine/intraday_config.json` is listed in `.gitignore` yet remains **tracked** — `.gitignore` does not untrack an already-tracked file. Its current tracked content is placeholders only, so there is no live exposure, but the file is not actually protected by the ignore rule.

---

## 12. Proposed change IDs — DESIGN ONLY

**A documented fix is not an approved fix. A verified defect is not authorized for deployment.**

| ID | Purpose | Files | Touches frozen code? | Depends on | Auth |
|---|---|---|---|---|---|
| **S-37** | Poisoned cache-key remediation | `.github/workflows/intraday.yml` | No | — | Required |
| **S-38** | Post-exit instrumentation (S-20) | `options_trader.py`, `telemetry.py` | **Yes** | — | Required |
| **S-39** | Dashboard stamp / build volume (S-30, U-002) | `generate_dashboard.py`, `.gitignore` | No | — | Required |
| **S-40** | Research alignment (S-36) | `research_signal_quality.py` | No | S-01 decision | Required |
| **S-41** | Level-2 sequential portfolio replay | new module | No | — | Required |
| **S-42** | Ranking-path guard (U-001) | `options_trader.py` | **Yes** | — | Required |
| **S-43** | Workflow `timeout-minutes` (U-020) | `.github/workflows/intraday.yml` | No | — | Required |

**S-37 design (documented, not deployed):** replace the single cache step with a restore step plus a conditional save step. Restore by a run-scoped key with prefix restore-keys; record the cache file modification time in shell before and after the trader step; run the save step **only when that timestamp changed** — that is, only when a download actually succeeded. Workflow-only; **no production Python is required**, because the modification time already changes if and only if a refresh succeeded. Existing artifacts stay compatible through the prefix restore-key. Rollback is a revert of one workflow file.

---

## 13. Dependency graph (verified, not assumed)

```
S-41 Level-2 replay ─┬─> S-01    (hard: P&L impact unmeasurable without it)
                     ├─> PQ-1    (hard: n=6 stop events)
                     └─> PQ-2 ─> S-03r   (hard: envelope undecidable until policy set)

S-01 ─> S-36 ─> ranking weight rationale     (verified: ranking_engine.py:19)
S-37 ─> S-33 / S-34a / S-34b operational effectiveness   (soft)
S-38 ─> S-20 trend-reversal research confidence

Independently isolable, no dependencies: S-39, S-42, S-43, U-002, U-017
```

---

## 14. Priority

- **P0 — immediate:** none blocking Monday.
- **P1:** S-37 · S-01 · PQ-1 · PQ-2 · S-03r · U-003 · U-008 · U-009 · U-017 · U-018
- **P2:** S-40 · S-41 · S-42 (U-001) · S-12 · S-21 · S-32 · U-013 · U-014 · U-004 to U-007
- **P3:** S-38 · S-39 · U-002 · U-010 · U-011 · U-012 · U-015 · U-020
- **UNKNOWN:** S-07, S-08, S-10, S-11, S-13, S-14, S-15, S-16, S-17, S-18, S-19, S-22, S-23, S-24, S-27, S-31

Priorities reflect verified severity and reachability, not wording. U-001 is P2 because every reachable raise site was found guarded (section 4.1).

---

## 15. Monday observation protection

Monday 2026-08-24 is **OBSERVATION ONLY**. Predictions **P1 to P9 are frozen and must not be revised before evidence arrives.**

**Must NOT deploy before Monday:** S-37 (changes first-run cache state and would invalidate P3, P6, P7, P9) · S-38 and S-42 (change `options_trader.py`, therefore the executing commit, and would invalidate P1 and P2) · S-39, S-40, S-41, S-43 (no observational impact, but held for cleanliness).

**Must NOT change:** S-01, PQ-1, PQ-2, S-03r, or any trading-policy parameter.

If a Monday safety invariant is violated: **STOP. Do not fix, commit, push or revert. Preserve raw evidence and report.**

---

## 16. Recommended post-Monday execution order

1. Monday observation — nothing deploys first.
2. **U-017** token rotation — independent of everything, no code change.
3. **S-42** ranking-path guard.
4. **S-37** cache-key remediation.
5. **S-38** post-exit instrumentation.
6. **S-41** Level-2 replay — highest leverage; unblocks S-01, PQ-1, PQ-2 and S-03r.
7. S-39 · S-40 · S-43.
8. **PQ-2 → S-03r → PQ-1 → S-01**, informed by the S-41 output.

**Priority principle:** safety defects → availability and reliability → observability and evidence → research infrastructure → strategy policy → optimization. **Do not optimize while a genuine safety defect remains unresolved.**

---

## 17. Change log

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-08-22 | Initial exhaustive forensic reconciliation. 76 raw candidates, 0 unreconciled. |
| **1.1** | **2026-08-22** | **U-001 severity corrected** from safety-critical to defence-in-depth after all four candidate raise sites were verified guarded (section 4.1). **U-017 verified** in full without reproducing the credential (section 11). Noted that `intraday_config.json` is gitignored yet still tracked. |
