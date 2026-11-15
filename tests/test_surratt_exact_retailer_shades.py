"""Drive exact Surratt referrals through offers.resolve and its signed redirect."""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from main import app
from routes import agent_shop_gateway as gateway, outbound_links
from services import exact_retailer_shades as shades
from services.surratt_retailer_pilot import (
    KIOKII_FLAG, KIOKII_RETAILER_COHORT, PILOT_FLAG, PROOF_KEY, RETAILERS, SURRATT_RETAILER_COHORT,
)
from tests.test_offers_one_per_seller_buyer_currency import _seed, _shade


FIRST = "31365263163459"
SECOND = "31365263196227"
BRAND = "surrattbeauty.com"


def _sku(product_key, merchant_id, native_id, barcode):
    return {
        "product_key": product_key, "merchant_id": merchant_id, "sku_key": f"{product_key}::v::{native_id}",
        "source_product_id": product_key.rsplit("::", 1)[-1], "source_variant_id": native_id,
        "sku_payload": {"variant_id": native_id, "variant_id_provenance": "merchant_issued"},
        "barcode": barcode, "offer_id": None,
    }


def fixture_rows(named, cohort=SURRATT_RETAILER_COHORT):
    rows = []
    for product_key, listing in cohort.listings.items():
        for native, (canonical, gtin) in listing.variants.items():
            row = _sku(product_key, listing.merchant_id, native, gtin)
            row.update(
                offer_id=f"offer-{native}", offer_type="retailer", readiness_tier="referral_only",
                merchant_name=listing.host, seller_kind="retailer", seller_domain=listing.host, source_domain=listing.host,
                canonical_url=listing.family_url, family_destination=listing.family_url, source_ref=listing.family_url,
                availability="in_stock", currency="USD", price_amount=19 if canonical == FIRST else 10,
                updated_at=datetime.now(timezone.utc), price_checked_at=datetime.now(timezone.utc),
                shade_proof=json.dumps({
                    "version": 1, "canonical_product_key": cohort.canonical_product_key,
                    "canonical_native_variant_id": canonical, "retailer_native_variant_id": native,
                    "gtin14": gtin, "retailer_landing_url": f"{listing.family_url}?variant={native}",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "price_minor": 1900 if canonical == FIRST else 1000, "availability": "in_stock",
                }),
            )
            rows.append(row)
    barcode = next((gtin for listing in cohort.listings.values() for canonical, gtin in listing.variants.values()
                    if canonical == named), None)
    return [_sku(cohort.canonical_product_key, cohort.canonical_merchant_id, named, barcode), *rows]


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")
    monkeypatch.setenv(PILOT_FLAG, "1")
    monkeypatch.setattr(gateway, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=[BRAND, *shades.RETAILER_HOSTS]))
    monkeypatch.setattr(gateway, "_issue_served_clicks", AsyncMock())
    monkeypatch.setattr(gateway, "_group_seed_siblings_mode", lambda: "off")
    monkeypatch.setattr(outbound_links, "log_outbound_click", AsyncMock())
    monkeypatch.setattr(gateway.settings, "outbound_warm_handoff_enabled", True)
    monkeypatch.setattr(gateway.settings, "outbound_warm_handoff_internal_key", "test-key")
    monkeypatch.setattr(gateway.settings, "outbound_warm_handoff_brands_raw", ",".join(shades.RETAILER_HOSTS))
    warm = AsyncMock()
    monkeypatch.setattr(outbound_links, "resolve_warm_handoff", warm)
    return TestClient(app), warm


def _invoke(client, *, sku_id, limit=5, market="US", assumed=False, cohort=SURRATT_RETAILER_COHORT):
    product = {"product_id": shades.canonical_signature(cohort), "sku_id": sku_id}
    result = client.post("/agent/shop/v1/invoke", json={
        "operation": "offers.resolve",
        "payload": {"product": product, "limit": limit, "market": market, "market_assumed": assumed, "tool": "*"},
        "metadata": {"source": "creator-agent-ui"},
    })
    assert result.status_code == 200, result.text
    return result.json()["offers"]


def _wire_sources(monkeypatch, named, rows, *, brand=BRAND, retailer="bluemercury.com", retailer_family=None,
                  generic_rows=()):
    variant = {**_shade(0, "Named"), "variant_id": named, "shopify_variant_id": named}
    brand_seed = _seed(seed_id="seed-official", host=brand, variants=[variant])
    legacy_seed = _seed(seed_id="seed-legacy-retailer", host=retailer, variants=[variant])
    if retailer_family:
        legacy_seed.update(canonical_url=retailer_family, destination_url=retailer_family)
    calls = []

    async def fetch(query, values=None):
        query = str(query)
        if "AS shade_proof" in query:
            calls.append("exact")
            return rows
        if "JOIN catalog_offers o ON o.product_key = p.product_key" in query:
            calls.append("legacy")
            return list(generic_rows)
        if "FROM external_product_seeds" in query:
            return [brand_seed, legacy_seed]
        return []

    monkeypatch.setattr(gateway.database, "fetch_all", fetch)
    gate = type("GateStatus", (), {"blocker_anomaly_types": []})()
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", AsyncMock(return_value=(False, gate)))
    return calls


@pytest.mark.parametrize("named", [FIRST, SECOND])
def test_both_retailers_use_own_native_shade_and_cold_redirect(monkeypatch, http, named):
    client, warm = http
    calls = _wire_sources(monkeypatch, named, fixture_rows(named))
    offers = _invoke(client, sku_id=f"gid://shopify/ProductVariant/{named}")
    retailer = {offer["merchant_id"]: offer for offer in offers if offer["source"]["type"] == "catalog_offer"}
    assert set(retailer) == {listing.merchant_id for listing in RETAILERS.values()}
    assert any(offer["source"].get("seed_id") == "seed-official" for offer in offers)
    assert all(offer["source"].get("seed_id") != "seed-legacy-retailer" for offer in offers)
    assert calls == ["exact"]
    for listing in RETAILERS.values():
        offer = retailer[listing.merchant_id]
        native = next(native for native, (canonical, _) in listing.variants.items() if canonical == named)
        assert offer["source"]["variant_match"] == "exact"
        assert offer["source"]["variant_id"] == named
        assert offer["source"]["retailer_variant_id"] == native
        assert offer["url"] == f"{listing.family_url}?variant={native}"
        assert offer["cart_prefilled"] is False
        assert not offer.get("execution_spec")
        token = parse_qs(urlsplit(offer["affiliate_url"]).query)["token"][0]
        redirect = client.get("/r", params={"token": token}, headers={"user-agent": "Mozilla/5.0"}, follow_redirects=False)
        target = urlsplit(redirect.headers["location"])
        assert redirect.status_code == 302
        assert target.hostname == listing.host
        assert target.path == "/products/surratt-lipslique"
        assert parse_qs(target.query)["variant"] == [native]
    warm.assert_not_awaited()


def test_wrong_evidence_and_non_explicit_market_cannot_substitute_a_shade(monkeypatch, http):
    client, _ = http
    rows = fixture_rows(FIRST)
    rows[1]["availability"] = "unknown"
    rows[3]["shade_proof"] = rows[4]["shade_proof"]
    calls = _wire_sources(monkeypatch, FIRST, rows)
    exact = _invoke(client, sku_id=FIRST, limit=1)
    assert [offer["source"].get("seed_id") for offer in exact] == ["seed-official"]
    _invoke(client, sku_id=FIRST, assumed=True)
    monkeypatch.setenv(PILOT_FLAG, "0")
    _invoke(client, sku_id=FIRST)
    assert calls == ["exact", "legacy", "legacy"]


@pytest.mark.parametrize("delta", [{"price_minor": 1999}, {"price_minor": True}, {"availability": "out_of_stock"}])
@pytest.mark.asyncio
async def test_current_price_and_stock_must_match_proof(delta):
    rows = fixture_rows(FIRST)
    rows[1]["shade_proof"] = json.dumps({**json.loads(rows[1]["shade_proof"]), **delta})
    db = type("DB", (), {"fetch_all": AsyncMock(return_value=rows)})()
    offers = await shades.read_offers(db, FIRST, timeout=1)
    assert [offer["retailer_variant_id"] for offer in offers] == ["47230401151268"]


def test_exact_selection_precedes_price_ranking_and_limit(monkeypatch, http):
    client, _ = http
    _wire_sources(monkeypatch, FIRST, fixture_rows(FIRST))
    offers = _invoke(client, sku_id=FIRST, limit=2)
    assert len(offers) == 2
    retailer = [offer for offer in offers if offer["source"]["type"] == "catalog_offer"]
    assert len(retailer) == 1 and retailer[0]["price"] == 19
    assert retailer[0]["source"]["variant_id"] == FIRST


@pytest.mark.parametrize(("canonical", "native"), [("52547428942138", "51629126123840"),
                                                  ("52547429007674", "51896079974720")])
def test_kiokii_uses_own_native_shade_in_signed_referral(monkeypatch, http, canonical, native):
    client, warm = http
    cohort = KIOKII_RETAILER_COHORT
    (listing_key, listing), = cohort.listings.items()
    from services.catalog_enrichment_agent.ingestion import retailer_product_key
    from services.seller_identity import make_observed_retailer_id

    assert listing_key == retailer_product_key(listing.host, listing.family_url)
    assert listing.merchant_id == make_observed_retailer_id(listing.host)
    monkeypatch.delenv(PILOT_FLAG)
    monkeypatch.setenv(KIOKII_FLAG, "1")
    generic_offer = {
        "offer_id": "offer-other-shop", "merchant_id": "seller-other-shop", "product_key": "other-shop-product",
        "destination_url": "https://example.com/product", "offer_type": "retailer", "readiness_tier": "referral_only",
        "availability": "in_stock", "currency": "USD", "price_amount": 15,
    }
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market",
                        AsyncMock(return_value=[listing.host, "amuseseoulmakeup.com", "example.com"]))
    monkeypatch.setattr(gateway.settings, "outbound_warm_handoff_brands_raw", listing.host)
    calls = _wire_sources(monkeypatch, canonical, fixture_rows(canonical, cohort), brand="amuseseoulmakeup.com",
                          retailer=listing.host, retailer_family=listing.family_url, generic_rows=[generic_offer])
    offers = _invoke(client, sku_id=canonical, cohort=cohort)
    assert {offer["source"].get("seed_id") for offer in offers} == {"seed-official", None}
    ordinary, = [offer for offer in offers if offer["merchant_id"] == generic_offer["merchant_id"]]
    assert ordinary.get("cart_prefilled") is None and ordinary["source"].get("variant_match") is None
    selected, = [offer for offer in offers if offer["merchant_id"] == listing.merchant_id]
    assert selected["merchant_id"] == listing.merchant_id
    assert selected["source"]["variant_id"] == canonical
    assert selected["source"]["retailer_variant_id"] == native
    assert selected["cart_prefilled"] is False
    assert not selected.get("execution_spec")
    token = parse_qs(urlsplit(selected["affiliate_url"]).query)["token"][0]
    response = client.get("/r", params={"token": token}, headers={"user-agent": "Mozilla/5.0"}, follow_redirects=False)
    assert response.status_code == 302
    target = urlsplit(response.headers["location"])
    assert target._replace(query="").geturl() == listing.family_url
    assert parse_qs(target.query)["variant"] == [native]
    assert calls == ["legacy", "exact"]
    warm.assert_not_awaited()


def test_kiokii_exact_offer_is_gated_and_requires_a_signature(monkeypatch, http):
    client, _ = http
    cohort = KIOKII_RETAILER_COHORT
    canonical = "52547429007674"
    calls = _wire_sources(monkeypatch, canonical, fixture_rows(canonical, cohort), brand="amuseseoulmakeup.com")
    offers = _invoke(client, sku_id=canonical, cohort=cohort)
    assert not any(offer["source"]["type"] == "catalog_offer" for offer in offers)
    monkeypatch.setenv(KIOKII_FLAG, "1")
    for fields in ({"market": "CA"}, {"market": "SG"}, {"assumed": True}, {"sku_id": None}, {"sku_id": FIRST}):
        offers = _invoke(client, cohort=cohort, **({"sku_id": canonical} | fields))
        assert not any(offer["source"]["type"] == "catalog_offer" for offer in offers)
    monkeypatch.setattr(gateway, "_make_external_redirect_url", AsyncMock(return_value=None))
    offers = _invoke(client, sku_id=canonical, cohort=cohort)
    assert not any(offer["source"]["type"] == "catalog_offer" for offer in offers)
    assert calls == ["legacy"] * 7 + ["exact"]


@pytest.mark.asyncio
async def test_matching_second_retailer_sku_and_duplicate_offer_are_ambiguous():
    rows = fixture_rows(FIRST)
    rows += [{**rows[1], "sku_key": "another-live-gtin-sku", "offer_id": None}]
    rows += [{**rows[3], "offer_id": "another-live-same-sku-offer"}]
    db = type("DB", (), {"fetch_all": AsyncMock(return_value=rows)})()
    assert await shades.read_offers(db, FIRST, timeout=1) == []


@pytest.mark.parametrize("observation", [None, "2026-11-01T22:00:00", timedelta(hours=-4, seconds=-1), timedelta(minutes=3)])
@pytest.mark.asyncio
async def test_missing_or_out_of_window_proof_is_withheld_even_with_fresh_price(observation):
    rows = fixture_rows(FIRST)
    proof = json.loads(rows[1]["shade_proof"])
    proof["observed_at"] = ((datetime.now(timezone.utc) + observation).isoformat()
                            if isinstance(observation, timedelta) else observation)
    rows[1]["shade_proof"] = json.dumps(proof)
    db = type("DB", (), {"fetch_all": AsyncMock(return_value=rows)})()
    offers = await shades.read_offers(db, FIRST, timeout=1)
    assert [offer["retailer_variant_id"] for offer in offers] == ["47230401151268"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("cohort", "canonical", "native_ids"), [
    (SURRATT_RETAILER_COHORT, FIRST, ("32360830435403", "47230401151268")),
    (KIOKII_RETAILER_COHORT, "52547429007674", ("51896079974720",)),
], ids=["surratt", "kiokii"])
async def test_postgres_reader_requires_exact_ownership_market_and_live_chain(cohort, canonical, native_ids):
    from databases import Database

    url = os.getenv("DATABASE_URL") or ""
    if not url.startswith(("postgresql://", "postgres://")):
        pytest.skip("requires a disposable PostgreSQL DATABASE_URL")
    # The tables are temporary to this exact connection and shadow the stock names only here.
    async with Database(url) as db, db.connection() as conn:
        for ddl in (
            "catalog_products (product_key text, merchant_id text, content_key text, canonical_url text, "
            "platform text, source_product_id text, pivota_signature_id text, sync_status text, "
            "suppressed_at timestamptz, suppression_reason text)",
            "product_group_members (merchant_id text, platform text, platform_product_id text, "
            "product_group_id text, is_primary boolean)",
            "index_pipeline_state (content_key text, serving_eligible boolean)",
            "catalog_merchants (merchant_id text, merchant_name text, status text, metadata_json jsonb)",
            "catalog_skus (sku_key text, product_key text, merchant_id text, source_product_id text, platform text, "
            "source_variant_id text, sku_payload jsonb, barcode text, currency text, suppressed_at timestamptz, suppression_reason text)",
            "catalog_offers (offer_id text, product_key text, sku_key text, merchant_id text, market text, "
            "offer_type text, is_first_party boolean, readiness_tier text, source_domain text, source_ref text, offer_payload jsonb, "
            "offer_mode text, availability text, currency text, price_confidence numeric, updated_at timestamptz, "
            "price_checked_at timestamptz, merchant_effective_price numeric, estimated_best_price numeric, list_price numeric, "
            "suppressed_at timestamptz, suppression_reason text)",
        ):
            await conn.execute(f"CREATE TEMP TABLE {ddl}")
        await conn.execute("INSERT INTO index_pipeline_state VALUES (:ck, TRUE)", {"ck": cohort.canonical_content_key})

        rows = fixture_rows(canonical, cohort)
        for row in {row["product_key"]: row for row in rows}.values():
            product_key, merchant = row["product_key"], row["merchant_id"]
            values = {"pk": product_key, "mid": merchant, "product": row["source_product_id"]}
            await conn.execute(
                "INSERT INTO catalog_products VALUES (:pk, :mid, :ck, :url, 'external_seed', :product, :sig, 'live', NULL, NULL)",
                {**values, "ck": cohort.canonical_content_key, "url": row.get("canonical_url"),
                 "sig": shades.canonical_signature(cohort) if product_key == cohort.canonical_product_key else None},
            )
            await conn.execute(
                "INSERT INTO product_group_members VALUES (:mid, 'external_seed', :product, :group_id, :is_primary)",
                {"mid": merchant, "product": row["source_product_id"], "group_id": cohort.canonical_group_id,
                 "is_primary": product_key == cohort.canonical_product_key},
            )
            await conn.execute(
                "INSERT INTO catalog_merchants VALUES (:mid, :mid, 'observed', CAST(:metadata AS jsonb))",
                {"mid": merchant, "metadata": json.dumps({"seller_identity": {
                    "kind": row.get("seller_kind"), "etld1": row.get("seller_domain"),
                }})},
            )
        for row in rows:
            values = {key: row[key] for key in ("sku_key", "product_key", "merchant_id")}
            await conn.execute(
                "INSERT INTO catalog_skus VALUES (:sku_key, :product_key, :merchant_id, :product, 'external_seed', "
                ":native, CAST(:payload AS jsonb), :barcode, 'USD', NULL, NULL)",
                {**values, "product": row["source_product_id"], "native": row["source_variant_id"],
                 "payload": json.dumps(row["sku_payload"]), "barcode": row["barcode"]},
            )
            if row["offer_id"]:
                await conn.execute(
                    "INSERT INTO catalog_offers VALUES (:offer_id, :product_key, :sku_key, :merchant_id, 'US', 'retailer', "
                    "FALSE, 'referral_only', :host, :source, CAST(:payload AS jsonb), 'redirect', 'in_stock', 'USD', "
                    "NULL, NOW(), NOW(), NULL, NULL, :price, NULL, NULL)",
                    {**values, "offer_id": row["offer_id"], "host": row["seller_domain"], "source": row["source_ref"],
                     "payload": json.dumps({"destination_url": row["family_destination"], PROOF_KEY: json.loads(row["shade_proof"])}),
                     "price": row["price_amount"]},
                )
        offers = await shades.read_offers(conn, canonical, timeout=1, cohort=cohort)
        assert {offer["retailer_variant_id"] for offer in offers} == set(native_ids)
        await conn.execute("UPDATE index_pipeline_state SET serving_eligible = FALSE")
        assert await shades.read_offers(conn, canonical, timeout=1, cohort=cohort) == []
        await conn.execute("UPDATE index_pipeline_state SET serving_eligible = TRUE")
        await conn.execute("UPDATE catalog_offers SET market='CA' WHERE offer_id=ANY(:ids)",
                           {"ids": [f"offer-{native}" for native in native_ids]})
        assert await shades.read_offers(conn, canonical, timeout=1, cohort=cohort) == []
        await conn.execute("UPDATE catalog_offers SET market='US', suppression_reason='held' WHERE offer_id=ANY(:ids)",
                           {"ids": [f"offer-{native}" for native in native_ids]})
        assert await shades.read_offers(conn, canonical, timeout=1, cohort=cohort) == []
