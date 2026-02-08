import pytest


@pytest.mark.asyncio
async def test_payment_txn_reuses_existing_authorization_on_different_gateway(monkeypatch):
    from services import shopify_transactions_service as svc

    async def fake_list_shopify_order_transactions(**_kwargs):
        return [
            {
                "id": 101,
                "gateway": "manual",
                "authorization": "pi_123",
                "status": "success",
                "kind": "sale",
            }
        ]

    async def fake_create_shopify_order_transaction(**_kwargs):
        raise AssertionError("create_shopify_order_transaction should not be called")

    monkeypatch.setattr(
        svc,
        "list_shopify_order_transactions",
        fake_list_shopify_order_transactions,
    )
    monkeypatch.setattr(
        svc,
        "create_shopify_order_transaction",
        fake_create_shopify_order_transaction,
    )

    result = await svc.ensure_external_payment_transaction_best_effort(
        shop_domain="shop.myshopify.com",
        access_token="token",
        shopify_order_id="123",
        psp_used="stripe",
        external_payment_ref="pi_123",
        amount=12.0,
        currency="USD",
        pivota_order_id="ORD_1",
    )

    assert result["ok"] is True
    assert result["created"] is False
    assert result["matched_existing_authorization"] is True
    assert result["existing_gateway"] == "manual"


@pytest.mark.asyncio
async def test_payment_txn_treats_invalid_sale_kind_422_as_soft_skip(monkeypatch):
    from services import shopify_transactions_service as svc

    async def fake_list_shopify_order_transactions(**_kwargs):
        return []

    async def fake_create_shopify_order_transaction(**_kwargs):
        raise svc.ShopifyTransactionSyncHttpError(
            status_code=422,
            response_text='{"errors":{"kind":["sale is not a valid transaction"]}}',
        )

    async def fake_annotate_shopify_order_best_effort(**_kwargs):
        return {"ok": True, "status": 200}

    monkeypatch.setattr(
        svc,
        "list_shopify_order_transactions",
        fake_list_shopify_order_transactions,
    )
    monkeypatch.setattr(
        svc,
        "create_shopify_order_transaction",
        fake_create_shopify_order_transaction,
    )
    monkeypatch.setattr(
        svc,
        "annotate_shopify_order_best_effort",
        fake_annotate_shopify_order_best_effort,
    )

    result = await svc.ensure_external_payment_transaction_best_effort(
        shop_domain="shop.myshopify.com",
        access_token="token",
        shopify_order_id="123",
        psp_used="stripe",
        external_payment_ref="pi_456",
        amount=15.0,
        currency="USD",
        pivota_order_id="ORD_2",
    )

    assert result["ok"] is True
    assert result["created"] is False
    assert result["soft_skipped"] is True
    assert result["reason"] == "invalid_sale_kind"
    assert result["annotation"]["ok"] is True
