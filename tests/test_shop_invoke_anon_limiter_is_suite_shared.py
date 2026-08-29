"""`/agent/shop/v1/invoke` carries a SECOND rate limiter, and it is suite-shared.

This is the mechanism behind the intermittent `sweep` failure in
`test_offers_resolve_stability_30_calls` — a required gate that reddened
arbitrary PRs, took main red on 2026-08-27, and reddened #1927 three times.

Two limiters guard this path:

  1. `middleware/rate_limiter.py` — the anonymous ceiling. Measured live at the
     failure: rpm=10000, per_ip=0 (disabled), store_len=0. It CANNOT produce a
     429 from 30 calls, which is why resetting its stores never helped.
  2. `routes/agent_shop_gateway.py:_check_invoke_anon_rate_limit` — a per-IP
     budget for credential-less callers, default 60/min. Module-level state, and
     the burst test never reset it.

The window is the killer detail: `int(time.time() // 60)` is an ABSOLUTE
wall-clock minute, not a window starting when the test does. Every anonymous
invoke anywhere in the suite charges the same bucket, so the burst fails whenever
31+ of them happen to share its minute. That is why it passes in isolation, fails
under the full suite, and moves whenever any test is added anywhere — what
changes is which side of a minute boundary the burst lands on.

These tests are deterministic: they saturate the bucket explicitly rather than
hoping the flake reappears.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import routes.agent_shop_gateway as gateway
from main import app


PAYLOAD = {
    "operation": "offers.resolve",
    "payload": {"product": {"product_id": "does-not-need-to-exist"}, "limit": 1},
    "metadata": {"source": "test"},
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _saturate(ip: str) -> None:
    """Charge the bucket to its ceiling for the CURRENT wall-clock minute."""
    gateway._INVOKE_ANON_IP_LIMIT_STORE[ip] = (
        int(time.time() // 60),
        gateway._invoke_anon_rpm(),
    )


def _discover_ip(client: TestClient) -> str:
    """The key the limiter uses for this client, read from its own store.

    Derived rather than hardcoded: the key comes from
    `_review_media_client_ip`, and pinning a literal here would make this test
    pass while silently testing a key the limiter no longer uses.
    """
    gateway._INVOKE_ANON_IP_LIMIT_STORE.clear()
    client.post("/agent/shop/v1/invoke", json=PAYLOAD)
    keys = list(gateway._INVOKE_ANON_IP_LIMIT_STORE)
    assert len(keys) == 1, f"expected exactly one limiter key, got {keys}"
    return keys[0]


def test_a_saturated_bucket_429s_a_credential_less_caller(client, monkeypatch) -> None:
    """The mechanism, stated as a fact rather than an inference.

    No middleware involvement: this is the gateway's own limiter, and a caller
    that has done nothing wrong is refused because the rest of the suite spent
    the budget inside the same wall-clock minute.
    """
    monkeypatch.setattr(gateway, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    ip = _discover_ip(client)
    _saturate(ip)

    res = client.post("/agent/shop/v1/invoke", json=PAYLOAD)
    assert res.status_code == 429, (
        f"expected the saturated bucket to refuse, got {res.status_code}"
    )


def test_clearing_the_store_restores_service(client, monkeypatch) -> None:
    """And the reset is what makes a burst test measure the resolver.

    Anything other than 429 proves the limiter is no longer the gate — the
    request may still fail on data, which is irrelevant here.
    """
    monkeypatch.setattr(gateway, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    ip = _discover_ip(client)
    _saturate(ip)
    assert client.post("/agent/shop/v1/invoke", json=PAYLOAD).status_code == 429

    gateway._INVOKE_ANON_IP_LIMIT_STORE.clear()
    assert client.post("/agent/shop/v1/invoke", json=PAYLOAD).status_code != 429


def test_a_credentialled_caller_is_never_charged(client, monkeypatch) -> None:
    """The limiter is scoped to credential-less callers by design.

    Pinned because widening it would silently rate-limit the first-party proxy
    and every keyed agent — a far worse outage than the flaky test that led here.
    """
    monkeypatch.setattr(gateway, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    ip = _discover_ip(client)
    _saturate(ip)

    res = client.post(
        "/agent/shop/v1/invoke", json=PAYLOAD, headers={"x-api-key": "anything"}
    )
    assert res.status_code != 429


def test_the_burst_test_resets_this_store(monkeypatch) -> None:
    """The fix itself, pinned where a rewrite would have to notice it.

    Asserts on the burst test's SOURCE because the leak is an omission — there is
    no behaviour to observe when a reset is missing except an intermittent
    failure somewhere else, hours later, in a different file.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "tests" / "test_pdp_resolution_stability.py"
    ).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "_INVOKE_ANON_IP_LIMIT_STORE" in body, (
        "the 30-call burst no longer isolates itself from the gateway's per-IP "
        "anon limiter; it will fail intermittently under the full suite"
    )
