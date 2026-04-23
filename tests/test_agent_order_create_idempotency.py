from __future__ import annotations


def test_default_order_create_idempotency_key_scopes_quote_first_replay_by_psp() -> None:
    import routes.agent_api as module

    assert (
        module._default_order_create_idempotency_key(
            merchant_id="merch_1",
            quote_id="q_1",
            preferred_psp="adyen",
        )
        == "merch_1:q_1:adyen"
    )


def test_default_order_create_idempotency_key_omits_provider_when_absent() -> None:
    import routes.agent_api as module

    assert (
        module._default_order_create_idempotency_key(
            merchant_id="merch_1",
            quote_id="q_1",
            preferred_psp=None,
        )
        == "merch_1:q_1"
    )


def test_default_order_create_idempotency_key_preserves_stripe_checkout_mode() -> None:
    import routes.agent_api as module

    assert (
        module._default_order_create_idempotency_key(
            merchant_id="merch_1",
            quote_id="q_1",
            preferred_psp="stripe_checkout",
        )
        == "merch_1:q_1:stripe_checkout"
    )
