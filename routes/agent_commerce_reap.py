"""The agent-facing door onto the Reap agentic purchase rail (WP4).

Three routes over the machinery WP1–WP3 built. This module owns NO state and makes NO partner
call: it decides whether a purchase may be opened, builds the one `PurchaseRow` that
`services.reap_agentic_purchase.start_purchase` will trust, and reads rows back to the buyer they
belong to. Everything that talks to Reap happens later, in the poller, on another process.

── WHAT 404 MEANS HERE ──────────────────────────────────────────────────────────────────────

While `REAP_AGENTIC_ENABLED` is off or the client has no credentials, EVERY route on this router
answers **404**, not 503. That is a deliberate lie about existence and it is the right one: the
agent door's job on receiving it is to fall back to another rail, and a 503 reads as "this rail
is the answer, try again shortly" — which would make an unarmed rail look like an outage and
stall a buyer behind it. The rail is dark by default, so 404 is also the honest description of
production today.

The gate is the FIRST statement of each of the three handlers rather than a router-level
dependency, and that costs one ordering property: an unauthenticated caller gets 401 from
`get_agent_context` before it can learn the route is dark. Three reasons it is still here:

  * a router-level dependency makes all three gates ONE mutation. The mutant table for this PR
    kills the dial check on POST, on GET and on the list separately, and it can only do that if
    they are separate statements. A guard nothing can kill on its own is a guard nothing checks.
  * the caller that matters is the door, which is always authenticated. It sees 404.
  * the gate is not duplicated. There is exactly one check per route — no router-level copy —
    because a second, unreachable copy reads as protection that does not exist.

Body validation runs INSIDE the handler (`payload: Dict[str, Any] = Body(...)` then
`model_validate`) rather than in the signature, for the same reason: a pydantic model in the
signature is validated by FastAPI BEFORE the handler body, so a malformed body on a dark rail
would answer 422 and tell the caller the route is there.

── WHAT THIS ROUTE REFUSES TO TRUST ─────────────────────────────────────────────────────────

**The price is never the caller's.** The request names a product; it does not name a price, and
if it did this module would ignore it. `_load_catalog_row` reads OUR catalog and the number it
finds is the number `start_purchase` stores as `our_price_minor`, which is the number
`verify_quote` multiplies by the quantity and compares against Reap's subtotal, exactly, with no
tolerance. A caller-supplied price would make that whole check a comparison of the caller's claim
against itself.

**The merchant is never the caller's either.** `reap_agentic_eligibility` is an ALLOWLIST: a
domain with no enabled row for the buyer's market refuses `merchant_not_eligible`. There is no
"unknown means allowed" arm anywhere below.

**The buyer is never the caller's.** The purchase is opened for the buyer that
`buyer_identity_links` maps `(agent_id, hash(agent_user_ref))` to, and for nobody else. An agent
cannot name a buyer in the body; there is no field for it.

── THE THREE IDENTIFIERS, AND WHY REAP GETS THE THIRD ───────────────────────────────────────

    buyer_id                the global buyer. Joins to orders, addresses, users. NEVER leaves.
    agent_user_ref_hash     the ownership conjunct on the purchase row. NEVER leaves.
    reap_buyer_ref          opaque, per-buyer, minted here. This is `owner.id` at Reap.

`reap_agentic_buyer_refs` is the map, and it is a THIRD identifier rather than a reuse of
`buyer_agent_links.agent_scoped_buyer_ref` because an enrollment is a CARD: scoping the ref to an
agent would give one buyer two refs, two enrollments and two cards for what is one card, and
migration 224's "at most one active enrollment per buyer_ref" would hold twice instead of once.
Neither identifier appears in any response this module writes — see `_public_body`, which builds
from the ledger's allowlist rather than from the row.

── PII ──────────────────────────────────────────────────────────────────────────────────────

The buyer's email and address enter through the request body, are handed to `start_purchase`,
and are never logged, never echoed in an error, and never in a response. `PurchaseRefused.reason`
is our vocabulary and carries no request value; the partner's text never reaches a caller at all
because this module never speaks to the partner. The one exception carrying partner-adjacent
prose is `rc.ReapRequestError` out of `build_shipping_address`, whose message names MISSING KEYS
and no values — and it is mapped to a bare reason code below rather than forwarded.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

import db.reap_agentic_ledger as ledger
import services.reap_agentic_client as rc
import services.reap_agentic_purchase as svc
from db.buyer_vault import hash_agent_user_ref, mint_pairwise_buyer_ref
from db.database import database
from routes.agent_auth import AgentContext, get_agent_context
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from services.commerce_attribution_service import new_click_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/v2/commerce/reap", tags=["agent-commerce-reap"])


# ── refusals ─────────────────────────────────────────────────────────────────────────────────

#: Every refusal this router can answer, and the status it answers with. `svc.PurchaseRefused` is
#: the ONE exception type on this path — the route raises it for its own refusals too, not just
#: the ones `start_purchase` raises — so each handler has one `except` and one place where a
#: reason becomes a status.
#:
#: THE DEFAULT IS 409, NOT 500 AND NOT 422. A reason this map has not heard of is still a refusal
#: we chose to make; answering 500 would page somebody for a decision the code made on purpose,
#: and answering 422 would tell the caller to edit a request that may be perfectly well-formed.
_REFUSAL_STATUS: Dict[str, int] = {
    # The rail is not here. Indistinguishable from the dial being off, deliberately: both mean
    # "fall back", and a caller that could tell them apart would learn our configuration.
    "not_available_on_this_rail": 404,
    "rail_disabled": 404,
    "rail_unconfigured": 404,
    # The caller is not authenticated as a buyer. 401 and not 403, because 403 would assert that
    # we know who this buyer is and are refusing them — we do not know, there is no token. It
    # also matches `get_agent_user_identity`, which already answers 401 for a token that is
    # present and bad; a token that is absent is the same failure one step earlier.
    "agent_user_required": 401,
    # The request is wrong and the caller can fix it by editing the request.
    #
    # 400 AND NOT 422, WHICH IS WHAT THE PLAN FOR THIS WORK ASKED FOR. This app CANNOT EMIT A
    # 422: `middleware/error_handler.ErrorHandlerMiddleware` normalises every 4xx JSON response
    # app-wide, and its `status_code == 422` branch rewrites the status to **400** and replaces
    # the body's details with a `validation_errors` list. A route that answered 422 would
    # therefore arrive at the caller as a 400 whose reason code had been thrown away — the worst
    # of both. Answering 400 outright keeps the status the route chose and keeps the reason where
    # the contract page says it is. See docs/reap_agentic_routes.md for the envelope the
    # middleware wraps these in.
    "invalid_request": 400,
    "invalid_address": 400,
    "invalid_return_url": 400,
    "currency_unsupported": 400,
    # The request is well-formed; the world does not permit it. Editing the body will not help.
    "merchant_not_eligible": 409,
    "buyer_unlinked": 409,
    "row_not_found": 409,
    "row_unpriced": 409,
    "row_not_shopify": 409,
}

_DEFAULT_REFUSAL_STATUS = 409


def _refused(exc: svc.PurchaseRefused) -> JSONResponse:
    """A refusal on the wire: `{"error": "<reason>"}` and nothing else.

    NOT `HTTPException`, whose body would be `{"detail": {...}}`. The gateway team codes against
    docs/reap_agentic_routes.md and this is the shape that page promises; an envelope is a
    contract, not a formatting preference. `str(exc)` is NOT used — it carries the detail, which
    names fields and bounds and is for our logs, not for a caller.
    """
    reason = str(getattr(exc, "reason", "") or "invalid_request")
    return JSONResponse(
        status_code=_REFUSAL_STATUS.get(reason, _DEFAULT_REFUSAL_STATUS),
        content={"error": reason},
    )


def _rail_is_live() -> bool:
    """Both halves, read at call time. `is_enabled()` is the operator's dial; `is_configured()`
    is whether we have anywhere to send a request. A rail with no credentials is as absent as one
    that is switched off, and `start_purchase` refuses both, so the door is told the same thing
    either way."""
    return svc.is_enabled() and rc.is_configured()


def _require_rail() -> None:
    if not _rail_is_live():
        raise svc.PurchaseRefused("not_available_on_this_rail")


def _require_agent_user(agent_user: Optional[AgentUserContext]) -> str:
    """The end-user ref, or a refusal. A purchase needs a BUYER — there is nobody to enroll a
    card for, nobody to own the row, and nobody to scope a later read to."""
    ref = str(getattr(agent_user, "agent_user_ref", "") or "").strip() if agent_user else ""
    if not ref:
        raise svc.PurchaseRefused("agent_user_required")
    return ref


# ── the request body ─────────────────────────────────────────────────────────────────────────


class ReapShippingAddress(BaseModel):
    """THE CLIENT'S FIELD NAMES, on purpose, and not the snake_case shape
    `routes/agent_commerce.CommerceShippingAddress` uses.

    `BuyerContact.shipping_address` is handed straight to `rc.build_shipping_address`, which
    whitelists `firstName / lastName / phone / addressLine1 / city / country` as REQUIRED and
    `addressLine2 / region / postalCode` as optional. Accepting a second spelling here would put
    a translation table between the wire and the validator — a second contract to keep in step
    with a partner's spec, and the first place a field would be silently dropped when the spec
    moves. One name per field, all the way through.

    `phone` is required because the client requires it. The brief for this work called it
    optional; the code it has to satisfy does not, and `build_shipping_address` refuses an
    incomplete address rather than sending a partial one.

    Extra keys are dropped by the model rather than refused, and then dropped AGAIN by
    `build_shipping_address` — a caller cannot widen what reaches a third party by adding keys.
    """

    firstName: Optional[str] = None
    lastName: Optional[str] = None
    phone: Optional[str] = None
    addressLine1: str = Field(..., min_length=1)
    city: str = Field(..., min_length=1)
    country: str = Field(..., min_length=2, max_length=2)
    addressLine2: Optional[str] = None
    region: Optional[str] = None
    postalCode: Optional[str] = None


class ReapBuyer(BaseModel):
    """`name` and `phone` are FALLBACKS for the address fields, never overrides.

    They exist because a caller that already holds a contact record should not have to restate
    the name in the address, and they are applied only where the address left the field empty —
    an address that names a recipient always wins, because that is the field that will be on the
    parcel. `name` splits on the LAST space: "Ada Lovelace" -> ("Ada", "Lovelace"). That is a
    guess, which is why it is a fallback and not the primary path.
    """

    email: str = Field(..., min_length=3)
    name: Optional[str] = None
    phone: Optional[str] = None
    shipping_address: ReapShippingAddress


class StartPurchaseRequest(BaseModel):
    merchant_domain: str = Field(..., min_length=1, max_length=255)
    product_key: str = Field(..., min_length=1)
    variant_key: Optional[str] = None
    quantity: int = Field(1, ge=1, le=svc.MAX_QUANTITY)
    buyer: ReapBuyer
    return_url: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    #: Accepted and NOT forwarded anywhere. The click id this rail records is one WE mint (see
    #: `new_click_id` below): a caller-supplied attribution context cannot be trusted to be
    #: unique, and the purchase row has exactly one attribution column. Kept in the schema so the
    #: door can send what it already sends to the other commerce routes without a 422, and so
    #: that the day there is somewhere to put it, the field is already the one callers use.
    click_context: Optional[Dict[str, Any]] = None


def _buyer_address_for_client(buyer: ReapBuyer) -> Dict[str, Any]:
    """The address in the client's own vocabulary, with the two fallbacks applied."""
    address: Dict[str, Any] = {
        key: value
        for key, value in buyer.shipping_address.model_dump().items()
        if str(value or "").strip()
    }
    if not address.get("phone") and str(buyer.phone or "").strip():
        address["phone"] = str(buyer.phone).strip()
    if not address.get("firstName") and not address.get("lastName"):
        whole = str(buyer.name or "").strip()
        if whole:
            first, _, last = whole.rpartition(" ")
            # A single-word name goes in `firstName`, leaving `lastName` missing — which
            # `build_shipping_address` then refuses by name. Guessing a surname would be worse
            # than saying we do not have one.
            address["firstName"] = (first or whole).strip()
            if last and first:
                address["lastName"] = last.strip()
    return address


# ── eligibility ──────────────────────────────────────────────────────────────────────────────

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

#: The sentinel that distinguishes the MERCHANT row from an OVERRIDE row. `''`, not NULL: see the
#: header of db/migrations/226_reap_agentic_routes.sql for why a NULL here behaves differently on
#: the two dialects and a value does not.
_MERCHANT_ROW = ""

_ELIGIBILITY_SQL = """
    SELECT merchant_domain, product_key, variant_key, market_country, enabled,
           accept_variant_labels, also_accept_domains
      FROM reap_agentic_eligibility
     WHERE merchant_domain = :merchant_domain
       AND market_country = :market_country
       AND product_key IN (:merchant_row, :product_key)
"""


def _decode_alias_list(value: Any) -> Tuple[str, ...]:
    """jsonb on Postgres arrives as a `str`; TEXT on SQLite always does. One decode for both.

    Anything that is not a list of non-empty strings decodes to `()`. The ledger applies the real
    bounds (32 entries, 128 characters, hostname shape) and RAISES on a violation, which
    `start_purchase` turns into `invalid_request` — so a malformed operator row refuses the
    purchase rather than silently resolving without the alias it was created to supply.
    """
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except Exception:  # noqa: BLE001
            return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item or "").strip())


class _Eligibility:
    __slots__ = ("market_country", "accept_variant_labels", "also_accept_domains")

    def __init__(
        self,
        market_country: str,
        accept_variant_labels: Tuple[str, ...],
        also_accept_domains: Tuple[str, ...],
    ) -> None:
        self.market_country = market_country
        self.accept_variant_labels = accept_variant_labels
        self.also_accept_domains = also_accept_domains


async def _eligibility(
    *, merchant_domain: str, market_country: str, product_key: str, variant_key: Optional[str]
) -> _Eligibility:
    """The allowlist read. Refuses `merchant_not_eligible` unless an ENABLED merchant row exists
    for exactly this domain in exactly this market.

    THE MARKET IS A CONJUNCT IN THE SQL, not a comparison afterwards. A filter applied to a row
    already loaded is a filter a refactor can drop while every test that reads the returned value
    still passes — the same argument the ledger's header makes about the ownership conjunct, and
    the same stakes: this one is the only thing that keeps the rail domestic.

    An OVERRIDE row (a real `product_key`) contributes aliases and NOTHING ELSE. It cannot make
    an ineligible merchant eligible, and `enabled` is not even read from it: eligibility is a
    property of the merchant in a market, and a per-product exception to that would be a way to
    turn a merchant on without turning it on.
    """
    rows = await database.fetch_all(
        _ELIGIBILITY_SQL,
        {
            "merchant_domain": merchant_domain,
            "market_country": market_country,
            "merchant_row": _MERCHANT_ROW,
            "product_key": product_key,
        },
    )
    merchant_row: Optional[Dict[str, Any]] = None
    labels: List[str] = []
    domains: List[str] = []
    for raw in rows:
        row = dict(raw)
        if str(row.get("product_key") or "") == _MERCHANT_ROW:
            merchant_row = row
            continue
        row_variant = str(row.get("variant_key") or "")
        # An override pinned to a variant applies to THAT variant only; one with a blank variant
        # applies to every variant of the product. A pinned row must not leak onto its siblings —
        # an accepted label names one object.
        if row_variant and row_variant != str(variant_key or ""):
            continue
        labels.extend(_decode_alias_list(row.get("accept_variant_labels")))
        domains.extend(_decode_alias_list(row.get("also_accept_domains")))

    if not merchant_row or not bool(merchant_row.get("enabled")):
        raise svc.PurchaseRefused(
            "merchant_not_eligible", "no enabled eligibility row for this domain and market"
        )

    # Order-preserving dedupe: the ledger caps the list at 32 entries and raises past it, and two
    # override rows can legitimately name the same alias.
    return _Eligibility(
        market_country=str(merchant_row.get("market_country") or "").upper(),
        accept_variant_labels=tuple(dict.fromkeys(labels)),
        also_accept_domains=tuple(dict.fromkeys(domains)),
    )


# ── the buyer ────────────────────────────────────────────────────────────────────────────────


async def _buyer_id_for(*, agent_id: str, agent_user_ref_hash: str) -> str:
    """`(agent_id, hash(agent_user_ref))` -> `buyer_id`, or `buyer_unlinked`.

    ── WHY THIS REFUSES INSTEAD OF CREATING A LINK ──────────────────────────────────────────

    The brief left the choice open. The code decides it: the ONLY writer of
    `buyer_identity_links` is `routes/buyer_api._upsert_buyer_identity_link`, and it is reached
    from a BUYER-AUTHENTICATED surface — the buyer signs in, and that is what binds the agent's
    opaque user ref to a real buyer account. `routes/agent_checkout_intents` only READS the link,
    and proceeds happily without one, because there a missing link costs a form PREFILL.

    Here it costs the buyer's identity. The link is what a long-lived card enrollment hangs off:
    minting one from an agent's assertion alone would attach a stored card to a buyer account
    nothing else in the system knows about, and the same human signing in tomorrow would get a
    second account and be asked for their card again. Refusing sends the door to a rail that does
    not need a durable buyer, which is the correct fallback and is what `not eligible` already
    means everywhere else on this path.
    """
    row = await database.fetch_one(
        """
        SELECT buyer_id
          FROM buyer_identity_links
         WHERE agent_id = :agent_id
           AND agent_user_ref_hash = :agent_user_ref_hash
         LIMIT 1
        """,
        {"agent_id": agent_id, "agent_user_ref_hash": agent_user_ref_hash},
    )
    buyer_id = str(dict(row).get("buyer_id") or "").strip() if row else ""
    if not buyer_id:
        raise svc.PurchaseRefused("buyer_unlinked", "no buyer is linked to this agent user")
    return buyer_id


async def _reap_buyer_ref(buyer_id: str) -> str:
    """The opaque `owner.id` for this buyer. Insert-or-read, and race-safe by re-reading.

    Read first, because that is what almost every call does. On a miss, mint and insert; a
    concurrent caller that got there first raises a unique violation on either the primary key or
    `uq_reap_agentic_buyer_refs_ref`, and the answer to that is to READ WHAT THEY WROTE, never to
    retry the mint. Two refs for one buyer would be two enrollments for one card.
    """
    existing = await database.fetch_one(
        "SELECT reap_buyer_ref FROM reap_agentic_buyer_refs WHERE buyer_id = :buyer_id",
        {"buyer_id": buyer_id},
    )
    if existing:
        ref = str(dict(existing).get("reap_buyer_ref") or "").strip()
        if ref:
            return ref

    minted = mint_pairwise_buyer_ref()
    try:
        await database.execute(
            """
            INSERT INTO reap_agentic_buyer_refs (buyer_id, reap_buyer_ref)
            VALUES (:buyer_id, :reap_buyer_ref)
            """,
            {"buyer_id": buyer_id, "reap_buyer_ref": minted},
        )
        return minted
    except Exception:  # noqa: BLE001
        pass

    row = await database.fetch_one(
        "SELECT reap_buyer_ref FROM reap_agentic_buyer_refs WHERE buyer_id = :buyer_id",
        {"buyer_id": buyer_id},
    )
    ref = str(dict(row).get("reap_buyer_ref") or "").strip() if row else ""
    if not ref:
        # The INSERT failed for a reason that was not a lost race. Refusing is the only safe
        # answer: continuing would mean opening a purchase under a ref nothing has stored, and
        # the enrollment it creates would be unreachable from the next purchase.
        raise svc.PurchaseRefused("buyer_unlinked", "could not establish a buyer reference")
    return ref


# ── our catalog row ──────────────────────────────────────────────────────────────────────────

#: WHY THIS QUERY EXISTS RATHER THAN A CALL TO `services.pivot_query_service`.
#:
#: `_fetch_canonical_rows_for_product` / `_for_sku` read the same three tables and would give
#: every field below. They are RECALL readers: they INNER JOIN `catalog_offers`, so a product
#: that exists and has no unsuppressed offer returns nothing at all and is indistinguishable
#: from a product that does not exist — and this route has to tell `row_unpriced` from
#: `row_not_found`, because they send the door to different places. They also carry recall's
#: visibility gates (merchant `indexable`, seller status), which are about whether a row may be
#: BROWSED. Whether it may be BOUGHT is what `reap_agentic_eligibility` answers, by a human,
#: per merchant, and a second unrelated set of gates on top of that allowlist would be a second
#: place to debug a purchase that "should work".
#:
#: What IS copied verbatim is the price precedence —
#: `coalesce(merchant_effective_price, estimated_best_price, list_price)` — because a purchase
#: that priced a row differently from every other surface in the repo would be the worst kind of
#: divergence: silent, and only visible on the charge.
#:
#: `o.market` IS NOT A CONJUNCT. The offer table has a market column and filtering on it looks
#: obviously right; the SG census (market:SG -> 0 rows) is the record of what it actually does
#: to a lane that names a market. Domesticity is enforced where it is decidable — the
#: eligibility row's `market_country` against the buyer's shipping country — and not by a column
#: whose population nobody has measured on this cohort.
#:
#: SUPPRESSION IS A CONJUNCT ON ALL THREE TABLES, both columns each. A suppressed row is one
#: somebody took out of circulation; selling it is the one thing that must not still work.
_PRODUCT_SQL = """
    SELECT p.product_key, p.title AS product_title, p.brand, p.category, p.product_type,
           p.platform, p.source_domain
      FROM catalog_products p
     WHERE p.product_key = :product_key
       AND lower(p.source_domain) = :merchant_domain
       AND p.suppression_reason IS NULL
       AND p.suppressed_at IS NULL
"""

_SKU_BY_KEY_SQL = """
    SELECT s.sku_key, s.title AS variant_title, s.currency AS sku_currency
      FROM catalog_skus s
     WHERE s.sku_key = :variant_key
       AND s.product_key = :product_key
       AND s.suppression_reason IS NULL
       AND s.suppressed_at IS NULL
"""

_SKUS_FOR_PRODUCT_SQL = """
    SELECT s.sku_key, s.title AS variant_title, s.currency AS sku_currency
      FROM catalog_skus s
     WHERE s.product_key = :product_key
       AND s.suppression_reason IS NULL
       AND s.suppressed_at IS NULL
     ORDER BY s.sku_key
     LIMIT 2
"""

#: `CAST(... AS TEXT)` ON THE PRICE, AND IT IS NOT COSMETIC. This is raw SQL, so no SQLAlchemy
#: result processor runs: asyncpg hands a `numeric` back as a `Decimal`, and SQLite hands the same
#: column back as a **binary float**. `major_to_minor` refuses a float OUTRIGHT — deliberately,
#: because 42.50 as a float is 42.4999999999999964 and a charge that inherits that error is wrong
#: in a direction nobody chose. So without the cast this route prices correctly on Postgres and
#: refuses `row_unpriced` for every priced row on SQLite: a dialect split that no amount of
#: SQLite testing would reveal as anything but "the fixture is wrong". Text round-trips exactly on
#: both, and `Decimal(str(...))` is the parse the converter already does.
#: The ORDER BY deliberately stays on the numeric expression — ordering on the text would sort
#: '9.00' after '10.00'.
_OFFER_SQL = """
    SELECT o.currency,
           CAST(coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price)
                AS TEXT) AS price
      FROM catalog_offers o
     WHERE o.sku_key = :sku_key
       AND o.product_key = :product_key
       AND o.suppression_reason IS NULL
       AND o.suppressed_at IS NULL
       AND coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) IS NOT NULL
     ORDER BY coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) ASC
     LIMIT 1
"""


async def _load_catalog_row(
    *,
    merchant_domain: str,
    product_key: str,
    variant_key: Optional[str],
    market_country: str,
    accept_variant_labels: Tuple[str, ...],
    also_accept_domains: Tuple[str, ...],
) -> svc.PurchaseRow:
    """OUR row for the thing being bought. The price comes from here and from nowhere else.

    Refuses, in this order and for these reasons:
      `row_not_found`   no such product under THIS domain, or the variant is not this product's,
                        or no variant was named and the product does not have exactly one.
      `row_not_shopify` the row's intake lane is not `shopify`.
      `row_unpriced`    no usable offer, or an amount/currency this rail cannot convert exactly.

    THE DOMAIN IS A CONJUNCT, not a check afterwards. `merchant_domain` is the key the
    eligibility allowlist was read under; if it were not also the key the product is read under,
    a caller could name an enabled merchant and a product belonging to a different one. A product
    that exists under another domain answers `row_not_found` — the same answer as one that does
    not exist, so this route cannot be used to enumerate another merchant's catalogue.
    """
    product = await database.fetch_one(
        _PRODUCT_SQL, {"product_key": product_key, "merchant_domain": merchant_domain}
    )
    if not product:
        raise svc.PurchaseRefused("row_not_found", "no catalog product for this domain and key")
    product = dict(product)

    # THE INTAKE LANE, NOT THE STOREFRONT, and this route wants the intake lane. `external_seed`
    # rows are overwhelmingly Shopify storefronts, and the gateway normalises them for DISPLAY on
    # evidence held in a seed snapshot — evidence that is not in `catalog_*` and that this module
    # would have to re-derive to reuse. A purchase is not a display: if the row we are about to
    # charge a card against did not come through the merchant sync, we do not claim to know what
    # its storefront is, and we refuse.
    if str(product.get("platform") or "").strip().lower() != "shopify":
        raise svc.PurchaseRefused("row_not_shopify", "the catalog row's intake lane is not shopify")

    if variant_key:
        # EXACT MATCH ON sku_key, NEVER A DERIVATION. There are three live spellings of a variant
        # sku key in this repo (`::v::`, `::v:` and the sync lane's own), they collide on the
        # identity index, and a route that rebuilt a key from a product key and a variant id
        # would pick the wrong lane's row for thousands of products.
        sku_row = await database.fetch_one(
            _SKU_BY_KEY_SQL, {"variant_key": variant_key, "product_key": product_key}
        )
        if not sku_row:
            raise svc.PurchaseRefused("row_not_found", "no catalog sku for this product and key")
        sku = dict(sku_row)
    else:
        # No variant named: the product must have exactly ONE, or we do not know what is being
        # bought. `LIMIT 2` is how "exactly one" is asked without counting the whole table.
        candidates = [dict(r) for r in await database.fetch_all(
            _SKUS_FOR_PRODUCT_SQL, {"product_key": product_key}
        )]
        if len(candidates) != 1:
            raise svc.PurchaseRefused(
                "row_not_found", "no variant named and the product does not have exactly one"
            )
        sku = candidates[0]

    offer = await database.fetch_one(
        _OFFER_SQL, {"sku_key": sku["sku_key"], "product_key": product_key}
    )
    if not offer:
        raise svc.PurchaseRefused("row_unpriced", "no usable offer for this sku")
    offer = dict(offer)

    currency = str(offer.get("currency") or sku.get("sku_currency") or "").strip().upper()
    if not currency:
        raise svc.PurchaseRefused("row_unpriced", "the offer names no currency")

    # THE RAIL'S OWN CONVERTER, which refuses rather than rounds. A price we cannot state exactly
    # in minor units is one we do not buy against: `verify_quote` compares
    # `quantity * our_price_minor` to Reap's subtotal with NO tolerance, so a rounded value here
    # is a refused purchase three states later, reported as a price change that never happened.
    price_minor = ledger.amount_minor_or_none(offer.get("price"), currency)
    if not price_minor or price_minor <= 0:
        raise svc.PurchaseRefused("row_unpriced", "the offer price is not an exact minor amount")

    product_name = str(product.get("product_title") or "").strip()
    if not product_name:
        raise svc.PurchaseRefused("row_not_found", "the catalog row has no title")

    variant_title = str(sku.get("variant_title") or "").strip() or None
    return svc.PurchaseRow(
        merchant_domain=merchant_domain,
        product_key=product_key,
        variant_key=str(sku.get("sku_key") or "") or None,
        product_name=product_name,
        variant_title=variant_title,
        brand=str(product.get("brand") or "").strip() or None,
        category=str(product.get("category") or product.get("product_type") or "").strip() or None,
        our_price_minor=int(price_minor),
        currency=currency,
        market_country=market_country,
        accept_variant_labels=accept_variant_labels,
        also_accept_domains=also_accept_domains,
        # THE TWO ARE EXCLUSIVE AND `start_purchase` REFUSES THE INCONSISTENT PAIR. A sku with no
        # title (the synthetic single-variant row) is exactly the "single value axis accepted
        # without a title" case the client documents, and the ABSENCE of `variant_title` is how
        # that is carried — there is no column for the flag.
        declared_single_variant=variant_title is None,
    )


# ── idempotency ──────────────────────────────────────────────────────────────────────────────

#: Where Reap sends the buyer's browser after a hosted page. There is no such constant in the
#: client — it validates a return URL but never supplies one — so this rail's default lives here.
#:
#: A CODE DEFAULT, derived from the client's own host allowlist, with the env var as an override.
#: The other order (env var required, code fallback absent) is the shape that took five flags
#: down with one environment wipe; and because the value is pushed through
#: `rc.validate_return_url` on every call like any caller-supplied one, an operator who sets
#: `REAP_AGENTIC_RETURN_URL` to a host that is not ours gets a refusal, not an open redirector
#: with a payment page in front of it.
_RETURN_URL_ENV = "REAP_AGENTIC_RETURN_URL"


def _default_return_url() -> str:
    configured = (os.getenv(_RETURN_URL_ENV) or "").strip()
    if configured:
        return configured
    hosts = rc.return_url_hosts()
    return f"https://{hosts[0]}/reap/return"


#: How long a key is honoured. Beyond it the same key starts a NEW purchase, which is the right
#: default for a key an agent is likely to reuse across sessions — and the reason the window is
#: enforced here, in a policy line, rather than by a constraint in the table.
_IDEMPOTENCY_WINDOW_SECONDS = 24 * 60 * 60


async def _replayed_purchase_id(
    *, agent_id: str, agent_user_ref_hash: str, idempotency_key: str
) -> Optional[str]:
    row = await database.fetch_one(
        """
        SELECT purchase_id, created_at
          FROM reap_agentic_purchase_keys
         WHERE agent_id = :agent_id
           AND agent_user_ref_hash = :agent_user_ref_hash
           AND idempotency_key = :idempotency_key
        """,
        {
            "agent_id": agent_id,
            "agent_user_ref_hash": agent_user_ref_hash,
            "idempotency_key": idempotency_key,
        },
    )
    if not row:
        return None
    record = dict(row)
    created = _aware(record.get("created_at"))
    if created is not None:
        age = (_now() - created).total_seconds()
        if age > _IDEMPOTENCY_WINDOW_SECONDS:
            return None
    return str(record.get("purchase_id") or "").strip() or None


async def _claim_idempotency_key(
    *, agent_id: str, agent_user_ref_hash: str, idempotency_key: str, purchase_id: str
) -> str:
    """Record the key, and return the purchase id that WON.

    The insert happens after the purchase exists, because the key has to point at something. Two
    concurrent requests with one key therefore both create a purchase, and exactly one of them
    lands the key; the loser reads the winner's id back and its own row is terminated by the
    caller. That is the honest shape of this race on a ledger that mints its own ids — the
    alternative, reserving the key first, needs an id before there is a row and leaves a poisoned
    key behind whenever `start_purchase` refuses.
    """
    try:
        await database.execute(
            """
            INSERT INTO reap_agentic_purchase_keys (
                agent_id, agent_user_ref_hash, idempotency_key, purchase_id
            ) VALUES (
                :agent_id, :agent_user_ref_hash, :idempotency_key, :purchase_id
            )
            """,
            {
                "agent_id": agent_id,
                "agent_user_ref_hash": agent_user_ref_hash,
                "idempotency_key": idempotency_key,
                "purchase_id": purchase_id,
            },
        )
        return purchase_id
    except Exception:  # noqa: BLE001
        pass

    winner = await _replayed_purchase_id(
        agent_id=agent_id,
        agent_user_ref_hash=agent_user_ref_hash,
        idempotency_key=idempotency_key,
    )
    return winner or purchase_id


async def _abandon_duplicate(purchase_id: str) -> None:
    """Terminate the purchase that lost an idempotency race.

    `refused` is terminal, so the SAME statement that writes it NULLs `buyer_email` and
    `shipping_address` — which is the reason this is a transition and not a DELETE. The row stays
    for accounting, the PII does not, and the poller will never look at a terminal row.

    The unfenced `ledger.transition` is correct HERE and nowhere in a worker: this row was
    created moments ago by this request, has never been claimed, and no other process knows its
    id. Failure is swallowed — the loser row is already invisible to the caller, and the sweeps
    bound it either way.
    """
    try:
        await ledger.transition(
            purchase_id,
            from_states=("resolving",),
            to_state="refused",
            refusal_reason="duplicate_idempotency_key",
        )
    except Exception:  # noqa: BLE001
        logger.warning("reap_agentic: could not abandon duplicate purchase=%s", purchase_id)


# ── the owner-facing body ────────────────────────────────────────────────────────────────────

#: Every state in which a hosted URL is a link we want a buyer to open. `resolving` and `quoting`
#: have no page yet; the terminals have one that is spent, and a completed purchase whose
#: approval link still worked would be a second charge waiting to happen.
_HOSTED_STATES = frozenset({"needs_enrollment", "awaiting_approval"})

#: Lifted out of the flat view into `totals` so each number has ONE spelling in the response.
_TOTAL_KEYS = (
    "our_price_minor",
    "quoted_total_minor",
    "final_total_minor",
    "shipping_minor",
    "tax_minor",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    """A comparable UTC datetime, or None.

    THREE SHAPES ARRIVE HERE AND ONLY ONE OF THEM IS A DATETIME. asyncpg hands a `timestamptz`
    back as an AWARE datetime; SQLite hands the same column back as a **string**, because this
    module's reads of `reap_agentic_purchase_keys` are raw SQL with no result processor; and the
    ledger's own reads are already normalised to NAIVE UTC datetimes. Comparing an aware datetime
    to a naive one raises, and treating a string as "not a datetime" silently skips the
    comparison — which is how the idempotency window came to be unenforced on SQLite while every
    test that did not age a row still passed.

    `ledger._decode_dt` is the repo's one parser for the string shape, and it is reused rather
    than reimplemented: a second parser would be a second opinion about what SQLite stored, and
    the ledger is the module that decided the storage format.
    """
    if isinstance(value, str):
        value = ledger._decode_dt(value)
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _public_body(view: Mapping[str, Any]) -> Dict[str, Any]:
    """The owner-facing shape, built FROM the ledger's allowlist and never from the row.

    `view` is already `ledger.public_purchase_view` output, so the only keys that can appear are
    `PUBLIC_PURCHASE_COLUMNS`. This function only ever REMOVES keys and adds ones it computes; it
    never reads a column. That is what makes "a buyer id cannot appear in this response" a
    property of the code rather than a thing the tests hope for — there is no expression here
    that could put one there.
    """
    body = {key: value for key, value in view.items()}
    state = str(body.get("state") or "")

    currency = body.get("currency")
    totals: Dict[str, Any] = {"currency": currency}
    for key in _TOTAL_KEYS:
        totals[key] = body.pop(key, None)
    body.pop("currency", None)
    body["totals"] = totals

    # THE HOSTED URL IS RE-VETTED AT READ TIME, against the same allowlist that let it be stored.
    # Defence in depth, and not theatre: the value in the column came from a partner, it is the
    # one field on this row whose whole purpose is to be opened by a human, and the cost of the
    # check is a `urlparse`. A URL that no longer passes is DROPPED rather than reported — there
    # is nowhere to send the buyer, which is exactly what an absent key says.
    hosted_url = body.pop("hosted_url", None)
    hosted_expires = body.pop("hosted_url_expires_at", None)
    expires_at = _aware(hosted_expires)
    if (
        state in _HOSTED_STATES
        and rc.hosted_url_is_allowed(hosted_url)
        and (expires_at is None or expires_at > _now())
    ):
        body["hosted_url"] = hosted_url
        body["hosted_url_expires_at"] = hosted_expires

    # Reap's order id is EVIDENCE on every other state and an ANSWER on `completed`. Renamed on
    # the way out because the caller is being told "your order exists and this is its reference",
    # not handed a partner handle to look things up with.
    order_id = body.pop("reap_order_id", None)
    if state == "completed" and order_id:
        body["order_reference"] = order_id

    body["poll_after_seconds"] = svc.POLL_INTERVALS.get(state)
    return body


async def _owner_view(
    *, purchase_id: str, agent_id: str, agent_user_ref_hash: str
) -> Optional[Dict[str, Any]]:
    view = await ledger.get_purchase_for_owner(purchase_id, agent_id, agent_user_ref_hash)
    return _public_body(view) if view else None


def _not_found() -> JSONResponse:
    """WRONG OWNER AND NO SUCH PURCHASE ARE THE SAME ANSWER. 404, never 403: a 403 confirms the
    id exists, which turns this endpoint into an oracle for guessing other buyers' purchase ids.
    A new response object each time — `JSONResponse` is not safe to share across requests."""
    return JSONResponse(status_code=404, content={"error": "purchase_not_found"})


# ── routes ───────────────────────────────────────────────────────────────────────────────────


@router.post("/purchases")
async def start_reap_purchase(
    payload: Dict[str, Any] = Body(...),
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    """Open a purchase and answer at once. MAKES NO PARTNER CALL — the poller does that."""
    try:
        _require_rail()
        agent_user_ref = _require_agent_user(agent_user)

        try:
            req = StartPurchaseRequest.model_validate(payload)
        except ValidationError:
            # The pydantic error names fields AND the values that failed, and one of those fields
            # is the buyer's address. Never forwarded, never logged.
            raise svc.PurchaseRefused("invalid_request", "the request body did not validate")

        merchant_domain = req.merchant_domain.strip().lower()
        market_country = str(req.buyer.shipping_address.country or "").strip().upper()
        if not _COUNTRY_RE.match(market_country):
            raise svc.PurchaseRefused("invalid_address", "country must be two letters")

        agent_id = str(context.agent_id)
        agent_user_ref_hash = hash_agent_user_ref(agent_user_ref)
        if not agent_user_ref_hash:
            raise svc.PurchaseRefused("agent_user_required")

        idempotency_key = str(req.idempotency_key or "").strip() or None
        if idempotency_key:
            replayed = await _replayed_purchase_id(
                agent_id=agent_id,
                agent_user_ref_hash=agent_user_ref_hash,
                idempotency_key=idempotency_key,
            )
            if replayed:
                # THE SAME ID, AND THE PURCHASE'S REAL STATE. Not a hardcoded "resolving": by the
                # time a caller retries, the row may have moved, and answering with a state it is
                # no longer in would be a lie in the one field the caller polls on.
                view = await _owner_view(
                    purchase_id=replayed,
                    agent_id=agent_id,
                    agent_user_ref_hash=agent_user_ref_hash,
                )
                if view:
                    return JSONResponse(
                        status_code=202,
                        content={
                            "purchase_id": replayed,
                            "status": view.get("state"),
                            "poll_after_seconds": view.get("poll_after_seconds"),
                        },
                    )

        # ELIGIBILITY BEFORE THE CATALOG READ. A domain nobody enabled gets the same answer
        # whether or not we hold its products, so an ineligible merchant cannot be probed for
        # what is in our catalogue.
        eligible = await _eligibility(
            merchant_domain=merchant_domain,
            market_country=market_country,
            product_key=req.product_key.strip(),
            variant_key=str(req.variant_key or "").strip() or None,
        )

        row = await _load_catalog_row(
            merchant_domain=merchant_domain,
            product_key=req.product_key.strip(),
            variant_key=str(req.variant_key or "").strip() or None,
            market_country=eligible.market_country,
            accept_variant_labels=eligible.accept_variant_labels,
            also_accept_domains=eligible.also_accept_domains,
        )

        buyer_id = await _buyer_id_for(
            agent_id=agent_id, agent_user_ref_hash=agent_user_ref_hash
        )
        buyer_ref = await _reap_buyer_ref(buyer_id)

        # THE CALLER'S URL, OR OURS — AND IT IS NOT VALIDATED HERE.
        #
        # `start_purchase` runs `rc.validate_return_url` on whatever it is handed, builds both
        # stage variants from it, and raises `PurchaseRefused("invalid_return_url")` — the same
        # reason code this route would have used. A copy of that check here would be a guard that
        # can never be the one that fires: it would refuse exactly the inputs the next line
        # refuses, and a mutation that deleted it would kill no test. An unreachable guard reads
        # as protection that does not exist, so there is one validator and it is the one that
        # owns the URL.
        #
        # What this line DOES decide is which URL gets validated. A caller that names none gets
        # ours, and ours goes through the same validator — so an operator who points
        # `REAP_AGENTIC_RETURN_URL` at a host that is not ours gets a refusal rather than an open
        # redirector with a payment page in front of it.
        return_url = str(req.return_url or "").strip() or _default_return_url()

        purchase_id = await svc.start_purchase(
            agent_id=agent_id,
            agent_user_ref_hash=agent_user_ref_hash,
            buyer_ref=buyer_ref,
            row=row,
            buyer=svc.BuyerContact(
                email=str(req.buyer.email or "").strip(),
                shipping_address=_buyer_address_for_client(req.buyer),
            ),
            quantity=int(req.quantity),
            # MINTED HERE, NEVER TAKEN FROM THE REQUEST. This is the only join between a Reap
            # checkout and the session that started it, and a caller-supplied one could collide
            # with another caller's.
            click_id=new_click_id(),
            return_url=return_url,
        )

        if idempotency_key:
            winner = await _claim_idempotency_key(
                agent_id=agent_id,
                agent_user_ref_hash=agent_user_ref_hash,
                idempotency_key=idempotency_key,
                purchase_id=purchase_id,
            )
            if winner != purchase_id:
                await _abandon_duplicate(purchase_id)
                purchase_id = winner

        logger.info(
            "reap_agentic: route opened purchase=%s merchant=%s agent=%s",
            purchase_id,
            merchant_domain,
            agent_id,
        )
        return JSONResponse(
            status_code=202,
            content={
                "purchase_id": purchase_id,
                "status": "resolving",
                "poll_after_seconds": svc.POLL_INTERVALS.get("resolving"),
            },
        )
    except svc.PurchaseRefused as exc:
        return _refused(exc)


@router.get("/purchases/{purchase_id}")
async def get_reap_purchase(
    purchase_id: str,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    try:
        _require_rail()
        agent_user_ref = _require_agent_user(agent_user)
        agent_user_ref_hash = hash_agent_user_ref(agent_user_ref)
        if not agent_user_ref_hash:
            raise svc.PurchaseRefused("agent_user_required")
    except svc.PurchaseRefused as exc:
        return _refused(exc)

    # BOTH CONJUNCTS, IN THE LEDGER'S SQL. Not re-implemented here and not re-checked afterwards:
    # `get_purchase_for_owner` puts `agent_id` AND `agent_user_ref_hash` in the WHERE clause, so
    # "not yours" and "does not exist" are one answer produced by one statement, and there is no
    # filter in this module for a refactor to drop.
    view = await _owner_view(
        purchase_id=str(purchase_id),
        agent_id=str(context.agent_id),
        agent_user_ref_hash=agent_user_ref_hash,
    )
    if not view:
        return _not_found()
    return view


#: The list is capped whatever the caller asks for. `le=` on the query parameter answers 422 for
#: an over-large value, and the ledger clamps again at 500 — this is the tighter of the two,
#: because a history page is a page.
_LIST_MAX = 100


@router.get("/purchases")
async def list_reap_purchases(
    limit: int = Query(default=20, ge=1, le=_LIST_MAX),
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    try:
        _require_rail()
        agent_user_ref = _require_agent_user(agent_user)
        agent_user_ref_hash = hash_agent_user_ref(agent_user_ref)
        if not agent_user_ref_hash:
            raise svc.PurchaseRefused("agent_user_required")
    except svc.PurchaseRefused as exc:
        return _refused(exc)

    # THE SAME CONJUNCT AS THE SINGLE READ, and this is the read that matters more: a get that
    # leaks needs an id to be guessed first, and a list that leaks hands the whole set over.
    views = await ledger.list_purchases_for_owner(
        str(context.agent_id), agent_user_ref_hash, limit=min(int(limit), _LIST_MAX)
    )
    return {
        "purchases": [_public_body(view) for view in views],
        "limit": min(int(limit), _LIST_MAX),
    }
