"""The employee GPT-5.5 review lane is an LLM-ONLY publish authority.

`POST /employee/pdps/{pdp_id}/modules/{module_key}/gpt55-review` calls
`review_module_version(actor_type=REVIEW_ACTOR_GPT55, ...)`. That actor_type
skips the whole `if actor_type == REVIEW_ACTOR_HUMAN` RBAC block in the service
(`allowed_pdp_review_actions`), and the route passes no `actor_role` at all --
so an employee token of ANY role drove an auto-publish for every module in
`MACHINE_PUBLISH_MODULES`, `offers` and `identity` included, with no per-role
publish check anywhere on the path.

The merchant-facing twin of the same call (`routes/merchant_pdp.py::
approve_product_pdp_module`) already fences itself to a named module set before
it calls the service. These tests pin the employee route to the SAME shared set
and prove the fence is enforced in the route, before the service call, with no
row written.
"""

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from db.database import database
from routes.employee_pdp_governance import router as employee_pdp_router
from services.pdp_governance_service import (
    MACHINE_PUBLISH_MODULES,
    PDP_MODULE_KEYS,
    pdp_module_versions,
)
from utils.auth import get_current_employee


def _employee_user(role: str = "admin"):
    return {"sub": "employee-test", "email": "employee@example.com", "role": role}


def _client(role: str = "admin") -> TestClient:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not database.is_connected:
            await database.connect()
        try:
            yield
        finally:
            if database.is_connected:
                await database.disconnect()

    app = FastAPI(lifespan=lifespan)
    app.include_router(employee_pdp_router)
    app.dependency_overrides[get_current_employee] = lambda: _employee_user(role)
    return TestClient(app)


def _codex_rubric(evidence_ref: str):
    """A well-formed Codex artifact that the merge path accepts as a pass --
    every required check true, evidence present, right review channel. Nothing
    but a route-level module fence stands between this and a publish."""
    return {
        "decision": "pass",
        "confidence": 0.94,
        "reasons": ["source-grounded, reviewed in Codex window"],
        "checks": {
            "source_grounded": True,
            "seller_entity_checkout_not_confused": True,
            "variant_market_consistent": True,
            "no_medical_regulated_promo_or_fake_review_claim": True,
            "machine_publish_allowed_module": True,
        },
        "evidence_refs": [evidence_ref],
        "reviewed_in": "codex_external_window",
    }


def _resolve(client: TestClient, seed_id: str) -> str:
    resolved = client.get(
        "/employee/pdps/resolve",
        params={"product_key": f"external_seed|external|{seed_id}", "market": "US"},
    )
    assert resolved.status_code == 200, resolved.text
    return resolved.json()["pdp"]["pdp_id"]


def _draft(client: TestClient, pdp_id: str, module_key: str, payload: dict, seed_id: str) -> str:
    draft = client.post(
        f"/employee/pdps/{pdp_id}/modules/{module_key}/draft",
        json={
            "payload": payload,
            "source_refs": [{"type": "external_seed", "id": seed_id}],
            "generated_by": "llm_candidate",
        },
    )
    assert draft.status_code == 200, draft.text
    return draft.json()["module"]["id"]


async def _version_row(version_id: str):
    return await database.fetch_one(
        pdp_module_versions.select().where(pdp_module_versions.c.id == version_id)
    )


# The offers payload the LLM lane must not be able to publish. Deliberately
# benign: it passes every content check in run_gpt55_quality_gate, so the ONLY
# thing that can stop it is the module fence under test.
_OFFERS_PAYLOAD = {
    "offers": [
        {
            "seller": "Example Shop",
            "price": "19.99",
            "currency": "USD",
            "availability": "in_stock",
        }
    ]
}


@pytest.mark.parametrize("role", ["admin", "employee", "outsourced"])
def test_gpt55_review_refuses_offers_module_and_writes_nothing(role):
    """REGRESSION. Against the pre-fix build this publishes: 200, decision
    'pass', published True, and the offers row flips to stage 'published'.
    Money-bearing offer content, auto-published by an LLM artifact the caller
    supplied, on a token whose role was never consulted."""
    seed_id = f"external-allowlist-offers-{role}"
    with _client(role=role) as client:
        pdp_id = _resolve(client, seed_id)
        version_id = _draft(client, pdp_id, "offers", _OFFERS_PAYLOAD, seed_id)

        reviewed = client.post(
            f"/employee/pdps/{pdp_id}/modules/offers/gpt55-review",
            json={"version_id": version_id, "rubric": _codex_rubric(f"external_seed:{seed_id}")},
        )

        assert reviewed.status_code == 400, reviewed.text
        assert reviewed.json()["detail"] == "MODULE_NOT_LLM_REVIEWABLE"

        # ...and NOTHING was written: the draft is untouched, no review actor,
        # no decision, still staged.
        row = client.portal.call(_version_row, version_id)
        assert row is not None
        assert row["stage"] == "staged"
        assert row["status"] == "draft"
        assert row["review_actor_type"] is None
        assert row["review_decision"] is None
        assert row["published_at"] is None

        # The projection still serves only the system baseline offers module --
        # our submitted offer never reached the published payload. (Asserting
        # `offers is None` would be vacuous: the baseline projection publishes an
        # offers module of its own, so the real claim is that OUR content is
        # absent from it.)
        detail = client.get(f"/employee/pdps/{pdp_id}").json()
        assert "Example Shop" not in json.dumps(detail["published_payload"] or {})
        assert "19.99" not in json.dumps(detail["published_payload"] or {})


def test_gpt55_review_still_publishes_an_allowed_module():
    """Positive counterpart: the fence narrows the lane, it does not close it.
    'copy' -- the one module the merchant twin also self-approves -- still
    rides the LLM-only publish path end to end."""
    seed_id = "external-allowlist-copy-allowed"
    with _client() as client:
        pdp_id = _resolve(client, seed_id)
        version_id = _draft(
            client,
            pdp_id,
            "copy",
            {"title": "Plain Cotton Tee", "description": "Soft everyday shirt for casual wear."},
            seed_id,
        )

        reviewed = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/gpt55-review",
            json={"version_id": version_id, "rubric": _codex_rubric(f"external_seed:{seed_id}")},
        )

        assert reviewed.status_code == 200, reviewed.text
        body = reviewed.json()
        assert body["decision"] == "pass"
        assert body["published"] is True
        assert body["module"]["review_actor_type"] == "gpt55_quality_gate"

        detail = client.get(f"/employee/pdps/{pdp_id}").json()
        assert detail["published_payload"]["copy"]["title"] == "Plain Cotton Tee"


def test_gpt55_review_refuses_a_human_co_review_module_before_the_service_call():
    """gallery is machine-UNpublishable AND human-co-review. Pre-fix the route
    let it reach the service, which returned 200/needs_human_review. The fence
    turns that into a refusal at the door -- same outcome for the data, an
    explicit one for the caller."""
    seed_id = "external-allowlist-gallery"
    with _client() as client:
        pdp_id = _resolve(client, seed_id)
        version_id = _draft(
            client,
            pdp_id,
            "gallery",
            {"images": [{"url": "https://example.com/photo.jpg"}]},
            seed_id,
        )

        reviewed = client.post(
            f"/employee/pdps/{pdp_id}/modules/gallery/gpt55-review",
            json={"version_id": version_id, "rubric": _codex_rubric(f"external_seed:{seed_id}")},
        )
        assert reviewed.status_code == 400, reviewed.text
        assert reviewed.json()["detail"] == "MODULE_NOT_LLM_REVIEWABLE"

        row = client.portal.call(_version_row, version_id)
        assert row["review_actor_type"] is None
        assert row["published_at"] is None


def test_gpt55_review_rejects_an_unknown_module_key_without_touching_the_service():
    with _client() as client:
        reviewed = client.post(
            "/employee/pdps/pdp_does_not_matter/modules/not_a_module/gpt55-review",
            json={"version_id": "v1", "rubric": _codex_rubric("x:y")},
        )
        # The module fence runs before pdp/version resolution, so an unknown key
        # is a 400 about the module -- never a 404 that leaks whether the pdp
        # exists.
        assert reviewed.status_code == 400, reviewed.text
        assert reviewed.json()["detail"] == "MODULE_NOT_LLM_REVIEWABLE"


def test_llm_only_allowlist_is_complete_and_names_only_real_modules():
    """The allowlist is a security boundary, so it must be total: every module
    the machine-publish lane can publish is either explicitly allowed or
    explicitly excluded WITH a reason, and neither list may name a module the
    governance registry does not have."""
    from services.pdp_governance_service import (
        LLM_ONLY_PUBLISH_EXCLUSIONS,
        LLM_ONLY_PUBLISH_MODULES,
    )

    registry = set(PDP_MODULE_KEYS)

    assert LLM_ONLY_PUBLISH_MODULES, "an empty allowlist would silently close the lane"
    assert LLM_ONLY_PUBLISH_MODULES <= registry, (
        f"allowlist names modules outside the registry: "
        f"{sorted(LLM_ONLY_PUBLISH_MODULES - registry)}"
    )
    assert set(LLM_ONLY_PUBLISH_EXCLUSIONS) <= registry, (
        f"exclusions name modules outside the registry: "
        f"{sorted(set(LLM_ONLY_PUBLISH_EXCLUSIONS) - registry)}"
    )

    # No module may be both allowed and excluded.
    assert not (LLM_ONLY_PUBLISH_MODULES & set(LLM_ONLY_PUBLISH_EXCLUSIONS))

    # Every module reachable by the machine-publish lane is decided one way or
    # the other. A new entry in MACHINE_PUBLISH_MODULES that nobody classified
    # fails HERE rather than quietly widening the employee LLM lane.
    undecided = MACHINE_PUBLISH_MODULES - LLM_ONLY_PUBLISH_MODULES - set(LLM_ONLY_PUBLISH_EXCLUSIONS)
    assert not undecided, f"machine-publishable modules with no LLM-lane verdict: {sorted(undecided)}"

    # And every whole-registry module too, so gallery/reviews cannot drift in
    # unclassified either.
    undecided_registry = registry - LLM_ONLY_PUBLISH_MODULES - set(LLM_ONLY_PUBLISH_EXCLUSIONS)
    assert not undecided_registry, f"registry modules with no LLM-lane verdict: {sorted(undecided_registry)}"

    for module_key, reason in LLM_ONLY_PUBLISH_EXCLUSIONS.items():
        assert isinstance(reason, str) and reason.strip(), f"{module_key} excluded without a reason"


def test_merchant_and_employee_lanes_read_the_same_set():
    """One definition, two routes. If someone widens the merchant set the
    employee lane widens with it (and vice versa) -- they cannot drift apart
    into two different answers to 'what may an LLM-only review publish?'."""
    import routes.employee_pdp_governance as employee_route
    import routes.merchant_pdp as merchant_route
    from services.pdp_governance_service import LLM_ONLY_PUBLISH_MODULES

    assert merchant_route.MERCHANT_SELF_APPROVE_MODULES is LLM_ONLY_PUBLISH_MODULES
    assert employee_route.LLM_ONLY_PUBLISH_MODULES is LLM_ONLY_PUBLISH_MODULES
