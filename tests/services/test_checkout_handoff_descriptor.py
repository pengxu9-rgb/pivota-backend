from __future__ import annotations

from typing import Any, Mapping


def _ctx() -> dict:
    return {
        "merchant_id": "merch_1",
        "product_key": "prod::merch_1::shopify::p1",
        "sku_key": "prod::merch_1::shopify::p1::v::v1",
        "product": {
            "merchant_id": "merch_1",
            "product_key": "prod::merch_1::shopify::p1",
            "pivota_signature_id": "sig_abc123",
        },
        "sku": {"sku_key": "prod::merch_1::shopify::p1::v::v1"},
    }


def _deliverability(status: str = "transactable") -> dict:
    return {
        "status": status,
        "checkout": {
            "allows_pivota_order": True,
            "allows_psp_creation": True,
            "commerce_path": "pivota_direct_quote_first",
            "validation_authority": "pivota_live_quote",
            "execution_policy_version": "2026-04-29.v1",
            "offer": {
                "offer_id": "offer_1",
                "merchant_id": "merch_1",
                "product_key": "prod::merch_1::shopify::p1",
                "sku_key": "prod::merch_1::shopify::p1::v::v1",
            },
        },
    }


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_descriptor_emits_identifier_only_shape_without_live_url():
    from services.checkout_handoff_descriptor import build_checkout_handoff_descriptor

    descriptor = build_checkout_handoff_descriptor(
        sku_ctx=_ctx(),
        deliverability=_deliverability(),
        audit_run_id="audit_1",
    )

    assert descriptor is not None
    assert descriptor["status"] == "eligible"
    assert descriptor["kind"] == "pivota_agent_checkout_handoff"
    assert descriptor["merchant_id"] == "merch_1"
    assert descriptor["offer_id"] == "offer_1"
    assert descriptor["pivota_signature_id"] == "sig_abc123"
    assert descriptor["source_audit_run_id"] == "audit_1"
    assert "handoff_url" not in descriptor

    forbidden = {
        "amount",
        "expected_amount",
        "total_amount",
        "unit_price",
        "price",
        "tax",
        "shipping",
        "currency",
        "confirmation_token",
        "payment_token",
    }
    assert forbidden.isdisjoint(set(_walk_keys(descriptor)))


def test_descriptor_omits_non_transactable_sku():
    from services.checkout_handoff_descriptor import build_checkout_handoff_descriptor

    assert (
        build_checkout_handoff_descriptor(
            sku_ctx=_ctx(),
            deliverability=_deliverability("servable_not_transactable"),
        )
        is None
    )


def test_descriptor_missing_sig_or_offer_is_not_linkable():
    from services.checkout_handoff_descriptor import build_checkout_handoff_descriptor

    ctx = _ctx()
    ctx["product"].pop("pivota_signature_id")
    deliverability = _deliverability()
    deliverability["checkout"]["offer"].pop("offer_id")

    descriptor = build_checkout_handoff_descriptor(
        sku_ctx=ctx,
        deliverability=deliverability,
        handoff_url="https://agent.pivota.cc/checkout/handoff?token=t",
    )

    assert descriptor is not None
    assert descriptor["status"] == "not_linkable"
    assert "handoff_url" not in descriptor
    assert descriptor["reason_codes"] == ["handoff_identity_or_policy_incomplete"]
    assert "catalog_products.pivota_signature_id" in descriptor["missing_inputs"]
    assert "checkout.offer.offer_id" in descriptor["missing_inputs"]


def test_descriptor_keeps_explicit_http_handoff_url_only_when_eligible():
    from services.checkout_handoff_descriptor import build_checkout_handoff_descriptor

    descriptor = build_checkout_handoff_descriptor(
        sku_ctx=_ctx(),
        deliverability=_deliverability(),
        handoff_url="https://agent.pivota.cc/checkout/handoff?token=t",
    )

    assert descriptor is not None
    assert descriptor["status"] == "eligible"
    assert descriptor["handoff_url"] == "https://agent.pivota.cc/checkout/handoff?token=t"

    invalid = build_checkout_handoff_descriptor(
        sku_ctx=_ctx(),
        deliverability=_deliverability(),
        handoff_url="/future/relative/path",
    )
    assert invalid is not None
    assert "handoff_url" not in invalid
