import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from db.database import database
from routes.employee_pdp_governance import router as employee_pdp_router
from routes.merchant_pdp import router as merchant_pdp_router
from services.pdp_governance_service import (
    apply_pdp_identity_review_action,
    audit_pdp_identity_groups,
    correct_pdp_product_group_membership,
    get_pdp_offer_reconciliation,
    get_pdp_projection,
    hydrate_pdp_subject_index,
    list_pdp_review_queue,
    list_pdp_subjects,
)
from utils.auth import get_current_employee, get_current_user


async def _ensure_product_group_members_schema():
    """Create product_group_members, then backstop columns other test modules
    omit — with a shared test DB their CREATE TABLE IF NOT EXISTS may win with
    a narrower shape (e.g. test_index_pipeline_state_service has no
    is_primary/updated_at)."""
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS product_group_members (
          product_group_id TEXT,
          merchant_id TEXT,
          platform TEXT,
          platform_product_id TEXT,
          is_primary BOOLEAN DEFAULT FALSE,
          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for ddl in [
        "ALTER TABLE product_group_members ADD COLUMN is_primary BOOLEAN DEFAULT FALSE",
        "ALTER TABLE product_group_members ADD COLUMN updated_at DATETIME",
    ]:
        try:
            await database.execute(ddl)
        except Exception:
            pass


async def _ensure_external_product_seeds_schema():
    """Same backstop pattern for external_product_seeds: another module's
    narrower CREATE TABLE IF NOT EXISTS may win in the shared test DB, and the
    hydration query orders by created_at."""
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS external_product_seeds (
          id TEXT PRIMARY KEY,
          external_product_id TEXT NULL,
          market TEXT NOT NULL,
          tool TEXT DEFAULT '*',
          destination_url TEXT,
          canonical_url TEXT NULL,
          domain TEXT NULL,
          title TEXT NULL,
          image_url TEXT NULL,
          status TEXT DEFAULT 'active',
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for ddl in [
        "ALTER TABLE external_product_seeds ADD COLUMN tool TEXT DEFAULT '*'",
        "ALTER TABLE external_product_seeds ADD COLUMN image_url TEXT",
        "ALTER TABLE external_product_seeds ADD COLUMN created_at DATETIME",
        "ALTER TABLE external_product_seeds ADD COLUMN updated_at DATETIME",
    ]:
        try:
            await database.execute(ddl)
        except Exception:
            pass


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


def test_employee_offer_reconciliation_exposes_confirmed_and_candidate_sections():
    with _client() as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-offer-reconciliation", "market": "US"},
        )
        assert resolved.status_code == 200
        pdp_id = resolved.json()["pdp"]["pdp_id"]

        reconciliation = client.get(f"/employee/pdps/{pdp_id}/offers/reconciliation")
        assert reconciliation.status_code == 200
        body = reconciliation.json()
        assert body["status"] == "success"
        assert body["pdp"]["pdp_id"] == pdp_id
        assert "confirmed_internal_seller_count" in body["summary"]
        assert "confirmed_external_offer_count" in body["summary"]
        assert "near_match_candidate_count" in body["summary"]
        assert isinstance(body["confirmed"]["internal_sellers"], list)
        assert isinstance(body["confirmed"]["external_offers"], list)
        assert isinstance(body["candidates"]["merchant_products"], list)
        assert isinstance(body["candidates"]["external_seeds"], list)
        assert "view" in body["allowed_actions"]


def test_identity_candidate_task_and_action_routes_delegate_with_employee_actor(monkeypatch):
    import routes.employee_pdp_governance as pdp_routes

    captured = {}

    async def fake_create_identity_task(**kwargs):
        captured["create"] = kwargs
        return {
            "status": "success",
            "created": True,
            "task": {"task_id": "pdptask_identity_1", "module_key": "identity"},
        }

    async def fake_apply_identity_action(**kwargs):
        captured["action"] = kwargs
        return {
            "status": "success",
            "decision": "pass",
            "identity_action": kwargs["action"],
        }

    monkeypatch.setattr(pdp_routes, "create_pdp_identity_review_task", fake_create_identity_task)
    monkeypatch.setattr(pdp_routes, "apply_pdp_identity_review_action", fake_apply_identity_action)

    with _client(role="employee") as client:
        created = client.post(
            "/employee/pdps/pdp_identity_route/identity-review-tasks",
            json={
                "candidate_type": "external_seed_near_match",
                "candidate_ref": "eps_candidate_1",
                "notes": "Candidate looks like the same PDP offer.",
            },
        )
        assert created.status_code == 200
        assert created.json()["task"]["task_id"] == "pdptask_identity_1"
        assert captured["create"]["pdp_id"] == "pdp_identity_route"
        assert captured["create"]["actor_role"] == "employee"
        assert captured["create"]["actor_id"] == "employee-test"

        action = client.post(
            "/employee/pdps/review-queue/pdptask_identity_1/identity-action",
            json={
                "action": "attach_external_offer",
                "notes": "Evidence and source URL match.",
                "checklist": {"source_grounded": True, "product_identity_match": True},
                "policy_labels": ["identity_match", "external_offer_attach"],
                "decision_tree_path": ["identity", "external_offer", "attach"],
            },
        )
        assert action.status_code == 200
        assert action.json()["identity_action"] == "attach_external_offer"
        assert captured["action"]["task_id"] == "pdptask_identity_1"
        assert captured["action"]["actor_role"] == "employee"
        assert captured["action"]["actor_id"] == "employee-test"


def test_identity_candidate_task_creation_returns_real_task_id(monkeypatch):
    import services.pdp_governance_service as pdp_service

    async def fake_find_candidate(**_kwargs):
        return {
            "id": "eps_identity_task_real",
            "source": "external_seed",
            "candidate_type": "external_seed_near_match",
            "title": "Candidate shade",
            "confidence": 0.91,
            "match_reasons": ["title_similarity:0.91"],
            "canonical_url": "https://example.com/candidate",
            "requires_human": True,
        }

    monkeypatch.setattr(pdp_service, "_find_offer_reconciliation_candidate", fake_find_candidate)

    with _client(role="employee") as client:
        resolved = client.get(
            "/employee/pdps/resolve",
            params={"product_key": "external_seed|external|external-route-identity-task-real", "market": "US"},
        )
        assert resolved.status_code == 200
        pdp_id = resolved.json()["pdp"]["pdp_id"]

        created = client.post(
            f"/employee/pdps/{pdp_id}/identity-review-tasks",
            json={
                "candidate_type": "external_seed_near_match",
                "candidate_ref": "eps_identity_task_real",
                "notes": "Create formal identity task from candidate evidence.",
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["task"]["task_id"].startswith("pdptask_")
        assert body["task"]["version_id"] == body["module"]["id"]

        detail = client.get(f"/employee/pdps/review-queue/{body['task']['task_id']}")
        assert detail.status_code == 200
        assert detail.json()["module"]["module_key"] == "identity"
        assert "attach_external_offer" in detail.json()["task"]["allowed_actions"]


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
        assert "assign" not in item["allowed_actions"]
        assert "escalate" not in item["allowed_actions"]

        task_detail = client.get(f"/employee/pdps/review-queue/{item['task_id']}")
        assert task_detail.status_code == 200
        assert task_detail.json()["task"]["task_id"] == item["task_id"]
        assert task_detail.json()["module"]["module_key"] == "copy"


@pytest.mark.asyncio
async def test_hydration_indexes_grouped_ungrouped_and_external_subjects():
    if not database.is_connected:
        await database.connect()
    try:
        await _ensure_product_group_members_schema()
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS products_cache (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              merchant_id TEXT NOT NULL,
              platform TEXT NOT NULL,
              platform_product_id TEXT NOT NULL,
              product_data JSON NOT NULL,
              cache_status TEXT DEFAULT 'fresh',
              cached_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              expires_at DATETIME NULL,
              ttl_seconds INTEGER DEFAULT 3600,
              access_count INTEGER DEFAULT 0,
              last_accessed_at DATETIME NULL
            )
            """
        )
        for ddl in [
            "ALTER TABLE products_cache ADD COLUMN cache_status TEXT DEFAULT 'fresh'",
            "ALTER TABLE products_cache ADD COLUMN ttl_seconds INTEGER DEFAULT 3600",
            "ALTER TABLE products_cache ADD COLUMN access_count INTEGER DEFAULT 0",
            "ALTER TABLE products_cache ADD COLUMN last_accessed_at DATETIME NULL",
        ]:
            try:
                await database.execute(ddl)
            except Exception:
                pass
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_products (
              canonical_product_id TEXT PRIMARY KEY,
              merchant_id TEXT NOT NULL,
              platform TEXT NOT NULL,
              platform_product_id TEXT NOT NULL,
              title TEXT NOT NULL,
              default_image_url TEXT NULL,
              standard_product_data JSON NOT NULL,
              expires_at DATETIME NULL,
              source_recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await _ensure_external_product_seeds_schema()
        await database.execute("DELETE FROM product_group_members WHERE merchant_id = 'hydrate-merchant'")
        await database.execute("DELETE FROM products_cache WHERE merchant_id = 'hydrate-merchant'")
        await database.execute("DELETE FROM canonical_products WHERE merchant_id = 'hydrate-merchant'")
        await database.execute("DELETE FROM external_product_seeds WHERE id LIKE :seed_prefix", {"seed_prefix": "hydrate-seed-%"})

        await database.execute(
            """
            INSERT INTO product_group_members (product_group_id, merchant_id, platform, platform_product_id, is_primary, updated_at)
            VALUES ('pg_hydrate_1', 'hydrate-merchant', 'shopify', 'grouped-product', TRUE, CURRENT_TIMESTAMP)
            """
        )
        await database.execute(
            """
            INSERT INTO products_cache (merchant_id, platform, platform_product_id, product_data, expires_at)
            VALUES ('hydrate-merchant', 'shopify', 'grouped-product', :grouped_payload, '2999-01-01 00:00:00')
            """,
            {"grouped_payload": json.dumps({"title": "Grouped Hydration Product"})},
        )
        await database.execute(
            """
            INSERT INTO products_cache (merchant_id, platform, platform_product_id, product_data, expires_at)
            VALUES ('hydrate-merchant', 'shopify', 'ungrouped-product', :ungrouped_payload, '2999-01-01 00:00:00')
            """,
            {"ungrouped_payload": json.dumps({"title": "Ungrouped Hydration Product"})},
        )
        # The app-boot metadata shape of canonical_products (db/canonical_commerce.py)
        # has NOT NULL source_payload_hash; the local CREATE above does not. Insert
        # column-adaptively so the test works against whichever shape won the
        # shared test DB.
        canonical_cols = {
            str(dict(row).get("name"))
            for row in await database.fetch_all("PRAGMA table_info(canonical_products)")
        }
        extra_col = ", source_payload_hash" if "source_payload_hash" in canonical_cols else ""
        extra_val = ", 'test-hash'" if extra_col else ""
        await database.execute(
            f"""
            INSERT INTO canonical_products (
              canonical_product_id, merchant_id, platform, platform_product_id, title,
              standard_product_data, expires_at{extra_col}
            )
            VALUES (
              'canonical-hydrate-1', 'hydrate-merchant', 'shopify', 'canonical-product',
              'Canonical Hydration Product', :canonical_payload, '2999-01-01 00:00:00'{extra_val}
            )
            """,
            {"canonical_payload": json.dumps({"title": "Canonical Hydration Product"})},
        )
        for idx in range(1, 56):
            await database.execute(
                """
                INSERT INTO external_product_seeds (id, external_product_id, market, destination_url, title, status)
                VALUES (:id, NULL, 'US', :url, :title, 'active')
                """,
                {
                    "id": f"hydrate-seed-{idx}",
                    "url": f"https://example.com/hydrate-{idx}",
                    "title": f"External Hydration Product {idx}",
                },
            )

        result = await hydrate_pdp_subject_index(limit=0, actor_id="test")
        assert result["limit_mode"] == "all"

        listing = await list_pdp_subjects(limit=200, market="US")
        subjects = {(item["subject_type"], item["subject_ref"]) for item in listing["items"]}

        assert ("product_group", "pg_hydrate_1") in subjects
        assert ("merchant_product", "hydrate-merchant|shopify|ungrouped-product") in subjects
        assert ("merchant_product", "hydrate-merchant|shopify|canonical-product") in subjects
        assert ("external_product", "hydrate-seed-1") in subjects
        assert ("external_product", "hydrate-seed-55") in subjects
    finally:
        if database.is_connected:
            await database.disconnect()


@pytest.mark.asyncio
async def test_offer_reconciliation_counts_only_live_internal_seller_offers():
    if not database.is_connected:
        await database.connect()
    try:
        await _ensure_product_group_members_schema()
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS products_cache (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              merchant_id TEXT NOT NULL,
              platform TEXT NOT NULL,
              platform_product_id TEXT NOT NULL,
              product_data JSON NOT NULL,
              cache_status TEXT DEFAULT 'fresh',
              cached_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              expires_at DATETIME NULL,
              ttl_seconds INTEGER DEFAULT 3600,
              access_count INTEGER DEFAULT 0,
              last_accessed_at DATETIME NULL
            )
            """
        )
        for ddl in [
            "ALTER TABLE products_cache ADD COLUMN cache_status TEXT DEFAULT 'fresh'",
            "ALTER TABLE products_cache ADD COLUMN ttl_seconds INTEGER DEFAULT 3600",
            "ALTER TABLE products_cache ADD COLUMN access_count INTEGER DEFAULT 0",
            "ALTER TABLE products_cache ADD COLUMN last_accessed_at DATETIME NULL",
        ]:
            try:
                await database.execute(ddl)
            except Exception:
                pass
        await _ensure_external_product_seeds_schema()
        await database.execute("DELETE FROM product_group_members WHERE product_group_id = 'pg_offer_live_only'")
        await database.execute("DELETE FROM products_cache WHERE merchant_id IN ('stale-offer-merchant', 'live-offer-merchant')")

        await database.execute(
            """
            INSERT INTO product_group_members (product_group_id, merchant_id, platform, platform_product_id, is_primary, updated_at)
            VALUES
              ('pg_offer_live_only', 'stale-offer-merchant', 'shopify', 'missing-cache-product', TRUE, CURRENT_TIMESTAMP),
              ('pg_offer_live_only', 'live-offer-merchant', 'wix', 'checkout-product', FALSE, CURRENT_TIMESTAMP)
            """
        )
        await database.execute(
            """
            INSERT INTO products_cache (merchant_id, platform, platform_product_id, product_data, expires_at)
            VALUES ('live-offer-merchant', 'wix', 'checkout-product', :payload, '2999-01-01 00:00:00')
            """,
            {
                "payload": json.dumps(
                    {
                        "title": "Checkout Ready Harness",
                        "image_url": "https://example.com/harness.jpg",
                        "price": 36,
                        "currency": "EUR",
                        "variants": [{"variant_id": "v1"}],
                        "orderable": True,
                    }
                )
            },
        )

        detail = await get_pdp_projection(product_key="live-offer-merchant|wix|checkout-product", actor_role="admin")
        pdp_id = detail["pdp"]["pdp_id"]
        assert detail["pdp"]["seller_count"] == 1
        assert detail["pdp"]["representative_product_key"] == "live-offer-merchant|wix|checkout-product"
        assert detail["pdp"]["title"] == "Checkout Ready Harness"

        reconciliation = await get_pdp_offer_reconciliation(pdp_id=pdp_id, actor_role="admin")
        assert reconciliation["summary"]["confirmed_internal_seller_count"] == 1
        assert reconciliation["summary"]["seller_count"] == 1
        assert reconciliation["summary"]["pdp_index_seller_count"] == 2
        assert [row["product_key"] for row in reconciliation["confirmed"]["internal_sellers"]] == [
            "live-offer-merchant|wix|checkout-product"
        ]
    finally:
        if database.is_connected:
            await database.disconnect()


@pytest.mark.asyncio
async def test_offer_reconciliation_exposes_identity_signals_without_blocking_candidates():
    if not database.is_connected:
        await database.connect()
    try:
        await _ensure_product_group_members_schema()
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS products_cache (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              merchant_id TEXT NOT NULL,
              platform TEXT NOT NULL,
              platform_product_id TEXT NOT NULL,
              product_data JSON NOT NULL,
              cache_status TEXT DEFAULT 'fresh',
              cached_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              expires_at DATETIME NULL,
              ttl_seconds INTEGER DEFAULT 3600,
              access_count INTEGER DEFAULT 0,
              last_accessed_at DATETIME NULL
            )
            """
        )
        await database.execute("DELETE FROM product_group_members WHERE product_group_id = 'pg_identity_signals'")
        await database.execute("DELETE FROM products_cache WHERE merchant_id = 'identity-signal-merchant'")
        await database.execute(
            """
            INSERT INTO product_group_members (product_group_id, merchant_id, platform, platform_product_id, is_primary, updated_at)
            VALUES ('pg_identity_signals', 'identity-signal-merchant', 'shopify', 'confirmed-harness', TRUE, CURRENT_TIMESTAMP)
            """
        )
        await database.execute(
            """
            INSERT INTO products_cache (merchant_id, platform, platform_product_id, product_data, expires_at)
            VALUES
              ('identity-signal-merchant', 'shopify', 'confirmed-harness', :confirmed_payload, '2999-01-01 00:00:00'),
              ('identity-signal-merchant', 'shopify', 'nearby-harness', :candidate_payload, '2999-01-01 00:00:00')
            """,
            {
                "confirmed_payload": json.dumps(
                    {
                        "title": "Comfy Tactical Dog Harness for Small to Medium Dogs",
                        "image_url": "https://example.com/confirmed.jpg",
                        "price": 36,
                        "currency": "USD",
                        "variants": [{"id": "v1"}],
                    }
                ),
                "candidate_payload": json.dumps(
                    {
                        "title": "Reflective Tactical Dog Harness for Small to Medium Dogs",
                        "image_url": "https://example.com/candidate.jpg",
                        "price": 32,
                        "currency": "USD",
                        "variants": [{"id": "v2"}],
                    }
                ),
            },
        )

        resolved = await get_pdp_projection(
            product_key="identity-signal-merchant|shopify|confirmed-harness",
            actor_role="admin",
        )
        reconciliation = await get_pdp_offer_reconciliation(pdp_id=resolved["pdp"]["pdp_id"], actor_role="admin")

        confirmed = reconciliation["confirmed"]["internal_sellers"][0]
        assert confirmed["verification_status"] == "confirmed"
        assert confirmed["identity_confidence"] >= 0.8
        assert any(item["type"] == "product_group_member" for item in confirmed["identity_evidence"])

        candidate = next(
            row
            for row in reconciliation["candidates"]["merchant_products"]
            if row["product_key"] == "identity-signal-merchant|shopify|nearby-harness"
        )
        assert candidate["match_status"] == "candidate"
        assert candidate["verification_status"] in {"suggested_match", "possible_match", "evidence_only"}
        assert "title_based_candidate_only" in candidate["risk_flags"]
        assert "same_merchant_distinct_product_candidate" in candidate["risk_flags"]
        assert candidate["identity_evidence"]
    finally:
        if database.is_connected:
            await database.disconnect()


@pytest.mark.asyncio
async def test_identity_audit_job_creates_non_blocking_identity_review_task():
    if not database.is_connected:
        await database.connect()
    try:
        await _ensure_product_group_members_schema()
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS products_cache (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              merchant_id TEXT NOT NULL,
              platform TEXT NOT NULL,
              platform_product_id TEXT NOT NULL,
              product_data JSON NOT NULL,
              cache_status TEXT DEFAULT 'fresh',
              cached_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              expires_at DATETIME NULL,
              ttl_seconds INTEGER DEFAULT 3600,
              access_count INTEGER DEFAULT 0,
              last_accessed_at DATETIME NULL
            )
            """
        )
        await database.execute("DELETE FROM product_group_members WHERE product_group_id = 'pg_identity_audit_job'")
        await database.execute("DELETE FROM products_cache WHERE merchant_id = 'identity-audit-merchant'")
        await database.execute(
            """
            INSERT INTO product_group_members (product_group_id, merchant_id, platform, platform_product_id, is_primary, updated_at)
            VALUES ('pg_identity_audit_job', 'identity-audit-merchant', 'shopify', 'primary-harness', TRUE, CURRENT_TIMESTAMP)
            """
        )
        await database.execute(
            """
            INSERT INTO products_cache (merchant_id, platform, platform_product_id, product_data, expires_at)
            VALUES
              ('identity-audit-merchant', 'shopify', 'primary-harness', :primary_payload, '2999-01-01 00:00:00'),
              ('identity-audit-merchant', 'shopify', 'similar-harness', :candidate_payload, '2999-01-01 00:00:00')
            """,
            {
                "primary_payload": json.dumps(
                    {
                        "title": "Comfy Tactical Dog Harness for Small to Medium Dogs",
                        "image_url": "https://example.com/primary.jpg",
                        "price": 36,
                        "currency": "USD",
                        "variants": [{"id": "v1"}],
                    }
                ),
                "candidate_payload": json.dumps(
                    {
                        "title": "Reflective Tactical Dog Harness for Small to Medium Dogs",
                        "image_url": "https://example.com/similar.jpg",
                        "price": 30,
                        "currency": "USD",
                        "variants": [{"id": "v2"}],
                    }
                ),
            },
        )
        detail = await get_pdp_projection(
            product_key="identity-audit-merchant|shopify|primary-harness",
            actor_role="admin",
        )
        pdp_id = detail["pdp"]["pdp_id"]
        await database.execute("DELETE FROM pdp_review_tasks WHERE pdp_id = :pdp_id", {"pdp_id": pdp_id})
        await database.execute("DELETE FROM pdp_module_versions WHERE pdp_id = :pdp_id", {"pdp_id": pdp_id})
        await database.execute("DELETE FROM pdp_audit_log WHERE pdp_id = :pdp_id", {"pdp_id": pdp_id})

        result = await audit_pdp_identity_groups(limit=20, actor_type="system_policy", actor_id="test_identity_audit")
        created = [item for item in result["created"] if item["pdp_id"] == pdp_id]
        assert created
        assert "near_match_candidate_present" in created[0]["risk_flags"]
        assert "candidate:same_merchant_distinct_product_candidate" in created[0]["risk_flags"]

        refreshed = await get_pdp_projection(pdp_id=pdp_id, actor_role="admin")
        identity = next(module for module in refreshed["modules"] if module["module_key"] == "identity")
        review = identity["staged"]["payload"]["identity_review"]
        assert review["candidate_type"] == "product_group_identity_audit"
        assert review["status"] == "pending"
        assert review["merchant_candidates"]

        audit_task_id = created[0]["task_id"]
        candidate = review["merchant_candidates"][0]
        candidate_ref = candidate["product_key"]
        converted = await apply_pdp_identity_review_action(
            task_id=audit_task_id,
            action="create_identity_candidate_task",
            candidate_type="merchant_product_near_match",
            candidate_ref=candidate_ref,
            notes="Convert audit evidence into a formal identity review task.",
            policy_labels=["identity:audit_to_review_task"],
            decision_tree_path=["identity_audit", "create_identity_candidate_task"],
            actor_role="admin",
            actor_id="employee@example.com",
        )
        assert converted["identity_action"] == "create_identity_candidate_task"
        assert converted["task"]["status"] == "resolved"
        converted_review = converted["module"]["payload"]["identity_review"]
        assert converted_review["status"] == "converted"
        candidate_task = converted["action_result"]["task"]
        assert candidate_task["module_key"] == "identity"

        candidate_detail = await get_pdp_projection(pdp_id=pdp_id, actor_role="admin")
        candidate_identity = next(module for module in candidate_detail["modules"] if module["module_key"] == "identity")
        formal_review = candidate_identity["staged"]["payload"]["identity_review"]
        assert formal_review["candidate_type"] == "merchant_product_near_match"
        assert formal_review["candidate_ref"] == candidate_ref
        assert formal_review["created_from"] == "product_group_identity_audit"

        queue = await list_pdp_review_queue(
            actor_role="admin",
            actor_id="employee@example.com",
            tab="identity_audit",
            limit=50,
        )
        audit_items = [item for item in queue["items"] if item["pdp_id"] == pdp_id]
        assert not audit_items

        rerun = await audit_pdp_identity_groups(limit=20, actor_type="system_policy", actor_id="test_identity_audit")
        assert any(item["pdp_id"] == pdp_id for item in rerun["existing"])

        with pytest.raises(PermissionError, match="PDP_REVIEW_ACTION_FORBIDDEN"):
            await audit_pdp_identity_groups(
                limit=1,
                actor_type="human_employee",
                actor_id="outsourced@example.com",
                actor_role="outsourced",
            )
    finally:
        if database.is_connected:
            await database.disconnect()


@pytest.mark.asyncio
async def test_senior_product_group_correction_replaces_wrong_confirmed_seller():
    if not database.is_connected:
        await database.connect()
    try:
        await _ensure_product_group_members_schema()
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS products_cache (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              merchant_id TEXT NOT NULL,
              platform TEXT NOT NULL,
              platform_product_id TEXT NOT NULL,
              product_data JSON NOT NULL,
              cache_status TEXT DEFAULT 'fresh',
              cached_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              expires_at DATETIME NULL,
              ttl_seconds INTEGER DEFAULT 3600,
              access_count INTEGER DEFAULT 0,
              last_accessed_at DATETIME NULL
            )
            """
        )
        await database.execute("DELETE FROM product_group_members WHERE product_group_id = 'pg_identity_correction'")
        await database.execute("DELETE FROM products_cache WHERE merchant_id IN ('wrong-wix-merchant', 'right-shopify-merchant')")
        await database.execute(
            """
            INSERT INTO product_group_members (product_group_id, merchant_id, platform, platform_product_id, is_primary, updated_at)
            VALUES ('pg_identity_correction', 'wrong-wix-merchant', 'wix', 'wrong-product', TRUE, CURRENT_TIMESTAMP)
            """
        )
        await database.execute(
            """
            INSERT INTO products_cache (merchant_id, platform, platform_product_id, product_data, expires_at)
            VALUES
              ('wrong-wix-merchant', 'wix', 'wrong-product', :wrong_payload, '2999-01-01 00:00:00'),
              ('right-shopify-merchant', 'shopify', 'right-product', :right_payload, '2999-01-01 00:00:00')
            """,
            {
                "wrong_payload": json.dumps(
                    {
                        "title": "Wrong Wix Harness",
                        "image_url": "https://example.com/wrong.jpg",
                        "price": 12,
                        "currency": "USD",
                    }
                ),
                "right_payload": json.dumps(
                    {
                        "title": "Comfy Tactical Dog Harness for Small to Medium Dogs",
                        "image_url": "https://example.com/shopify-harness.jpg",
                        "price": 36,
                        "currency": "USD",
                        "variants": [{"id": "v1"}],
                    }
                ),
            },
        )

        resolved = await get_pdp_projection(
            product_key="wrong-wix-merchant|wix|wrong-product",
            actor_role="admin",
        )
        pdp_id = resolved["pdp"]["pdp_id"]

        body = await correct_pdp_product_group_membership(
            pdp_id=pdp_id,
            add_product_key="right-shopify-merchant|shopify|right-product",
            set_primary_product_key="right-shopify-merchant|shopify|right-product",
            remove_product_keys=["wrong-wix-merchant|wix|wrong-product"],
            reason="Correct confirmed seller after merchant owner feedback.",
            policy_labels=["identity:correct_confirmed_seller", "source:merchant_owner_feedback"],
            checklist={"source_grounded": True, "product_identity_match": True},
            decision_tree_path=["identity", "product_group_correction", "publish"],
            override_reason="Senior correction replaces an incorrectly grouped seller.",
            actor_role="admin",
            actor_id="employee-test",
        )
        assert body["pdp"]["representative_product_key"] == "right-shopify-merchant|shopify|right-product"
        assert body["pdp"]["seller_count"] == 1
        assert body["published_modules"][0]["module_key"] == "identity"
        assert body["reconciliation"]["confirmed"]["internal_sellers"][0]["product_key"] == "right-shopify-merchant|shopify|right-product"

        detail = await get_pdp_projection(pdp_id=pdp_id, actor_role="admin")
        modules_by_key = {module["module_key"]: module for module in detail["modules"]}
        assert detail["published_payload"]["identity"]["product_group_id"] == "pg_identity_correction"
        assert detail["published_payload"]["identity"]["seller_count"] == 1
        assert detail["published_payload"]["identity"]["last_identity_correction"]["primary_product_key"] == "right-shopify-merchant|shopify|right-product"
        assert any(ref.get("id") == "right-shopify-merchant|shopify|right-product" for ref in modules_by_key["copy"]["source_refs"])
        assert any(ref.get("id") == "right-shopify-merchant|shopify|right-product" for ref in modules_by_key["quality"]["source_refs"])
        assert any(row["action"] == "identity_product_group_corrected" for row in detail["activity"])
    finally:
        if database.is_connected:
            await database.disconnect()


def test_product_group_correction_forbidden_for_non_senior():
    with _client(role="outsourced") as client:
        response = client.post(
            "/employee/pdps/pdp_any/identity/product-group-correction",
            json={
                "add_product_key": "merchant|shopify|product",
                "reason": "Attempted correction.",
                "policy_labels": ["identity:test"],
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "PDP_REVIEW_ACTION_FORBIDDEN"


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
    """gallery never published here; what changed is WHERE it is stopped.

    It used to reach review_module_version and come back 200 /
    needs_human_review. The employee gpt55-review route now fences itself to
    LLM_ONLY_PUBLISH_MODULES before the service call (an LLM-only pass has no
    role check behind it), so a human-co-review module is refused at the door.
    Same data outcome -- nothing published -- and an explicit answer instead of
    a silent one. See tests/test_employee_pdp_gpt55_module_allowlist.py.
    """
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
        assert reviewed.status_code == 400
        assert reviewed.json()["detail"] == "MODULE_NOT_LLM_REVIEWABLE"

        detail = client.get(f"/employee/pdps/{pdp_id}").json()
        gallery = next(m for m in detail["modules"] if m["module_key"] == "gallery")
        assert gallery["published_payload"] is None
        assert gallery["review_actor_type"] is None


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
