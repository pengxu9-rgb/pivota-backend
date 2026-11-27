"""`GET /agent/internal/commerce/purchases/{purchase_id}`: the server-to-server purchase read (AgenPas),
and `POST /agent/internal/commerce/purchases/{purchase_id}/consents`: the AgenPas consent write.

The buyer opens a signed AgenPas link on the gateway. They hold no agent key and no JWT, and the gateway
never stores a partner's credentials, so the gateway reads the purchase here, server to server.

  AUTH   `X-Internal-Key` must equal AGENPAS_INTERNAL_READ_KEY, a key DEDICATED to this read: a key that
         reads any agent's purchase must not share scope with the introspect key
         (AGENT_AUTH_INTROSPECT_INTERNAL_KEY), which this route never accepts. Compared in constant time,
         as bytes. Missing on the server, or shorter than INTERNAL_READ_KEY_MIN_LENGTH (32) characters:
         503, never open. Missing or wrong in the request: 401.
  SCOPE  `X-Agenpas-Agent-Id`, the agent id the gateway verified on the signed link. The read is scoped
         by (agent id, purchase id) IN SQL (db/agent_purchase_ledger.get_for_agent): another agent's
         purchase and no purchase are the same 404. NOT by the end user: the gateway's `usr_…` ref is not
         the backend's agent-user hash, so there is nothing to scope by. `X-Agenpas-Agent-User-Ref`, if
         sent, is ignored on purpose.
  BODY   the SAME unified body `GET /agent/v2/commerce/purchases/{id}` returns, built by the same
         builders (services/payment_orchestration/purchase_view, through the public route's
         `_unified_for`), from the parent's own owner identity: no PII, no rail ids, only vetted URLs.
  DIAL   AGENPAS_INTERNAL_READ_ENABLED, default off, read WHEN THE APP IS BUILT (`mount`, called by
         main.py). Off: the route is not registered at all, so every method on the path, and the OpenAPI
         schema, answer exactly as for a path that does not exist. TURNING IT ON NEEDS A RESTART (a new
         revision). The dial is also checked on every request: turned off without a restart, a GET answers
         the unknown-route 404 before the key is looked at (other methods then answer 405 until the restart).

`POST /agent/internal/commerce/purchases/{purchase_id}/shipping-address`: the buyer's own shipping address and
email, from the gateway's form, for an A2A-rail purchase the agent opened without them (the owner's decision: the
agent sends it; if not, AgenPas asks). SAME router, so the same dial (registered only when it is on at build),
the same key and the same (agent id, purchase id) scoping; `X-Agenpas-Agent-User-Ref` is read and ignored.

  BODY   flat JSON: email, firstName, lastName, phone, addressLine1, addressLine2, city, region, postalCode,
         country (the door's names). A part the row already has may be left out; a part that is sent is
         read whole, by the door's rules (services/payment_orchestration/a2a_shipping_address). US only.
  WRITE  a compare-and-swap (db/a2a_rail_ledger.submit_shipping_address): only while the row waits
         (`opened`, a part empty, the deadline not passed) and only into the empty part; the row is then due
         at once, so the worker's next tick goes on.
  CAPTURE and CONSENT, checked after the key, the scoping and the not-found, BEFORE the body is parsed and
         before any write: capture off now (a2a_shipping_address.capture_enabled: a dial turned off while the
         purchase waited) is 409 {"error": "shipping_address_not_collected"}; with no consent row for (a2a, rail
         purchase, shipping_address, epoch 0) it is 409 {"error": "consent_required"}; a failed consent read is a
         503 (fail closed). Nothing is written on any of them. (ErrorHandlerMiddleware re-wraps a 409: the
         gateway reads `detail.error`.)
  ANSWER 200 the unified view (as the read) | 422 {"status": "error", "error": "invalid_shipping_address",
         "field_errors": {field: code}} with codes required / invalid / too_long / unsupported, never a value | 409
         {"error": "shipping_address_conflict"} when the row no longer waits and what it holds differs (the
         SAME contact again is a 200) or it is not an A2A-rail purchase | 404 | 400 agent_id_required | 401 |
         503. Nothing of the body is logged, echoed or put in an exception.

The GET writes nothing, heals no parent (a rail purchase whose parent row is still missing is a 404
here until the events job or a public read heals it) and calls no rail. NOT exempt from the rate limiter
(only the introspect key is, middleware/rate_limiter.py): gateway page reads count as anonymous traffic
and can be throttled (429) under a burst. An exemption or a limit of its own is a follow-up.

CONSENT (services/payment_orchestration/purchase_consent.py has the design). With AGENPAS_CONSENT_GATE on
(default off, read per request), the GET body is gated like every other surface (no rail card / approval
URL before a consent) and, for a state that asks for one, carries a `consent` block:
`{required, step, satisfied, available, terms_version, privacy_version, terms_url, privacy_url,
offer: {id, issued_at}}` (the versions and urls only when configured; the offer only while available and
not yet satisfied). Gate off: no block, the body is byte for byte what it was.

THE WRITE is a SECOND router with its OWN key and its OWN dial, so the read key stays read-only:
  AUTH   `X-Internal-Key` must equal AGENPAS_INTERNAL_CONSENT_KEY (>= 32 characters, and not equal to the
         read key: a write key that is also the read key is no separate key). Missing / short / equal to
         the read key on the server: 503. Missing or wrong in the request (the read key included): 401.
  SCOPE  `X-Agenpas-Agent-Id`, as for the read. A `pp_` id is normalised to its rail id; the consent is
         keyed on (rail, rail purchase id, step, approval epoch) and the rail row is read agent-scoped,
         so the write does not need the parent row (it heals it best effort, after every check passed).
  BODY   exactly `{step, offer_id, issued_at, terms_version, privacy_version}` (strings, <= 1 KiB): the offer
         the GET showed (not exactly that shape: 400 consent_refused). Accepted only when the versions are
         the configured ones (else 409 consent_stale), the purchase is not final and is in that step (else
         409 consent_stale), the offer's HMAC (which binds the agent id too) verifies (else 400
         consent_refused; 409 consent_stale when it verifies only under the previous secret) and it is at least AGENPAS_CONSENT_MIN_DWELL_SECONDS old (else
         409 consent_too_soon) and at most 1800 s (else 409 consent_stale). Configuration or offer secret
         missing: 503 consent_unavailable. Any refusal writes nothing. A repeat is the same one row
         (201 when this call wrote it, 200 when it already existed). Calls no rail.
  DIAL   AGENPAS_CONSENT_WRITE_ENABLED, default off, read at build (`mount`) like the read's dial: off, the
         route is not registered (the OpenAPI schema is unchanged); turned off without a restart, the
         write answers the unknown-route 404 before the key is looked at.

THE PRODUCT IMAGE (AGENPAS_PRODUCT_IMAGE_ENABLED, default off, read per request). On, a GET body, either rail,
carries a top-level `product_image_url` (a clean public https URL) only when the catalog has an image that is
surely of the variant bought (services/payment_orchestration/purchase_product_image: the bought sku's own image,
or the product's for a one-variant product whose purchase names no variant; never a copied hero image).
The gateway fetches it server-side for the page; the buyer's browser never loads a store's host. None, or a
failed read: no field, never a 503. Never in the public route or the webhooks. Off: the body is byte for byte
what it was.

THE CARD (AGENPAS_PAYMENT_CARD_ENABLED, default off, read per request). On, a GET body carries a top-level
`payment_card = {network, last4}` (network the partner's own word, e.g. "VISA", or null):
  * Reap, once the purchase's checkout is bound to a card (awaiting_approval, processing, completed, under the
    dispatch journal: db/reap_agentic_ledger.get_payment_card_for_owner says why that row's enrollment IS the paying
    card); never before the checkout exists;
  * A2A, once the token is minted on the card (awaiting_authorization, paying, completed): the network and last four
    from the rail's own card list, recorded for that card (migration 298; db/a2a_rail_ledger.get_payment_card_for_owner),
    and only while the row still names that card. Never the checkout answer's cardLast4 (the token's).
Never in the public route or the webhooks: only the buyer's page shows it. A failed card read drops the field (the page
shows no card), never the body. Off: the body is byte for byte what it was.

THE CARD CHOICE (AGENPAS_CARD_PICKER_ENABLED, default off, read per request, and only with AGENPAS_CONSENT_GATE on too;
db/a2a_rail_ledger has the design). On,
an A2A-rail purchase whose order is locked waits, before any token is asked for, for the buyer to confirm a card; while
it waits, a GET body carries a top-level
`card_choice = {confirmed, new_card, expires_at, cards: [{handle, network, last4, added_at, selected}]}` (`new_card`: the
buyer asked to add a new card and the worker is about to open the card page): the buyer's ACTIVE cards
as the worker last read them from the rail (newest first; `selected` is the card the purchase will be paid with unless
the buyer picks another). `handle` names a card for THIS purchase only (an HMAC under this router's key, never the
rail's card id). No field
otherwise (another rail, no card list recorded, past the token or the deadline) or when the read fails (never a 503).
Never in the public route or the webhooks.

`POST /agent/internal/commerce/purchases/{purchase_id}/card-choice`: the buyer's confirmation, from the page's Approve, or
their "Add a new card" (migration 301).
SAME router as the read (its dial, key and scoping, like the shipping-address write).
  BODY   exactly `{"handle": "<24 hex>"}`, or exactly `{"new_card": true}`, at most 256 bytes (else 400
         {"error": "invalid_card_choice"}). `new_card` records the request (db/a2a_rail_ledger.request_new_card, the same
         conditions as the confirmation): the worker leaves the wait for the rail's card page, binds the new card, takes
         it as the confirmed card and locks the order again. A repeat is the same answer; a saved card after it, or a
         new card after a saved one was confirmed, is 409 card_choice_closed.
  CONSENT checked before the body is read: no consent row for (a2a, rail purchase, approval, its epoch) is 409
         {"error": "consent_required"} (the page offers the terms first); a failed consent read is a 503.
  WRITE  a compare-and-swap (db/a2a_rail_ledger.confirm_card_choice) on exactly the cards it read just before (a
         handle is fixed for its purchase): sets the card, records the confirmation and makes the row due at once (the
         worker then asks for the token on it).
  ANSWER 200 the unified view with its `card_choice` (confirmed) | 409 {"error": "card_choice_unavailable"} (the dial
         off) | 409 {"error": "consent_required"} | 409 {"error": "card_choice_closed"} (not an A2A purchase, not waiting on a card, or already confirmed
         with another card; the same card again is a 200) | 409 {"error": "card_choice_stale"} (the handle is not one
         of the cards the purchase now offers, the list changed, or the worker holds the purchase this moment:
         re-read and send again) | 400 | 404 | 401 | 503. A 4xx is re-wrapped by ErrorHandlerMiddleware (the
         gateway reads `detail.error`). Calls no rail; nothing of the body is logged.

THE AGENT'S NAME. A GET body, either rail, carries a top-level `agent_display_name` (a string), the page's
"Requested by" line: the name set by hand in agents.buyer_display_name (migration 297, a gated one-off) when that
column is not NULL (it wins, and never falls through: '' hides a portal name), else the name the purchase's agent
registered with in the developer portal (agents.agent_name) unless db/agents.portal_name_is_blocked (a payment
brand, a bank, "secure", Pivota or AgenPaz in a common lookalike spelling, a non-ASCII Latin letter or a letter of
a script outside the allowlist, Latin mixed with Cyrillic or Greek, or a placeholder). A rename hides it until checked again (PUT /agents/{agent_id}). Never a fallback (the user's full name, a legacy
`name`, "Agent", "Unknown"). No name, one without a name's shape (db/agents.clean_buyer_display_name, never
repaired) or a failed read: no field, never a 503. No dial. Not in the public route or the webhooks: the agent
knows its own name.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

import db.a2a_rail_ledger as a2a_ledger
import db.agent_purchase_consents as consents_db
import db.agents as agents_db
import db.agent_purchase_ledger as purchases
import db.reap_agentic_ledger as reap_ledger
import services.payment_orchestration.purchase_product_image as product_image
import db.reap_continuation as continuation
from routes import agent_commerce_purchases as public
from services.payment_orchestration import a2a_markets
from services.payment_orchestration import a2a_shipping_address as shipping_capture
from services.payment_orchestration import purchase_consent
from services.payment_orchestration.rails import RAIL_ID_PREFIX

logger = logging.getLogger(__name__)

AGENPAS_INTERNAL_READ_ENABLED_ENV = "AGENPAS_INTERNAL_READ_ENABLED"
#: This route's own key. Not the introspect key: that one is held for other /agent/internal routers.
INTERNAL_KEY_ENV = "AGENPAS_INTERNAL_READ_KEY"
#: A configured key shorter than this counts as missing (503): a short key is a guessable key.
INTERNAL_READ_KEY_MIN_LENGTH = 32
_TRUTHY = frozenset({"1", "true", "on", "yes"})
#: An agent id as the gateway passes it: an opaque id, checked before any bind (a NUL on Postgres raises).
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")


#: The card on the GET body (see THE CARD above). Read per request.
AGENPAS_PAYMENT_CARD_ENABLED_ENV = "AGENPAS_PAYMENT_CARD_ENABLED"
AGENPAS_PRODUCT_IMAGE_ENABLED_ENV = "AGENPAS_PRODUCT_IMAGE_ENABLED"


def is_enabled() -> bool:
    return (os.getenv(AGENPAS_INTERNAL_READ_ENABLED_ENV) or "").strip().lower() in _TRUTHY


def payment_card_enabled() -> bool:
    return (os.getenv(AGENPAS_PAYMENT_CARD_ENABLED_ENV) or "").strip().lower() in _TRUTHY


def product_image_enabled() -> bool:
    return (os.getenv(AGENPAS_PRODUCT_IMAGE_ENABLED_ENV) or "").strip().lower() in _TRUTHY


async def _payment_card(parent: Dict[str, Any], agent_id: str, owner: str) -> Optional[Dict[str, Any]]:
    """The card the purchase pays with, or None (no card yet, another rail, or a failed read: never a 503). Reap: the
    bound checkout's enrollment. A2A: the card the token was minted on, from the rail's own card list (migration 298)."""
    read = {"reap": reap_ledger.get_payment_card_for_owner, "a2a": a2a_ledger.get_payment_card_for_owner}.get(
        str(parent.get("rail") or ""))
    if read is None:
        return None
    try:
        return await read(str(parent["rail_purchase_id"]), agent_id, owner)
    except Exception as exc:  # noqa: BLE001 - the card is decoration: its fault drops it, never the body
        logger.warning("agent_internal_purchases: card read failed error_type=%s", type(exc).__name__)
        return None


async def _product_image(parent: Dict[str, Any], agent_id: str, owner: str) -> Optional[str]:
    """The image URL of what the purchase bought (services/payment_orchestration/purchase_product_image), or
    None: none that is surely the bought variant's, or a failed read (never a 503)."""
    try:
        return await product_image.product_image_for_owner(
            str(parent.get("rail") or ""), str(parent["rail_purchase_id"]), agent_id, owner)
    except Exception as exc:  # noqa: BLE001 - the image is decoration: its fault drops it, never the body
        logger.warning("agent_internal_purchases: product image read failed error_type=%s", type(exc).__name__)
        return None


def _handle_key() -> str:
    """The key card handles are keyed with: this router's own key (the route answered, so it is configured)."""
    return str(os.getenv(INTERNAL_KEY_ENV) or "").strip()


def _card_choice_view(purchase_id: str, choice: Dict[str, Any]) -> Dict[str, Any]:
    expires_at = choice.get("expires_at")
    return {
        "confirmed": bool(choice.get("confirmed")),
        "new_card": bool(choice.get("new_card")),
        "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else None,
        "cards": [
            {"handle": a2a_ledger.card_handle(purchase_id, o["id"], _handle_key()), "network": o.get("network"),
             "last4": o.get("last4"), "added_at": o.get("added_at"), "selected": o["id"] == choice.get("selected")}
            for o in choice["options"]
        ],
    }


async def _card_choice(parent: Dict[str, Any], agent_id: str, owner: str) -> Optional[Dict[str, Any]]:
    """The card choice the purchase offers (see THE CARD CHOICE above), or None: another rail, nothing to choose, or a
    failed read (never a 503)."""
    if str(parent.get("rail") or "") != "a2a":
        return None
    rail_id = str(parent["rail_purchase_id"])
    try:
        choice = await a2a_ledger.get_card_choice_for_owner(rail_id, agent_id, owner)
    except Exception as exc:  # noqa: BLE001 - the choice is the page's: its fault drops it, never the body
        logger.warning("agent_internal_purchases: card choice read failed error_type=%s", type(exc).__name__)
        return None
    return _card_choice_view(rail_id, choice) if choice is not None else None


async def _agent_display_name(agent_id: str) -> Optional[str]:
    """The agent's buyer-facing name (db/agents.get_buyer_display_name), or None: none, a bad shape, a blocked
    portal name, or a failed read (never a 503)."""
    try:
        return await agents_db.get_buyer_display_name(agent_id)
    except Exception as exc:  # noqa: BLE001 - the name is decoration: its fault drops it, never the body
        logger.warning("agent_internal_purchases: agent name read failed error_type=%s", type(exc).__name__)
        return None


def _read_enabled() -> None:
    # Off: exactly what FastAPI answers for a path it does not have.
    if not is_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _require_internal_key(x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key")) -> None:
    expected = str(os.getenv(INTERNAL_KEY_ENV) or "").strip()
    if len(expected) < INTERNAL_READ_KEY_MIN_LENGTH:
        # Never open: a server without the key (or with one too short to count) refuses everyone.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"error": "internal_key_not_configured"})
    provided = str(x_internal_key or "").strip()
    # Compare as BYTES: Starlette decodes headers latin-1, and hmac.compare_digest on str RAISES on
    # non-ASCII (routes/agent_internal_decision_slates.py has the history).
    if not provided or not hmac.compare_digest(provided.encode("utf-8", "surrogateescape"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "unauthorized"})


# The guards are on the ROUTER, not the handler, so a route added here later inherits them
# (tests/test_unauthenticated_admin_ops_routes.py has the history). The dial runs first.
router = APIRouter(
    prefix="/agent/internal/commerce/purchases",
    tags=["agent-internal-commerce-purchases"],
    dependencies=[Depends(_read_enabled), Depends(_require_internal_key)],
)


def mount(app: Any) -> bool:
    """Register the read on `app` only while its dial is on, and the consent write only while ITS dial is
    on (both read now, at build). Returns whether anything was registered. Both dials off: nothing is, so
    the app is exactly what it was without this module."""
    mounted = False
    if is_enabled():
        app.include_router(router)
        mounted = True
    write = purchase_consent.write_enabled()
    if write:
        app.include_router(consent_router)
        mounted = True
    # Recorded either way: `available` is false in this process while the write is not registered here.
    purchase_consent.record_write_mount(write)
    return mounted


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "purchase_not_found"})


def _unavailable() -> JSONResponse:
    """A ledger or view fault: "try again", never a 500 (and never which part failed)."""
    return JSONResponse(status_code=503, content={"error": "temporarily_unavailable"})


async def _parent_for_agent(purchase_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
    if purchase_id.startswith("pp_"):
        return await purchases.get_for_agent(purchase_id, agent_id)
    rail = next((r for prefix, r in RAIL_ID_PREFIX.items() if purchase_id.startswith(prefix)), None)
    if rail is None:
        return None
    return await purchases.get_by_rail_id_for_agent(rail, purchase_id, agent_id)


@router.get("/{purchase_id}")
async def get_purchase_for_gateway(
    purchase_id: str,
    x_agenpas_agent_id: Optional[str] = Header(None, alias="X-Agenpas-Agent-Id"),
    # Read so the contract is visible, and IGNORED for scoping: the gateway's user ref is not the
    # backend's agent-user hash (see the module docstring). Never logged.
    x_agenpas_agent_user_ref: Optional[str] = Header(None, alias="X-Agenpas-Agent-User-Ref"),
):
    del x_agenpas_agent_user_ref
    agent_id = str(x_agenpas_agent_id or "").strip()
    if not _AGENT_ID_RE.match(agent_id):
        return JSONResponse(status_code=400, content={"error": "agent_id_required"})
    purchase_id = str(purchase_id or "").strip()
    if not purchases.is_enabled() or not public._PURCHASE_ID_RE.fullmatch(purchase_id):
        return _not_found()
    try:
        parent = await _parent_for_agent(purchase_id, agent_id)
    except Exception as exc:  # noqa: BLE001 - any ledger fault is "say nothing, retry", never a 500
        logger.warning("agent_internal_purchases: ledger read failed error_type=%s", type(exc).__name__)
        return _unavailable()
    owner = str((parent or {}).get("agent_user_ref_hash") or "")
    if parent is None or not owner:
        return _not_found()
    # The owner hash goes only to the owner-scoped view builders, never into the body.
    parent = {k: v for k, v in parent.items() if k != "agent_user_ref_hash"}
    gated = purchase_consent.gate_on()
    try:
        if not gated:
            body, error = await public._unified_for(parent, agent_id, owner)
        else:
            # ONE consent read, used by the view's gate and by the consent block alike (FAILED on a fault:
            # the page stays withheld and the block says unavailable).
            consents = await purchase_consent.snapshot(agent_id, [(parent["rail"], str(parent["rail_purchase_id"]))])
            body, error = await public._unified_for(parent, agent_id, owner, consents)
            if error is None:
                block = purchase_consent.consent_block(
                    rail=str(parent["rail"]), rail_purchase_id=str(parent["rail_purchase_id"]),
                    unified=str(body.get("state") or ""), consents=consents, agent_id=agent_id,
                    detail=body.get("detail"),
                    # Reap asks the agent for the buyer's contact again first: no consent until it is back.
                    blocked=(body.get("detail") or {}).get("contact_reentry_required") is True)
                if block is not None:
                    body["consent"] = block
    except Exception as exc:  # noqa: BLE001 - a rail read or view fault is "try again", never a 500
        logger.warning("agent_internal_purchases: view failed error_type=%s", type(exc).__name__)
        return _unavailable()
    if error is not None:
        return error
    if payment_card_enabled():
        card = await _payment_card(parent, agent_id, owner)
        if card is not None:
            body["payment_card"] = card
    name = await _agent_display_name(agent_id)
    if name is not None:
        body["agent_display_name"] = name
    if product_image_enabled():
        image_url = await _product_image(parent, agent_id, owner)
        if image_url is not None:
            body["product_image_url"] = image_url
    if a2a_ledger.card_picker_active():
        choice = await _card_choice(parent, agent_id, owner)
        if choice is not None:
            body["card_choice"] = choice
    return body


def _conflict() -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "shipping_address_conflict"})


def _same_contact(row: Dict[str, Any], email: Optional[str], address: Optional[Dict[str, str]]) -> bool:
    """The row holds exactly what was sent (an identical resubmission), every part it needs filled."""
    stored_email = str(row.get("buyer_email") or "")
    stored_address = row.get("shipping_address") if isinstance(row.get("shipping_address"), dict) else None
    if not stored_email or not stored_address:
        return False
    if email is not None and stored_email.lower() != email.lower():
        return False
    return address is None or dict(stored_address) == dict(address)


@router.post("/{purchase_id}/shipping-address")
async def submit_shipping_address_for_gateway(
    purchase_id: str,
    request: Request,
    x_agenpas_agent_id: Optional[str] = Header(None, alias="X-Agenpas-Agent-Id"),
    x_agenpas_agent_user_ref: Optional[str] = Header(None, alias="X-Agenpas-Agent-User-Ref"),
):
    del x_agenpas_agent_user_ref  # ignored for scoping, as on the read; never logged
    agent_id = str(x_agenpas_agent_id or "").strip()
    if not _AGENT_ID_RE.match(agent_id):
        return JSONResponse(status_code=400, content={"error": "agent_id_required"})
    purchase_id = str(purchase_id or "").strip()
    if not purchases.is_enabled() or not public._PURCHASE_ID_RE.fullmatch(purchase_id):
        return _not_found()
    try:
        parent = await _parent_for_agent(purchase_id, agent_id)
        owner = str((parent or {}).get("agent_user_ref_hash") or "")
        if parent is None or not owner:
            return _not_found()
        if parent.get("rail") != "a2a":
            return _conflict()  # only the A2A rail asks the buyer for an address
        rail_id = str(parent["rail_purchase_id"])
        row = await a2a_ledger.get_purchase_for_owner(rail_id, agent_id, owner)
    except Exception as exc:  # noqa: BLE001 - any ledger fault is "say nothing, retry", never a 500
        logger.warning("agent_internal_purchases: shipping address read failed error_type=%s", type(exc).__name__)
        return _unavailable()
    if row is None:
        return _not_found()
    # BOTH CHECKS BEFORE THE BODY IS READ AT ALL (not even parsed), and before any write:
    # 1. Capture must be on now (THE predicate the door, the view and the worker read): a dial turned off
    #    while a purchase waited (the consent gate, the capture dial, a checkout step) ends the collection.
    if not shipping_capture.capture_enabled():
        return JSONResponse(status_code=409, content={"error": shipping_capture.SHIPPING_ADDRESS_NOT_COLLECTED})
    # 2. The form is a consent-gated step (services/payment_orchestration/purchase_consent.py): no address is
    #    written before the buyer accepted on the AgenPas page. THE SAME read and rule as the views' gate
    #    (purchase_consent.snapshot + satisfied), always run: a failed read is FAILED (503, fail closed), and with
    #    the gate off the snapshot is LOOKUP, which `satisfied` never counts (unreachable here: capture needs the
    #    gate, but it fails closed if it ever were).
    consents = await purchase_consent.snapshot(agent_id, [("a2a", rail_id)])
    if consents is purchase_consent.FAILED:
        return _unavailable()  # the read failed: fail closed, 503, nothing written
    if not purchase_consent.satisfied(consents, "a2a", rail_id, purchase_consent.SHIPPING_STEP):
        return JSONResponse(status_code=409, content={"error": "consent_required"})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - the parser's message can quote the body
        body = None
    # THE waiting rule's "missing" (a2a_shipping_address.awaits_contact): an address that lacks a required field
    # is asked for whole, and the ledger replaces exactly that stored value (a compare-and-swap on it).
    merchant = row.get("merchant_domain")
    replaces = shipping_capture.incomplete_address(row.get("shipping_address"), merchant)
    email, address, field_errors = shipping_capture.read_submission(
        body, email_required=not row.get("buyer_email"),
        address_required=shipping_capture.address_missing(row.get("shipping_address"), merchant),
        market=a2a_markets.market_for_currency(row.get("currency")), merchant_domain=merchant)
    if field_errors:
        # The app's "already normalised" error shape (`status` + `error`), which ErrorHandlerMiddleware passes
        # through with its status: a 422 stays a 422 (a FastAPI-default 422 would be re-nested as a 400).
        return JSONResponse(status_code=422, content={
            "status": "error", "error": "invalid_shipping_address", "field_errors": field_errors})
    try:
        written = await a2a_ledger.submit_shipping_address(
            rail_id, agent_id, owner, buyer_email=email, shipping_address=address, replaces_address=replaces)
        if written is None:
            current = await a2a_ledger.get_purchase_for_owner(rail_id, agent_id, owner)
            if current is None:
                return _not_found()
            if not _same_contact(current, email, address):
                return _conflict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_internal_purchases: shipping address write failed error_type=%s", type(exc).__name__)
        return _unavailable()
    parent = {k: v for k, v in parent.items() if k != "agent_user_ref_hash"}
    try:
        view, error = await public._unified_for(parent, agent_id, owner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_internal_purchases: view failed error_type=%s", type(exc).__name__)
        return _unavailable()
    return error if error is not None else view  # 200: the unified view, encoded as the read encodes it


# ── the card choice write ────────────────────────────────────────────────────────────────────

_CARD_CHOICE_BODY_MAX_BYTES = 256


def _card_choice_refusal(error: str) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": error})


async def _confirmed_with(rail_id: str, agent_id: str, owner: str, handle: str) -> bool:
    """The purchase already confirmed THIS card (a repeat that lands after the worker moved on): the same card again
    is the same answer, never a conflict."""
    row = await a2a_ledger.get_purchase_for_owner(rail_id, agent_id, owner)
    method_id = str((row or {}).get("payment_method_id") or "")
    return bool(row and row.get("card_confirmed_at") is not None and method_id
                and a2a_ledger.card_handle(rail_id, method_id, _handle_key()) == handle)


async def _record_new_card(rail_id: str, agent_id: str, owner: str) -> Optional[str]:
    """Record that the buyer asked for a new card. None when it is recorded (now, or by an identical earlier call); else
    the refusal."""
    choice = await a2a_ledger.get_card_choice_for_owner(rail_id, agent_id, owner)
    if choice is None:
        # Nothing to choose any more: an earlier identical request (the worker already left for the card page) is fine.
        row = await a2a_ledger.get_purchase_for_owner(rail_id, agent_id, owner)
        asked = (row is not None and row.get("card_new_requested_at") is not None and row.get("card_confirmed_at") is None
                 and row.get("state") in ("quoting", "needs_payment_method"))
        return None if asked else "card_choice_closed"
    if choice["confirmed"]:
        return "card_choice_closed"
    if choice["new_card"]:
        return None
    if await a2a_ledger.request_new_card(rail_id, agent_id, owner) is not None:
        return None
    current = await a2a_ledger.get_card_choice_for_owner(rail_id, agent_id, owner)
    if current is not None and current["new_card"]:
        return None
    return "card_choice_stale" if current is not None and not current["confirmed"] else "card_choice_closed"


async def _record_card_choice(rail_id: str, agent_id: str, owner: str, handle: str) -> Optional[str]:
    """Record the buyer's card. None when it is recorded (now, or by an identical earlier call); else the refusal."""
    choice = await a2a_ledger.get_card_choice_for_owner(rail_id, agent_id, owner)
    if choice is None:
        # Nothing to choose any more: a repeat of the confirmed card (after the worker moved on) is the same answer.
        return None if await _confirmed_with(rail_id, agent_id, owner, handle) else "card_choice_closed"
    key = _handle_key()
    method_id = next((o["id"] for o in choice["options"] if a2a_ledger.card_handle(rail_id, o["id"], key) == handle), None)
    if method_id is None:
        return "card_choice_stale"
    if choice["confirmed"] or choice["new_card"]:
        return None if choice["confirmed"] and method_id == choice["selected"] else "card_choice_closed"
    written = await a2a_ledger.confirm_card_choice(
        rail_id, agent_id, owner, method_id=method_id, options_text=choice["options_text"])
    if written is not None or await _confirmed_with(rail_id, agent_id, owner, handle):
        return None
    current = await a2a_ledger.get_card_choice_for_owner(rail_id, agent_id, owner)
    # Still waiting: the list changed under the page (re-read). Otherwise it moved on or another card was confirmed.
    return "card_choice_stale" if current is not None and not current["confirmed"] else "card_choice_closed"


@router.post("/{purchase_id}/card-choice")
async def confirm_card_choice_for_gateway(
    purchase_id: str,
    request: Request,
    x_agenpas_agent_id: Optional[str] = Header(None, alias="X-Agenpas-Agent-Id"),
    x_agenpas_agent_user_ref: Optional[str] = Header(None, alias="X-Agenpas-Agent-User-Ref"),
):
    del x_agenpas_agent_user_ref  # ignored for scoping, as on the read; never logged
    agent_id = str(x_agenpas_agent_id or "").strip()
    if not _AGENT_ID_RE.match(agent_id):
        return JSONResponse(status_code=400, content={"error": "agent_id_required"})
    purchase_id = str(purchase_id or "").strip()
    if not purchases.is_enabled() or not public._PURCHASE_ID_RE.fullmatch(purchase_id):
        return _not_found()
    try:
        parent = await _parent_for_agent(purchase_id, agent_id)
    except Exception as exc:  # noqa: BLE001 - any ledger fault is "say nothing, retry", never a 500
        logger.warning("agent_internal_purchases: card choice parent read failed error_type=%s", type(exc).__name__)
        return _unavailable()
    owner = str((parent or {}).get("agent_user_ref_hash") or "")
    if parent is None or not owner:
        return _not_found()
    if not a2a_ledger.card_picker_active():
        return _card_choice_refusal("card_choice_unavailable")
    if parent.get("rail") != "a2a":
        return _card_choice_refusal("card_choice_closed")
    rail_id = str(parent["rail_purchase_id"])
    # The card is part of what the buyer approves: no card is recorded before the buyer accepted the approval step's
    # terms on the AgenPas page. THE SAME read and rule as the views' gate (purchase_consent.snapshot + satisfied), before
    # the body is read: a failed read is a 503 (fail closed), no consent a 409. (The choice runs only with the gate on.)
    consents = await purchase_consent.snapshot(agent_id, [("a2a", rail_id)])
    if consents is purchase_consent.FAILED:
        return _unavailable()
    if not purchase_consent.satisfied(consents, "a2a", rail_id, purchase_consent.STEP_FOR_STATE["locking"]):
        return _card_choice_refusal("consent_required")
    raw = await request.body()
    handle = None
    new_card = False
    if len(raw) <= _CARD_CHOICE_BODY_MAX_BYTES:
        try:
            body = json.loads(raw)
        except Exception:  # noqa: BLE001 - the parser's message can quote the body
            body = None
        if isinstance(body, dict) and set(body) == {"handle"} and a2a_ledger.is_card_handle(body["handle"]):
            handle = body["handle"]
        elif isinstance(body, dict) and set(body) == {"new_card"} and body["new_card"] is True:
            new_card = True
    if handle is None and not new_card:
        return JSONResponse(status_code=400, content={"error": "invalid_card_choice"})
    try:
        if new_card:
            refused = await _record_new_card(rail_id, agent_id, owner)
        else:
            refused = await _record_card_choice(rail_id, agent_id, owner, handle)
        if refused is not None:
            return _card_choice_refusal(refused)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_internal_purchases: card choice write failed error_type=%s", type(exc).__name__)
        return _unavailable()
    parent = {k: v for k, v in parent.items() if k != "agent_user_ref_hash"}
    try:
        view, error = await public._unified_for(parent, agent_id, owner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_internal_purchases: view failed error_type=%s", type(exc).__name__)
        return _unavailable()
    if error is not None:
        return error
    confirmed = await _card_choice(parent, agent_id, owner)
    if confirmed is not None:
        view["card_choice"] = confirmed
    return view  # 200: the unified view, encoded as the read encodes it


# ── the consent write ────────────────────────────────────────────────────────────────────────

#: The write's own key. Never the read key, never the introspect key.
CONSENT_KEY_ENV = purchase_consent.CONSENT_KEY_ENV
INTERNAL_CONSENT_KEY_MIN_LENGTH = purchase_consent.CONSENT_KEY_MIN_LENGTH
_CONSENT_BODY_KEYS = frozenset({"step", "offer_id", "issued_at", "terms_version", "privacy_version"})
_CONSENT_BODY_MAX_BYTES = 1024
_OFFER_ID_RE = re.compile(r"^[0-9a-f]{64}\Z")
#: The Reap states in which a purged contact means "re-enter it first" (routes/agent_commerce_reap).
_REAP_CONTACT_REENTRY_STATES = frozenset({"resolving", "needs_enrollment", "quoting"})


def _write_enabled() -> None:
    # Off: exactly what FastAPI answers for a path it does not have.
    if not purchase_consent.write_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def consent_write_ready() -> bool:
    """Whether a consent POST could succeed now (services/payment_orchestration/purchase_consent.write_ready):
    registered here (`mount` records it), its dial on, its key valid. Offers are made only then."""
    return purchase_consent.write_ready()


def _require_consent_key(x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key")) -> None:
    expected = purchase_consent.configured_consent_key()
    if expected is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"error": "internal_key_not_configured"})
    provided = str(x_internal_key or "").strip()
    if not provided or not hmac.compare_digest(provided.encode("utf-8", "surrogateescape"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "unauthorized"})


consent_router = APIRouter(
    prefix="/agent/internal/commerce/purchases",
    tags=["agent-internal-commerce-purchases"],
    dependencies=[Depends(_write_enabled), Depends(_require_consent_key)],
)


def _refusal(status_code: int, error: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error})


def _consent_body(raw: bytes) -> Optional[Dict[str, str]]:
    """The exact body shape, or None. Never echoes what was sent."""
    if len(raw) > _CONSENT_BODY_MAX_BYTES:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or set(body) != _CONSENT_BODY_KEYS:
        return None
    if not all(isinstance(v, str) for v in body.values()):
        return None
    if (body["step"] not in consents_db.STEPS or not _OFFER_ID_RE.match(body["offer_id"])
            or purchase_consent.parse_issued_at(body["issued_at"]) is None
            or not purchase_consent.valid_version(body["terms_version"])
            or not purchase_consent.valid_version(body["privacy_version"])):
        return None
    return body


async def _consent_target(purchase_id: str, agent_id: str) -> Optional[Tuple[str, str, Dict[str, Any], Optional[str]]]:
    """(rail, rail purchase id, the agent's rail row, the parent id if known), or None."""
    parent_id: Optional[str] = None
    if purchase_id.startswith("pp_"):
        parent = await purchases.get_for_agent(purchase_id, agent_id)
        if parent is None:
            return None
        rail, rail_id, parent_id = str(parent["rail"]), str(parent["rail_purchase_id"]), str(parent["id"])
    else:
        rail = next((r for prefix, r in RAIL_ID_PREFIX.items() if purchase_id.startswith(prefix)), None)
        if rail is None:
            return None
        rail_id = purchase_id
    row = await consents_db.rail_purchase_for_agent(rail, rail_id, agent_id)
    return (rail, rail_id, row, parent_id) if row is not None else None


async def _healed_parent_id(rail: str, rail_id: str, agent_id: str) -> Optional[str]:
    """The parent's id, writing the parent if it is missing. Best effort: None on any fault."""
    try:
        parent = await purchases.get_by_rail_id_for_agent(rail, rail_id, agent_id)
        if parent is None:
            heal = purchases.ensure_reap_parent if rail == "reap" else purchases.ensure_a2a_parent
            await heal(rail_id, basis="backfill")
            parent = await purchases.get_by_rail_id_for_agent(rail, rail_id, agent_id)
        return str(parent["id"]) if parent else None
    except Exception as exc:  # noqa: BLE001 - the consent does not need its parent
        logger.warning("agent_internal_purchases: consent parent heal failed error_type=%s", type(exc).__name__)
        return None


@consent_router.post("/{purchase_id}/consents")
async def record_purchase_consent(
    purchase_id: str,
    request: Request,
    x_agenpas_agent_id: Optional[str] = Header(None, alias="X-Agenpas-Agent-Id"),
):
    agent_id = str(x_agenpas_agent_id or "").strip()
    if not _AGENT_ID_RE.match(agent_id):
        return JSONResponse(status_code=400, content={"error": "agent_id_required"})
    purchase_id = str(purchase_id or "").strip()
    if not purchases.is_enabled() or not public._PURCHASE_ID_RE.fullmatch(purchase_id):
        return _not_found()
    body = _consent_body(await request.body())
    if body is None:
        return _refusal(400, "consent_refused")
    cfg = purchase_consent.config()
    if cfg is None:
        return _refusal(503, "consent_unavailable")
    # The backend, not the browser, says which version is current.
    if (body["terms_version"], body["privacy_version"]) != (cfg.terms_version, cfg.privacy_version):
        return _refusal(409, "consent_stale")
    try:
        target = await _consent_target(purchase_id, agent_id)
    except Exception as exc:  # noqa: BLE001 - any ledger fault is "say nothing, retry", never a 500
        logger.warning("agent_internal_purchases: consent target read failed error_type=%s", type(exc).__name__)
        return _unavailable()
    if target is None:
        return _not_found()
    rail, rail_id, row, parent_id = target
    step = body["step"]
    # Not final and in this step, in one check: a final (or unmapped) state asks for no step at all. The A2A
    # shipping-address step: while the row is `opened` and still waits for the buyer's contact (capture on).
    awaits_contact = rail == "a2a" and shipping_capture.awaits_contact(row)  # THE rule the read applies too
    if purchase_consent.step_for(rail, row.get("state"), awaits_contact=bool(awaits_contact)) != step:
        return _refusal(409, "consent_stale")
    # Reap asks the agent for the buyer's contact again first (the read shows no offer then): the same rule
    # as routes/agent_commerce_reap._owner_public_body's `contact_reentry_required`.
    if rail == "reap" and row.get("state") in _REAP_CONTACT_REENTRY_STATES and continuation.contact_required(row):
        return _refusal(409, "consent_stale")
    epoch = purchase_consent.approval_epoch(rail, step)
    fields = dict(agent_id=agent_id, rail=rail, rail_purchase_id=rail_id, step=step, epoch=epoch,
                  issued_at=body["issued_at"])
    if not purchase_consent.offer_matches(cfg, body["offer_id"], **fields):
        # Minted under the secret before the last rotation: well-formed and ours, just old. The page re-reads.
        previous = purchase_consent.previous_config(cfg)
        if previous is not None and purchase_consent.offer_matches(previous, body["offer_id"], **fields):
            return _refusal(409, "consent_stale")
        return _refusal(400, "consent_refused")
    issued = purchase_consent.parse_issued_at(body["issued_at"])
    age = (datetime.now(timezone.utc) - issued).total_seconds()
    if age < purchase_consent.min_dwell_seconds():
        return _refusal(409, "consent_too_soon")
    if age > purchase_consent.MAX_OFFER_AGE_SECONDS:
        return _refusal(409, "consent_stale")
    if parent_id is None:
        parent_id = await _healed_parent_id(rail, rail_id, agent_id)
    try:
        record, created = await consents_db.record(
            rail=rail, rail_purchase_id=rail_id, agent_id=agent_id, step=step, approval_epoch=epoch,
            terms_version=cfg.terms_version, privacy_version=cfg.privacy_version, terms_url=cfg.terms_url,
            privacy_url=cfg.privacy_url, offer_issued_at=issued, purchase_id=parent_id,
            rail_buyer_ref=(str(row["rail_buyer_ref"]) if step == "card_binding" and row.get("rail_buyer_ref") else None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_internal_purchases: consent write failed error_type=%s", type(exc).__name__)
        return _unavailable()
    # WARNING, not INFO: prod drops module-logger INFO, and this is the audit line.
    logger.warning("agent_internal_purchases: consent recorded rail=%s purchase=%s step=%s epoch=%s created=%s",
                   rail, rail_id, step, epoch, created)
    return JSONResponse(status_code=201 if created else 200, content={
        "created": created,
        "consent": {
            "step": record["step"], "approval_epoch": record["approval_epoch"],
            "terms_version": record["terms_version"], "privacy_version": record["privacy_version"],
            "evidence": record["evidence"], "accepted_at": record["accepted_at"].isoformat(),
        },
    })
