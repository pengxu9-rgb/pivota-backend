from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from services import external_seed_review_pilot as pilot


def _brand_response(listing):
    return {
        "id": listing.native_product_id,
        "variants": [
            {"id": int(vid), "title": shade, "sku": sku, "barcode": gtin if not listing.corroboration_url else None,
             "available": True, "price": listing.price_minor}
            for vid, gtin, shade, sku in listing.variants
        ],
    }


@pytest.mark.parametrize(("response_host", "accepted"), [
    ("www.surrattbeauty.com", True), ("surrattbeauty.com", True),
    ("shop.surrattbeauty.com", False), ("different.example", False),
])
async def test_www_storefront_session_accepts_only_its_existing_host_equivalence(response_host, accepted):
    listing = pilot.LISTINGS[pilot.SURRATT_SEED_ID]
    path = "/products/lipslique.js?country=US"
    response = httpx.Response(200, request=httpx.Request("GET", f"https://{response_host}{path}"),
                              headers={"set-cookie": "cart_currency=USD"}, json=_brand_response(listing))
    client = SimpleNamespace(request=AsyncMock(return_value=response))
    polite = SimpleNamespace(before_request=AsyncMock(), note_response=lambda *_a, **_kw: None)
    session = pilot._Session(client, listing.host, polite, {}, "US")
    if accepted:
        assert (await session.json_presented(path))[2:] == ("USD", None)
    else:
        with pytest.raises(pilot.MarketsRefused):
            await session.json_presented(path)
    client.request.assert_awaited_once_with("GET", f"https://{listing.host}{path}")


@pytest.mark.parametrize("seed_id", pilot.LISTINGS)
async def test_market_evidence_preserves_native_barcode_provenance(seed_id, monkeypatch):
    listing = pilot.LISTINGS[seed_id]
    body = _brand_response(listing)
    response = httpx.Response(200, json={"currency": "USD", "ships_to_countries": ["US"]})
    brand = SimpleNamespace(
        request=AsyncMock(return_value=response),
        json_presented=AsyncMock(return_value=(200, body, "USD", None)),
    )
    shipping = SimpleNamespace(request=AsyncMock(return_value=httpx.Response(200, json={
        "currency": "USD", "ships_to_countries": ["US"], "myshopify_domain": listing.native_shop_domain,
    })))
    corroboration = SimpleNamespace(json=AsyncMock(return_value=(200, {
        "id": 10073908707648, "title": "AMUSE Banana Lip Oil 4.5ml",
        "variants": [
            {"id": 51629126123840, "title": "02 Banana", "barcode": "8800299133494"},
            {"id": 51896079974720, "title": "04 Juicy Peach", "barcode": "8800299133517"},
        ],
    })))
    sessions = {listing.host: brand, "kiokii.com": corroboration}
    if listing.shipping_source_url:
        sessions[listing.seller_host] = shipping
    monkeypatch.setattr(pilot, "_Session", lambda _, host, *_args: sessions[host])

    evidence = await pilot.capture_shopify_evidence(listing)
    assert len(evidence["target_sources"]) == 2
    for vid, gtin, _, _ in listing.variants:
        source = evidence["sources"][f"shopify_us:{vid}"]
        assert source["gtin"] == gtin
        if listing.corroboration_url:
            assert source["native_barcode"] is None
            assert evidence["sources"][f"barcode:{vid}"]["native_barcode"] == gtin
            assert "price_minor" not in evidence["sources"][f"barcode:{vid}"]
        else:
            assert source["native_barcode"] == gtin
        assert source["offer_url"] == f"{listing.url}?variant={vid}"
    if listing.shipping_source_url:
        shipping.request.assert_awaited_once_with("GET", "/meta.json", expect_json=True)
        brand.request.assert_not_awaited()
        assert source["shipping_source_url"] == listing.shipping_source_url
        shipping.request.return_value = httpx.Response(200, json={
            "currency": "USD", "ships_to_countries": ["US"], "myshopify_domain": "elsewhere.myshopify.com",
        })
        assert await pilot.capture_shopify_evidence(listing) == {"reason": "storefront_shop_identity_changed"}
        shipping.request.return_value = httpx.Response(200, json={
            "currency": "USD", "ships_to_countries": ["US"], "myshopify_domain": listing.native_shop_domain,
        })
    if listing.corroboration_url:
        corroboration.json.return_value[1]["variants"][0]["barcode"] = "8800299133517"
        assert await pilot.capture_shopify_evidence(listing) == {"reason": "variant_barcode_corroboration_unverified"}
    brand.json_presented.return_value = (200, body, None, "cookie_absent")
    fallback = AsyncMock(return_value=None)
    monkeypatch.setattr(pilot, "_cart_confirmed_product", fallback)
    assert await pilot.capture_shopify_evidence(listing) == {"reason": "storefront_us_price_unverified"}
    fallback.return_value = body
    if listing.corroboration_url:
        corroboration.json.return_value[1]["variants"][0]["barcode"] = "8800299133494"
    confirmed = await pilot.capture_shopify_evidence(listing)
    assert confirmed["sources"][f"shopify_us:{listing.variants[0][0]}"]["currency_proof"] == "store_base_and_same_session_cart"
    brand.json_presented.return_value = (200, body, "CAD", None)
    fallback.reset_mock()
    assert await pilot.capture_shopify_evidence(listing) == {"reason": "storefront_us_price_unverified"}
    fallback.assert_not_awaited()


async def test_machine_identity_requires_pinned_dedicated_subject(monkeypatch):
    from routes import employee_products as ep
    from fastapi.security import HTTPAuthorizationCredentials

    monkeypatch.setattr(ep, "_seed_publish_locks_enabled", lambda: True)
    monkeypatch.delenv("EXTERNAL_SEED_MACHINE_REVIEW_ENABLED", raising=False)
    with pytest.raises(HTTPException) as disabled:
        await ep._machine_seed_reviewer(None)
    assert disabled.value.status_code == 404

    machine = "machine@example.iam.gserviceaccount.com"
    monkeypatch.setenv("EXTERNAL_SEED_MACHINE_REVIEW_ENABLED", "true")
    monkeypatch.setenv("EXTERNAL_SEED_MACHINE_REVIEW_AUDIENCE", "https://api.example/seed-review")
    monkeypatch.setenv("EXTERNAL_SEED_MACHINE_REVIEW_SERVICE_ACCOUNT", machine)
    monkeypatch.setenv("EXTERNAL_SEED_MACHINE_REVIEW_SERVICE_SUBJECT", "1234")
    monkeypatch.setenv("OPS_GATEWAY_SERVICE_ACCOUNTS", "gateway@example.iam.gserviceaccount.com")
    monkeypatch.setenv("OPS_GATEWAY_OIDC_AUDIENCE", "https://api.example")
    monkeypatch.setattr(ep.gateway_oidc_auth, "verify_google_service_identity", lambda *_args, **_kw: {"sub": "9876"})
    credential = HTTPAuthorizationCredentials(scheme="Bearer", credentials="synthetic")
    with pytest.raises(HTTPException) as wrong:
        await ep._machine_seed_reviewer(credential)
    assert wrong.value.status_code == 401
    monkeypatch.setattr(ep.gateway_oidc_auth, "verify_google_service_identity", lambda *_args, **_kw: {"sub": "1234"})
    assert await ep._machine_seed_reviewer(credential) == f"{machine}:1234"
    monkeypatch.setenv("OPS_GATEWAY_SERVICE_ACCOUNTS", machine)
    with pytest.raises(HTTPException) as shared:
        await ep._machine_seed_reviewer(credential)
    assert shared.value.status_code == 503
