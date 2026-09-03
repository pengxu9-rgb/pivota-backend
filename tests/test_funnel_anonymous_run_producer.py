"""The funnel producer: an unowned run per domain, read publicly, claimed at
conversion.

Three surfaces, and the risky one is unauthenticated:

  POST /public/store-audit/intake      -> creates (or reuses) an unowned run
  GET  /public/store-audit/run/{id}    -> the deterministic projection, NO auth
  POST /api/merchant-center/audit/claim/{id} -> authenticated, domain-gated

Most of this file is refusals. The producer's whole risk is that an
unauthenticated endpoint now creates database rows and reads them back, so the
tests that matter are the ones proving it will not create rows without bound,
will not spend model credits, and will not answer for a run that is not its
own.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.store_audit_public_intake as sap
import db.merchant_audit_runs as mar
from utils.auth import get_current_merchant


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
async def db(monkeypatch, tmp_path):
    """File-backed SQLite: `databases` opens a connection per operation and
    every :memory: connection gets its own empty database."""
    from databases import Database
    d = Database(f"sqlite:///{tmp_path}/funnel.db")
    await d.connect()
    await d.execute(
        """
        CREATE TABLE merchant_audit_runs (
          run_id TEXT PRIMARY KEY,
          merchant_id TEXT,
          merchant_claimed_at TEXT,
          requested_at TIMESTAMP NOT NULL,
          status TEXT NOT NULL,
          subject_type TEXT,
          product_keys TEXT,
          partial_result_jsonb TEXT
        )
        """
    )
    monkeypatch.setattr(mar, "database", d)
    monkeypatch.setattr(mar, "_DDL_READY", True)
    yield d
    await d.disconnect()
    monkeypatch.setattr(mar, "_DDL_READY", False)


# ---- the producer: reuse + lane fencing ------------------------------------
#
# `record_anonymous_funnel_run` writes ARRAY-typed product_keys, which cannot
# bind on SQLite — the INSERT is exercised in
# tests/test_funnel_anonymous_run_producer_postgres.py against the real
# dialect. Here the rows are hand-inserted so the REUSE logic (which is pure
# Python over a bounded query) is tested where it is cheap.

async def _insert(db, *, domain, merchant_id=None, when=None,
                  subject_type=None, run_id=None):
    rid = run_id or str(uuid.uuid4())
    await db.execute(
        "INSERT INTO merchant_audit_runs "
        "(run_id, merchant_id, requested_at, status, subject_type, "
        " product_keys, partial_result_jsonb) "
        "VALUES (:r, :m, :t, 'succeeded', :s, '', :p)",
        {"r": rid, "m": merchant_id, "t": when or _now(),
         "s": subject_type or mar.SUBJECT_TYPE_PUBLIC_FUNNEL,
         "p": '{"funnel": {"domain": "%s"}}' % domain},
    )
    return rid


def test_the_domain_reader_normalizes_and_refuses_junk():
    assert mar.funnel_domain_of(
        {"partial_result_jsonb": {"funnel": {"domain": "Anua.com"}}}
    ) == "anua.com"
    # `databases` can hand a JSONB column back as a raw string.
    assert mar.funnel_domain_of(
        {"partial_result_jsonb": '{"funnel": {"domain": "anua.com"}}'}
    ) == "anua.com"
    # Cases only a real normalizer handles — `.strip().lower()` would let
    # every one of these through. The stored value is echoed to anonymous
    # callers AND is the authorization key the claim gate compares, so the
    # read side must not trust the column just because we wrote it.
    assert mar.funnel_domain_of(
        {"partial_result_jsonb": {"funnel": {"domain": "anua.com/../../etc"}}}
    ) == "anua.com"
    assert mar.funnel_domain_of(
        {"partial_result_jsonb": {"funnel": {"domain": "https://anua.com/p/1"}}}
    ) == "anua.com"
    assert mar.funnel_domain_of(
        {"partial_result_jsonb": {"funnel": {"domain": "www.anua.com"}}}
    ) == "anua.com"
    for bad_host in ("127.0.0.1", "localhost", "user:pw@anua.com",
                     "anua.com:8080", "not a host"):
        assert mar.funnel_domain_of(
            {"partial_result_jsonb": {"funnel": {"domain": bad_host}}}
        ) is None, bad_host

    for junk in ({}, {"partial_result_jsonb": None},
                 {"partial_result_jsonb": "not json"},
                 {"partial_result_jsonb": {"funnel": {}}},
                 {"partial_result_jsonb": {"funnel": {"domain": 7}}}):
        assert mar.funnel_domain_of(junk) is None


async def test_the_freshest_unclaimed_run_for_a_domain_is_reused(db):
    rid = await _insert(db, domain="anua.com")
    found = await mar.find_unclaimed_funnel_run_for_domain(
        domain="Anua.com", since=_now() - timedelta(hours=1))
    assert found and found["run_id"] == rid


async def test_a_claimed_run_is_not_reused(db):
    """Reuse must never hand a registered merchant's run to a stranger."""
    await _insert(db, domain="anua.com", merchant_id="m-1")
    assert await mar.find_unclaimed_funnel_run_for_domain(
        domain="anua.com", since=_now() - timedelta(hours=1)) is None


async def test_another_domains_run_is_not_reused(db):
    await _insert(db, domain="anua.com")
    assert await mar.find_unclaimed_funnel_run_for_domain(
        domain="pixi.com", since=_now() - timedelta(hours=1)) is None


async def test_a_run_outside_the_window_is_not_reused(db):
    await _insert(db, domain="anua.com", when=_now() - timedelta(days=5))
    assert await mar.find_unclaimed_funnel_run_for_domain(
        domain="anua.com", since=_now() - timedelta(hours=1)) is None


async def test_a_run_from_another_lane_is_never_reused(db):
    """subject_type is the lane fence: a real merchant audit must not be
    handed to the public funnel because it happens to be unowned."""
    await _insert(db, domain="anua.com", subject_type="merchant_url")
    assert await mar.find_unclaimed_funnel_run_for_domain(
        domain="anua.com", since=_now() - timedelta(hours=1)) is None


# ---- the unauthenticated read ----------------------------------------------

def _client(monkeypatch, *, row: Optional[Dict[str, Any]],
            merchant: str = "m-1") -> TestClient:
    async def fake_fetch(*, run_id: str):
        return row

    async def empty(**_kw):
        return []

    monkeypatch.setattr(sap, "_enabled", lambda: True)
    import db.merchant_audit_runs as m
    monkeypatch.setattr(m, "fetch_audit_run_by_id", fake_fetch)
    import db.audit_evidence as ae
    monkeypatch.setattr(ae, "list_evidence_for_run", empty)
    monkeypatch.setattr(ae, "list_findings_for_run", empty)
    monkeypatch.setattr(ae, "list_actions_for_run", empty)
    app = FastAPI()
    app.include_router(sap.router)
    app.include_router(sap.claim_router)
    app.dependency_overrides[get_current_merchant] = lambda: merchant
    return TestClient(app)


def _funnel_row(**over) -> Dict[str, Any]:
    row = {
        "run_id": "r-1",
        "merchant_id": None,
        "subject_type": mar.SUBJECT_TYPE_PUBLIC_FUNNEL,
        "partial_result_jsonb": {"funnel": {"domain": "anua.com"}},
    }
    row.update(over)
    return row


def test_the_public_read_serves_only_the_deterministic_audience(monkeypatch):
    res = _client(monkeypatch, row=_funnel_row()).get(
        "/public/store-audit/run/r-1")
    assert res.status_code == 200
    body = res.json()
    assert body["domain"] == "anua.com"
    assert body["projection"]["audience"] == "public_anonymous"
    assert body["projection"]["deterministic_only"] is True
    assert body["projection"]["claimable"] is True


def test_the_public_read_refuses_a_claimed_run(monkeypatch):
    """A claimed audit must stop being publicly readable to anyone who kept
    the URL — it belongs to a merchant now."""
    res = _client(monkeypatch, row=_funnel_row(merchant_id="m-1")).get(
        "/public/store-audit/run/r-1")
    assert res.status_code == 404


def test_the_public_read_refuses_a_run_from_another_lane(monkeypatch):
    """Otherwise any real merchant audit becomes anonymously readable by id."""
    res = _client(monkeypatch, row=_funnel_row(subject_type="merchant_url")).get(
        "/public/store-audit/run/r-1")
    assert res.status_code == 404


def test_the_public_read_refuses_an_unknown_run(monkeypatch):
    res = _client(monkeypatch, row=None).get("/public/store-audit/run/r-1")
    assert res.status_code == 404


def test_every_public_refusal_is_the_same_status(monkeypatch):
    """Distinguishing 'not yours' from 'does not exist' would let an anonymous
    caller enumerate which run ids are real."""
    codes = {
        _client(monkeypatch, row=None).get(
            "/public/store-audit/run/r-1").status_code,
        _client(monkeypatch, row=_funnel_row(merchant_id="m-1")).get(
            "/public/store-audit/run/r-1").status_code,
        _client(monkeypatch, row=_funnel_row(subject_type="merchant_url")).get(
            "/public/store-audit/run/r-1").status_code,
    }
    assert codes == {404}


def test_the_public_read_404s_while_the_lane_is_dark(monkeypatch):
    c = _client(monkeypatch, row=_funnel_row())
    monkeypatch.setattr(sap, "_enabled", lambda: False)
    assert c.get("/public/store-audit/run/r-1").status_code == 404


# ---- the claim --------------------------------------------------------------

def _claim(monkeypatch, *, row, bound, merchant="m-1", claimed=True):
    async def fake_bound(merchant_id):
        return bound

    async def fake_claim(*, run_id, merchant_id):
        return claimed

    import services.brand_claim_service as bcs
    monkeypatch.setattr(bcs, "merchant_bound_domains", fake_bound)
    monkeypatch.setattr(sap, "claim_audit_run_for_merchant", fake_claim)
    return _client(monkeypatch, row=row, merchant=merchant).post(
        "/api/merchant-center/audit/claim/r-1")


def test_a_merchant_bound_to_the_domain_claims_the_run(monkeypatch):
    res = _claim(monkeypatch, row=_funnel_row(), bound={"anua.com"})
    assert res.status_code == 200
    assert res.json() == {"claimed": True, "audit_run_id": "r-1"}


def test_a_merchant_not_bound_to_the_domain_is_refused(monkeypatch):
    """The run id is NOT a capability — it is handed to whoever submits a
    public domain, so the domain binding is what authorizes the claim."""
    res = _claim(monkeypatch, row=_funnel_row(), bound={"someoneelse.com"})
    assert res.status_code == 403
    assert res.json()["detail"]["error"] == "DOMAIN_NOT_BOUND"


def test_an_empty_binding_set_claims_nothing(monkeypatch):
    """Fail CLOSED: a merchant with no bound domains claims nothing, and a
    binding lookup that fails is treated the same way."""
    assert _claim(monkeypatch, row=_funnel_row(), bound=set()).status_code == 403


def test_a_binding_lookup_failure_fails_closed(monkeypatch):
    async def boom(merchant_id):
        raise RuntimeError("db down")

    import services.brand_claim_service as bcs
    monkeypatch.setattr(bcs, "merchant_bound_domains", boom)
    res = _client(monkeypatch, row=_funnel_row()).post(
        "/api/merchant-center/audit/claim/r-1")
    assert res.status_code == 403


def test_an_already_claimed_run_conflicts(monkeypatch):
    res = _claim(monkeypatch, row=_funnel_row(merchant_id="m-2"),
                 bound={"anua.com"})
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "ALREADY_CLAIMED"


def test_an_unbound_merchant_learns_nothing_about_a_claimed_run(monkeypatch):
    """The domain gate must run BEFORE the already-claimed check. With the
    order reversed, any authenticated merchant holding a run id learns whether
    it is claimed — without ever passing the gate that is supposed to be the
    price of learning anything about it."""
    res = _claim(monkeypatch, row=_funnel_row(merchant_id="someone-else"),
                 bound={"notmine.com"})
    assert res.status_code == 403, (
        "an unbound caller must be refused before the claimed-state check"
    )
    assert res.json()["detail"]["error"] == "DOMAIN_NOT_BOUND"


def test_a_refusal_does_not_echo_the_domain_back(monkeypatch):
    """The 403 body used to include the run's domain, handing an unbound
    caller a fact they did not supply."""
    res = _claim(monkeypatch, row=_funnel_row(), bound={"notmine.com"})
    assert "anua.com" not in res.text


def test_a_run_from_another_lane_cannot_be_claimed_here(monkeypatch):
    res = _claim(monkeypatch, row=_funnel_row(subject_type="merchant_url"),
                 bound={"anua.com"})
    assert res.status_code == 404


def test_the_claim_route_is_not_under_the_public_prefix():
    """An authenticated route under /public/* is one misread nginx rule from
    being treated as anonymous."""
    assert sap.claim_router.prefix == "/api/merchant-center"
    assert not any(
        r.path.startswith("/public/") for r in sap.claim_router.routes
    )
    assert all(r.path.startswith("/public/") for r in sap.router.routes)


# ---- the intake route, driven with a WORKING producer ----------------------
#
# Every route test above this point ran on SQLite, where the producer's
# ARRAY-typed INSERT silently returns None — so the endpoint was exercising
# its fallback, not its feature. These stub the two producer calls so the
# route's own wiring is what is under test. Without them, deleting
# `audit_run_id=` from the 202 response passes the whole suite.

def _intake_client(monkeypatch, *, route_run_id=None, produced="new-run-1",
                   reused=None):
    """The intake handler with its DB seams stubbed."""
    seen = {"created": [], "enqueued": []}

    async def fake_find(*, domain, since, limit=2000):
        seen.setdefault("find", []).append(domain)
        return {"run_id": reused} if reused else None

    async def fake_record(*, domain):
        seen["created"].append(domain)
        return produced

    async def no_evidence(**_k):
        return None

    async def fake_route(**_k):
        return {"execution_route_id": "er-1",
                "last_audit_run_id": route_run_id}

    async def fake_enqueue(**kw):
        seen["enqueued"].append(kw)
        return "vr-1"

    async def fake_count(**_k):
        return 0

    monkeypatch.setattr(sap, "_enabled", lambda: True)
    monkeypatch.setattr(sap, "find_unclaimed_funnel_run_for_domain", fake_find)
    monkeypatch.setattr(sap, "record_anonymous_funnel_run", fake_record)
    monkeypatch.setattr(sap, "fetch_latest_route_evidence_for_domain", no_evidence)
    monkeypatch.setattr(sap, "fetch_latest_verification_for_domain", no_evidence)
    monkeypatch.setattr(sap, "fetch_route_for_domain", fake_route)
    monkeypatch.setattr(sap, "enqueue_verification_run", fake_enqueue)
    monkeypatch.setattr(sap, "count_recent_intake_verifications", fake_count)
    monkeypatch.setattr(
        sap, "_intake_limiter",
        sap._SlidingWindowLimiter(limit=50, window_seconds=60.0))
    app = FastAPI()
    app.include_router(sap.router)
    return TestClient(app), seen


def test_the_intake_returns_the_run_id_it_created(monkeypatch):
    """The PR's headline output field. Nothing asserted it at the HTTP layer,
    so deleting it from the response passed the entire suite."""
    client, seen = _intake_client(monkeypatch)
    res = client.post("/public/store-audit/intake",
                      json={"store_url": "https://anua.com"})
    assert res.status_code == 202
    assert res.json()["audit_run_id"] == "new-run-1"
    assert seen["created"] == ["anua.com"]
    assert seen["enqueued"][0]["audit_run_id"] == "new-run-1"


def test_the_intake_reuses_rather_than_creating(monkeypatch):
    client, seen = _intake_client(monkeypatch, reused="old-run-9")
    res = client.post("/public/store-audit/intake",
                      json={"store_url": "https://anua.com"})
    assert res.json()["audit_run_id"] == "old-run-9"
    assert seen["created"] == []


def test_a_route_that_already_points_somewhere_is_never_repointed(monkeypatch):
    """THE regression this guard exists for. fetch_route_for_domain ignores
    merchant_id, the receipt path's ON CONFLICT does
    COALESCE(EXCLUDED.last_audit_run_id, ...) — a non-null value OVERWRITES —
    and the reprobe job reads that pointer. Without this, an anonymous visitor
    typing a live merchant's domain repoints that merchant's route at an
    unowned run, and every future reprobe deposits their acceptance evidence
    somewhere anyone can read."""
    client, seen = _intake_client(monkeypatch, route_run_id="merchant-run-7")
    res = client.post("/public/store-audit/intake",
                      json={"store_url": "https://anua.com"})
    assert res.status_code == 202
    # No funnel run minted, and the merchant's own id is what the probe keeps.
    assert seen["created"] == []
    assert seen["enqueued"][0]["audit_run_id"] == "merchant-run-7"
    # ...and it is NOT handed to the anonymous caller.
    assert res.json()["audit_run_id"] is None


def test_a_persistence_failure_never_returns_someone_elses_run_id(monkeypatch):
    """The fallback used to be route.last_audit_run_id, which put a real
    merchant's run id in an unauthenticated response body."""
    client, seen = _intake_client(monkeypatch, produced=None)
    res = client.post("/public/store-audit/intake",
                      json={"store_url": "https://anua.com"})
    assert res.status_code == 202
    assert res.json()["audit_run_id"] is None
    enq = seen["enqueued"][0]["audit_run_id"]
    assert enq and enq != "merchant-run-7"


def test_the_public_read_is_rate_limited(monkeypatch):
    """The only limiter on the new unauthenticated GET. Deleting it passed
    every test."""
    monkeypatch.setattr(
        sap, "_teaser_limiter",
        sap._SlidingWindowLimiter(limit=2, window_seconds=60.0))
    c = _client(monkeypatch, row=_funnel_row())
    assert c.get("/public/store-audit/run/r-1").status_code == 200
    assert c.get("/public/store-audit/run/r-1").status_code == 200
    assert c.get("/public/store-audit/run/r-1").status_code == 429


# ---- the reuse lookup's own guarantees --------------------------------------

async def test_the_reuse_lookup_returns_the_FRESHEST_run(db):
    """`ORDER BY requested_at DESC`. Flipping it to ASC passed everything —
    'the freshest unclaimed run' was asserted nowhere."""
    old = await _insert(db, domain="anua.com", when=_now() - timedelta(hours=6))
    new = await _insert(db, domain="anua.com", when=_now() - timedelta(minutes=5))
    found = await mar.find_unclaimed_funnel_run_for_domain(
        domain="anua.com", since=_now() - timedelta(hours=12))
    assert found["run_id"] == new and found["run_id"] != old


async def test_the_scan_bound_exceeds_what_the_reuse_window_can_hold(db):
    """If `limit` is smaller than the number of unclaimed runs inside the
    window, an older run for the domain falls off the end and the caller mints
    a duplicate — silently, and worse the busier the funnel gets."""
    target = await _insert(db, domain="target.com",
                           when=_now() - timedelta(hours=2))
    for i in range(250):
        await _insert(db, domain=f"filler{i}.com",
                      when=_now() - timedelta(minutes=i))
    found = await mar.find_unclaimed_funnel_run_for_domain(
        domain="target.com", since=_now() - timedelta(hours=24))
    assert found and found["run_id"] == target, (
        "the target fell outside the candidate scan — the default limit is "
        "too small for the window it is called with"
    )

