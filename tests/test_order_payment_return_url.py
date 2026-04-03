import pytest


def test_build_order_payment_return_url_defaults_to_checkout_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.order_routes as module

    monkeypatch.setenv("CHECKOUT_UI_BASE_URL", "https://agent.pivota.cc")

    assert (
        module._build_order_payment_return_url("ORD_TEST_123", {})
        == "https://agent.pivota.cc/order/success?orderId=ORD_TEST_123&finalizing=1"
    )


def test_build_order_payment_return_url_supports_placeholder_override() -> None:
    import routes.order_routes as module

    assert (
        module._build_order_payment_return_url(
            "ORD_TEST_456",
            {
                "payment_return_url": "https://agent.pivota.cc/order/success?orderId={order_id}&finalizing=1&return=%2Fcreator",
            },
        )
        == "https://agent.pivota.cc/order/success?orderId=ORD_TEST_456&finalizing=1&return=%2Fcreator"
    )
