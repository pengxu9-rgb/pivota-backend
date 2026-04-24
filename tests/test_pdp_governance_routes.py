import os
import tempfile
from contextlib import asynccontextmanager

os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{os.path.join(tempfile.gettempdir(), f'pivota_pdp_routes_{os.getpid()}.db')}",
)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from db.database import database
from routes.employee_pdp_governance import router as employee_pdp_router
from routes.merchant_pdp import router as merchant_pdp_router
from utils.auth import get_current_employee, get_current_user


def _employee_user():
    return {
        "sub": "employee-test",
        "email": "employee@example.com",
        "role": "admin",
    }


def _merchant_user():
    return {
        "sub": "merchant-test",
        "email": "merchant@example.com",
        "role": "merchant",
        "merchant_id": "test-merchant",
    }


def _client() -> TestClient:
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
    app.include_router(merchant_pdp_router)
    app.dependency_overrides[get_current_employee] = _employee_user
    app.dependency_overrides[get_current_user] = _merchant_user
    return TestClient(app)


def test_employee_external_only_projection_exposes_module_governance_fields():
    with _client() as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-1", "market": "US"},
        )
        assert resolved.status_code == 200
        pdp_id = resolved.json()["pdp"]["pdp_id"]

        detail = client.get(f"/employee/pdps/{pdp_id}")
        assert detail.status_code == 200
        body = detail.json()

        assert body["pdp"]["subject_type"] == "external_product"
        assert body["pdp"]["external_only"] is True
        assert body["published_payload"]["identity"]["external_product_id"] == "external-route-1"

        for module in body["modules"]:
            assert "status" in module
            assert "source_refs" in module
            assert "last_reviewer" in module
            assert "review_actor_type" in module
            assert "published_payload" in module


def test_gpt55_gate_can_publish_low_risk_llm_candidate_after_review():
    with _client() as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-2", "market": "US"},
        )
        pdp_id = resolved.json()["pdp"]["pdp_id"]

        draft = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/draft",
            json={
                "payload": {
                    "title": "Plain Cotton Tee",
                    "description": "Soft everyday shirt for casual wear.",
                },
                "source_refs": [{"type": "external_seed", "id": "external-route-2"}],
                "generated_by": "llm_candidate",
                "generation_ref": "gen-route-test",
            },
        )
        assert draft.status_code == 200
        draft_id = draft.json()["module"]["id"]

        reviewed = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/gpt55-review",
            json={
                "version_id": draft_id,
                "rubric": {
                    "decision": "pass",
                    "confidence": 0.94,
                    "reasons": ["source-grounded low-risk copy reviewed in Codex window"],
                    "checks": {
                        "source_grounded": True,
                        "seller_entity_checkout_not_confused": True,
                        "variant_market_consistent": True,
                        "no_medical_regulated_promo_or_fake_review_claim": True,
                        "machine_publish_allowed_module": True,
                    },
                    "evidence_refs": ["external_seed:external-route-2"],
                    "reviewed_in": "codex_external_window",
                },
            },
        )
        assert reviewed.status_code == 200
        body = reviewed.json()
        assert body["decision"] == "pass"
        assert body["published"] is True
        assert body["module"]["review_actor_type"] == "gpt55_quality_gate"
        assert body["module"]["review_model"] == "gpt-5.5"

        detail = client.get(f"/employee/pdps/{pdp_id}").json()
        assert detail["published_payload"]["copy"]["title"] == "Plain Cotton Tee"


def test_gpt55_gate_does_not_publish_codex_pass_with_failed_check():
    with _client() as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-failed-codex-check", "market": "US"},
        )
        pdp_id = resolved.json()["pdp"]["pdp_id"]

        draft = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/draft",
            json={
                "payload": {
                    "title": "Plain Cotton Tee",
                    "description": "Soft everyday shirt for casual wear.",
                },
                "source_refs": [{"type": "external_seed", "id": "external-route-failed-codex-check"}],
                "generated_by": "llm_candidate",
            },
        )
        assert draft.status_code == 200

        reviewed = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/gpt55-review",
            json={
                "version_id": draft.json()["module"]["id"],
                "rubric": {
                    "decision": "pass",
                    "confidence": 0.94,
                    "reasons": ["Codex reviewer found source grounding incomplete."],
                    "checks": {
                        "source_grounded": False,
                        "seller_entity_checkout_not_confused": True,
                        "variant_market_consistent": True,
                        "no_medical_regulated_promo_or_fake_review_claim": True,
                        "machine_publish_allowed_module": True,
                    },
                    "evidence_refs": ["external_seed:external-route-failed-codex-check"],
                    "reviewed_in": "codex_external_window",
                },
            },
        )
        assert reviewed.status_code == 200
        body = reviewed.json()
        assert body["decision"] == "needs_human_review"
        assert body["published"] is False
        assert "codex_pass_failed_checks:source_grounded" in body["rubric"]["reasons"]


def test_gpt55_gate_requires_codex_rubric_artifact_for_low_risk_publish():
    with _client() as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-rubric-required", "market": "US"},
        )
        pdp_id = resolved.json()["pdp"]["pdp_id"]

        draft = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/draft",
            json={
                "payload": {"title": "Plain Cotton Tee", "description": "Soft everyday shirt."},
                "source_refs": [{"type": "external_seed", "id": "external-route-rubric-required"}],
                "generated_by": "llm_candidate",
            },
        )
        reviewed = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/gpt55-review",
            json={"version_id": draft.json()["module"]["id"]},
        )
        assert reviewed.status_code == 200
        body = reviewed.json()
        assert body["decision"] == "needs_human_review"
        assert body["published"] is False
        assert "codex_gpt55_review_artifact_required" in body["rubric"]["reasons"]


def test_gpt55_gate_does_not_publish_human_required_gallery():
    with _client() as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-3", "market": "US"},
        )
        pdp_id = resolved.json()["pdp"]["pdp_id"]

        draft = client.post(
            f"/employee/pdps/{pdp_id}/modules/gallery/draft",
            json={
                "payload": {
                    "images": [{"url": "https://example.com/photo.jpg", "rights_status": "third_party"}],
                },
                "source_refs": [{"type": "external_seed", "id": "external-route-3"}],
                "generated_by": "llm_candidate",
            },
        )
        assert draft.status_code == 200

        reviewed = client.post(
            f"/employee/pdps/{pdp_id}/modules/gallery/gpt55-review",
            json={"version_id": draft.json()["module"]["id"]},
        )
        assert reviewed.status_code == 200
        body = reviewed.json()
        assert body["decision"] == "needs_human_review"
        assert body["published"] is False


def test_merchant_contribution_is_staged_not_directly_published():
    with _client() as client:
        status = client.get("/merchant/pdps/product/shopify/route-sku-1", params={"market": "US"})
        assert status.status_code == 200
        pdp_id = status.json()["pdp"]["pdp_id"]

        submitted = client.post(
            "/merchant/pdps/product/shopify/route-sku-1/contributions",
            json={
                "module_key": "copy",
                "payload": {"title": "Merchant suggested title"},
                "notes": "Merchant correction",
                "market": "US",
            },
        )
        assert submitted.status_code == 200
        assert submitted.json()["contribution"]["status"] == "submitted"
        assert submitted.json()["draft"]["stage"] == "staged"

        after = client.get("/merchant/pdps/product/shopify/route-sku-1", params={"market": "US"})
        copy_module = next(m for m in after.json()["modules"] if m["module_key"] == "copy")
        assert copy_module["staged"]["generated_by"] == "merchant_contribution"
        assert copy_module["published_payload"].get("title") != "Merchant suggested title"
        assert after.json()["pdp"]["pdp_id"] == pdp_id
