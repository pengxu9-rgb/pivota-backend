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

    create_calls = []

    async def fake_create_shopify_order_transaction(**kwargs):
        create_calls.append(kwargs["transaction"])
        if len(create_calls) == 1:
            raise svc.ShopifyTransactionSyncHttpError(
                status_code=422,
                response_text='{"errors":{"kind":["sale is not a valid transaction"]}}',
            )
        return {"id": 555, "gateway": "manual"}

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
    assert result["parent_transaction_id"] == 555
    assert result["parent_transaction_gateway"] == "manual"
    assert result["parent_transaction_source"] == "created_manual_parent"
    assert result["parent_transaction_created"] is True
    assert create_calls[1]["kind"] == "sale"
    assert create_calls[1]["gateway"] == "manual"


@pytest.mark.asyncio
async def test_refund_txn_prefers_explicit_parent_transaction_and_parent_gateway(monkeypatch):
    from services import shopify_transactions_service as svc

    seen = {}

    async def fake_list_shopify_order_transactions(**_kwargs):
        return [
            {"id": 777, "gateway": "manual", "kind": "sale", "status": "success", "authorization": "pi_parent"},
        ]

    async def fake_create_shopify_order_transaction(**kwargs):
        seen["transaction"] = dict(kwargs["transaction"])
        return {"id": 888}

    monkeypatch.setattr(svc, "list_shopify_order_transactions", fake_list_shopify_order_transactions)
    monkeypatch.setattr(svc, "create_shopify_order_transaction", fake_create_shopify_order_transaction)

    result = await svc.ensure_external_refund_transaction_best_effort(
        shop_domain="shop.myshopify.com",
        access_token="token",
        shopify_order_id="123",
        psp_used="stripe",
        external_refund_ref="re_123",
        amount=12.0,
        currency="USD",
        parent_transaction_id=777,
        pivota_order_id="ORD_3",
    )

    assert result["ok"] is True
    assert result["created"] is True
    assert result["parent_transaction_id"] == 777
    assert result["parent_transaction_gateway"] == "manual"
    assert seen["transaction"]["parent_id"] == 777
    assert seen["transaction"]["gateway"] == "manual"


@pytest.mark.asyncio
async def test_refund_txn_soft_skips_when_parent_transaction_is_missing(monkeypatch):
    from services import shopify_transactions_service as svc

    async def fake_list_shopify_order_transactions(**_kwargs):
        return []

    async def fake_annotate_shopify_order_best_effort(**_kwargs):
        return {"ok": True, "status": 200}

    async def fake_create_shopify_order_transaction(**_kwargs):
        raise AssertionError("refund transaction should not be created without a parent transaction")

    monkeypatch.setattr(svc, "list_shopify_order_transactions", fake_list_shopify_order_transactions)
    monkeypatch.setattr(svc, "annotate_shopify_order_best_effort", fake_annotate_shopify_order_best_effort)
    monkeypatch.setattr(svc, "create_shopify_order_transaction", fake_create_shopify_order_transaction)

    result = await svc.ensure_external_refund_transaction_best_effort(
        shop_domain="shop.myshopify.com",
        access_token="token",
        shopify_order_id="123",
        psp_used="stripe",
        external_refund_ref="re_456",
        amount=12.0,
        currency="USD",
        pivota_order_id="ORD_4",
    )

    assert result["ok"] is True
    assert result["created"] is False
    assert result["soft_skipped"] is True
    assert result["reason"] == "missing_parent_transaction"


@pytest.mark.asyncio
async def test_refund_txn_is_idempotent_when_parent_gateway_differs_from_psp(monkeypatch):
    """The retry-safety property the durable sync queue depends on.

    The dedupe loop used to skip any transaction whose gateway differed from
    `normalize_shopify_gateway(psp_used)`, while the create writes
    `parent_gateway or gateway`. With a `manual` parent and psp_used="stripe"
    the row this function created could never match its own dedupe filter, so
    every call created another refund transaction for the same refund —
    measured at 3 calls, 3 rows, all carrying the same authorization.
    """
    from services import shopify_transactions_service as svc

    # A store whose transactions actually persist, so a second call can see
    # what the first one wrote.
    store = [
        {"id": 777, "gateway": "manual", "kind": "sale", "status": "success",
         "authorization": "pi_parent"},
    ]
    creates = []

    async def fake_list(**_kwargs):
        return list(store)

    async def fake_create(**kwargs):
        txn = dict(kwargs["transaction"])
        creates.append(txn)
        row = dict(txn)
        row["id"] = 9000 + len(creates)
        store.append(row)
        return {"id": row["id"]}

    monkeypatch.setattr(svc, "list_shopify_order_transactions", fake_list)
    monkeypatch.setattr(svc, "create_shopify_order_transaction", fake_create)

    async def call():
        return await svc.ensure_external_refund_transaction_best_effort(
            shop_domain="shop.myshopify.com",
            access_token="token",
            shopify_order_id="123",
            psp_used="stripe",
            external_refund_ref="re_IDEMPOTENT",
            amount=12.0,
            currency="USD",
            parent_transaction_id=777,
            pivota_order_id="ORD_RETRY",
        )

    first = await call()
    second = await call()
    third = await call()

    assert first["created"] is True
    # The row was written with the PARENT's gateway; dedupe must still find it.
    assert creates[0]["gateway"] == "manual"
    assert second["created"] is False
    assert third["created"] is False
    assert len(creates) == 1, f"refund written {len(creates)} times: {creates}"


@pytest.mark.asyncio
async def test_refund_txn_refuses_to_write_when_the_transaction_list_is_unreadable(monkeypatch):
    """A failed read must not become a write.

    The existing-transaction list is the only thing preventing a duplicate. With
    an explicit parent_transaction_id, an empty list sails past the dedupe loop
    and creates a second refund — so a list failure has to refuse, not proceed.
    """
    from services import shopify_transactions_service as svc

    async def fake_list(**_kwargs):
        raise RuntimeError("shopify 503")

    async def fake_create(**_kwargs):
        raise AssertionError(
            "must not create a refund transaction while the existing list is unknown"
        )

    async def fake_annotate(**_kwargs):
        # Stubbed so a mutant that removes the refusal fails on the assertion
        # above rather than on a live outbound request to shop.myshopify.com.
        return {"ok": True, "status": 200}

    monkeypatch.setattr(svc, "list_shopify_order_transactions", fake_list)
    monkeypatch.setattr(svc, "create_shopify_order_transaction", fake_create)
    monkeypatch.setattr(svc, "annotate_shopify_order_best_effort", fake_annotate)

    result = await svc.ensure_external_refund_transaction_best_effort(
        shop_domain="shop.myshopify.com",
        access_token="token",
        shopify_order_id="123",
        psp_used="stripe",
        external_refund_ref="re_UNREADABLE",
        amount=12.0,
        currency="USD",
        parent_transaction_id=777,
        pivota_order_id="ORD_LIST_FAIL",
    )

    assert result["ok"] is False
    assert result["retryable"] is True
    assert result["reason"] == "transaction_list_unavailable"


@pytest.mark.asyncio
async def test_refund_txn_does_not_mistake_a_non_refund_row_for_the_refund(monkeypatch):
    """Dedupe keys on `authorization` alone now, so the `kind` guard is what
    stops an unrelated transaction carrying the same string from reading as
    "already refunded" — which would silently skip a refund the merchant needs."""
    from services import shopify_transactions_service as svc

    creates = []

    async def fake_list(**_kwargs):
        return [
            {"id": 777, "gateway": "manual", "kind": "sale", "status": "success",
             "authorization": "shared_ref"},
        ]

    async def fake_create(**kwargs):
        creates.append(dict(kwargs["transaction"]))
        return {"id": 9001}

    monkeypatch.setattr(svc, "list_shopify_order_transactions", fake_list)
    monkeypatch.setattr(svc, "create_shopify_order_transaction", fake_create)

    result = await svc.ensure_external_refund_transaction_best_effort(
        shop_domain="shop.myshopify.com",
        access_token="token",
        shopify_order_id="123",
        psp_used="stripe",
        external_refund_ref="shared_ref",
        amount=12.0,
        currency="USD",
        parent_transaction_id=777,
        pivota_order_id="ORD_KIND",
    )

    assert result["created"] is True
    assert len(creates) == 1


@pytest.mark.asyncio
async def test_refund_txn_annotates_when_there_is_no_refund_reference(monkeypatch):
    """This path used to return before making ANY Shopify call, so unlike every
    sibling non-write path it left nothing for the merchant to reconcile. On a
    partial refund (no cancel step) the whole job then completed having touched
    Shopify zero times."""
    from services import shopify_transactions_service as svc

    annotated = []

    async def fake_annotate(**kwargs):
        annotated.append(kwargs)
        return {"ok": True, "status": 200}

    async def fake_list(**_kwargs):
        raise AssertionError("must not list transactions without a refund ref")

    monkeypatch.setattr(svc, "annotate_shopify_order_best_effort", fake_annotate)
    monkeypatch.setattr(svc, "list_shopify_order_transactions", fake_list)

    result = await svc.ensure_external_refund_transaction_best_effort(
        shop_domain="shop.myshopify.com",
        access_token="token",
        shopify_order_id="123",
        psp_used="checkout",
        external_refund_ref=None,
        amount=12.0,
        currency="USD",
        pivota_order_id="ORD_NO_REF",
    )

    assert result["ok"] is False
    assert result["reason"] == "missing_gateway_or_refund_ref"
    assert result["annotation"]["ok"] is True
    assert len(annotated) == 1
    assert "pivota-missing-refund-reference" in annotated[0]["tags"]


@pytest.mark.asyncio
async def test_refund_txn_ignores_a_failed_refund_row_carrying_our_reference(monkeypatch):
    """A FAILED refund transaction is not a refund. Without the status guard it
    read as "already refunded", nothing was written, and the job completed."""
    from services import shopify_transactions_service as svc

    creates = []

    async def fake_list(**_kwargs):
        return [
            {"id": 777, "gateway": "manual", "kind": "sale", "status": "success",
             "authorization": "pi_parent"},
            {"id": 888, "gateway": "stripe", "kind": "refund", "status": "failure",
             "authorization": "re_FAILED"},
        ]

    async def fake_create(**kwargs):
        creates.append(dict(kwargs["transaction"]))
        return {"id": 9002}

    monkeypatch.setattr(svc, "list_shopify_order_transactions", fake_list)
    monkeypatch.setattr(svc, "create_shopify_order_transaction", fake_create)

    result = await svc.ensure_external_refund_transaction_best_effort(
        shop_domain="shop.myshopify.com",
        access_token="token",
        shopify_order_id="123",
        psp_used="stripe",
        external_refund_ref="re_FAILED",
        amount=12.0,
        currency="USD",
        parent_transaction_id=777,
        pivota_order_id="ORD_FAILED_ROW",
    )

    assert result["created"] is True
    assert len(creates) == 1
