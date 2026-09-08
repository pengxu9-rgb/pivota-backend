"""Resolve one of our catalog rows to a Reap variant and ask Reap for a quote. No money moves here.

WHAT THIS IS. A client for Reap's agentic module: `products/search` -> `products/details` ->
`products/variant` -> `quotes`. Reap then opens a hosted approval page where THE BUYER enters
THEIR OWN card, and the outcome is read back by polling `GET /agentic/checkouts/{id}`.

    Pivota never deposits, prefunds, custodies, or is liable for a balance (constraint, 6 Sep).

That constraint is why this rail is the right one: the money leg is entirely between the buyer
and Reap. Nothing in this module authorises, captures, or funds anything. The Program-Funded
rail -- `services/reap_external_auth`, the card-issuance lane, the revocation sweep -- is dormant
BY DESIGN under the same constraint, and this is not it. This module deliberately stops at the
quote; checkout creation is a separate decision and a separate review.

WHY IT IS A REBUILD, AND WHAT THE PREVIOUS ONE GOT WRONG. PR #2136 was written from a
DESCRIPTION of this API rather than from the spec, and every load-bearing detail was wrong: it
posted to `/quotes` (404), it sent `ucpItemId` (the field is `variantId`), it sent `source`,
`merchant` and `attribution` blocks that are not fields at all, and it omitted the required
`Reap-Version` header. It could not have returned 200 from any host. Everything in this file is
taken from the published OpenAPI 3.1 document (`docs.reap.global/api-reference/openapi.json`,
read 8 Sep) and from ~40 real sandbox calls, and where something is still unverified it says so
in place rather than reading as settled.

THE CENTRAL PROBLEM THIS FILE SOLVES. Reap identifies a variant by ITS OWN opaque `var_...` id.
Our catalog holds the merchant's storefront variant id -- what the 8 Sep identity backfill
recovered -- and the two are unrelated namespaces. I previously told the user the backfill had
produced Reap's input; it had not. So a join has to be constructed, and it is the only part of
this file with any judgement in it:

    merchant domain -> `products[].merchant.name`
    product name    -> `products[].name`
    our variant TITLE -> `options[].values[].label` -> `optionId` -> POST products/variant

WHY NOT `defaultVariant`. Because it is AVAILABILITY-ORDERED, not the storefront default.
Measured: Fenty Eau de Parfum has one `Size` axis -- Standard $140.00 (unavailable at Reap),
Mini $95.00, Travel $39.00 -- and Reap previews Mini. Taking the default would have quoted a
different size at a 32% lower price while looking entirely successful, and reading the gap as
staleness would have "corrected" a row that was right. `select_option_ids` therefore fails
closed on every axis it cannot match, and no code path here falls back to a default variant.

FAIL CLOSED, EVERYWHERE. Every ambiguity in the join refuses instead of guessing: more than one
candidate product, a merchant whose name we cannot tie to our domain, an option axis with no
matching label, a details response that reports the product under `errors`. The cost of refusing
is a referral link the user already has. The cost of guessing is quoting a buyer for the wrong
physical object.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("reap_agentic_client")

#: Hosts a base URL may name, as a suffix match. From the OpenAPI `servers` block:
#: sandbox.api / prod.api / mx.sandbox.api / mx.prod.api -- all under reap.global, so this one
#: entry admits every real host and nothing else.
#:
#: The predecessor shipped ("reap.global", "reap.so", "reapfin.com"). I invented the last two.
#: A guessed entry here is not harmless padding: this list exists so that a mistyped
#: REAP_API_BASE_URL fails closed instead of delivering our API key to whatever host the typo
#: names, and every extra suffix widens exactly the set it exists to narrow. Add a host only
#: with a Reap document that names it.
ALLOWED_HOST_SUFFIXES = ("reap.global",)

#: Required header on EVERY agentic endpoint, and enum-constrained to this single value in the
#: spec. Its absence is a 4xx, not a default -- omitting it was one of #2136's four defects.
REAP_VERSION = "2025-02-14"

#: Required by the spec on quote and checkout creation (not on the read-only product endpoints).
_IDEMPOTENT_PATHS = ("/agentic/quotes", "/agentic/checkouts")

_DEFAULT_TIMEOUT_S = 12.0

#: Reap's own id prefixes, used to reject a value from the wrong namespace before it is sent.
#: This is the guard that would have caught #2136's central error: our storefront variant id
#: (`41669483823149`) does not look like `var_...`, and sending it asks Reap to price something
#: that does not exist in their catalog.
VARIANT_ID_PREFIX = "var_"
PRODUCT_ID_PREFIX = "prd_"


class ReapConfigError(RuntimeError):
    """Misconfiguration an operator must fix. Never retried, never degraded into 'unavailable'."""


class ReapRequestError(RuntimeError):
    """A request could not be built. Raised before egress, so it carries no partner data."""


# --- configuration ------------------------------------------------------------------------


def base_url() -> Optional[str]:
    return (os.getenv("REAP_API_BASE_URL") or "").strip().rstrip("/") or None


def _api_key() -> Optional[str]:
    """Never logged, never returned in an error, never placed in a URL or query string."""
    return (os.getenv("REAP_API_KEY") or "").strip() or None


def is_configured() -> bool:
    """Both halves or nothing happens. A base URL without a key is a stream of 401s at a partner;
    a key without a base URL is a credential sitting in an env var for no reason."""
    return bool(base_url() and _api_key())


def validate_base_url(raw: Optional[str] = None) -> str:
    """Return the base URL or raise. HTTPS + an allowlisted host, re-checked on every call.

    Checked at request time rather than import time: the value can then be corrected without a
    deploy, and a check that ran once at startup would keep passing for a value that has since
    changed.
    """
    url = (raw if raw is not None else base_url()) or ""
    if not url:
        raise ReapConfigError("REAP_API_BASE_URL is not set")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ReapConfigError(f"REAP_API_BASE_URL must be https, got {parsed.scheme or 'none'}")
    host = (parsed.hostname or "").lower()
    if not host or not any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES
    ):
        # The host is named because an operator has to fix it. The KEY never appears in any
        # message this module produces.
        raise ReapConfigError(f"REAP_API_BASE_URL host {host!r} is not an allowed Reap host")
    return url


def idempotency_key(scope: str, body: Dict[str, Any]) -> str:
    """Deterministic in the request body, so a retry after a timeout cannot create a second quote.

    Derived from the body rather than a fresh uuid because the dangerous retry is the one where
    we never saw the response: a new key there asks Reap for a second quote for the same cart.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return f"pivota-{scope}-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _headers(key: str, path: str, body: Dict[str, Any]) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Reap-Version": REAP_VERSION,
        "User-Agent": "Pivota/1.0 (+https://pivota.cc)",
    }
    if path in _IDEMPOTENT_PATHS:
        headers["Idempotency-Key"] = idempotency_key(path.rsplit("/", 1)[-1], body)
    return headers


# --- the join: pure, testable, and the only part with judgement in it -----------------------


def _norm(text: Any) -> str:
    """Casefold, strip accents-free punctuation and collapse whitespace. Used for MATCHING only;
    never for anything we send, so a normalisation bug cannot alter a request."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def merchant_domain_matches(reap_merchant_name: Any, our_domain: Any) -> bool:
    """Is Reap's `merchant.name` the merchant our row belongs to?

    Reap returns a display name, not a domain, and our catalog holds a domain. There is no
    identifier in common, so this compares the registrable-ish stem of the domain against the
    normalised name. It is deliberately strict about the direction: the stem must appear in the
    name, not the reverse, because a two-letter stem would otherwise match half their catalog.

    UNVERIFIED AT SCALE: measured on `fentybeauty.com` -> "Fenty Beauty" and `cosrx.com` ->
    "COSRX". A merchant whose display name shares no token with its domain will refuse here, and
    refusing is the right failure -- it produces a referral link, not a wrong quote. If that
    turns out to be common, the fix is a stored mapping, NOT a looser comparison.
    """
    domain = str(our_domain or "").strip().lower()
    if not domain:
        return False
    domain = re.sub(r"^(?:https?://)?(?:www\.)?", "", domain).split("/")[0]
    stem = _norm(domain.rsplit(".", 1)[0].split(".")[-1] if "." in domain else domain)
    stem = stem.replace(" ", "")
    if len(stem) < 3:
        return False
    name = _norm(reap_merchant_name).replace(" ", "")
    return bool(name) and stem in name


@dataclass
class ProductMatch:
    ok: bool
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    reason: Optional[str] = None
    #: Every candidate that survived the merchant filter. Populated on ambiguity so a caller can
    #: report WHAT it refused between rather than only that it refused.
    candidates: List[Dict[str, Any]] = field(default_factory=list)


def match_product(
    search_payload: Any, *, merchant_domain: str, product_name: str
) -> ProductMatch:
    """Pick the one Reap product that is our row, or refuse.

    Two filters, both required. The merchant filter is a correctness boundary: Reap's index is
    multi-merchant, and a name-only match happily returns another retailer's listing of the same
    branded item -- which we would then quote, price, and attribute to the wrong door.

    The name filter is exact-after-normalisation. It is intentionally not fuzzy: a search for
    "Fenty Eau de Parfum" returns seven products of which four are BUNDLES, and one of those
    bundles is priced at exactly our row's $140.00, so a "closest price" or "best prefix" rule
    would have selected it and looked like a perfect match. When more than one survives, this
    refuses and hands back the candidates.
    """
    data = search_payload if isinstance(search_payload, dict) else {}
    products = data.get("products")
    if not isinstance(products, list) or not products:
        return ProductMatch(ok=False, reason="no_products_returned")

    wanted = _norm(product_name)
    if not wanted:
        return ProductMatch(ok=False, reason="no_product_name_supplied")

    same_merchant = [
        p for p in products
        if isinstance(p, dict)
        and merchant_domain_matches((p.get("merchant") or {}).get("name"), merchant_domain)
    ]
    if not same_merchant:
        return ProductMatch(ok=False, reason="merchant_not_in_results")

    exact = [p for p in same_merchant if _norm(p.get("name")) == wanted]
    if not exact:
        return ProductMatch(
            ok=False, reason="no_exact_name_match",
            candidates=[{"id": p.get("id"), "name": p.get("name")} for p in same_merchant[:10]],
        )
    if len(exact) > 1:
        return ProductMatch(
            ok=False, reason="ambiguous_name_match",
            candidates=[{"id": p.get("id"), "name": p.get("name")} for p in exact[:10]],
        )

    product_id = str(exact[0].get("id") or "").strip()
    if not product_id.startswith(PRODUCT_ID_PREFIX):
        # Cheap namespace check. #2136's whole failure was sending an id from the wrong
        # namespace, and it went undetected because nothing ever looked at the shape.
        return ProductMatch(ok=False, reason="product_id_not_in_reap_namespace")
    return ProductMatch(ok=True, product_id=product_id, product_name=str(exact[0].get("name")))


@dataclass
class OptionMatch:
    ok: bool
    option_ids: List[str] = field(default_factory=list)
    #: Axis name -> the label we chose, so a caller can show a human what was matched.
    chosen: Dict[str, str] = field(default_factory=dict)
    reason: Optional[str] = None
    unmatched_axes: List[str] = field(default_factory=list)


def select_option_ids(detail_product: Any, wanted_labels: Sequence[str]) -> OptionMatch:
    """Turn our variant title into one `optionId` per axis, or refuse.

    EVERY axis must be matched. A product with Size and Color axes cannot be resolved from a
    title that only names a colour: the missing axis would have to be defaulted, and defaulting
    is precisely the mistake that made Reap's $95 Mini look like our $140 Standard. So an
    unmatched axis is a refusal, and the axis is named in the result.

    Availability is REPORTED, not enforced. `available: false` on the chosen value means Reap
    believes that size is unbuyable today; that is one third-party observation about a variant
    the merchant's own storefront may still list, and it belongs in the caller's decision, not
    in a silent substitution here.
    """
    product = detail_product if isinstance(detail_product, dict) else {}
    options = product.get("options")
    if not isinstance(options, list) or not options:
        # No axes at all: a single-variant product. There is nothing to resolve, and the caller
        # must use the variant the details response already carries rather than call
        # products/variant with an empty option list.
        return OptionMatch(ok=False, reason="product_has_no_option_axes")

    wanted = [_norm(w) for w in wanted_labels if _norm(w)]
    if not wanted:
        return OptionMatch(ok=False, reason="no_variant_title_supplied")

    chosen: Dict[str, str] = {}
    option_ids: List[str] = []
    unmatched: List[str] = []
    for axis in options:
        axis = axis if isinstance(axis, dict) else {}
        axis_name = str(axis.get("name") or "").strip() or "?"
        values = axis.get("values") if isinstance(axis.get("values"), list) else []
        hit = None
        for value in values:
            value = value if isinstance(value, dict) else {}
            label = _norm(value.get("label"))
            if label and label in wanted:
                if hit is not None and _norm(hit.get("label")) != label:
                    # Two different labels on ONE axis both matched our title. Refusing beats
                    # taking the first: the title genuinely does not determine this axis.
                    return OptionMatch(ok=False, reason=f"ambiguous_on_axis:{axis_name}")
                hit = value
        if hit is None:
            unmatched.append(axis_name)
            continue
        option_id = str(hit.get("optionId") or "").strip()
        if not option_id:
            return OptionMatch(ok=False, reason=f"value_has_no_option_id:{axis_name}")
        option_ids.append(option_id)
        chosen[axis_name] = str(hit.get("label"))

    if unmatched:
        return OptionMatch(
            ok=False, reason="axes_not_determined_by_title",
            unmatched_axes=unmatched, chosen=chosen,
        )
    return OptionMatch(ok=True, option_ids=option_ids, chosen=chosen)


def variant_title_tokens(title: Any) -> List[str]:
    """Split one of our variant titles into candidate axis labels.

    Shopify joins multi-axis titles with " / " ("Standard / Rose"); single-axis titles are the
    label itself. The whole title is kept as a candidate too, because a single-axis label can
    legitimately contain a slash.
    """
    raw = str(title or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    out = [raw] if raw not in parts else []
    return out + parts


# --- request bodies: every field name taken from the spec, none invented --------------------


def build_search_request(
    *,
    query: str,
    merchant_name: Optional[str] = None,
    merchant_mode: str = "PREFER",
    country: Optional[str] = None,
    currency: Optional[str] = None,
    available_only: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """`POST /agentic/products/search`.

    `merchantPreference.mode` is PREFER or ONLY. ONLY returned 503 in sandbox for every value
    tried, and every value produced a `MERCHANT_NOT_FOUND` warning, so the default here is
    PREFER and the caller is expected to enforce the merchant itself via `match_product` --
    which it must do regardless, because PREFER does not guarantee anything.
    """
    text = str(query or "").strip()
    if not text:
        raise ReapRequestError("search needs a query")
    body: Dict[str, Any] = {"query": text}
    if merchant_name:
        mode = str(merchant_mode or "PREFER").upper()
        if mode not in ("PREFER", "ONLY"):
            raise ReapRequestError(f"merchantPreference.mode must be PREFER or ONLY, got {mode!r}")
        body["merchantPreference"] = {"mode": mode, "merchantName": str(merchant_name)}
    context = {}
    if country:
        context["country"] = str(country)
    if currency:
        context["currency"] = str(currency)
    if context:
        body["context"] = context
    if available_only:
        body["filters"] = {"availability": "AVAILABLE_ONLY"}
    if limit:
        body["pagination"] = {"limit": int(limit)}
    return body


def build_details_request(product_ids: Sequence[str]) -> Dict[str, Any]:
    ids = [str(p).strip() for p in (product_ids or []) if str(p or "").strip()]
    if not ids:
        raise ReapRequestError("details needs at least one productId")
    return {"productIds": ids}


def build_variant_request(*, product_id: str, option_ids: Sequence[str]) -> Dict[str, Any]:
    pid = str(product_id or "").strip()
    if not pid:
        raise ReapRequestError("variant resolution needs a productId")
    ids = [str(o).strip() for o in (option_ids or []) if str(o or "").strip()]
    if not ids:
        raise ReapRequestError("variant resolution needs at least one optionId")
    return {"productId": pid, "optionIds": ids}


def build_quote_items(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`[{variantId, quantity}]`, both required by the spec.

    `variantId` must be Reap's own `var_...`. This refuses anything else BY SHAPE, which is the
    guard #2136 lacked: it sent our storefront variant id under a field name it had guessed, and
    nothing in the module was in a position to notice.
    """
    out: List[Dict[str, Any]] = []
    for raw in items if isinstance(items, (list, tuple)) else []:
        row = raw if isinstance(raw, dict) else {}
        variant_id = str(row.get("variantId") or "").strip()
        if not variant_id:
            raise ReapRequestError("each line item needs a variantId")
        if not variant_id.startswith(VARIANT_ID_PREFIX):
            raise ReapRequestError(
                f"variantId must be a Reap id ({VARIANT_ID_PREFIX}...), got {variant_id[:12]!r}; "
                "a storefront variant id is a different namespace and cannot be priced by Reap"
            )
        try:
            quantity = int(row.get("quantity", 1))
        except (TypeError, ValueError):
            raise ReapRequestError(f"quantity for {variant_id} is not an integer")
        if quantity < 1:
            # Refused rather than corrected to 1: a caller that computed 0 meant something, and
            # it was not "one".
            raise ReapRequestError(f"quantity for {variant_id} must be >= 1, got {quantity}")
        out.append({"variantId": variant_id, "quantity": quantity})
    if not out:
        raise ReapRequestError("a quote needs at least one line item")
    return out


#: Exactly the address fields in the spec. A key outside this set is DROPPED rather than passed
#: through, so a caller cannot widen what we hand a third party by adding keys to a dict.
_ADDRESS_REQUIRED = ("firstName", "lastName", "phone", "addressLine1", "city", "country")
_ADDRESS_OPTIONAL = ("addressLine2", "region", "postalCode")


def build_shipping_address(address: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Normalise a shipping address, or refuse an incomplete one.

    This is real buyer PII crossing to a third party, so it is whitelisted field by field and
    an incomplete address is a refusal rather than a partial send. `postalCode` and `region` are
    optional in the spec even for US addresses; that is Reap's call, not ours to tighten here.
    """
    if not address:
        return None
    if not isinstance(address, dict):
        raise ReapRequestError("shippingAddress must be an object")
    out: Dict[str, str] = {}
    missing = []
    for key in _ADDRESS_REQUIRED:
        value = str(address.get(key) or "").strip()
        if not value:
            missing.append(key)
        else:
            out[key] = value
    if missing:
        raise ReapRequestError(f"shippingAddress is missing required fields: {','.join(missing)}")
    for key in _ADDRESS_OPTIONAL:
        value = str(address.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def build_quote_request(
    *,
    items: Sequence[Dict[str, Any]],
    email: str,
    shipping_address: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """`POST /agentic/quotes`. Required: `items`, `email`.

    THERE IS NO ATTRIBUTION FIELD. Not "it does not survive checkout" -- it is not in the
    schema, and Reap accepts unknown keys silently with a 200 and drops them. So sending one
    would produce a request that looks like it carries attribution, a response that looks like a
    success, and no attribution anywhere. The only join available to us is client-side:
    `owner.reference` at checkout creation plus a query string on our own `returnUrl`, matched
    afterwards against Reap's `orderId` by polling, since agentic resources have no webhooks.
    That is a commercial conversation with Reap, and no code in this file can substitute for it.
    """
    address = str(email or "").strip()
    if not address or "@" not in address:
        raise ReapRequestError("a quote needs a buyer email")
    body: Dict[str, Any] = {"items": build_quote_items(items), "email": address}
    shipping = build_shipping_address(shipping_address)
    if shipping:
        body["shippingAddress"] = shipping
    return body


# --- transport ------------------------------------------------------------------------------


@dataclass
class ReapResponse:
    ok: bool
    status: Optional[int] = None
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    #: Reap returns `warnings[]` on search with a 200. `MERCHANT_NOT_FOUND` arrives this way for
    #: every merchantPreference value tried in sandbox, so a caller that reads only the status
    #: would record a clean success for a search that ignored its merchant scope entirely.
    warnings: List[str] = field(default_factory=list)


async def _post(path: str, body: Dict[str, Any], *, timeout_seconds: Optional[float] = None) -> ReapResponse:
    """One POST. Returns a result; raises only on misconfiguration.

    A network failure is a result rather than an exception because the caller is a serving path
    deciding whether to offer a rail, not a job that can fail. Misconfiguration DOES raise: a
    wrong host or a missing key is an operator error that must be visible rather than degrade
    quietly into "Reap is unavailable" on every request forever.
    """
    if not is_configured():
        return ReapResponse(ok=False, error="reap_client_not_configured")
    url = validate_base_url()
    key = _api_key() or ""
    timeout = float(timeout_seconds or os.getenv("REAP_API_TIMEOUT_SECONDS") or _DEFAULT_TIMEOUT_S)

    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{url}{path}", json=body, headers=_headers(key, path, body))
    except Exception as exc:  # noqa: BLE001
        # The exception TYPE only. Never the request: the body can carry a shipping address and
        # the headers carry the key, and an exception string is the easiest place for either to
        # end up in a log.
        logger.warning("reap %s failed: %s", path, type(exc).__name__)
        return ReapResponse(ok=False, error=f"transport_error:{type(exc).__name__}")

    if resp.status_code >= 400:
        # The response BODY is deliberately not logged or returned to a serving caller: a
        # partner's error payload can echo the request, and the request can contain a buyer's
        # address. Operators reproducing a failure should use the probe script, not prod logs.
        logger.warning("reap %s rejected: status=%s", path, resp.status_code)
        return ReapResponse(ok=False, status=resp.status_code, error=f"reap_status_{resp.status_code}")

    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return ReapResponse(ok=False, status=resp.status_code, error="unparseable_response")

    data = payload if isinstance(payload, dict) else {}
    warnings = [str(w) for w in data.get("warnings") or [] if w]
    if warnings:
        logger.info("reap %s returned warnings: %s", path, ",".join(sorted(set(warnings))[:5]))
    return ReapResponse(ok=True, status=resp.status_code, data=data, warnings=warnings)


async def search_products(**kwargs: Any) -> ReapResponse:
    timeout = kwargs.pop("timeout_seconds", None)
    return await _post("/agentic/products/search", build_search_request(**kwargs), timeout_seconds=timeout)


async def product_details(product_ids: Sequence[str], *, timeout_seconds: Optional[float] = None) -> ReapResponse:
    return await _post("/agentic/products/details", build_details_request(product_ids), timeout_seconds=timeout_seconds)


async def resolve_variant(*, product_id: str, option_ids: Sequence[str], timeout_seconds: Optional[float] = None) -> ReapResponse:
    return await _post(
        "/agentic/products/variant",
        build_variant_request(product_id=product_id, option_ids=option_ids),
        timeout_seconds=timeout_seconds,
    )


async def request_quote(**kwargs: Any) -> ReapResponse:
    timeout = kwargs.pop("timeout_seconds", None)
    return await _post("/agentic/quotes", build_quote_request(**kwargs), timeout_seconds=timeout)


def details_for(details_payload: Any, product_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Pull one product out of a details response, reading `errors[]` as well as `products[]`.

    The details endpoint is a BATCH: it returns 200 with a per-product `errors` array, so a
    product that could not be read arrives inside a successful response. Reading only `products`
    would turn that into "not found" and lose the code Reap gave us. (This is the same shape that
    made a route return a green 200 with `created=0` earlier this month.)
    """
    data = details_payload if isinstance(details_payload, dict) else {}
    for product in data.get("products") or []:
        if isinstance(product, dict) and str(product.get("id") or "") == product_id:
            return product, None
    for err in data.get("errors") or []:
        if isinstance(err, dict) and str(err.get("productId") or "") == product_id:
            return None, str(err.get("code") or "unknown_error")
    return None, "product_not_in_response"


# --- the orchestrator -----------------------------------------------------------------------


@dataclass
class VariantResolution:
    """What we learned trying to turn one of our catalog rows into a Reap variant."""

    ok: bool
    #: Reap's `var_...`. The ONLY value that may be sent as `items[].variantId`.
    variant_id: Optional[str] = None
    product_id: Optional[str] = None
    #: Reap's price for the variant we resolved, as `(amount, currency)`.
    price: Optional[Tuple[float, str]] = None
    #: Reap's availability for that variant. False is an observation about Reap's index, NOT
    #: evidence the merchant has delisted it -- their storefront may still list it, and one
    #: negative is not a delisting.
    available: Optional[bool] = None
    #: Set when Reap's price differs from the price on our row. Reported, never acted on here:
    #: a difference is as likely to be our staleness as theirs, and the caller owns that call.
    price_disagrees: bool = False
    matched_options: Dict[str, str] = field(default_factory=dict)
    #: Why we refused, in the vocabulary of the step that refused.
    reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    candidates: List[Dict[str, Any]] = field(default_factory=list)


async def resolve_our_row(
    *,
    merchant_domain: str,
    product_name: str,
    variant_title: Optional[str] = None,
    our_price: Optional[float] = None,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> VariantResolution:
    """search -> details -> variant, for one of our catalog rows. Fails closed at every step.

    This is the step PR #2136 had no seam for, and its absence is why that client could not have
    been repaired by renaming a field: it started from our id and there was nowhere to put the
    lookup that turns our id into theirs.

    `our_price` is optional and is used ONLY to set `price_disagrees`. It is never used to pick
    between candidates -- a "closest price" rule would have chosen the $140.00 Fenty gift-tray
    bundle over the $140.00 Standard perfume, which is the exact trap this design avoids.
    """
    found = await search_products(
        query=product_name, merchant_name=None, country=country, currency=currency,
        timeout_seconds=timeout_seconds,
    )
    if not found.ok:
        return VariantResolution(ok=False, reason=found.error or "search_failed")

    match = match_product(found.data, merchant_domain=merchant_domain, product_name=product_name)
    if not match.ok:
        return VariantResolution(
            ok=False, reason=f"search:{match.reason}",
            warnings=found.warnings, candidates=match.candidates,
        )

    detail = await product_details([match.product_id or ""], timeout_seconds=timeout_seconds)
    if not detail.ok:
        return VariantResolution(ok=False, reason=detail.error or "details_failed", warnings=found.warnings)
    product, err = details_for(detail.data, match.product_id or "")
    if product is None:
        return VariantResolution(ok=False, reason=f"details:{err}", warnings=found.warnings)

    options = select_option_ids(product, variant_title_tokens(variant_title))
    if not options.ok:
        # NOTE the thing NOT done here: there is no fallback to `defaultVariant`. It is
        # availability-ordered, so on the measured Fenty row it would have silently substituted
        # the $95 Mini for the $140 Standard and returned ok=True.
        return VariantResolution(
            ok=False, reason=f"options:{options.reason}",
            matched_options=options.chosen, warnings=found.warnings,
        )

    resolved = await resolve_variant(
        product_id=match.product_id or "", option_ids=options.option_ids,
        timeout_seconds=timeout_seconds,
    )
    if not resolved.ok:
        return VariantResolution(ok=False, reason=resolved.error or "variant_failed", warnings=found.warnings)

    variant = resolved.data
    variant_id = str(variant.get("id") or "").strip()
    if not variant_id.startswith(VARIANT_ID_PREFIX):
        return VariantResolution(ok=False, reason="resolved_id_not_in_reap_namespace", warnings=found.warnings)

    price_block = variant.get("price") if isinstance(variant.get("price"), dict) else {}
    amount = price_block.get("amount")
    price = (float(amount), str(price_block.get("currency") or "")) if isinstance(amount, (int, float)) else None
    disagrees = bool(
        price is not None and our_price is not None and abs(price[0] - float(our_price)) >= 0.01
    )
    return VariantResolution(
        ok=True, variant_id=variant_id, product_id=match.product_id, price=price,
        available=variant.get("available"), price_disagrees=disagrees,
        matched_options=options.chosen, warnings=found.warnings,
    )
