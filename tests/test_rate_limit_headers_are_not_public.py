"""RateLimitMiddleware must not publish the global RATE_LIMIT_RPM to the public.

Measured on prod 2026-08-11, before this fix:

    curl -H 'x-api-key: nope' https://api.pivota.cc/agent/<no-such-route>
    -> HTTP/2 404
       x-ratelimit-limit: 120        <- the live RATE_LIMIT_RPM
       x-ratelimit-remaining: 119    <- and the bucket state

The middleware runs BEFORE authentication and buckets on an api_key it never
validates, so the old unconditional stamp after `call_next` handed the deployed
threshold to any caller who set the header to any value.

Two traps this file is built around:

TRAP 1 — "just gate on status < 400" does not work. The citation and discovery
routes under /agent/ are deliberately public and answer 200 to an invalid key
(that is the whole point of the agent-readability work), so a non-error status
does not imply the caller authenticated.

TRAP 2 — a test that only checks "the header is absent" is vacuous if the global
limit and the per-agent limit are the same number, and it cannot tell "we stopped
leaking" from "we publish the wrong number". Every test here therefore uses
_GLOBAL_RPM and _AGENT_LIMIT as DISTINCT sentinels and asserts which one appears.
"""
from __future__ import annotations

import re

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from middleware.rate_limiter import RateLimitMiddleware

# Deliberately different, and neither is a plausible default (100/120/1000),
# so a hardcoded or defaulted value can never satisfy these assertions.
_GLOBAL_RPM = 8_641
_AGENT_LIMIT = 37
_AGENT_USED = 5

_RL_HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")

# Headers a bare FastAPI response legitimately carries. Anything else on an
# anonymous /agent/ response is new surface and must be justified here.
_BASELINE_HEADERS = {"content-length", "content-type"}

# Any header whose NAME looks rate-limit-ish, however spelled. The three-name
# denylist this replaces let two real leaks through: the IETF draft spelling
# `RateLimit-Remaining` (no `x-` prefix) and an invented `X-RateLimit-Policy`.
_RL_NAME_RE = re.compile(r"rate.?limit", re.I)


def _assert_no_threshold_anywhere(response, global_rpm: int) -> None:
    """The guarantee, asserted three independent ways.

    A value check alone missed `remaining = rpm - 1`, from which the caller
    infers the threshold immediately. A name check alone missed a header that
    carries the number under an unrelated name. And pinning only the
    constructor sentinel missed a mutant reading `settings.rate_limit_rpm`,
    which differs from the sentinel in tests but is the SAME value in prod.
    """
    from config.settings import settings

    names = {k.lower() for k in response.headers}
    offenders = {n for n in names if _RL_NAME_RE.search(n)}
    assert not offenders, f"rate-limit-ish header(s) leaked: {sorted(offenders)}"

    assert names <= _BASELINE_HEADERS, (
        f"unexpected header(s) on an anonymous response: "
        f"{sorted(names - _BASELINE_HEADERS)} — if one is benign, add it to "
        f"_BASELINE_HEADERS deliberately"
    )

    # Values: the threshold itself, the off-by-one that reveals it, and the
    # settings-derived value a mutant would more plausibly reach for.
    forbidden = {
        str(global_rpm),
        str(global_rpm - 1),
        str(global_rpm + 1),
        str(settings.rate_limit_rpm),
        str(settings.rate_limit_rpm - 1),
    }
    blob = str(dict(response.headers)) + response.text
    for value in forbidden:
        assert value not in blob, f"threshold-revealing value {value!r} in response"


def _middleware_of(app: FastAPI) -> RateLimitMiddleware:
    """Reach the live RateLimitMiddleware instance to inspect its bucket state."""
    node = app.middleware_stack
    while node is not None:
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    raise AssertionError("RateLimitMiddleware not found in the stack")


def _app(global_rpm: int = _GLOBAL_RPM) -> FastAPI:
    """An app whose /agent/ routes mimic the three real caller shapes."""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=global_rpm)

    @app.get("/agent/public/thing")
    async def public_thing():
        # A public agent route: 200 even for a bogus key. No auth ran, so
        # nothing records rate-limit state.
        return {"ok": True}

    @app.get("/agent/private/thing")
    async def private_thing(request: Request):
        # Stands in for a route behind get_agent_context: authentication ran and
        # recorded the ENFORCED per-agent limit, exactly as agent_auth does.
        request.state.agent_rate_limit_limit = _AGENT_LIMIT
        request.state.agent_rate_limit_used = _AGENT_USED
        return {"ok": True}

    @app.get("/not-agent/thing")
    async def not_agent():
        return {"ok": True}

    return app


@pytest.fixture()
def client() -> TestClient:
    with TestClient(_app()) as c:
        yield c


def _rl(response) -> dict:
    return {k: v for k, v in response.headers.items() if k.lower() in _RL_HEADERS}


# --------------------------------------------------------------------------
# The leak itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-api-key": "totally-invalid"},
        {"x-api-key": ""},
        {"authorization": "Bearer not-a-real-token"},
        {"x-api-key": "ak_live_" + "f" * 64},  # well-FORMED but unknown key
    ],
    ids=["anonymous", "junk-key", "empty-key", "bearer-junk", "wellformed-unknown"],
)
def test_unauthenticated_callers_get_no_rate_limit_headers(headers) -> None:
    """The exact prod repro, across every unauthenticated caller shape."""
    with TestClient(_app()) as c:
        res = c.get("/agent/public/thing", headers=headers)

    assert res.status_code == 200
    assert _rl(res) == {}, f"leaked {_rl(res)} to an unauthenticated caller"
    _assert_no_threshold_anywhere(res, _GLOBAL_RPM)


def test_unknown_route_under_agent_does_not_leak_either() -> None:
    """The literal prod command: junk key + nonexistent /agent/ path -> 404."""
    with TestClient(_app()) as c:
        res = c.get("/agent/definitely-not-a-route", headers={"x-api-key": "nope"})

    assert res.status_code == 404
    assert _rl(res) == {}
    _assert_no_threshold_anywhere(res, _GLOBAL_RPM)


# --------------------------------------------------------------------------
# What legitimate clients still get — and that it is the RIGHT number
# --------------------------------------------------------------------------


def test_authenticated_caller_gets_its_OWN_limit_not_the_global_one() -> None:
    """The distinguishing test: per-agent limit published, global one absent.

    If this asserted only "a limit header exists", restoring the old global
    stamp would pass. It must assert WHICH number.
    """
    with TestClient(_app()) as c:
        res = c.get("/agent/private/thing", headers={"x-api-key": "some-key"})

    assert res.status_code == 200
    assert res.headers["x-ratelimit-limit"] == str(_AGENT_LIMIT)
    # MINUS ONE: `used` excludes the in-flight request, because the usage-log row
    # is written by UsageLoggerMiddleware only AFTER call_next while
    # check_rate_limit runs inside it. An earlier cut of this fix asserted
    # `_AGENT_LIMIT - _AGENT_USED` and thereby pinned an off-by-one that would
    # make a well-behaved client send one request too many.
    assert res.headers["x-ratelimit-remaining"] == str(
        _AGENT_LIMIT - _AGENT_USED - 1
    )
    assert "x-ratelimit-reset" in res.headers
    # The global threshold appears nowhere.
    assert str(_GLOBAL_RPM) not in str(dict(res.headers))
    assert res.headers["x-ratelimit-limit"] != str(_GLOBAL_RPM)


def test_authenticated_caller_with_no_api_key_header_still_gets_its_limit() -> None:
    """Checkout-token callers send no x-api-key and used to fall out early.

    The keyless branch returns before any bucket accounting, so if stamping only
    happened on the accounted path these callers would silently lose their
    pacing headers.
    """
    with TestClient(_app()) as c:
        res = c.get("/agent/private/thing")

    assert res.headers.get("x-ratelimit-limit") == str(_AGENT_LIMIT)


def test_trusted_internal_key_path_also_stamps_the_agent_limit(monkeypatch) -> None:
    """The trusted-key branch is a third early return; it must behave the same."""
    monkeypatch.setenv("RATE_LIMIT_TRUSTED_API_KEYS", "trusted-abc")
    # global_rpm=1 proves the trusted branch is the one being taken: an
    # untrusted key would be 429 by the second call. Without this the test
    # passes whether or not the trusted early-return stamps, because the
    # accounted path stamps too — a vacuous assertion.
    with TestClient(_app(global_rpm=1)) as c:
        results = [
            c.get("/agent/private/thing", headers={"x-api-key": "trusted-abc"})
            for _ in range(3)
        ]

    assert [r.status_code for r in results] == [200, 200, 200], "bypass not taken"
    for res in results:
        assert res.headers.get("x-ratelimit-limit") == str(_AGENT_LIMIT)
    assert "1" != res.headers.get("x-ratelimit-limit")


# --------------------------------------------------------------------------
# The 429 the middleware itself raises, pre-auth
# --------------------------------------------------------------------------


def test_preauth_429_gives_backoff_signal_without_the_threshold() -> None:
    """A caller who trips the limit may back off, but is not told the number.

    Retry-After and Reset are what a client needs; Limit is not. The old body
    said "Rate limit of 120 requests per minute exceeded" in plain text, which
    leaked the threshold even with the header removed — so this asserts the body
    too, not just headers.
    """
    with TestClient(_app(global_rpm=2)) as c:
        headers = {"x-api-key": "burner-key"}
        codes = [c.get("/agent/public/thing", headers=headers).status_code for _ in range(4)]
        assert 429 in codes, f"never tripped the limit: {codes}"
        res = next(
            r
            for r in (c.get("/agent/public/thing", headers=headers) for _ in range(3))
            if r.status_code == 429
        )

    assert res.status_code == 429
    assert "retry-after" in {k.lower() for k in res.headers}
    assert "x-ratelimit-limit" not in {k.lower() for k in res.headers}
    body = res.json()
    assert body["error"] == "rate_limit_exceeded"
    # The threshold (2) is a tiny number, so check the phrasing that carried it
    # rather than the digit, which could appear incidentally in a timestamp.
    assert "requests per minute" not in body["message"]
    assert "requests per minute" not in res.text
    # N6/N7: the threshold must not return under a new BODY key either, and the
    # message must not spell it out in some other phrasing. Assert the body's
    # key set, and that the number appears nowhere in it.
    assert set(body) == {"error", "message", "retry_after"}, sorted(body)
    assert "2" not in body["message"]


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------


def test_non_agent_paths_are_untouched() -> None:
    with TestClient(_app()) as c:
        res = c.get("/not-agent/thing", headers={"x-api-key": "whatever"})

    assert res.status_code == 200
    assert _rl(res) == {}


def test_agent_auth_429_headers_are_not_clobbered_by_the_middleware() -> None:
    """agent_auth raises 429 with the agent's OWN limit; that must survive.

    The old code overwrote X-RateLimit-Limit after call_next, replacing the
    accurate per-agent value with the global one on exactly the response where a
    client most needs it correct.
    """
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=_GLOBAL_RPM)

    from fastapi import HTTPException

    @app.get("/agent/quota/thing")
    async def quota_thing():
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded: 41/41 requests per minute",
            headers={"X-RateLimit-Limit": "41", "X-RateLimit-Remaining": "0"},
        )

    with TestClient(app) as c:
        res = c.get("/agent/quota/thing", headers={"x-api-key": "some-key"})

    assert res.status_code == 429
    assert res.headers["x-ratelimit-limit"] == "41", "middleware clobbered it"
    assert str(_GLOBAL_RPM) not in str(dict(res.headers))

# --------------------------------------------------------------------------
# The REDIS 429 branch — this is the PRODUCTION path (REDIS_URL is set on the
# web service), and mutation testing caught that the in-memory test above does
# not cover it: reverting the redis branch alone left the suite green.
# --------------------------------------------------------------------------


class _FakeRedis:
    """Minimal async stand-in: counts incr calls per key."""

    def __init__(self) -> None:
        self.counts: dict = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, ttl: int) -> bool:
        return True


def test_redis_preauth_429_also_withholds_the_threshold(monkeypatch) -> None:
    """Same guarantee as the in-memory branch, on the branch prod actually runs."""
    fake = _FakeRedis()
    monkeypatch.setattr(
        "middleware.rate_limiter.get_redis_client", lambda: fake
    )

    app = _app(global_rpm=2)
    with TestClient(app) as c:
        mw = _middleware_of(app)
        headers = {"x-api-key": "burner-redis"}
        results = [c.get("/agent/public/thing", headers=headers) for _ in range(4)]

    # Prove we really took the redis branch, not the in-memory fallback.
    # `fake.counts` alone is NOT sufficient: it only proves incr() ran. Any
    # change that degrades to the fallback after incr (an exception below it,
    # say) still populates counts while the 429 comes from in-memory code — and
    # a mutant restoring the leak inside the now-dead redis block ships green.
    # The in-memory path is the one that fills request_store, so an empty store
    # is what proves redis produced the 429.
    assert fake.counts, "redis branch was never exercised"
    assert not any(mw.request_store.values()), (
        "in-memory fallback produced this 429, so the redis branch is untested"
    )
    tripped = [r for r in results if r.status_code == 429]
    assert tripped, f"never tripped: {[r.status_code for r in results]}"
    res = tripped[0]
    assert "x-ratelimit-limit" not in {k.lower() for k in res.headers}
    assert "retry-after" in {k.lower() for k in res.headers}
    assert "requests per minute" not in res.text
    assert str(2) not in res.json()["message"]


def test_redis_success_path_publishes_only_the_agent_limit(monkeypatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(
        "middleware.rate_limiter.get_redis_client", lambda: fake
    )

    with TestClient(_app()) as c:
        anon = c.get("/agent/public/thing", headers={"x-api-key": "k1"})
        auth = c.get("/agent/private/thing", headers={"x-api-key": "k2"})

    assert fake.counts, "redis branch was never exercised"
    assert _rl(anon) == {}
    _assert_no_threshold_anywhere(anon, _GLOBAL_RPM)  # N3: this was missing
    assert auth.headers["x-ratelimit-limit"] == str(_AGENT_LIMIT)
    assert str(_GLOBAL_RPM) not in str(dict(auth.headers))


# --------------------------------------------------------------------------
# The OTHER half of the fix: agent_auth must actually record the per-agent
# limit. Mutation testing caught that every test above sets request.state by
# hand, so deleting the recording in agent_auth left the suite green while
# authenticated clients silently lost their headers.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_auth_records_the_enforced_limit_on_request_state(
    monkeypatch,
) -> None:
    """Drive the real dependency and assert it writes what the middleware reads.

    The attribute names are a CONTRACT between routes/agent_auth.py and
    middleware/rate_limiter.py with nothing else binding them together, so a
    rename on either side is a silent regression. This is the test that notices.
    """
    import routes.agent_auth as aa

    key = "ak_live_" + "a" * 64
    agent = {
        "agent_id": "agent_x",
        "agent_name": "Agent X",
        "is_active": True,
        "rate_limit": _AGENT_LIMIT,
        "daily_quota": 10_000,
        "allowed_merchants": None,
    }

    async def _get_agent_by_key(k, metrics_out=None):
        assert k == key
        return agent

    async def _check_rate_limit(agent_id, rate_limit=None):
        # The per-agent limit is what this returns; assert the dependency passes
        # the agent's own value through rather than a global default.
        assert rate_limit == _AGENT_LIMIT
        return True, _AGENT_USED, _AGENT_LIMIT

    async def _check_daily_quota(agent_id, daily_quota=None):
        return True, 0, 10_000

    async def _update_agent_stats(*a, **kw):
        return None

    monkeypatch.setattr(aa, "get_agent_by_key", _get_agent_by_key)
    monkeypatch.setattr(aa, "check_rate_limit", _check_rate_limit)
    monkeypatch.setattr(aa, "check_daily_quota", _check_daily_quota)
    monkeypatch.setattr(aa, "update_agent_stats", _update_agent_stats)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/agent/private/thing",
        "headers": [],
        "query_string": b"",
        "state": {},
    }
    request = Request(scope)

    context = await aa.get_agent_context(
        request, api_key=key, bearer=None, checkout_token=None
    )

    assert context.agent_id == "agent_x"
    assert getattr(request.state, "agent_rate_limit_limit", None) == _AGENT_LIMIT
    assert getattr(request.state, "agent_rate_limit_used", None) == _AGENT_USED

# --------------------------------------------------------------------------
# Gaps found by adversarial review of the first cut. Each of these mutants
# shipped GREEN before the corresponding test existed.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [403, 429, 500])
def test_authenticated_error_responses_keep_their_pacing_headers(status_code) -> None:
    """D3/D3b: status-gating the stamp must not be reintroducible silently.

    The docstring in rate_limiter.py argues at length that gating on
    `status < 400` is the WRONG fix for the leak. But nothing tested the
    regression direction: a mutant adding `if response.status_code >= 400:
    return` to the helper kept every leak test green, while every authenticated
    client lost its pacing headers on exactly the responses (429!) where they
    matter most.
    """
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=_GLOBAL_RPM)

    @app.get("/agent/err/thing")
    async def err(request: Request):
        request.state.agent_rate_limit_limit = _AGENT_LIMIT
        request.state.agent_rate_limit_used = _AGENT_USED
        return JSONResponse(status_code=status_code, content={"detail": "nope"})

    with TestClient(app, raise_server_exceptions=False) as c:
        res = c.get("/agent/err/thing", headers={"x-api-key": "some-key"})

    assert res.status_code == status_code
    assert res.headers.get("x-ratelimit-limit") == str(_AGENT_LIMIT), (
        f"authenticated caller lost its limit header on {status_code}"
    )


@pytest.mark.parametrize(
    "limit,used,expected",
    [
        (0, 0, None),            # D5: a zero limit is not publishable
        (-1, 0, None),           # negative is nonsense, not "unlimited"
        (10, 0, "9"),            # first request of the window
        (10, 9, "0"),            # last one allowed
        (10, 10, "0"),           # D9: at the cap, clamp — never negative
        (10, 99, "0"),           # over the cap, e.g. a stale/racy count
    ],
)
def test_remaining_is_clamped_and_offset_by_the_inflight_request(
    limit, used, expected
) -> None:
    """D5/D9: the arithmetic at the edges, including the -1 and the clamp."""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=_GLOBAL_RPM)

    @app.get("/agent/edge/thing")
    async def edge(request: Request):
        request.state.agent_rate_limit_limit = limit
        request.state.agent_rate_limit_used = used
        return {"ok": True}

    with TestClient(app) as c:
        res = c.get("/agent/edge/thing", headers={"x-api-key": "some-key"})

    if expected is None:
        assert "x-ratelimit-limit" not in {k.lower() for k in res.headers}
        assert "x-ratelimit-remaining" not in {k.lower() for k in res.headers}
    else:
        assert res.headers["x-ratelimit-limit"] == str(limit)
        assert res.headers["x-ratelimit-remaining"] == expected


@pytest.mark.asyncio
async def test_checkout_token_path_also_records_the_enforced_limit(
    monkeypatch,
) -> None:
    """D6: agent_auth writes the state at TWO sites; only one was covered.

    Deleting the checkout-token recording left the suite green, because the one
    contract test passes checkout_token=None and the header tests hand-set
    request.state.
    """
    import routes.agent_auth as aa

    class _Ctx:
        agent_id = "agent_ck"
        agent_name = "Agent Checkout"
        agent = {"rate_limit": _AGENT_LIMIT, "daily_quota": 10_000}

    async def _from_token(request, token):
        assert token == "tok-abc"
        return _Ctx()

    async def _check_rate_limit(agent_id, rate_limit=None):
        assert rate_limit == _AGENT_LIMIT
        return True, _AGENT_USED, _AGENT_LIMIT

    async def _check_daily_quota(agent_id, daily_quota=None):
        return True, 0, 10_000

    async def _update_agent_stats(*a, **kw):
        return None

    monkeypatch.setattr(aa, "_get_agent_context_from_checkout_token", _from_token)
    monkeypatch.setattr(aa, "check_rate_limit", _check_rate_limit)
    monkeypatch.setattr(aa, "check_daily_quota", _check_daily_quota)
    monkeypatch.setattr(aa, "update_agent_stats", _update_agent_stats)

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/agent/private/thing",
            "headers": [],
            "query_string": b"",
            "state": {},
        }
    )

    await aa.get_agent_context(
        request, api_key=None, bearer=None, checkout_token="tok-abc"
    )

    assert getattr(request.state, "agent_rate_limit_limit", None) == _AGENT_LIMIT
    assert getattr(request.state, "agent_rate_limit_used", None) == _AGENT_USED
    assert getattr(request.state, "agent_authenticated", False) is True


def test_authenticated_caller_with_no_per_agent_limit_gets_the_global_one() -> None:
    """F3: the internal-trusted branch authenticates but records no limit.

    Those keys are still bucketed by this middleware against the global limit
    (the middleware's trusted set and agent_auth's internal-trusted set are NOT
    the same set of env vars), so the global limit genuinely IS what is enforced
    on them. Pre-fix they received it; the first cut of this fix silently
    dropped their headers entirely.
    """
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=_GLOBAL_RPM)

    @app.get("/agent/trusted/thing")
    async def trusted(request: Request):
        # Authenticated, but no per-agent rate_limit exists to report.
        request.state.agent_authenticated = True
        return {"ok": True}

    with TestClient(app) as c:
        res = c.get("/agent/trusted/thing", headers={"x-api-key": "internal-only"})

    assert res.headers.get("x-ratelimit-limit") == str(_GLOBAL_RPM)
    # And the bucket's real remaining, not a fabricated one.
    assert res.headers.get("x-ratelimit-remaining") == str(_GLOBAL_RPM - 1)


def test_the_global_fallback_needs_authentication_not_just_accounting() -> None:
    """The F3 fallback must not become a new leak.

    An anonymous caller is also 'accounted' by the middleware, so the fallback
    is gated on request.state.agent_authenticated. If that gate were dropped,
    every keyed anonymous caller would receive the global threshold again.
    """
    with TestClient(_app()) as c:
        res = c.get("/agent/public/thing", headers={"x-api-key": "junk"})

    assert _rl(res) == {}
    _assert_no_threshold_anywhere(res, _GLOBAL_RPM)

@pytest.mark.asyncio
async def test_internal_trusted_path_marks_the_caller_as_authenticated(
    monkeypatch,
) -> None:
    """F3b: the third recording site in agent_auth, driven for real.

    Mutation testing caught this one too: deleting the marker from the
    internal-trusted branch shipped GREEN, because
    test_authenticated_caller_with_no_per_agent_limit_gets_the_global_one
    hand-sets request.state.agent_authenticated in a synthetic route and never
    executes agent_auth. Same defect as D6 and M6 — asserting the consuming
    side of a two-file contract while leaving the producing side untested.

    This branch records NO per-agent limit by design (it runs no
    check_rate_limit and _build_internal_trusted_agent carries no rate_limit),
    so the marker is the only thing that lets these callers keep any headers.
    """
    import routes.agent_auth as aa

    key = "internal-trusted-secret"
    # Patch the parsed tuple, so the real _is_internal_trusted_api_key logic
    # (hmac.compare_digest over the tuple) is what decides.
    monkeypatch.setattr(aa, "_INTERNAL_TRUSTED_API_KEYS", (key,))

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/agent/private/thing",
            "headers": [],
            "query_string": b"",
            "state": {},
        }
    )

    context = await aa.get_agent_context(
        request, api_key=key, bearer=None, checkout_token=None
    )

    assert context.agent_id.startswith("agent_internal_trusted_")
    assert getattr(request.state, "agent_authenticated", False) is True, (
        "internal-trusted callers would silently lose their pacing headers"
    )
    # And no per-agent limit is invented for them.
    assert getattr(request.state, "agent_rate_limit_limit", None) is None
