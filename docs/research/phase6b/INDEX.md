# Phase 6B — evidence index

Preserved 2026-10-03. Documentation only: no trading code, configuration,
workflow, state or research-ledger change accompanies this record. The
research ledger stands at **71** (`RESEARCH-MEASUREMENT-NOTES.md`, §3).

## Chain

```
Phase 6B design review report        (original: PRESENT — report.md)
    ↓
Phase 6B scoped amendment            (amendment.md)
    ↓
Phase 6B errata                      (errata.md)
    ↓
E4 identity verification             (e4_identity.md)
    ↓
ONE_MINUTE NFO feasibility probe     (commit f23575d8, run 37044455850)
    ↓
Stage 2 accepted report              (probe_stage2_report.md)
    ↓
Disposition C — MATERIAL INSTRUMENTATION REQUIRED
```

Each later document corrects or qualifies the earlier ones. Read the original
report only together with the amendment, the errata and the E4 check.

## PUBLIC PERMANENT RECORD

Committed in this directory. sha256 is of the content as stored by git (LF
line endings).

| file | document | sha256 |
|---|---|---|
| `report.md` | Phase 6B design review report (original, extracted exactly from the session transcript) | `87ca01c02965b9bafe82da6cb9a0bc7eea274421797b8461fa7e94db576c9be8` |
| `amendment.md` | Phase 6B scoped amendment (one marked redaction) | `d703287b9bbb8a6bf2d57ff8e2ca7a73329079e442622e4e519d2da6786aa6ba` |
| `errata.md` | Phase 6B errata | `e9ee038a073df55d95be35a6e732766e19901fc7681207bc5628bc7fa0a84c48` |
| `e4_identity.md` | E4 identity verification | `bf7c8a820ccea38d6dd5e4f4db312879c3d52b586ee467c54bff6e2f12373972` |
| `probe_stage2_report.md` | Probe Stage 2 report, accepted (market-data values omitted) | `ad0b077972a54cfea6171ca4b399f3500e125be8681c47bd05d5cf1e3ba60569` |

Each file carries a provenance header recording its source, the source's
sha256 and every change made below the header.

## PRIVATE RAW EVIDENCE

Not committed. Listed so it can be located and verified.

| item | location | sha256 | size |
|---|---|---|---|
| probe artifact zip | operator's local disk, `probe_out\angel-candle-probe.zip` | `e09a816fbab65004f17623cb86093c1c637ae9205f40f1ec0f8e8b9674829bee` | 26,236 bytes |
| `probe_result.json` (unzipped from the above) | operator's local disk, `probe_out\probe_result.json` | `ee5622193e9acdcda67a8b3936be8dadbb4a1c9d1cec41ced4fdcb469bc0f937` | 183,939 bytes |

- Current private copy: the operator's local disk, hash-verified, **not backed
  up — non-durable**.
- `PRIVATE_RAW_ARTIFACT_DESTINATION = NOT ESTABLISHED`
- The GitHub artifact copy expires `2026-11-01T18:00:27Z`.

## Data pins

- Phase 6B figures: trading-state `9c583bbf` (2026-09-28). Trades after
  2026-09-28 are not included.
- Probe contract selection: trading-state `ce560692`.

## Probe artifact metadata

- Artifact ID `11242958659`, name `angel-candle-probe`, from run
  `37044455850` (`workflow_dispatch`, head `f23575d8`, success).
- Zip sha256 `e09a816fbab65004f17623cb86093c1c637ae9205f40f1ec0f8e8b9674829bee`;
  26,236 bytes.
- Created `2026-10-02T18:00:27Z`; expires `2026-11-01T18:00:27Z`.

## Accepted probe findings

- **#3 (monthly) AVAILABLE**: 373 in-session bars with OHLC,
  `BANKNIFTY27OCT2654200PE`, token 49420.
- **#7′ (weekly) AVAILABLE**: 375 in-session bars with OHLC,
  `NIFTY06OCT2622550PE`, token 40700.
- Controls #1 and #2 passed. #4 returned 374.
- Contract X NOT ESTABLISHED; #5 and #6 NOT RUN.
- Rule 1.7 is applied as in-session AND with OHLC, jointly.
- Limitation: one session, four contracts, one run.
- D contingency: NOT TRIGGERED — retired by #3 (AVAILABLE). Phase 6B
  disposition C stands.

## Observations recorded without interpretation

- Equity control #1 has no bars from 15:15 to 15:28 inclusive, and has a
  15:29 bar. Cause NOT ESTABLISHED.
- Each NFO request returned one bar stamped 15:30, outside the session.

## Qualification

- A1 (milestone distribution, "exact lower bound, all 255") includes 34
  model-output exit returns (15 at ≥ +5%). A live-only A1 was NOT COMPUTED.

## Key corrections to the original report

The original report (report.md) is preserved unaltered. These later
findings correct or qualify it; cite them, not the original figures.

- §12A.1 excursion bound: live-price trades only, 88 of 221 (39.8%).
  96 of 255 is a label count only; the 8 model-era "Trailing stop" labels
  are defect-derived and their excursion is NOT ESTABLISHED (errata E1).
- Era boundary: the stabilization floor first ran 2026-08-28. Live
  trailing exits by era: 1 of 17, 59 of 135, 28 of 69 (errata E1).
- A2 is the union of three evidence sources with different coverage, not
  a complete high-water measurement of the book (amendment §1D; errata E1).
- First-poll exposure: 4/51, 1/51, 0/51 on the measurable population;
  45 of 96 NOT ESTABLISHED (errata E2).
- Floor binding: 7 of 28 post-stabilization trailing exits with recorded
  high-water; the formula reproduces 52 of 52 (amendment §5).
- +5% reach, 19.6% versus 61.2%: same field, different populations;
  the gap is selection (amendment §2).
- Path-observed and high-water sets: proven one-to-one, 121 = 121 (E4).
- Option B: NOT AVAILABLE AS BUILT — requires either a rule-scope
  decision for A or new infrastructure for C (errata E3).

## Superseded and recorded facts

Recorded in the errata (E5) and referenced here, not restated as new:

- The stabilization floor first ran in production on 2026-08-28.
- The options-book P&L forensic boundary needs its own correction.
- U-014b is not a durable repository artifact.
- The trader job holds `contents: write` together with the Angel
  credentials, so the scope of the separation rule is undecided.

## NOT ESTABLISHED

- Raw versus carry-forward/synthetic bars.
- Agreement with bid-based marks.
- Intrabar ordering.
- Same-day post-expiry retrieval.
- Expired-contract retrieval.
- ONE_MINUTE lookback depth.
- ONE_MINUTE truncation behaviour.
- Throughput at collection scale.
- Completeness across other sessions and contracts.
- Option B persistence and the separation-rule scope.

## Redistribution note

> Raw API candle payload is intentionally not committed to the public
> repository because permission/compliance for public redistribution of
> this market-data payload has not been established.

Existing public quote snapshots are precedent only and do not establish
compliance.

## Scope

> Phase 6B does not establish that any trailing activation threshold
> improves trading performance. It establishes that a historical
> ONE_MINUTE NFO candle endpoint was successfully demonstrated for the
> tested monthly and weekly contracts.
