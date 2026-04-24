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


def _employee_user(role: str = "admin"):
    return {
        "sub": "employee-test",
        "email": "employee@example.com",
        "role": role,
    }


def _merchant_user():
    return {
        "sub": "merchant-test",
        "email": "merchant@example.com",
        "role": "merchant",
        "merchant_id": "test-merchant",
    }


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
    app.include_router(merchant_pdp_router)
    app.dependency_overrides[get_current_employee] = lambda: _employee_user(role)
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


def test_employee_pdp_list_exposes_pagination_metadata():
    with _client() as client:
        for seed_id in ("external-route-page-1", "external-route-page-2", "external-route-page-3"):
            resolved = client.get(
                "/employee/pdps/resolve",
                params={"product_key": f"external_seed|external|{seed_id}", "market": "US"},
            )
            assert resolved.status_code == 200

        first = client.get("/employee/pdps", params={"limit": 1, "market": "US"})
        assert first.status_code == 200
        body = first.json()
        assert body["count"] == 1
        assert body["limit"] == 1
        assert body["offset"] == 0
        assert body["next_offset"] >= 1
        assert body["total"] >= 3
        assert body["has_more"] is True

        second = client.get("/employee/pdps", params={"limit": 1, "offset": body["next_offset"], "market": "US"})
        assert second.status_code == 200
        second_body = second.json()
        assert second_body["offset"] == body["next_offset"]
        assert second_body["count"] == 1
        assert second_body["next_offset"] > second_body["offset"]


def test_employee_pdp_hydration_status_and_sync_refresh():
    with _client() as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-hydration", "market": "US"},
        )
        assert resolved.status_code == 200

        status = client.get("/employee/pdps/hydration")
        assert status.status_code == 200
        status_body = status.json()
        assert status_body["status"] == "success"
        assert status_body["total"] >= 1
        assert "markets" in status_body

        refreshed = client.post("/employee/pdps/hydration", json={"limit": 10, "background": False})
        assert refreshed.status_code == 200
        refreshed_body = refreshed.json()
        assert refreshed_body["status"] == "success"
        assert refreshed_body["limit"] == 10
        assert refreshed_body["after"]["total"] >= status_body["total"]


def test_employee_gallery_image_upload_uses_database_asset_storage(monkeypatch):
    import services.pdp_governance_service as pdp_service

    monkeypatch.setattr(pdp_service, "_pdp_gallery_bucket", lambda: "")
    monkeypatch.setattr(pdp_service, "_pdp_gallery_public_base_url", lambda: "")
    monkeypatch.setattr(pdp_service, "_pdp_gallery_asset_public_base_url", lambda: "https://api.example.com")

    with _client() as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-gallery-db-upload", "market": "US"},
        )
        assert resolved.status_code == 200
        pdp_id = resolved.json()["pdp"]["pdp_id"]

        uploaded = client.post(
            f"/employee/pdps/{pdp_id}/modules/gallery/images",
            data={
                "alt_text": "Database stored image",
                "role": "gallery",
                "rights_status": "owned_or_licensed",
            },
            files={"file": ("front.png", b"fake-db-image-bytes", "image/png")},
        )
        assert uploaded.status_code == 200
        body = uploaded.json()
        image = body["image"]
        assert image["url"].startswith("https://api.example.com/employee/pdps/gallery-assets/pdp_gallery_asset_")
        assert image["storage"]["type"] == "database"
        assert body["module"]["status"] == "needs_human_review"

        asset_id = image["storage"]["asset_id"]
        asset = client.get(f"/employee/pdps/gallery-assets/{asset_id}")
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith("image/png")
        assert asset.content == b"fake-db-image-bytes"


def test_employee_gallery_image_upload_creates_human_review_draft(monkeypatch):
    import services.pdp_governance_service as pdp_service

    class FakeS3:
        def __init__(self):
            self.objects = {}

        def put_object(self, Bucket, Key, Body, ContentType):
            self.objects[(Bucket, Key)] = {"body": Body, "content_type": ContentType}

    fake_s3 = FakeS3()
    monkeypatch.setattr(pdp_service, "_pdp_gallery_bucket", lambda: "gallery-bucket")
    monkeypatch.setattr(pdp_service, "_pdp_gallery_public_base_url", lambda: "https://cdn.example.com")
    monkeypatch.setattr(pdp_service, "_pdp_gallery_s3_client", lambda: fake_s3)

    with _client() as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-gallery-upload", "market": "US"},
        )
        assert resolved.status_code == 200
        pdp_id = resolved.json()["pdp"]["pdp_id"]

        uploaded = client.post(
            f"/employee/pdps/{pdp_id}/modules/gallery/images",
            data={
                "alt_text": "Front product image",
                "role": "primary",
                "rights_status": "owned_or_licensed",
                "source_note": "Employee uploaded licensed image",
            },
            files={"file": ("front.png", b"fake-image-bytes", "image/png")},
        )
        assert uploaded.status_code == 200
        body = uploaded.json()
        assert body["image"]["url"].startswith("https://cdn.example.com/pdp-gallery/")
        assert body["image"]["is_primary"] is True
        assert body["module"]["module_key"] == "gallery"
        assert body["module"]["status"] == "needs_human_review"
        assert body["module"]["requires_human"] is True
        assert fake_s3.objects

        detail = client.get(f"/employee/pdps/{pdp_id}")
        gallery = next(module for module in detail.json()["modules"] if module["module_key"] == "gallery")
        assert gallery["staged"]["payload"]["images"][0]["alt"] == "Front product image"
        assert gallery["staged"]["payload"]["primary_image_url"] == body["image"]["url"]


def test_review_queue_exposes_module_tasks_and_permissions():
    with _client(role="outsourced") as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-review-queue", "market": "US"},
        )
        assert resolved.status_code == 200
        pdp_id = resolved.json()["pdp"]["pdp_id"]
        draft = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/draft",
            json={
                "payload": {"title": "Queue ready title", "description": "Source-grounded draft."},
                "source_refs": [{"type": "external_seed", "id": "external-route-review-queue"}],
                "generated_by": "employee_edit",
            },
        )
        assert draft.status_code == 200

        queue = client.get("/employee/pdps/review-queue", params={"tab": "publish_ready", "module_key": "copy"})
        assert queue.status_code == 200
        items = queue.json()["items"]
        item = next(row for row in items if row["pdp_id"] == pdp_id and row["module_key"] == "copy")
        assert item["version_id"] == draft.json()["module"]["id"]
        assert "publish" in item["allowed_actions"]
        assert item["source_summary"]["count"] == 1
        assert "changed_paths" in item["diff_summary"]

        filtered = client.get(
            "/employee/pdps/review-queue",
            params={
                "tab": "publish_ready",
                "module_key": "copy",
                "seller_count": "external_only",
                "source_type": "external_seed",
                "staleness": "fresh",
                "priority": "normal",
            },
        )
        assert filtered.status_code == 200
        assert any(row["pdp_id"] == pdp_id and row["module_key"] == "copy" for row in filtered.json()["items"])


def test_published_monitor_uses_read_only_synthetic_rows():
    with _client(role="admin") as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-published-monitor", "market": "US"},
        )
        assert resolved.status_code == 200
        pdp_id = resolved.json()["pdp"]["pdp_id"]
        detail = client.get(f"/employee/pdps/{pdp_id}")
        assert detail.status_code == 200

        queue = client.get(
            "/employee/pdps/review-queue",
            params={"tab": "published_monitor", "module_key": "copy", "market": "US"},
        )
        assert queue.status_code == 200
        item = next(row for row in queue.json()["items"] if row["pdp_id"] == pdp_id and row["module_key"] == "copy")
        assert item["task_id"].startswith("published:")
        assert item["status"] == "published_monitor"
        assert item["module_status"] == "published"
        assert "view" in item["allowed_actions"]
        assert "rollback" in item["allowed_actions"]
        assert "publish" not in item["allowed_actions"]


def test_outsourced_publish_requires_checklist_and_blocks_high_risk():
    with _client(role="outsourced") as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-outsourced-permission", "market": "US"},
        )
        pdp_id = resolved.json()["pdp"]["pdp_id"]
        draft = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/draft",
            json={
                "payload": {"title": "Reviewed low risk", "description": "Plain sourced description."},
                "source_refs": [{"type": "external_seed", "id": "external-route-outsourced-permission"}],
            },
        )
        assert draft.status_code == 200
        blocked = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/review",
            json={"version_id": draft.json()["module"]["id"], "decision": "pass"},
        )
        assert blocked.status_code == 400
        assert blocked.json()["detail"] == "PDP_REVIEW_CHECKLIST_REQUIRED"

        reviewed = client.post(
            f"/employee/pdps/{pdp_id}/modules/copy/review",
            json={
                "version_id": draft.json()["module"]["id"],
                "decision": "pass",
                "checklist": {"source_grounded": True, "no_forbidden_claim": True},
                "policy_labels": ["low_risk_copy"],
                "decision_tree_path": ["copy", "low_risk", "publish"],
            },
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["published"] is True

        gallery = client.post(
            f"/employee/pdps/{pdp_id}/modules/gallery/draft",
            json={"payload": {"images": [{"url": "https://example.com/a.png", "rights_status": "unknown"}]}},
        )
        assert gallery.status_code == 403
        assert gallery.json()["detail"] == "PDP_REVIEW_ACTION_FORBIDDEN"


def test_employee_high_risk_publish_blocked_but_senior_can_publish():
    with _client(role="admin") as admin_client:
        resolved = admin_client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-senior-permission", "market": "US"},
        )
        pdp_id = resolved.json()["pdp"]["pdp_id"]
        gallery = admin_client.post(
            f"/employee/pdps/{pdp_id}/modules/gallery/draft",
            json={
                "payload": {
                    "images": [
                        {
                            "url": "https://example.com/gallery.png",
                            "rights_status": "owned_or_licensed",
                        }
                    ]
                },
                "source_refs": [{"type": "employee_gallery_url", "url": "https://example.com/gallery.png"}],
            },
        )
        assert gallery.status_code == 200
        version_id = gallery.json()["module"]["id"]

    with _client(role="employee") as employee_client:
        blocked = employee_client.post(
            f"/employee/pdps/{pdp_id}/modules/gallery/review",
            json={
                "version_id": version_id,
                "decision": "pass",
                "checklist": {"rights_verified": True},
                "policy_labels": ["gallery_rights_verified"],
            },
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "PDP_REVIEW_ACTION_FORBIDDEN"

    with _client(role="senior_employee") as senior_client:
        reviewed = senior_client.post(
            f"/employee/pdps/{pdp_id}/modules/gallery/review",
            json={
                "version_id": version_id,
                "decision": "pass",
                "checklist": {"rights_verified": True},
                "policy_labels": ["gallery_rights_verified"],
                "override_reason": "Senior verified image rights for high-risk gallery publish.",
            },
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["published"] is True
        detail = senior_client.get(f"/employee/pdps/{pdp_id}").json()
        gallery_module = next(module for module in detail["modules"] if module["module_key"] == "gallery")
        assert "override" in gallery_module["allowed_actions"]


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
