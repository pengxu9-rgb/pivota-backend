"""Regression tests for every defect adversarial review found in the first cut.

Each test names the finding it pins. Two of these describe defects that would
have caused a production incident, so they are written to fail loudly rather than
subtly.

THE BIG ONE — the whole file exists because of it: every test in
test_anonymous_rate_limit.py exercises the IN-MEMORY backend, because REDIS_URL
is unset in the test environment. Production has REDIS_URL set, so production ran
the *other* branch, and mutants that made enforcement completely inert in prod
shipped green. The core guarantees here are therefore parametrised over BOTH
backends.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from middleware.rate_limiter import (
    RateLimitMiddleware,
    _constant_time_match,
    _non_negative_int,
)

_CEIL = 5


class _FakeRedis:
    """incr/decr/expire over a dict, enough to drive the real code path."""

    def __init__(self) -> None:
        self.store: dict = {}
        self.expires: dict = {}
        self.incr_calls = 0

    async def incr(self, key: str) -> int:
        self.incr_calls += 1
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def decr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.expires[key] = ttl
        return True


@pytest.fixture(params=["memory", "redis"])
def backend(request, monkeypatch):
    """The two counter backends. 'redis' is what production actually runs."""
    if request.param == "redis":
        fake = _FakeRedis()
        monkeypatch.setattr(
            "middleware.rate_limiter.get_redis_client", lambda: fake
        )
        return fake
    monkeypatch.setattr("middleware.rate_limiter.get_redis_client", lambda: None)
    return None


def _app(monkeypatch, *, ceiling=_CEIL, per_ip=0, enabled="true", **env) -> FastAPI:
    monkeypatch.setenv("ANON_RATE_LIMIT_ENABLED", enabled)
    monkeypatch.setenv("ANON_RATE_LIMIT_GLOBAL_RPM", str(ceiling))
    monkeypatch.setenv("ANON_RATE_LIMIT_PER_IP_RPM", str(per_ip))
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=10_000)

    # REAL public paths. The first cut's tests used invented paths like
    # /agent/public/thing, so a mutant that exempted `/agent/v1/` — the entire
    # real public surface — shipped green.
    @app.get("/agent/v1/citation/search")
    async def citation_search():
        return {"ok": True}

    @app.get("/agent/v1/products/search")
    async def products_search():
        return {"ok": True}

    @app.api_route("/agent/v2/orders", methods=["GET", "HEAD", "POST"])
    async def orders():
        return {"ok": True}

    return app


def _codes(client, n, headers=None, path="/agent/v1/citation/search", method="get"):
    call = getattr(client, method)
    return [call(path, headers=headers or {}).status_code for _ in range(n)]


# ==========================================================================
# P0-1 — an unauthenticated 500 introduced by the fix itself
# ==========================================================================


def test_non_ascii_credential_header_does_not_500(monkeypatch) -> None:
    """`secrets.compare_digest` raises TypeError on a non-ASCII str.

    Starlette decodes header bytes as latin-1, so one raw byte >= 0x80 in
    X-ADMIN-KEY reached that compare — and the exemption check runs on EVERY
    /agent/* request, so this was an unauthenticated 500 across the whole public
    agent surface, shipped by a rate-limit fix. httptools (uvicorn[standard]) is
    lenient about high bytes, so it is reachable over the wire.

    NOTE this drives the middleware helper with RAW header bytes rather than
    going through TestClient: httpx refuses to transmit a non-ASCII header value,
    so an end-to-end test cannot reach the defect at all — it would pass while
    production 500s. That is the trap this test is written around.
    """
    monkeypatch.setenv("PROMOTIONS_ADMIN_KEY", "real-admin-key")
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "introspect-secret")
    monkeypatch.setattr("middleware.rate_limiter.get_redis_client", lambda: None)
    mw = RateLimitMiddleware(app=None, requests_per_minute=10_000)

    for header in (b"x-admin-key", b"x-internal-key"):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/agent/v1/citation/search",
                # 0xE9 is 'é' in latin-1 — exactly what a hostile client sends.
                "headers": [(header, bytes([0xE9, 0x41, 0xFF]))],
                "query_string": b"",
                "client": ("100.64.0.7", 1234),
                "state": {},
            }
        )
        value = request.headers.get(header.decode())
        assert not value.isascii(), "the fixture must actually be non-ASCII"
        # Before the fix this raised TypeError, which became a 500.
        assert mw._is_exempt_caller(request) is False


def test_constant_time_match_never_raises_on_hostile_input() -> None:
    for supplied, expected in [
        ("café", "secret"),
        ("é", "é"),
        ("ok", ""),
        ("", "ok"),
        ("\ud800", "x"),  # lone surrogate, via surrogatepass
        ("plain", "plain"),
    ]:
        result = _constant_time_match(supplied, expected)
        assert isinstance(result, bool)
    assert _constant_time_match("same", "same") is True
    assert _constant_time_match("a", "b") is False
    # Identical non-ASCII values must still MATCH, not merely not-raise.
    assert _constant_time_match("café", "café") is True


# ==========================================================================
# P0-2 — the outage: /agent/internal/auth/introspect
# ==========================================================================


def test_internal_key_caller_is_exempt(monkeypatch, backend) -> None:
    """THE BLOCKER. `/agent/internal/auth/introspect` uses X-Internal-Key.

    It is how the Node gateway validates EVERY agent API key, from one egress
    IP. A 429 there is classified AUTH_INTROSPECT_REJECTED by the gateway, which
    is EXCLUDED from its emergency-auth-fallback allowlist (that accepts only
    AUTH_INTROSPECT_UNAVAILABLE and AUTH_INTROSPECT_ERROR_RESULT) — so a 429 is
    treated worse than a 500 and returns 503 for every authenticated agent
    request, with no negative caching to damp the retries. Verified in
    PIVOTA-Agent/src/server.js.
    """
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "introspect-secret")
    app = _app(monkeypatch, ceiling=2)
    with TestClient(app) as c:
        codes = _codes(c, 8, headers={"x-internal-key": "introspect-secret"})

    assert codes == [200] * 8, f"the gateway's auth channel was throttled: {codes}"


def test_WRONG_internal_key_is_not_exempt(monkeypatch, backend) -> None:
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "introspect-secret")
    app = _app(monkeypatch, ceiling=3)
    with TestClient(app) as c:
        codes = _codes(c, 7, headers={"x-internal-key": "wrong"})

    assert 429 in codes, f"any X-Internal-Key value bypassed the limit: {codes}"


def test_unset_internal_key_env_grants_no_exemption(monkeypatch, backend) -> None:
    """An absent env var must not make every caller exempt."""
    monkeypatch.delenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", raising=False)
    app = _app(monkeypatch, ceiling=3)
    with TestClient(app) as c:
        codes = _codes(c, 7, headers={"x-internal-key": ""})
        codes += _codes(c, 2, headers={"x-internal-key": "anything"})

    assert 429 in codes, "unset env produced a blanket exemption"


# ==========================================================================
# P0-3 — the ceiling must not sit below legitimate authenticated aggregate
# ==========================================================================


def test_ceiling_default_scales_with_the_per_key_limit(monkeypatch) -> None:
    """A flat 600 was exhausted by 5 agents at RATE_LIMIT_RPM=120 (prod).

    Each distinct key may spend `requests_per_minute`, so a fixed ceiling
    silently throttles authenticated agents inside their contracted quota — and
    hands them the anonymous 429, with no per-agent headers to pace against,
    which is the exact defect #1724 fixed on the header side.
    """
    monkeypatch.delenv("ANON_RATE_LIMIT_GLOBAL_RPM", raising=False)
    monkeypatch.setattr("middleware.rate_limiter.get_redis_client", lambda: None)

    for per_key, expected_floor in [(120, 1200), (1000, 10_000), (10, 600)]:
        mw = RateLimitMiddleware(app=None, requests_per_minute=per_key)
        assert mw.anon_global_rpm == expected_floor, (
            f"per-key {per_key}: ceiling {mw.anon_global_rpm} "
            f"expected {expected_floor}"
        )
        assert mw.anon_global_rpm >= per_key * 10, (
            "the ceiling can sit below what a handful of authenticated agents "
            "may legitimately spend"
        )


# ==========================================================================
# The per-IP layer ships OFF
# ==========================================================================


def test_per_ip_layer_is_disabled_by_default(monkeypatch, backend) -> None:
    """Review found this the dangerous half — and it is off unless asked for.

    The aggregation points that matter are server-side callers' single egress
    IPs: the Node gateway and the Vercel UI each front ALL their users from one
    address, so a per-IP bucket throttles those users collectively. It is also
    redundant with SHOP_INVOKE_ANON_RPM (default 60, per-IP) which
    agent_shop_gateway.py has run on /agent/shop/v1/invoke since 2026-08-08.
    """
    monkeypatch.delenv("ANON_RATE_LIMIT_PER_IP_RPM", raising=False)
    app = _app(monkeypatch, ceiling=10_000)
    del app  # rebuild without the per-IP env set at all
    monkeypatch.setenv("ANON_RATE_LIMIT_GLOBAL_RPM", "10000")
    monkeypatch.setenv("ANON_RATE_LIMIT_ENABLED", "true")
    monkeypatch.delenv("ANON_RATE_LIMIT_PER_IP_RPM", raising=False)

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=10_000)

    @app.get("/agent/v1/citation/search")
    async def citation_search():
        return {"ok": True}

    with TestClient(app) as c:
        codes = _codes(c, 40, headers={"x-forwarded-for": "203.0.113.5"})

    assert codes == [200] * 40, (
        f"the per-IP layer is enforcing by default; one server-side caller's "
        f"egress IP would throttle all of its users: {codes}"
    )
    # And assert the CONFIGURED VALUE directly. A behavioural burst of 40 cannot
    # distinguish "disabled" from "defaulted to 60" — mutation testing caught
    # exactly that, so the invariant is pinned at the source.
    node = app.middleware_stack
    while node is not None and not isinstance(node, RateLimitMiddleware):
        node = getattr(node, "app", None)
    assert node is not None
    assert node.anon_per_ip_rpm == 0, (
        f"per-IP default is {node.anon_per_ip_rpm}, expected 0 (disabled)"
    )


def test_zero_disables_per_ip_but_zero_ceiling_falls_back(monkeypatch) -> None:
    """0 means different things for the two knobs, deliberately."""
    assert _non_negative_int("0", 60) == 0        # per-IP: 0 disables
    assert _non_negative_int("-1", 60) == 60      # negative is nonsense
    assert _non_negative_int("garbage", 60) == 60
    from middleware.rate_limiter import _positive_int

    assert _positive_int("0", 600) == 600         # ceiling: 0 would block all


def test_per_ip_still_works_when_explicitly_enabled(monkeypatch, backend) -> None:
    app = _app(monkeypatch, ceiling=10_000, per_ip=3)
    with TestClient(app) as c:
        abuser = _codes(c, 5, headers={"x-forwarded-for": "203.0.113.9"})
        victim = c.get(
            "/agent/v1/citation/search", headers={"x-forwarded-for": "203.0.113.10"}
        )

    assert abuser == [200, 200, 200, 429, 429], abuser
    # Same /24 as the abuser — a coarsened key (e.g. /24 or /8) would fail here,
    # which the first cut's isolation test could not detect because it used two
    # different /8 networks.
    assert victim.status_code == 200, "bucket is coarser than a single address"


def test_ipv4_mapped_ipv6_shares_a_bucket_with_its_ipv4_form(monkeypatch, backend) -> None:
    """::ffff:203.0.113.5 and 203.0.113.5 are one host, so one bucket.

    Without normalisation a caller got double the per-IP budget by alternating
    spellings — and the docstring already CLAIMED spellings were normalised.
    """
    app = _app(monkeypatch, ceiling=10_000, per_ip=2)
    with TestClient(app) as c:
        codes = [
            c.get(
                "/agent/v1/citation/search",
                headers={"x-forwarded-for": xff},
            ).status_code
            for xff in (
                "203.0.113.5",
                "::ffff:203.0.113.5",
                "203.0.113.5",
                "::ffff:203.0.113.5",
            )
        ]

    assert 429 in codes, f"two spellings of one host got separate budgets: {codes}"


def test_identity_normalisation_directly() -> None:
    def _identity(xff):
        return RateLimitMiddleware._anonymous_identity(
            Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/agent/v1/citation/search",
                    "headers": [(b"x-forwarded-for", xff.encode())],
                    "query_string": b"",
                    "client": ("100.64.0.7", 1234),
                    "state": {},
                }
            )
        )

    assert _identity("::ffff:203.0.113.5") == _identity("203.0.113.5")
    assert _identity("2001:0db8::0001") == _identity("2001:db8::1")


# ==========================================================================
# P1-7 — a rejected request must not top up the window that rejected it
# ==========================================================================


def test_rejected_requests_do_not_refill_the_window(monkeypatch, backend) -> None:
    """Otherwise a caller ignoring its 429s holds the ceiling at 429 forever.

    The counter is charged before the verdict, so without a refund every
    rejected retry re-fills the global bucket and the public agent surface never
    recovers while the flood continues.
    """
    app = _app(monkeypatch, ceiling=3)
    with TestClient(app) as c:
        assert _codes(c, 3) == [200, 200, 200]
        # 20 rejected retries...
        assert _codes(c, 20) == [429] * 20
        node = app.middleware_stack
        while node is not None and not isinstance(node, RateLimitMiddleware):
            node = getattr(node, "app", None)

        if backend is None:
            bucket, count = node._anon_store["global"]
            assert count <= 3 + 1, (
                f"rejected retries inflated the window to {count}; the bucket "
                f"can never drain while a flood continues"
            )
        else:
            key = next(k for k in backend.store if k.startswith("anon_rate_limit:global"))
            assert backend.store[key] <= 3 + 1, backend.store[key]


# ==========================================================================
# P1-6 — the two backends must agree on what the limit means
# ==========================================================================


def test_both_backends_allow_exactly_the_ceiling(monkeypatch, backend) -> None:
    """Redis was a fixed window, memory was sliding — so the limit actually
    enforced depended on whether Redis was up. Worse, the keyed path sets
    `self.redis = None` permanently after one error, so a single blip silently
    switched algorithms for the life of the process.
    """
    app = _app(monkeypatch, ceiling=4)
    with TestClient(app) as c:
        codes = _codes(c, 6)

    assert codes == [200] * 4 + [429, 429], f"backend={backend!r}: {codes}"


def test_redis_branch_is_genuinely_exercised(monkeypatch) -> None:
    """Guard against the whole redis path silently going untested again."""
    fake = _FakeRedis()
    monkeypatch.setattr("middleware.rate_limiter.get_redis_client", lambda: fake)
    app = _app(monkeypatch, ceiling=3)
    with TestClient(app) as c:
        _codes(c, 5)

    assert fake.incr_calls >= 5, "the redis branch was not used"
    assert any(k.startswith("anon_rate_limit:global:") for k in fake.store)
    # A TTL must be set, or keys accumulate in Redis forever.
    assert fake.expires, "no TTL was set on the counter key"


# ==========================================================================
# Mutation gaps the first cut left open
# ==========================================================================


def test_the_429_headers_are_an_ALLOWLIST(monkeypatch, backend) -> None:
    """A one-name denylist let `X-RateLimit-Policy: 600;w=60` ship green.

    Pin the exact header set instead — the standing rule after #1719/#1721/#1724.
    """
    app = _app(monkeypatch, ceiling=1)
    with TestClient(app) as c:
        c.get("/agent/v1/citation/search")
        res = c.get("/agent/v1/citation/search")

    assert res.status_code == 429
    rate_ish = {k.lower() for k in res.headers if "ratelimit" in k.lower().replace("-", "")}
    assert rate_ish == {"x-ratelimit-remaining", "x-ratelimit-reset"}, (
        f"unexpected rate-limit header(s) on the 429: {sorted(rate_ish)}"
    )
    assert "1" not in res.headers.get("x-ratelimit-remaining", "x")  # it is "0"
    body = res.json()
    assert set(body) == {"error", "message", "retry_after"}, sorted(body)


def test_the_ceiling_is_global_not_per_path(monkeypatch, backend) -> None:
    """A per-path ceiling shipped green: spending it on one path must exhaust it
    for the others, or the 'single budget' claim is false."""
    app = _app(monkeypatch, ceiling=3)
    with TestClient(app) as c:
        first = _codes(c, 3, path="/agent/v1/citation/search")
        other = c.get("/agent/v1/products/search")

    assert first == [200] * 3
    assert other.status_code == 429, "the ceiling is per-path, not global"


def test_real_public_paths_are_covered(monkeypatch, backend) -> None:
    """A mutant exempting `/agent/v1/` — the entire real public surface — shipped
    green, because no test used a real path."""
    app = _app(monkeypatch, ceiling=2)
    with TestClient(app) as c:
        assert 429 in _codes(c, 5, path="/agent/v1/citation/search")

    app2 = _app(monkeypatch, ceiling=2)
    with TestClient(app2) as c:
        assert 429 in _codes(c, 5, path="/agent/v1/products/search")


def test_HEAD_requests_are_limited_too(monkeypatch, backend) -> None:
    """A method carve-out shipped green."""
    app = _app(monkeypatch, ceiling=2)
    with TestClient(app) as c:
        codes = _codes(c, 6, path="/agent/v2/orders", method="head")

    assert 429 in codes, f"HEAD escaped the ceiling: {codes}"


def test_a_tiny_store_cap_cannot_disable_the_ceiling(monkeypatch) -> None:
    """Dropping the cap guard made the limiter return 1 forever once full.

    The ceiling's key is charged on every request so it is always tracked first;
    a cap of 1 must therefore still enforce.
    """
    monkeypatch.setattr("middleware.rate_limiter.get_redis_client", lambda: None)
    monkeypatch.setenv("ANON_RATE_LIMIT_MAX_TRACKED_KEYS", "1")
    app = _app(monkeypatch, ceiling=3, per_ip=2)
    with TestClient(app) as c:
        codes = _codes(c, 8, headers={"x-forwarded-for": "203.0.113.77"})

    assert 429 in codes, f"a tiny cap disabled enforcement entirely: {codes}"


@pytest.mark.parametrize(
    "shape",
    [
        {"RAILWAY_ENVIRONMENT_NAME": "production", "RAILWAY_ENVIRONMENT": "production"},
        # Cloud Run: no RAILWAY_* exists at all. A limiter that had been gated
        # on one would go inert here with nothing to notice it.
        {"K_SERVICE": "pivota-backend", "PIVOTA_ENV": "production"},
        # And the unresolved revision, where the shim fails closed to prod.
        {"K_SERVICE": "pivota-backend"},
    ],
    ids=["railway", "cloud_run", "cloud_run_unresolved"],
)
def test_enforcement_is_not_environment_gated(monkeypatch, backend, shape) -> None:
    """A mutant gating on RAILWAY_ENVIRONMENT_NAME shipped green: enforced in
    CI, dead in prod. Set the prod-looking env and assert it still enforces."""
    for key in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "K_SERVICE", "PIVOTA_ENV"):
        monkeypatch.delenv(key, raising=False)
    for key, value in shape.items():
        monkeypatch.setenv(key, value)
    app = _app(monkeypatch, ceiling=2)
    with TestClient(app) as c:
        codes = _codes(c, 6)

    assert 429 in codes, f"enforcement is environment-gated: {codes}"

# ==========================================================================
# Gaps a SECOND mutation round found in the fixes above
# ==========================================================================


def test_enforcement_survives_a_window_rollover(monkeypatch) -> None:
    """M6: the store cap must not make the limiter inert at the next window.

    The cap check sits on the "new key or new bucket" branch, so within one
    window the already-tracked global key never reaches it — a mutant dropping
    the `key not in store` guard therefore looked harmless. At the bucket
    rollover the global key DOES take that branch, and an unguarded cap makes it
    return "1" forever without storing, so the ceiling silently stops enforcing
    from the second window onwards. Crossing the boundary is the only way to see
    it.
    """
    monkeypatch.setattr("middleware.rate_limiter.get_redis_client", lambda: None)
    monkeypatch.setenv("ANON_RATE_LIMIT_MAX_TRACKED_KEYS", "1")
    monkeypatch.setenv("ANON_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("ANON_RATE_LIMIT_GLOBAL_RPM", "3")
    monkeypatch.setenv("ANON_RATE_LIMIT_PER_IP_RPM", "0")

    mw = RateLimitMiddleware(app=None, requests_per_minute=10_000)
    window = mw.window_seconds

    # Window 1: the ceiling of 3 is enforced.
    base = 1_000_000.0
    counts = [mw._hit_window("global", base + i * 0.001) for i in range(5)]
    assert counts == [1, 2, 3, 4, 5], counts

    # Window 2, one full window later — the same key must keep counting.
    later = base + window + 1
    counts2 = [mw._hit_window("global", later + i * 0.001) for i in range(5)]
    assert counts2 == [1, 2, 3, 4, 5], (
        f"after a window rollover the counter stopped incrementing: {counts2} — "
        f"the ceiling would never fire again"
    )


def test_redis_client_sets_socket_timeouts(monkeypatch) -> None:
    """N7: failing OPEN on the verdict is worthless if the call itself hangs.

    RateLimitMiddleware now calls Redis on the request path for every /agent/*
    request. Without socket timeouts a blackholed Redis (accepting TCP, never
    answering) turns each rate-limit check into an unbounded stall on the public
    agent surface — a surface that made zero Redis calls before this change.
    Nothing asserted the timeouts existed, so removing them shipped green.
    """
    import utils.redis_client as rc

    captured = {}

    class _FakeRedisModule:
        @staticmethod
        def from_url(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return object()

    monkeypatch.setattr(rc, "redis", _FakeRedisModule)
    monkeypatch.setattr(rc, "_client", None)
    monkeypatch.setattr(rc.settings, "redis_url", "redis://localhost:6379/0")

    client = rc.get_redis_client()

    assert client is not None
    assert captured.get("socket_connect_timeout"), (
        "no socket_connect_timeout: a blackholed Redis hangs the agent surface"
    )
    assert captured.get("socket_timeout"), "no socket_timeout"
    assert captured["socket_connect_timeout"] > 0
    assert captured["socket_timeout"] > 0
    monkeypatch.setattr(rc, "_client", None)


def test_redis_timeout_env_override_rejects_zero(monkeypatch) -> None:
    """A 0 means "no timeout" to redis-py — the failure mode being closed."""
    import utils.redis_client as rc

    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_SECONDS", "0")
    assert rc._timeout_seconds("REDIS_SOCKET_TIMEOUT_SECONDS", 2.0) == 2.0
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_SECONDS", "-3")
    assert rc._timeout_seconds("REDIS_SOCKET_TIMEOUT_SECONDS", 2.0) == 2.0
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_SECONDS", "0.5")
    assert rc._timeout_seconds("REDIS_SOCKET_TIMEOUT_SECONDS", 2.0) == 0.5
