"""Regression test for the OI retention classifier.

The 2026-09-06 run is the regression case. It printed a correct attribution
column showing that the paired candle call failed wherever OI failed, and
then emitted `RETENTION: weeks` anyway. These tests assert that the rows from
that exact run now return INDETERMINATE, and that no depth label can be
reached from rows whose candle control failed.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from angel_oi_classify import classify, INDETERMINATE, TRACKS

FAILED = 0


def check(name, got, want):
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"         got  {got!r}")
        print(f"         want {want!r}")


# ------------------------------------------------------------------------
# THE REGRESSION CASE. Token 42635, NIFTY08SEP2623900CE, run 2026-09-06.
# The served prefix shows a contract coming into existence (75 -> 74 -> 49 ->
# 18 -> 22 -> 0); the three boundary rows quoted in the review had OI=0 AND
# candles=0. Only those boundary rows decide the assertion - the served rows
# are present so the case is realistic, not so it passes.
# ------------------------------------------------------------------------
RUN_20260906 = [
    {"offset": 1,  "date": "2026-09-04", "oi_rows": 75, "candle_rows": 75},
    {"offset": 2,  "date": "2026-09-03", "oi_rows": 74, "candle_rows": 74},
    {"offset": 3,  "date": "2026-09-02", "oi_rows": 49, "candle_rows": 49},
    {"offset": 7,  "date": "2026-08-28", "oi_rows": 18, "candle_rows": 18},
    {"offset": 14, "date": "2026-08-21", "oi_rows": 22, "candle_rows": 22},
    {"offset": 21, "date": "2026-08-14", "oi_rows": 0,  "candle_rows": 0},
    {"offset": 25, "date": "2026-08-12", "oi_rows": 0,  "candle_rows": 0},
    {"offset": 30, "date": "2026-08-07", "oi_rows": 0,  "candle_rows": 0},
]

print("=" * 74)
print("OI RETENTION CLASSIFIER - regression tests")
print("=" * 74)

label, reason, ev = classify(RUN_20260906)
check("2026-09-06 run no longer yields a depth label", label, INDETERMINATE)
print(f"         reason: {reason[:96]}...")

# The three boundary rows alone must also be INDETERMINATE.
label2, _, _ = classify(RUN_20260906[5:])
check("boundary rows alone -> INDETERMINATE", label2, INDETERMINATE)

# A depth label must be unreachable from candle-failed rows, at any depth.
for off in (7, 21, 60, 400):
    lab, _, _ = classify([{"offset": off, "oi_rows": 0, "candle_rows": 0}])
    check(f"candles=0 at {off}d cannot produce a depth label",
          lab, INDETERMINATE)

# ---- positive controls: the classifier must still work when it should ----
DIVERGES = [
    {"offset": 60,  "oi_rows": 75, "candle_rows": 75},
    {"offset": 90,  "oi_rows": 0,  "candle_rows": 75},   # OI stops, candles do not
    {"offset": 180, "oi_rows": 0,  "candle_rows": 75},
]
label3, reason3, ev3 = classify(DIVERGES)
check("true divergence at 90d -> 'months'", label3, "months")
check("evidence carries only the diverged rows", len(ev3), 2)

TRACKING = [
    {"offset": 60,  "oi_rows": 75, "candle_rows": 75},
    {"offset": 180, "oi_rows": 67, "candle_rows": 67},
    {"offset": 365, "oi_rows": 75, "candle_rows": 75},
]
label4, reason4, _ = classify(TRACKING)
check("OI served wherever candles are -> TRACKS", label4, TRACKS)

# Mixed: candle-failed rows must not drag a tracking result into a depth label.
MIXED = TRACKING + [
    {"offset": 400, "oi_rows": 0, "candle_rows": 0},
    {"offset": 500, "oi_rows": 0, "candle_rows": 0},
]
# A shallow no-divergence sweep must NOT be reported as TRACKS: the
# 2026-09-06 run reached only 14d, which proves nothing about retention.
SHALLOW = [
    {"offset": 1,  "oi_rows": 75, "candle_rows": 75},
    {"offset": 14, "oi_rows": 22, "candle_rows": 22},
    {"offset": 30, "oi_rows": 0,  "candle_rows": 0},
]
check("shallow no-divergence sweep -> INDETERMINATE, not TRACKS",
      classify(SHALLOW)[0], INDETERMINATE)
label5, _, _ = classify(MIXED)
check("candle-failed rows do not turn TRACKS into a depth label",
      label5, TRACKS)

# Shallow divergence with no served row is an upper bound, and says so.
label6, reason6, _ = classify([{"offset": 60, "oi_rows": 0, "candle_rows": 75}])
check("no served row -> still a label", label6, "months")
check("...and it is flagged as an upper bound",
      "upper bound" in reason6, True)

# Non-monotone divergence must be flagged rather than silently reduced.
NONMONO = [
    {"offset": 60,  "oi_rows": 0,  "candle_rows": 75},
    {"offset": 180, "oi_rows": 75, "candle_rows": 75},
]
label7, reason7, _ = classify(NONMONO)
check("deeper-served-than-boundary is flagged NON-MONOTONE",
      "NON-MONOTONE" in reason7, True)

check("empty input -> INDETERMINATE", classify([])[0], INDETERMINATE)

print("=" * 74)
print(f"RESULT: {'PASS' if FAILED == 0 else f'{FAILED} FAILED'}")
print("=" * 74)
sys.exit(1 if FAILED else 0)
