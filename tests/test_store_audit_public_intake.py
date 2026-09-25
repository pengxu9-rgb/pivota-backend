"""Public store-audit intake/teaser tests.

All endpoint tests go through TestClient: flag gating, validation, and
response_model behavior live in the HTTP layer, and direct-call tests have
already missed a defect there once (PR #1874).
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import store_audit_public_intake as intake_module


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _enabled_and_isolated(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_PUBLIC_INTAKE_ENABLED", "true")
    # Fresh limiters per test: the module-level ones accumulate across tests.
    monkeypatch.setattr(
        intake_module, "_intake_limiter",
        intake_module._SlidingWindowLimiter(limit=5, window_seconds=60.0),
    )
    monkeypatch.setattr(
        intake_module, "_teaser_limiter",
        intake_module._SlidingWindowLimiter(limit=30, window_seconds=60.0),
    )


@pytest.fixture()
def db(monkeypatch):
    """Default DB stubs: cold domain, empty queue, cap untouched."""
    calls = {"enqueue": [], "upsert": []}

    async def no_evidence(**_kwargs):
        return None

    async def no_run(**_kwargs):
        return None

    async def no_route(**_kwargs):
        return None

    async def fake_upsert(**kwargs):
        calls["upsert"].append(kwargs)
        return {"execution_route_id": "route-new", "last_audit_run_id": None}

    async def fake_enqueue(**kwargs):
        calls["enqueue"].append(kwargs)
        return "verify-1"

    async def fake_count(**_kwargs):
        return 0

    monkeypatch.setattr(
        intake_module, "fetch_latest_route_evidence_for_domain", no_evidence)
    monkeypatch.setattr(
        intake_module, "fetch_latest_verification_for_domain", no_run)
    # classify_run_pointer reads through db.merchant_audit_runs.database, a
    # DIFFERENT handle from the one this fixture stubs — so without an
    # explicit answer here the fence's verdict depends on suite state, and the
    # test passes alone and fails in the full run. Say which case is modelled.
    async def pointer_is_someone_elses(*, run_id):
        from db.merchant_audit_runs import (
            RUN_POINTER_ABSENT, RUN_POINTER_OTHER,
        )
        return RUN_POINTER_OTHER if run_id else RUN_POINTER_ABSENT

    monkeypatch.setattr(
        intake_module, "classify_run_pointer", pointer_is_someone_elses,
    )
    monkeypatch.setattr(intake_module, "fetch_route_for_domain", no_route)
    monkeypatch.setattr(intake_module, "upsert_execution_route", fake_upsert)
    monkeypatch.setattr(intake_module, "enqueue_verification_run", fake_enqueue)
    monkeypatch.setattr(
        intake_module, "count_recent_intake_verifications", fake_count)
    return calls


def _client():
    app = FastAPI()
    app.include_router(intake_module.router)
    return TestClient(app, raise_server_exceptions=False)


def _intake(client, store_url="https://shop.acme.com"):
    return client.post(
        "/public/store-audit/intake", json={"store_url": store_url},
    )


# --- gating -----------------------------------------------------------------

def test_disabled_flag_is_a_404_on_both_endpoints(monkeypatch, db):
    monkeypatch.delenv("STORE_AUDIT_PUBLIC_INTAKE_ENABLED", raising=False)
    client = _client()
    assert _intake(client).status_code == 404
    assert client.get(
        "/public/store-audit/teaser", params={"store_url": "shop.acme.com"},
    ).status_code == 404
    assert db["enqueue"] == []


# --- domain normalization ---------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("shop.example.com", "shop.example.com"),
    ("https://Shop.Example.com/products/a?b=c", "shop.example.com"),
    ("www.shop.example.com", "shop.example.com"),
    ("http://shop.example.com", "shop.example.com"),
    ("xn--bcher-kva.example.com", "xn--bcher-kva.example.com"),
    ("example.xn--p1ai", "example.xn--p1ai"),
])
def test_normalize_accepts_public_hosts(raw, expected):
    assert intake_module.normalize_store_domain(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "notadomain", "localhost", "127.0.0.1", "https://127.0.0.1",
    "[::1]", "https://[2001:db8::1]/", "shop.example.com:8443",
    "https://user:pw@shop.example.com", "ftp://shop.example.com",
    "internal.corp", "printer.local", "db.internal", "unit.test",
    "svc.home.arpa", "a..b.com", "-bad.example.com", "x" * 600,
    # inet_aton-style IP literals that ipaddress.ip_address does not parse
    # but resolvers happily read as loopback/private addresses.
    "127.1", "0x7f.0.0.1", "127.0.0.0x1", "010.010.010.010",
    "192.168.000.001", "0177.0.0.1",
])
def test_normalize_rejects_junk_and_internal_hosts(raw):
    assert intake_module.normalize_store_domain(raw) is None


def test_invalid_store_url_is_422(db):
    response = _intake(_client(), store_url="127.0.0.1")
    assert response.status_code == 422
    assert db["enqueue"] == []


# --- intake state machine ---------------------------------------------------

def test_cold_domain_creates_discovery_placeholder_and_enqueues(db):
    response = _intake(_client())
    assert response.status_code == 202
    assert response.json()["state"] == "pending"
    assert len(db["upsert"]) == 1
    upsert = db["upsert"][0]
    assert upsert["route_kind"] == "ucp_discovery"
    assert upsert["endpoint"] == "https://shop.acme.com/"
    assert "merchant_id" not in upsert
    assert len(db["enqueue"]) == 1
    enqueue = db["enqueue"][0]
    assert enqueue["verifier_id"] == "ucp_probe"
    assert enqueue["execution_route_id"] == "route-new"
    assert enqueue["max_retries"] == 1
    assert enqueue["idempotency_key"].startswith("public_intake:shop.acme.com:")


def test_fresh_positive_evidence_short_circuits_without_enqueue(db, monkeypatch):
    async def fresh_evidence(**_kwargs):
        return {
            "evidence_level": "tested",
            "expires_at": _now() + timedelta(days=1),
            "created_at": _now() - timedelta(hours=1),
        }

    monkeypatch.setattr(
        intake_module, "fetch_latest_route_evidence_for_domain", fresh_evidence)
    response = _intake(_client())
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "ready"
    assert body["agent_ready"] is True
    assert body["evidence_level"] == "tested"
    assert db["enqueue"] == []


def test_expired_positive_is_stale_not_negative_and_reprobes(db, monkeypatch):
    async def expired_evidence(**_kwargs):
        return {
            "evidence_level": "tested",
            "expires_at": _now() - timedelta(days=1),
            "created_at": _now() - timedelta(days=8),
        }

    async def old_success(**_kwargs):
        return {"status": "succeeded", "completed_at": _now() - timedelta(days=8)}

    async def real_route(**kwargs):
        if "ucp" in tuple(kwargs.get("route_kinds") or ()):
            return {"execution_route_id": "route-real", "last_audit_run_id": "audit-9"}
        return None

    monkeypatch.setattr(
        intake_module, "fetch_latest_route_evidence_for_domain", expired_evidence)
    monkeypatch.setattr(
        intake_module, "fetch_latest_verification_for_domain", old_success)
    monkeypatch.setattr(intake_module, "fetch_route_for_domain", real_route)
    response = _intake(_client())
    assert response.status_code == 202
    body = response.json()
    # Stale positive must re-probe — and must never read as "not agent-ready".
    assert body["state"] == "pending"
    assert body.get("agent_ready") is None
    assert db["upsert"] == []
    assert len(db["enqueue"]) == 1
    assert db["enqueue"][0]["execution_route_id"] == "route-real"
    # STILL "audit-9": this route points at a run the fake DB answers for, so
    # classify_run_pointer returns OTHER and the funnel producer declines —
    # the merchant's own pointer is preserved exactly as it was pre-#2019.
    assert db["enqueue"][0]["audit_run_id"] == "audit-9"


def test_fresh_negative_is_served_without_reprobe(db, monkeypatch):
    async def recent_success(**_kwargs):
        return {"status": "succeeded", "completed_at": _now() - timedelta(hours=2)}

    monkeypatch.setattr(
        intake_module, "fetch_latest_verification_for_domain", recent_success)
    response = _intake(_client())
    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "ready"
    assert body["agent_ready"] is False
    assert db["enqueue"] == []


def test_aged_out_negative_reprobes(db, monkeypatch):
    async def stale_success(**_kwargs):
        return {"status": "succeeded", "completed_at": _now() - timedelta(days=30)}

    monkeypatch.setattr(
        intake_module, "fetch_latest_verification_for_domain", stale_success)
    response = _intake(_client())
    assert response.status_code == 202
    assert response.json()["state"] == "pending"
    assert len(db["enqueue"]) == 1


def test_in_flight_run_answers_pending_without_second_enqueue(db, monkeypatch):
    async def running(**_kwargs):
        return {"status": "claimed", "created_at": _now()}

    monkeypatch.setattr(
        intake_module, "fetch_latest_verification_for_domain", running)
    response = _intake(_client())
    assert response.status_code == 202
    assert response.json()["state"] == "pending"
    assert db["enqueue"] == []


def test_blocked_probe_answers_inconclusive_never_negative(db, monkeypatch):
    async def blocked(**_kwargs):
        return {"status": "blocked", "created_at": _now() - timedelta(hours=1)}

    monkeypatch.setattr(
        intake_module, "fetch_latest_verification_for_domain", blocked)
    client = _client()
    teaser = client.get(
        "/public/store-audit/teaser", params={"store_url": "shop.acme.com"},
    )
    assert teaser.status_code == 200
    body = teaser.json()
    assert body["state"] == "inconclusive"
    assert body.get("agent_ready") is None


def test_daily_cap_refuses_new_probes(db, monkeypatch):
    async def cap_hit(**_kwargs):
        return 200

    monkeypatch.setattr(
        intake_module, "count_recent_intake_verifications", cap_hit)
    response = _intake(_client())
    assert response.status_code == 429
    assert db["enqueue"] == []


def test_intake_rate_limit_trips_per_client(db):
    client = _client()
    codes = [
        _intake(client, store_url=f"s{i}.example.com").status_code
        for i in range(6)
    ]
    assert codes[:5] == [202] * 5
    assert codes[5] == 429


def test_teaser_unknown_for_never_seen_domain(db):
    response = _client().get(
        "/public/store-audit/teaser", params={"store_url": "shop.acme.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "unknown"
    assert body["domain"] == "shop.acme.com"
