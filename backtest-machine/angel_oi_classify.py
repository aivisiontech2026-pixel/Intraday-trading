"""Retention classification for the OI probe. Pure, testable, no I/O.

WHY THIS IS A SEPARATE MODULE. The previous probe printed a correct
attribution column and then emitted a label its own column excluded:

    21d  2026-08-14   OI=0  candles=0  INDET
    25d  2026-08-12   OI=0  candles=0  INDET
    30d  2026-08-07   OI=0  candles=0  INDET
    -> RETENTION: weeks

The boundary was computed from OI row counts alone; the paired candle result
was displayed but never consumed. That is a structural defect, not a typo,
and the fix is structural: the classifier below can only see rows whose
paired candle call SUCCEEDED. A row with candle_rows == 0 cannot separate

    A  the contract was not listed yet
    C  it was listed but carried no open interest

from retention, so it is filtered out before any depth label can be reached.
It is not possible to emit "weeks", "months" or any other depth label from
INDET rows, because those rows are gone before the label is chosen.
"""

# Ordered shallow -> deep. Boundaries are in days.
DEPTH_LABELS = (
    (10, "~7 days"),
    (45, "weeks"),
    (200, "months"),
    (400, ">=1 year"),
    (10 ** 9, "multi-year"),
)
INDETERMINATE = "INDETERMINATE"
# A "they stop together" result only carries information if the sweep reached
# a depth where retention could plausibly bind. 60d is already demonstrated as
# served (token 71501), so anything shallower is uninformative by construction.
MIN_DEPTH_FOR_TRACKS = 60
TRACKS = "OI TRACKS CANDLE AVAILABILITY"


def _depth_label(days):
    for lim, name in DEPTH_LABELS:
        if days <= lim:
            return name
    return "multi-year"


def classify(rows):
    """(label, reason, evidence_rows) from paired OI/candle observations.

    `rows` is an iterable of dicts with at least:
        offset      int   days back
        oi_rows     int   rows returned by getOIData
        candle_rows int   rows returned by getCandleData, SAME token/window

    Only rows with candle_rows > 0 can contribute to a depth label.
    """
    rows = [dict(r) for r in rows]
    usable = [r for r in rows if int(r.get("candle_rows", 0)) > 0]
    if not usable:
        return (INDETERMINATE,
                "no offset produced a successful paired candle call, so cause A "
                "(not yet listed) and cause C (no open interest) were never "
                "excluded; retention was not measured",
                [])

    diverged = [r for r in usable if int(r["oi_rows"]) == 0]
    served = [r for r in usable if int(r["oi_rows"]) > 0]

    if not diverged:
        deepest = max(int(r["offset"]) for r in usable)
        # "They stop together" only means something if the sweep reached a
        # depth where retention COULD have bound. On the 2026-09-06 run the
        # deepest served offset was 14d on a contract listed three weeks
        # earlier: OI tracked candles across a fortnight and the deeper
        # offsets failed on both sides. That is not evidence that retention
        # is unbounded, it is evidence that nothing was tested. The threshold
        # is 60d because the review already demonstrated OI served at 60d on
        # token 71501, so a shallower sweep adds nothing to what is known.
        if deepest < MIN_DEPTH_FOR_TRACKS:
            return (INDETERMINATE,
                    f"no divergence, but the deepest served offset is only "
                    f"{deepest}d - shallower than the {MIN_DEPTH_FOR_TRACKS}d "
                    f"already known to be served. Every deeper offset failed on "
                    f"BOTH sides, which cannot separate a listing wall from "
                    f"retention. The sweep did not reach a depth where "
                    f"retention could bind",
                    usable)
        return (TRACKS,
                f"at every offset where candles were served, OI was served too "
                f"(deepest {deepest}d, {len(usable)} paired observations). "
                f"Retention is not the binding constraint within the tested "
                f"range; contract availability is",
                usable)

    boundary = min(int(r["offset"]) for r in diverged)
    label = _depth_label(boundary)
    note = ""
    if not served:
        note = (" NOTE: no served row inside the sweep, so the true boundary "
                "may be shallower than the shallowest offset tested; this "
                "label is an upper bound")
    deep_served = [r for r in served if int(r["offset"]) > boundary]
    if deep_served:
        note += (f" NON-MONOTONE: OI was served at "
                 f"{sorted(int(r['offset']) for r in deep_served)}d, deeper "
                 f"than the {boundary}d divergence - a single retention wall "
                 f"does not explain this")
    return (label,
            f"OI absent while candles present at "
            f"{sorted(int(r['offset']) for r in diverged)}d; the contract "
            f"existed and traded in those windows, so A and C are excluded "
            f"and only retention remains. Boundary {boundary}d.{note}",
            diverged)
