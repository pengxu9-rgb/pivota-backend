from __future__ import annotations


def test_after_sales_requested_refund_is_capped_to_remaining_balance() -> None:
    import routes.merchant_api_extensions as module

    amount = module._resolve_after_sales_refund_amount(
        order={"total": "100.00", "total_refunded": "80.00"},
        requested_amount="50.00",
        approved_override=None,
    )

    assert amount == 20.0


def test_after_sales_negative_total_refunded_cannot_inflate_cap() -> None:
    import routes.merchant_api_extensions as module

    amount = module._resolve_after_sales_refund_amount(
        order={"total": "100.00", "total_refunded": "-25.00"},
        requested_amount="999.00",
        approved_override=None,
    )

    assert amount == 100.0


def test_after_sales_fully_refunded_order_has_no_refundable_amount() -> None:
    import routes.merchant_api_extensions as module

    amount = module._resolve_after_sales_refund_amount(
        order={"total": "100.00", "total_refunded": "100.00"},
        requested_amount="10.00",
        approved_override=None,
    )

    assert amount is None
