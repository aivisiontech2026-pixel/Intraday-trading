> **PROVENANCE — preserved document**
>
> - Document: Phase 6B scoped amendment
> - Origin: operator-supplied copy of Claude Code session output
> - Supplied file: `Textfiles/phase6b_amendment.txt`
> - Supplied file sha256: `34717f9fa423d17080c347c8e05abfac4bbe7404f3312b254e414c0b19b5bcc4`
> - Date preserved: 2026-10-03
> - Changes below the marker: ONE in-place redaction, marked in place: on supplied line 138 the archive repository's name is replaced with the backtick-wrapped marker `<REDACTED_ARCHIVE_REPO>` (backticks so it renders on GitHub) plus a bracketed note. The same string on supplied line 130 names a WORKFLOW FILE in this repository, not the archive repository, and is retained.
> - Git stores this file with LF line endings (core.autocrlf=true); the supplied file uses CRLF. No other byte of the text below the marker differs, except the changes listed above.

<!-- END PROVENANCE HEADER -->
PHASE 6B — SCOPED AMENDMENT
Read-only. Nothing in the code, config, schema, workflows, state, archive or credentials was modified. No API was probed, no P&L or win rate was computed, no commits were made. The ledger stays at 71.

One verified fact runs through several items below. The stabilization floor was committed on 2026-08-26 (8428c38f) but was not merged to main until 2026-08-27 22:20 IST (0d389482). The cycle.code_sha column shows production ran e240b458 (no floor) for every session on 08-26 and 08-27. The floor first ran in a trading session on 2026-08-28. Each place where the Phase 6B report used "2026-08-26" as the floor boundary is corrected here.

1. A2 coverage and decomposition
A. Coverage of high_water_at_exit. It is recorded only for exits from 2026-08-17 15:15:59 onward, which is when exit_snapshot begins.


all closed trades        121 of 255  (47.5%)
Trailing stop exits       52 of  96  (54.2%)
non-Trailing exits        69 of 159  (43.4%)
live: all                121 of 221  (54.8%)
live: Trailing            52 of  88  (59.1%)
live: non-Trailing        69 of 133  (51.9%)
The high-water component covers 121 of the 255 trades. Within the trailing-exit group, it covers 52 of the 96.

B / E. Decomposition by evidence source, all 255 trades. The three sources are:

E — final exit return at or above the level
T — the Trailing stop label (proves +10% and nothing higher)
H — the recorded high_water_at_exit
The union is counted without double-counting: E, plus T trades not already in E, plus H trades in neither.


lvl     E    T    H |  E&T  E&H  T&H  all3 |   E  +T\E  +H\(E,T) = UNION   published   diff
+5%    74   96   79 |   45   33   52    27 |  74    51       21  =   146      146        0
+7%    62   96   69 |   36   27   52    23 |  62    60       13  =   135      135        0
+8%    59   96   64 |   35   25   52    22 |  59    61        9  =   129      129        0
+10%   51   96   56 |   30   23   52    20 |  51    66        1  =   118      118        0
+12%   47    0   51 |    0   20    0     0 |  47     0       31  =    78       78        0
+15%   39    0   37 |    0   16    0     0 |  39     0       21  =    60       60        0
+20%   30    0   27 |    0   11    0     0 |  30     0       16  =    46       46        0
The decomposition reconciles exactly to the published A2 figures at all seven levels. One side result: T&H equals 52 at +5% to +10%. That means every one of the 52 trailing exits with a recorded high-water has that high-water at or above +10%. This independently confirms the label semantics.

C. Three-era segmentation, with verified boundaries.

Era	Dates	Trades	High-water coverage	Path-observed figures possible?
ERA 1	before 2026-08-06	51	0 / 51	No
ERA 2	2026-08-06 to 2026-08-27	135	52 / 135	Partial: 52 trades exit after 2026-08-17 15:15 and have coverage; 83 do not
ERA 3	2026-08-28 onward	69	69 / 69	Yes
How each boundary was verified:

ERA 1 / ERA 2: options_trades.peak_source is NULL on every exit through 2026-08-05 and populated from 2026-08-06. That date matches commit 25c65b84 (2026-08-06 01:09). code_sha was not recorded until 2026-08-21, so this boundary rests on corroborating data, not a recorded commit.
ERA 2 / ERA 3: verified directly from code_sha.
Path coverage inside ERA 2: both position_snapshot and exit_snapshot begin at 2026-08-17 15:15:59 (verified from the data).

UNION by era      +5%  +7%  +8%  +10%  +12%  +15%  +20%
ERA 1  (n=51)      23   23   23    23    18    15    13     E and T only
ERA 2  (n=135)     80   74   72    66    31    24    16
ERA 3  (n=69)      43   38   34    29    29    21    17
The full per-source tables are in the run log. In ERA 1, every figure rests on exit return and the label alone.

§12A.1 era split, recomputed on the verified boundary:


                       published (08-26 boundary)     verified (08-28 boundary)
ERA 1                   9 of  51  (17.6%)              9 of  51  (17.6%)
ERA 2                  52 of 121  (43.0%)             59 of 135  (43.7%)
ERA 3                  35 of  83  (42.2%)             28 of  69  (40.6%)
D. Corrected A2 wording.

"A2 — all 255 trades, the union of three evidence sources with different coverage:

final-exit-return evidence, covering all 255;
label-implied evidence, a lower bound at +10% only, covering the 96 trailing exits;
high_water_at_exit evidence, covering 121 of 255 — only exits from 2026-08-17 15:15 onward, none in ERA 1.
It is not a complete high-water measurement of the book."

Path-observed evidence (C1 and C2) is a separate population and is not part of A2.

2. §5 / §6C +5% reconciliation
A. Are the 51 a subset of the 121? Yes. Every reconstructible trade is path-observed. The 121 path-observed trades split into:

51 reconstructible
66 ambiguous (two or more snapshots)
4 with a single snapshot
B. Definitions.

Path-observed: at least one position_snapshot row for that token inside the trade's entry-to-exit window.
Reconstructible (Phase 6A): at least two such rows, and no interval in which a candidate threshold was crossed at an unobserved interval high or more than one threshold was crossed at once, and no interval where arming and stop-out both fall inside an unobserved gap.
C. The field behind "reached +5%". It is identical in both calculations: the maximum of position_snapshot.mark (bid if the bid is positive, otherwise LTP) over the same token and window. Phase 6A's "observed peak" and Phase 6B's C1 are the same expression.

E. Four cells. Each method is applied to each population.


                                         51 reconstructible    121 path-observed
Phase 6A method   max(mark)                10/51  (19.6%)        74/121  (61.2%)
Phase 6B-C1 method max(mark)               10/51  (19.6%)        74/121  (61.2%)
context: 6B-C2    max(high_water)          10/51  (19.6%)        79/121  (65.3%)
Cross-tab of the 121:


reconstructible       n= 51   reached +5%  10   never  41
ambiguous (>=2 obs)   n= 66   reached +5%  62   never   4
single snapshot       n=  4   reached +5%   2   never   2
                                            74           47
Cause: selection only. Holding the population fixed and changing the method moves nothing, because the methods are the same field. Changing the population moves the rate from 19.6% to 61.2%. Both published figures are correct measurements of different populations.

The two selection mechanisms are different and must not be conflated.

The 51 are outcome-selected. Ambiguity is caused by price crossing thresholds, so the filter removes the trades that rose: 62 of the 66 excluded ambiguous trades reached +5%. Its reach rate is biased downward by construction and says nothing about the book.
The 121 are coverage/date-selected. They are the exits from 2026-08-17 15:15 onward: the tail of ERA 2 and all of ERA 3, so only the regime after intra-interval-high capture, and mostly after the floor. They are not filtered on outcome. Their rate describes that period, not the whole book or ERA 1.
Final reconciliation: complete. No result here is NOT ESTABLISHED.

3. §12A.1 ordering-assumption immunity
§12A.1 establishes evidence of favorable excursion to at least +10%. It does not establish that the +10% level was actionable before any adverse intrabar movement, nor that a trailing stop could have executed at that level.

The high-water that justifies the label is either an observed polled bid or LTP, or the exchange's own session traded high. Both are actual prints. Saying "the premium reached +10%" requires that the print happened; it does not require knowing whether the high came before or after the low. The narrow excursion claim is therefore not contaminated by production's high-before-low ordering. Any claim about executability, or about the exit that followed, would be contaminated.

3A. First post-entry intra-interval-high check.

Can the first post-entry poll be labelled INTRA_INTERVAL_HIGH? Yes. At entry, day_high_seen is set to quote.get("high") or entry_px from the entry quote (options_trader.py:609-628). On the first poll, a session high above that baseline must have printed after the entry quote.
How often, among the 96 trailing exits? Measurable only for the 52 with post-entry path data; the other 44 are NOT ESTABLISHED.
First poll labelled INTRA_INTERVAL_HIGH: 5
Armed on the first poll (high-water at or above 1.10 × entry): 2 (2.1% of 96)
Armed on the first poll through intra-interval capture: 1 (1.0% of 96)
Not applicable, because the answer to point 1 is yes.
Could the first post-entry session high predate entry? Only through the fallback: if the entry quote carried no session high, the baseline becomes entry_px, and a pre-entry high would then pass the guard. Checked against the most recent persisted quote for each token at or before entry: 0 of 51 had a zero or missing high. One trade has no persisted entry-cycle quote (NOT ESTABLISHED), and the 44 earlier trades are NOT ESTABLISHED. The exposure exists in the code and was not observed in the data.
Is high-water state per position? Confirmed.
high_water, day_high_seen, day_low_seen and peak_source sit on the options_positions row (primary key id).
That row is inserted fresh at entry with baselines taken from the entry quote, and deleted at close (:794).
A re-entry gets a new row, whose baseline already absorbs the earlier session.
In the data: 0 of the 96 share a token and day with another trade, and 0 pairs of trades anywhere in the book overlap in the same token.
One residual note: position_snapshot is keyed by token. Analyses join it by token plus time window, which is safe given there are zero overlaps.
4. Option B persistence feasibility
Repository facts:

Intraday-trading is public (verified through the API: private: False).
The only written separation rule is scrip-archive.yml:16-17: "That is what makes it safe for this job to hold a write token at all — it holds a write credential OR the Angel credentials, never both." As written, it is a property of the archiver job.
The trader's own job already receives both. intraday.yml grants contents: write (line 88) and passes the four ANGEL_* secrets (lines 154–157) into the same trade job.
Persistence destinations:

Destination	Credential to write	Angel credentials in the same job?	Retention	Survives to 2027-01-04?	Status
A. trading-state branch	default GITHUB_TOKEN with contents: write	Yes, unless split into two jobs	Permanent	Yes	The rule as written does not prohibit it, and the trader job is precedent. It breaks a strict per-job reading. Rule K (state_sync.py:172-175) forbids other workflows from writing observability.db, so the data would need its own file under the per-file ownership model. Every per-minute run clones the branch, so its growth adds a cost to each run.
B. Actions artifact	default token	Either	90 days, as used in this repo	No — data from 2026-10-02 expires 2026-12-31, four days before the review	Not suitable as persistence. This is the same problem the archiver header records: "An artifact expires, so an artifact is not an archive." Usable only as a handoff within a single run.
C. Separate research repository	write PAT scoped to that repository	Only if the same job also fetches	Permanent	Yes	Compliant under the strict reading only as two jobs: job 1 holds Angel credentials and uploads a same-run artifact; job 2 holds the PAT and pushes. Neither the repository nor the PAT exists.
D. Existing `<REDACTED_ARCHIVE_REPO>` [archive repository name redacted in the public copy] repository	ARCHIVE_REPO_TOKEN	Must not be: the written rule forbids it in that job	Permanent	Yes	Possible only as the two-job split, and that would modify the archive workflow, which is out of scope.
4A. The read side.

The day's held and eligible-but-unselected tokens are in observability.db on trading-state (tables position_snapshot and candidate_snapshot).
trading-state is in a public repository, so reading it needs no credential. This audit has read it anonymously throughout.
The default GITHUB_TOKEN (contents: read) of Intraday-trading also reaches it.
The rule as written only covers a write credential held in the archiver job. It says nothing about read-only credentials, and nothing about any other job. Since the read needs no credential, the read side raises no separation issue under any reading.
Verdict on Option B: OPTION B — NOT CURRENTLY FEASIBLE as specified.

No destination is both durable and available today under the strict per-job reading. Artifacts expire before the review; C does not exist; D would modify the archive.
Destination A is available today only if the separation rule is read as confined to the archiver. That reading is outside what the rule currently covers, and it needs an explicit decision before Option B can be called feasible. That decision is not made here.
The D contingency is unchanged and independent: ONE_MINUTE NFO candles are NOT ESTABLISHED.
5. Floor-binding denominator correction
7 of 28 post-stabilization Trailing stop exits with a recorded high-water had the floor as the binding level (25.0%). Post-stabilization means exits from 2026-08-28 onward, verified by code_sha.

The original "8 of 52" is retained only as a historical audit observation. Its denominator mixed eras, and it applied the floor to two sessions (08-26 and 08-27) that ran without it.

The same correction resolves the Phase 6B report's one unexplained formula mismatch.

The mismatched trade was RELIANCE29SEP261310CE on 2026-08-26 (Δ₹0.1096).
It ran on e240b458, so it exited at the raw trail, and my check wrongly applied the floor to it.
With the verified boundary, the formula reproduces 52 of 52 exactly. That mismatch is no longer NOT ESTABLISHED.
6. Boundary-table cross-reference
To be placed directly below the §3 table of hypothetical activations:

This table gives only the mathematical stop location at activation. It does not establish that earlier activation is economically desirable. An earlier analysis of earlier trail activation on observed winners (referred to as U-014b) found that changing activation can materially change which exits occur. That analysis is part of the Phase 6A analytical discussion and is not a durable repository artifact. A search of the tree, every commit message on all branches, and a pickaxe search of history for its figures and worked example found no record. The table must not be read as evidence that lower activation is preferable.

6A. Supersession map
Amendment item	Section of the original report	What it replaces	Status
1	§6, A2 heading "Whole book, adding the Trailing stop label and the recorded high_water_at_exit"	The wording. The figures reproduce exactly.	CORRECTED wording; figures UNCHANGED
1	§6, the §12A.1 three-era split (52/121, 35/83)	Era boundary moved to the verified 2026-08-28; now 59/135 and 28/69	CORRECTED
2	§6 C1 "74 = 61.2%" against Phase 6A's "41 of 51 never reached +5%"	Both stand. Same field; the gap is selection.	QUALIFIED
3	§6, §12A.1 interpretation	Adds excursion-not-executability, the first-poll exposure (2 of 96; 1 through intra-interval capture), the fallback check (0 of 51) and per-position confirmation	QUALIFIED
4	§12 / §16 Option B ("hold Angel credentials; must keep the rule…")	Persistence unresolved; artifact retention fails the review date; rule scope needs a decision. Status becomes NOT CURRENTLY FEASIBLE as specified.	CORRECTED
5	§3, "In 8 of the 52 checked trailing exits, the floor was the level that bound"	7 of 28 (25.0%) on the verified boundary	CORRECTED
5	§1, "51 of 52 … The one mismatch (Δ₹0.11) is NOT ESTABLISHED"	52 of 52; the mismatch was an era misassignment	CORRECTED
5	§1, §3 and the §6 stop-floor classification, "since 2026-08-26" / "before 2026-08-26"	2026-08-28: committed 08-26, merged 08-27 22:20, first ran 08-28	CORRECTED
6	§3, the boundary table	Cross-reference added; U-014b stated as a non-repository source	QUALIFIED
7	§20, disposition and D contingency	—	UNCHANGED
Outside this amendment's scope, flagged only: the earlier options-book P&L forensic split its "sequence around the merge" at 2026-08-26. By the same code_sha evidence, the stabilization code first ran on 2026-08-28. That report's pre/post split is therefore off by two sessions.

7. Disposition

C — MATERIAL INSTRUMENTATION REQUIRED
Unchanged. The D contingency also stands unchanged: if ONE_MINUTE NFO candles are unavailable, Option B cannot proceed as designed, and tick-stream architecture remains the alternative, needing its own feasibility analysis. The quote-API rate limits and ONE_MINUTE availability remain open questions; neither was probed here.