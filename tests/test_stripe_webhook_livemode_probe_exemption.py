"""Livemode gate on the Stripe PSP webhook: test-psp probe exemption.

In production the webhook drops livemode=false events unconditionally — EXCEPT
for the controlled test-processor probe: an event whose order resolves to a
merchant in TEST_PSP_PROBE_MERCHANTS, while ALLOW_TEST_PSP_PROBE is on, and
whose order metadata itself requested the test-psp bypass (the same conjuncts
order_routes._resolve_order_live_readiness_requirement used to allow the
test-mode charge).

The load-bearing property is BINDING: the exemption must attach to the SAME
order the branch handler goes on to mutate. If the gate resolved on a broader
set of references than the handler, one sanctioned probe order would become a
reusable passport — name it where the gate looks, point the handler's field at
a different order, and a fake test-mode event drives a real one. The
`is_not_a_passport` / `refund` / `auth_first` tests below pin that; they are
regression tests for four working exploits found in review.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


PROBE_MERCHANT = "merch_efbc46b4619cfbdf"
OTHER_MERCHANT = "merch_someone_else"


def _order_row(
    *,
    order_id: str,
    merchant_id: str,
    payment_intent_id: str,
    requested_bypass: bool,
    total: str = "45.20",
    metadata_as_json_string: bool = False,
    extra_metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {"agent_v2": {"hosted_checkout": True}}
    if requested_bypass:
        metadata["allow_test_psp_surfaces"] = True
    if extra_metadata:
        metadata.update(extra_metadata)
    return {
        "order_id": order_id,
        "merchant_id": merchant_id,
        "payment_intent_id": payment_intent_id,
        "payment_status": "awaiting_payment",
        "psp_used": "stripe",
        "total": total,
        "currency": "usd",
        # orders.metadata is a JSON column (db/orders.py), so a real row can hand
        # back a STRING. Exercising both shapes keeps the decode path honest.
        "metadata": json.dumps(metadata) if metadata_as_json_string else metadata,
    }


def _install_webhook_plumbing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event: Dict[str, Any],
    orders: List[Dict[str, Any]],
) -> Dict[str, list]:
    """Wire signature verification + DB resolution over a set of orders.

    Records every downstream effect the gate must not cause on a dropped event.
    """
    import db.database as database_module
    import routes.order_routes as order_routes_module
    import routes.webhook_routes as webhook_routes_module

    by_reference = {str(o["payment_intent_id"]): o for o in orders}
    by_order_id = {str(o["order_id"]): o for o in orders}

    calls: Dict[str, list] = {
        "paid": [],
        "fetch_one": [],
        "get_order": [],
        "update_order": [],
        "finalize_auth_first": [],
        "refund_finalize": [],
    }

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Dict[str, Any] | None:
        calls["fetch_one"].append(dict(values))
        reference = values.get("payment_intent_id")
        if reference is None:
            return None
        row = by_reference.get(str(reference))
        return dict(row) if row else None

    async def fake_get_order(order_id: str) -> Dict[str, Any] | None:
        calls["get_order"].append(order_id)
        row = by_order_id.get(str(order_id))
        return dict(row) if row else None

    async def fake_update_order(order_id: str, fields: Dict[str, Any]) -> bool:
        calls["update_order"].append((order_id, dict(fields)))
        return True

    async def fake_mark_order_paid(order_id: str) -> bool:
        calls["paid"].append(order_id)
        return True

    async def fake_finalize_authorized_payment_order(
        order_id: str, *, order: Any = None, source_event: str = ""
    ) -> Dict[str, Any]:
        calls["finalize_auth_first"].append((order_id, source_event))
        return {"status": "success"}

    async def fake_finalize_stripe_refund_success(
        order: Dict[str, Any], **kwargs: Any
    ) -> Dict[str, Any]:
        calls["refund_finalize"].append((order.get("order_id"), dict(kwargs)))
        return {"status": "success"}

    async def _noop(*args: Any, **kwargs: Any) -> Any:
        return None

    async def fake_get_primary_store(merchant_id: str) -> Dict[str, Any]:
        return {"platform": "shopify"}

    async def fake_create_shopify_order(order_id: str) -> bool:
        return True

    async def fake_emit_merchant_webhook_event(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "delivered"}

    def fake_construct_event(payload: bytes, signature: str | None, secret: str) -> Dict[str, Any]:
        return event

    monkeypatch.setattr(
        webhook_routes_module.settings, "stripe_webhook_secret", "whsec_test", raising=False
    )
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook,
        "construct_event",
        staticmethod(fake_construct_event),
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    # Module references, not source modules — patching db.orders would not rebind
    # the names webhook_routes already imported.
    monkeypatch.setattr(webhook_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(webhook_routes_module, "update_order", fake_update_order)
    monkeypatch.setattr(webhook_routes_module, "mark_order_paid", fake_mark_order_paid)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", _noop)
    monkeypatch.setattr(
        webhook_routes_module, "create_order_snapshot_evidence_pack", _noop
    )
    monkeypatch.setattr(webhook_routes_module, "get_primary_store", fake_get_primary_store)
    monkeypatch.setattr(
        webhook_routes_module, "emit_merchant_webhook_event", fake_emit_merchant_webhook_event
    )
    monkeypatch.setattr(order_routes_module, "create_shopify_order", fake_create_shopify_order)
    monkeypatch.setattr(
        order_routes_module,
        "finalize_authorized_payment_order",
        fake_finalize_authorized_payment_order,
    )
    monkeypatch.setattr(
        webhook_routes_module,
        "_finalize_stripe_refund_success",
        fake_finalize_stripe_refund_success,
    )
    return calls


def _arm_probe(monkeypatch: pytest.MonkeyPatch, *, env_on: bool = True) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    if env_on:
        monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    else:
        monkeypatch.delenv("ALLOW_TEST_PSP_PROBE", raising=False)
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", PROBE_MERCHANT)


async def _post_webhook() -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_probe_test_mode"}',
            headers={"stripe-signature": "sig_probe"},
        )


def _succeeded_event(
    *, object_id: str, metadata: Dict[str, Any] | None = None, amount: int = 4520
) -> Dict[str, Any]:
    obj: Dict[str, Any] = {"id": object_id, "amount": amount, "currency": "usd"}
    if metadata is not None:
        obj["metadata"] = metadata
    return {
        "type": "payment_intent.succeeded",
        "livemode": False,
        "id": "evt_probe_test_mode",
        "data": {"object": obj},
    }


_DROPPED = {
    "status": "ignored",
    "event": "payment_intent.succeeded",
    "reason": "test_mode_event_in_production",
}


# ---------------------------------------------------------------------------
# The exemption is granted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prod_processes_test_mode_event_for_armed_probe_merchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _succeeded_event(object_id="pi_probe_test_mode")
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        orders=[
            _order_row(
                order_id="ORD_PROBE",
                merchant_id=PROBE_MERCHANT,
                payment_intent_id="pi_probe_test_mode",
                requested_bypass=True,
            )
        ],
    )
    _arm_probe(monkeypatch)

    resp = await _post_webhook()

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("reason") != "test_mode_event_in_production"
    assert body["status"] == "success"
    # The machinery actually ran — not merely a non-drop response.
    assert calls["paid"] == ["ORD_PROBE"]
    assert any(v.get("payment_intent_id") == "pi_probe_test_mode" for v in calls["fetch_one"])


@pytest.mark.asyncio
async def test_prod_processes_hosted_checkout_probe_order_via_metadata_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real probe shape: the order stores the cs_ session id, the success
    event carries a fresh pi_, and the link is metadata.order_id. Also pins the
    JSON-STRING metadata decode, which is what a real orders row hands back."""
    event = _succeeded_event(
        object_id="pi_fresh_from_stripe", metadata={"order_id": "ORD_PROBE_HOSTED"}
    )
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        orders=[
            _order_row(
                order_id="ORD_PROBE_HOSTED",
                merchant_id=PROBE_MERCHANT,
                payment_intent_id="cs_probe_session",
                requested_bypass=True,
                metadata_as_json_string=True,
            )
        ],
    )
    _arm_probe(monkeypatch)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json().get("reason") != "test_mode_event_in_production"
    assert calls["paid"] == ["ORD_PROBE_HOSTED"]
    # Resolution genuinely went through the metadata hint.
    assert "ORD_PROBE_HOSTED" in calls["get_order"]


# ---------------------------------------------------------------------------
# The exemption is refused — each conjunct
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prod_drops_test_mode_event_for_non_allowlisted_merchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _succeeded_event(object_id="pi_probe_test_mode")
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        orders=[
            _order_row(
                order_id="ORD_OTHER",
                merchant_id=OTHER_MERCHANT,
                payment_intent_id="pi_probe_test_mode",
                requested_bypass=True,
            )
        ],
    )
    _arm_probe(monkeypatch)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json() == _DROPPED
    assert calls["paid"] == []
    # The allowlist decision was made on the real resolved row, not short-circuited.
    assert any(v.get("payment_intent_id") == "pi_probe_test_mode" for v in calls["fetch_one"])


@pytest.mark.asyncio
async def test_prod_drops_test_mode_event_for_probe_merchant_when_env_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _succeeded_event(object_id="pi_probe_test_mode")
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        orders=[
            _order_row(
                order_id="ORD_PROBE",
                merchant_id=PROBE_MERCHANT,
                payment_intent_id="pi_probe_test_mode",
                requested_bypass=True,
            )
        ],
    )
    _arm_probe(monkeypatch, env_on=False)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json() == _DROPPED
    assert calls["paid"] == []
    # With the env off the gate must short-circuit BEFORE touching the DB, so a
    # test-mode secret cannot drive queries in production.
    assert calls["fetch_one"] == []
    assert calls["get_order"] == []


@pytest.mark.asyncio
async def test_prod_drops_test_mode_event_when_order_never_requested_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _succeeded_event(object_id="pi_probe_test_mode")
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        orders=[
            _order_row(
                order_id="ORD_PROBE_NO_BYPASS",
                merchant_id=PROBE_MERCHANT,
                payment_intent_id="pi_probe_test_mode",
                requested_bypass=False,
            )
        ],
    )
    _arm_probe(monkeypatch)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json() == _DROPPED
    assert calls["paid"] == []
    # Env armed + merchant allowlisted, so the resolver DID run and the refusal
    # came from the order's own metadata — without this the test passes against
    # a build with no gate at all.
    assert any(v.get("payment_intent_id") == "pi_probe_test_mode" for v in calls["fetch_one"])


# ---------------------------------------------------------------------------
# Binding: a probe order must not act as a passport for another order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_order_is_not_a_passport_for_a_foreign_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metadata.order_id names the sanctioned probe order, but data["id"] — the
    reference the handler mutates on — names a different merchant's live order.
    The gate must resolve the SAME order the handler will, and refuse."""
    event = _succeeded_event(
        object_id="cs_victim_session",
        metadata={"order_id": "ORD_PROBE"},
        amount=49900,
    )
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        orders=[
            _order_row(
                order_id="ORD_PROBE",
                merchant_id=PROBE_MERCHANT,
                payment_intent_id="cs_probe_session",
                requested_bypass=True,
            ),
            _order_row(
                order_id="ORD_VICTIM_REAL",
                merchant_id="merch_real_victim",
                payment_intent_id="cs_victim_session",
                requested_bypass=False,
                total="499.00",
            ),
        ],
    )
    _arm_probe(monkeypatch)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json() == _DROPPED
    assert calls["paid"] == []


@pytest.mark.asyncio
async def test_test_mode_refund_event_is_never_exempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """charge.refunded resolves its order on data["payment_intent"] via a raw
    query with no cross-tenant guard — a reference this gate does not mirror —
    so it must never be exemptible, whatever the passport says."""
    event = {
        "type": "charge.refunded",
        "livemode": False,
        "id": "evt_probe_test_mode",
        "data": {
            "object": {
                "id": "ch_exploit",
                "payment_intent": "cs_victim_session",
                "amount_refunded": 49900,
                "currency": "usd",
                "metadata": {"order_id": "ORD_PROBE"},
            }
        },
    }
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        orders=[
            _order_row(
                order_id="ORD_PROBE",
                merchant_id=PROBE_MERCHANT,
                payment_intent_id="cs_probe_session",
                requested_bypass=True,
            ),
            _order_row(
                order_id="ORD_VICTIM_REAL",
                merchant_id="merch_real_victim",
                payment_intent_id="cs_victim_session",
                requested_bypass=False,
                total="499.00",
            ),
        ],
    )
    _arm_probe(monkeypatch)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json()["reason"] == "test_mode_event_in_production"
    assert calls["refund_finalize"] == []


@pytest.mark.asyncio
async def test_checkout_session_passport_does_not_finalize_a_foreign_auth_first_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """checkout.session.completed resolves on data["id"]; pointing
    data["payment_intent"] at the probe order must not buy an exemption that
    lets a fake test-mode event capture a real authorization."""
    event = {
        "type": "checkout.session.completed",
        "livemode": False,
        "id": "evt_probe_test_mode",
        "data": {
            "object": {
                "id": "cs_auth_first",
                "payment_intent": "cs_probe_session",
                "metadata": {"payment_flow": "authorization_first"},
            }
        },
    }
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        orders=[
            _order_row(
                order_id="ORD_PROBE",
                merchant_id=PROBE_MERCHANT,
                payment_intent_id="cs_probe_session",
                requested_bypass=True,
            ),
            _order_row(
                order_id="ORD_AUTH_FIRST_REAL",
                merchant_id="merch_real_victim",
                payment_intent_id="cs_auth_first",
                requested_bypass=False,
                total="899.00",
                extra_metadata={
                    "payment_flow": {
                        "mode": "authorization_first",
                        "psp": "stripe",
                        "capture_method": "manual",
                    }
                },
            ),
        ],
    )
    _arm_probe(monkeypatch)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json()["reason"] == "test_mode_event_in_production"
    assert calls["finalize_auth_first"] == []
    assert calls["paid"] == []


@pytest.mark.asyncio
async def test_gate_never_writes_to_the_order_it_inspects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate resolves with allow_repoint=False. Deciding whether to exempt
    must be read-only: a DROPPED test-mode event must not repoint a live order's
    payment_intent_id onto the test-mode PI."""
    event = _succeeded_event(
        object_id="pi_testmode", metadata={"order_id": "ORD_VICTIM_REAL"}
    )
    calls = _install_webhook_plumbing(
        monkeypatch,
        event=event,
        orders=[
            _order_row(
                order_id="ORD_VICTIM_REAL",
                merchant_id=OTHER_MERCHANT,
                payment_intent_id="pi_victim_real",
                requested_bypass=True,
            )
        ],
    )
    _arm_probe(monkeypatch)

    resp = await _post_webhook()

    assert resp.status_code == 200
    assert resp.json() == _DROPPED
    assert calls["paid"] == []
    # The order keeps its real PI — no write happened while deciding.
    assert calls["update_order"] == []
