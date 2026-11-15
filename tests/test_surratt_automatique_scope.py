"""Keep the Automatique pilot scoped to its reviewed US seed and exact referral."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from main import app
from routes import agent_shop_gateway as gateway, outbound_links
from services import external_seed_review_pilot as pilot
from services.catalog_sync_service import make_catalog_product_key, make_catalog_sku_key, make_pivota_signature_id
from services.outbound_links_service import parse_and_verify_redirect_token
from tests.test_offers_one_per_seller_buyer_currency import _seed


SEED_ID = "eps_0123456789abcdef01234567"
PIN_ENV = "EXTERNAL_SEED_MACHINE_REVIEW_AUTOMATIQUE_SEED_ID"
LISTING = pilot.AUTOMATIQUE_LISTING
NATIVE_ID, GTIN, SHADE, SELLER_SKU = LISTING.variants[0]
PRODUCT_KEY = make_catalog_product_key(LISTING.seller_ref, "external_seed", pilot.AUTOMATIQUE_EXTERNAL_PRODUCT_ID)
SIGNATURE = make_pivota_signature_id(LISTING.seller_ref, "external_seed", pilot.AUTOMATIQUE_EXTERNAL_PRODUCT_ID)
SELECTED_PDP = f"{LISTING.url}?variant={NATIVE_ID}"


def test_automatique_machine_review_needs_the_exact_seed_pin(monkeypatch):
    legacy = (pilot.AMUSE_SEED_ID, pilot.ROMAND_SEED_ID, pilot.SURRATT_SEED_ID)
    monkeypatch.delenv(PIN_ENV, raising=False)
    assert pilot.machine_review_listing(SEED_ID) is None

    monkeypatch.setenv(PIN_ENV, SEED_ID)
    assert pilot.machine_review_listing(SEED_ID) is LISTING
    assert pilot.machine_review_listing(SEED_ID[:-1] + "8") is None
    for seed_id in legacy:
        assert pilot.machine_review_listing(seed_id) is pilot.LISTINGS[seed_id]

    for invalid in (SEED_ID.upper(), SEED_ID + " ", "eps_short", "eps_" + "g" * 24):
        monkeypatch.setenv(PIN_ENV, invalid)
        assert pilot.machine_review_listing(invalid) is None

    monkeypatch.setenv(PIN_ENV, pilot.SURRATT_SEED_ID)
    assert pilot.machine_review_listing(pilot.SURRATT_SEED_ID) is pilot.LISTINGS[pilot.SURRATT_SEED_ID]


@pytest.fixture
def referral_case(monkeypatch):
    variant = {
        "variant_id": NATIVE_ID,
        "variant_id_provenance": "merchant_issued",
        "shopify_variant_id": NATIVE_ID,
        "barcode": GTIN,
        "sku": SELLER_SKU,
        "title": SHADE,
        "price_amount": 34.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "url": SELECTED_PDP,
    }
    seed = _seed(seed_id=SEED_ID, host=LISTING.host, variants=[variant])
    seed.update(
        external_product_id=pilot.AUTOMATIQUE_EXTERNAL_PRODUCT_ID,
        seller_ref=LISTING.seller_ref,
        attached_product_key=PRODUCT_KEY,
        canonical_url=LISTING.url,
        destination_url=LISTING.url,
        title=LISTING.title,
        price_amount=34.0,
    )
    seed["seed_data"].update(brand=LISTING.brand, product_id=str(LISTING.native_product_id))
    sku_rows = [{
        "product_key": PRODUCT_KEY,
        "merchant_id": LISTING.seller_ref,
        "source_product_id": pilot.AUTOMATIQUE_EXTERNAL_PRODUCT_ID,
        "source_variant_id": NATIVE_ID,
        "sku_key": make_catalog_sku_key(PRODUCT_KEY, NATIVE_ID),
        "sku_payload": {"variant_id": NATIVE_ID, "variant_id_provenance": "merchant_issued"},
        "barcode": GTIN,
    }]

    async def fetch_all(query, values=None):
        sql = str(query)
        if "FROM catalog_skus" in sql:
            return sku_rows
        if "FROM external_product_seeds" in sql and "JOIN catalog_offers" not in sql:
            return [seed]
        return []

    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")
    monkeypatch.setattr(gateway, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    monkeypatch.setattr(gateway, "_group_seed_siblings_mode", lambda: "off")
    monkeypatch.setattr(gateway.database, "fetch_all", fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", AsyncMock(return_value=(
        False, SimpleNamespace(blocker_anomaly_types=[]),
    )))
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=[LISTING.seller_host]))
    monkeypatch.setattr(gateway, "_issue_served_clicks", AsyncMock())
    monkeypatch.setattr(outbound_links, "log_outbound_click", AsyncMock())
    monkeypatch.setattr(gateway.settings, "outbound_warm_handoff_enabled", True)
    monkeypatch.setattr(gateway.settings, "outbound_warm_handoff_internal_key", "test-key")
    monkeypatch.setattr(gateway.settings, "outbound_warm_handoff_brands_raw", LISTING.seller_host)
    monkeypatch.setattr(gateway.settings, "outbound_warm_handoff_rollout_pct", 100)
    warm = AsyncMock()
    monkeypatch.setattr(outbound_links, "resolve_warm_handoff", warm)
    return SimpleNamespace(client=TestClient(app), seed=seed, variant=variant, sku_rows=sku_rows, warm=warm)


def _resolve(client, *, variant=NATIVE_ID, market="US", assumed=False):
    product = {"product_id": SIGNATURE}
    if variant is not None:
        product["sku_id"] = variant
    response = client.post("/agent/shop/v1/invoke", json={
        "operation": "offers.resolve",
        "payload": {"product": product, "market": market, "market_assumed": assumed, "tool": "*"},
        "metadata": {"source": "creator-agent-ui"},
    })
    assert response.status_code == 200, response.text
    return response.json()["offers"]


def test_automatique_turns_a_supported_cart_into_a_signed_exact_referral(monkeypatch, referral_case):
    would_mint_cart = []
    stock_identity = gateway._external_seed_redirect_identity

    def observe_identity(**kwargs):
        identity = stock_identity(**kwargs)
        if kwargs.get("offer_variant_id") == NATIVE_ID:
            would_mint_cart.append(identity["cart_variant_id"])
        return identity

    monkeypatch.setattr(gateway, "_external_seed_redirect_identity", observe_identity)

    offer, = _resolve(referral_case.client)
    assert NATIVE_ID in would_mint_cart, "the fixture must exercise a valid Shopify cart candidate"
    assert (offer["price"], offer["currency"], offer["in_stock"]) == (34, "USD", True)
    assert offer["source"]["variant_match"] == "exact"
    assert offer["source"]["variant_id"] == NATIVE_ID
    assert offer["cart_prefilled"] is False
    spec = offer["execution_spec"]
    assert spec["cart_url"] is None and spec["variant_id"] is None and spec["rail"] == "referral"

    token, = parse_qs(urlsplit(offer["affiliate_url"]).query)["token"]
    claims = parse_and_verify_redirect_token(token)
    assert claims["ctx"]["seller_ref"] == LISTING.seller_ref
    assert claims["ctx"]["variant_declined"] == "not_the_named_variant"
    assert claims["market"] == "US" and claims["market_observed"] is True
    response = referral_case.client.get("/r", params={"token": token}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == claims["dest"] == spec["pdp_url"]
    target = urlsplit(response.headers["location"])
    assert (target.scheme, target.hostname, target.path) == ("https", LISTING.host, urlsplit(LISTING.url).path)
    assert parse_qs(target.query)["variant"] == [NATIVE_ID]
    referral_case.warm.assert_not_awaited()


@pytest.mark.parametrize("mismatch", [
    "wrong_variant", "no_variant", "other_market", "assumed_market", "wrong_seller",
    "wrong_gtin", "wrong_sku", "unselected_pdp", "missing_catalog_sku",
])
def test_automatique_does_not_fall_back_to_a_family_link(referral_case, mismatch):
    seed, variant = referral_case.seed, referral_case.variant
    args = {}
    if mismatch == "wrong_variant":
        args["variant"] = "43054723465284"
    elif mismatch == "no_variant":
        args["variant"] = None
    elif mismatch == "other_market":
        # A currency filter by itself would hide the offer and leave the market scope untested.
        args["market"] = seed["market"] = "CA"
        seed["price_currency"] = variant["price_currency"] = "CAD"
    elif mismatch == "assumed_market":
        args["assumed"] = True
    elif mismatch == "wrong_seller":
        seed["seller_ref"] = "merch_obs_unrelated"
    elif mismatch == "wrong_gtin":
        variant["barcode"] = "609015652433"
    elif mismatch == "wrong_sku":
        variant["sku"] = "SB002-14"
    elif mismatch == "unselected_pdp":
        variant["url"] = LISTING.url
    elif mismatch == "missing_catalog_sku":
        referral_case.sku_rows.clear()
    assert _resolve(referral_case.client, **args) == []
