"""Tenant scoping on `/v1/catalog/*`.

Every route in `routes/catalog_routes.py` used to be guarded only by
`Depends(get_current_user)`, which accepts ANY valid JWT of ANY role and was
consumed only for a `requested_by` label. The `merchant_id` came straight from
the request body / path / query, so any token holder could drive
`catalog_merchants` / `catalog_products` / `catalog_skus` / `catalog_offers`
writes against an arbitrary merchant — including ADR-009 `merch_obs_*`
observed-seller identities, which belong to no tenant.

These tests drive the REAL routes through the REAL router. Every refusal case
also asserts the underlying service was never called, because a 403 returned
after the write would leave the defect fully open while looking fixed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.catalog_routes as module
from utils.auth import get_current_user


VICTIM = "merch_obs_victim"

ADMIN = {"sub": "u_admin", "email": "ops@pivota.invalid", "role": "admin"}
SUPER_ADMIN = {"sub": "u_su", "email": "su@pivota.invalid", "role": "super_admin"}
SELF_MERCHANT = {"sub": "u_v", "email": "v@x.invalid", "role": "merchant", "merchant_id": VICTIM}

# Every identity below is a valid JWT payload that `get_current_user` returns
# happily, and none of them owns VICTIM.
OTHER_MERCHANT = {"sub": "u_o", "email": "o@x.invalid", "role": "merchant", "merchant_id": "merch_other"}
AGENT = {"sub": "u_a", "email": "a@x.invalid", "role": "agent", "agent_id": "agent_1"}
EMPLOYEE = {"sub": "u_e", "email": "e@x.invalid", "role": "employee", "employee_id": "emp_1"}
NO_MERCHANT_CLAIM = {"sub": "u_n", "email": "n@x.invalid", "role": "merchant"}

# Near-misses. The comparison must be EXACT equality: case-insensitive,
# prefix, or substring matching all look harmless until you notice that a
# tenant legitimately holding `merch_obs` would reach every ADR-009
# `merch_obs_*` observed-seller id — precisely the population this gate exists
# to protect.
NEAR_MISS_IDENTITIES = [
    pytest.param({**SELF_MERCHANT, "merchant_id": VICTIM.upper()}, id="claim-differs-only-in-case"),
    pytest.param({**SELF_MERCHANT, "merchant_id": "merch_obs"}, id="claim-is-a-prefix-of-the-target"),
    pytest.param({**SELF_MERCHANT, "merchant_id": "obs"}, id="claim-is-a-substring-of-the-target"),
    pytest.param({**SELF_MERCHANT, "merchant_id": VICTIM + "_x"}, id="target-is-a-prefix-of-the-claim"),
]

REFUSED_IDENTITIES = [
    pytest.param(OTHER_MERCHANT, id="other-merchant"),
    pytest.param(AGENT, id="agent"),
    pytest.param(EMPLOYEE, id="employee"),
    pytest.param(NO_MERCHANT_CLAIM, id="merchant-token-without-merchant_id-claim"),
    *NEAR_MISS_IDENTITIES,
]

ALLOWED_IDENTITIES = [
    pytest.param(ADMIN, id="admin"),
    pytest.param(SUPER_ADMIN, id="super-admin"),
    pytest.param(SELF_MERCHANT, id="the-merchant-itself"),
]


def _build_app(user: Dict[str, Any]) -> FastAPI:
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return app


@pytest.fixture
def sinks(monkeypatch) -> Dict[str, List[Dict[str, Any]]]:
    """Record every service call the routes can make, and fake a success."""
    calls: Dict[str, List[Dict[str, Any]]] = {
        "create_catalog_sync_job": [],
        "background": [],
        "record_catalog_sync_event": [],
        "rebuild_beauty_verticals_for_merchant": [],
        "reconcile_catalog_incentives_for_merchant": [],
        "get_catalog_sync_job": [],
    }

    async def fake_create_catalog_sync_job(**kwargs):
        calls["create_catalog_sync_job"].append(kwargs)
        return {
            "job_id": "job_123",
            "merchant_id": kwargs["merchant_id"],
            "connector": kwargs["connector"],
            "mode": kwargs["mode"],
            "status": "pending",
            "scope_json": kwargs["scope"],
            "stats_json": {},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    async def fake_background(**kwargs):
        calls["background"].append(kwargs)

    async def fake_record_catalog_sync_event(**kwargs):
        calls["record_catalog_sync_event"].append(kwargs)
        return {
            "event_id": "evt_1",
            "merchant_id": kwargs["merchant_id"],
            "connector": "shopify",
            "event_type": kwargs["event_type"],
            "topic": kwargs.get("topic"),
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        }

    async def fake_rebuild(**kwargs):
        calls["rebuild_beauty_verticals_for_merchant"].append(kwargs)
        return {"merchant_id": kwargs["merchant_id"], "rebuilt": 3}

    async def fake_reconcile_incentives(**kwargs):
        calls["reconcile_catalog_incentives_for_merchant"].append(kwargs)
        return {
            "merchant_id": kwargs["merchant_id"],
            "source_system": kwargs["source_system"],
            "payment_incentives_synced": 1,
            "offer_links_synced": 2,
            "reconciled_at": datetime.now(timezone.utc),
        }

    async def fake_get_catalog_sync_job(job_id: str):
        calls["get_catalog_sync_job"].append({"job_id": job_id})
        if job_id != "job_of_victim":
            return None
        return {
            "job_id": job_id,
            "merchant_id": VICTIM,
            "connector": "shopify",
            "mode": "reconcile",
            "status": "completed",
            "scope_json": {"limit": 500},
            "stats_json": {"products_ingested": 41},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    monkeypatch.setattr(module, "create_catalog_sync_job", fake_create_catalog_sync_job)
    monkeypatch.setattr(module, "_run_catalog_job_background", fake_background)
    monkeypatch.setattr(module, "record_catalog_sync_event", fake_record_catalog_sync_event)
    monkeypatch.setattr(module, "rebuild_beauty_verticals_for_merchant", fake_rebuild)
    monkeypatch.setattr(module, "reconcile_catalog_incentives_for_merchant", fake_reconcile_incentives)
    monkeypatch.setattr(module, "get_catalog_sync_job", fake_get_catalog_sync_job)
    return calls


# --- the write routes ------------------------------------------------------
#
# (path, http call kwargs, the sinks key that proves the write was reached)

WRITE_ROUTES = [
    pytest.param(
        {
            "method": "post",
            "url": "/v1/catalog/sync/jobs",
            "json": {"merchant_id": VICTIM, "connector": "shopify", "mode": "reconcile", "limit": 250},
        },
        "create_catalog_sync_job",
        id="POST-sync-jobs",
    ),
    pytest.param(
        {
            "method": "post",
            "url": f"/v1/catalog/connectors/shopify/webhooks?merchant_id={VICTIM}&event_type=products/update",
            "json": {"id": 1},
        },
        "record_catalog_sync_event",
        id="POST-shopify-webhooks",
    ),
    pytest.param(
        {"method": "post", "url": f"/v1/catalog/reconcile/merchants/{VICTIM}"},
        "create_catalog_sync_job",
        id="POST-reconcile-merchants",
    ),
    pytest.param(
        {"method": "post", "url": f"/v1/catalog/verticals/beauty/rebuild/{VICTIM}"},
        "rebuild_beauty_verticals_for_merchant",
        id="POST-beauty-rebuild",
    ),
    pytest.param(
        {
            "method": "post",
            "url": f"/v1/catalog/incentives/reconcile/{VICTIM}",
            "json": {"source_system": "merchant_config", "payment_incentives": []},
        },
        "reconcile_catalog_incentives_for_merchant",
        id="POST-incentives-reconcile",
    ),
]


def _call(client: TestClient, spec: Dict[str, Any]):
    kwargs = {k: v for k, v in spec.items() if k not in {"method", "url"}}
    return getattr(client, spec["method"])(spec["url"], **kwargs)


@pytest.mark.parametrize("spec,sink_key", WRITE_ROUTES)
@pytest.mark.parametrize("user", REFUSED_IDENTITIES)
def test_non_admin_cannot_write_another_merchants_catalog(sinks, spec, sink_key, user) -> None:
    client = TestClient(_build_app(user))

    response = _call(client, spec)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "cannot run catalog jobs for another merchant"
    # The refusal must land BEFORE the write, not after it.
    assert sinks[sink_key] == []
    assert sinks["background"] == []


@pytest.mark.parametrize("spec,sink_key", WRITE_ROUTES)
@pytest.mark.parametrize("user", ALLOWED_IDENTITIES)
def test_admins_and_the_owning_merchant_are_still_allowed(sinks, spec, sink_key, user) -> None:
    client = TestClient(_build_app(user))

    response = _call(client, spec)

    assert response.status_code == 200, response.text
    assert len(sinks[sink_key]) == 1
    assert sinks[sink_key][0]["merchant_id"] == VICTIM


def test_blank_merchant_id_does_not_match_a_token_without_the_claim(sinks) -> None:
    """`"" == ""` must not be an authorization decision.

    A token with no `merchant_id` claim and a blank requested id would both
    normalize to the empty string; only the truthiness guard in
    `_has_catalog_scope` stops that pair from matching.
    """
    client = TestClient(_build_app(NO_MERCHANT_CLAIM))

    response = client.post("/v1/catalog/sync/jobs", json={"merchant_id": "", "connector": "shopify"})

    assert response.status_code == 403, response.text
    assert sinks["create_catalog_sync_job"] == []


@pytest.mark.parametrize(
    "spec,sink_key,padded",
    [
        pytest.param(
            {"method": "post", "url": "/v1/catalog/sync/jobs",
             "json": {"merchant_id": "\u00a0" + VICTIM + "\n", "connector": "shopify"}},
            "create_catalog_sync_job", None, id="body-merchant_id",
        ),
        pytest.param(
            {"method": "post", "url": f"/v1/catalog/verticals/beauty/rebuild/%20{VICTIM}%20"},
            "rebuild_beauty_verticals_for_merchant", None, id="path-merchant_id",
        ),
        pytest.param(
            {"method": "post",
             "url": f"/v1/catalog/connectors/shopify/webhooks?merchant_id=%20{VICTIM}%20&event_type=products/update",
             "json": {}},
            "record_catalog_sync_event", None, id="query-merchant_id",
        ),
    ],
)
def test_the_id_that_was_authorized_is_the_id_that_is_written(sinks, spec, sink_key, padded) -> None:
    """The gate must normalize ONCE and pass the normalized id downstream.

    `.strip()` eats NBSP, newlines and spaces, so authorizing the stripped
    value while handing the sink the raw one lets a caller mint a
    `catalog_merchants` row keyed on "\xa0merch_obs_victim\n". Self-targeted
    only — you can pad only your own id — so this is data pollution rather
    than escalation, but the authorized id and the written id must agree.
    """
    client = TestClient(_build_app(SELF_MERCHANT))

    response = _call(client, spec)

    assert response.status_code == 200, response.text
    assert sinks[sink_key][0]["merchant_id"] == VICTIM
    if sink_key == "create_catalog_sync_job":
        # The BackgroundTask carries its own copy of the id.
        assert sinks["background"][0]["merchant_id"] == VICTIM


def test_whitespace_padding_does_not_defeat_the_comparison(sinks) -> None:
    client = TestClient(_build_app({**SELF_MERCHANT, "merchant_id": f"  {VICTIM}  "}))

    response = client.post(f"/v1/catalog/reconcile/merchants/{VICTIM}")

    assert response.status_code == 200, response.text
    assert sinks["create_catalog_sync_job"][0]["merchant_id"] == VICTIM


# --- the read route --------------------------------------------------------


@pytest.mark.parametrize("user", REFUSED_IDENTITIES)
def test_reading_another_merchants_sync_job_is_not_found(sinks, user) -> None:
    """404, not 403 — a 403 would confirm the job id exists."""
    client = TestClient(_build_app(user))

    response = client.get("/v1/catalog/sync/jobs/job_of_victim")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Catalog sync job not found"
    # Identical to the response for an id that does not exist at all.
    assert client.get("/v1/catalog/sync/jobs/no_such_job").json() == response.json()


@pytest.mark.parametrize("user", ALLOWED_IDENTITIES)
def test_admins_and_the_owning_merchant_can_read_the_sync_job(sinks, user) -> None:
    client = TestClient(_build_app(user))

    response = client.get("/v1/catalog/sync/jobs/job_of_victim")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["merchant_id"] == VICTIM
    assert body["stats"]["products_ingested"] == 41


def test_every_route_on_the_catalog_router_refuses_a_foreign_identity(sinks) -> None:
    """A new route on this router must actually REFUSE, not merely mention the guard.

    This deliberately does not inspect source text. The previous version of this
    canary grepped `inspect.getsource` for the guard's name, which a handler
    satisfied by naming it in a docstring or comment while being wide open — a
    realistic copy-paste, since `_require_catalog_scope`'s own docstring mentions
    it three times. So drive every route with a foreign identity instead and
    require a refusal.
    """
    client = TestClient(_build_app(AGENT))

    unguarded = []
    for route in module.router.routes:
        path = route.path.replace("{merchant_id}", VICTIM).replace("{job_id}", "job_of_victim")
        for method in sorted(set(route.methods) - {"HEAD", "OPTIONS"}):
            response = client.request(
                method,
                path,
                # Satisfy every handler's schema so the refusal we observe is the
                # gate and never a 422 from validation running first.
                params={"merchant_id": VICTIM, "event_type": "products/update"},
                json={"merchant_id": VICTIM, "connector": "shopify"},
            )
            if response.status_code not in (403, 404):
                unguarded.append(f"{method} {route.path} -> {response.status_code}")

    assert unguarded == [], f"catalog routes that did not refuse a foreign identity: {unguarded}"
    # And nothing was written on the way to those refusals.
    assert all(calls == [] for key, calls in sinks.items() if key != "get_catalog_sync_job")
