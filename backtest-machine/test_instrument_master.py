"""S-33: instrument-master stale-cache fallback.

    python test_instrument_master.py

Exits non-zero on failure, matching the other suites' convention.

The property under test is that a REFRESH FAILURE no longer destroys a
usable local master, WITHOUT ever serving data that is invalid or older
than the authorized bound. Age and validity are independent gates and
neither may stand in for the other.

Every download here is injected. Nothing contacts Angel One.
"""

import json
import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import angelone_client as angel

FAILURES = []
TMP = Path(tempfile.gettempdir()) / "scripmaster_test.json"

UNIV = ["NIFTY", "RELIANCE"]


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


# ----------------------------------------------------------- fixtures ---
def rec(name="NIFTY", strike=2400000, opt="CE", expiry=None, token="1001"):
    """One raw master record in the exchange's own wire format."""
    exp = expiry or (date.today() + timedelta(days=7))
    return {"token": token, "symbol": f"{name}{exp:%d%b%Y}".upper() + opt,
            "name": name, "expiry": f"{exp:%d%b%Y}".upper(),
            "strike": str(strike), "lotsize": "65", "exch_seg": "NFO",
            "instrumenttype": "OPTIDX" if name == "NIFTY" else "OPTSTK",
            "tick_size": "5", "freeze_qty": "1", "is_cas_enabled": ""}


def good_master(expiry=None):
    """Minimal master that satisfies the real functional requirement:
    an underlying with BOTH a CE and a PE on a selectable expiry."""
    return [rec(opt="CE", expiry=expiry, token="1001"),
            rec(opt="PE", expiry=expiry, token="1002"),
            rec(name="RELIANCE", strike=131000, opt="CE",
                expiry=expiry, token="2001"),
            rec(name="RELIANCE", strike=131000, opt="PE",
                expiry=expiry, token="2002")]


def write_cache(payload, age_h=0.0):
    """Write the cache and backdate its mtime to simulate content age."""
    if isinstance(payload, str):
        TMP.write_text(payload, encoding="utf-8")
    else:
        TMP.write_text(json.dumps(payload), encoding="utf-8")
    when = time.time() - age_h * 3600
    os.utime(TMP, (when, when))


def reset(download=None):
    """Fresh module state with the cache pointed at a scratch file.

    `download` is called instead of requests.get: None means 'no injection
    configured' and raises, which is the download-failure case.
    """
    angel._MASTER_CACHE = None
    angel._MASTER_HEALTH.update(status="UNKNOWN", age_h=None, reason=None)
    angel.SCRIP_CACHE = TMP
    angel.AVAILABLE = True
    calls = {"n": 0}

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    def _get(url, timeout=None):
        calls["n"] += 1
        if download is None:
            raise ConnectionError("injected: refresh unavailable")
        return _Resp(download)

    angel.requests = type("R", (), {"get": staticmethod(_get)})
    return calls


def clear():
    if TMP.exists():
        try:
            TMP.unlink()
        except Exception:
            pass


def n_contracts(m):
    return sum(len(v) for v in m.values())


# -------------------------------------------------------------- tests ---
def test_fresh_valid_cache():
    print("\n[1] fresh valid cache -> used, no download")
    clear()
    write_cache(good_master(), age_h=1.0)
    calls = reset(download=good_master())
    m = angel.load_instrument_master(UNIV)
    check("master returned", bool(m), True)
    check("no download attempted", calls["n"], 0)
    check("health OK", angel.master_health()["status"], "OK")


def test_fresh_valid_cache_download_failure():
    print("\n[2] fresh valid cache + refresh unavailable -> still usable")
    clear()
    write_cache(good_master(), age_h=1.0)
    calls = reset(download=None)
    m = angel.load_instrument_master(UNIV)
    check("master returned", bool(m), True)
    check("download never reached", calls["n"], 0)
    check("health OK", angel.master_health()["status"], "OK")


def test_stale_valid_within_bound():
    print("\n[3] stale valid cache <=72h + download failure -> STALE_VALID")
    for age in (20.1, 24.0, 71.9):
        clear()
        write_cache(good_master(), age_h=age)
        calls = reset(download=None)
        m = angel.load_instrument_master(UNIV)
        h = angel.master_health()
        check(f"age {age}h -> records served", bool(m), True)
        check(f"age {age}h -> classified STALE", h["status"], "STALE")
        check(f"age {age}h -> download WAS attempted first", calls["n"], 1)
        check(f"age {age}h -> age recorded", round(h["age_h"] or 0), round(age))

    # The boundary itself cannot be tested through mtime: wall-clock time
    # advances between writing the file and reading its age, so "exactly
    # 72.0h" is not a state that can be held. Drive the age directly so
    # the comparison is exercised deterministically.
    print("      boundary, with age injected rather than measured:")
    real_age = angel._cache_age_h
    try:
        for age, want in ((71.999, "STALE"), (72.0, "STALE"),
                          (72.001, "STALE_EXPIRED")):
            clear()
            write_cache(good_master(), age_h=1.0)
            reset(download=None)
            angel._cache_age_h = lambda a=age: a
            angel.load_instrument_master(UNIV)
            check(f"age exactly {age}h -> {want}",
                  angel.master_health()["status"], want)
    finally:
        angel._cache_age_h = real_age
    check("bound is 72h", angel.SCRIP_CACHE_MAX_STALE_H, 72)


def test_stale_beyond_bound():
    print("\n[4] stale valid cache >72h + download failure -> unavailable")
    for age in (72.2, 100.0, 500.0):
        clear()
        write_cache(good_master(), age_h=age)
        reset(download=None)
        m = angel.load_instrument_master(UNIV)
        check(f"age {age}h -> refused", m, {})
        check(f"age {age}h -> STALE_EXPIRED",
              angel.master_health()["status"], "STALE_EXPIRED")


def test_missing_cache():
    print("\n[5] missing cache + download failure -> unavailable")
    clear()
    reset(download=None)
    check("no master", angel.load_instrument_master(UNIV), {})
    check("health MISSING", angel.master_health()["status"], "MISSING")


def test_corrupt_cache():
    print("\n[6] corrupt cache + download failure -> unavailable")
    clear()
    write_cache("{not json at all", age_h=24.0)
    reset(download=None)
    check("no master", angel.load_instrument_master(UNIV), {})
    check("health CORRUPT", angel.master_health()["status"], "CORRUPT")


def test_empty_cache():
    print("\n[7] empty cache -> unavailable")
    clear()
    write_cache("", age_h=24.0)
    reset(download=None)
    check("no master", angel.load_instrument_master(UNIV), {})
    check("health EMPTY", angel.master_health()["status"], "EMPTY")
    print("      empty JSON array is also empty, not 'valid but small'")
    clear()
    write_cache([], age_h=24.0)
    reset(download=None)
    check("[] -> no master", angel.load_instrument_master(UNIV), {})
    check("[] -> health EMPTY", angel.master_health()["status"], "EMPTY")


def test_truncated_cache():
    print("\n[8] truncated cache + download failure -> unavailable")
    clear()
    body = json.dumps(good_master())
    write_cache(body[:len(body) // 2], age_h=24.0)
    reset(download=None)
    check("no master", angel.load_instrument_master(UNIV), {})
    check("health CORRUPT", angel.master_health()["status"], "CORRUPT")


def test_malformed_records_skipped():
    print("\n[9] malformed records skipped, well-formed ones still served")
    clear()
    payload = good_master() + [
        "this is not a mapping",
        {"token": "9", "symbol": "NIFTYxxCE", "name": "NIFTY"},   # no expiry
        {"token": "9", "symbol": "NIFTY01JAN2026CE", "name": "NIFTY",
         "expiry": "NOTADATE", "strike": "1", "lotsize": "1",
         "exch_seg": "NFO", "instrumenttype": "OPTIDX"},
        {"token": "9", "symbol": "NIFTY01JAN2026CE", "name": "NIFTY",
         "expiry": "01JAN2026", "strike": "abc", "lotsize": "1",
         "exch_seg": "NFO", "instrumenttype": "OPTIDX"},
    ]
    write_cache(payload, age_h=24.0)
    reset(download=None)
    m = angel.load_instrument_master(UNIV)
    check("master served", bool(m), True)
    check("only the 4 valid records indexed", n_contracts(m), 4)
    check("health STALE", angel.master_health()["status"], "STALE")


def test_universe_mismatch():
    print("\n[10] universe mismatch -> unavailable, distinct from network")
    clear()
    write_cache(good_master(), age_h=24.0)
    reset(download=None)
    m = angel.load_instrument_master(["SOMETHINGELSE"])
    check("no master", m, {})
    check("health UNIVERSE_MISMATCH",
          angel.master_health()["status"], "UNIVERSE_MISMATCH")
    print("      and it is NOT reported as a download failure")
    check("distinct from MISSING/CORRUPT",
          angel.master_health()["status"] in ("MISSING", "CORRUPT"), False)


def test_expired_contracts_not_selectable():
    print("\n[11][12] stale master holding EXPIRED contracts")
    clear()
    past = date.today() - timedelta(days=10)
    future = date.today() + timedelta(days=7)
    payload = good_master(expiry=future) + [
        rec(opt="CE", expiry=past, token="8001"),
        rec(opt="PE", expiry=past, token="8002")]
    write_cache(payload, age_h=48.0)
    reset(download=None)
    m = angel.load_instrument_master(UNIV)
    check("stale master served", angel.master_health()["status"], "STALE")
    check("expired records ARE present in the file",
          any(r["expiry"] < date.today() for r in m["NIFTY"]), True)
    # the real selection path, unchanged by S-33
    exp = angel.nearest_expiry(m, "NIFTY", date.today(), min_dte=1)
    check("nearest_expiry skips the expired one", exp, future)
    check("selected expiry is in the future", exp > date.today(), True)
    got = angel.list_expiries(m, "NIFTY", date.today() + timedelta(days=1))
    check("no expired expiry is selectable",
          [e for e in got if e < date.today()], [])
    opt = angel.find_option(m, "NIFTY", exp, 24000.0, "CE")
    check("a current contract still resolves", opt is not None, True)
    check("resolved contract is not expired", opt["expiry"] > date.today(),
          True)


def test_fresh_mtime_but_bad_content():
    print("\n[13] fresh mtime + truncated content -> REJECTED (age != validity)")
    clear()
    body = json.dumps(good_master())
    write_cache(body[:len(body) // 2], age_h=0.1)   # brand new, still broken
    reset(download=None)                            # refresh also unavailable
    m = angel.load_instrument_master(UNIV)
    check("young corrupt content refused", m, {})
    check("classified CORRUPT not OK",
          angel.master_health()["status"], "CORRUPT")
    print("      a partial write leaves FRESH mtime on BAD content -")
    print("      this is exactly why age alone can never admit data")


def test_failed_download_does_not_touch_mtime():
    print("\n[14] failed refresh must not modify the cache")
    clear()
    write_cache(good_master(), age_h=30.0)
    before_m, before_s = TMP.stat().st_mtime, TMP.stat().st_size
    before_bytes = TMP.read_bytes()
    reset(download=None)
    angel.load_instrument_master(UNIV)
    check("mtime unchanged", TMP.stat().st_mtime, before_m)
    check("size unchanged", TMP.stat().st_size, before_s)
    check("bytes unchanged", TMP.read_bytes(), before_bytes)


def test_repeated_failures_do_not_rejuvenate():
    print("\n[15] repeated failures must not walk the age forward")
    clear()
    write_cache(good_master(), age_h=71.5)
    ages = []
    for _ in range(4):
        reset(download=None)
        angel.load_instrument_master(UNIV)
        ages.append(angel.master_health()["age_h"])
    check("age is monotonically non-decreasing",
          all(ages[i] <= ages[i + 1] + 1e-6 for i in range(len(ages) - 1)),
          True)
    check("age never resets toward zero", min(ages) > 71.0, True)
    print("      so a cache CANNOT be kept alive past 72h by failing repeatedly")


def test_rejected_download_does_not_destroy_cache():
    print("\n[16] an unusable DOWNLOAD must not overwrite a good cache")
    clear()
    write_cache(good_master(), age_h=30.0)
    before = TMP.read_bytes()
    reset(download=[])                   # downloads an empty master
    m = angel.load_instrument_master(UNIV)
    check("empty download rejected", m, {})
    check("good cache preserved on disk", TMP.read_bytes(), before)


def test_successful_download_updates_cache():
    print("\n[17] successful refresh writes the cache normally")
    clear()
    write_cache(good_master(), age_h=30.0)
    old = TMP.stat().st_mtime
    fresh_payload = good_master() + [rec(opt="CE", token="7777", strike=2450000)]
    calls = reset(download=fresh_payload)
    m = angel.load_instrument_master(UNIV)
    check("download used", calls["n"], 1)
    check("health OK", angel.master_health()["status"], "OK")
    check("age reported as 0", angel.master_health()["age_h"], 0.0)
    check("cache rewritten", TMP.stat().st_mtime > old, True)
    check("new record present", n_contracts(m), 5)


def test_stale_preserves_selection_semantics():
    print("\n[18] stale master preserves candidate-generation semantics")
    clear()
    payload = good_master()
    write_cache(payload, age_h=48.0)
    reset(download=None)
    stale = angel.load_instrument_master(UNIV)
    # same payload, but served as a FRESH cache
    clear()
    write_cache(payload, age_h=1.0)
    reset(download=None)
    fresh = angel.load_instrument_master(UNIV)
    check("identical index from the same bytes",
          {k: sorted(r["token"] for r in v) for k, v in stale.items()},
          {k: sorted(r["token"] for r in v) for k, v in fresh.items()})
    e_s = angel.nearest_expiry(stale, "NIFTY", date.today(), min_dte=1)
    e_f = angel.nearest_expiry(fresh, "NIFTY", date.today(), min_dte=1)
    check("same expiry chosen", e_s, e_f)
    check("same contract chosen",
          angel.find_option(stale, "NIFTY", e_s, 24000.0, "CE")["token"],
          angel.find_option(fresh, "NIFTY", e_f, 24000.0, "CE")["token"])


def test_recovery_after_failure():
    print("\n[19][20] failure -> recovery yields no duplicate/extra state")
    clear()
    write_cache(good_master(), age_h=30.0)
    reset(download=None)
    a = angel.load_instrument_master(UNIV)
    check("cycle 1 served STALE", angel.master_health()["status"], "STALE")
    reset(download=good_master())
    b = angel.load_instrument_master(UNIV)
    check("cycle 2 recovers to OK", angel.master_health()["status"], "OK")
    check("same contract set before and after recovery",
          {k: sorted(r["token"] for r in v) for k, v in a.items()},
          {k: sorted(r["token"] for r in v) for k, v in b.items()})
    print("      each cycle is a fresh process; the module memo is per-process")
    reset(download=None)
    angel._MASTER_CACHE = {"NIFTY": []}
    check("memo short-circuits without re-reading",
          angel.load_instrument_master(UNIV), {"NIFTY": []})


def test_aug21_reconstruction():
    print("\n[21] AUG-21 reconstruction: valid ~24h cache + refresh failure")
    clear()
    write_cache(good_master(), age_h=23.98)     # Aug-20 09:16 -> Aug-21 09:15
    reset(download=None)
    m = angel.load_instrument_master(UNIV)
    h = angel.master_health()
    print(f"      BEFORE S-33: returned {{}} -> options book had no candidates")
    print(f"      AFTER  S-33: status={h['status']} age={h['age_h']:.1f}h "
          f"contracts={n_contracts(m)}")
    check("master is available", bool(m), True)
    check("classified STALE, not OK", h["status"], "STALE")
    check("candidate generation can proceed",
          angel.nearest_expiry(m, "NIFTY", date.today(), min_dte=1) is not None,
          True)
    check("failure reason retained for diagnosis",
          "ConnectionError" in (h["reason"] or ""), True)


if __name__ == "__main__":
    for fn in (test_fresh_valid_cache, test_fresh_valid_cache_download_failure,
               test_stale_valid_within_bound, test_stale_beyond_bound,
               test_missing_cache, test_corrupt_cache, test_empty_cache,
               test_truncated_cache, test_malformed_records_skipped,
               test_universe_mismatch, test_expired_contracts_not_selectable,
               test_fresh_mtime_but_bad_content,
               test_failed_download_does_not_touch_mtime,
               test_repeated_failures_do_not_rejuvenate,
               test_rejected_download_does_not_destroy_cache,
               test_successful_download_updates_cache,
               test_stale_preserves_selection_semantics,
               test_recovery_after_failure, test_aug21_reconstruction):
        fn()
    clear()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All instrument-master (S-33) tests passed.")
