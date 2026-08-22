"""S-34b: instrument-master refresh throttling.

    python test_refresh_throttle.py

Exits non-zero on failure, matching the other suites' convention.

S-34a bounds how LONG one refresh may block. S-34b bounds how OFTEN one
is attempted. The property under test is that suppressing an attempt is
never able to change WHAT may be served - a throttled cycle must be
indistinguishable from a cycle whose refresh failed instantly, with S-33
then deciding on its own terms.

The clock is injected everywhere, so every boundary is exact and no test
depends on wall-clock timing. Nothing contacts Angel One.
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import angelone_client as angel
import options_trader as ot

FAILURES = []
TMP = Path(tempfile.gettempdir()) / "throttle_scrip.json"
R = ot.MASTER_REFRESH_RETRY_S          # 1800s


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


# ----------------------------------------------------------- fixtures ---
def rec(name="NIFTY", opt="CE", token="1001", expiry=None):
    e = expiry or (date.today() + timedelta(days=7))
    return {"token": token, "symbol": f"{name}{e:%d%b%Y}".upper() + opt,
            "name": name, "expiry": f"{e:%d%b%Y}".upper(),
            "strike": "2400000", "lotsize": "65", "exch_seg": "NFO",
            "instrumenttype": "OPTIDX"}


def good(expiry=None):
    return [rec(opt="CE", token="1001", expiry=expiry),
            rec(opt="PE", token="1002", expiry=expiry)]


def write_cache(payload, age_h):
    if payload is None:
        if TMP.exists():
            TMP.unlink()
        return
    TMP.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                   encoding="utf-8")
    when = time.time() - age_h * 3600
    os.utime(TMP, (when, when))


def mem_db():
    """A real sqlite meta table - the same shape options_trader creates."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    return c


class BrokenDB:
    """Every access raises, to exercise the unreadable-database path."""

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("injected: database unavailable")


def load(refresh_allowed, download=None, universe=("NIFTY",)):
    """Run the real loader with an injected transport."""
    angel._MASTER_CACHE = None
    angel._MASTER_HEALTH.update(status="UNKNOWN", age_h=None, reason=None,
                                refresh_attempted=False)
    angel.SCRIP_CACHE = TMP
    angel.AVAILABLE = True
    calls = {"n": 0}

    class _Resp:
        def __init__(self, p):
            self._p = p
            self.status_code = 200

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=None):
            b = json.dumps(self._p).encode()
            step = chunk_size or len(b) or 1
            for i in range(0, len(b), step):
                yield b[i:i + step]

    def _get(url, timeout=None, stream=None, allow_redirects=None):
        calls["n"] += 1
        if download is None:
            raise ConnectionError("injected: refresh unavailable")
        return _Resp(download)

    class _Session:
        get = staticmethod(_get)

        def close(self):
            pass

    angel.requests = type("R", (), {"get": staticmethod(_get),
                                    "Session": _Session})
    m = angel.load_instrument_master(list(universe),
                                     refresh_allowed=refresh_allowed)
    return m, angel.master_health(), calls["n"]


def clear():
    if TMP.exists():
        try:
            TMP.unlink()
        except Exception:
            pass


# ------------------------------------------------ throttle arithmetic ---
def test_no_state():
    print("\n[1] no throttle state -> refresh permitted")
    c = mem_db()
    check("permitted", ot.master_refresh_allowed(c, now_epoch=1000.0), True)


def test_active_and_expired():
    print("\n[2][3] active throttle suppresses; expired permits")
    c = mem_db()
    ot.meta_set(c, ot.MASTER_REFRESH_KEY, 1000.0)
    check("1s after failure -> suppressed",
          ot.master_refresh_allowed(c, now_epoch=1001.0), False)
    check("half the interval -> suppressed",
          ot.master_refresh_allowed(c, now_epoch=1000.0 + R / 2), False)
    check("well past the interval -> permitted",
          ot.master_refresh_allowed(c, now_epoch=1000.0 + R * 5), True)


def test_boundary():
    print("\n[4][5][6] retry boundary, driven by an injected clock")
    c = mem_db()
    ot.meta_set(c, ot.MASTER_REFRESH_KEY, 1000.0)
    check(f"just before ({R - 1}s)",
          ot.master_refresh_allowed(c, now_epoch=1000.0 + R - 1), False)
    check(f"exactly at ({R}s) -> permitted (inclusive)",
          ot.master_refresh_allowed(c, now_epoch=1000.0 + R), True)
    check(f"just after ({R + 1}s)",
          ot.master_refresh_allowed(c, now_epoch=1000.0 + R + 1), True)
    check("interval is 30 minutes", R, 1800)


def test_anomalies_fail_open():
    print("\n[7][8][9] every unusable state FAILS OPEN")
    for label, value in (("corrupt / non-numeric", "not-a-number"),
                         ("empty string", ""),
                         ("whitespace", "   "),
                         ("negative", "-5000"),
                         ("absurdly large", "1e18"),
                         ("json blob", '{"ts": 1}')):
        c = mem_db()
        ot.meta_set(c, ot.MASTER_REFRESH_KEY, value)
        got = ot.master_refresh_allowed(c, now_epoch=1000.0)
        check(f"{label} -> refresh permitted", got, True)
    print("      a future timestamp must not suppress indefinitely:")
    c = mem_db()
    ot.meta_set(c, ot.MASTER_REFRESH_KEY, 9_000_000.0)
    check("future timestamp -> permitted",
          ot.master_refresh_allowed(c, now_epoch=1000.0), True)
    print("      clock stepping BACKWARDS mid-session:")
    c = mem_db()
    ot.meta_set(c, ot.MASTER_REFRESH_KEY, 5000.0)
    check("now < failed_at -> permitted",
          ot.master_refresh_allowed(c, now_epoch=4000.0), True)


def test_database_unavailable():
    print("\n[10] database unavailable -> fail open, trading continues")
    check("read failure -> permitted",
          ot.master_refresh_allowed(BrokenDB(), now_epoch=1000.0), True)
    raised = None
    try:
        ot.record_master_refresh(BrokenDB(),
                                 {"refresh_attempted": True, "status": "STALE"},
                                 now_epoch=1000.0)
    except Exception as e:                       # pragma: no cover
        raised = f"{type(e).__name__}"
    check("write failure never raises", raised, None)


def test_process_restart():
    print("\n[11] throttle state survives a process restart")
    path = Path(tempfile.gettempdir()) / "throttle_meta.db"
    if path.exists():
        path.unlink()
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    ot.record_master_refresh(c, {"refresh_attempted": True, "status": "STALE"},
                             now_epoch=1000.0)
    c.commit()
    c.close()
    c2 = sqlite3.connect(path)                   # a NEW process would do this
    check("state readable after reopen",
          ot.master_refresh_allowed(c2, now_epoch=1001.0), False)
    check("and still expires normally",
          ot.master_refresh_allowed(c2, now_epoch=1000.0 + R), True)
    c2.close()
    path.unlink()


# --------------------------------------------------- state transitions ---
def test_failure_sets_timestamp():
    print("\n[12][13] an actual failed refresh sets, then updates, the stamp")
    c = mem_db()
    ot.record_master_refresh(c, {"refresh_attempted": True, "status": "STALE"},
                             now_epoch=1000.0)
    check("set on first failure",
          float(ot.meta_get(c, ot.MASTER_REFRESH_KEY)), 1000.0)
    ot.record_master_refresh(c, {"refresh_attempted": True, "status": "STALE"},
                             now_epoch=2000.0)
    check("updated on the next actual attempt",
          float(ot.meta_get(c, ot.MASTER_REFRESH_KEY)), 2000.0)


def test_throttled_skip_never_restamps():
    print("\n[14] a THROTTLED SKIP must not touch the stamp")
    print("      (otherwise the interval restarts every cycle and the")
    print("       suppression extends itself without bound)")
    c = mem_db()
    ot.record_master_refresh(c, {"refresh_attempted": True, "status": "STALE"},
                             now_epoch=1000.0)
    for t in (1100.0, 1200.0, 1500.0, 1799.0):
        ot.record_master_refresh(c, {"refresh_attempted": False,
                                     "status": "STALE"}, now_epoch=t)
    check("stamp unchanged after 4 skips",
          float(ot.meta_get(c, ot.MASTER_REFRESH_KEY)), 1000.0)
    check("and the interval still expires on time",
          ot.master_refresh_allowed(c, now_epoch=1000.0 + R), True)


def test_success_clears():
    print("\n[15] a successful validated refresh clears the stamp")
    c = mem_db()
    ot.record_master_refresh(c, {"refresh_attempted": True, "status": "STALE"},
                             now_epoch=1000.0)
    ot.record_master_refresh(c, {"refresh_attempted": True, "status": "OK"},
                             now_epoch=1100.0)
    check("cleared", ot.meta_get(c, ot.MASTER_REFRESH_KEY), "")
    check("refresh permitted again immediately",
          ot.master_refresh_allowed(c, now_epoch=1101.0), True)


def test_unusable_payload_is_a_failure():
    print("\n[16] a download that validates as unusable is a FAILURE")
    for status in ("UNIVERSE_MISMATCH", "EMPTY", "CORRUPT"):
        c = mem_db()
        ot.record_master_refresh(c, {"refresh_attempted": True,
                                     "status": status}, now_epoch=1000.0)
        check(f"{status} -> stamp set",
              float(ot.meta_get(c, ot.MASTER_REFRESH_KEY)), 1000.0)


# ------------------------------------------- loader / S-33 interaction ---
def test_stale_within_bound_throttled():
    print("\n[17] stale <=72h + throttle -> served, NO network attempt")
    clear()
    write_cache(good(), age_h=24.0)
    m, h, calls = load(refresh_allowed=False)
    check("records served", bool(m), True)
    check("classified STALE", h["status"], "STALE")
    check("no refresh attempted", calls, 0)
    check("health says not attempted", h["refresh_attempted"], False)
    check("reason identifies the throttle", h["reason"], "refresh throttled")


def test_expired_bypasses():
    print("\n[18] stale >72h + throttle -> BYPASS, refresh attempted")
    for age in (72.5, 100.0, 500.0):
        clear()
        write_cache(good(), age_h=age)
        m, h, calls = load(refresh_allowed=False)
        check(f"age {age}h -> refresh ATTEMPTED despite throttle", calls, 1)
        check(f"age {age}h -> not served", m, {})
        check(f"age {age}h -> STALE_EXPIRED", h["status"], "STALE_EXPIRED")


def test_missing_corrupt_empty_mismatch_bypass():
    print("\n[19][20][21][22] unusable fallback ALWAYS bypasses the throttle")
    cases = (("missing", None, 0, "MISSING"),
             ("corrupt", "{not json", 24.0, "CORRUPT"),
             ("empty file", "", 24.0, "EMPTY"),
             ("empty array", [], 24.0, "EMPTY"),
             ("truncated", json.dumps(good())[:40], 24.0, "CORRUPT"))
    for label, payload, age, want in cases:
        clear()
        write_cache(payload, age)
        m, h, calls = load(refresh_allowed=False)
        check(f"{label} -> refresh ATTEMPTED", calls, 1)
        check(f"{label} -> {want}", h["status"], want)
    clear()
    write_cache(good(), age_h=24.0)
    m, h, calls = load(refresh_allowed=False, universe=("SOMETHINGELSE",))
    check("universe mismatch -> refresh ATTEMPTED", calls, 1)
    check("universe mismatch -> UNIVERSE_MISMATCH",
          h["status"], "UNIVERSE_MISMATCH")


def test_72h_boundary_unchanged():
    print("\n[23] the throttle cannot extend the S-33 72h bound")
    real = angel._cache_age_h
    try:
        for age, want_calls, want_status in ((71.999, 0, "STALE"),
                                             (72.0, 0, "STALE"),
                                             (72.001, 1, "STALE_EXPIRED")):
            clear()
            write_cache(good(), age_h=1.0)
            angel._cache_age_h = lambda a=age: a
            m, h, calls = load(refresh_allowed=False)
            check(f"age {age}h -> status {want_status}", h["status"],
                  want_status)
            check(f"age {age}h -> refresh attempts {want_calls}", calls,
                  want_calls)
    finally:
        angel._cache_age_h = real
    print("      >72h is refused with the throttle ACTIVE, exactly as")
    print("      S-33 refuses it with the throttle absent")


def test_fresh_cache_unaffected():
    print("\n[24] a fresh cache ignores the throttle entirely")
    clear()
    write_cache(good(), age_h=1.0)
    for allowed in (True, False):
        m, h, calls = load(refresh_allowed=allowed)
        check(f"refresh_allowed={allowed} -> served", bool(m), True)
        check(f"refresh_allowed={allowed} -> OK", h["status"], "OK")
        check(f"refresh_allowed={allowed} -> no download", calls, 0)


def test_throttled_equals_instant_failure():
    print("\n[25] a THROTTLED cycle == a cycle whose refresh failed instantly")
    print("      (same records, same status, for every cache state)")
    for label, payload, age in (("stale 24h", good(), 24.0),
                                ("stale 100h", good(), 100.0),
                                ("missing", None, 0),
                                ("corrupt", "{bad", 24.0)):
        clear()
        write_cache(payload, age)
        a_m, a_h, _ = load(refresh_allowed=False)
        clear()
        write_cache(payload, age)
        b_m, b_h, _ = load(refresh_allowed=True, download=None)
        check(f"{label}: same records", a_m, b_m)
        check(f"{label}: same status", a_h["status"], b_h["status"])


def test_s33_validation_preserved():
    print("\n[26] S-33 validation still runs on the throttled path")
    clear()
    payload = good() + ["not a mapping",
                        {"token": "9", "symbol": "NIFTYxxCE", "name": "NIFTY"}]
    write_cache(payload, age_h=24.0)
    m, h, calls = load(refresh_allowed=False)
    check("malformed records dropped, valid ones kept",
          sum(len(v) for v in m.values()), 2)
    check("no refresh needed", calls, 0)
    print("      and expired contracts stay unselectable:")
    clear()
    past = date.today() - timedelta(days=10)
    fut = date.today() + timedelta(days=7)
    write_cache(good(expiry=fut) + [rec(opt="CE", token="8001", expiry=past),
                                    rec(opt="PE", token="8002", expiry=past)],
                age_h=24.0)
    m, h, _ = load(refresh_allowed=False)
    exp = angel.nearest_expiry(m, "NIFTY", date.today(), min_dte=1)
    check("nearest_expiry skips the expired one", exp, fut)
    check("no past expiry selectable",
          [e for e in angel.list_expiries(m, "NIFTY",
                                          date.today() + timedelta(days=1))
           if e < date.today()], [])


def test_s34a_timeout_interaction():
    print("\n[27] S-34a deadline + S-34b throttle compose")
    clear()
    write_cache(good(), age_h=24.0)
    angel._MASTER_CACHE = None
    angel._MASTER_HEALTH.update(status="UNKNOWN", age_h=None, reason=None,
                                refresh_attempted=False)
    angel.SCRIP_CACHE = TMP
    angel.AVAILABLE = True
    hits = {"n": 0}
    real = angel._fetch_master

    def boom(*a, **k):
        hits["n"] += 1
        raise angel.MasterRefreshTimeout("injected: total budget exceeded")
    angel._fetch_master = boom
    try:
        m = angel.load_instrument_master(["NIFTY"], refresh_allowed=True)
        h = angel.master_health()
        check("allowed -> deadline breach reached", hits["n"], 1)
        check("served STALE", h["status"], "STALE")
        check("marked as an actual attempt", h["refresh_attempted"], True)
        c = mem_db()
        ot.record_master_refresh(c, h, now_epoch=1000.0)
        check("timeout stamps the throttle",
              float(ot.meta_get(c, ot.MASTER_REFRESH_KEY)), 1000.0)
        angel._MASTER_CACHE = None
        hits["n"] = 0
        angel.load_instrument_master(["NIFTY"], refresh_allowed=False)
        check("throttled -> _fetch_master never called", hits["n"], 0)
        check("and no time is spent on the deadline at all",
              angel.master_health()["refresh_attempted"], False)
    finally:
        angel._fetch_master = real


def test_s34a_constants_frozen():
    print("\n[28] S-34a remains frozen")
    check("budget 20s", angel.SCRIP_REFRESH_BUDGET_S, 20)
    check("socket cap 5s", angel.SCRIP_REFRESH_SOCKET_CAP_S, 5)
    check("chunk 32 KB", angel.SCRIP_REFRESH_CHUNK_B, 32 * 1024)
    check("stale bound 72h", angel.SCRIP_CACHE_MAX_STALE_H, 72)
    check("fresh bound 20h", angel.SCRIP_CACHE_MAX_AGE_H, 20)


def test_default_keeps_callers_compatible():
    print("\n[29] existing callers stay compatible")
    import inspect
    sig = inspect.signature(angel.load_instrument_master)
    check("refresh_allowed defaults to True",
          sig.parameters["refresh_allowed"].default, True)
    clear()
    write_cache(good(), age_h=1.0)
    angel._MASTER_CACHE = None
    angel.SCRIP_CACHE = TMP
    m = angel.load_instrument_master(["NIFTY"])     # one positional arg
    check("single-argument call still works", bool(m), True)


def test_recovery_cycle():
    print("\n[30] failure -> throttled -> retry -> recovery")
    c = mem_db()
    clear()
    write_cache(good(), age_h=24.0)
    m, h, calls = load(refresh_allowed=True, download=None)
    ot.record_master_refresh(c, h, now_epoch=0.0)
    check("cycle 1: attempted and failed", (calls, h["status"]), (1, "STALE"))
    allowed = ot.master_refresh_allowed(c, now_epoch=60.0)
    m, h, calls = load(refresh_allowed=allowed)
    ot.record_master_refresh(c, h, now_epoch=60.0)
    check("cycle 2: throttled, no attempt", calls, 0)
    check("cycle 2: stamp preserved",
          float(ot.meta_get(c, ot.MASTER_REFRESH_KEY)), 0.0)
    allowed = ot.master_refresh_allowed(c, now_epoch=float(R))
    m, h, calls = load(refresh_allowed=allowed, download=good())
    ot.record_master_refresh(c, h, now_epoch=float(R))
    check("cycle 3: retried after the interval", calls, 1)
    check("cycle 3: recovered to OK", h["status"], "OK")
    check("cycle 3: stamp cleared", ot.meta_get(c, ot.MASTER_REFRESH_KEY), "")


if __name__ == "__main__":
    for fn in (test_no_state, test_active_and_expired, test_boundary,
               test_anomalies_fail_open, test_database_unavailable,
               test_process_restart, test_failure_sets_timestamp,
               test_throttled_skip_never_restamps, test_success_clears,
               test_unusable_payload_is_a_failure,
               test_stale_within_bound_throttled, test_expired_bypasses,
               test_missing_corrupt_empty_mismatch_bypass,
               test_72h_boundary_unchanged, test_fresh_cache_unaffected,
               test_throttled_equals_instant_failure,
               test_s33_validation_preserved, test_s34a_timeout_interaction,
               test_s34a_constants_frozen,
               test_default_keeps_callers_compatible, test_recovery_cycle):
        fn()
    clear()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All refresh-throttle (S-34b) tests passed.")
