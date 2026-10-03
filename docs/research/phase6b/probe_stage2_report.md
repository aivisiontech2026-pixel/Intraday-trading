> **PROVENANCE — preserved document**
>
> - Document: ONE_MINUTE NFO feasibility probe — Stage 2 report (accepted)
> - Origin: operator-supplied copy of Claude Code session output
> - Supplied file: `Textfiles/probe_stage2_report.txt`
> - Supplied file sha256: `766119920898865b20b755bca0a14fb410119a306281bfc1f5789b583d0d35a1`
> - Date preserved: 2026-10-03
> - Changes below the marker: MARKET-DATA OMISSION (A3), marked in place: on supplied lines 54-58 ("first three bars of each request") the numeric bar values are replaced with `[bar values omitted from public copy — raw market data; see private raw artifact]`; each line's request label and the three bar timestamps are kept. Counts, timestamps, field structure, missing-minute lists, out-of-session timestamps, verdicts, latencies and metadata are unchanged. The string shared with the archive repository's name appears once (supplied line 19) as a WORKFLOW / CONCURRENCY-GROUP name and is retained.
> - Git stores this file with LF line endings (core.autocrlf=true); the supplied file uses CRLF. No other byte of the text below the marker differs, except the changes listed above.

<!-- END PROVENANCE HEADER -->
ONE_MINUTE NFO CANDLE PROBE — STAGE 2 REPORT
No commit of the artifact or this report. probe_out/ is untracked. No re-run and no further Angel calls. HEAD is still f23575d8. Ledger: 71.

Integrity checks (done before reading)
Zip hash: angel-candle-probe.zip, 26,236 bytes, sha256 e09a816fbab65004f17623cb86093c1c637ae9205f40f1ec0f8e8b9674829bee. This matches the upload log, and GitHub's own artifact metadata gives the same digest.
Contents: one member, probe_result.json (183,939 bytes).
Credential scan of the artifact:
0 JWT shapes, 0 Bearer headers, 0 X-PrivateKey/IP/MAC headers.
0 session- or credential-key names (jwtToken, refreshToken, feedToken, access_token, api_key, totp and similar).
0 token-like runs of 32 characters or more.
The probe's own runner-side scan checked the exact credential values (the short numeric PIN excepted) and recorded {secret_values_found: 0, jwt_shapes_found: 0, bearer_found: 0}. Raw bodies were kept, and no SCAN_FAILED.txt was written.
Credential scan of the pasted log: the four ANGEL_* variables appear only as GitHub * masks. There are no JWT shapes, Bearer headers or token-like strings, and no <redacted> substitutions.
Clean. Proceeding.

1. Commit, run and preconditions
item	value
Commit	f23575d8e9a6ab3ece70bf2837b4fb51623a4cc2. Both files are hash-bound to the reviewed Stage 1 output, and the pushed SHA equals the local SHA.
Push-triggered runs	0 on the head SHA, and 0 created between the push and the check
Preconditions (17:49:27Z = 23:19 IST)	Outside market hours; nothing active in intraday-trader, scrip-archive or angel-candle-probe
Run	37044455850, workflow_dispatch, head f23575d8… (per GitHub metadata), created 17:59:49Z (23:29:49 IST), completed / success
Probe execution	23:30:20 → 23:30:27 IST; login ok; 5 getCandleData calls (cap 12); no retries; no stop
2. Contracts
These were selected before any request, from trading-state ce560692….

symbol	token	exch	expiry	role
E	RELIANCE-EQ	2885	NSE	—	control (#1)
A	BANKNIFTY27OCT2654200PE	49420	NFO	2026-10-27 (monthly)	control (#2); verdict (#3)
B	RELIANCE27OCT261180PE	106673	NFO	2026-10-27 (monthly)	#4
W	NIFTY06OCT2622550PE	40700	NFO	2026-10-06 (weekly)	#7′
X	NOT ESTABLISHED	—	—	—	#5 and #6 not run
3. Requests
Per-request stdout, verbatim (no <redacted> substitutions present):


  #1  E NSE ONE_MINUTE  2026-10-01 09:15 -> 2026-10-01 15:30  status=True code=- bars=361 in-session=361 in-session+ohlc=361
  #2  A NFO FIVE_MINUTE 2026-10-01 09:15 -> 2026-10-01 15:30  status=True code=- bars=76 in-session=75 in-session+ohlc=75
  #3  A NFO ONE_MINUTE  2026-10-01 09:15 -> 2026-10-01 15:30  status=True code=- bars=374 in-session=373 in-session+ohlc=373
  #4  B NFO ONE_MINUTE  2026-10-01 09:15 -> 2026-10-01 15:30  status=True code=- bars=375 in-session=374 in-session+ohlc=374
  #5  NOT RUN - contract X not established
  #6  NOT RUN - contract X not established
  #7'  W NFO ONE_MINUTE  2026-10-01 09:15 -> 2026-10-01 15:30  status=True code=- bars=376 in-session=375 in-session+ohlc=375
  getCandleData calls made: 5 (cap 12)
  result written: True
From the artifact. Every count recounts identically from the raw bodies.

#	status	code / message	bars	in-session ∧ OHLC	first bar	last bar	fields/bar	latency
1	True	'' / SUCCESS	361	361	2026-10-01T09:15:00+05:30	2026-10-01T15:29:00+05:30	6	1.040 s
2	True	'' / SUCCESS	76	75	…09:15:00+05:30	…15:30:00+05:30	6	0.790 s
3	True	'' / SUCCESS	374	373	…09:15:00+05:30	…15:30:00+05:30	6	0.937 s
4	True	'' / SUCCESS	375	374	…09:15:00+05:30	…15:30:00+05:30	6	0.906 s
7′	True	'' / SUCCESS	376	375	…09:15:00+05:30	…15:30:00+05:30	6	1.003 s
Each bar has six fields [timestamp, open, high, low, close, volume], with an explicit +05:30 offset. The first three bars of each request:

#1 [09:15], [09:16], [09:17] [bar values omitted from public copy — raw market data; see private raw artifact]
#2 [09:15], [09:20], [09:25] [bar values omitted from public copy — raw market data; see private raw artifact]
#3 [09:15], [09:16], [09:17] [bar values omitted from public copy — raw market data; see private raw artifact]
#4 [09:15], [09:16], [09:17] [bar values omitted from public copy — raw market data; see private raw artifact]
#7′ [09:15], [09:16], [09:17] [bar values omitted from public copy — raw market data; see private raw artifact]
Missing in-session minutes. These are the full lists, derived from the kept raw bodies against the 09:15–15:29 grid of 375 minutes.

#	missing	full list
1	14	15:15, 15:16, 15:17, 15:18, 15:19, 15:20, 15:21, 15:22, 15:23, 15:24, 15:25, 15:26, 15:27, 15:28
3	2	11:27, 11:54
4	1	14:16
7′	0	—
(#2 is FIVE_MINUTE: all 75 five-minute slots are present.)

Bars counted in bar_count but outside the session.

#	out-of-session bar timestamps
1	none
2	2026-10-01T15:30:00+05:30
3	2026-10-01T15:30:00+05:30
4	2026-10-01T15:30:00+05:30
7′	2026-10-01T15:30:00+05:30
These are reported only, without interpretation, as the scope requires.

4. Verdicts — rule 1.7, applied jointly (in session AND with OHLC)
Controls: #1 succeeded (status True, 361 bars) and #2 succeeded (status True, 75 joint bars). Both succeeded, so the probe was working.

Monthly verdict (#3, decides the disposition)

AVAILABLE
#3 returned 373 bars inside the 2026-10-01 session, all with OHLC, for token A (BANKNIFTY27OCT2654200PE, monthly). The rule needs at least one. Disposition C stands.

Weekly-contract result (#7′, reported separately and not merged)

AVAILABLE
#7′ returned 375 bars inside the session, all with OHLC, for token W (NIFTY06OCT2622550PE, weekly). The same rule 1.7 conditions and the same controls (#1, #2) apply.

The stock monthly #4 (RELIANCE27OCT261180PE) also returned 374 joint bars. It doesn't bear on either verdict.

5. Expired-contract and lookback results
#5 and #6 (expired contract X): NOT RUN. X is NOT ESTABLISHED, because no 29SEP26 contract was quoted on its expiry day under the trader's nearest_expiry(min_dte=1). Whether ONE_MINUTE candles can be fetched for a contract on or after its expiry remains NOT ESTABLISHED.
Lookback (formerly #7 and #8): removed from the plan before the run, at your Stage 1 amendment. How far back ONE_MINUTE data reaches is NOT ESTABLISHED. Every request here was a single session one day back.
6. Open items, still NOT ESTABLISHED
Fetching on the same day after the close of an expiry day (untestable on a non-trading day, and X was not run).
ONE_MINUTE retrieval for expired contracts (#5 and #6 not run).
ONE_MINUTE lookback depth, and whether the measured 1,611-row silent truncation applies at ONE_MINUTE.
The per-second rate limit of the quote API (getMarketData).
The Option B persistence and separation-rule scope decision (from the Phase 6B errata): unchanged, and outside this probe.
Backlog security item, recorded without action: the shared stream scrubber and GitHub's secret masking both redact short numeric secrets wherever they appear in a log, and the position of the redaction can reveal the value from context. Nothing changed. This run's log showed no such substitutions.
7. Disposition

C — MATERIAL INSTRUMENTATION REQUIRED
The D contingency is not triggered. Rule 1.7 returned AVAILABLE for #3, so the ONE_MINUTE NFO candle availability that Option B depends on is established for a live monthly contract and, separately, for a live weekly contract, for a session one day back. Nothing in this run changes the disposition.

Artifact expiry: created 2026-10-02T18:00:27Z; expires 2026-11-01T18:00:27Z (2026-11-01 23:30:27 IST), per GitHub's metadata, i.e. creation plus 30 days.