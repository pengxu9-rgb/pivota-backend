"""Unauthenticated /agent/* traffic must actually be limited.

Before this, `middleware/rate_limiter.py` began the keyless path with

    if not api_key:
        return await call_next(request)   # no limiting whatsoever

justified by a comment reading "will fail auth later" — an assumption that
stopped being true when the citation and discovery routes under /agent/ became
public by design. And the keyed path was no better: its bucket keys on an
UNVALIDATED api_key, so `x-api-key: <random>` per request mints a fresh bucket
and evades it entirely.

THE TWO THINGS THIS FILE EXISTS TO PROVE, because they are what a naive
implementation gets wrong:

1. ROTATION DOES NOT HELP. Rotating x-api-key, or X-Forwarded-For, or both, must
   not buy unlimited throughput. Any limiter keyed solely on caller-supplied
   values fails this, which is why there is an identity-independent ceiling.

2. THE PEER ADDRESS IS NEVER THE IDENTITY. Behind Railway every peer is in
   100.64.0.0/10 (RFC 6598 CGNAT) — measured from prod access logs, 12 distinct
   proxy addresses and no real client IPs. Bucketing on `request.client.host`
   would collapse every external caller into a handful of shared buckets, so one
   abuser would lock out everyone else: a limiter that is a DoS amplifier. A
   test that only checks "abuse gets a 429" passes happily with that bug, so
   there are explicit isolation tests below.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from middleware.rate_limiter import RateLimitMiddleware, _positive_int

_GLOBAL_CAP = 5
_PER_IP_CAP = 3


def _app(
    monkeypatch,
    *,
    global_rpm: int = _GLOBAL_CAP,
    per_ip_rpm: int = _PER_IP_CAP,
    enabled: str = "true",
    trusted: str | None = None,
    admin_key: str | None = None,
) -> FastAPI:
    monkeypatch.setenv("ANON_RATE_LIMIT_ENABLED", enabled)
    monkeypatch.setenv("ANON_RATE_LIMIT_GLOBAL_RPM", str(global_rpm))
    monkeypatch.setenv("ANON_RATE_LIMIT_PER_IP_RPM", str(per_ip_rpm))
    if trusted is not None:
        monkeypatch.setenv("RATE_LIMIT_TRUSTED_API_KEYS", trusted)
    if admin_key is not None:
        monkeypatch.setenv("PROMOTIONS_ADMIN_KEY", admin_key)

    app = FastAPI()
    # A high per-key limit so the PRE-EXISTING keyed bucket never fires; any 429
    # in these tests must come from the new anonymous layers, not that one.
    app.add_middleware(RateLimitMiddleware, requests_per_minute=10_000)

    @app.get("/agent/public/thing")
    async def public_thing():
        return {"ok": True}

    @app.get("/not-agent/thing")
    async def not_agent():
        return {"ok": True}

    return app


def _codes(client: TestClient, n: int, headers=None, path="/agent/public/thing"):
    return [client.get(path, headers=headers or {}).status_code for _ in range(n)]


# --------------------------------------------------------------------------
# The bypass itself
# --------------------------------------------------------------------------


def test_anonymous_caller_is_eventually_limited(monkeypatch) -> None:
    """The plain gap: no credentials at all used to mean no limit at all."""
    with TestClient(_app(monkeypatch)) as c:
        codes = _codes(c, _GLOBAL_CAP + 3)

    assert 429 in codes, f"anonymous traffic was never limited: {codes}"
    assert codes[0] == 200, "the very first request must not be blocked"
    # Everything after the first 429 stays blocked within the window.
    first = codes.index(429)
    assert all(code == 429 for code in codes[first:]), codes


def test_rotating_the_api_key_does_not_evade_the_limit(monkeypatch) -> None:
    """HALF THE ORIGINAL BUG. The keyed bucket is keyed on an unvalidated key.

    An earlier draft of this fix limited only KEYLESS callers, which left this
    wide open: every request carries a brand-new key, so the per-key bucket is
    always empty and never fires.
    """
    with TestClient(_app(monkeypatch)) as c:
        codes = [
            c.get("/agent/public/thing", headers={"x-api-key": f"rotate-{i}"}).status_code
            for i in range(_GLOBAL_CAP + 3)
        ]

    assert 429 in codes, f"key rotation bought unlimited throughput: {codes}"


def test_rotating_x_forwarded_for_does_not_evade_the_limit(monkeypatch) -> None:
    """The other rotation. X-Forwarded-For is caller-supplied.

    The per-IP bucket cannot stop this — a fresh IP per request is a fresh
    bucket. Only the identity-independent ceiling can, which is precisely why it
    exists. If someone deletes the global layer believing the per-IP layer is
    enough, this test is what fails.
    """
    with TestClient(_app(monkeypatch)) as c:
        codes = [
            c.get(
                "/agent/public/thing",
                headers={"x-forwarded-for": f"203.0.113.{i}"},
            ).status_code
            for i in range(1, _GLOBAL_CAP + 4)
        ]

    assert 429 in codes, f"XFF rotation bought unlimited throughput: {codes}"


def test_rotating_both_headers_together_does_not_evade(monkeypatch) -> None:
    with TestClient(_app(monkeypatch)) as c:
        codes = [
            c.get(
                "/agent/public/thing",
                headers={"x-api-key": f"k-{i}", "x-forwarded-for": f"198.51.100.{i}"},
            ).status_code
            for i in range(1, _GLOBAL_CAP + 4)
        ]

    assert 429 in codes, f"rotating both evaded the limit: {codes}"


# --------------------------------------------------------------------------
# The DoS-amplifier hazard: distinct clients must not share a bucket
# --------------------------------------------------------------------------


def test_one_abusive_ip_does_not_lock_out_a_different_ip(monkeypatch) -> None:
    """Per-IP isolation, with the global ceiling raised out of the way.

    If the implementation keyed on request.client.host — the Railway CGNAT proxy
    — both callers would share one bucket and the victim would be blocked by the
    abuser's traffic. That is strictly worse than no limiter, so it gets its own
    test rather than being implied.
    """
    app = _app(monkeypatch, global_rpm=10_000, per_ip_rpm=_PER_IP_CAP)
    with TestClient(app) as c:
        abuser = _codes(
            c, _PER_IP_CAP + 3, headers={"x-forwarded-for": "203.0.113.9"}
        )
        victim = c.get(
            "/agent/public/thing", headers={"x-forwarded-for": "198.51.100.7"}
        )

    assert 429 in abuser, f"the abusive IP was never limited: {abuser}"
    assert victim.status_code == 200, (
        "a different IP was collateral damage — the bucket is shared, which "
        "means the peer address (or something equally coarse) is being used"
    )


def test_the_peer_address_is_not_used_as_the_identity(monkeypatch) -> None:
    """Every request here shares one peer (TestClient) but declares no IP.

    With the global ceiling raised, a keyless caller sending no
    X-Forwarded-For must NOT be bucketed at all — because the only thing left to
    key on is the peer, and behind Railway that is a shared proxy address.
    Unlimited here is the correct, deliberate outcome: the global ceiling is what
    covers these callers.
    """
    app = _app(monkeypatch, global_rpm=10_000, per_ip_rpm=2)
    with TestClient(app) as c:
        codes = _codes(c, 8)

    assert codes == [200] * 8, (
        f"a per-identity bucket was applied without any declared IP, so it must "
        f"be keyed on the shared peer address: {codes}"
    )


@pytest.mark.parametrize(
    "xff",
    ["not-an-ip", "", "   ", "127.0.0.1", "0.0.0.0", "1.2.3.4, 5.6.7.8, junk"],
    ids=["garbage", "empty", "whitespace", "loopback", "unspecified", "list-with-junk"],
)
def test_unusable_forwarded_values_do_not_mint_private_buckets(
    monkeypatch, xff
) -> None:
    """Unusable values must not escape the global ceiling.

    NOTE what this does and does not assert: it pins that such callers are still
    covered by the identity-independent ceiling. It cannot distinguish "junk was
    ignored" from "junk minted its own bucket", because with the per-IP limit
    raised out of the way both look identical. The precise claim — that an
    unparseable value yields NO identity, so rotating junk buys nothing the
    per-IP layer would otherwise deny — is asserted directly in
    test_anonymous_identity_only_accepts_a_real_routable_ip.
    """
    app = _app(monkeypatch, global_rpm=4, per_ip_rpm=10_000)
    with TestClient(app) as c:
        codes = _codes(c, 7, headers={"x-forwarded-for": xff} if xff else {})

    assert 429 in codes, f"junk XFF {xff!r} escaped the global ceiling: {codes}"


# --------------------------------------------------------------------------
# Who is exempt
# --------------------------------------------------------------------------


def test_trusted_internal_key_is_not_limited(monkeypatch) -> None:
    app = _app(monkeypatch, global_rpm=2, trusted="trusted-abc")
    with TestClient(app) as c:
        codes = _codes(c, 6, headers={"x-api-key": "trusted-abc"})

    assert codes == [200] * 6, f"an internal trusted key was throttled: {codes}"


def test_valid_admin_key_is_not_limited(monkeypatch) -> None:
    """/agent/internal/promotions authenticates with X-ADMIN-KEY, not agent auth.

    It sends no x-api-key and is polled internally roughly every 30s, so without
    this exemption the new limiter would eventually throttle an internal poller.
    """
    app = _app(monkeypatch, global_rpm=2, admin_key="super-secret-admin")
    with TestClient(app) as c:
        codes = _codes(c, 6, headers={"x-admin-key": "super-secret-admin"})

    assert codes == [200] * 6, f"a valid admin key was throttled: {codes}"


def test_WRONG_admin_key_is_still_limited(monkeypatch) -> None:
    """The exemption must check the credential, not merely its presence."""
    app = _app(monkeypatch, global_rpm=3, admin_key="super-secret-admin")
    with TestClient(app) as c:
        codes = _codes(c, 7, headers={"x-admin-key": "wrong-value"})

    assert 429 in codes, f"presenting ANY admin header bypassed the limit: {codes}"


def test_non_agent_paths_are_never_limited(monkeypatch) -> None:
    app = _app(monkeypatch, global_rpm=2)
    with TestClient(app) as c:
        codes = _codes(c, 8, path="/not-agent/thing")

    assert codes == [200] * 8, f"limiting leaked outside /agent/: {codes}"


# --------------------------------------------------------------------------
# Operability: the kill switch, config safety, and the 429 shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_the_kill_switch_disables_enforcement(monkeypatch, value) -> None:
    """This change can black-hole public traffic; the off switch must work."""
    app = _app(monkeypatch, global_rpm=2, enabled=value)
    with TestClient(app) as c:
        codes = _codes(c, 8)

    assert codes == [200] * 8, f"ANON_RATE_LIMIT_ENABLED={value} did not disable: {codes}"


@pytest.mark.parametrize(
    "raw,expected",
    [("0", 60), ("-5", 60), ("", 60), ("abc", 60), (None, 60), ("120", 120), ("  7 ", 7)],
)
def test_a_bad_limit_falls_back_instead_of_blocking_everything(raw, expected) -> None:
    """A 0 or negative limit would reject ALL unauthenticated traffic.

    `int("0")` succeeds, so a naive parse turns a typo into a total outage of the
    public agent surface. Non-positive values must fall back to the default.
    """
    assert _positive_int(raw, 60) == expected


def test_a_zero_env_limit_does_not_block_all_traffic(monkeypatch) -> None:
    """The same guarantee through the real middleware, not just the helper."""
    monkeypatch.setenv("ANON_RATE_LIMIT_GLOBAL_RPM", "0")
    monkeypatch.setenv("ANON_RATE_LIMIT_PER_IP_RPM", "0")
    monkeypatch.setenv("ANON_RATE_LIMIT_ENABLED", "true")
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=10_000)

    @app.get("/agent/public/thing")
    async def public_thing():
        return {"ok": True}

    with TestClient(app) as c:
        codes = _codes(c, 5)

    assert codes == [200] * 5, f"a 0 limit blocked live traffic: {codes}"


def test_the_429_does_not_publish_either_threshold(monkeypatch) -> None:
    """#1724's guarantee must hold for the new rejection path too."""
    app = _app(monkeypatch, global_rpm=2, per_ip_rpm=2)
    with TestClient(app) as c:
        res = None
        for _ in range(6):
            candidate = c.get("/agent/public/thing")
            if candidate.status_code == 429:
                res = candidate
                break

    assert res is not None
    names = {k.lower() for k in res.headers}
    assert "x-ratelimit-limit" not in names
    assert "retry-after" in names
    body = res.json()
    assert set(body) == {"error", "message", "retry_after"}, sorted(body)
    assert "requests per minute" not in res.text
    assert "2" not in body["message"]


# --------------------------------------------------------------------------
# Resource safety
# --------------------------------------------------------------------------


def test_the_tracked_key_store_is_bounded(monkeypatch) -> None:
    """A rotating attacker must not grow the in-memory store without bound.

    The pre-existing `request_store` is an unbounded defaultdict keyed on an
    unvalidated api_key, so rotation grows it forever — a slow memory
    exhaustion. Adding IP-shaped keys would have made that easier to reach, so
    the new store is capped.
    """
    monkeypatch.setenv("ANON_RATE_LIMIT_MAX_TRACKED_KEYS", "5")
    app = _app(monkeypatch, global_rpm=10_000, per_ip_rpm=10_000)
    mw = None
    with TestClient(app) as c:
        for i in range(1, 60):
            c.get("/agent/public/thing", headers={"x-forwarded-for": f"203.0.113.{i}"})
        node = app.middleware_stack
        while node is not None:
            if isinstance(node, RateLimitMiddleware):
                mw = node
                break
            node = getattr(node, "app", None)

    assert mw is not None
    assert len(mw._anon_store) <= 5, (
        f"store grew to {len(mw._anon_store)} keys despite a cap of 5 — "
        f"rotation is a memory-exhaustion vector"
    )


def test_redis_failure_fails_open(monkeypatch) -> None:
    """Infrastructure trouble must not take down the public agent surface."""

    class _BrokenRedis:
        async def incr(self, key):
            raise RuntimeError("redis is down")

        async def expire(self, key, ttl):
            raise RuntimeError("redis is down")

    monkeypatch.setattr(
        "middleware.rate_limiter.get_redis_client", lambda: _BrokenRedis()
    )
    app = _app(monkeypatch, global_rpm=2, per_ip_rpm=2)
    with TestClient(app) as c:
        codes = _codes(c, 6)

    assert codes == [200] * 6, f"a redis outage blocked live traffic: {codes}"

def test_anonymous_identity_only_accepts_a_real_routable_ip() -> None:
    """Direct assertion on the identity function, one case per rejection reason.

    Going through HTTP cannot separate "no identity" from "an identity nobody
    else shares", so the discrimination is asserted here instead.
    """

    def _identity(xff: str | None):
        headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/agent/public/thing",
                "headers": headers,
                "query_string": b"",
                "client": ("100.64.0.7", 12345),  # the Railway CGNAT peer
                "state": {},
            }
        )
        return RateLimitMiddleware._anonymous_identity(request)

    # Accepted: a real IP, normalised.
    assert _identity("203.0.113.5") == "ip:203.0.113.5"
    assert _identity("  203.0.113.5  ") == "ip:203.0.113.5"
    assert _identity("203.0.113.5, 70.41.3.18") == "ip:203.0.113.5"
    # IPv6 is compressed so two spellings of one address share a bucket.
    assert _identity("2001:0db8:0000:0000:0000:0000:0000:0001") == "ip:2001:db8::1"
    assert _identity("2001:db8::1") == "ip:2001:db8::1"

    # Rejected — each of these would otherwise be a free bucket per value.
    for bad in ("not-an-ip", "", "   ", "127.0.0.1", "::1", "0.0.0.0", "999.1.1.1"):
        assert _identity(bad) is None, f"{bad!r} was accepted as an identity"

    # And with no header at all, the peer address must NOT be substituted.
    assert _identity(None) is None
    assert "100.64.0.7" not in str(_identity("not-an-ip"))

def test_the_configured_limit_is_the_EXACT_number_allowed(monkeypatch) -> None:
    """Boundary, not just "a 429 happens eventually".

    Asserting only that a 429 appears somewhere in a long-enough burst cannot
    tell `> limit` from `>= limit`, and the latter silently enforces limit-1 —
    so ANON_RATE_LIMIT_GLOBAL_RPM=600 would really allow 599. Mutation testing
    caught exactly that: swapping the comparison left the whole suite green.

    The contract: a limit of N allows N requests per window, then rejects.
    """
    cap = 4
    app = _app(monkeypatch, global_rpm=cap, per_ip_rpm=10_000)
    with TestClient(app) as c:
        codes = _codes(c, cap + 2)

    assert codes == [200] * cap + [429, 429], (
        f"expected exactly {cap} allowed then rejections, got {codes}"
    )


def test_the_per_ip_limit_is_also_exact(monkeypatch) -> None:
    cap = 3
    app = _app(monkeypatch, global_rpm=10_000, per_ip_rpm=cap)
    with TestClient(app) as c:
        codes = _codes(c, cap + 2, headers={"x-forwarded-for": "203.0.113.44"})

    assert codes == [200] * cap + [429, 429], (
        f"expected exactly {cap} allowed then rejections, got {codes}"
    )
