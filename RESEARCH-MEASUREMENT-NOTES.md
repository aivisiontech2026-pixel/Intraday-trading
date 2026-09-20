# RESEARCH MEASUREMENT NOTES

Reusable facts about **how this project measures**, kept separate from findings
about any particular strategy. Read this before writing a pre-registration, so
power estimates start from the right numbers.

---

## 1. DESIGN EFFECT DEPENDS ON THE SAMPLING SCHEME, NOT THE CORPUS

Measured twice, on the same 20-symbol universe, with materially different
answers:

| sampling scheme | design effect | measured in |
|---|---|---|
| fixed decision times (5 per session, all 20 symbols) | **~5.7x** | Phase 1 / 3A, two corpora |
| **event-driven** (signal fires when a condition is met) | **1.33 - 2.27x** | Phase 4A, Angel 5m holdout |
| per-symbol DAILY (1 obs per symbol per session) | ~2.5x | Phase 3A |
| market-wide daily (1 obs per session) | 1.0x | by construction |

**Why.** At five synchronised decision instants, all 20 symbols are sampled at
the same moment and share one market-wide move, so within-session correlation
is near maximal. Event-driven mechanisms fire at scattered times across the
session — Phase 4A's ORB breakouts spread from 09:30 through the afternoon —
so the common component is far weaker and each observation carries more
independent information.

**Consequence for power.** The same corpus yields materially more resolving
power under event-driven sampling. Phase 4A's holdout (167 sessions) resolved
effects down to **2.4 - 11.7 bps** at 80% power with a Bonferroni z of 3.38.
An estimate built on 5.7x would have predicted roughly twice that and might
have failed a power gate the study actually cleared with margin.

**Use 5.7x only for fixed-decision-time designs.** For event-driven designs,
measure it; 1.33 - 2.27x is the observed range so far. Never assume the
favourable end.

Observed per-cell values, Phase 4A holdout:

```
H1 ORB   15m 1.33x   30m 1.44x   60m 1.60x   EOD 2.27x
H2 shock 15m 1.45x   30m 1.50x   60m 1.41x   EOD 1.78x
```

The design effect rises with horizon in both mechanisms, because longer
forward windows overlap more across symbols within a session. EOD is always
the binding cell.

---

## 2. FORWARD-RETURN DISPERSION, ANGEL 5-MINUTE, 20 SYMBOLS

Single-leg, signal-conditional, holdout 2026-01-01..2026-09-03 (167 sessions):

```
15 min   sd ~27 bps        60 min   sd ~49 bps
30 min   sd ~37 bps        EOD      sd ~92 bps
```

A long/short PAIR is **1.275x** a single leg, not sqrt(2) — the legs are
correlated (Phase 2A, measured on random pairs). Cross-sectional designs
should use 1.275x, which is the pessimistic and correct figure.

---

## 3. THE PERSISTED TEST LEDGER

Multiplicity is corrected against the **cumulative** ledger, not per-phase.
Phase 4A's operator instruction settled this: the project is not a fresh
statistical universe each time, and the correction is nearly free because the
clustered SE dominates.

```
after Phase 3C    61
after Phase 4A    69
```

At 69 tests, z = 3.380. Going from 4 tests to 69 moves z from 2.50 to 3.38 —
about 0.9 of a standard error, which is small next to the clustering
correction it sits beside.

---

## 4. WHAT IS CLOSED, AND HOW CLOSE IT WAS

Every direction closed so far has landed within roughly one basis point of
zero, or within one percentage point of its breakeven, on samples large enough
to resolve single-digit basis points. None was a near miss.

| direction | result | where |
|---|---|---|
| EMA/VWAP confluence | 49.24% vs 60.72% bar | Phase 1-2A |
| long premium, all structures | no cell tradeable at 52% | Phase 2A |
| short premium | no measurable volatility risk premium | Phase 2B |
| daily OI skew | r = -0.042, corrected CI includes zero | Phase 3C |
| H1 opening-range breakout | -0.87 to +0.12 bps vs 27.4 bps | Phase 4A |
| H2 volatility-normalised momentum | -0.21 to +0.93 bps vs 27.4 bps | Phase 4A |
| H3 shock reversal | H2 with the sign flipped; same failure | Phase 4B |

**Three of these produced magnitude predictability with no directional
content** — India VIX (Phase 3A), OI skew's absolute channel (3C), and H2's
absolute forward movement (4A, 19-65 bps with intervals excluding zero).
Magnitude is not direction, and this project has now mistaken one for the
other zero times because the falsifier was written down first each time.
