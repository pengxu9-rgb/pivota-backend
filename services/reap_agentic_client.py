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

A 200 IS NOT EVIDENCE REAP DID WHAT WE ASKED. Resolving an option value whose `available` flag
is false SILENTLY SUBSTITUTES a different variant: asking for `Size=Standard` ($140, unavailable)
returns 200 with the Mini variant at $95, no warning and no error field. So the response is
checked against the request every time -- `variant_matches_request` -- and a substitution is a
refusal. Same family as `defaultVariant` being availability-ordered, and the reason nothing in
this module reads a status code as an answer.

SEARCH IS QUERY-SENSITIVE AND NON-DETERMINISTIC, AND THAT BITES BEFORE ANY MATCHER RUNS. For one product: the
brand-led phrasing returns the merchant, the bare product name returns other merchants' listings
and not ours, and the fully specific phrasing returns nothing. `resolve_our_row` therefore tries
several phrasings, brand-led first, and records what it tried. On the strength of a single
bare-name search I previously reported that flowerbeauty.com was not in Reap's index. It is.
The identical query is also not repeatable -- it returned nothing for that merchant twice and
then returned the product half an hour later -- so no single pass, however many phrasings it
tries, can establish that something is ABSENT from the index. Refusals here are provisional.

REAP'S IDS ARE SESSION HANDLES, NOT IDENTITY. Five searches for the same product on one day
returned five different `prd_...` ids, each with its own `var_...` set. All stayed resolvable and
quotable for hours, so they are durable enough to carry through a checkout -- but a stored
(domain, product, title) -> `var_...` mapping is a session handle in a column that reads like a
foreign key. Resolve fresh, quote, discard; `VariantResolution.resolved_at` says how old a handle
is. This is the second time on this integration that an id looked more permanent than it was.

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
import time
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

#: Per-path read timeouts. NOT one number: the product endpoints answer in well under a second,
#: but a quote takes 13-16 s measured across nine merchants, because Reap is talking to the
#: merchant's own commerce layer while we wait. The 12 s default this module shipped with would
#: have timed out EVERY quote while every test passed -- a bound that only a live call can find.
_DEFAULT_TIMEOUT_S = 12.0
_QUOTE_TIMEOUT_S = 35.0
_SLOW_PATHS = ("/agentic/quotes", "/agentic/checkouts")


def default_timeout_for(path: str) -> float:
    return _QUOTE_TIMEOUT_S if path in _SLOW_PATHS else _DEFAULT_TIMEOUT_S

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


#: Reap retains an idempotency key for 24 h, but a quote's `expiresAt` is roughly 5 minutes. A
#: key derived from the body ALONE therefore replays a long-dead quote to the same cart the next
#: day -- the caller gets a 200 carrying an expired `expiresAt` and prices that may have moved.
#: Bucketing the key by time keeps the property we actually want (a retry after a lost response
#: does not mint a second quote) without the one we do not (a request tomorrow is not a retry).
#: Four minutes, so a replayed key can only ever return a quote that is still inside its window.
_IDEMPOTENCY_BUCKET_S = 240


def idempotency_key(
    scope: str,
    body: Dict[str, Any],
    *,
    now: Optional[float] = None,
    bucket_seconds: int = _IDEMPOTENCY_BUCKET_S,
) -> str:
    """Deterministic in the request body AND in a coarse time bucket.

    The retry this protects against is the one where we never saw the response: a fresh key there
    would ask Reap for a second quote for the same cart, and the body-derived part handles that.
    The bucket handles the opposite error, which the first version of this function had -- the
    same cart tomorrow is not a retry, and replaying yesterday's key returns yesterday's quote,
    already expired, with a 200 on it.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    bucket = int((now if now is not None else time.time()) // max(1, int(bucket_seconds)))
    material = f"{canonical}|{bucket}"
    return f"pivota-{scope}-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


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


def normalise_domain(value: Any) -> str:
    """Strip scheme, `www.`, path and case from a domain so two spellings of one host compare
    equal. Used for MATCHING only, never for anything we send."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    text = text.split("@")[-1].split(":")[0]
    return re.sub(r"^www\.", "", text).strip(".")


def merchant_domain_matches(reap_merchant_name: Any, our_domain: Any) -> bool:
    """Is Reap's `merchant.name` the merchant our row belongs to? An EXACT domain comparison.

    CORRECTED 8 Sep. An earlier version of this function compared the stem of our domain against
    what I believed was a display name ("fentybeauty.com" -> "Fenty Beauty"), because I had
    written down "Reap returns a display name, not a domain" without checking. `merchant.name`
    is the DOMAIN, and it is the only merchant key Reap returns. That mistake was not merely
    redundant work: stem-in-name matching also accepts `cosrx.com` for a merchant named
    `notcosrx.com`, which is a substring match on a security-relevant boundary.

    Exact and fail-closed. If a merchant ever legitimately fails this, the fix is a stored
    mapping, never a looser comparison.
    """
    ours = normalise_domain(our_domain)
    return bool(ours) and normalise_domain(reap_merchant_name) == ours


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
    search_payload: Any, *, merchant_domain: str, product_name: str,
    also_accept_domains: Sequence[str] = (),
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

    # `also_accept_domains` is the hook a stored mapping plugs into, and it is deliberately a
    # PARAMETER rather than a table in this file. Measured: 8 of 78 merchant names in Reap's
    # index carry a subdomain (`us.refybeauty.com`, `shop.simon.com`, `jbbwell.myshopify.com`,
    # ...), each of which fails against its apex spelling. Whether that bites depends on how our
    # merchant table spells those hosts, which this module cannot see -- so the caller supplies
    # the aliases it knows about, and the comparison itself stays exact. Loosening the comparison
    # to make these pass would also admit `notcosrx.com` for `cosrx.com`.
    accepted = [merchant_domain, *(also_accept_domains or ())]
    same_merchant = [
        p for p in products
        if isinstance(p, dict)
        and any(merchant_domain_matches((p.get("merchant") or {}).get("name"), d)
                for d in accepted)
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
    #: Axis name -> `available` on the value we chose, read from `options[].values[]`.
    #: Carried because the previous version's docstring CLAIMED availability was "reported, not
    #: enforced" while nothing anywhere reported it -- the flag was read and dropped. A docstring
    #: describing a behaviour the code does not have is worse than silence: it is a false claim
    #: that survives review.
    chosen_available: Dict[str, Optional[bool]] = field(default_factory=dict)
    #: Axes whose chosen value Reap marks unavailable. Non-empty means `/variant` MUST NOT be
    #: called: it will substitute an available sibling and answer 200.
    unavailable_axes: List[str] = field(default_factory=list)


def select_option_ids(detail_product: Any, wanted_labels: Sequence[str]) -> OptionMatch:
    """Turn our variant title into one `optionId` per axis, or refuse.

    EVERY axis must be matched. A product with Size and Color axes cannot be resolved from a
    title that only names a colour: the missing axis would have to be defaulted, and defaulting
    is precisely the mistake that made Reap's $95 Mini look like our $140 Standard. So an
    unmatched axis is a refusal, and the axis is named in the result.

    AVAILABILITY IS NOW ACTUALLY REPORTED. The previous version of this docstring said it was
    "reported, not enforced" -- and nothing reported it: `values[].available` was read past and
    dropped, so a caller had no way to know the value it asked for was unbuyable. Worse, the
    `available` a caller then saw came from the SUBSTITUTED variant, so an unavailable Standard
    surfaced as an available Mini. Both the flag and the list of unavailable axes are carried out
    of here now.

    An unavailable axis is fatal to the `/variant` call, not merely informational: measured, Reap
    answers 200 with an available sibling rather than the id we asked for, so the id we want does
    not exist to be fetched. `resolve_our_row` refuses before making that call.
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
    chosen_available: Dict[str, Optional[bool]] = {}
    option_ids: List[str] = []
    unmatched: List[str] = []
    unavailable: List[str] = []
    for axis in options:
        axis = axis if isinstance(axis, dict) else {}
        axis_name = str(axis.get("name") or "").strip() or "?"
        values = axis.get("values") if isinstance(axis.get("values"), list) else []
        if len(values) == 1 and isinstance(values[0], dict):
            # A SINGLE-VALUE AXIS IS DETERMINED. There is nothing for a title to choose between,
            # so requiring the title to match its label protects nothing and refuses real rows:
            # measured on flowerbeauty.com's "Petal Pout Lip Color", where Reap indexes the
            # per-shade page as its own product with one `Shade` axis carrying one value,
            # `"Flamingo Flirt - Cream"` -- a label our title ("Flamingo Flirt") cannot match
            # because it carries a finish suffix we do not store.
            #
            # Structural, like the option-less rule, and safe for the same reason: product
            # identity is already pinned by (merchant, exact product name), and with one value
            # there is no sibling to be substituted for. Note that `chosen` records REAP's label,
            # not our title, so the substitution check downstream compares against what we
            # actually asked for.
            single = values[0]
            option_id = str(single.get("optionId") or "").strip()
            if not option_id:
                return OptionMatch(ok=False, reason=f"value_has_no_option_id:{axis_name}")
            option_ids.append(option_id)
            chosen[axis_name] = str(single.get("label"))
            availability = single.get("available")
            chosen_available[axis_name] = availability if isinstance(availability, bool) else None
            if availability is False:
                unavailable.append(axis_name)
            continue

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
        availability = hit.get("available")
        chosen_available[axis_name] = availability if isinstance(availability, bool) else None
        if availability is False:
            unavailable.append(axis_name)

    if unmatched:
        return OptionMatch(
            ok=False, reason="axes_not_determined_by_title",
            unmatched_axes=unmatched, chosen=chosen, chosen_available=chosen_available,
        )
    return OptionMatch(ok=True, option_ids=option_ids, chosen=chosen,
                       chosen_available=chosen_available, unavailable_axes=unavailable)


def variant_matches_request(variant: Any, chosen: Dict[str, str]) -> Optional[str]:
    """Does the variant Reap returned actually have the options we asked for? Returns a refusal
    reason, or None if it matches.

    THIS IS THE MOST IMPORTANT GUARD IN THE FILE, and it exists because the API does not behave
    the way its status code suggests. Measured 8 Sep: resolving an option value whose
    `available` flag is false SILENTLY SUBSTITUTES a different variant. Asking for
    `Size=Standard` ($140, unavailable) returns **200** with `id: var_bb8b...`, `name: "Mini"`,
    `options: [{"name": "Size", "value": "Mini"}]`, price $95. No warning. No error field. A
    caller that trusts the 200 -- as this module did when it was first written -- quotes the
    buyer for a different physical object at a 32% lower price and has a clean success in hand.

    It is the same family as `previewVariant` being availability-ordered, and it is why nothing
    here may treat "Reap returned 200" as "Reap did what we asked". The response is checked
    against the request, every time.
    """
    data = variant if isinstance(variant, dict) else {}
    got = {
        str(o.get("name") or ""): str(o.get("value") or "")
        for o in data.get("options") or [] if isinstance(o, dict)
    }
    for axis, label in chosen.items():
        if axis not in got:
            return f"response_missing_axis:{axis}"
        if _norm(got[axis]) != _norm(label):
            # Named in full because this is the case a human has to be able to see at a glance.
            return f"substituted_on_axis:{axis}:asked={label}:got={got[axis]}"
    return None


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


#: How many search phrasings `resolve_our_row` will try before giving up. Bounded because
#: search takes up to ~9 s: three attempts is a ~27 s worst case before the quote's own 13-16 s,
#: which is already at the edge of what a serving path can spend. Raise it in a batch job, not
#: in a request path.
MAX_SEARCH_ATTEMPTS = 3


def search_queries(
    *, product_name: str, brand: Optional[str] = None, category: Optional[str] = None
) -> List[str]:
    """The phrasings to try, best first.

    REAP'S SEARCH IS QUERY-SENSITIVE, and this bites before any matcher runs. Measured on one
    product: "Flower Beauty lip color" returns flowerbeauty.com; "Petal Pout Lip Color" -- the
    bare product name, which is what this module used to send -- returns two hits, NEITHER from
    flowerbeauty.com; "Flower Beauty Petal Pout" returns six, none; and the fully specific
    "Petal Pout Lip Color Flamingo Flirt" returns zero. So a `merchant_not_in_results` refusal
    is evidence about the QUERY at least as much as about the index: on the strength of the bare
    name I previously concluded flowerbeauty.com was not indexed by Reap at all, and it is.

    Brand-led first, because that is the phrasing that worked. Deduplicated, order preserved.

    THE THIRD SLOT IS THE RECALL PLAY, and it is the one that found the merchant. Measured on
    flowerbeauty.com's "Petal Pout Lip Color": the brand-led and bare-name phrasings both missed
    the merchant entirely, and only "Flower Beauty lip color" surfaced it. An earlier version
    generated that phrasing ONLY when a caller supplied `category` -- so a caller without a
    category (most of them) lost the phrasing most likely to work, and a resolver that had the
    right rule refused the row anyway.

    WHEN THERE IS NO CATEGORY, THE BRAND ALONE TAKES THE SLOT -- AND THAT FALLBACK IS UNVERIFIED
    AGAINST THE LIVE API. The reasoning is that Reap answers a bare brand with a slice of that
    merchant's catalogue, which `match_product` then filters on exact name, making it a broader
    net rather than a looser match. That is a PREDICTION about Reap's behaviour, not an
    observation: every live run that found this merchant used `<brand> <category>`. Do not read
    this paragraph as evidence the fallback works. If it turns out not to, the fix is for callers
    to supply a category -- which for our rows is derivable from the catalog -- and not to widen
    the matching.

    AND THE SAME PHRASING IS NOT REPEATABLE, WHICH MAKES THE WHOLE LADDER NECESSARY RATHER THAN
    BELT-AND-BRACES. Reap's search is non-deterministic: the identical query returned zero hits
    for this merchant twice at ~19:15 and ~19:20 and then returned the product at ~19:45. Two
    runs of the SAME row against the SAME code an hour apart succeeded on DIFFERENT rungs -- the
    second phrasing once, the third the next time. So the later phrasings are not tie-breakers
    for an unusual row; they are what makes any given run land at all, and a caller that supplies
    less than the full ladder is not trading a little recall, it is coin-flipping.

    It also means `queries_tried` records what was SENT, never what re-sending would return, and
    that no single pass -- however many phrasings -- establishes that something is absent from
    the index. A `merchant_not_in_results` refusal is provisional and worth retrying later.
    """
    name = str(product_name or "").strip()
    brand_text = str(brand or "").strip()
    category_text = str(category or "").strip()
    if not name and not brand_text:
        raise ReapRequestError("a search needs at least a product name or a brand")
    broad = ""
    if brand_text:
        broad = f"{brand_text} {category_text}".strip() if category_text else brand_text
    ordered = [
        f"{brand_text} {name}".strip() if brand_text else "",
        name,
        broad,
    ]
    out: List[str] = []
    for query in ordered:
        query = query.strip()
        if query and query not in out:
            out.append(query)
    return out


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
    #: 503 AGENTIC_SERVICE_UNAVAILABLE on a QUOTE. Set as an inference, not a fact: measured on
    #: nine merchants, the two that 503 (laurageller.com, shop.simon.com) are the two that are
    #: not UCP merchants, and Reap's own FAQ scopes coverage to UCP merchants. n=2, so this is a
    #: hypothesis with a mechanism, not a proven rule -- treat it as "probably not completable
    #: via Reap", record it, and do not use it to suppress a merchant permanently on one sample.
    merchant_probably_not_completable: bool = False
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
    timeout = float(timeout_seconds or os.getenv("REAP_API_TIMEOUT_SECONDS") or default_timeout_for(path))

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
        return ReapResponse(
            ok=False, status=resp.status_code, error=f"reap_status_{resp.status_code}",
            merchant_probably_not_completable=(resp.status_code == 503 and path in _SLOW_PATHS),
        )

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
    #:
    #: DO NOT PERSIST THIS ON A CATALOG ROW. Measured 8 Sep: five searches for the same product
    #: on the same day returned five different `prd_...` ids, each with its own `var_...` set.
    #: All of them stay resolvable and quotable for hours, so they are durable HANDLES -- but
    #: they are not an identity, and a stored (domain, product, title) -> `var_...` mapping is
    #: storing a session handle in a column that reads like a foreign key. Resolve fresh, quote,
    #: discard. `resolved_at` is here so a caller can see how old a handle is.
    variant_id: Optional[str] = None
    product_id: Optional[str] = None
    #: Epoch seconds at which the ids above were minted by Reap.
    resolved_at: Optional[float] = None
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
    #: Axis -> Reap's `available` flag on the value WE ASKED FOR, not on whatever came back.
    chosen_available: Dict[str, Optional[bool]] = field(default_factory=dict)
    #: Why we refused, in the vocabulary of the step that refused.
    reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    #: True when the product has no option axes and the single variant was taken from
    #: `defaultVariant`. The ONE legitimate use of that field -- see `resolve_our_row`.
    single_variant_product: bool = False
    #: Set when the quote leg reported 503. See `ReapResponse.merchant_probably_not_completable`.
    merchant_probably_not_completable: bool = False
    #: Every search phrasing attempted, in order. On a refusal this says whether the query was
    #: ever the problem -- which, for this API, it often is.
    queries_tried: List[str] = field(default_factory=list)


async def resolve_our_row(
    *,
    merchant_domain: str,
    product_name: str,
    variant_title: Optional[str] = None,
    brand: Optional[str] = None,
    category: Optional[str] = None,
    our_price: Optional[float] = None,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    also_accept_domains: Sequence[str] = (),
    max_search_attempts: int = MAX_SEARCH_ATTEMPTS,
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
    # Several phrasings, because Reap's search is query-sensitive and a bare product name is the
    # phrasing measured to MISS. `merchant_not_in_results` on one query is not evidence the
    # merchant is absent from the index -- I drew exactly that wrong conclusion about
    # flowerbeauty.com from a single bare-name search.
    queries = search_queries(product_name=product_name, brand=brand, category=category)
    tried: List[str] = []
    found: Optional[ReapResponse] = None
    match: Optional[ProductMatch] = None
    for query in queries[:max(1, int(max_search_attempts))]:
        tried.append(query)
        found = await search_products(
            query=query, merchant_name=None, country=country, currency=currency,
            timeout_seconds=timeout_seconds,
        )
        if not found.ok:
            # A transport or status failure is not a phrasing problem; retrying phrasings would
            # just spend the budget on the same error.
            return VariantResolution(ok=False, reason=found.error or "search_failed",
                                     queries_tried=tried,
                                     merchant_probably_not_completable=found.merchant_probably_not_completable)
        match = match_product(found.data, merchant_domain=merchant_domain,
                              product_name=product_name,
                              also_accept_domains=also_accept_domains)
        if match.ok:
            break

    if found is None or match is None or not match.ok:
        return VariantResolution(
            ok=False, reason=f"search:{match.reason if match else 'no_query_attempted'}",
            queries_tried=tried,
            warnings=found.warnings if found else [],
            candidates=match.candidates if match else [],
        )

    detail = await product_details([match.product_id or ""], timeout_seconds=timeout_seconds)
    if not detail.ok:
        return VariantResolution(ok=False, reason=detail.error or "details_failed", warnings=found.warnings, queries_tried=tried)
    product, err = details_for(detail.data, match.product_id or "")
    if product is None:
        return VariantResolution(ok=False, reason=f"details:{err}", warnings=found.warnings, queries_tried=tried)

    axes = product.get("options") if isinstance(product.get("options"), list) else []
    if not axes:
        # A product with NO axes has exactly one variant, and `/agentic/products/variant` cannot
        # be used for it at all: an empty `optionIds` is a 422 ("expected array to have >=1
        # items"). So `defaultVariant` is read directly here.
        #
        # This is the one legitimate read of that field and it is not a weakening of the rule
        # above it. The rule refuses `defaultVariant` as a FALLBACK -- as a substitute for a
        # variant we asked for and could not resolve -- because it is availability-ordered among
        # SIBLINGS. With no siblings there is no ordering and no substitution possible: the
        # default is the product. The guard that keeps these apart is `axes` being empty, not a
        # judgement call at the call site.
        default = product.get("defaultVariant") if isinstance(product.get("defaultVariant"), dict) else {}
        variant_id = str(default.get("id") or "").strip()
        if not variant_id.startswith(VARIANT_ID_PREFIX):
            return VariantResolution(ok=False, reason="single_variant_product_has_no_variant_id",
                                     warnings=found.warnings, queries_tried=tried)
        price = _price_of(default)
        return VariantResolution(
            ok=True, variant_id=variant_id, product_id=match.product_id, price=price,
            available=default.get("available"), single_variant_product=True,
            resolved_at=time.time(), queries_tried=tried,
            price_disagrees=_disagrees(price, our_price), warnings=found.warnings,
        )

    options = select_option_ids(product, variant_title_tokens(variant_title))
    if not options.ok:
        # NOTE the thing NOT done here: there is no fallback to `defaultVariant`. It is
        # availability-ordered, so on the measured Fenty row it would have silently substituted
        # the $95 Mini for the $140 Standard and returned ok=True.
        return VariantResolution(
            ok=False, reason=f"options:{options.reason}",
            matched_options=options.chosen, warnings=found.warnings, queries_tried=tried,
        )

    if options.unavailable_axes:
        # Do not call `/variant`. Reap does not return the id of an unavailable value; it answers
        # 200 with an available sibling, and the previous version took that answer, relabelled it
        # from its own request, and reported the sibling's lower price as drift on our row.
        #
        # This is a DIFFERENT refusal from "we could not resolve it", and the distinction is
        # commercially real: the variant exists and our row is right, Reap just will not sell it
        # today. The referral link still works, and nothing about our row should be corrected.
        axis = options.unavailable_axes[0]
        return VariantResolution(
            ok=False,
            reason=f"options:value_unavailable_at_reap:{axis}:{options.chosen.get(axis)}",
            matched_options=options.chosen, chosen_available=options.chosen_available,
            available=False, warnings=found.warnings, queries_tried=tried,
        )

    resolved = await resolve_variant(
        product_id=match.product_id or "", option_ids=options.option_ids,
        timeout_seconds=timeout_seconds,
    )
    if not resolved.ok:
        # 400 and 422 mean different things on this endpoint: 422 is a malformed body (ours to
        # fix), 400 AGENTIC_REQUEST_REJECTED is Reap declining to resolve (theirs). Both are
        # carried through in the status so the distinction survives to whoever reads it.
        return VariantResolution(ok=False, reason=resolved.error or "variant_failed",
                                 warnings=found.warnings, queries_tried=tried)

    variant = resolved.data
    variant_id = str(variant.get("id") or "").strip()
    if not variant_id.startswith(VARIANT_ID_PREFIX):
        return VariantResolution(ok=False, reason="resolved_id_not_in_reap_namespace",
                                 warnings=found.warnings, queries_tried=tried)

    # A 200 is not evidence Reap did what we asked. See `variant_matches_request`.
    mismatch = variant_matches_request(variant, options.chosen)
    if mismatch:
        return VariantResolution(ok=False, reason=f"variant:{mismatch}",
                                 matched_options=options.chosen,
                                 chosen_available=options.chosen_available,
                                 warnings=found.warnings, queries_tried=tried)

    price = _price_of(variant)
    return VariantResolution(
        ok=True, variant_id=variant_id, product_id=match.product_id, price=price,
        available=variant.get("available"), price_disagrees=_disagrees(price, our_price),
        resolved_at=time.time(),
        matched_options=options.chosen, chosen_available=options.chosen_available,
        warnings=found.warnings, queries_tried=tried,
    )


def _price_of(variant: Any) -> Optional[Tuple[float, str]]:
    """`price.amount` is a decimal in MAJOR units as a JSON number, not a minor-unit integer."""
    block = (variant or {}).get("price") if isinstance(variant, dict) else None
    block = block if isinstance(block, dict) else {}
    amount = block.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    return float(amount), str(block.get("currency") or "")


def _disagrees(price: Optional[Tuple[float, str]], our_price: Optional[float]) -> bool:
    return bool(price is not None and our_price is not None
                and abs(price[0] - float(our_price)) >= 0.01)


def quote_total(quote_payload: Any) -> Optional[Tuple[float, str]]:
    """`amountBreakdown.finalAmount` as `(amount, currency)`.

    Provided because the breakdown has ONE field nested a level deeper than its siblings:
    `itemsSubtotal.amount`, `shipping.amount` and `finalAmount.amount` are flat, but tax is
    `tax.amount.amount` (alongside `tax.includedInPrices`). A caller summing the parts by a
    uniform rule silently drops or mis-reads tax.
    """
    data = quote_payload if isinstance(quote_payload, dict) else {}
    breakdown = data.get("amountBreakdown") if isinstance(data.get("amountBreakdown"), dict) else {}
    return _price_of({"price": breakdown.get("finalAmount")})


def quote_tax(quote_payload: Any) -> Optional[Tuple[float, str]]:
    """The one amount nested a level deeper than the others: `amountBreakdown.tax.amount.amount`."""
    data = quote_payload if isinstance(quote_payload, dict) else {}
    breakdown = data.get("amountBreakdown") if isinstance(data.get("amountBreakdown"), dict) else {}
    tax = breakdown.get("tax") if isinstance(breakdown.get("tax"), dict) else {}
    return _price_of({"price": tax.get("amount")})


# --- what a refusal means -------------------------------------------------------------------
#
# An ORDERED LIST of (prefix, explanation), not a dict. That is not a style preference: the
# probe script held this as a dict literal, I added a second `"options"` key to it, and Python
# silently kept the LAST one -- so the careful explanation I had just written was dead code and
# the script printed a placeholder for the one refusal that most needs explaining. A duplicate
# key in a dict literal is not an error in any linter we run. A list cannot hide one, because
# `explain_refusal` returns the FIRST match and a test asserts every reason resolves to distinct
# non-placeholder copy.
#
# Longest prefixes first, so a specific reason wins over its step.
REFUSAL_EXPLANATIONS: List[Tuple[str, str]] = [
    ("options:value_unavailable_at_reap",
     "The variant EXISTS and our row is right. Reap declines to sell that value today, and\n"
     "asking /variant for it returns 200 with an available sibling -- so we stop before the\n"
     "call. The referral link still works, and NOTHING about our row should be corrected: this\n"
     "is the case where a naive price comparison would 'fix' a correct price to a wrong one."),
    ("options:axes_not_determined_by_title",
     "The product was found; our variant title did not pin every option axis. This one is ours\n"
     "to fix -- either our title lacks a qualifier Reap's label carries, or the product has an\n"
     "axis our catalog does not record. Defaulting here is what would substitute a cheaper\n"
     "sibling for the variant we meant."),
    ("options:no_variant_title_supplied",
     "The product has option axes and we passed no variant title, so there is nothing to\n"
     "resolve against. Pass the title from the row."),
    ("options:product_has_no_option_axes",
     "Unexpected: a product with no axes should have been handled by the single-variant path.\n"
     "If you see this, the details response changed shape."),
    ("options:ambiguous_on_axis",
     "Two different labels on ONE axis both matched our title. The title genuinely does not\n"
     "determine that axis; taking the first would be a guess."),
    ("search:merchant_not_in_results",
     "Reap's index did not yield this merchant's product under any phrasing tried (listed\n"
     "above). Reap's search is QUERY-SENSITIVE: the bare product name is measured to miss\n"
     "products the brand-led phrasing finds, so this is evidence about the QUERY at least as\n"
     "much as about the index. DO NOT conclude the merchant is unindexed from it -- that is\n"
     "exactly how flowerbeauty.com got written off. Reap's search is also NON-DETERMINISTIC:\n"
     "the identical query returned nothing for that merchant twice and then returned the\n"
     "product half an hour later, so a single pass establishes nothing about absence. If\n"
     "`merchant.name` carries a subdomain (8 of 78 do), pass it via `also_accept_domains`\n"
     "rather than loosening the comparison."),
    ("search:ambiguous_name_match",
     "More than one product on the right merchant has exactly our product name. Refusing beats\n"
     "picking: the candidates are listed above."),
    ("search:no_exact_name_match",
     "The merchant was found but no product name matched exactly. Candidates are listed above --\n"
     "check whether Reap's name carries a qualifier ours does not, or vice versa."),
    ("search:product_id_not_in_reap_namespace",
     "The matched product's id is not a Reap `prd_...`. Refused before going further: an id from\n"
     "the wrong namespace is how the predecessor of this module failed."),
    ("search:no_products_returned",
     "The search returned zero products for every phrasing tried."),
    ("variant:substituted_on_axis",
     "Reap returned 200 for a DIFFERENT variant than the one we asked for. This is the silent\n"
     "substitution; the reason line names what we asked for and what came back. Refusing is\n"
     "correct -- quoting it would price the wrong physical object."),
    ("variant:response_missing_axis",
     "Reap's variant response did not carry an axis we asked about, so we cannot confirm it is\n"
     "the variant we wanted."),
    ("details:", "The product id resolved but details refused it. The code shown is Reap's."),
    ("single_variant_product_has_no_variant_id",
     "An option-less product whose `defaultVariant.id` is not a Reap `var_...`. Do not send it."),
    ("resolved_id_not_in_reap_namespace",
     "Reap returned an id that is not a `var_...`. Refused rather than passed into a quote."),
    ("reap_status_503",
     "Reap's search index is far wider than its checkout coverage. Measured: the merchants that\n"
     "503 on a quote are the ones that are not UCP merchants. Treat as per-merchant, not as an\n"
     "outage -- but n is small, so record it rather than suppressing the merchant."),
    ("transport_error",
     "A network failure, not a verdict about the merchant or the product. Retry."),
    ("reap_client_not_configured",
     "REAP_API_BASE_URL and REAP_API_KEY are not both set. Nothing was sent."),
]


def explain_refusal(reason: Optional[str]) -> str:
    """One paragraph on what a `VariantResolution.reason` actually means, or a fallback.

    Lives here rather than in the probe script because it is a statement about REAP'S
    behaviour -- the same vocabulary any caller has to interpret -- and because a copy that
    sits next to the code emitting the reasons is the copy that gets updated when they change.
    """
    text = str(reason or "")
    for prefix, explanation in REFUSAL_EXPLANATIONS:
        if text.startswith(prefix):
            return explanation
    return "Unrecognised refusal. Read the reason string above and the module that emits it."
