"""S-28: a quote belongs to the agent that made it (and to the checkout-token intent it was made
under). Order create, the quote read, offer preflight and checkout-intent creation refuse anyone
else's quote exactly as if it did not exist: a quote carries its preview's buyer email and address,
and order create used to copy them into the new order for any agent at the same merchant.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import httpx
import pytest
from fastapi import HTTPException

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import services.quote_service as quote_service  # noqa: E402
from services.quote_service import QUOTE_NOT_FOUND_DETAIL, caller_may_use_quote  # noqa: E402

AGENT_A = "agent_owner"
AGENT_B = "agent_other"
MERCHANT = "merch_placeholder"
QUOTE = "q_placeholder_owned"
INTENT = "ci_placeholder_a"
OTHER_INTENT = "ci_placeholder_b"


def _stamped(intent: Optional[str]) -> Dict[str, Any]:
    return {"metadata": {"owner": {"checkout_intent_id": intent}}, "pricing": {"total": "10.00"}}


def _row(*, agent: Optional[str] = AGENT_A, snapshot: Any = None) -> Dict[str, Any]:
    return {
        "quote_id": QUOTE,
        "merchant_id": MERCHANT,
        "agent_id": agent,
        "status": "active",
        "expires_at": None,
        "request_json": {"customer_email": "buyer@example.com", "shipping_address": {"line1": "1 Placeholder St"}},
        "snapshot_json": _stamped(None) if snapshot is None else snapshot,
    }


def _key(agent: str = AGENT_A):
    return SimpleNamespace(agent_id=agent, allowed_merchants=None, can_access_merchant=lambda _m: True)


def _token(agent: str = AGENT_A, intent: str = INTENT, **claims: Any):
    ctx = _key(agent)
    ctx.checkout_token_payload = {"agent_id": agent, "intent_id": intent, "scopes": ["checkout"], **claims}
    return ctx


def _may(row: Dict[str, Any], ctx: Any) -> bool:
    return caller_may_use_quote(
        quote_id=row["quote_id"], quote_agent_id=row["agent_id"], snapshot_json=row["snapshot_json"], context=ctx
    )


# --- the rule -------------------------------------------------------------------------------------


def test_the_agent_that_made_a_quote_may_use_it_with_its_key():
    assert _may(_row(), _key()) is True
    assert _may(_row(snapshot=_stamped(INTENT)), _key()) is True


def test_another_agent_may_not():
    assert _may(_row(), _key(AGENT_B)) is False
    assert _may(_row(snapshot=_stamped(INTENT)), _token(AGENT_B, INTENT)) is False


def test_the_internal_trusted_service_keys_own_quotes_as_one_caller():
    from routes.agent_auth import _build_internal_trusted_agent

    first = _build_internal_trusted_agent("internal-key-one")["agent_id"]
    second = _build_internal_trusted_agent("internal-key-two")["agent_id"]
    assert first != second

    assert _may(_row(agent=first), _key(second)) is True
    assert _may(_row(agent=first), _key(AGENT_A)) is False
    assert _may(_row(agent=AGENT_A), _key(first)) is False


def test_a_quote_with_no_agent_is_nobodys():
    assert _may(_row(agent=None), _key()) is False
    assert _may(_row(agent=""), _key()) is False


def test_a_caller_with_no_agent_id_may_not():
    assert _may(_row(), _key("")) is False


def test_a_token_may_use_a_quote_made_under_its_own_intent():
    assert _may(_row(snapshot=_stamped(INTENT)), _token()) is True


def test_a_token_may_not_use_another_checkouts_quote_of_the_same_agent():
    assert _may(_row(snapshot=_stamped(OTHER_INTENT)), _token()) is False


def test_a_token_may_not_use_its_agents_key_made_quote_unless_minted_for_it():
    assert _may(_row(snapshot=_stamped(None)), _token()) is False
    assert _may(_row(snapshot=_stamped(None)), _token(quote_id=QUOTE)) is True
    assert _may(_row(snapshot=_stamped(OTHER_INTENT)), _token(quote_id=QUOTE)) is True


def test_a_token_with_no_intent_never_matches_a_quote_with_no_intent():
    ctx = _token()
    ctx.checkout_token_payload.pop("intent_id")
    assert _may(_row(snapshot=_stamped(None)), ctx) is False


def test_a_token_minted_for_a_quote_still_needs_the_same_agent():
    assert _may(_row(agent=AGENT_B, snapshot=_stamped(None)), _token(quote_id=QUOTE)) is False


@pytest.mark.parametrize("snapshot", [{}, {"metadata": {}}, {"metadata": {"brief": {"brief_id": "b"}}}, "{}", None])
def test_a_legacy_quote_without_a_stamp_is_usable_by_any_token_of_its_agent(snapshot):
    row = _row(snapshot=snapshot if snapshot is not None else {})
    if snapshot is None:
        row["snapshot_json"] = None
    assert _may(row, _token()) is True
    assert _may(row, _token(AGENT_B)) is False


def test_a_stamp_stored_as_json_text_is_read():
    row = _row(snapshot='{"metadata": {"owner": {"checkout_intent_id": "ci_placeholder_b"}}}')
    assert _may(row, _token()) is False
    assert _may(row, _token(intent=OTHER_INTENT)) is True


# --- the preview stamps the quote -----------------------------------------------------------------


async def _preview(monkeypatch, **kwargs) -> Dict[str, Any]:
    from services.shopify_pricing_service import ShopifyPricingResult

    svc = quote_service.QuoteService()
    captured: Dict[str, Any] = {}

    async def fake_cart_quote(**_kw):
        return ShopifyPricingResult(
            engine="shopify_storefront_cart",
            engine_ref="cart_1",
            currency="USD",
            pricing={
                "subtotal": Decimal("10.00"),
                "discount_total": Decimal("0.00"),
                "shipping_fee": Decimal("0.00"),
                "tax": Decimal("0.00"),
                "total": Decimal("10.00"),
            },
            promotion_lines=[],
            line_items=[{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}],
            delivery_options=[],
            debug={"debug_id": "d_1"},
            discount_evidence={},
        )

    async def fake_insert(row):
        captured.update(row)

    async def fake_offer_evidence(**_kw):
        return {}

    monkeypatch.setattr(svc.pricing_storefront, "preview_cart_quote", fake_cart_quote)
    monkeypatch.setattr(quote_service, "insert_quote", fake_insert)
    monkeypatch.setattr(quote_service, "resolve_payment_offer_evidence", fake_offer_evidence)
    await svc.preview_quote(
        merchant_id=MERCHANT,
        agent_id=AGENT_A,
        items=[{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}],
        discount_codes=None,
        customer_email="buyer@example.com",
        shipping_address=None,
        selected_delivery_option=None,
        emit_analytics=False,
        **kwargs,
    )
    return captured


async def test_a_quote_previewed_under_a_token_is_stamped_with_its_intent(monkeypatch):
    row = await _preview(monkeypatch, checkout_intent_id=INTENT)
    assert row["agent_id"] == AGENT_A
    assert row["snapshot_json"]["metadata"]["owner"] == {"checkout_intent_id": INTENT}


async def test_a_quote_previewed_with_a_key_is_stamped_with_no_intent(monkeypatch):
    row = await _preview(monkeypatch)
    assert row["snapshot_json"]["metadata"]["owner"] == {"checkout_intent_id": None}


async def test_the_stamp_is_outside_the_quote_hash(monkeypatch):
    import copy

    from services.pcs_hash import sha256_json

    row = await _preview(monkeypatch, checkout_intent_id=INTENT)
    unstamped = copy.deepcopy(row["snapshot_json"])
    unstamped["metadata"].pop("owner")
    if not unstamped["metadata"]:
        unstamped.pop("metadata")
    expected = sha256_json(
        {"request": row["request_json"], "snapshot": unstamped, "request_fingerprint": row["request_fingerprint"]}
    )
    assert row["quote_hash_sha256"] == expected


async def test_a_replacement_quote_keeps_the_owner_of_the_quote_it_replaces(monkeypatch):
    svc = quote_service.QuoteService()
    calls = []

    async def fake_preview(**kwargs):
        calls.append(kwargs)
        return {"pricing": {"total": "11.00"}, "engine": "e", "engine_ref": "r", "quote_id": "q_new", "expires_at": None}

    monkeypatch.setattr(svc, "preview_quote", fake_preview)
    monkeypatch.setattr(svc, "_quote_snapshot_mismatches", lambda **_k: [{"field": "total"}])
    monkeypatch.setattr(svc, "_quote_for_confirmation_response", lambda q: q)
    snap = quote_service.QuoteSnapshot(
        quote_id=QUOTE, merchant_id=MERCHANT, agent_id=AGENT_A, expires_at=None, status="active",
        engine="e", engine_ref="r", request_fingerprint="f", request_json={"items": []},
        snapshot_json=_stamped(INTENT), quote_hash_sha256=None, debug_id=None,
    )

    with pytest.raises(quote_service.QuoteError):
        await svc.validate_quote_snapshot_live(snap, create_replacement_quote_on_mismatch=True)

    assert [c["checkout_intent_id"] for c in calls] == [INTENT, INTENT]
    assert {c["agent_id"] for c in calls} == {AGENT_A}


async def test_the_v1_preview_route_stamps_the_tokens_intent(monkeypatch):
    import routes.quote_routes as quote_routes
    from models.quote import QuotePreviewRequest

    seen = {}

    async def fake_preview(self, **kwargs):
        seen.update(kwargs)
        raise quote_service.QuoteError("STOP", "stop")

    async def ready():
        return None

    monkeypatch.setattr(quote_service.QuoteService, "preview_quote", fake_preview)
    monkeypatch.setattr(quote_routes, "ensure_database_ready", ready)
    req = QuotePreviewRequest(merchant_id=MERCHANT, items=[{"product_id": "p_1", "variant_id": "v_1", "quantity": 1}])

    with pytest.raises(HTTPException):
        await quote_routes.preview_quote(req, context=_token())
    assert seen["checkout_intent_id"] == INTENT and seen["agent_id"] == AGENT_A

    with pytest.raises(HTTPException):
        await quote_routes.preview_quote(req, context=_key())
    assert seen["checkout_intent_id"] is None


# --- the routes -----------------------------------------------------------------------------------


class _Reached(Exception):
    pass


@pytest.fixture
def quote_row(monkeypatch):
    """`get_quote` answers this row (None = no such quote). Patched on the service module, where the
    guard reads it, and on the v2 module, which reads it for the quote GET."""
    import routes.agent_v2 as agent_v2

    state: Dict[str, Any] = {"row": _row()}

    async def fake_get_quote(quote_id: str):
        row = state["row"]
        return dict(row) if row and quote_id == row["quote_id"] else None

    async def no_expire(_quote_id: str):
        return None

    monkeypatch.setattr(quote_service, "get_quote", fake_get_quote)
    monkeypatch.setattr(agent_v2, "get_quote", fake_get_quote)
    monkeypatch.setattr(agent_v2, "expire_quote_if_needed", no_expire)
    return state


def _foreign_cases():
    return [
        pytest.param(lambda: _row(agent=AGENT_B), _key, id="other-agent"),
        pytest.param(lambda: None, _key, id="missing"),
        pytest.param(lambda: _row(snapshot=_stamped(OTHER_INTENT)), _token, id="other-intent-token"),
    ]


@pytest.fixture
def app_with(monkeypatch):
    """The real app, its agent context overridden, governance and telemetry inert."""
    from main import app
    from routes.agent_auth import get_agent_context
    import mvp.events as mvp_events
    import services.agent_governance as governance_module

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(mvp_events, "emit_best_effort", lambda **_: None)
    monkeypatch.setattr(governance_module.agent_governance, "validate_request", noop)
    monkeypatch.setattr(governance_module.agent_governance, "record_response", noop)

    def install(ctx_factory):
        async def override():
            return ctx_factory()

        app.dependency_overrides[get_agent_context] = override
        return app

    yield install
    app.dependency_overrides.pop(get_agent_context, None)


async def _post(app, path: str, body: Dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body)


V1_BODY = {
    "merchant_id": MERCHANT,
    "quote_id": QUOTE,
    "customer_email": "caller@example.com",
    "items": [
        {"product_id": "p_1", "product_title": "Item", "variant_id": "v_1", "quantity": 1,
         "unit_price": "10.00", "subtotal": "10.00"}
    ],
    "shipping_address": {"name": "Caller", "address_line1": "2 Placeholder Ave", "city": "Town",
                         "postal_code": "00000", "country": "US"},
    "currency": "USD",
    "metadata": {},
}


@pytest.fixture
def v1_downstream(monkeypatch):
    """Everything v1 create does after the guard raises `_Reached` (a 418), so passing is visible."""
    import routes.agent_api as agent_api

    async def reached(*_a, **_k):
        raise HTTPException(status_code=418, detail="reached")

    monkeypatch.setattr(agent_api, "get_primary_store", reached)
    monkeypatch.setattr(agent_api, "_load_replayable_agent_order_create_response", reached)


@pytest.mark.parametrize("row,ctx", _foreign_cases())
async def test_v1_order_create_refuses_a_quote_the_caller_does_not_own(app_with, quote_row, v1_downstream, row, ctx):
    quote_row["row"] = row()
    resp = await _post(app_with(ctx), "/agent/v1/orders/create", V1_BODY)
    assert resp.status_code == 404
    assert resp.json()["detail"] == QUOTE_NOT_FOUND_DETAIL


@pytest.mark.parametrize(
    "row,ctx",
    [
        pytest.param(lambda: _row(), _key, id="own-key"),
        pytest.param(lambda: _row(snapshot=_stamped(INTENT)), _token, id="own-intent-token"),
        pytest.param(lambda: _row(snapshot={}), _token, id="legacy-same-agent-token"),
    ],
)
async def test_v1_order_create_lets_the_owner_through(app_with, quote_row, v1_downstream, row, ctx):
    quote_row["row"] = row()
    resp = await _post(app_with(ctx), "/agent/v1/orders/create", V1_BODY)
    assert resp.status_code == 418


V2_BODY = {"quote_id": QUOTE, "buyer_context": {"customer_email": "caller@example.com"}}


@pytest.fixture
def v2_downstream(monkeypatch):
    async def reached(self, **_k):
        raise HTTPException(status_code=418, detail="reached")

    monkeypatch.setattr(quote_service.QuoteService, "load_active_quote_or_raise", reached)


@pytest.mark.parametrize("row,ctx", _foreign_cases())
async def test_v2_order_create_refuses_a_quote_the_caller_does_not_own(app_with, quote_row, v2_downstream, row, ctx):
    quote_row["row"] = row()
    resp = await _post(app_with(ctx), "/agent/v2/orders", V2_BODY)
    assert resp.status_code == 404
    assert resp.json()["detail"] == QUOTE_NOT_FOUND_DETAIL


async def test_v2_order_create_lets_the_owner_through(app_with, quote_row, v2_downstream):
    resp = await _post(app_with(_key), "/agent/v2/orders", V2_BODY)
    assert resp.status_code == 418


async def test_v2_quote_read_answers_a_foreign_quote_like_a_missing_one(app_with, quote_row):
    app = app_with(_key)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        own = await client.get(f"/agent/v2/quotes/{QUOTE}")
        quote_row["row"] = _row(agent=AGENT_B)
        foreign = await client.get(f"/agent/v2/quotes/{QUOTE}")
        missing = await client.get("/agent/v2/quotes/q_placeholder_missing")

    def answer(resp):
        body = resp.json()
        body.pop("metadata", None)  # timestamp + request id
        return resp.status_code, body

    assert own.status_code == 200
    assert answer(foreign) == answer(missing)
    assert foreign.status_code == 404


async def test_offer_preflight_refuses_a_foreign_quote(quote_row, monkeypatch):
    import routes.offer_routes as offer_routes

    async def reached(self, **_k):
        raise _Reached()

    monkeypatch.setattr(quote_service.QuoteService, "load_active_quote_or_raise", reached)
    quote_row["row"] = _row(agent=AGENT_B)
    with pytest.raises(HTTPException) as exc:
        await offer_routes.preflight_quote_offer(offer_routes.OfferPreflightRequest(quote_id=QUOTE), context=_key())
    assert (exc.value.status_code, exc.value.detail) == (404, QUOTE_NOT_FOUND_DETAIL)

    quote_row["row"] = _row()
    with pytest.raises(_Reached):
        await offer_routes.preflight_quote_offer(offer_routes.OfferPreflightRequest(quote_id=QUOTE), context=_key())


async def test_a_checkout_intent_cannot_be_minted_for_a_foreign_quote(quote_row, monkeypatch):
    import routes.agent_checkout_intents as intents

    async def reached(self, **_k):
        raise _Reached()

    monkeypatch.setattr(quote_service.QuoteService, "load_active_quote_or_raise", reached)
    req = intents.CreateCheckoutIntentRequest(
        items=[{"product_id": "p_1", "merchant_id": MERCHANT, "quantity": 1}], quote_id=QUOTE
    )

    quote_row["row"] = _row(agent=AGENT_B)
    with pytest.raises(HTTPException) as exc:
        await intents.create_checkout_intent(req, context=_key(), agent_user=None)
    assert (exc.value.status_code, exc.value.detail) == (404, QUOTE_NOT_FOUND_DETAIL)

    quote_row["row"] = _row()
    with pytest.raises(_Reached):
        await intents.create_checkout_intent(req, context=_key(), agent_user=None)
