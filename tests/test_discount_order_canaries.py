from scripts.check_discount_order_canaries import _audit_order


def _discounted_order(**overrides):
    order = {
        "order_id": "ORD_TEST",
        "total": "4.07",
        "total_refunded": "0.00",
        "payment_status": "paid",
        "shopify_order_id": "7531638980936",
        "psp_used": "stripe",
        "metadata": {
            "pricing_quote": {
                "pricing": {"discount_total": "29.00"},
                "discount_evidence": {"pricing_confidence": "authoritative"},
            }
        },
    }
    order.update(overrides)
    return order


def test_canary_accepts_external_psp_refund_with_ignored_shopify_webhook():
    findings = _audit_order(
        _discounted_order(total_refunded="4.07"),
        [
            {"amount": "4.07", "status": "completed", "source": "pivota_merchant", "platform_type": None},
            {"amount": "4.07", "status": "ignored", "source": "platform_webhook", "platform_type": "shopify"},
        ],
    )

    assert findings == []


def test_canary_flags_shopify_webhook_double_count_on_external_psp_order():
    findings = _audit_order(
        _discounted_order(total_refunded="8.14"),
        [
            {"amount": "4.07", "status": "completed", "source": "pivota_merchant", "platform_type": None},
            {"amount": "4.07", "status": "completed", "source": "platform_webhook", "platform_type": "shopify"},
        ],
    )

    checks = {finding.check for finding in findings if finding.severity == "fail"}
    assert "order_total_refunded_exceeds_total" in checks
    assert "completed_refund_ledger_exceeds_total" in checks
    assert "shopify_refund_webhook_mutated_external_psp_order" in checks


def test_canary_flags_paid_discounted_order_missing_shopify_link():
    findings = _audit_order(_discounted_order(shopify_order_id=""), [])

    assert any(finding.check == "missing_shopify_order_link" for finding in findings)


def test_canary_flags_non_authoritative_paid_discount_pricing():
    order = _discounted_order()
    order["metadata"]["pricing_quote"]["discount_evidence"]["pricing_confidence"] = "partial"

    findings = _audit_order(order, [])

    assert any(finding.check == "non_authoritative_discount_pricing" for finding in findings)
