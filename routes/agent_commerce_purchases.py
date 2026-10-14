"""`/agent/v2/commerce/purchases`: the rail-neutral purchase door (payment orchestration P0, P2c).

One id (`pp_…`) and one state vocabulary for a purchase on any payment rail, with the rail's own
owner-facing body nested under `detail`. Two rails: Reap and the A2A token rail. A rail's own id
(`rp_…`, `ap_…`) is accepted too, so a caller holding it can read the unified view without a lookup.

READS (GET), DARK BY DEFAULT. While `AGENT_PURCHASE_LEDGER_ENABLED` is off every read answers the
same 404, whatever its shape. Nothing a read does calls a rail.

CREATE (POST), DARK BY DEFAULT, behind its own dial (`PAYMENT_ORCHESTRATOR_ENABLED`; off: 404 to
everything, and agents keep calling the Reap route directly). It takes the Reap route's create body
plus an optional `payment_preference.rail`, and:
  1. normalises the request exactly as the Reap route's cart-link lane does and looks the
     idempotency key up FIRST, in the one key table both rails share: a retry replays the original
     purchase on its original rail, whatever the router would answer today;
  2. asks services/payment_orchestration/router.py which rail takes a new request;
  3. Reap: hands the request to the Reap route unchanged (its gates, its body). A2A: opens the
     purchase here, through the same checked pieces the Reap cart-link lane uses (consent, the
     merchant's disabled row, the Tier B verdict, OUR catalog price, the attribution cart link), plus
     the A2A rail's own amount cap. It makes NO rail call: the A2A worker does that later.
An explicit `payment_preference.rail` the router cannot honour is the same 404 as a dark rail: it is
never sent to the other rail, and the reason is logged, not returned.

OWNERSHIP IS THE RAIL'S AND THE LEDGER'S, IN SQL. The parent read conjuncts on the agent and the
end user (db/agent_purchase_ledger.py), and the nested detail is read through the Reap route's own
owner-scoped `_owner_view`. "Not yours" and "does not exist" are the same 404.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Literal, Mapping, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

import db.a2a_rail_ledger as a2a_ledger
import db.agent_purchase_ledger as purchases
import db.merchant_purchasability as purchasability
import db.reap_agentic_ledger as reap_ledger
import db.tierb_cart_link_eligibility as tierb_eligibility
import routes.agent_commerce_reap as reap_routes
import services.reap_agentic_purchase as reap_svc
from db.buyer_vault import hash_agent_user_ref
from db.database import IS_POSTGRES, database
from routes.agent_auth import AgentContext, get_agent_context
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from services.commerce_attribution_service import IssuedClick, issue_click, new_click_id
from services.outbound_links_service import build_shopify_cart_permalink
from services.payment_orchestration import a2a_purchase_view, purchase_view
from services.payment_orchestration import router as rail_router
from services.payment_orchestration.rails import A2A_STATE_MAP, RAIL_ID_PREFIX

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/v2/commerce/purchases", tags=["agent-commerce-purchases"])

#: The exact id shapes this route reads: our `pp_` id and each rail's own id (all `<prefix><24 hex>`, see
#: db/agent_purchase_ledger.new_purchase_id and db/reap_agentic_ledger.new_purchase_id). Anything else is
#: the same 404 as an unknown id, decided BEFORE a bind: on Postgres a NUL byte in a bound string raises
#: out of asyncpg as a 500.
_PURCHASE_ID_RE = re.compile(r"(?:pp_|" + "|".join(re.escape(p) for p in RAIL_ID_PREFIX) + r")[0-9a-f]{24}")


class _LedgerUnavailable(Exception):
    """The agent_purchases table could not be read (missing, or the database refused)."""


def _ledger_unavailable() -> JSONResponse:
    return JSONResponse(status_code=503, content={"error": "ledger_unavailable"})


def _dark() -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_available"})


def _owner_hash(agent_user: Optional[AgentUserContext]) -> str:
    agent_user_ref = reap_routes._require_agent_user(agent_user)
    agent_user_ref_hash = hash_agent_user_ref(agent_user_ref)
    if not agent_user_ref_hash:
        raise reap_svc.PurchaseRefused("agent_user_required")
    return agent_user_ref_hash


async def _parent_for(purchase_id: str, agent_id: str, owner: str) -> Optional[Dict[str, Any]]:
    """The owner's parent row for a `pp_` or rail id, healing a rail purchase that has none yet.

    The heal runs only after the rail ledger's own owner-scoped read has said the purchase is this
    caller's, so it can never create a parent for somebody else's purchase.
    """
    try:
        if purchase_id.startswith("pp_"):
            return await purchases.get_for_owner(purchase_id, agent_id, owner)
        rail = next((r for prefix, r in RAIL_ID_PREFIX.items() if purchase_id.startswith(prefix)), None)
        if rail not in ("reap", "a2a"):
            return None
        parent = await purchases.get_by_rail_id_for_owner(rail, purchase_id, agent_id, owner)
        if parent is not None:
            return parent
        if rail == "reap":
            if await reap_ledger.get_purchase_for_owner(purchase_id, agent_id, owner) is None:
                return None
            await purchases.ensure_reap_parent(purchase_id, basis="backfill")
        else:
            if await a2a_ledger.get_purchase_for_owner(purchase_id, agent_id, owner) is None:
                return None
            await purchases.ensure_a2a_parent(purchase_id, basis="backfill")
        return await purchases.get_by_rail_id_for_owner(rail, purchase_id, agent_id, owner)
    except Exception as exc:  # noqa: BLE001 - any ledger fault is "say nothing, retry", never a 500
        logger.warning("agent_purchases: ledger read failed error_type=%s", type(exc).__name__)
        raise _LedgerUnavailable() from None


async def _unified_for(parent: Mapping[str, Any], agent_id: str, owner: str):
    """(body, None) on success, (None, response) when the purchase cannot be answered."""
    body, problem = await purchase_view.unified_purchase(
        parent, agent_id=agent_id, agent_user_ref_hash=owner
    )
    if problem == "not_found":
        return None, reap_routes._not_found()
    if problem == "state_unmapped":
        logger.warning(
            "agent_purchases: unmapped rail state rail=%s purchase=%s", parent["rail"], parent["id"]
        )
        return None, JSONResponse(status_code=503, content={"error": "state_unmapped"})
    return body, None


@router.get("/{purchase_id}")
async def get_purchase(
    purchase_id: str,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    if not purchases.is_enabled():
        return _dark()
    try:
        owner = _owner_hash(agent_user)
    except reap_svc.PurchaseRefused as exc:
        return reap_routes._refused(exc)

    purchase_id = str(purchase_id or "").strip()
    if not _PURCHASE_ID_RE.fullmatch(purchase_id):
        return reap_routes._not_found()
    agent_id = str(context.agent_id)
    try:
        parent = await _parent_for(purchase_id, agent_id, owner)
    except _LedgerUnavailable:
        return _ledger_unavailable()
    if parent is None:
        return reap_routes._not_found()
    body, error = await _unified_for(parent, agent_id, owner)
    return error if error is not None else body


@router.get("")
async def list_purchases(
    request: Request,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    if not purchases.is_enabled():
        return _dark()
    try:
        owner = _owner_hash(agent_user)
        limit = reap_routes._limit_from(request)
    except reap_svc.PurchaseRefused as exc:
        return reap_routes._refused(exc)

    agent_id = str(context.agent_id)
    # This owner's Reap purchases that predate the ledger (or whose post-commit write failed)
    # get their parent now, so the first page is complete. A failure here only means the page
    # may miss rows the backfill will add; it never fails the read.
    for heal in (purchases.heal_reap_parents_for_owner, purchases.heal_a2a_parents_for_owner):
        try:
            await heal(agent_id, owner)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_purchases: owner heal failed error_type=%s", type(exc).__name__)

    try:
        parents = await purchases.list_for_owner(agent_id, owner, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_purchases: ledger list failed error_type=%s", type(exc).__name__)
        return _ledger_unavailable()

    # ONE ANSWER FOR THE WHOLE PAGE. A row whose rail state has no unified word fails the page with the
    # same 503 the single read gives, rather than vanishing from the owner's history (a caller
    # reconciling history would read a missing row as a purchase that does not exist).
    out = []
    for parent in parents:
        body, error = await _unified_for(parent, agent_id, owner)
        if body is not None:
            out.append(body)
        elif error is not None and error.status_code == 503:
            return error
        else:
            # The rail row is gone or no longer this owner's, which the copied identity makes impossible;
            # say so in the log and leave it out rather than fail the page.
            logger.warning("agent_purchases: parent without readable rail row purchase=%s", parent["id"])
    return {"purchases": out, "limit": limit}


# ── create ───────────────────────────────────────────────────────────────────────────────────


class PaymentPreference(BaseModel):
    rail: Optional[Literal["reap", "a2a"]] = None


class CreatePurchaseRequest(reap_routes.StartPurchaseRequest):
    """The Reap route's create body, plus the caller's rail choice (optional). The Reap route parses
    the same body when a request is handed to it; it ignores `payment_preference`."""

    payment_preference: Optional[PaymentPreference] = None


class _LostKeyRace(Exception):
    """Another request with this key won while ours was being written: roll ours back."""

    def __init__(self, winner: str):
        super().__init__(winner)
        self.winner = winner


_A2A_CLICK_SURFACE = "a2a_cart_link"
_RECORD_PARENT_TIMEOUT_S = 1.0


async def _a2a_accepted(purchase_id: str, agent_id: str, owner: str) -> JSONResponse:
    view = await a2a_purchase_view.owner_view(purchase_id=purchase_id, agent_id=agent_id, agent_user_ref_hash=owner)
    if view is None:
        return reap_routes._not_found()
    # The unified read's words (`state` is the shared vocabulary, `rail_state` the rail's own), so a
    # caller sees one shape on create and on GET. A request handed to Reap answers in Reap's shape.
    return JSONResponse(status_code=202, content={
        "purchase_id": purchase_id,
        "rail": "a2a",
        "state": A2A_STATE_MAP.get(str(view.get("state") or "")),
        "rail_state": view.get("state"),
        "poll_after_seconds": view.get("poll_after_seconds"),
        "variant_title": view.get("variant_title"),
    })


@router.post("")
async def create_purchase(
    request: Request,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    """Open a purchase on the rail the router picks. MAKES NO RAIL CALL."""
    # Both dials: a purchase this door opens is read through the unified ledger (an A2A purchase has no
    # other read), so the door is dark while the ledger is.
    if not rail_router.is_enabled() or not purchases.is_enabled():
        return _dark()
    try:
        agent_user_ref = reap_routes._require_agent_user(agent_user)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - the parser's message can quote the body
            raise reap_svc.PurchaseRefused("invalid_request", "the request body is not JSON")
        if not isinstance(payload, dict):
            raise reap_svc.PurchaseRefused("invalid_request", "the request body must be an object")
        try:
            req = CreatePurchaseRequest.model_validate(payload)
        except ValidationError:
            # The pydantic error can quote the buyer's address: never forwarded, never logged.
            raise reap_svc.PurchaseRefused("invalid_request", "the request body did not validate")

        # THE REAP ROUTE'S NORMALISATION, IN ITS ORDER, so the fingerprint below is byte-for-byte the
        # one the Reap route computes for the same body: one key is one attempt on either rail.
        consent_version = reap_routes._consent_version(req.buyer.consent_version)
        offer_code = reap_routes._offer_code(req.offer_code)
        merchant_host = reap_routes._identifier(req.merchant_domain, "merchant_domain", max_chars=255).lower()
        merchant_domain = reap_routes._merchant_domain_key(merchant_host)
        product_key = reap_routes._identifier(req.product_key, "product_key", max_chars=1024)
        variant_key, hashed_variant = reap_routes._variant_selector(req)
        market_country = purchasability.normalize_market(req.buyer.shipping_address.country)
        if market_country is None:
            raise reap_svc.PurchaseRefused("invalid_address", "country must be two letters")
        agent_id = str(context.agent_id)
        owner = hash_agent_user_ref(agent_user_ref)
        if not owner:
            raise reap_svc.PurchaseRefused("agent_user_required")
        idempotency_key = reap_routes._identifier(req.idempotency_key, "idempotency_key", max_chars=128)
        shipping_address = reap_routes._buyer_address_for_client(req.buyer)
        buyer_email = str(req.buyer.email or "").strip()
        return_url = str(req.return_url or "").strip() or reap_routes._default_return_url()
        request_hash = reap_routes._request_hash(
            merchant_domain=merchant_domain, product_key=product_key, variant_key=hashed_variant,
            quantity=int(req.quantity), email=buyer_email, shipping_address=shipping_address,
            return_url=return_url, item_source=req.item_source, offer_code=offer_code,
            expected_unit_price_minor=req.expected_unit_price_minor, expected_currency=req.expected_currency,
        )

        # THE KEY FIRST. Raises `idempotency_conflict` when this key was used for a different body.
        replayed = await reap_routes._replayed_purchase_id(
            agent_id=agent_id, agent_user_ref_hash=owner, idempotency_key=idempotency_key, request_hash=request_hash)
        if replayed and replayed.startswith("rp_"):
            # The original is a Reap purchase: the Reap route replays it (and records the consent).
            return await reap_routes.start_reap_purchase(request, context, agent_user)
        if replayed:
            return await _a2a_accepted(replayed, agent_id, owner)

        if req.expected_unit_price_minor is None:
            # A new attempt binds the money the buyer was shown (the Reap route's rule). Refused BEFORE
            # the router, so its answer cannot be told apart by whether the merchant is on a rail's list.
            raise reap_svc.PurchaseRefused(
                "invalid_request", "expected_unit_price_minor and expected_currency are required")
        choice = rail_router.choose_rail(
            agent_id=agent_id, merchant_domain=merchant_domain, market_country=market_country,
            item_source=req.item_source, has_offer_code=offer_code is not None,
            preferred=req.payment_preference.rail if req.payment_preference else None,
        )
        if choice.rail == "reap":
            return await reap_routes.start_reap_purchase(request, context, agent_user)
        if choice.rail is None:
            # WARNING, not INFO: prod drops module-logger INFO, and an operator needs to see why.
            logger.warning("agent_purchases: explicit rail refused agent=%s reason=%s", agent_id, choice.refusal)
            raise reap_svc.PurchaseRefused("not_available_on_this_rail")
        try:
            return await _open_a2a_purchase(
                req=req, agent_id=agent_id, owner=owner, merchant_host=merchant_host, merchant_domain=merchant_domain,
            product_key=product_key, variant_key=variant_key, market_country=market_country,
            consent_version=consent_version, buyer_email=buyer_email, shipping_address=shipping_address,
                idempotency_key=idempotency_key, request_hash=request_hash, choice=choice,
            )
        except _LostKeyRace:
            # A concurrent request with this key opened a Reap purchase first: Reap replays it.
            return await reap_routes.start_reap_purchase(request, context, agent_user)
    except reap_routes._PurchasePersistenceUnavailable:
        return JSONResponse(status_code=503, content={"error": "checkout_outcome_unknown"})
    except reap_svc.PurchaseRefused as exc:
        return reap_routes._refused(exc)


async def _open_a2a_purchase(
    *, req: CreatePurchaseRequest, agent_id: str, owner: str, merchant_host: str, merchant_domain: str,
    product_key: str, variant_key: Optional[str], market_country: str, consent_version: str, buyer_email: str,
    shipping_address: Mapping[str, Any], idempotency_key: str, request_hash: str, choice: "rail_router.RailChoice",
) -> JSONResponse:
    """The A2A rail's create: every check the Reap cart-link lane makes, in its order, then one durable
    unit (the purchase and its key). Refusals raise `PurchaseRefused`; nothing is written before them."""
    # The Reap rail's own buyer admission (pure): a real email, printable text, the client's required
    # address fields. Its cleaned values are what is stored and later sent to the rail.
    buyer_email, shipping_address = reap_svc._validated_buyer(
        reap_svc.BuyerContact(email=buyer_email, shipping_address=dict(shipping_address)))
    # The same purchasability gate as the Reap route (dark unless enforced; fails closed).
    if purchasability.is_enforcement_enabled():
        if not await purchasability.is_purchasable(merchant_domain, market_country):
            raise reap_svc.PurchaseRefused(
                "merchant_not_purchasable", "no fresh positive purchasability fact for this domain and market")
    # An operator's "off" for this merchant holds on every rail.
    await reap_routes._refuse_if_merchant_disabled(merchant_domain=merchant_domain, market_country=market_country)
    # The cart link must be one the storefront honours for this market (the daily Tier B verdict).
    if not await tierb_eligibility.is_cart_link_eligible(merchant_host, market_country):
        raise reap_svc.PurchaseRefused("merchant_not_eligible")
    if req.variant_id is not None:
        # The buyer's storefront variant, resolved to the catalog's own sku (never a door's spelling).
        variant_key = await reap_routes._resolve_selected_variant_key(
            merchant_domain=merchant_host, product_key=product_key,
            variant_id=req.variant_id, market_country=market_country)
    facts, seller_ref, variant_id, seed_kind = await reap_routes._load_cart_link_item(
        merchant_domain=merchant_host, product_key=product_key, variant_key=variant_key, market_country=market_country)
    # A selected variant is bought as itself or not at all (`variant_id` above is the PROVEN one).
    reap_routes._require_selected_variant(variant_id, req.variant_id)
    unit_minor, currency = facts["our_price_minor"], facts["currency"]
    if type(unit_minor) is not int or unit_minor != req.expected_unit_price_minor or currency != req.expected_currency:
        raise reap_svc.PurchaseRefused("price_changed", "the selected offer money changed")
    if not rail_router.amount_ok(unit_minor * int(req.quantity), currency):
        raise reap_svc.PurchaseRefused("amount_over_limit", "the total is over this rail's limit for its currency")

    # MINTED HERE, NEVER THE CALLER'S: the click that attributes the order, and the link that carries it.
    click_id = new_click_id()
    cart_url = build_shopify_cart_permalink(
        shop_domain=merchant_host, variant_id=variant_id, click_id=click_id,
        quantity=int(req.quantity), country=market_country,
    )
    if cart_url is None:
        raise reap_svc.PurchaseRefused("row_variant_unverified")
    # Fail closed: no click row, no purchase (`issue_click` raises on a database error).
    await issue_click(IssuedClick(
        click_id=click_id, surface=_A2A_CLICK_SURFACE, commerce_surface=_A2A_CLICK_SURFACE,
        source_channel="agent", agent_id=agent_id, merchant_id=seller_ref,
        destination_url=cart_url, dest_domain=merchant_host,
        context={"seller_ref": seller_ref, "seed_kind": seed_kind, "item_source": "cart_link",
                 "product_key": product_key, "variant_key": variant_key, "rail": "a2a"},
    ))

    try:
        async with database.transaction():
            if not IS_POSTGRES:
                # Reserve SQLite's writer before the inserts (the Reap route's reason).
                await database.execute("UPDATE reap_agentic_purchase_keys SET purchase_id=purchase_id WHERE 0=1")
            row = await a2a_ledger.create_purchase(
                agent_id=agent_id, agent_user_ref_hash=owner, merchant_domain=merchant_host, checkout_url=cart_url,
                currency=currency, our_price_minor=unit_minor, quantity=int(req.quantity), product_key=product_key,
                product_name=facts.get("product_name"), variant_title=facts.get("variant_title"),
                consent_version=consent_version, buyer_email=buyer_email, shipping_address=shipping_address,
            )
            winner = await reap_routes._claim_idempotency_key(
                agent_id=agent_id, agent_user_ref_hash=owner, idempotency_key=idempotency_key,
                purchase_id=row["id"], request_hash=request_hash)
            if winner != row["id"]:
                raise _LostKeyRace(winner)
        purchase_id = row["id"]
    except _LostKeyRace as lost:
        # Ours is rolled back with its key write; the winner (on whichever rail) is the answer.
        if lost.winner.startswith("rp_"):
            raise
        return await _a2a_accepted(lost.winner, agent_id, owner)
    except reap_svc.PurchaseRefused:
        raise
    except ValueError:
        # The ledger refused a value it would store (it validates everything): not the caller's to fix.
        logger.warning("agent_purchases: a2a create refused by the ledger agent=%s", agent_id)
        raise reap_svc.PurchaseRefused("row_not_found", "the catalog row cannot be stored for this rail")
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_purchases: a2a atomic create unavailable error_type=%s", type(exc).__name__)
        raise reap_routes._PurchasePersistenceUnavailable() from None

    logger.warning("agent_purchases: opened a2a purchase=%s merchant=%s agent=%s basis=%s",
                purchase_id, merchant_domain, agent_id, choice.basis)
    try:
        # Best effort, bounded: the unified read heals a missing parent from the committed child.
        await asyncio.wait_for(
            purchases.ensure_a2a_parent(purchase_id, basis=choice.basis, dropped=choice.dropped),
            _RECORD_PARENT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_purchases: a2a parent write deferred error_type=%s", type(exc).__name__)
    return await _a2a_accepted(purchase_id, agent_id, owner)
