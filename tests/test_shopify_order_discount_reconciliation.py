import os
from decimal import Decimal


os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/test")

from routes import order_routes


def _pricing_quote_meta():
    return {
        "quote_id": "q_123",
        "pricing": {"total": "95.00", "discount_total": "5.00"},
        "promotion_lines": [
            {"source": "shopify", "method": "code", "code": "SAVE5", "amount": "-5.00"},
        ],
        "discount_evidence": {
            "pricing_confidence": "authoritative",
            "codes": [{"code": "SAVE5", "applicable": True, "source": "shopify_storefront_cart"}],
            "applications": [
                {"source": "shopify", "method": "code", "discount_class": "product", "code": "SAVE5", "amount": "-5.00"}
            ],
        },
    }


def test_shopify_order_discount_codes_and_annotations_from_quote_evidence():
    pricing_quote_meta = _pricing_quote_meta()

    assert order_routes._build_shopify_order_discount_codes(pricing_quote_meta) == [
        {"code": "SAVE5", "amount": "5.00", "type": "fixed_amount"}
    ]

    tags, note_attributes = order_routes._build_shopify_discount_order_annotations(
        order_id="ord_123",
        pricing_quote_meta=pricing_quote_meta,
    )

    assert "pivota_quote_id:q_123" in tags
    assert any(tag.startswith("pivota_discount_evidence:") for tag in tags)
    assert {"name": "pivota_quote_id", "value": "q_123"} in note_attributes
    assert {"name": "pivota_order_id", "value": "ord_123"} in note_attributes


def test_shopify_order_reconciliation_passes_when_totals_match(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "fail_closed")
    result = order_routes._reconcile_shopify_discount_order(
        order={"total": "95.00"},
        pricing_quote_meta=_pricing_quote_meta(),
        shopify_order={
            "total_price": "95.00",
            "total_discounts": "5.00",
            "transactions": [{"kind": "sale", "status": "success", "amount": "95.00"}],
        },
        transaction_amount=Decimal("95.00"),
    )

    assert result["passed"] is True
    assert result["status"] == "passed"
    assert result["mismatches"] == []


def test_shopify_order_reconciliation_fails_closed_on_discount_mismatch(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "fail_closed")
    result = order_routes._reconcile_shopify_discount_order(
        order={"total": "95.00"},
        pricing_quote_meta=_pricing_quote_meta(),
        shopify_order={
            "total_price": "95.00",
            "total_discounts": "0.00",
            "transactions": [{"kind": "sale", "status": "success", "amount": "95.00"}],
        },
        transaction_amount=Decimal("95.00"),
    )

    assert result["passed"] is False
    assert result["status"] == "failed"
    assert result["mismatches"] == ["shopify_discount_total"]


def test_shopify_order_reconciliation_observe_logs_mismatch_without_blocking(monkeypatch):
    monkeypatch.setenv("SHOPIFY_DISCOUNT_RECONCILIATION_MODE", "observe")
    result = order_routes._reconcile_shopify_discount_order(
        order={"total": "95.00"},
        pricing_quote_meta=_pricing_quote_meta(),
        shopify_order={
            "total_price": "95.00",
            "total_discounts": "0.00",
            "transactions": [{"kind": "sale", "status": "success", "amount": "95.00"}],
        },
        transaction_amount=Decimal("95.00"),
    )

    assert result["passed"] is True
    assert result["status"] == "failed"
    assert result["mismatches"] == ["shopify_discount_total"]
