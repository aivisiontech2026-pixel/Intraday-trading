> **PROVENANCE — preserved document**
>
> - Document: E4 identity verification
> - Origin: operator-supplied copy of Claude Code session output
> - Supplied file: `Textfiles/phase6b_e4_identity.txt`
> - Supplied file sha256: `3bb4dd851adb52c9d2d9d5c2bd3da26d7dc826fb6c97e08871d10e4d287249eb`
> - Date preserved: 2026-10-03
> - Changes below the marker: none.
> - Git stores this file with LF line endings (core.autocrlf=true); the supplied file uses CRLF. No other byte of the text below the marker differs, except the changes listed above.

<!-- END PROVENANCE HEADER -->
PHASE 6B — E4 IDENTITY VERIFICATION
Read-only. Nothing in the code, config, trading-state, archive, credentials or any prior document was modified. No API calls, no P&L, no commits. Ledger: 71.

1. Starting state
item	value
Repository HEAD	441cc531e48ce49b758d625856872f932fc44a11, main, git status clean
trading-state read	9c583bbfbcd3e1415f079656b69b51ed3f4fcf22 (resolves locally)
options_trades.db	local 662c0667… = blob at 9c583bbf → MATCH
observability.db	local 1f2cc10c… = blob at 9c583bbf → MATCH
Counts	255 closed / 221 live / 34 model / 96 Trailing stop → all as expected
All checks pass. Proceeding.

2. Checks
C1. Schema. From PRAGMA table_info(exit_snapshot):

id, cycle_id, token, trading_symbol, decided_at, exit_reason, exit_price, trigger_value, high_water_at_exit, peak_source, quote_snapshot_id, pnl, entry_price, initial_stop_level, dist_to_initial_stop, underlying_spot, underlying_direction

There is no trade id or position id. Two columns give a stronger key than the composite:

token, which options_trades also has (per PRAGMA table_info(options_trades));
decided_at, the exit timestamp.
So a second key was built:

K1 (the errata's key) — (trading_symbol, exit date, ROUND(exit_price, 2))
K2 — token AND |options_trades.exit_time − exit_snapshot.decided_at| ≤ 5 s
C4 and C5 were run under both keys.

C2. Raw counts, with no join involved.


SELECT COUNT(*) FROM exit_snapshot                                      -> 121
SELECT COUNT(*) FROM exit_snapshot WHERE high_water_at_exit IS NOT NULL -> 121
SELECT MIN(decided_at), MAX(decided_at) FROM exit_snapshot              -> 2026-08-17T15:15:59.165999 .. 2026-09-25T14:13:40.277805
|H| = 121 holds without any join. No exit_snapshot row could have been lost silently.

C3. Is the K1 key unique on both sides?


options_trades WHERE exit_time >= '2026-08-17T15:15:59.164580'    n=121   duplicate key groups 0   max rows/group 1
exit_snapshot  WHERE high_water_at_exit IS NOT NULL              n=121   duplicate key groups 0   max rows/group 1
How the key is derived:

Exit date is exit_time[:10] on one side and decided_at[:10] on the other. Both are naive ISO strings written by datetime.now() in the same trader process, which runs with TZ: Asia/Kolkata (intraday.yml:98 at 441cc531). So both are IST.
exit_price is rounded to 2 decimals on both sides before comparison.
C4. Matching in both directions.

check	K1	K2
exit_snapshot rows matching no trade	0	0
Trades in P matching no exit_snapshot row	0	0
Trades matching more than one exit_snapshot row	0	0
exit_snapshot rows matching more than one trade	0	0
Trades exiting at or after the start but not in P	0	—
Under K2, all 121 matches are unique. The timestamp gap across matched pairs is 61 to 171 microseconds (median 101): both rows are written in the same close_option call. exit_reason agrees with options_trades.reason on 121 of 121 pairs.

C5. Mapping test.

condition	result
Raw count from C2 = 121	true
No duplicate groups on either side	true
No unmatched rows in either direction	true
Matched trade set = P, by options_trades.id	true
K1 and K2 produce the same mapping, pair for pair	true
3. Verdict

ONE-TO-ONE
Every C5 condition holds under the errata's key and under the independent token-plus-timestamp key. The two keys agree pair for pair, and the 121 count is established without relying on a join. The errata's E4 verdict (IDENTICAL) stands. No trade is NOT ESTABLISHED.

All figures that depend on this identity stand unchanged:

"the 121 are coverage/date-selected, not filtered on outcome"
the A2 H column at all seven levels
Phase 6B C2 (79 of 121 at +5%)
the high-water coverage figures (121 of 255; 52 of 96)
The errata's separate qualification still applies: 4 of the 121 (ids 135–138) have path data only from 2026-08-17 15:15:59.

4. Supersession-map row
erratum	document	section	superseded statement	replacement	status
E4	errata	E4, verdict	"IDENTICAL", based on a join key never shown to be unique	Proven one-to-one: raw count 121 with no join; K1 unique on both sides; no unmatched rows either way; matched set = P by id; an independent key (token + exit timestamp, 61–171 µs apart) gives the identical mapping pair for pair	UNCHANGED
5. Disposition

C — MATERIAL INSTRUMENTATION REQUIRED
The D contingency is unchanged: if ONE_MINUTE NFO candles are unavailable, Option B cannot proceed as designed, and tick-stream architecture remains the alternative, needing its own feasibility analysis. ONE_MINUTE NFO candles remain NOT ESTABLISHED.