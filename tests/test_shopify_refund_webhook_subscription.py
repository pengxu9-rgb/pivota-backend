"""Every Shopify topic the canonical adapter maps must actually be subscribed.

Audit finding P1-D (2026-09-04): `services/shopify_commerce_event_adapter.py`
mapped `refunds/create`, but no install path registered it. The OAuth
required-topics list and the ops sweep list omitted it, and the App Store
app's `shopify.app.toml` subscribed only `orders/paid` + `app/uninstalled`.
Refunds therefore reached the ledger only for merchants who had run the verify
flow, and App Store installs never received `orders/create` or
`orders/cancelled` either. A topic the adapter maps is a topic the funnel
counts on; these tests make an unsubscribed mapped topic a build failure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi import BackgroundTasks

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from routes.merchant_store_connections import _SHOPIFY_OAUTH_REQUIRED_WEBHOOK_TOPICS  # noqa: E402
from routes.ops_shopify_integration_routes import _SWEEP_WEBHOOK_TOPICS  # noqa: E402
from services.shopify_commerce_event_adapter import SUPPORTED_SHOPIFY_TOPICS  # noqa: E402

_TOML = BACKEND_ROOT / "shopify.app.toml"
_STATIC_ORDERS_URI = "https://api.pivota.cc/webhooks/shopify/orders"


def _app_owned_topics() -> set[str]:
    config = tomllib.loads(_TOML.read_text(encoding="utf-8"))
    for subscription in config["webhooks"]["subscriptions"]:
        if subscription.get("uri") == _STATIC_ORDERS_URI:
            return set(subscription.get("topics") or [])
    raise AssertionError(f"no app-owned subscription delivers to {_STATIC_ORDERS_URI}")


# ---- the three subscription homes ------------------------------------------------


def test_oauth_install_registers_every_topic_the_adapter_maps():
    missing = SUPPORTED_SHOPIFY_TOPICS - set(_SHOPIFY_OAUTH_REQUIRED_WEBHOOK_TOPICS)
    assert not missing, f"adapter maps {sorted(missing)} but OAuth installs never subscribe them"
    assert "refunds/create" in _SHOPIFY_OAUTH_REQUIRED_WEBHOOK_TOPICS


def test_ops_sweep_registers_exactly_the_oauth_list():
    """The sweep re-registers what an install should have; a drift between the
    two means a repaired store and a fresh install disagree on what arrives."""
    assert list(_SWEEP_WEBHOOK_TOPICS) == list(_SHOPIFY_OAUTH_REQUIRED_WEBHOOK_TOPICS)


def test_app_store_install_subscribes_every_topic_the_adapter_maps():
    """App A holds no write_webhooks scope: the toml is its ONLY delivery path."""
    topics = _app_owned_topics()
    missing = SUPPORTED_SHOPIFY_TOPICS - topics
    assert not missing, f"adapter maps {sorted(missing)} but App Store installs never receive them"
    assert "app/uninstalled" in topics, "uninstall cleanup rides the same subscription"


def test_app_scopes_cover_the_refund_webhook():
    """refunds/create requires read_orders; a scope edit must not strand it."""
    config = tomllib.loads(_TOML.read_text(encoding="utf-8"))
    scopes = {scope.strip() for scope in config["access_scopes"]["scopes"].split(",")}
    assert "read_orders" in scopes
    assert config["access_scopes"]["use_legacy_install_flow"] is False, (
        "app-owned webhook subscriptions are rejected under the legacy install flow"
    )


# ---- the static endpoint does not gate the added topics -------------------------


class _FakeHeaders:
    def __init__(self, headers: Dict[str, str]) -> None:
        self._h = {k.lower(): v for k, v in headers.items()}

    def get(self, key: str, default: Any = None) -> Any:
        return self._h.get(key.lower(), default)


class _FakeRequest:
    def __init__(self, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        self._body = body
        self.headers = _FakeHeaders(headers or {})

    async def body(self) -> bytes:
        return self._body


_SECRET = "app-a-client-secret"


def _signature(payload: bytes) -> str:
    return base64.b64encode(hmac.new(_SECRET.encode(), payload, hashlib.sha256).digest()).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", sorted(SUPPORTED_SHOPIFY_TOPICS))
async def test_static_endpoint_hands_every_mapped_topic_to_the_shared_processor(monkeypatch, topic):
    """Subscribing a topic in the toml is pointless if the static endpoint
    drops it. Drive the real handler with a valid App A signature and prove
    each mapped topic reaches `_process_shopify_webhook_event`, the same
    function the per-merchant OAuth route calls."""
    import routes.webhook_routes as wr

    seen = []

    async def spy(**kwargs):
        seen.append(kwargs)
        return {"status": "success", "topic": kwargs["topic"]}

    async def resolve(_domain):
        return "merch_app_a"

    monkeypatch.setattr(wr, "_shopify_app_secret_candidates", lambda: [_SECRET])
    monkeypatch.setattr(wr, "_resolve_merchant_id_by_shop_domain", resolve)
    monkeypatch.setattr(wr, "record_shopify_webhook", lambda *a, **k: None)
    monkeypatch.setattr(wr, "_process_shopify_webhook_event", spy)

    payload = json.dumps({"id": 987, "order_id": 654, "transactions": []}).encode()
    response = await wr.handle_shopify_orders_static_webhook(
        request=_FakeRequest(payload),
        background_tasks=BackgroundTasks(),
        x_shopify_hmac_sha256=_signature(payload),
        x_shopify_topic=topic,
        x_shopify_shop_domain="appa-store.myshopify.com",
        x_shopify_webhook_id="wh_1",
        x_shopify_triggered_at="2026-09-04T10:00:00Z",
    )

    assert response["topic"] == topic
    assert len(seen) == 1
    assert seen[0]["topic"] == topic
    assert seen[0]["merchant_id"] == "merch_app_a"
    assert seen[0]["signature_verified"] is True


@pytest.mark.asyncio
async def test_static_endpoint_still_refuses_an_unsigned_refund(monkeypatch):
    import routes.webhook_routes as wr
    from fastapi import HTTPException

    async def never(**_kwargs):
        raise AssertionError("an unsigned delivery must not reach processing")

    monkeypatch.setattr(wr, "_shopify_app_secret_candidates", lambda: [_SECRET])
    monkeypatch.setattr(wr, "record_shopify_webhook", lambda *a, **k: None)
    monkeypatch.setattr(wr, "_process_shopify_webhook_event", never)

    payload = json.dumps({"id": 987, "order_id": 654}).encode()
    with pytest.raises(HTTPException) as error:
        await wr.handle_shopify_orders_static_webhook(
            request=_FakeRequest(payload),
            background_tasks=BackgroundTasks(),
            x_shopify_hmac_sha256=_signature(b"a different body"),
            x_shopify_topic="refunds/create",
            x_shopify_shop_domain="appa-store.myshopify.com",
            x_shopify_webhook_id="wh_2",
            x_shopify_triggered_at=None,
        )
    assert error.value.status_code == 401
