> **PROVENANCE — preserved document**
>
> - Document: Phase 6B errata
> - Origin: operator-supplied copy of Claude Code session output
> - Supplied file: `Textfiles/phase6b_errata.txt`
> - Supplied file sha256: `c79170e1764684bf706b88cc6fe8e38cc776c70523a33237212e8bf19ebd144f`
> - Date preserved: 2026-10-03
> - Changes below the marker: none. The string shared with the archive repository's name appears once (supplied line 110) as a WORKFLOW FILE reference and is retained.
> - Git stores this file with LF line endings (core.autocrlf=true); the supplied file uses CRLF. No other byte of the text below the marker differs, except the changes listed above.

<!-- END PROVENANCE HEADER -->
PHASE 6B — ERRATA
Read-only. No code, config, schema, workflows, trading-state, archive, credentials or prior document modified. No Angel API calls. No P&L. No commits. Ledger: 71.

1. Starting state
item	value
Repository HEAD	441cc531e48ce49b758d625856872f932fc44a11, branch main, git status clean
trading-state commit read	9c583bbfbcd3e1415f079656b69b51ed3f4fcf22 (2026-09-28 16:15 IST)
How it was pinned	Local options_trades.db (blob 662c0667…) and observability.db (blob 1f2cc10c…) match that commit blob-for-blob
Remote trading-state HEAD now	ce560692… (2026-10-02). It has advanced and was not read; trades after 2026-09-28 are excluded
count	query	result	expected
closed trades	SELECT COUNT(*) FROM options_trades	255	255 ✓
live-price	… WHERE price_source IS NOT NULL	221	221 ✓
model-priced	… WHERE price_source IS NULL	34	34 ✓
trailing	… WHERE reason='Trailing stop'	96	96 ✓
All match. Proceeding. Every data figure below comes from trading-state 9c583bbf.

E1. Model-priced (34) vs live-price (221)
A. The 34 (WHERE price_source IS NULL):

entered	exited	n	trailing
2026-07-28	2026-07-29 09:15	8	8
2026-07-29	2026-07-29	18	0
2026-07-30	2026-07-30	8	0
All 34 are in ERA 1.
By reason: 21 Expiry close-out, 8 Trailing stop (as expected), 4 Initial stop, 1 Trend reversal exit.
The first live-price exit was 2026-07-31 10:31.
B. Price basis, from the code that actually ran. code_sha is not recorded before 2026-08-21, so the code is identified from commit history, not from a record. The rule used: the first-parent main HEAD going into each session, on the assumption that each run used main at trigger time.

session	commit in force	entry price	exit price	label high-water	trigger mark
07-28	ed4fbc3f	MODEL-DERIVED (Black–Scholes; IV from implied_vol(spot, atm_iv=0.20); ed4fbc3f:options_trader.py:149,166-168)	— (nothing closed: close crashed, per de378311)	MIXED, in the wrong units — current_price = info["spot"] * theta_decay, so the underlying's price stood in for the premium (ed4fbc3f:306-314)	the same spot × theta value (ed4fbc3f:306-311)
07-29	de378311	MODEL-DERIVED (de378311:150,167-169)	MODEL-DERIVED (Black–Scholes, de378311:315-320,340-346)	max(stored, Black–Scholes) (de378311:323); for the 8 carried positions the stored value was the 07-28 spot-scale figure	MODEL-DERIVED
07-30	577a3a5f	MODEL-DERIVED (Black–Scholes, with Angel IV when available; 577a3a5f:166-168,191-197)	MODEL-DERIVED (577a3a5f:352-358,378-384)	MODEL-DERIVED (577a3a5f:361)	MODEL-DERIVED
de378311's own commit message describes the 07-28 defect: "current_price was set directly to info['spot'] (the UNDERLYING's raw price …) and used as if it were the OPTION PREMIUM … off by orders of magnitude."

The data match that mechanism. All 8 trailing exits were entered in one cycle (07-28 09:31) and exited at the first cycle of 07-29 (09:15). All carry Trailing stop even where exit/entry is 0.406, 0.671, 0.886 and 1.000. A correctly armed trail cannot produce those ratios.

C. Consequences.

The §12A.1 "actual print" excursion claim does not apply to the 8 model-era trailing exits. Their labels came from comparing the underlying's price with the option premium. Whether they reached +10% in premium terms is NOT ESTABLISHED (8 trades).

The clean excursion bound, live trades only: Trailing stop = 88 of 221 (39.8%), matching the expected figure.

era	live trailing	%
ERA 1	1 of 17	5.9%
ERA 2	59 of 135	43.7%
ERA 3	28 of 69	40.6%
A2 union split by price regime:


             +5%  +7%  +8%  +10%  +12%  +15%  +20%
live  (221)  127  116  110    99    63    47    35
model  (34)   19   19   19    19    15    13    11
ERA 1 live (17)    4    4    4     4     3     2     2
ERA 1 model (34)  19   19   19    19    15    13    11
Reconciliation: the live row reproduces published B exactly at all seven levels, and live + model reproduces published A2 exactly at all seven levels.

ERA 1's flat union of 23 (from +5% through +10%), from the split alone: 19 of the 23 come from model-priced trades; the 4 live trades are also flat. The flatness is mostly, but not only, model-priced. This is descriptive.

Exit prices for all 34 are model output. Their E (exit-return) evidence is a model value, not an observed price. 15 of the 34 have E ≥ +5%.

D. All 34 are kept, and shown separately.

E2. First-poll denominator (amendment §3A)
A. Measurable means trailing exits whose position_snapshot rows include the first post-entry poll. Concretely: the entry is at or after the position_snapshot start (MIN(evaluated_at) = 2026-08-17 15:15:59.164580), and the first snapshot falls in the first cycle that starts after entry (checked against cycle.process_started_at).

Trailing exits with any path data: 52
Straddlers (entered before the start, exited after): 1 — id 136, INFY25AUG261140PE, entered 11:56:52, exited 08-17 15:15:59. Its first snapshot is +25.70% via INTRA_INTERVAL_HIGH, but that snapshot is not its first post-entry poll. Excluded.
Path data from entry but first snapshot not in the first post-entry cycle: 0
Measurable: 51. The denominator is 51, not 52, because of the straddler.
B / C. Recomputed on the 51:

check	amendment figure	corrected
first poll labelled INTRA_INTERVAL_HIGH	5	4 / 51 (7.8%)
armed on the first poll	2	1 / 51 (2.0%)
armed on the first poll via intra-interval capture	1	0 / 51 (0.0%)
entry quote with zero or missing session high	0 of 51, 1 NE	0 / 51 checkable; 0 NOT ESTABLISHED
The straddler accounts for one count in each of the first three rows. It is also the amendment's "1 NOT ESTABLISHED" fallback case, because it entered before quotes were persisted. Once it is excluded, that case disappears.

D. NOT ESTABLISHED: 45 of 96. That is the 44 with no path data plus the one straddler. No first-poll rate is expressed against 96.

E3. Option B wording
The amendment's §4 verdict is replaced with:

"NOT AVAILABLE AS BUILT — requires either a rule-scope decision for A or new infrastructure for C."

Added: GitHub artifact retention is a rolling 90-day limit. Every artifact expires 90 days after it is created, so no dataset held in artifacts ever reaches back further than 90 days, whatever the review date. The 2027-01-04 review is one instance of this problem, not the whole of it.

This is a wording correction, not a change of disposition. No destination was re-analysed.

E4. Identity of the 121
A.

P = trades with at least one position_snapshot row for the token inside the entry-to-exit window.
H = trades with a non-NULL exit_snapshot.high_water_at_exit.
exit_snapshot has no trade id, so H is joined on (trading_symbol, exit date, ROUND(exit_price, 2)).
B.


|P| = 121   |H| = 121   |P ∩ H| = 121   |P \ H| = 0   |H \ P| = 0
C. Closed trades exiting at or after 2026-08-17 15:15:59 with zero in-window snapshots: 0.

D. Straddlers across all reasons: 4 — ids 135, 136, 137, 138. Only id 136 is a trailing exit. These four have path data only from 15:15:59.

E. IDENTICAL. Statements that depend on identity stand, including "the 121 are coverage/date-selected, not filtered on outcome". Separately qualified: 4 of the 121 (ids 135–138) have truncated paths, so "path-observed" means at least one snapshot, not "path from entry", for those 4.

E5. Superseded facts (recorded only)
The stabilization floor first ran in production on 2026-08-28. It was committed in 8428c38f on 2026-08-26, merged in 0d389482 on 2026-08-27 at 22:20 IST, and verified by code_sha (e240b458 ran on 08-26 and 08-27; 0d389482 on 08-28). This supersedes every "since 2026-08-26" and "before 2026-08-26" floor boundary.
The earlier options-book P&L forensic split pre/post-merge at 2026-08-26. It needs its own scoped correction later. Not recomputed here.
U-014b is not a durable repository artifact and must not be cited as repository evidence. Its figures are not reproduced here.
intraday.yml at 441cc531: the workflow-level permissions: contents: write (lines 87–88) applies to the trade job (line 95), which receives ANGEL_* secrets at lines 154–157. The written separation rule (scrip-archive.yml:16-17 at 441cc531) needs an explicit scope decision before the Option A architecture is used. Recorded only; no recommendation.
E6. Durability
Neither the Phase 6B report nor the scoped amendment exists at any committed path.

git grep -i "PHASE 6B" across every local and remote branch: no matches.
git log --all --grep="phase 6b": no matches.
Search of the repository working tree and the session scratchpad: no files.
Both exist only as conversation output. Nothing was committed.

3. Supersession map
erratum	document	section	superseded statement	replacement	status
E1	6B report	§6, §12A.1	"Trailing stop" bound of 96 of 255 treated as excursion evidence for all 96	The excursion bound applies to live trades only: 88 of 221. The 8 model-era labels came from the unit-mismatch defect; their excursion is NOT ESTABLISHED	CORRECTED
E1	amendment	§3, ¶2	"The high-water that justifies the label is … an observed polled bid or LTP, or the exchange's own session traded high. Both are actual prints."	True for live-price trades only; false for the 8 model-era trailing exits	CORRECTED
E1	amendment	§1C, §12A.1 era split	ERA 1 "9 of 51 (17.6%)"	Live ERA 1: 1 of 17 (5.9%). 8 of the 9 are model-era defect labels	QUALIFIED
E1	6B report / amendment	§6 A2; amendment §1D	A2 wording, with no price-regime split	A2 includes 34 model-priced trades (E evidence is model output; their 8 T labels are defect-derived). Live row = B; model row = 19/19/19/19/15/13/11	QUALIFIED
E1	amendment	§1C	ERA 1 union of 23 flat from +5% to +10%	19 of the 23 are model-priced; the 4 live are also flat	QUALIFIED
E2	amendment	§3A	5 / 2 / 1; fallback 0 of 51 with 1 NE; denominator 52	4/51, 1/51, 0/51; fallback 0/51 checkable with 0 NE; straddler id 136 excluded; 45 of 96 NOT ESTABLISHED	CORRECTED
E2	amendment	§6A row 3	"first-poll exposure (2 of 96; 1 through intra-interval capture)"	1 of 51 measurable (2.0%); 0 of 51 via intra-interval capture; 45 of 96 NOT ESTABLISHED. Never a % of 96	CORRECTED
E3	amendment	§4 verdict	"OPTION B — NOT CURRENTLY FEASIBLE as specified"	"NOT AVAILABLE AS BUILT — requires either a rule-scope decision for A or new infrastructure for C."	CORRECTED (wording only)
E3	amendment	§4, destination B	"expires 2026-12-31, four days before the review"	A rolling 90-day limit; no artifact-held data older than 90 days ever exists	QUALIFIED
E4	amendment	§2, selection mechanisms	"the 121 are coverage/date-selected, not filtered on outcome"	Identity verified: P = H = 121	UNCHANGED
E4	amendment / 6B report	§2; 6B §6 C1/C2	"path-observed" read as a path from entry	4 of the 121 (ids 135–138) have paths only from 2026-08-17 15:15:59	QUALIFIED
E5.1	6B report	§1, §3, §6 floor classification	"since / before 2026-08-26"	2026-08-28 (already corrected in amendment §5; reaffirmed)	CORRECTED
E5.2	amendment	out-of-scope note	P&L forensic split at 2026-08-26	Pending its own scoped correction	NOT ESTABLISHED (not recomputed)
E5.3	amendment	§6	U-014b cited as a non-repository source	Unchanged	UNCHANGED
E5.4	amendment	§4, repository facts	Trader job holds contents: write and ANGEL_*; rule scope undecided	Recorded with line references at 441cc531	UNCHANGED
—	6B report / amendment	§20 / §7	Disposition C, D contingency	—	UNCHANGED
4. Disposition

C — MATERIAL INSTRUMENTATION REQUIRED
The D contingency is unchanged: if ONE_MINUTE NFO candles are unavailable, Option B cannot proceed as designed, and tick-stream architecture remains the alternative, needing its own feasibility analysis. ONE_MINUTE NFO candles remain NOT ESTABLISHED.