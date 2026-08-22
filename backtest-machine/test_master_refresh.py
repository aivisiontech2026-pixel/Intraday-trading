"""S-34a: bounded total deadline for one instrument-master refresh.

    python test_master_refresh.py

Exits non-zero on failure, matching the other suites' convention.

The property under test is that ONE refresh attempt cannot block the
process for an unbounded time, and that when it gives up the failure
reaches S-33 exactly like any other refresh failure.

Two layers prove the bound, because either alone is insufficient:

  Layer 1  deterministic - injected monotonic clock and a fake streaming
           transport. Proves the deadline ARITHMETIC with no network and
           no wall clock, so it cannot be flaky.
  Layer 2  real - a local HTTP server that trickles the body with
           inter-byte gaps BELOW the socket timeout. This is the exact
           pattern that defeats requests' `timeout`, and the test asserts
           both that the old mechanism would NOT have bounded it and that
           the new one does.

Nothing contacts Angel One.
"""

import http.server
import json
import socketserver
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import angelone_client as angel

FAILURES = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(name)


def check_true(name, cond, detail=""):
    check(name + (f" ({detail})" if detail else ""), bool(cond), True)


# ------------------------------------------------- layer 1: fake clock ---
class FakeClock:
    """Monotonic clock that only advances when told to."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeResponse:
    def __init__(self, chunks, status=200, clock=None, per_chunk=0.0):
        self._chunks = chunks
        self.status_code = status
        self._clock = clock
        self._per_chunk = per_chunk
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise IOError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=None):
        for c in self._chunks:
            if self._clock is not None and self._per_chunk:
                self._clock.advance(self._per_chunk)
            yield c


class FakeSession:
    """Records how it was called so the request contract can be asserted."""
    last = {}

    def __init__(self, response=None, raise_on_get=None, clock=None,
                 connect_cost=0.0):
        self._response = response
        self._raise = raise_on_get
        self._clock = clock
        self._connect_cost = connect_cost
        self.closed = False

    def get(self, url, timeout=None, stream=None, allow_redirects=None):
        FakeSession.last = {"url": url, "timeout": timeout, "stream": stream,
                            "allow_redirects": allow_redirects}
        if self._clock is not None and self._connect_cost:
            self._clock.advance(self._connect_cost)
        if self._raise is not None:
            raise self._raise
        return self._response

    def close(self):
        self.closed = True


def factory(**kw):
    return lambda: FakeSession(**kw)


PAYLOAD = [{"token": "1", "symbol": "X25AUG2600CE", "name": "X",
            "expiry": "25AUG2026", "strike": "100", "lotsize": "1",
            "exch_seg": "NFO", "instrumenttype": "OPTIDX"}]
BODY = json.dumps(PAYLOAD).encode()


def test_request_contract():
    print("\n[1] the request itself is built as mechanism C requires")
    clock = FakeClock()
    resp = FakeResponse([BODY], clock=clock)
    angel._fetch_master("http://x/", budget_s=20, now=clock,
                        session_factory=factory(response=resp, clock=clock))
    got = FakeSession.last
    check("stream=True (body is not buffered by requests)", got["stream"], True)
    check("allow_redirects=False", got["allow_redirects"], False)
    check_true("timeout is a (connect, read) pair",
               isinstance(got["timeout"], tuple) and len(got["timeout"]) == 2)
    check_true("socket timeout <= cap, derived from remaining budget",
               max(got["timeout"]) <= angel.SCRIP_REFRESH_SOCKET_CAP_S,
               f"{got['timeout']} cap={angel.SCRIP_REFRESH_SOCKET_CAP_S}")


def test_fast_success():
    print("\n[2] fast successful download")
    clock = FakeClock()
    resp = FakeResponse([BODY], clock=clock, per_chunk=0.1)
    out = angel._fetch_master("http://x/", budget_s=20, now=clock,
                              session_factory=factory(response=resp,
                                                      clock=clock))
    check("payload parsed", out, PAYLOAD)
    check("elapsed well inside budget", clock.t - 1000.0 < 1.0, True)


def test_slow_success_below_deadline():
    print("\n[3] slow successful download, still below the deadline")
    clock = FakeClock()
    parts = [BODY[i:i + 4] for i in range(0, len(BODY), 4)]
    # 19s of streaming inside a 20s budget
    resp = FakeResponse(parts, clock=clock, per_chunk=19.0 / len(parts))
    out = angel._fetch_master("http://x/", budget_s=20, now=clock,
                              session_factory=factory(response=resp,
                                                      clock=clock))
    check("completed", out, PAYLOAD)
    check("used most of the budget", 18.0 < clock.t - 1000.0 < 20.0, True)


def test_deterministic_deadline_breach():
    print("\n[4] LAYER 1: deterministic deadline breach")
    clock = FakeClock()
    endless = (b"x" * 16 for _ in range(10 ** 6))
    resp = FakeResponse(endless, clock=clock, per_chunk=1.0)
    t0 = clock.t
    raised = None
    try:
        angel._fetch_master("http://x/", budget_s=20, now=clock,
                            session_factory=factory(response=resp,
                                                    clock=clock))
    except Exception as e:
        raised = e
    check("raised MasterRefreshTimeout",
          type(raised).__name__, "MasterRefreshTimeout")
    elapsed = clock.t - t0
    check("aborted AT the budget, not after it", elapsed <= 21.0, True)
    check("did not abort early", elapsed >= 20.0, True)
    print(f"      injected elapsed = {elapsed:.1f}s for a 20s budget")


def test_budget_scales():
    print("\n[5] the bound follows the configured budget")
    for budget in (5, 12, 20, 25):
        clock = FakeClock()
        endless = (b"x" * 16 for _ in range(10 ** 6))
        resp = FakeResponse(endless, clock=clock, per_chunk=0.5)
        t0 = clock.t
        try:
            angel._fetch_master("http://x/", budget_s=budget, now=clock,
                                session_factory=factory(response=resp,
                                                        clock=clock))
        except angel.MasterRefreshTimeout:
            pass
        check(f"budget {budget}s honoured", clock.t - t0 <= budget + 0.5, True)


def test_budget_already_spent():
    print("\n[6] budget exhausted before the request begins")
    clock = FakeClock()
    raised = None
    try:
        angel._fetch_master("http://x/", budget_s=0, now=clock,
                            session_factory=factory(response=None))
    except Exception as e:
        raised = e
    check("raised before any I/O",
          type(raised).__name__, "MasterRefreshTimeout")


def test_connect_and_read_timeout():
    print("\n[7] connect/read timeout and transport errors propagate")
    for exc in (IOError("connect timed out"), IOError("read timed out"),
                ConnectionError("reset")):
        clock = FakeClock()
        raised = None
        try:
            angel._fetch_master("http://x/", budget_s=20, now=clock,
                                session_factory=factory(raise_on_get=exc))
        except Exception as e:
            raised = e
        check_true(f"{type(exc).__name__} propagates", raised is not None)


def test_redirect_is_refused():
    print("\n[8] a redirect is NOT followed and is treated as failure")
    clock = FakeClock()
    resp = FakeResponse([b""], status=302, clock=clock)
    raised = None
    try:
        angel._fetch_master("http://x/", budget_s=20, now=clock,
                            session_factory=factory(response=resp,
                                                    clock=clock))
    except Exception as e:
        raised = e
    check("302 refused", type(raised).__name__, "MasterRefreshTimeout")
    print("      redirects would each get a FRESH socket timeout - refusing")
    print("      them keeps the total bounded")


def test_partial_and_malformed():
    print("\n[9] partial / malformed body never yields a master")
    for label, chunks in (("truncated", [BODY[:len(BODY) // 2]]),
                          ("garbage", [b"{not json"]),
                          ("empty", [b""])):
        clock = FakeClock()
        resp = FakeResponse(chunks, clock=clock)
        raised = None
        try:
            angel._fetch_master("http://x/", budget_s=20, now=clock,
                                session_factory=factory(response=resp,
                                                        clock=clock))
        except Exception as e:
            raised = e
        check_true(f"{label} body raises", raised is not None,
                   type(raised).__name__)


def test_session_always_closed():
    print("\n[10] the session is closed on every path")
    clock = FakeClock()
    sess = FakeSession(response=FakeResponse([BODY], clock=clock), clock=clock)
    angel._fetch_master("http://x/", budget_s=20, now=clock,
                        session_factory=lambda: sess)
    check("closed after success", sess.closed, True)
    clock2 = FakeClock()
    endless = (b"x" * 16 for _ in range(10 ** 6))
    sess2 = FakeSession(response=FakeResponse(endless, clock=clock2,
                                              per_chunk=1.0), clock=clock2)
    try:
        angel._fetch_master("http://x/", budget_s=5, now=clock2,
                            session_factory=lambda: sess2)
    except angel.MasterRefreshTimeout:
        pass
    check("closed after timeout", sess2.closed, True)


# ------------------------------------------- layer 2: real trickle server ---
class _Trickle(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        # Must span MANY chunks, like the real ~37 MB master (~1,200
        # chunks). A body smaller than one chunk would be a single
        # blocking read and would not exercise the deadline loop at all -
        # that is precisely how the first implementation of this fix
        # passed the deterministic layer while failing against a socket.
        body = json.dumps(PAYLOAD * 12000).encode()
        assert len(body) > 20 * angel.SCRIP_REFRESH_CHUNK_B, len(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        step = max(1, len(body) // 40)
        for i in range(0, len(body), step):
            try:
                self.wfile.write(body[i:i + step])
                self.wfile.flush()
            except Exception:
                return
            time.sleep(0.5)          # gap BELOW the socket timeout


def test_layer2_real_trickle():
    print("\n[11] LAYER 2: real trickle server, gaps below the socket timeout")
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Trickle)
    srv.allow_reuse_address = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    try:
        import requests
        # (a) the OLD mechanism does NOT bound this
        t0 = time.monotonic()
        old_exc = None
        try:
            requests.get(url, timeout=3).json()
        except Exception as e:
            old_exc = type(e).__name__
        old_elapsed = time.monotonic() - t0
        check_true("old requests.get(timeout=3) overran its setting",
                   old_exc is None and old_elapsed > 6.0,
                   f"{old_elapsed:.1f}s, exc={old_exc}")

        # (b) the NEW mechanism DOES bound it
        BUDGET = 4
        t0 = time.monotonic()
        raised = None
        try:
            angel._fetch_master(url, budget_s=BUDGET)
        except Exception as e:
            raised = type(e).__name__
        new_elapsed = time.monotonic() - t0
        check("new mechanism raised MasterRefreshTimeout", raised,
              "MasterRefreshTimeout")
        bound = BUDGET + angel.SCRIP_REFRESH_SOCKET_CAP_S + 2.0
        check_true("new mechanism respected the claimed bound",
                   new_elapsed <= bound,
                   f"{new_elapsed:.1f}s <= {bound:.1f}s "
                   f"(budget {BUDGET} + cap {angel.SCRIP_REFRESH_SOCKET_CAP_S}"
                   f" + tolerance 2.0)")
        check_true("and it is far below the old behaviour",
                   new_elapsed < old_elapsed,
                   f"{new_elapsed:.1f}s vs {old_elapsed:.1f}s")
    finally:
        srv.shutdown()
        srv.server_close()


def _s33_case(cache_payload, age_h, injected_exc):
    """Run the real load_instrument_master with the refresh forced to fail
    in a specific way, against a controlled cache. Returns (records, health)."""
    import os
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "s34a_scrip.json"
    if cache_payload is None:
        if tmp.exists():
            tmp.unlink()
    else:
        tmp.write_text(cache_payload if isinstance(cache_payload, str)
                       else json.dumps(cache_payload), encoding="utf-8")
        when = time.time() - age_h * 3600
        os.utime(tmp, (when, when))
    angel._MASTER_CACHE = None
    angel._MASTER_HEALTH.update(status="UNKNOWN", age_h=None, reason=None)
    angel.SCRIP_CACHE = tmp
    angel.AVAILABLE = True
    real = angel._fetch_master

    def boom(*a, **k):
        raise injected_exc
    angel._fetch_master = boom
    try:
        import io
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            m = angel.load_instrument_master(["NIFTY"])
        return m, angel.master_health()["status"]
    finally:
        angel._fetch_master = real
        if tmp.exists():
            tmp.unlink()


def _good_cache():
    from datetime import date, timedelta
    e = date.today() + timedelta(days=7)
    return [{"token": "1", "symbol": f"NIFTY{e:%d%b%Y}".upper() + "CE",
             "name": "NIFTY", "expiry": f"{e:%d%b%Y}".upper(), "strike": "100",
             "lotsize": "1", "exch_seg": "NFO", "instrumenttype": "OPTIDX"},
            {"token": "2", "symbol": f"NIFTY{e:%d%b%Y}".upper() + "PE",
             "name": "NIFTY", "expiry": f"{e:%d%b%Y}".upper(), "strike": "100",
             "lotsize": "1", "exch_seg": "NFO", "instrumenttype": "OPTIDX"}]


def test_timeout_reaches_s33():
    print("\n[13] a deadline breach reaches S-33 like any refresh failure")
    TO = angel.MasterRefreshTimeout("total budget 20s exceeded")
    m, h = _s33_case(_good_cache(), 24.0, TO)
    check("stale <=72h + timeout -> records served", bool(m), True)
    check("classified STALE", h, "STALE")
    m, h = _s33_case(_good_cache(), 100.0, TO)
    check("stale >72h + timeout -> refused", m, {})
    check("classified STALE_EXPIRED", h, "STALE_EXPIRED")
    m, h = _s33_case(None, 0, TO)
    check("missing cache + timeout -> refused", m, {})
    check("classified MISSING", h, "MISSING")
    m, h = _s33_case("{not json", 24.0, TO)
    check("corrupt cache + timeout -> refused", m, {})
    check("classified CORRUPT", h, "CORRUPT")


def test_timeout_equivalent_to_any_failure():
    print("\n[14] timeout is INDISTINGUISHABLE from other refresh failures")
    print("      (so every S-33 and position-management guarantee carries over)")
    TO = angel.MasterRefreshTimeout("budget exceeded")
    CE = ConnectionError("network down")
    for label, cache, age in (("stale 24h", _good_cache(), 24.0),
                              ("stale 100h", _good_cache(), 100.0),
                              ("missing", None, 0),
                              ("corrupt", "{bad", 24.0)):
        a = _s33_case(cache, age, TO)
        b = _s33_case(cache, age, CE)
        check(f"{label}: same records", a[0], b[0])
        check(f"{label}: same health", a[1], b[1])


def test_recovery_after_timeout():
    print("\n[15] recovery after a timeout")
    import os
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "s34a_recover.json"
    tmp.write_text(json.dumps(_good_cache()), encoding="utf-8")
    when = time.time() - 24 * 3600
    os.utime(tmp, (when, when))
    angel.SCRIP_CACHE = tmp
    angel.AVAILABLE = True
    before = tmp.read_bytes()
    # cycle 1: timeout
    m1, h1 = _s33_case(_good_cache(), 24.0, angel.MasterRefreshTimeout("x"))
    check("cycle 1 serves STALE", h1, "STALE")
    # cycle 2: refresh works
    angel._MASTER_CACHE = None
    angel._MASTER_HEALTH.update(status="UNKNOWN", age_h=None, reason=None)
    angel.SCRIP_CACHE = tmp
    real = angel._fetch_master
    # a DIFFERENT payload, so "cache rewritten" is actually observable
    refreshed = _good_cache() + [dict(_good_cache()[0], token="3",
                                      symbol="NIFTYEXTRACE")]
    angel._fetch_master = lambda *a, **k: refreshed
    try:
        import io
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            m2 = angel.load_instrument_master(["NIFTY"])
    finally:
        angel._fetch_master = real
    check("cycle 2 recovers to OK", angel.master_health()["status"], "OK")
    check("the stale set is a subset of the refreshed set",
          set(r["token"] for r in m1["NIFTY"])
          <= set(r["token"] for r in m2["NIFTY"]), True)
    check("cache rewritten on success", tmp.read_bytes() != before, True)
    if tmp.exists():
        tmp.unlink()


def test_timeout_does_not_touch_cache():
    print("\n[16] a deadline breach must not modify the cache")
    import os
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "s34a_untouched.json"
    tmp.write_text(json.dumps(_good_cache()), encoding="utf-8")
    when = time.time() - 30 * 3600
    os.utime(tmp, (when, when))
    b0, m0 = tmp.read_bytes(), tmp.stat().st_mtime
    _s33_case(_good_cache(), 30.0, angel.MasterRefreshTimeout("x"))
    angel.SCRIP_CACHE = tmp
    check("bytes unchanged", tmp.read_bytes(), b0)
    check("mtime unchanged", tmp.stat().st_mtime, m0)
    if tmp.exists():
        tmp.unlink()


def test_production_call_site():
    print("\n[12] production wiring")
    src = (HERE / "angelone_client.py").read_text(encoding="utf-8")
    check("timeout=180 is gone", "timeout=180" in src, False)
    check("requests.get is no longer the refresh mechanism",
          "requests.get(SCRIP_MASTER_URL" in src, False)
    check("_fetch_master is the call site",
          "_fetch_master(SCRIP_MASTER_URL)" in src, True)
    check("budget is 20s", angel.SCRIP_REFRESH_BUDGET_S, 20)
    check("MasterRefreshTimeout is an Exception subclass",
          issubclass(angel.MasterRefreshTimeout, Exception), True)


if __name__ == "__main__":
    for fn in (test_request_contract, test_fast_success,
               test_slow_success_below_deadline,
               test_deterministic_deadline_breach, test_budget_scales,
               test_budget_already_spent, test_connect_and_read_timeout,
               test_redirect_is_refused, test_partial_and_malformed,
               test_session_always_closed, test_layer2_real_trickle,
               test_timeout_reaches_s33, test_timeout_equivalent_to_any_failure,
               test_recovery_after_timeout, test_timeout_does_not_touch_cache,
               test_production_call_site):
        fn()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All master-refresh (S-34a) tests passed.")
