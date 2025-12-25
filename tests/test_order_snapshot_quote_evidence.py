from services.pcs_evidence_pack_service import _extract_pricing_quote_evidence


def test_extract_pricing_quote_evidence_includes_hash():
    order = {
        "order_id": "ORD_1",
        "currency": "USD",
        "total": "10.00",
        "metadata": {
            "pricing_quote": {
                "quote_id": "q_1",
                "expires_at": "2025-01-01T00:00:00Z",
                "engine": "shopify_rest_checkout",
                "engine_ref": "ref_1",
                "request_fingerprint": "f" * 64,
                "pricing": {"subtotal": "10.00", "discount_total": "0.00", "shipping_fee": "0.00", "tax": "0.00", "total": "10.00"},
                "promotion_lines": [],
                "line_items": [{"product_id": "p1", "variant_id": "v1"}],
            }
        },
    }
    evidence = _extract_pricing_quote_evidence(order)
    assert evidence is not None
    assert evidence["quote_id"] == "q_1"
    assert isinstance(evidence.get("quote_hash_sha256"), str)
    assert len(evidence["quote_hash_sha256"]) == 64


def test_extract_pricing_quote_evidence_returns_none_when_missing():
    assert _extract_pricing_quote_evidence({"metadata": {}}) is None

