"""POST /api/brands/official-domains/declare — the ROUTE, not the service.

D6 of the fifth review: nothing anywhere called this endpoint. Auth scoping, the
status-to-HTTP mapping and body validation were all unverified, and two of the
service's refusals had already shipped as HTTP 200 once because the mapping was
asserted by no test. Fresh app + auth override + monkeypatched service, mirroring
tests/test_brand_claim_routes.py — no DB.

The security property: the merchant is the AUTHENTICATED one, never a body field.
The contract property: every refusal the service can return has a non-2xx code,
and "we could not check" is neither a 4xx nor a "taken".
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import brand_claim_routes
from services import brand_claim_service as svc
from utils.auth import get_current_merchant

PATH = "/api/brands/official-domains/declare"


def _client(merchant: str = "merch_self") -> TestClient:
    app = FastAPI()
    app.include_router(brand_claim_routes.router)

    async def _override() -> str:
        return merchant

    app.dependency_overrides[get_current_merchant] = _override
    return TestClient(app)


def _service_returning(monkeypatch, result: dict):
    calls = []

    async def _fake(merchant_id, domain):
        calls.append((merchant_id, domain))
        return dict(result)

    monkeypatch.setattr(svc, "declare_official_domain", _fake)
    return calls


def test_the_merchant_comes_from_the_token_never_the_body(monkeypatch):
    calls = _service_returning(monkeypatch, {"status": svc.DECLARE_OK, "domain": "anua.us"})
    r = _client("merch_self").post(
        PATH, json={"domain": "anua.us", "merchant_id": "merch_victim"},
    )
    assert r.status_code == 200, r.text
    assert calls == [("merch_self", "anua.us")]


def test_unauthenticated_is_refused_before_the_service_runs(monkeypatch):
    calls = _service_returning(monkeypatch, {"status": svc.DECLARE_OK, "domain": "anua.us"})
    app = FastAPI()
    app.include_router(brand_claim_routes.router)  # no auth override
    r = TestClient(app).post(PATH, json={"domain": "anua.us"})
    assert r.status_code in (401, 403), r.text
    assert calls == []


@pytest.mark.parametrize("body", [{}, {"domain": None}, {"dmain": "anua.us"}, {"domain": 12}])
def test_a_malformed_body_is_422_before_the_service_runs(monkeypatch, body):
    calls = _service_returning(monkeypatch, {"status": svc.DECLARE_OK, "domain": "x"})
    r = _client().post(PATH, json=body)
    assert r.status_code == 422, r.text
    assert calls == []


@pytest.mark.parametrize(
    "status, code",
    [
        (svc.DECLARE_INVALID_HOST, 422),
        (svc.DECLARE_NOT_REGISTRABLE, 422),
        (svc.DECLARE_TOO_MANY, 429),
        (svc.DECLARE_TAKEN, 409),
        (svc.DECLARE_UNAVAILABLE, 503),
        (svc.DECLARE_WRITE_FAILED, 500),
    ],
)
def test_every_refusal_has_a_non_2xx_code(monkeypatch, status, code):
    """Two of these shipped as 200 once (public suffix, over cap): a client
    reading the status code saw success and nothing had been written."""
    _service_returning(monkeypatch, {"status": status, "domain": "anua.us"})
    r = _client().post(PATH, json={"domain": "anua.us"})
    assert r.status_code == code, (status, r.status_code, r.text)


def test_a_write_failure_does_not_blame_the_hostname(monkeypatch):
    _service_returning(monkeypatch, {"status": svc.DECLARE_WRITE_FAILED, "domain": "anua.us"})
    r = _client().post(PATH, json={"domain": "anua.us"})
    assert r.status_code == 500
    assert "hostname" not in r.json()["detail"].lower()


def test_unavailable_is_our_outage_not_theirs_and_not_taken(monkeypatch):
    """503, with retry wording: a 4xx tells the merchant they did something
    wrong, and 409 tells them a rival owns their domain. Neither is true when
    the owned-set read failed."""
    _service_returning(monkeypatch, {"status": svc.DECLARE_UNAVAILABLE, "domain": "anua.us"})
    r = _client().post(PATH, json={"domain": "anua.us"})
    assert r.status_code == 503
    detail = r.json()["detail"].lower()
    assert "retry" in detail
    assert "another merchant" not in detail


def test_taken_does_not_name_the_other_merchant(monkeypatch):
    _service_returning(monkeypatch, {"status": svc.DECLARE_TAKEN, "domain": "rival.com"})
    r = _client().post(PATH, json={"domain": "rival.com"})
    assert r.status_code == 409
    assert "merch" not in r.json()["detail"].lower().replace("merchant", "")


@pytest.mark.parametrize(
    "status", [svc.DECLARE_ALREADY_PROVEN, svc.DECLARE_ALREADY_KNOWN],
)
def test_nothing_to_do_answers_200_with_the_status_visible(monkeypatch, status):
    """Not a refusal of the request — the host is already the merchant's — so
    200, but the status must reach the client so the portal can say why no new
    row appeared."""
    _service_returning(monkeypatch, {"status": status, "domain": "brand.com"})
    r = _client().post(PATH, json={"domain": "brand.com"})
    assert r.status_code == 200
    assert r.json()["status"] == status


def test_a_successful_declaration_says_it_does_not_count_yet(monkeypatch):
    _service_returning(
        monkeypatch,
        {
            "status": svc.DECLARE_OK,
            "domain": "anua.us",
            "source": "declared",
            "counts_toward_official_set": False,
            "next_step": "start a claim",
        },
    )
    r = _client().post(PATH, json={"domain": "anua.us"})
    assert r.status_code == 200
    body = r.json()
    assert body["counts_toward_official_set"] is False
    assert body["source"] == "declared"


def test_every_service_status_constant_is_mapped_by_the_route():
    """A new DECLARE_* status added to the service without a branch here would
    fall through to `return result` and ship as 200 — the D4 shape again. Pin
    the set the route knows against the set the service defines."""
    import inspect

    src = inspect.getsource(brand_claim_routes.declare_official_domain)
    statuses = {
        name for name in dir(svc)
        if name.startswith("DECLARE_") and isinstance(getattr(svc, name), str)
    }
    handled_as_200 = {"DECLARE_OK", "DECLARE_ALREADY_PROVEN", "DECLARE_ALREADY_KNOWN"}
    for name in statuses - handled_as_200:
        assert f"svc.{name}" in src, f"{name} has no HTTP mapping in the route"
