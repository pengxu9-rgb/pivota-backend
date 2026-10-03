"""Read-only selector preparation cases shared by real SQLite and PostgreSQL collectors.
Only auth dependencies are fixture-controlled. SQL catalog/proof/eligibility reads are real;
the route suites' network ban remains in force. Preparation touches no commerce state.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from uuid import uuid4
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from db.database import IS_POSTGRES, database
import routes.agent_commerce_reap as reap
from routes.agent_auth import get_agent_context
from routes.agent_user_auth import get_agent_user_context

DOMAIN = "judydoll.com"
EXT = "ext_0f95730ee5ba05a6b7957ada"
PRODUCT = "prod::external_seed::external_seed::" + EXT
SKU = PRODUCT + "::sku_58ae6f8de2c8797993f2"
VARIANT = "49819267301653"
SELLER = "merch_obs_a25cbba37ef98c52"
SEED = "reap_prepare_owned_seed"
BASE = "/agent/v2/commerce/reap/purchases/prepare"


def body(**changes):
    return {"merchant_domain": DOMAIN, "product_key": PRODUCT, "variant_id": VARIANT,
            "quantity": 1, "market_country": "US", "item_source": "cart_link", **changes}


def error(response):
    payload = response.json()
    if isinstance(payload.get("detail"), dict):
        return payload["detail"].get("error")
    return payload.get("error") if isinstance(payload.get("error"), str) else payload.get("error", {}).get("details", {}).get("error")


@asynccontextmanager
async def isolated_prepare_seed_table():
    """Own a complete fixture and restore any inherited table, schema and rows.

    The full sweep shares one database. Other seed suites may leave a narrower
    schema than migration 044; IF NOT EXISTS cannot establish this reader's schema.
    Renaming preserves inherited rows and dependent references without healing
    another suite's schema or weakening the production proof query.
    """
    found = await database.fetch_one(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='external_product_seeds'"
        if IS_POSTGRES else "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_product_seeds'"
    )
    inherited = "reap_prepare_inherited_" + uuid4().hex
    if found:
        await database.execute(f"ALTER TABLE external_product_seeds RENAME TO {inherited}")
    created = False
    try:
        await database.execute("CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, status TEXT, domain TEXT, market TEXT, destination_url TEXT, canonical_url TEXT, attached_product_key TEXT, attached_variant_id TEXT, seed_data TEXT)")
        created = True
        yield
    finally:
        if created:
            await database.execute("DROP TABLE external_product_seeds")
        if found:
            await database.execute(f"ALTER TABLE {inherited} RENAME TO external_product_seeds")


@pytest.fixture(autouse=True)
async def prepare_seed_cleanup(_db):
    async with isolated_prepare_seed_table():
        yield


@pytest.mark.parametrize("inherited_wide", [False, True])
async def test_prepare_seed_fixture_preserves_inherited_schema_and_rows(inherited_wide):
    # Reproduce a preceding suite's table within this test's already isolated table.
    await database.execute("DROP TABLE external_product_seeds")
    extra = ", attached_variant_id TEXT" if inherited_wide else ""
    await database.execute("CREATE TABLE external_product_seeds (id TEXT PRIMARY KEY, owner_value TEXT" + extra + ")")
    await database.execute("INSERT INTO external_product_seeds (id,owner_value) VALUES ('other-suite','preserve-me')")

    async def columns():
        rows = await database.fetch_all(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='external_product_seeds' ORDER BY ordinal_position"
            if IS_POSTGRES else "PRAGMA table_info(external_product_seeds)"
        )
        return [row["column_name" if IS_POSTGRES else "name"] for row in rows]

    before_columns = await columns()
    before_rows = [dict(row) for row in await database.fetch_all("SELECT * FROM external_product_seeds")]
    async with isolated_prepare_seed_table():
        assert "attached_variant_id" in await columns()
        assert await database.fetch_all("SELECT * FROM external_product_seeds") == []
        await database.execute("INSERT INTO external_product_seeds (id,attached_variant_id) VALUES ('owned-fixture','123')")
    assert await columns() == before_columns
    assert [dict(row) for row in await database.fetch_all("SELECT * FROM external_product_seeds")] == before_rows


async def seed(monkeypatch, *, proof_age_days=0, source="external_product_seeds_mirror_v1", price="13.99", proof=True):
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    monkeypatch.delenv("REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED", raising=False)
    await database.execute(
        "INSERT INTO catalog_products (product_key,merchant_id,platform,source_product_id,title,brand,source_domain,source_system,source_ref,seed_kind) VALUES (:pk,:seller,'external_seed',:ext,'Lip Ink','Judydoll',:domain,:source,:seed,'self')",
        {"pk": PRODUCT, "seller": SELLER, "ext": EXT, "domain": DOMAIN, "source": source, "seed": SEED},
    )
    await database.execute(
        "INSERT INTO catalog_skus (sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,title,currency) VALUES (:sku,:pk,:seller,'external_seed',:ext,:variant,'Selected shade','USD')",
        {"sku": SKU, "pk": PRODUCT, "seller": SELLER, "ext": EXT, "variant": EXT + ":" + VARIANT},
    )
    await database.execute(
        "INSERT INTO catalog_offers (offer_id,sku_key,product_key,merchant_id,currency,merchant_effective_price,availability) VALUES ('prepare-offer',:sku,:pk,:seller,'USD',:price,'in_stock')",
        {"sku": SKU, "pk": PRODUCT, "seller": SELLER, "price": price},
    )
    sd = {"snapshot": {"brand": "Judydoll", "storefront_platform": "shopify", "storefront_platform_source": "products_js_v1", "variants": [{"title": "Selected shade", "price": price, "shopify_variant_id": VARIANT}]}}
    if proof:
        sd["snapshot"]["shopify_cart_proof"] = {"source": "products_js_v1", "product_js_url": "https://judydoll.com/products/lip-ink.js", "live_variant_count": 1, "variant_id": VARIANT, "checked_at": (datetime.now(timezone.utc) - timedelta(days=proof_age_days)).isoformat()}
    await database.execute(
        "INSERT INTO external_product_seeds (id,status,domain,market,destination_url,attached_product_key,seed_data) VALUES (:id,'active',:domain,'US','https://judydoll.com/products/lip-ink',:pk,:data)",
        {"id": SEED, "domain": DOMAIN, "pk": PRODUCT, "data": json.dumps(sd)},
    )
    await database.execute("INSERT INTO tierb_cart_link_eligibility (shop_domain,market,verdict,checked_at) VALUES (:domain,'US','ELIGIBLE',CURRENT_TIMESTAMP)", {"domain": DOMAIN})


async def unchanged_state():
    tables = ["catalog_products", "catalog_skus", "catalog_offers", "external_product_seeds", "tierb_cart_link_eligibility", "reap_agentic_eligibility", "buyer_identity_links", "reap_agentic_buyer_refs", "reap_agentic_enrollments", "reap_agentic_purchase_keys", "reap_agentic_purchases", "surface_click_events"]
    return {table: sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in await database.fetch_all("SELECT * FROM " + table)) for table in tables}


async def test_prepare_stored_hashed_sku_exact_readonly_witness(client, monkeypatch):
    await seed(monkeypatch)
    before = await unchanged_state()
    first = await client.post(BASE, json=body())
    assert first.status_code == 200, first.text
    assert first.json() == {"selection": {"product_key": PRODUCT, "variant_id": VARIANT, "variant_key": SKU, "merchant_domain": DOMAIN, "market": "US", "currency": "USD", "unit_price_minor": 1399, "quantity": 1, "item_source": "cart_link"}}
    again = await client.post(BASE, json=body())
    assert again.json() == first.json()
    assert await unchanged_state() == before


@pytest.mark.parametrize("change", [{"variant_id": "9"}, {"product_key": PRODUCT + "-foreign"}, {"merchant_domain": "foreign.example"}, {"market_country": "SG"}])
async def test_prepare_wrong_identity_no_mutation(client, monkeypatch, change):
    await seed(monkeypatch)
    before = await unchanged_state()
    response = await client.post(BASE, json=body(**change))
    assert response.status_code == 409, response.text
    assert "selection" not in response.json()
    assert await unchanged_state() == before


@pytest.mark.parametrize("change", [{"quantity": True}, {"quantity": "1"}, {"quantity": 1.0}, {"variant_id": int(VARIANT)}, {"variant_id": "gid://shopify/ProductVariant/" + VARIANT}, {"variant_id": "0"}, {"item_source": "reap_variant"}, {"unit_price_minor": 1}, {"variant_key": SKU}, {"buyer": {}}, {"market_country": "unknown"}])
async def test_prepare_strict_shape_before_reads(client, monkeypatch, change):
    async def forbidden(*args, **kwargs):
        raise AssertionError("invalid preparation performed SQL")
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    with monkeypatch.context() as patches:
        patches.setattr(database, "fetch_one", forbidden)
        patches.setattr(database, "fetch_all", forbidden)
        patches.setattr(database, "execute", forbidden)
        response = await client.post(BASE, json=body(**change))
        assert response.status_code == 400, response.text
        assert error(response) == "invalid_request"


@pytest.mark.parametrize("gate", ["REAP_AGENTIC_ENABLED", "REAP_AGENTIC_CREATE_ENABLED", "REAP_AGENTIC_CART_LINK_ENABLED"])
async def test_prepare_disabled_before_body_or_sql(client, monkeypatch, gate):
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    monkeypatch.setenv(gate, "0")
    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled preparation performed SQL")
    with monkeypatch.context() as patches:
        patches.setattr(database, "fetch_one", forbidden)
        patches.setattr(database, "fetch_all", forbidden)
        patches.setattr(database, "execute", forbidden)
        response = await client.post(BASE, content="not-json")
        assert response.status_code == 404 and error(response) == "not_available_on_this_rail"


@pytest.mark.parametrize("kind", ["missing", "invalid_buyer", "invalid_agent"])
async def test_prepare_auth_before_sql(client, monkeypatch, prepare_auth_app, kind):
    monkeypatch.setenv("REAP_AGENTIC_CART_LINK_ENABLED", "1")
    async def forbidden(*args, **kwargs):
        raise AssertionError("unauthenticated preparation performed SQL")
    def missing():
        return None
    def invalid():
        raise HTTPException(status_code=401, detail="invalid credential")
    dep = get_agent_context if kind == "invalid_agent" else get_agent_user_context
    old = prepare_auth_app.dependency_overrides.get(dep)
    prepare_auth_app.dependency_overrides[dep] = missing if kind == "missing" else invalid
    with monkeypatch.context() as patches:
        patches.setattr(database, "fetch_one", forbidden)
        patches.setattr(database, "fetch_all", forbidden)
        patches.setattr(database, "execute", forbidden)
        try:
            response = await client.post(BASE, json=body())
            assert response.status_code == 401
        finally:
            if old is None:
                prepare_auth_app.dependency_overrides.pop(dep, None)
            else:
                prepare_auth_app.dependency_overrides[dep] = old


@pytest.mark.parametrize("failure", ["duplicate", "suppressed", "placeholder", "foreign_source", "foreign_sku", "proof_missing", "proof_expired", "wrong_seed_product", "eligibility_expired", "own_unpriced", "own_foreign_currency", "sku_foreign_currency", "own_conflicting_price"])
async def test_prepare_evidence_refusal_no_mutation(client, monkeypatch, failure):
    await seed(monkeypatch, proof=failure != "proof_missing", proof_age_days=10 if failure == "proof_expired" else 0, source="unknown" if failure == "foreign_source" else "external_product_seeds_mirror_v1")
    if failure == "duplicate":
        await database.execute("INSERT INTO catalog_skus (sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,title,currency) SELECT :other,product_key,merchant_id,platform,source_product_id,:variant,title,currency FROM catalog_skus WHERE sku_key=:sku", {"other": PRODUCT + "::alias", "variant": VARIANT, "sku": SKU})
    elif failure == "suppressed":
        await database.execute("UPDATE catalog_skus SET suppression_reason='unsafe' WHERE sku_key=:sku", {"sku": SKU})
    elif failure == "placeholder":
        await database.execute("UPDATE catalog_skus SET sku_key=:placeholder,source_variant_id=:pk WHERE sku_key=:sku", {"placeholder": PRODUCT + "::canonical", "pk": PRODUCT, "sku": SKU})
    elif failure == "foreign_sku":
        await database.execute("UPDATE catalog_skus SET source_variant_id=:foreign WHERE sku_key=:sku", {"foreign": "ext_other:" + VARIANT, "sku": SKU})
    elif failure == "wrong_seed_product":
        await database.execute("UPDATE external_product_seeds SET attached_product_key='foreign' WHERE id=:id", {"id": SEED})
    elif failure == "eligibility_expired":
        old = datetime.now(timezone.utc) - timedelta(hours=49)
        await database.execute("UPDATE tierb_cart_link_eligibility SET checked_at=:old", {"old": old if IS_POSTGRES else old.strftime("%Y-%m-%d %H:%M:%S")})
    elif failure == "own_unpriced":
        await database.execute("DELETE FROM catalog_offers")
    elif failure == "own_foreign_currency":
        await database.execute("UPDATE catalog_offers SET currency='EUR'")
    elif failure == "sku_foreign_currency":
        await database.execute("UPDATE catalog_skus SET currency='EUR'")
    elif failure == "own_conflicting_price":
        await database.execute("INSERT INTO catalog_offers (offer_id,sku_key,product_key,merchant_id,currency,merchant_effective_price,availability) SELECT 'conflicting',sku_key,product_key,merchant_id,currency,14.99,availability FROM catalog_offers WHERE offer_id='prepare-offer'")
    before = await unchanged_state()
    response = await client.post(BASE, json=body())
    assert response.status_code == 409, response.text
    assert await unchanged_state() == before


async def test_prepare_own_price_and_resolved_scope_not_caller_guessed(client, monkeypatch, prepare_auth_agent):
    await seed(monkeypatch)
    monkeypatch.setenv("REAP_AGENTIC_PILOT_SCOPE", json.dumps({"agent_ids": [prepare_auth_agent], "merchant_domains": [DOMAIN], "markets": ["US"], "product_keys": [PRODUCT], "quantities": [1], "variant_keys": ["shopify:" + VARIANT], "currency": "USD", "max_total_minor": 1398}))
    before = await unchanged_state()
    response = await client.post(BASE, json=body())
    assert response.status_code == 404 and error(response) == "not_available_on_this_rail"
    assert await unchanged_state() == before

async def test_prepare_witness_create_replay_and_paused_recovery(client, monkeypatch):
    await seed(monkeypatch)
    before = await unchanged_state()
    witness = (await client.post(BASE, json=body())).json()['selection']
    assert await unchanged_state() == before
    original = {'item_source': 'cart_link', 'merchant_domain': witness['merchant_domain'],
                'product_key': witness['product_key'], 'variant_key': witness['variant_key'],
                'quantity': witness['quantity'], 'idempotency_key': 'owned-prepare-original',
                'return_url': 'https://agent.pivota.cc/reap/return',
                'buyer': {'email': 'selection@example.test', 'name': 'Selection Verifier',
                          'phone': '+14155550100', 'consent_version': 'selection-test-v1',
                          'shipping_address': {'firstName': 'Selection', 'lastName': 'Verifier',
                              'phone': '+14155550100', 'addressLine1': '900 Brannan St',
                              'city': 'San Francisco', 'country': 'US', 'postalCode': '94103'}}}
    created = await client.post('/agent/v2/commerce/reap/purchases', json=original)
    assert created.status_code == 202, created.text
    again = await client.post('/agent/v2/commerce/reap/purchases', json=original)
    assert again.status_code == 202 and again.json()['purchase_id'] == created.json()['purchase_id']
    row = dict(await database.fetch_one('SELECT * FROM reap_agentic_purchases'))
    assert row['variant_key'] == 'shopify:' + VARIANT and row['our_price_minor'] == 1399
    assert await database.fetch_val('SELECT COUNT(*) FROM reap_agentic_purchase_keys') == 1
    monkeypatch.setenv('REAP_AGENTIC_CREATE_ENABLED', '0')
    monkeypatch.setenv('REAP_AGENTIC_PILOT_SCOPE', '{}')
    # Recovery must never resolve today's catalog or run the new preparation path.
    async def forbidden(*args, **kwargs):
        raise AssertionError('read-only recovery performed selector preparation')
    monkeypatch.setattr(reap, '_load_cart_link_item', forbidden)
    stable = await unchanged_state()
    recovered = await client.post('/agent/v2/commerce/reap/purchases/recover', json=original)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['id'] == created.json()['purchase_id']
    assert await unchanged_state() == stable
