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


@pytest.mark.parametrize("failure", ["suppressed", "foreign_source", "foreign_sku", "proof_missing", "proof_expired", "wrong_seed_product", "eligibility_expired", "own_unpriced", "own_foreign_currency", "sku_foreign_currency", "own_conflicting_price"])
async def test_prepare_evidence_refusal_no_mutation(client, monkeypatch, failure):
    await seed(monkeypatch, proof=failure != "proof_missing", proof_age_days=10 if failure == "proof_expired" else 0, source="unknown" if failure == "foreign_source" else "external_product_seeds_mirror_v1")
    if failure == "suppressed":
        await database.execute("UPDATE catalog_skus SET suppression_reason='unsafe' WHERE sku_key=:sku", {"sku": SKU})
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

def create_body(witness, idempotency_key, *, by="variant_key"):
    """The create body a door sends from a prepare witness: its money, and its sku (`variant_key`) or
    its storefront id (`by="variant_id"`, which the create pins the variant to) -- nothing guessed."""
    return {'item_source': 'cart_link', 'merchant_domain': witness['merchant_domain'],
                'product_key': witness['product_key'], by: witness[by],
                'quantity': witness['quantity'], 'idempotency_key': idempotency_key,
                'expected_unit_price_minor': witness['unit_price_minor'],
                'expected_currency': witness['currency'],
                'return_url': 'https://agent.pivota.cc/reap/return',
                'buyer': {'email': 'selection@example.test', 'name': 'Selection Verifier',
                          'phone': '+14155550100', 'consent_version': 'selection-test-v1',
                          'shipping_address': {'firstName': 'Selection', 'lastName': 'Verifier',
                              'phone': '+14155550100', 'addressLine1': '900 Brannan St',
                              'city': 'San Francisco', 'country': 'US', 'postalCode': '94103'}}}


async def add_spelling(key, *, merchant_id=SELLER, platform="external_seed", price="13.99", source_variant_id=VARIANT):
    """A second catalog spelling of the selected variant (another writer's sku key), with its own offer."""
    await database.execute(
        "INSERT INTO catalog_skus (sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,title,currency) VALUES (:sku,:pk,:seller,:platform,:ext,:variant,'Selected shade','USD')",
        {"sku": key, "pk": PRODUCT, "seller": merchant_id, "platform": platform, "ext": EXT, "variant": source_variant_id},
    )
    if price is not None:
        await database.execute(
            "INSERT INTO catalog_offers (offer_id,sku_key,product_key,merchant_id,currency,merchant_effective_price,availability) VALUES (:id,:sku,:pk,:seller,'USD',:price,'in_stock')",
            {"id": "prepare-offer-" + key.rsplit("::", 1)[-1], "sku": key, "pk": PRODUCT, "seller": merchant_id, "price": price},
        )


#: The selected variant's other spelling, sorting AFTER the stored hashed sku and BEFORE it.
SPELLED_AFTER = PRODUCT + "::v::" + VARIANT
SPELLED_BEFORE = PRODUCT + "::sku_00000000000000000000"


@pytest.mark.parametrize("other", [SPELLED_AFTER, SPELLED_BEFORE])
async def test_prepare_dual_spelling_with_agreeing_prices_names_the_lowest_key(client, monkeypatch, other):
    """One variant spelled by two writers is create's rule here too: each spelling priced, they
    agree, the lowest sku key is the witness -- and a create naming it buys that variant."""
    await seed(monkeypatch)
    await add_spelling(other)
    before = await unchanged_state()
    response = await client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    witness = response.json()["selection"]
    assert witness["variant_key"] == min(SKU, other)
    assert (witness["variant_id"], witness["unit_price_minor"], witness["currency"]) == (VARIANT, 1399, "USD")
    assert (await client.post(BASE, json=body())).json() == response.json()
    assert await unchanged_state() == before
    created = await client.post('/agent/v2/commerce/reap/purchases', json=create_body(witness, 'prepare-dual-spelling'))
    assert created.status_code == 202, created.text
    row = dict(await database.fetch_one('SELECT * FROM reap_agentic_purchases'))
    assert row['variant_key'] == 'shopify:' + VARIANT and row['our_price_minor'] == 1399


async def test_prepare_dual_spelling_with_an_unpriced_spelling_names_the_priced_one(client, monkeypatch):
    await seed(monkeypatch)
    await add_spelling(SPELLED_BEFORE, price=None)
    response = await client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    assert response.json()["selection"]["variant_key"] == SKU


async def test_prepare_dual_spelling_with_disagreeing_prices_is_price_ambiguous(client, monkeypatch):
    await seed(monkeypatch)
    await add_spelling(SPELLED_AFTER, price="14.99")
    before = await unchanged_state()
    response = await client.post(BASE, json=body())
    assert response.status_code == 409 and error(response) == "row_price_ambiguous", response.text
    assert "selection" not in response.json()
    assert await unchanged_state() == before


@pytest.mark.parametrize("foreign", [{"merchant_id": "merch_obs_another_listing"}, {"platform": "shopify"}])
async def test_prepare_another_listings_sku_naming_the_variant_is_unverified(client, monkeypatch, foreign):
    await seed(monkeypatch)
    await add_spelling(SPELLED_AFTER, **foreign)
    before = await unchanged_state()
    response = await client.post(BASE, json=body())
    assert response.status_code == 409 and error(response) == "row_variant_unverified", response.text
    assert await unchanged_state() == before


async def test_prepare_placeholder_only_succeeds_through_the_no_key_path(client, monkeypatch):
    """A mirror row holding only its `::canonical` placeholder: the storefront proof is the identity,
    the no-key load prices the placeholder, and the witness names THAT sku -- which a create then
    names and buys the same variant at the same price."""
    await seed(monkeypatch)
    placeholder = PRODUCT + "::canonical"
    await database.execute("UPDATE catalog_skus SET sku_key=:placeholder,source_variant_id=:pk WHERE sku_key=:sku", {"placeholder": placeholder, "pk": PRODUCT, "sku": SKU})
    await database.execute("UPDATE catalog_offers SET sku_key=:placeholder WHERE sku_key=:sku", {"placeholder": placeholder, "sku": SKU})
    before = await unchanged_state()
    response = await client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    witness = response.json()["selection"]
    assert witness == {"product_key": PRODUCT, "variant_id": VARIANT, "variant_key": placeholder, "merchant_domain": DOMAIN, "market": "US", "currency": "USD", "unit_price_minor": 1399, "quantity": 1, "item_source": "cart_link"}
    assert await unchanged_state() == before
    # A create may not NAME the placeholder (it pins no variant); it creates by the storefront id.
    named = await client.post('/agent/v2/commerce/reap/purchases', json=create_body(witness, 'prepare-placeholder-named'))
    assert named.status_code == 409 and error(named) == "row_variant_unverified", named.text
    assert await unchanged_state() == before
    created = await client.post('/agent/v2/commerce/reap/purchases', json=create_body(witness, 'prepare-placeholder-only', by="variant_id"))
    assert created.status_code == 202, created.text
    row = dict(await database.fetch_one('SELECT * FROM reap_agentic_purchases'))
    assert row['variant_key'] == 'shopify:' + VARIANT and row['our_price_minor'] == 1399


async def test_prepare_placeholder_only_with_disagreeing_placeholder_offers_refuses(client, monkeypatch):
    await seed(monkeypatch)
    placeholder = PRODUCT + "::canonical"
    await database.execute("UPDATE catalog_skus SET sku_key=:placeholder,source_variant_id=:pk WHERE sku_key=:sku", {"placeholder": placeholder, "pk": PRODUCT, "sku": SKU})
    await database.execute("UPDATE catalog_offers SET sku_key=:placeholder WHERE sku_key=:sku", {"placeholder": placeholder, "sku": SKU})
    await database.execute("INSERT INTO catalog_offers (offer_id,sku_key,product_key,merchant_id,currency,merchant_effective_price,availability) VALUES ('placeholder-other',:sku,:pk,:seller,'USD','14.99','in_stock')", {"sku": placeholder, "pk": PRODUCT, "seller": SELLER})
    response = await client.post(BASE, json=body())
    assert response.status_code == 409 and error(response) == "row_price_ambiguous", response.text


async def test_prepare_never_hands_out_a_witness_its_create_would_refuse(client, monkeypatch):
    """A sole variant whose real sku carries no offer: the no-key load prices the `::canonical`
    placeholder, but a create NAMING the placeholder beside a real sku is refused -- so the witness
    is refused here, not handed to a door whose create would then fail."""
    await seed(monkeypatch)
    placeholder = PRODUCT + "::canonical"
    await add_spelling(placeholder, source_variant_id=PRODUCT, price=None)
    await database.execute("UPDATE catalog_offers SET sku_key=:placeholder WHERE sku_key=:sku", {"placeholder": placeholder, "sku": SKU})
    before = await unchanged_state()
    response = await client.post(BASE, json=body())
    assert response.status_code == 409 and error(response) == "row_variant_unverified", response.text
    assert await unchanged_state() == before


async def make_placeholder_only():
    placeholder = PRODUCT + "::canonical"
    await database.execute("UPDATE catalog_skus SET sku_key=:placeholder,source_variant_id=:pk WHERE sku_key=:sku", {"placeholder": placeholder, "pk": PRODUCT, "sku": SKU})
    await database.execute("UPDATE catalog_offers SET sku_key=:placeholder WHERE sku_key=:sku", {"placeholder": placeholder, "sku": SKU})
    return placeholder


async def edit_seed_snapshot(edit):
    raw = await database.fetch_val("SELECT seed_data FROM external_product_seeds WHERE id=:id", {"id": SEED})
    data = json.loads(raw)
    edit(data["snapshot"])
    await database.execute("UPDATE external_product_seeds SET seed_data=:data WHERE id=:id", {"data": json.dumps(data), "id": SEED})


async def test_prepare_placeholder_only_pins_the_requested_variant(client, monkeypatch):
    """The storefront proof is a placeholder-only row's only identity: a request for any other
    variant than the one it proves is refused, never answered with the proven one."""
    await seed(monkeypatch)
    await make_placeholder_only()
    before = await unchanged_state()
    response = await client.post(BASE, json=body(variant_id="9"))
    assert response.status_code == 409 and error(response) == "row_variant_unverified", response.text
    assert "selection" not in response.json()
    assert await unchanged_state() == before


@pytest.mark.parametrize("by", ["variant_id", "variant_key"])
@pytest.mark.parametrize("placeholder_only", [True, False])
async def test_a_create_from_a_witness_is_pinned_when_the_proof_moves(client, monkeypatch, placeholder_only, by):
    """The storefront proof moving to another variant between prepare and create refuses the create,
    whichever way it names the variant: by `variant_id` it is held to it (`_require_selected_variant`);
    by a real sku the catalog contradicts the proof; and a create NAMING the `::canonical` placeholder,
    which names no variant, is refused outright (review probe T1: it bought the moved variant at the
    old price)."""
    await seed(monkeypatch)
    if placeholder_only:
        await make_placeholder_only()
    response = await client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    witness = response.json()["selection"]

    def move(snapshot):
        snapshot["shopify_cart_proof"]["variant_id"] = "44444444444444"
        snapshot["variants"][0]["shopify_variant_id"] = "44444444444444"
    await edit_seed_snapshot(move)
    created = await client.post('/agent/v2/commerce/reap/purchases', json=create_body(witness, 'prepare-pinned-' + str(placeholder_only), by=by))
    assert created.status_code == 409 and error(created) == "row_variant_unverified", created.text
    assert await database.fetch_val('SELECT COUNT(*) FROM reap_agentic_purchases') == 0


OTHER_VARIANT = "49819267301654"


@pytest.mark.parametrize("other", [SPELLED_AFTER, SPELLED_BEFORE])
async def test_prepare_multi_variant_product_with_a_dual_spelled_selection_names_the_lowest_key(client, monkeypatch, other):
    """A two-variant product, the selected one spelled by two writers: the resolver's per-spelling
    branch (not the sole-variant no-key path) prices both spellings and names the lowest key."""
    await seed(monkeypatch)
    await add_spelling(PRODUCT + "::sku_other_variant", source_variant_id=EXT + ":" + OTHER_VARIANT, price="15.00")
    await add_spelling(other)

    def two_variants(snapshot):
        proof = dict(snapshot.pop("shopify_cart_proof"))
        snapshot["variants"].append({"title": "Other shade", "price": "15.00", "shopify_variant_id": OTHER_VARIANT})
        snapshot["shopify_cart_variant_proofs"] = {VARIANT: {**proof, "scope": "buyer_selected_variant", "available": True, "live_variant_count": 2}}
    await edit_seed_snapshot(two_variants)
    before = await unchanged_state()
    response = await client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    witness = response.json()["selection"]
    assert witness["variant_key"] == min(SKU, other)
    assert (witness["variant_id"], witness["unit_price_minor"], witness["currency"]) == (VARIANT, 1399, "USD")
    assert await unchanged_state() == before


async def test_prepare_witness_create_replay_and_paused_recovery(client, monkeypatch):
    await seed(monkeypatch)
    before = await unchanged_state()
    witness = (await client.post(BASE, json=body())).json()['selection']
    assert await unchanged_state() == before
    original = {'item_source': 'cart_link', 'merchant_domain': witness['merchant_domain'],
                'product_key': witness['product_key'], 'variant_key': witness['variant_key'],
                'quantity': witness['quantity'], 'idempotency_key': 'owned-prepare-original',
                'expected_unit_price_minor': witness['unit_price_minor'],
                'expected_currency': witness['currency'],
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


async def respell_seed(*, domain, url_host, proof_host=None):
    """The seed's own spelling of the store: its `domain` column (None = NULL), its product URL's host,
    and its proof's products.js host (defaults to the URL host, as the backfill writes it)."""
    page = "https://" + url_host + "/products/lip-ink"
    raw = await database.fetch_val("SELECT seed_data FROM external_product_seeds WHERE id=:id", {"id": SEED})
    data = json.loads(raw)
    data["snapshot"]["shopify_cart_proof"]["product_js_url"] = "https://" + (proof_host or url_host) + "/products/lip-ink.js"
    await database.execute(
        "UPDATE external_product_seeds SET domain=:domain, destination_url=:page, seed_data=:data WHERE id=:id",
        {"domain": domain, "page": page, "data": json.dumps(data), "id": SEED})


@pytest.mark.parametrize("seed_domain,url_host", [
    ("www." + DOMAIN, "www." + DOMAIN),  # the seed spells the store www., the product apex (1,540 prod rows)
    (None, DOMAIN),                      # the seed has no domain: placed by its product URL
    (None, "www." + DOMAIN),
])
async def test_prepare_finds_the_seed_whichever_way_it_spells_the_store(client, monkeypatch, seed_domain, url_host):
    await seed(monkeypatch)
    await respell_seed(domain=seed_domain, url_host=url_host)
    response = await client.post(BASE, json=body())
    assert response.status_code == 200, response.text
    assert (response.json()["selection"]["variant_id"], response.json()["selection"]["unit_price_minor"]) == (VARIANT, 1399)


async def test_prepare_and_create_for_a_product_spelled_www_over_an_apex_seed(client, monkeypatch):
    """The other way round (40 prod rows): the product says `www.<store>`, the seed the apex. The door
    POSTs the product's spelling; the proof is checked on the seed's own host."""
    await seed(monkeypatch)
    await database.execute("UPDATE catalog_products SET source_domain=:d WHERE product_key=:pk", {"d": "www." + DOMAIN, "pk": PRODUCT})
    response = await client.post(BASE, json=body(merchant_domain="www." + DOMAIN))
    assert response.status_code == 200, response.text
    witness = response.json()["selection"]
    created = await client.post('/agent/v2/commerce/reap/purchases', json={
        'item_source': 'cart_link', 'merchant_domain': witness['merchant_domain'], 'product_key': witness['product_key'],
        'variant_key': witness['variant_key'], 'quantity': 1, 'idempotency_key': 'respelled-www-product',
        'expected_unit_price_minor': witness['unit_price_minor'], 'expected_currency': witness['currency'],
        'return_url': 'https://agent.pivota.cc/reap/return',
        'buyer': {'email': 'selection@example.test', 'name': 'Selection Verifier', 'phone': '+14155550100',
                  'consent_version': 'selection-test-v1',
                  'shipping_address': {'firstName': 'Selection', 'lastName': 'Verifier', 'phone': '+14155550100',
                                       'addressLine1': '900 Brannan St', 'city': 'San Francisco', 'country': 'US',
                                       'postalCode': '94103'}}})
    assert created.status_code == 202, created.text
    row = dict(await database.fetch_one('SELECT * FROM reap_agentic_purchases'))
    assert row['variant_key'] == 'shopify:' + VARIANT and row['our_price_minor'] == 1399


@pytest.mark.parametrize("seed_domain,url_host,proof_host", [
    ("uk." + DOMAIN, "uk." + DOMAIN, None),  # a subdomain is ANOTHER store
    ("evil" + DOMAIN, "evil" + DOMAIN, None),  # a lookalike
    (DOMAIN, "uk." + DOMAIN, None),          # the seed's domain says this store, its page another
    (None, "other.example", None),           # no domain, and the page is another store
    ("www." + DOMAIN, "www." + DOMAIN, DOMAIN),  # the proof was fetched from a host the seed's page is not
])
async def test_prepare_refuses_a_seed_of_another_store(client, monkeypatch, seed_domain, url_host, proof_host):
    await seed(monkeypatch)
    await respell_seed(domain=seed_domain, url_host=url_host, proof_host=proof_host)
    before = await unchanged_state()
    response = await client.post(BASE, json=body())
    assert response.status_code == 409 and error(response) == "row_variant_unverified", response.text
    assert await unchanged_state() == before
