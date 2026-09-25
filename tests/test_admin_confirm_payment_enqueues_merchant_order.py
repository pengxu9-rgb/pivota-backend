"""The admin confirm-payment route's merchant-order create is durable.

`routes/order_routes.confirm_payment` is one of five call sites that dispatched
the post-payment merchant-order create through `background_tasks.add_task` —
which runs after the response, in the API process, with no retry, and dies with
a Cloud Run revision swap while the buyer has already been charged.

Driving the route function directly, as tests/test_refund_api_canonical_psp.py
does, because the delivery path is the point: a handler test would pass with
this call site deleted.
"""

import pytest


class _FakeConfirmAdapter:
    async def confirm_payment(self, **kwargs):
        return True, "succeeded", None


def _order():
    return {
        "order_id": "ORD_ADMIN_CONFIRM",
        "merchant_id": "merch_admin",
        "payment_status": "awaiting_payment",
        "status": "pending",
        "payment_intent_id": "pi_admin_confirm",
        "total": "31.00",
        "currency": "USD",
        "metadata": {},
    }


def _wire(monkeypatch, module, sink):
    async def fake_ensure_database_ready():
        return None

    async def fake_get_order(order_id):
        return _order()

    async def fake_get_merchant_onboarding(merchant_id):
        return {"merchant_id": merchant_id}

    async def fake_resolve(order):
        return "stripe", _FakeConfirmAdapter()

    async def fake_mark_order_paid(order_id):
        return True

    async def fake_log_order_event(**kwargs):
        return None

    async def fake_enqueue(*, order_id, merchant_id, require_shopify_primary=False):
        sink.append({
            "order_id": order_id,
            "merchant_id": merchant_id,
            "require_shopify_primary": require_shopify_primary,
        })
        return "job-1"

    monkeypatch.setattr(module, "ensure_database_ready", fake_ensure_database_ready)
    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "_resolve_order_psp_adapter", fake_resolve)
    monkeypatch.setattr(module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(module, "log_order_event", fake_log_order_event)
    # Patch where the name is USED: order_routes binds it with a module-level
    # `from ... import`, so patching the source module would not reach it.
    monkeypatch.setattr(module, "enqueue_merchant_order_create", fake_enqueue)


@pytest.mark.asyncio
async def test_admin_confirm_payment_enqueues_a_durable_merchant_order_job(monkeypatch):
    from fastapi import BackgroundTasks
    import routes.order_routes as module

    enqueued = []
    _wire(monkeypatch, module, enqueued)

    queued = []
    background_tasks = BackgroundTasks()
    original = background_tasks.add_task

    def recording(func, *args, **kwargs):
        queued.append(getattr(func, "__name__", "unknown"))
        original(func, *args, **kwargs)

    background_tasks.add_task = recording

    result = await module.confirm_payment(
        module.PaymentConfirmRequest(
            order_id="ORD_ADMIN_CONFIRM", payment_method_id="pm_x"
        ),
        background_tasks,
        current_user={"user_id": "admin"},
    )

    assert result["status"] == "success"
    assert enqueued == [{
        "order_id": "ORD_ADMIN_CONFIRM",
        "merchant_id": "merch_admin",
        # This site has no store guard of its own: sync_order_to_connected_store
        # dispatches Shopify, WooCommerce, BigCommerce and Wix, and this path has
        # always relied on that.
        "require_shopify_primary": False,
    }]
    # And it is no longer a background task.
    assert "create_shopify_order_task" not in queued
