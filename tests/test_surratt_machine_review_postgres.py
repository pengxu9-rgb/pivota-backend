"""Exact-scope machine review against a disposable local PostgreSQL schema."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from services import external_seed_gemini_reviewer as reviewer
from services import external_seed_review_pilot as pilot


SURRATT = pilot.LISTINGS[pilot.SURRATT_SEED_ID]
PREFIX = "/employee/products/external-seeds"
MACHINE_EMAIL = "review@pilot.iam.gserviceaccount.com"


def draft_for(listing):
    from routes.employee_products import _stable_external_product_id

    return {
        "status": "draft", "market": "US", "tool": "*", "seller_ref": listing.seller_ref,
        "seed_kind": "cross", "domain": listing.seller_host, "destination_url": listing.url,
        "canonical_url": listing.url, "external_product_id": _stable_external_product_id(listing.url),
        "title": listing.title, "price_amount": listing.price_minor / 100,
        "price_currency": "USD", "availability": "in_stock",
    }, {
        "external_product_id": _stable_external_product_id(listing.url), "product_id": str(listing.native_product_id),
        "title": listing.title, "description": listing.description, "brand": listing.brand,
        "merchant_display_name": listing.brand,
        "variants": [
            {"variant_id": vid, "barcode": gtin, "sku": sku, "title": shade,
             "url": f"{listing.url}?variant={vid}", "price_currency": "USD",
             "price_amount": listing.price_minor / 100, "availability": "in_stock", "in_stock": True}
            for vid, gtin, shade, sku in listing.variants
        ],
    }


def test_surratt_draft_matches_only_the_pinned_seller_listing_and_variants():
    row, data = draft_for(SURRATT)
    assert pilot.draft_matches_listing(row, data, SURRATT)
    assert pilot.draft_matches_listing(row | {"domain": SURRATT.host}, data, SURRATT)
    assert not pilot.draft_matches_listing(row | {"seller_ref": "some-other-seller"}, data, SURRATT)
    assert not pilot.draft_matches_listing(row | {"domain": "shop.surrattbeauty.com"}, data, SURRATT)
    assert not pilot.draft_matches_listing(row | {"destination_url": SURRATT.listing_urls[1]}, data, SURRATT)
    assert not pilot.draft_matches_listing(row, data | {"variants": data["variants"][:1]}, SURRATT)
    assert not pilot.draft_matches_listing(row, data | {"variants": [
        data["variants"][0] | {"url": data["variants"][0]["url"].replace("www.", "")}, data["variants"][1],
    ]}, SURRATT)


@pytest.fixture
async def route_db(monkeypatch):
    url = os.getenv("PIVOTA_PG_TEST_URL") or os.getenv("DATABASE_URL", "")
    endpoint = urlsplit(url)
    if (endpoint.scheme not in ("postgres", "postgresql", "postgresql+asyncpg")
            or endpoint.hostname not in ("localhost", "127.0.0.1", "::1") or "test" not in endpoint.path):
        pytest.skip("requires a named local PostgreSQL test database")

    from databases import Database
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.reviews_center import employee_audit_logs
    from db.sql_migrations import split_statements
    from routes import employee_products as ep

    migrations = Path(__file__).resolve().parents[1] / "db" / "migrations"
    async with Database(url) as db:
        async with db.transaction(force_rollback=True):
            schema = "test_surratt_review_" + uuid4().hex
            await db.execute(f"CREATE SCHEMA {schema}")
            await db.execute(f"SET LOCAL search_path = {schema}")
            for name in ("044_external_product_seeds.sql", "058_catalog_core.sql",
                         "169_external_product_seeds_seller_ref.sql", "285_external_seed_machine_reviews.sql"):
                for statement in split_statements((migrations / name).read_text()):
                    if statement.strip().rstrip(";").upper() not in ("BEGIN", "COMMIT"):
                        await db.execute(statement)
            await db.execute(str(CreateTable(employee_audit_logs).compile(dialect=postgresql.dialect())))
            monkeypatch.setattr(ep, "database", db)
            monkeypatch.setattr(ep, "_seed_publish_locks_enabled", lambda: True)
            monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock())
            yield db


async def insert_draft(db, seed_id, listing, *, seller_source_ref):
    row, seed_data = draft_for(listing)
    fields = ", ".join(row)
    placeholders = ", ".join(f":{key}" for key in row)
    await db.execute(
        f"INSERT INTO external_product_seeds (id, seed_data, {fields}) VALUES (:id, :data, {placeholders})",
        {"id": seed_id, "data": json.dumps(seed_data), **row},
    )
    await db.execute(
        "INSERT INTO catalog_merchants (merchant_id, source_ref, status) VALUES (:id, :source_ref, 'observed')",
        {"id": listing.seller_ref, "source_ref": seller_source_ref},
    )
    return dict(await db.fetch_one("SELECT * FROM external_product_seeds WHERE id = :id", {"id": seed_id}))


@pytest.mark.parametrize(("case", "expected"), [
    ("default", "observe_only"), ("unpinned_version", "activation_version_not_pinned"),
    ("wrong_seller", "observed_seller_changed"), ("active_alias", "active_seed_conflict"),
    ("prior_rejection_without_note", "employee_rejection_recorded"),
    ("other_pilot", "observe_only"), ("activate", None),
])
async def test_real_machine_route_is_id_version_and_seller_scoped(route_db, monkeypatch, case, expected):
    from routes import employee_products as ep
    from services.external_seed_machine_review_ledger import load_machine_review

    seed_id = next(iter(pilot.LISTINGS)) if case == "other_pilot" else pilot.SURRATT_SEED_ID
    listing = pilot.LISTINGS[seed_id]
    seller_host = listing.host if case == "wrong_seller" else listing.seller_host
    row = await insert_draft(route_db, seed_id, listing, seller_source_ref=seller_host)
    version = ep._seed_review_version(row)
    if case == "prior_rejection_without_note":
        await ep.record_external_seed_audit_verdict(
            seed_id, ep.ExternalSeedAuditVerdictRequest(verdict="reject", reason="Test shade", review_version=version),
            current_user={"employee_id": "employee-fixture", "role": "employee"},
        )
        rejected = await route_db.fetch_one("SELECT notes FROM external_product_seeds WHERE id = :id", {"id": seed_id})
        assert "audit_reject:" in rejected["notes"]
        await route_db.execute("UPDATE external_product_seeds SET notes = NULL WHERE id = :id", {"id": seed_id})
        row = dict(await route_db.fetch_one("SELECT * FROM external_product_seeds WHERE id = :id", {"id": seed_id}))
        version = ep._seed_review_version(row)
    if case == "active_alias":
        await route_db.execute(
            "INSERT INTO external_product_seeds (id, market, destination_url, status) "
            "VALUES ('existing-active', 'US', :url, 'active')", {"url": listing.listing_urls[1]},
        )

    monkeypatch.setenv("EXTERNAL_SEED_MACHINE_REVIEW_ENABLED", "true")
    monkeypatch.setenv("EXTERNAL_SEED_MACHINE_REVIEW_AUDIENCE", "https://machine-review.test")
    monkeypatch.setenv("EXTERNAL_SEED_MACHINE_REVIEW_SERVICE_ACCOUNT", MACHINE_EMAIL)
    monkeypatch.setenv("EXTERNAL_SEED_MACHINE_REVIEW_SERVICE_SUBJECT", "12345")
    monkeypatch.delenv("EXTERNAL_SEED_MACHINE_REVIEW_ACTIVATION_ENABLED", raising=False)
    monkeypatch.delenv("EXTERNAL_SEED_MACHINE_REVIEW_ACTIVATION_VERSION", raising=False)
    if case != "default":
        monkeypatch.setenv("EXTERNAL_SEED_MACHINE_REVIEW_ACTIVATION_ENABLED", "true")
        if case != "unpinned_version":
            monkeypatch.setenv("EXTERNAL_SEED_MACHINE_REVIEW_ACTIVATION_VERSION", version)

    def google_identity(token, *, audience, allowed):
        assert (audience, allowed) == ("https://machine-review.test", {MACHINE_EMAIL})
        return {"sub": "12345" if token == "fixture-token" else "12346"}

    monkeypatch.setattr(ep.gateway_oidc_auth, "verify_google_service_identity", google_identity)
    monkeypatch.setattr(ep, "_is_domain_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(ep, "audit_external_seed_row", lambda *_args: {"findings": []})
    project = AsyncMock()
    monkeypatch.setattr(ep, "_project_published_seed", project)

    def storefront_session(_client, host, *_args):
        if host == listing.seller_host:
            meta = httpx.Response(200, json={"currency": "USD", "ships_to_countries": ["US"],
                                            "myshopify_domain": listing.native_shop_domain})
        else:
            assert host == listing.host
            meta = None
        detail = {"id": listing.native_product_id, "variants": [
            {"id": int(vid), "title": shade, "sku": sku, "barcode": gtin, "available": True,
             "price": listing.price_minor} for vid, gtin, shade, sku in listing.variants
        ]}
        return SimpleNamespace(request=AsyncMock(return_value=meta),
                               json_presented=AsyncMock(return_value=(200, detail, "USD", None)))

    async def vertex_post(model, request):
        payload = request["contents"][0]["parts"][0]["text"]
        assert seed_id not in payload and listing.seller_ref not in payload and version not in payload
        rows = [{"variant_id": vid, "scope": "brand_direct", "offer_id": None, "action": "employee_review",
                 "evidence_refs": [f"shopify_us:{vid}"]} for vid, _, _, _ in listing.variants]
        return {"modelVersion": model, "responseId": f"fixture-{model.replace('.', '_')}", "candidates": [
            {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"rows": rows})}]}},
        ]}

    monkeypatch.setattr(pilot, "_Session", storefront_session)
    monkeypatch.setattr(reviewer.vertex_gemini, "vertex_enabled", lambda: True)
    vertex = AsyncMock(side_effect=vertex_post)
    monkeypatch.setattr(reviewer, "_vertex_post", vertex)
    app = FastAPI()
    app.include_router(ep.router)
    req = {"request_id": str(uuid4()), "review_version": version}
    endpoint = f"{PREFIX}/{seed_id}/machine-review"
    headers = {"Authorization": "Bearer fixture-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(endpoint, json=req)).status_code == 401
        assert (await client.post(endpoint, json=req, headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.post(f"{PREFIX}/not-allowlisted/machine-review", json=req, headers=headers)).status_code == 404
        assert (await client.post(endpoint, json=req | {"review_version": "0" * 64}, headers=headers)).status_code == 409
        vertex.assert_not_awaited()
        response = await client.post(endpoint, json=req, headers=headers)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert await load_machine_review(route_db, req["request_id"]) == receipt
        assert receipt["policy_version"] == listing.policy_version
        assert receipt["decision"].get("hold_reason") == expected
        assert receipt["outcome"] == ("hold" if expected else "activated")
        assert bool(receipt["active_review_version"]) == (expected is None)
        assert {r["model_name"] for r in receipt["decision"]["receipts"].values()} == set(pilot.REVIEW_MODELS[:2])
        assert (await client.post(endpoint, json=req, headers=headers)).json() == receipt
        assert vertex.await_count == 2

    after = dict(await route_db.fetch_one("SELECT * FROM external_product_seeds WHERE id = :id", {"id": seed_id}))
    if expected:
        assert ep._seed_review_version(after) == version
        project.assert_not_awaited()
    else:
        assert after["status"] == "active"
        assert ep._seed_review_version(after) == receipt["active_review_version"]
        project.assert_awaited_once_with(seed_id, price_source=None)
