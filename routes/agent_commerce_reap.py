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

Both handlers take a raw `Request` and do ALL of their parsing inside the body, after the dial,
for the same reason. Anything in the signature — a `Body(...)` model, a `Query(le=...)` bound — is
validated by FastAPI BEFORE the handler runs, and its refusal is a 400 naming the field. That made
the dark rail probeable: a body of `[1, 2]`, the wrong content-type, or `?limit=500` each answered
400 while every well-formed request answered 404, and no test that sends only valid requests would
ever have noticed. `/openapi.json` still lists the paths — the routes are mounted at import — and
that residue is documented rather than papered over.

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
cannot name a buyer in the body; there is no field for it. Since WP4b that mapping is CREATED on
the first purchase when it is absent (see `_buyer_id_for`), and the important half of "never the
caller's" survives that intact: the identity is minted from randomness, never from the email or
anything else in the body, and the existing row always wins. An agent can cause a buyer to exist;
it still cannot choose WHICH buyer.

── CONSENT ──────────────────────────────────────────────────────────────────────────────────

`buyer.consent_version` is REQUIRED on the POST and its absence is `consent_required` (400),
checked after the dial and before eligibility, the catalog read, and every write. It is the
version tag of the terms the buyer accepted, stored on `reap_agentic_buyer_refs` — the row the
card enrollment hangs off — and rewritten on every purchase, so the pair
`(consent_version, consented_at)` is always the LATEST consent rather than the first.

The field exists because WP4b took a human out of the loop. Before it, a buyer arrived already
linked by a surface where they had signed in, and that sign-in WAS the consent; minting the
identity from an agent's assertion removes that step, so the door must carry the act forward.
The wording is the owner's and lives at the door. This route records which wording, and
deliberately does not adjudicate it — see `_consent_version`.

── THE THREE IDENTIFIERS, AND WHY REAP GETS THE THIRD ───────────────────────────────────────

    buyer_id                the global buyer. Joins to orders, addresses, users. NEVER leaves.
                            Minted here on an agent-only buyer's first purchase; it is then an
                            id in `shop_users`' format with NO `shop_users` row behind it, and
                            `_buyer_id_for` explains at length why creating that row from the
                            request's email would be an account-takeover primitive.
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

import hashlib
import json
import logging
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

import db.reap_agentic_ledger as ledger
# THE ONE consent-tag shape rule, imported rather than re-implemented. Bound at module level
# so tests can assert BY IDENTITY that this module, services/reap_agentic_purchase and the
# ledger call the same function object — see that function's docstring for the production
# divergence three copies of the rule produced.
from db.reap_agentic_ledger import require_consent_version as consent_shape
import db.tierb_cart_link_eligibility as tierb_eligibility
import db.merchant_purchasability as purchasability
import services.reap_agentic_client as rc
import services.reap_agentic_purchase as svc
from db.buyer_vault import hash_agent_user_ref, mint_pairwise_buyer_ref
from db.commerce_attribution import surface_click_events
from db.database import database
from routes.agent_auth import AgentContext, get_agent_context
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from services.commerce_attribution_service import new_click_id
from services.outbound_links_service import (
    build_shopify_cart_permalink,
    extract_shopify_numeric_variant_id,
)
from services.shopify_variant_identity import sole_verified_cart_variant_id
# THE ONE merchant-host canonicaliser, imported rather than re-implemented: lower case, ONE
# leading `www.` removed, and a ValueError for anything that is not a bare DNS host name —
# including a trailing dot. It is `tierb_cart_link_eligibility`'s own key function
# (`normalize_domain`) plus that one refusal, and for every bare host it agrees with
# `db.merchant_purchasability.normalize_domain`, the key of the purchasability facts. See
# `_merchant_domain_key` for why the route needs it.
from services.tierb_cart_link_merchants import canonical_merchant_domain

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
    # The buyer did not agree to anything. 400 AND NOT 401/403: the caller CAN fix this by
    # editing the request — it is a missing field, not a missing credential — and a 401 would
    # send a door that already holds a valid user token off to re-authenticate, which would
    # change nothing. It is listed apart from `invalid_request` because the door has to be able
    # to tell "your body is malformed" from "go and ask your user", which are different
    # instructions to different parts of a client.
    "consent_required": 400,
    # The request is well-formed; the world does not permit it. Editing the body will not help.
    "merchant_not_eligible": 409,
    # The merchant is ALLOWLISTED but holds no fresh, positive purchasability fact: nobody has
    # recently rendered its checkout from the buyer's vantage and seen a card method at our
    # price. A SEPARATE CODE from `merchant_not_eligible` on purpose — the two say different
    # things to an operator ("nobody listed this merchant" vs "this merchant is listed and we
    # cannot prove it can be paid"), and collapsing them would hide exactly the state this gate
    # exists to make visible. 409 like its neighbours: the body is fine, the world is not.
    "merchant_not_purchasable": 409,
    "buyer_unlinked": 409,
    "row_not_found": 409,
    "row_unpriced": 409,
    "row_not_shopify": 409,
    "row_variant_unverified": 409,
    "seller_identity_unverified": 409,
    "row_currency_mismatch": 409,
    "idempotency_conflict": 409,
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


#: Characters that must never reach a bind on this path. `Cc`/`Cf`/`Cs`/`Co`/`Cn` is the same
#: Unicode-category set `services.reap_agentic_purchase._clean_buyer_text` uses, and it is used
#: here for the opposite outcome — see `_identifier` below.
_UNPRINTABLE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _identifier(value: Any, name: str, *, max_chars: int) -> str:
    """One route-owned identifier, or `invalid_request`.

    ── WHY THIS REFUSES WHERE THE BUYER'S ADDRESS IS CLEANED ────────────────────────────────

    `_clean_buyer_text` STRIPS format characters out of the buyer's name and address and then
    refuses what is left, because a zero-width joiner pasted out of a web form is not an attack
    and refusing a whole purchase for it helps nobody. These four values are not prose. They are
    `merchant_domain`, `product_key`, `variant_key` and `idempotency_key`, and every one of them
    is matched EXACTLY against something in storage (the domain after one documented fold — see
    `_merchant_domain_key` — which is a rule both sides apply, not a repair of the input). Silently removing a character from an
    identifier does not sanitise it — it turns it into a different identifier, which then either
    matches nothing or, worse, matches something else. So these refuse.

    ── WHAT IT ACTUALLY STOPS, AND WHY ONLY THE POSTGRES ARM COULD SEE IT ───────────────────

    A single NUL byte in any of the four reached an asyncpg bind — in `_eligibility` and in
    `_replayed_purchase_id` — and asyncpg raises `CharacterNotInRepertoireError`, which is not a
    `PurchaseRefused`, so it escaped the handler's one `except` and the error middleware turned
    it into a **500**. SQLite stores NULs happily and answered a clean 409, so the SQLite arm
    could not see this at all: it is a defect that existed only on the dialect production runs.
    A 500 on a dark rail is also the one answer that tells a prober the route is really there.

    The length cap is here for the same reason the cleanliness check is: `merchant_domain` is
    `VARCHAR(255)` and `idempotency_key` is `VARCHAR(128)` in migration 226, and a value past
    those is a driver error rather than a refusal.
    """
    text = str(value or "").strip()
    if not text:
        raise svc.PurchaseRefused("invalid_request", f"{name} is required")
    if len(text) > max_chars:
        raise svc.PurchaseRefused("invalid_request", f"{name} is longer than {max_chars}")
    if any(unicodedata.category(ch) in _UNPRINTABLE for ch in text):
        # The VALUE is not named in the message or the log. It is caller-controlled, it is about
        # to be refused precisely because it contains something that does not render as itself,
        # and a log line is exactly where that matters.
        raise svc.PurchaseRefused(
            "invalid_request", f"{name} contains characters that are not printable"
        )
    return text


def _merchant_domain_key(host: str) -> str:
    """The MERCHANT this request names, as the key every allowlist and fact on this rail is
    matched under, or `invalid_request`.

    ── WHY `www.brand.com` AND `brand.com` HAVE TO BE ONE MERCHANT ──────────────────────────

    Three parties spell the same store independently and none of them agree. Shopify's
    `shop_domain` is what the sync writes into `catalog_products.source_domain`, and in
    production that is `www.Brand.com` — mixed case, with the `www.`. An operator writes the
    eligibility row by hand, and writes `brand.com`. The gateway sends the storefront host AS IT
    OBSERVED IT, which is either. Every one of those comparisons used to be EXACT, so whichever
    party disagreed with the other two got a refusal — `merchant_not_eligible` or
    `row_not_found` — that names the wrong cause and cannot be fixed from the door.

    So the rule is ONE canonical form, applied on BOTH sides of every comparison: this function
    on the request, the identical SQL expression on each stored column (see `_ELIGIBILITY_SQL`
    and `_PRODUCT_SQL`). It strips exactly ONE leading `www.`: `www.www.brand.com` is
    `www.brand.com`, and `wwwbrand.com` is a different store from `brand.com`, because a prefix
    that is not followed by a dot is part of the name.

    ── WHY THIS REFUSES RATHER THAN REDUCES ───────────────────────────────────────────────

    `https://brand.com/`, `brand.com:443`, `user@brand.com`, `brand.com.`, an IP literal or a
    single label is not a spelling of a merchant; it is a caller bug. Reducing it to a host would be a guess, and
    `_identifier` above already says what a guessed identifier turns into. Refused with the
    value NOT echoed — the canonicaliser's own `ValueError` names it, so it is not carried.

    ── THE FOLD RUNS EXACTLY ONCE, ON A SPELLING ─────────────────────────────────────────────

    It is not idempotent: `www.www.brand.com` -> `www.brand.com` -> `brand.com`. So the key this
    returns is compared only against a stored SPELLING folded by the same rule (the SQL), and it
    is never handed to a consumer that folds a SPELLING for itself: the Tier B verdict gets the
    observed host, and so does the purchase row, whose readers fold it — the resolver's
    `merchant_domain_matches`, and the attribution close (`svc._attribution_merchant_key`),
    which must key one merchant once whichever spelling bought. The one exception is
    `is_purchasable`, whose WRITER folds twice — see the handler. The CART-LINK
    lane's catalog read and URL get the observed host for a different reason: the permalink's host and the storefront
    evidence's host are compared byte-for-byte downstream (`sole_verified_cart_variant_id`,
    `cart_link_refusal`). See the handler.
    """
    try:
        return canonical_merchant_domain(host)
    except ValueError:
        raise svc.PurchaseRefused(
            "invalid_request", "merchant_domain is not a bare host name"
        ) from None


def _consent_version(value: Any) -> str:
    """The version of the terms this buyer accepted, or `consent_required`.

    ── WHY A PURCHASE IS WHERE THIS IS ASKED FOR ────────────────────────────────────────────

    Because this is the request that creates a DURABLE buyer identity and, behind it, a stored
    card at Reap. Before WP4b there was nowhere to put it: the buyer arrived already linked by a
    surface where a human had signed in, and that sign-in was the consent. Minting the identity
    from an agent's assertion removes the human from that step, so the door has to carry the act
    forward explicitly — and it has to carry it on the request that USES it, not on some earlier
    call whose result nothing here can see.

    ── THE RULE IS NOT HERE. IT IS `consent_shape`, AND SO IS THE SERVICE'S AND THE LEDGER'S ─

    `consent_shape` is `db.reap_agentic_ledger.require_consent_version`. This function is the
    mapping from its `ValueError` to this route's refusal code, and nothing else.

    IT USED TO BE A COPY OF THE RULE, one of three, and the three had drifted: this one and the
    ledger tested unicode CATEGORY membership while the service tested `str.isprintable()`, which
    is additionally False for every `Zs` except U+0020 and for U+2028/U+2029. A tag carrying a
    non-breaking space was therefore accepted HERE, written onto the buyer-ref row by
    `_record_consent`, and then refused by `start_purchase` with `consent_required` — the consent
    recorded, the purchase refused for not having one. Measured on Postgres: `buyer_refs = 1`,
    `purchases = 0`.

    ── WHAT IS DELIBERATELY NOT CHECKED, WHEREVER THE RULE LIVES ────────────────────────────

    THE VALUE IS NOT MATCHED AGAINST A LIST. There is no allowlist of known versions and there
    must not be one: the wording is the owner's, it changes without a deploy, and a backend that
    refused an unrecognised tag would reject the newest consent — the strongest one — the moment
    the door shipped it and before we had. We record what was accepted; we do not adjudicate it.
    What the record is FOR is answering "which wording was this buyer shown", and for that,
    storing an unfamiliar tag beats refusing it.

    ── WHAT A NON-STRING DOES ───────────────────────────────────────────────────────────────

    It never reaches here: `StartPurchaseRequest.buyer` types the field, so `123` / `true` / `{}`
    are `invalid_request` from pydantic before this runs. `consent_shape` refuses one too, which
    is what protects the direct callers of `start_purchase` that have no pydantic in front.
    """
    try:
        return str(consent_shape(value, required=True))
    except ValueError as exc:
        # The ledger's messages name the FIELD and the RULE and never the value — which is the
        # same courtesy `_identifier` extends, and the reason it is safe to carry one out here.
        raise svc.PurchaseRefused("consent_required", str(exc)) from None


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
    #: REQUIRED BY THE ROUTE, OPTIONAL IN THIS MODEL, AND THAT IS THE POINT. A pydantic
    #: `Field(...)` here would be validated before the handler's own checks and would arrive as
    #: `invalid_request` — the one code that tells a door to go and fix its JSON. A missing
    #: consent is not a malformed body; it is a missing act by a human, and it has its own code.
    #: `_consent_version` below is the real requirement, and it is a statement the mutant table
    #: can delete on its own.
    consent_version: Optional[str] = None


class StartPurchaseRequest(BaseModel):
    item_source: Literal["reap_variant", "cart_link"] = "reap_variant"
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
#:
#: IMPORTED, NOT REDECLARED (WP6). `db.reap_agentic_ledger` owns this constant and the SET form
#: of the same predicate (`list_enabled_merchant_markets`), which the purchasability sweep uses
#: to build its population. Two spellings of "which rows are merchant rows" is how a merchant
#: this route accepts came to be one the sweep never visited.
_MERCHANT_ROW = ledger.ELIGIBILITY_MERCHANT_ROW

#: THE CURRENCY A MARKET IMPLIES. A MAP AND NOT A COLUMN, deliberately.
#:
#: The currency of a market is a fact about the world, not a per-merchant setting: two operators
#: filling in a `market_currency` column for the same country could disagree, and one of them
#: would be wrong in a way only a charge would reveal. A column also has to be RIGHT on every row
#: an operator writes by hand, and the SQL snippet in the runbook is the interface most of them
#: will use.
#:
#: IT FAILS CLOSED. A market that is not in this map refuses `row_currency_mismatch` rather than
#: skipping the check, which is the same direction every other decision on this rail takes —
#: adding a market is a one-line change here and the runbook says so. That is the right trade for
#: a list this short: the rail is domestic-only, and the set of markets Reap's agentic rail is
#: transactable in is smaller still.
#:
#: NOTE FOR WHOEVER ENABLES SG. The curated lane writes USD rows, so an SG merchant whose offers
#: are denominated in USD will refuse here. That refusal is CORRECT — an SGD storefront quoted in
#: USD is exactly the divergence this check exists for — but it will look like a bug, so the
#: runbook names it under "Before arming".
_MARKET_CURRENCY: Dict[str, str] = {
    "US": "USD",
    "CA": "CAD",
    "GB": "GBP",
    "AU": "AUD",
    "SG": "SGD",
    "HK": "HKD",
    "JP": "JPY",
    "NZ": "NZD",
}

#: THE STORED DOMAIN IS CANONICALISED IN THE SQL, not trusted to be canonical.
#:
#: `:merchant_domain` is `_merchant_domain_key`'s output. The column is folded by the SAME rule
#: — lower case, ONE leading `www.` removed — spelled as a `CASE` over `LIKE` and `substr`
#: because SQLite has no `regexp_replace`, and the text is IDENTICAL on both dialects so one
#: statement is what both gates test. `LIKE 'www.%'` has no `_` in it, so the only wildcard is
#: the trailing `%`; `lower()` runs first because Postgres' LIKE is case-sensitive. Rows are
#: operator-entered and the runbook says to write them canonical; this expression is what
#: keeps a row somebody typed as `www.Brand.com` from silently refusing every purchase. (The
#: column is read as a SPELLING and folded once — see `_merchant_domain_key` — so the one host
#: whose canonical form is not a fixed point, `www.www.<x>`, is written as observed.)
#:
#: THE PRIMARY KEY'S LEADING COLUMN IS NO LONGER USABLE FOR THIS READ, and that is accepted
#: knowingly: the table is an allowlist of tens of hand-written rows, and a scan of it costs
#: less than the second spelling rule it would take to keep the index.
_ELIGIBILITY_SQL = """
    SELECT merchant_domain, product_key, variant_key, market_country, enabled,
           accept_variant_labels, also_accept_domains
      FROM reap_agentic_eligibility
     WHERE CASE WHEN lower(merchant_domain) LIKE 'www.%'
                THEN substr(lower(merchant_domain), 5)
                ELSE lower(merchant_domain) END = :merchant_domain
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
    merchant_rows: List[Dict[str, Any]] = []
    labels: List[str] = []
    domains: List[str] = []
    for raw in rows:
        row = dict(raw)
        if str(row.get("product_key") or "") == _MERCHANT_ROW:
            merchant_rows.append(row)
            continue
        row_variant = str(row.get("variant_key") or "")
        # An override pinned to a variant applies to THAT variant only; one with a blank variant
        # applies to every variant of the product. A pinned row must not leak onto its siblings —
        # an accepted label names one object.
        if row_variant and row_variant != str(variant_key or ""):
            continue
        labels.extend(_decode_alias_list(row.get("accept_variant_labels")))
        domains.extend(_decode_alias_list(row.get("also_accept_domains")))

    # EVERY MERCHANT ROW MUST BE ENABLED, not whichever one the database returned last. Since the
    # domain is matched canonically, `www.brand.com` and `brand.com` written as two rows are two
    # merchant rows for ONE merchant — and "turning a merchant off" is an UPDATE an operator runs
    # against the spelling in front of them. A disabled twin therefore wins, which is the only
    # order-independent answer and the one a payment gate should give. (Before canonical matching
    # the same ambiguity existed for a merchant row typed with a `variant_key`, and the answer
    # was row order.) The runbook's one-off collapses such twins.
    if not merchant_rows or not all(bool(r.get("enabled")) for r in merchant_rows):
        raise svc.PurchaseRefused(
            "merchant_not_eligible", "no enabled eligibility row for this domain and market"
        )
    merchant_row = merchant_rows[0]

    # Order-preserving dedupe: the ledger caps the list at 32 entries and raises past it, and two
    # override rows can legitimately name the same alias.
    return _Eligibility(
        market_country=str(merchant_row.get("market_country") or "").upper(),
        accept_variant_labels=tuple(dict.fromkeys(labels)),
        also_accept_domains=tuple(dict.fromkeys(domains)),
    )


# ── the buyer ────────────────────────────────────────────────────────────────────────────────


_BUYER_LINK_SQL = """
    SELECT buyer_id
      FROM buyer_identity_links
     WHERE agent_id = :agent_id
       AND agent_user_ref_hash = :agent_user_ref_hash
     LIMIT 1
"""


async def _linked_buyer_id(*, agent_id: str, agent_user_ref_hash: str) -> str:
    """The buyer this pair is already linked to, or `""`. A PURE READ — it mints nothing.

    Split out of `_buyer_id_for` so the replay path can record a consent against an existing
    buyer without acquiring the power to create one. See the call site: a replay must be able to
    update the consent tag, and must NOT be able to mint an identity before eligibility has been
    checked.
    """
    row = await database.fetch_one(
        _BUYER_LINK_SQL,
        {"agent_id": agent_id, "agent_user_ref_hash": agent_user_ref_hash},
    )
    return str(dict(row).get("buyer_id") or "").strip() if row else ""


async def _touch_link(*, agent_id: str, agent_user_ref_hash: str) -> None:
    """`last_seen_at` on a link we just used, the same as the prefill reader does.

    `routes/agent_checkout_intents._buyer_prefill_from_identity_link` stamps this on every read,
    and this route was the one surface that used a link without saying so — which made
    `last_seen_at` mean "last seen by the checkout-intent path" rather than "last seen", and would
    have made any dormancy sweep built on it retire buyers who purchase on this rail every day.

    BEST-EFFORT, like its twin. A bookkeeping column must not be able to refuse a purchase.
    """
    try:
        await database.execute(
            """
            UPDATE buyer_identity_links
               SET last_seen_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
             WHERE agent_id = :agent_id
               AND agent_user_ref_hash = :agent_user_ref_hash
            """,
            {"agent_id": agent_id, "agent_user_ref_hash": agent_user_ref_hash},
        )
    except Exception:  # noqa: BLE001
        pass


def _mint_buyer_id() -> str:
    """A new buyer id, in the id space `db/accounts.create_or_get_shop_user` mints into.

    `u_` + 16 hex characters: 18 characters into a `VARCHAR(50)` column, 64 bits of randomness,
    and indistinguishable at every reader from an id that came from a sign-up. The FORMAT is
    copied deliberately — a second spelling of "a buyer id" would be the kind of thing a later
    `startswith` somewhere reads as a type tag — but nothing else about that function is.
    """
    return f"u_{secrets.token_hex(8)}"


async def _buyer_id_for(*, agent_id: str, agent_user_ref_hash: str) -> str:
    """`(agent_id, hash(agent_user_ref))` -> `buyer_id`, minting one on the first purchase.

    ── WHAT CHANGED, AND WHOSE DECISION IT WAS ──────────────────────────────────────────────

    WP4 refused `buyer_unlinked` here, on the argument that the only writer of
    `buyer_identity_links` was `routes/buyer_api._upsert_buyer_identity_link` — a
    BUYER-AUTHENTICATED surface — so a link was something a human's sign-in created and nothing
    an agent asserted could. The consequence was that EVERY agent-only buyer was refused, which
    made the rail unarmable for the Minds door it was built for. The owner chose Option A on
    2026-09-18: mint the buyer identity here, on the first purchase, from the agent JWT's user
    reference. This function is that decision.

    ── WHAT IS MINTED, AND WHAT IS EMPHATICALLY NOT ─────────────────────────────────────────

    An OPAQUE buyer id, and NO `shop_users` row. Not "not yet" — never, on this path.

    The obvious reading of "create the buyer the base writer would" is to call
    `db.accounts.create_or_get_shop_user(buyer.email)`, and it is a HOLE. That function is
    `create_or_GET`: it looks the email up by `email_normalized` first and RETURNS THE EXISTING
    ACCOUNT when one matches. The email on this request is a string an agent put in a body. So an
    agent that asserts a stranger's address would be handed that stranger's real `buyer_id`, and
    the link written below would bind the agent's own user ref to a real human's account — after
    which `routes/agent_checkout_intents._buyer_prefill_from_identity_link` hands that agent the
    account's email and default shipping address on the next checkout intent. An account
    takeover, reachable from an agent token, with the victim's address as the prize.

    The identity minted here is AGENT-ASSERTED, not verified: the agent says "this is end user
    X", and nothing has checked that. Such an identity must not be able to collide with a
    verified one, so it is minted from randomness and never from anything the caller sent. The
    email in the body goes where it always went — to `start_purchase`, as the contact for THIS
    purchase — and is not an identity here.

    ── WHAT A ROW-LESS BUYER ID COSTS, MEASURED RATHER THAN ASSUMED ─────────────────────────

    `buyer_identity_links.buyer_id` is `VARCHAR(50)` with NO foreign key; there is no
    `REFERENCES shop_users` anywhere in the repo. The table has exactly two readers besides this
    module, and both tolerate a buyer id with no account behind it:

      * `routes/agent_checkout_intents._buyer_prefill_from_identity_link` runs
        `SELECT email FROM shop_users WHERE id = :id` inside a `try` and then reads
        `buyer_addresses` for a default, also inside one. With neither it returns `None` — which
        is precisely what it returned for this buyer YESTERDAY, when there was no link at all.
        The prefill does not regress; it stays absent.
      * `routes/buyer_api` writes the link, and writes it for a buyer that signed in.

    So the cost is a prefill nobody had. What is bought is a durable identity for the enrollment
    to hang off, which is the whole of WP4b.

    ── AND WHEN THE SAME HUMAN LATER SIGNS IN ───────────────────────────────────────────────

    `_upsert_buyer_identity_link` is a real upsert and REPOINTS the link to the signed-in
    account. That is correct and is left alone: a verified account supersedes a placeholder.
    What it costs is one re-enrollment — `reap_agentic_buyer_refs` is keyed on `buyer_id`, so
    the next purchase mints a fresh ref for the real account and Reap asks for the card once
    more. WP4 refused every such buyer forever to avoid exactly that; one re-enrollment after a
    sign-in is the cheaper half of the trade, and docs/reap_agentic_routes.md says so out loud
    so the door is not surprised by it.

    ── WHY THE INSERT CANNOT OVERWRITE ──────────────────────────────────────────────────────

    `ON CONFLICT DO NOTHING`, and then the answer is RE-READ rather than assumed. The row that
    exists wins, whoever wrote it: a concurrent first purchase, or a buyer-authenticated link
    written between our SELECT and our INSERT. `DO UPDATE` here would let this route — the one
    with the WEAKEST evidence of who the buyer is — overwrite a link a human's sign-in
    established. Returning `minted` without the re-read would be the same defect wearing a
    different hat: two concurrent first purchases would then proceed under two different buyer
    ids, one of which is in no table, and mint two enrollments for one card.
    """
    buyer_id = await _linked_buyer_id(
        agent_id=agent_id, agent_user_ref_hash=agent_user_ref_hash
    )
    if buyer_id:
        await _touch_link(agent_id=agent_id, agent_user_ref_hash=agent_user_ref_hash)
        return buyer_id

    minted = _mint_buyer_id()
    try:
        await database.execute(
            """
            INSERT INTO buyer_identity_links (
                agent_id, agent_user_ref_hash, buyer_id, created_at, updated_at, last_seen_at
            )
            VALUES (
                :agent_id, :agent_user_ref_hash, :buyer_id,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT (agent_id, agent_user_ref_hash) DO NOTHING
            """,
            {
                "agent_id": agent_id,
                "agent_user_ref_hash": agent_user_ref_hash,
                "buyer_id": minted,
            },
        )
    except Exception:  # noqa: BLE001
        # A dialect without `ON CONFLICT` raises the unique violation instead of absorbing it,
        # and the answer to both is the same one: read what is there now.
        pass

    # THE RE-READ IS NOT A VERIFICATION STEP, IT IS THE ANSWER. `minted` is never returned.
    row = await database.fetch_one(
        _BUYER_LINK_SQL,
        {"agent_id": agent_id, "agent_user_ref_hash": agent_user_ref_hash},
    )
    buyer_id = str(dict(row).get("buyer_id") or "").strip() if row else ""
    if not buyer_id:
        # The INSERT failed for a reason that was not a lost race. Fail CLOSED: continuing would
        # open a purchase — and an enrollment — for a buyer no table has heard of.
        raise svc.PurchaseRefused("buyer_unlinked", "could not establish a buyer identity")
    return buyer_id


async def _record_consent(buyer_id: str, consent_version: str) -> None:
    """The LATEST consent this buyer gave, on the row the enrollment hangs off.

    NOT WRAPPED IN A `try`, unlike almost everything else that touches this table. The writes
    around it are wrapped because their failure mode is a lost RACE, which has a correct
    recovery. This one's failure mode is a schema that is missing migration 227's columns, which
    has no recovery and must not be absorbed into a purchase that then proceeds as though the
    consent had been recorded. It surfaces as a 500 on a rail that is already live, which is what
    every other genuine driver failure on this path does.
    """
    await database.execute(
        """
        UPDATE reap_agentic_buyer_refs
           SET consent_version = :consent_version,
               consented_at = :consented_at
         WHERE buyer_id = :buyer_id
        """,
        {
            "consent_version": consent_version,
            "consented_at": _now(),
            "buyer_id": buyer_id,
        },
    )


async def _reap_buyer_ref(buyer_id: str, *, consent_version: str) -> str:
    """The opaque `owner.id` for this buyer. Insert-or-read, and race-safe by re-reading.

    Read first, because that is what almost every call does. On a miss, mint and insert; a
    concurrent caller that got there first raises a unique violation on either the primary key or
    `uq_reap_agentic_buyer_refs_ref`, and the answer to that is to READ WHAT THEY WROTE, never to
    retry the mint. Two refs for one buyer would be two enrollments for one card.

    THE CONSENT IS WRITTEN ON EVERY PATH THROUGH HERE. On the mint it rides in the same INSERT,
    so a row cannot exist without one. On every reuse it is rewritten, because the tag is the
    LATEST version this buyer accepted and a buyer who accepts v2 has not un-accepted it by
    having a row that says v1.
    """
    existing = await database.fetch_one(
        "SELECT reap_buyer_ref FROM reap_agentic_buyer_refs WHERE buyer_id = :buyer_id",
        {"buyer_id": buyer_id},
    )
    if existing:
        ref = str(dict(existing).get("reap_buyer_ref") or "").strip()
        if ref:
            await _record_consent(buyer_id, consent_version)
            return ref

    minted = mint_pairwise_buyer_ref()
    try:
        await database.execute(
            """
            INSERT INTO reap_agentic_buyer_refs (
                buyer_id, reap_buyer_ref, consent_version, consented_at
            )
            VALUES (:buyer_id, :reap_buyer_ref, :consent_version, :consented_at)
            """,
            {
                "buyer_id": buyer_id,
                "reap_buyer_ref": minted,
                "consent_version": consent_version,
                "consented_at": _now(),
            },
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
    await _record_consent(buyer_id, consent_version)
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
#: eligibility row's `market_country` against the buyer's shipping country, and the CURRENCY
#: check below — and not by a column whose population nobody has measured on this cohort.
#:
#: `o.merchant_id` IS A CONJUNCT, AND IT IS THE ONE THIS QUERY WAS MISSING.
#: `catalog_offers.merchant_id` is the OFFER SELLER, and it is NOT the same column as
#: `catalog_products.merchant_id`, which is whose catalogue the product row belongs to. The repo
#: says so structurally: `services/pivot_query_service._fetch_canonical_rows_for_sku` LEFT JOINs
#: `catalog_merchants` TWICE — once on `p.merchant_id` and once on `o.merchant_id` — precisely
#: because one sku can carry offers from several sellers.
#:
#: Without the conjunct this query was `ORDER BY price ASC LIMIT 1` across EVERY seller's offer
#: on the sku, so the cheapest one won. Two ways that goes wrong, and neither is hypothetical:
#:
#:   * a competing seller's offer at 1.00 on the same sku becomes `our_price_minor = 100`. The
#:     buyer is then sent through card enrolment, the quote comes back at the eligible merchant's
#:     real price, and `verify_quote` refuses `price_changed` — for a price change that never
#:     happened. The buyer has entered a card and bought nothing.
#:   * the same product mirrored into the crawl lane (`external_seed`) and synced by the merchant
#:     (`internal_merchant`) carries TWO offer rows with two prices, and the cheaper copy is
#:     often the stale one.
#:
#: The conjunct is `o.merchant_id = p.merchant_id`: the offer belonging to the merchant whose
#: catalogue this product is in, which is the merchant the eligibility allowlist was read under
#: (the domain is a conjunct on the product read). An eligible merchant with no offer of its OWN
#: on this sku is `row_unpriced` — not "somebody else will sell it to you".
#:
#: SUPPRESSION IS A CONJUNCT ON ALL THREE TABLES, both columns each. A suppressed row is one
#: somebody took out of circulation; selling it is the one thing that must not still work.
#:
#: `source_domain` IS CANONICALISED BY THE SAME EXPRESSION `_ELIGIBILITY_SQL` USES, character
#: for character. The Shopify sync writes Shopify's `shop_domain`, which in production is
#: `www.Brand.com`; a bare `lower()` compared that to the canonical `brand.com` the eligibility
#: row was read under and answered `row_not_found` for every product the merchant has. The
#: domain stays a CONJUNCT (see `_load_catalog_row`); only its spelling rule changed.
_PRODUCT_SQL = """
    SELECT p.product_key, p.merchant_id, p.title AS product_title, p.brand, p.category,
           p.product_type, p.platform, p.source_domain
      FROM catalog_products p
     WHERE p.product_key = :product_key
       AND CASE WHEN lower(p.source_domain) LIKE 'www.%'
                THEN substr(lower(p.source_domain), 5)
                ELSE lower(p.source_domain) END = :merchant_domain
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
       AND o.merchant_id = :merchant_id
       AND o.suppression_reason IS NULL
       AND o.suppressed_at IS NULL
       AND coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) IS NOT NULL
     ORDER BY coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) ASC
     LIMIT 1
"""


async def _load_catalog_row(
    *,
    merchant_domain: str,
    storefront_host: str,
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

    THE DOMAIN IS A CONJUNCT, not a check afterwards. `merchant_domain` is the CANONICAL key the
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
        _OFFER_SQL,
        {
            "sku_key": sku["sku_key"],
            "product_key": product_key,
            # THE SELLER. `p.merchant_id`, not a caller value and not the cheapest seller on the
            # sku — see the note above `_PRODUCT_SQL`.
            "merchant_id": str(product.get("merchant_id") or ""),
        },
    )
    if not offer:
        raise svc.PurchaseRefused(
            "row_unpriced", "the eligible merchant has no usable offer of its own on this sku"
        )
    offer = dict(offer)

    currency = str(offer.get("currency") or sku.get("sku_currency") or "").strip().upper()
    if not currency:
        raise svc.PurchaseRefused("row_unpriced", "the offer names no currency")

    # THE MARKET'S CURRENCY, AND IT IS THE LAST THING BETWEEN A DOMESTIC-ONLY RAIL AND A
    # CROSS-CURRENCY CHARGE. Everything upstream has established that the buyer ships to a market
    # this merchant is enabled for; nothing yet has established that the PRICE we are about to
    # commit to is denominated in that market's money. A US-priced offer quoted for an SG buyer
    # on an SGD storefront clears every other check here and then diverges at the quote, where
    # the only vocabulary for it is `price_changed` — a refusal that names the wrong cause and
    # arrives after the buyer has entered a card.
    expected = _MARKET_CURRENCY.get(market_country)
    if expected is None or currency != expected:
        raise svc.PurchaseRefused(
            "row_currency_mismatch",
            f"the offer is priced in {currency} and {market_country} is not",
        )

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
        # THE HOST AS OBSERVED, not the canonical key. Its readers fold it themselves: the
        # resolver's `rc.merchant_domain_matches`, and the attribution close
        # (`svc._attribution_merchant_key`). See `_merchant_domain_key`.
        merchant_domain=storefront_host,
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


# Tier B uses the same catalog price discipline as the variant lane, but its item identity is a
# merchant-issued Shopify numeric variant id, not Reap's opaque variant. A mirrored external-seed
# SKU is synthetic (`source_variant_id == product_key`). Its operator-entered
# `attached_variant_id` is NOT proof of Shopify identity: it can be a numeric SKU. The mirror
# therefore needs a product-bound seed with a sole variant stamped from storefront `.js` evidence.
_CART_PRODUCT_SQL = """
    SELECT p.product_key, p.merchant_id, p.seller_ref, p.seed_kind, p.platform,
           p.source_ref, p.source_system, p.title AS product_title
      FROM catalog_products p
     WHERE p.product_key = :product_key
       AND lower(p.source_domain) = :merchant_domain
       AND p.suppression_reason IS NULL
       AND p.suppressed_at IS NULL
"""
_CART_SKU_BY_KEY_SQL = """
    SELECT s.sku_key, s.source_variant_id, s.title AS variant_title, s.currency
      FROM catalog_skus s
     WHERE s.product_key = :product_key AND s.sku_key = :variant_key
       AND s.suppression_reason IS NULL AND s.suppressed_at IS NULL
"""
_CART_SINGLE_SKU_SQL = """
    SELECT s.sku_key, s.source_variant_id, s.title AS variant_title, s.currency
      FROM catalog_skus s
     WHERE s.product_key = :product_key
       AND s.suppression_reason IS NULL AND s.suppressed_at IS NULL
     ORDER BY s.sku_key LIMIT 2
"""
_CART_SEED_VARIANT_SQL = """
    SELECT e.attached_variant_id, e.seed_data, e.destination_url, e.canonical_url
      FROM external_product_seeds e
     WHERE e.id = :seed_id AND e.status = 'active'
       AND e.attached_product_key = :product_key
       AND lower(e.domain) = :merchant_domain AND upper(e.market) = :market_country
"""
_CART_OFFER_SQL = """
    SELECT o.currency,
           CAST(coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price)
                AS TEXT) AS price
      FROM catalog_offers o
     WHERE o.product_key = :product_key AND o.sku_key = :sku_key
       AND o.merchant_id = :merchant_id
       AND o.suppression_reason IS NULL AND o.suppressed_at IS NULL
       AND coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) IS NOT NULL
       AND lower(coalesce(o.availability, 'unknown')) NOT IN
           ('out_of_stock', 'sold_out', 'unavailable')
     ORDER BY coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) ASC
     LIMIT 1
"""


async def _load_cart_link_item(
    *, merchant_domain: str, product_key: str, variant_key: Optional[str],
    market_country: str,
) -> Tuple[Dict[str, Any], str, str, Optional[str]]:
    """Return server-priced cart item, seller identity, and Shopify variant, or refuse.

    The buyer/agent supplies only catalog keys. No caller URL, price, Shopify variant, or seller
    identity is accepted. Mirrored seeds need an attached product and a sole variant proven by
    storefront evidence. Numeric operator input alone cannot authorize a purchase.
    """
    raw = await database.fetch_one(
        _CART_PRODUCT_SQL, {"product_key": product_key, "merchant_domain": merchant_domain}
    )
    if raw is None:
        raise svc.PurchaseRefused("row_not_found", "no catalog product for this domain and key")
    product = dict(raw)
    platform = str(product.get("platform") or "").strip().lower()
    if platform not in ("shopify", "external_seed"):
        raise svc.PurchaseRefused("row_not_shopify", "cart-link product has no Shopify source")

    if variant_key:
        raw_sku = await database.fetch_one(
            _CART_SKU_BY_KEY_SQL, {"product_key": product_key, "variant_key": variant_key}
        )
        if raw_sku is None:
            raise svc.PurchaseRefused("row_not_found", "no sku for this product and key")
        sku = dict(raw_sku)
    else:
        skus = [dict(row) for row in await database.fetch_all(
            _CART_SINGLE_SKU_SQL, {"product_key": product_key}
        )]
        if len(skus) != 1:
            raise svc.PurchaseRefused("row_not_found", "variant is ambiguous")
        sku = skus[0]

    if platform == "shopify":
        variant_id = extract_shopify_numeric_variant_id(sku.get("source_variant_id"))
        seller_ref = str(product.get("seller_ref") or product.get("merchant_id") or "").strip()
    else:
        if str(product.get("source_system") or "") != "external_product_seeds_mirror_v1":
            raise svc.PurchaseRefused("row_variant_unverified", "external seed source is unknown")
        # A mirror is a product-grain row. Even a caller-named SKU cannot make an arbitrary
        # choice among multiple offers agree with a sole storefront variant.
        mirror_skus = [dict(row) for row in await database.fetch_all(
            _CART_SINGLE_SKU_SQL, {"product_key": product_key}
        )]
        if len(mirror_skus) != 1 or mirror_skus[0]["sku_key"] != sku["sku_key"]:
            raise svc.PurchaseRefused("row_variant_unverified", "mirror variant is ambiguous")
        seed = await database.fetch_one(
            _CART_SEED_VARIANT_SQL,
            {"seed_id": product.get("source_ref"), "product_key": product_key,
             "merchant_domain": merchant_domain,
             "market_country": market_country},
        )
        seed = dict(seed) if seed else {}
        seed_data = seed.get("seed_data")
        if isinstance(seed_data, str):
            try:
                seed_data = json.loads(seed_data)
            except (TypeError, ValueError):
                seed_data = None
        variant_id = sole_verified_cart_variant_id(
            seed_data,
            product_urls=[seed.get("canonical_url") or seed.get("destination_url")],
            shop_domain=merchant_domain,
        )
        # An attachment naming another id is a contradiction, even if its string is all digits.
        # Never use it as a fallback: that is the numeric-SKU wrong-cart bug in the gateway.
        attached_id = str(seed.get("attached_variant_id") or "").strip()
        if attached_id and extract_shopify_numeric_variant_id(attached_id) != variant_id:
            raise svc.PurchaseRefused("row_variant_unverified", "seed identity contradicts storefront")
        seller_ref = str(product.get("seller_ref") or "").strip()
    if not variant_id:
        raise svc.PurchaseRefused("row_variant_unverified", "no verified Shopify numeric variant")
    if not seller_ref or seller_ref != str(product.get("merchant_id") or "").strip():
        raise svc.PurchaseRefused("seller_identity_unverified", "catalog seller identity is ambiguous")

    offer = await database.fetch_one(
        _CART_OFFER_SQL,
        {"product_key": product_key, "sku_key": sku["sku_key"],
         "merchant_id": seller_ref},
    )
    if offer is None:
        raise svc.PurchaseRefused("row_unpriced", "seller has no usable offer on this sku")
    offer = dict(offer)
    currency = str(offer.get("currency") or sku.get("currency") or "").strip().upper()
    if currency != _MARKET_CURRENCY.get(market_country):
        raise svc.PurchaseRefused("row_currency_mismatch", "offer currency differs from market")
    price_minor = ledger.amount_minor_or_none(offer.get("price"), currency)
    if not price_minor or price_minor <= 0:
        raise svc.PurchaseRefused("row_unpriced", "offer price is not an exact minor amount")

    # URL and click id are filled by the route after it mints a new click. These are facts only;
    # there is no URL-shaped caller input anywhere in this path.
    return (
        {"shop_domain": merchant_domain, "our_price_minor": int(price_minor),
         "currency": currency, "market_country": market_country,
         "product_name": str(product.get("product_title") or "").strip() or None,
         "product_key": product_key},
        seller_ref,
        variant_id,
        str(product.get("seed_kind") or "").strip() or None,
    )


async def _record_cart_link_click(
    *, click_id: str, cart_url: str, shop_domain: str, seller_ref: str,
    product_key: str, variant_key: Optional[str], agent_id: str,
    seed_kind: Optional[str],
) -> None:
    """Persist the click identity before a purchase can be opened; failure closes the door."""
    await database.execute(surface_click_events.insert().values(
        click_id=click_id, merchant_id=seller_ref if len(seller_ref) <= 50 else None,
        surface="reap_cart_link", commerce_surface="reap_cart_link",
        source_channel="agent", agent_id=agent_id if len(agent_id) <= 64 else None,
        destination_url=cart_url, dest_domain=shop_domain,
        context={"seller_ref": seller_ref, "seed_kind": seed_kind, "item_source": "cart_link",
                 "product_key": product_key, "variant_key": variant_key},
        impression_count=0, click_count=1, first_click_at=_now(), last_click_at=_now(),
    ))


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


def _request_hash(
    *,
    merchant_domain: str,
    product_key: str,
    variant_key: Optional[str],
    quantity: int,
    email: str,
    shipping_address: Mapping[str, Any],
    return_url: str,
    item_source: str = "reap_variant",
) -> str:
    """A stable fingerprint of the fields that DECIDE this purchase.

    WHAT IS IN IT is everything a buyer would notice being different: the merchant, the product,
    the variant, how many, who it goes to and where, and where they come back to afterwards.
    Change any of those and it is a different purchase, whatever key the caller reused.

    WHAT IS NOT IN IT: `click_context` (accepted and forwarded nowhere) and the idempotency key
    itself (it is the lookup, not part of what is being compared). Also nothing the ROUTE derives
    rather than the caller supplying — the price, the buyer ref, the click id — because those are
    ours and a client retrying an identical request must hash identically even though the click
    id will differ.

    BUILT FROM THE NORMALISED VALUES, not the raw body. The domain is already the canonical
    merchant (`_merchant_domain_key`: lowercased, one `www.` removed), the keys stripped, the
    address already through `_buyer_address_for_client` with its fallbacks applied. A retry that
    differs only in the casing of a domain, or in whether it carries the `www.`, or a
    missing-then-supplied `buyer.name` that resolves to the same recipient is the SAME request and must replay; a
    retry that differs in the address is not, and must not.

    `sort_keys=True` and a fixed separator so two dicts with the same content hash the same
    whatever order they were built in — the whole value of the column depends on that.
    The cart-link lane also includes `item_source`; the legacy variant lane keeps its original
    hash shape so existing idempotency keys retain their meaning across this rollout.
    """
    facts = {
        "merchant_domain": merchant_domain,
        "product_key": product_key,
        "variant_key": variant_key or "",
        "quantity": int(quantity),
        "email": email,
        "shipping_address": {str(k): str(v) for k, v in dict(shipping_address).items()},
        "return_url": return_url,
    }
    if item_source != "reap_variant":
        facts["item_source"] = item_source
    canonical = json.dumps(
        facts,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _replayed_purchase_id(
    *, agent_id: str, agent_user_ref_hash: str, idempotency_key: str, request_hash: str
) -> Optional[str]:
    """The purchase this key already opened, or None — and `idempotency_conflict` when the key
    was used for a DIFFERENT request.

    The conflict is raised rather than ignored because the alternative is the failure mode the
    column exists to stop: answering 202 with a purchase of something the caller did not just ask
    for. A client that derives keys from a session or a cart id WILL reuse one across two
    different bodies, and it will read the 202 as "done".
    """
    row = await database.fetch_one(
        """
        SELECT purchase_id, request_hash, created_at
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
            # OUTSIDE THE WINDOW THE KEY IS FORGOTTEN, and that includes the conflict check: the
            # row is about to be replaced, and refusing a caller for disagreeing with a
            # fingerprint we are no longer honouring would be the worst of both rules.
            return None
    stored_hash = str(record.get("request_hash") or "")
    if stored_hash and stored_hash != request_hash:
        raise svc.PurchaseRefused(
            "idempotency_conflict", "this key was used for a different request"
        )
    return str(record.get("purchase_id") or "").strip() or None


async def _claim_idempotency_key(
    *,
    agent_id: str,
    agent_user_ref_hash: str,
    idempotency_key: str,
    purchase_id: str,
    request_hash: str,
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
                agent_id, agent_user_ref_hash, idempotency_key, purchase_id, request_hash
            ) VALUES (
                :agent_id, :agent_user_ref_hash, :idempotency_key, :purchase_id, :request_hash
            )
            """,
            {
                "agent_id": agent_id,
                "agent_user_ref_hash": agent_user_ref_hash,
                "idempotency_key": idempotency_key,
                "purchase_id": purchase_id,
                "request_hash": request_hash,
            },
        )
        return purchase_id
    except Exception:  # noqa: BLE001
        pass

    # THE LOSER RE-READS, AND THE RE-READ CAN REFUSE. Two concurrent requests with one key and
    # two different bodies race here: the winner's hash lands, and this call raises
    # `idempotency_conflict` for the loser rather than handing it the winner's purchase. That is
    # the same answer the sequential case gives, which is the point — a race must not be a way to
    # get an answer the ordinary path refuses.
    winner = await _replayed_purchase_id(
        agent_id=agent_id,
        agent_user_ref_hash=agent_user_ref_hash,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
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


#: WHY THESE TWO HANDLERS TAKE A RAW `Request`.
#:
#: A parameter in the signature — `payload: Dict[str, Any] = Body(...)`, `limit: int = Query(le=100)`
#: — is validated by FastAPI BEFORE the handler body runs, and its refusal is a 400 naming the
#: field. So while the rail was dark, `POST` with a body of `[1, 2]`, or with the wrong
#: content-type, or `GET ?limit=500`, all answered 400 with a field message while every valid
#: request answered 404. That is a working probe for a feature that is supposed to be absent, and
#: it survives every test that only ever sends well-formed requests.
#:
#: Taking the raw request moves ALL parsing inside the handler, after the dial. Every shape of
#: input now gets the same 404 from a dark rail, byte for byte.
#:
#: WHAT IS STILL VISIBLE, and cannot be fixed here: `/openapi.json` lists these paths whatever the
#: dial says, because the routes are mounted at import and the schema is built from the router.
#: That is a deliberate trade — see the note on the mount in `main.py` — and it is documented on
#: the contract page rather than papered over.


@router.post("/purchases")
async def start_reap_purchase(
    request: Request,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    """Open a purchase and answer at once. MAKES NO PARTNER CALL — the poller does that."""
    try:
        _require_rail()
        agent_user_ref = _require_agent_user(agent_user)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            # A body that is not JSON at all, or an empty one. The parser's message can quote the
            # body, so it is not carried.
            raise svc.PurchaseRefused("invalid_request", "the request body is not JSON")
        if not isinstance(payload, dict):
            raise svc.PurchaseRefused("invalid_request", "the request body must be an object")

        try:
            req = StartPurchaseRequest.model_validate(payload)
        except ValidationError:
            # The pydantic error names fields AND the values that failed, and one of those fields
            # is the buyer's address. Never forwarded, never logged.
            raise svc.PurchaseRefused("invalid_request", "the request body did not validate")

        # The cart-link lane has two further dark gates. Decide them before consent or any
        # merchant/catalog read so a disabled lane is the same 404 fallback as the base rail.
        if req.item_source == "cart_link" and (
            not svc.is_cart_link_enabled() or not rc.supports_cart_link_quote()
        ):
            raise svc.PurchaseRefused("not_available_on_this_rail")

        # ── CONSENT, AND WHERE IT SITS IN THE ORDER ──────────────────────────────────────────
        #
        # AFTER THE DIAL, WHICH IS NOT A STYLE CHOICE. `_require_rail()` is the first statement
        # of this handler and everything about a dark rail depends on it staying first: a
        # `consent_required` answered before the dial is a 400 where every well-formed request
        # gets a 404, which is a working probe for a feature that is supposed to be absent. That
        # is the exact defect the note above these handlers records about `Body(...)` and
        # `Query(...)`, and moving this check one line up would reintroduce it. The mutant table
        # for this PR kills it in that direction specifically.
        #
        # BEFORE ELIGIBILITY, THE CATALOG READ AND EVERY WRITE, which is the other half. A buyer
        # who has not consented must not have an identity minted for them, must not have a
        # purchase opened, and must not be able to learn — by the shape of the refusal — which
        # merchants we have enabled or what is in our catalogue.
        consent_version = _consent_version(req.buyer.consent_version)

        # EVERY ROUTE-OWNED IDENTIFIER THROUGH ONE CHECKPOINT, before any of them can reach a
        # bind. `.lower()` after the check rather than before: the check is about what the string
        # CONTAINS, and lowercasing cannot add or remove an unprintable character.
        merchant_host = _identifier(
            req.merchant_domain, "merchant_domain", max_chars=255
        ).lower()
        # TWO VALUES FROM ONE FIELD, and which one each consumer gets is the whole change.
        #
        # `merchant_domain` is the canonical MERCHANT: what the variant lane's eligibility and
        # catalog reads compare against (each folding its stored column by the same rule), and
        # what the idempotency hash carries (`www.` and apex are one merchant, so one purchase).
        #
        # `merchant_host` is the storefront host as the door observed it, lowercased. It goes to
        # the purchase row, whose readers fold it (the resolver's `merchant_domain_matches`
        # against Reap's own spelling of the host, and the attribution close, which keys the
        # merchant canonically); to the Tier B verdict, whose reader folds
        # it too; and to the CART-LINK lane's catalog read and URL, whose host is compared
        # byte-for-byte downstream. The purchasability lookup is the exception — see there.
        #
        # Validated here, for BOTH lanes, so a host that is not a bare host name is refused
        # before any read.
        merchant_domain = _merchant_domain_key(merchant_host)
        product_key = _identifier(req.product_key, "product_key", max_chars=1024)
        variant_key = (
            _identifier(req.variant_key, "variant_key", max_chars=1024)
            if str(req.variant_key or "").strip()
            else None
        )
        market_country = str(req.buyer.shipping_address.country or "").strip().upper()
        if not _COUNTRY_RE.match(market_country):
            raise svc.PurchaseRefused("invalid_address", "country must be two letters")

        agent_id = str(context.agent_id)
        agent_user_ref_hash = hash_agent_user_ref(agent_user_ref)
        if not agent_user_ref_hash:
            raise svc.PurchaseRefused("agent_user_required")

        idempotency_key = (
            _identifier(req.idempotency_key, "idempotency_key", max_chars=128)
            if str(req.idempotency_key or "").strip()
            else None
        )
        # THE RETURN URL AND THE ADDRESS ARE RESOLVED HERE, BEFORE THE REPLAY LOOKUP, because both
        # are part of what the idempotency key is a key FOR. `start_purchase` validates the URL;
        # this line only decides which one it validates.
        shipping_address = _buyer_address_for_client(req.buyer)
        buyer_email = str(req.buyer.email or "").strip()
        return_url = str(req.return_url or "").strip() or _default_return_url()

        request_hash = _request_hash(
            merchant_domain=merchant_domain,
            product_key=product_key,
            variant_key=variant_key,
            quantity=int(req.quantity),
            email=buyer_email,
            shipping_address=shipping_address,
            return_url=return_url,
            item_source=req.item_source,
        )

        if idempotency_key:
            # Raises `idempotency_conflict` when this key was used for a different request.
            replayed = await _replayed_purchase_id(
                agent_id=agent_id,
                agent_user_ref_hash=agent_user_ref_hash,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
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
                    # THE CONSENT IS RECORDED ON A REPLAY TOO, and this is the whole reason
                    # `_linked_buyer_id` exists as a separate read.
                    #
                    # The contract page, the runbook and migration 227's header all say the tag
                    # is rewritten on EVERY purchase and is always the latest version the buyer
                    # accepted. Returning here without writing it made that false for exactly the
                    # requests a door retries — which is where a consent version most plausibly
                    # changes mid-flight. The prose was right and the code was wrong; this is the
                    # code catching up.
                    #
                    # A READ, NOT `_buyer_id_for`. A replay implies the buyer already exists, so
                    # the lookup finds them; using the minting version here would hand the replay
                    # path the power to CREATE an identity before eligibility has been checked,
                    # and a caller could then mint buyer rows by probing merchants we never
                    # enabled. Nothing is written when there is no link — there is nothing to
                    # record a consent against.
                    replay_buyer_id = await _linked_buyer_id(
                        agent_id=agent_id, agent_user_ref_hash=agent_user_ref_hash
                    )
                    if replay_buyer_id:
                        await _record_consent(replay_buyer_id, consent_version)
                    return JSONResponse(
                        status_code=202,
                        content={
                            "purchase_id": replayed,
                            "status": view.get("state"),
                            "poll_after_seconds": view.get("poll_after_seconds"),
                        },
                    )

        # PURCHASABILITY BEFORE EITHER LANE'S ELIGIBILITY, because it is the broader refusal:
        # both allowlists say a merchant is PERMITTED, and neither says its checkout can be PAID.
        # flowerbeauty.com was allowlisted, priced a UCP cart, advertised `dev.shopify.card` (a
        # platform constant every Shopify store repeats) and rendered a checkout whose only
        # payment method was PayPal, at a price we did not hold.
        #
        # DARK BY DEFAULT. With MERCHANT_PURCHASABILITY_ENFORCE off this is not consulted at all
        # and the rail behaves exactly as it did; with it on, a merchant with no fresh positive
        # fact FROM THE BUYER VANTAGE is refused. `is_purchasable` fails CLOSED on a database
        # error, which is the right direction for a payment gate even though it is the wrong one
        # for a liveness check.
        #
        # HANDED THE CANONICAL MERCHANT, because that is what the WRITER hands its own fold. The
        # sweep builds its population with `normalize_domain(<allowlist row's domain>)` and then
        # passes that already-folded value to `record_check`, which folds it again; `is_purchasable`
        # folds its argument the same way. So the fact is keyed fold(fold(spelling)), and the read
        # matches it exactly when it is handed fold(spelling) — `merchant_domain`. For every host
        # that does not begin `www.www.` the second fold is a no-op and the distinction vanishes;
        # for the one that does, handing the observed host here would read a key no sweep writes.
        if purchasability.is_enforcement_enabled():
            if not await purchasability.is_purchasable(merchant_domain, market_country):
                raise svc.PurchaseRefused(
                    "merchant_not_purchasable",
                    "no fresh positive purchasability fact for this domain and market",
                )

        # ELIGIBILITY BEFORE THE CATALOG READ on either lane. The daily Tier B verdict is
        # distinct from the variant rail's operator allowlist; it expires after 48 hours.
        row = None
        cart_facts = None
        cart_variant = None
        cart_seller = None
        cart_seed_kind = None
        if req.item_source == "cart_link":
            # THE OBSERVED HOST, NOT THE CANONICAL MERCHANT — see `merchant_host` above. The Tier B
            # verdict is keyed canonically by its own reader, so this is the same lookup either
            # way; the catalog read, the storefront evidence and the permalink are not.
            if not await tierb_eligibility.is_cart_link_eligible(
                merchant_host, market_country
            ):
                raise svc.PurchaseRefused("merchant_not_eligible")
            cart_facts, cart_seller, cart_variant, cart_seed_kind = await _load_cart_link_item(
                merchant_domain=merchant_host,
                product_key=product_key,
                variant_key=variant_key,
                market_country=market_country,
            )
        else:
            eligible = await _eligibility(
                merchant_domain=merchant_domain,
                market_country=market_country,
                product_key=product_key,
                variant_key=variant_key,
            )
            row = await _load_catalog_row(
                merchant_domain=merchant_domain,
                storefront_host=merchant_host,
                product_key=product_key,
                variant_key=variant_key,
                market_country=eligible.market_country,
                accept_variant_labels=eligible.accept_variant_labels,
                also_accept_domains=eligible.also_accept_domains,
            )

        buyer_id = await _buyer_id_for(
            agent_id=agent_id, agent_user_ref_hash=agent_user_ref_hash
        )
        buyer_ref = await _reap_buyer_ref(buyer_id, consent_version=consent_version)

        click_id = new_click_id()
        cart_link_item = None
        if cart_facts is not None:
            cart_url = build_shopify_cart_permalink(
                shop_domain=merchant_host, variant_id=cart_variant, click_id=click_id,
                quantity=int(req.quantity), country=market_country,
            )
            if cart_url is None:
                raise svc.PurchaseRefused("row_variant_unverified")
            await _record_cart_link_click(
                click_id=click_id, cart_url=cart_url, shop_domain=merchant_host,
                seller_ref=cart_seller, product_key=product_key, variant_key=variant_key,
                agent_id=agent_id, seed_kind=cart_seed_kind,
            )
            cart_link_item = svc.CartLinkItem(cart_url=cart_url, **cart_facts)

        purchase_id = await svc.start_purchase(
            agent_id=agent_id,
            agent_user_ref_hash=agent_user_ref_hash,
            buyer_ref=buyer_ref,
            row=row,
            cart_link=cart_link_item,
            consent_version=consent_version,
            # THE RETURN URL IS NOT VALIDATED BY THIS ROUTE. `start_purchase` runs
            # `rc.validate_return_url` on whatever it is handed, builds both stage variants from
            # it, and raises `PurchaseRefused("invalid_return_url")` — the same reason code this
            # route would have used. A copy of that check here would be a guard that can never be
            # the one that fires, and an unreachable guard reads as protection that does not
            # exist. One validator, and it is the one that owns the URL — including for the
            # default, so an operator who points `REAP_AGENTIC_RETURN_URL` at a host that is not
            # ours gets a refusal rather than an open redirector with a payment page in front.
            buyer=svc.BuyerContact(email=buyer_email, shipping_address=shipping_address),
            quantity=int(req.quantity),
            # MINTED HERE, NEVER TAKEN FROM THE REQUEST. This is the only join between a Reap
            # checkout and the session that started it, and a caller-supplied one could collide
            # with another caller's.
            click_id=click_id,
            return_url=return_url,
        )

        if idempotency_key:
            winner = await _claim_idempotency_key(
                agent_id=agent_id,
                agent_user_ref_hash=agent_user_ref_hash,
                idempotency_key=idempotency_key,
                purchase_id=purchase_id,
                request_hash=request_hash,
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


#: The list is capped whatever the caller asks for. The ledger clamps again at 500; this is the
#: tighter of the two, because a history page is a page.
_LIST_MAX = 100


def _limit_from(request: Request) -> int:
    """`?limit=` as an int in 1..`_LIST_MAX`, or `invalid_request`.

    Hand-parsed because the dial has to be checked first — see the note above the handlers. An
    absent parameter is the default; a present one that is not a number, or is out of range, is a
    refusal rather than a silent clamp, because a caller that asked for 500 and got 20 has been
    given a page it does not know is partial.
    """
    raw = request.query_params.get("limit")
    if raw is None or str(raw).strip() == "":
        return 20
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise svc.PurchaseRefused("invalid_request", "limit must be an integer")
    if not (1 <= value <= _LIST_MAX):
        raise svc.PurchaseRefused("invalid_request", f"limit must be 1..{_LIST_MAX}")
    return value


@router.get("/purchases")
async def list_reap_purchases(
    request: Request,
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    try:
        _require_rail()
        agent_user_ref = _require_agent_user(agent_user)
        agent_user_ref_hash = hash_agent_user_ref(agent_user_ref)
        if not agent_user_ref_hash:
            raise svc.PurchaseRefused("agent_user_required")
        # Parsed HERE, not in the signature: a `Query(le=...)` refusal is a 400 that fires before
        # the handler and told a prober the route exists while the rail was dark.
        limit = _limit_from(request)
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
