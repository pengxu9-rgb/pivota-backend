"""Recover NUMERIC Shopify variant ids for crawled seeds, from the storefront's own
`/products/<handle>.js`.

WHY THIS EXISTS. `services/outbound_links_service.shopify_cart_base_url` builds
`https://{host}/cart/{numeric_variant}:{qty}` — the pre-filled cart that turns a cold
redirect into a handoff an agent can actually complete — and it refuses to fabricate a
variant id, so it returns None whenever the numeric id is unknown. Measured on prod
2026-08-21 over a 3,000-row sample of the serving corpus: only **28.0%** of rows carry a
variant with any id at all, while **42%** of the live products are genuinely multi-variant
(Size / Shade / Color / Format). So for most of the catalog we can name a product family
but not a purchasable variant.

The data is not hard to get — it is simply never collected. `services/external_offers_service`
parses HTML only (JSON-LD offers + a data-attribute SKU payload); it never asks the
storefront for `/products/<handle>.js`, which is a public, unauthenticated endpoint that
returns every variant with its numeric id. A live probe on 2026-08-21 recovered numeric ids
for **81 of 81** reachable Shopify PDPs.

WHAT THIS MODULE IS, AND WHAT IT IS NOT. The pure decision logic: URL derivation, `.js`
parsing, and the matching rule. It touches no database, no network and no serving path.

THE CONSUMER SHIPPED IN #1813 and lives at the bottom of this file: `storefront_is_shopify`
and `sole_stamped_variant_id`, read by `routes/agent_shop_gateway._external_seed_redirect_identity`
to flip a crawl seed's intake-lane label to a real `platform="shopify"` and prefill a cart
permalink. The producer that writes that evidence is `scripts/backfill_shopify_variant_ids.py`.

(An earlier revision of this paragraph said there was "deliberately no consumer yet" and that
the serving change had been "cut". That was true for about a day and then shipped, and the
note outlived it — the same stale-doc failure this codebase has been bitten by before. If you
are reading a claim like that here again, check it against the code before believing it.)

WHAT IT WRITES, AND WHERE. A NEW key, `shopify_variant_id`, on each EXISTING seed variant.

It stamps only; it never CREATES a variant array. An earlier version did, and that was wrong
in a way worth recording: the crawl cohort keeps its variants at
`seed_data["snapshot"]["variants"]` (scripts/onboard_external_brand_from_crawl.py,
scripts/backfill_crawl_seed_variants.py), while the serving readers
(`beauty_external_ranking._normalize_seed_variants`, `agent_api._seed_variants`,
`agent_sdk_fixed`) take TOP-LEVEL `variants` first and fall back to snapshot. So creating a
top-level array made a seed that already had good variants serve a poorer fabricated one —
no currency, no `variant_id`, "Default Title" as the display name — while the audit path
kept reading snapshot, and the same seed answered differently depending on who asked.
Authoring a variant array is a separate job with its own currency and readiness obligations;
see scripts/backfill_crawl_seed_variants.py, which already does it properly.

It does NOT touch the existing `variant_id`, which is read by services
(`catalog_variant_promoter`, `payment_offer_evidence_service`, `attached_seed_runtime_evidence`,
`pci_kb_scope_review`) that match it against catalog SKU identity. Overwriting a SKU-shaped
value with a Shopify numeric id would change what those matches mean — the same
similar-name-different-type foot-gun as `primary_recommendation_id` on the gateway.

THE MATCHING RULE REFUSES RATHER THAN GUESSES. A wrong variant id is worse than none: it
builds a cart URL that silently adds the wrong size and the buyer completes a purchase we
mis-specified. So a match is only made when it is unambiguous — see `match_variants`.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import parse_qs, urlparse, urlunparse

# Shopify caps a product at 100 variants; anything past that is not a Shopify product page.
MAX_VARIANTS = 100


def product_js_url(page_url: str) -> Optional[str]:
    """`https://brand.com/products/handle?variant=1` -> `https://brand.com/products/handle.js`

    Returns None when the URL is not a Shopify product page. Query and fragment are dropped:
    `?variant=` selects a variant for the RENDERER and changes nothing about the `.js`
    payload, which always lists every variant. A `/collections/x/products/handle` path is
    collapsed, because the collection prefix is presentational and the `.js` endpoint is
    served from the bare product path.
    """
    raw = str(page_url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    path = (parsed.path or "").rstrip("/")
    if path.endswith(".js"):
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    match = re.search(r"/products/([^/]+)$", path)
    if not match:
        return None
    handle = match.group(1)
    # A handle ending in .json/.js is already an API path someone half-built; normalize it.
    handle = re.sub(r"\.(json|js)$", "", handle)
    if not handle:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, f"/products/{handle}.js", "", "", ""))


def _norm_label(value: Any) -> str:
    """Fold a variant label for comparison: NFKD, casefold, collapse separators.

    Deliberately aggressive — `30 ml`, `30ML` and `30-ml` are the same option on a
    storefront, and treating them as different is what turns a matchable variant into a
    refusal. It is NOT aggressive enough to merge `30ml` and `50ml`: digits survive.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().strip()
    text = re.sub(r"[\s\-_/|,]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_product_js(payload: Any) -> List[Dict[str, Any]]:
    """Project a Shopify `/products/<handle>.js` body into the fields we need.

    `price` is in MINOR units in this payload (a documented Shopify shape), so it is
    converted once here rather than at each call site — the yen-amount-read-as-dollars
    class of bug starts with a minor-unit field crossing a boundary unconverted.
    A variant with no usable numeric id is dropped: it cannot serve the purpose.
    """
    if not isinstance(payload, dict):
        return []
    raw_variants = payload.get("variants")
    if not isinstance(raw_variants, list):
        return []

    out: List[Dict[str, Any]] = []
    for raw in raw_variants[:MAX_VARIANTS]:
        if not isinstance(raw, dict):
            continue
        numeric = _numeric_id(raw.get("id"))
        if not numeric:
            continue
        price_minor = raw.get("price")
        price = None
        if isinstance(price_minor, (int, float)) and not isinstance(price_minor, bool):
            price = round(float(price_minor) / 100.0, 2)
        options = [o for o in (raw.get("options") or []) if isinstance(o, str) and o.strip()]
        out.append(
            {
                "shopify_variant_id": numeric,
                "title": (str(raw.get("title")).strip() if raw.get("title") else None),
                "sku": (str(raw.get("sku")).strip() or None) if raw.get("sku") else None,
                "options": options,
                "price_amount": price,
                "available": bool(raw.get("available")),
            }
        )
    return out


def _numeric_id(value: Any) -> Optional[str]:
    """A bare numeric id, or the numeric tail of a `gid://shopify/ProductVariant/<n>`.

    Mirrors `outbound_links_service.extract_shopify_numeric_variant_id` on purpose: that
    function is what consumes the value, and a value it would reject is worthless here.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    text = str(value or "").strip()
    if not text:
        return None
    gid = re.search(r"gid://shopify/ProductVariant/(\d+)", text)
    if gid:
        return gid.group(1)
    return text if text.isdigit() else None


def _label_forms(value: Any) -> List[str]:
    """Both the spaced and the de-spaced fold of one label.

    `_norm_label` collapses separators to a single space, which makes `30 ml` and `30-ml`
    agree but still leaves them differing from `30ML` — a storefront writes all three for
    the same option, and refusing that match costs real coverage for no safety gain. Adding
    the whitespace-stripped form closes it WITHOUT weakening discrimination: digits survive
    both folds, so `30ml` and `50ml` can never collide.
    """
    spaced = _norm_label(value)
    if not spaced:
        return []
    tight = spaced.replace(" ", "")
    return [spaced] if tight == spaced else [spaced, tight]


def _candidate_labels(variant: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    for key in ("title", "display_label", "option_value", "size", "name"):
        value = variant.get(key)
        if value:
            labels.extend(_label_forms(value))
    for opt in variant.get("options") or []:
        labels.extend(_label_forms(opt))
    sku = variant.get("sku") or variant.get("variant_sku")
    if sku:
        labels.extend(_label_forms(sku))
    return [label for label in labels if label]


def match_variants(
    seed_variants: List[Dict[str, Any]],
    live_variants: List[Dict[str, Any]],
) -> Tuple[Dict[int, str], str]:
    """Map seed-variant INDEX -> numeric Shopify variant id, plus a reason code.

    THIS FUNCTION REFUSES RATHER THAN GUESSES, because a wrong id is worse than no id: it
    builds a cart URL that silently adds the wrong size, and the buyer completes a purchase
    we mis-specified. Only two situations are unambiguous enough to accept:

      `sole_variant`  — the seed has at most one variant and the storefront has exactly
                        one. There is nothing to confuse it with.
      `label_match`   — a seed variant's label set intersects exactly ONE live variant's
                        label set, and no other seed variant claims that same live variant.

    Everything else returns no mapping with a reason, so the caller can count WHY coverage
    is missing instead of reporting a bare failure. A partial result is honest: matched
    indices are returned even when siblings were ambiguous.
    """
    if not live_variants:
        return {}, "no_live_variants"

    # Deduplicate defensively: a storefront repeating an id would otherwise let one live
    # variant be claimed twice, which the uniqueness check below is meant to prevent.
    seen_ids: set = set()
    live: List[Dict[str, Any]] = []
    for item in live_variants:
        vid = item.get("shopify_variant_id")
        if not vid or vid in seen_ids:
            continue
        seen_ids.add(vid)
        live.append(item)
    if not live:
        return {}, "no_live_variants"

    if not seed_variants:
        # Checked BEFORE the sole-variant rule: `<= 1` matched the empty list too and
        # returned a mapping for index 0 of it, which was safe only because the one caller
        # happened to route empties elsewhere. Ordering the guard first removes the trap.
        return {}, "seed_has_no_variants"

    if len(seed_variants) == 1 and len(live) == 1:
        # Accepted on counts, without label agreement: when the storefront has exactly one
        # purchasable variant, it is the only cart URL this product can have, so a stale
        # seed label ("50ml" against a product now sold only as "30ml") does not make the
        # id wrong. Any ambiguity needs two candidates, and there are not two.
        return {0: live[0]["shopify_variant_id"]}, "sole_variant"

    live_labels = [set(_candidate_labels(item)) for item in live]
    proposed: Dict[int, int] = {}
    ambiguous = False
    for seed_index, seed_variant in enumerate(seed_variants):
        labels = set(_candidate_labels(seed_variant))
        if not labels:
            ambiguous = True
            continue
        hits = [i for i, live_label_set in enumerate(live_labels) if labels & live_label_set]
        if len(hits) == 1:
            proposed[seed_index] = hits[0]
        else:
            ambiguous = True

    # A live variant claimed by two seed variants means the labels do not discriminate;
    # dropping BOTH is the safe reading, since we cannot tell which is which.
    claim_counts: Dict[int, int] = {}
    for live_index in proposed.values():
        claim_counts[live_index] = claim_counts.get(live_index, 0) + 1
    resolved = {
        seed_index: live[live_index]["shopify_variant_id"]
        for seed_index, live_index in proposed.items()
        if claim_counts[live_index] == 1
    }
    if len(resolved) != len(proposed):
        ambiguous = True

    if not resolved:
        return {}, "no_confident_match"
    return resolved, "partial_label_match" if ambiguous else "label_match"


def stamp_variant_ids(
    seed_variants: List[Dict[str, Any]],
    live_variants: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (new_variants, report). Pure — never mutates its argument.

    Operates on a LIST, not on `seed_data`, so this module never has to know which of the
    two places a cohort keeps its variants. The caller owns that, and gets it wrong less
    often when the choice is explicit at the call site.

    Stamps only. A seed with no variants is returned untouched: authoring an array is a
    different job with currency and readiness obligations this function cannot meet (see the
    module docstring).

    An existing `shopify_variant_id` is never overwritten — a value already there was either
    verified or hand-set, and a later crawl is not better evidence than that.
    """
    existing = [dict(v) for v in (seed_variants or []) if isinstance(v, dict)]
    if not existing:
        return existing, {"action": "skipped", "reason": "seed_has_no_variants", "stamped": 0}

    mapping, reason = match_variants(existing, live_variants)
    stamped = 0
    for index, numeric in mapping.items():
        if existing[index].get("shopify_variant_id"):
            continue
        existing[index]["shopify_variant_id"] = numeric
        stamped += 1
    return existing, {
        "action": "stamped" if stamped else "unchanged",
        "reason": reason,
        "stamped": stamped,
    }


# ---------------------------------------------------------------------------------------------
# Consumer-side helpers: what the redirect lane reads.
#
# THE EVIDENCE CONTRACT. `platform` on a crawl seed records the INTAKE LANE
# ("external_seed"), not the software the storefront runs — and the cart-permalink gate in
# routes/agent_shop_gateway._make_external_redirect_url compares against exactly that label,
# so the whole crawl cohort (measured: 81/81 reachable PDPs are Shopify on custom domains)
# is refused a permalink it could serve. The missing fact is storefront platform, and a
# successful `/products/<handle>.js` parse IS definitive proof of it: only Shopify serves
# that endpoint in that shape. The producer therefore stamps, at parse-success time:
#
#     seed_data["snapshot"]["storefront_platform"]        = "shopify"
#     seed_data["snapshot"]["storefront_platform_source"] = "products_js_v1"
#
# and per matched variant, `shopify_variant_id` (see stamp_variant_ids). These helpers are
# the ONLY sanctioned readers. They are deliberately evidence-only: no CDN-host heuristics,
# no market defaults — the two shortcuts that manufactured this file's history of retracted
# claims.
# ---------------------------------------------------------------------------------------------

def storefront_is_shopify(seed_data: Any) -> bool:
    """True only on explicit stored evidence, never on inference.

    Two forms count, both producible solely by a successful `.js` parse: the stamped
    `storefront_platform` key, or any variant carrying a `shopify_variant_id` (stamped ids
    imply the parse succeeded even if the platform key predates the producer writing it).
    """
    if not isinstance(seed_data, dict):
        return False
    snapshot = seed_data.get("snapshot")
    if not isinstance(snapshot, dict):
        return False
    if str(snapshot.get("storefront_platform") or "").strip().lower() == "shopify":
        return True
    variants = snapshot.get("variants")
    if isinstance(variants, list):
        # NUMERIC stamped ids only. Round 4 found a live writer that lands arbitrary,
        # unvalidated variant keys in snapshot (scripts/recover_seed_data_from_catalog_extract
        # -> seed_data_writer, straight from an external extract service), so a junk
        # `shopify_variant_id: True` must not count as proof — false evidence here turns a
        # working PDP referral into a dead cart link on a non-Shopify storefront.
        return any(
            isinstance(v, dict) and _numeric_id(v.get("shopify_variant_id"))
            for v in variants
        )
    return False


def sole_stamped_variant_id(seed_data: Any) -> Optional[str]:
    """The stamped numeric id, but ONLY when the product itself has exactly one variant.

    The redirect is built at PRODUCT grain — the buyer has not chosen a variant — so
    prefilling a cart is only safe when there is nothing to choose. Round 4 caught the first
    version conflating two different facts: it required one distinct stamped ID, but
    `match_variants` deliberately supports a PARTIAL stamp (`partial_label_match`), so a
    three-variant product with one recovered id read as "sole" and would have prefilled an
    arbitrary variant of a multi-variant product — the exact wrong-size hazard this module
    exists to refuse. The unit is therefore the VARIANT ENTRY, not the recovered id: exactly
    one entry in the snapshot, and that entry stamped numeric. Anything else declines.
    """
    if not isinstance(seed_data, dict):
        return None
    snapshot = seed_data.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    variants = snapshot.get("variants")
    if not isinstance(variants, list):
        return None
    if len(variants) != 1 or not isinstance(variants[0], dict):
        return None
    sole = str(variants[0].get("shopify_variant_id") or "").strip()
    return sole if _numeric_id(sole) else None


#: The one provenance a cart proof may carry: a successful `/products/<handle>.js` fetch by
#: scripts/backfill_shopify_variant_ids.py, the ONLY writer of `snapshot.shopify_cart_proof`.
CART_PROOF_SOURCE = "products_js_v1"
#: `scope` of a proof that attests ONE NAMED variant of a (possibly multi-variant) product. The
#: original sole-variant proof carries NO `scope` key, and is left exactly as it was written.
CART_PROOF_SCOPE_NAMED = "named_variant"
#: The label `verified_cart_variant_id` reports for the unscoped sole-variant proof. A label for
#: callers only: it is never written into a proof.
CART_PROOF_SCOPE_SOLE = "sole_variant"
#: How old a proof may be. One number for both scopes.
CART_PROOF_MAX_AGE = timedelta(days=7)


def _cart_proof(seed_data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(seed_data, dict):
        return None
    snapshot = seed_data.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    proof = snapshot.get("shopify_cart_proof")
    return proof if isinstance(proof, dict) else None


def _cart_proof_fetch_is_trusted(
    proof: Dict[str, Any], *, product_urls: List[str], shop_domain: str, now: Optional[datetime],
) -> bool:
    """THE FETCH RULE, shared by both scopes: the proof came from a products.js fetch of THIS
    seed's own product URL, over https on the shop's own host (default port), and is fresh.

    `product_js_url` must be one of the seed's product URLs + `.js` (so a proof cannot be carried
    to another product), its scheme https and port 443, its host byte-equal to `shop_domain`, and
    `checked_at` a tz-aware ISO timestamp neither in the future nor older than
    `CART_PROOF_MAX_AGE`. Every scope-specific rule sits on top of this one; none restates it.
    """
    if proof.get("source") != CART_PROOF_SOURCE:
        return False
    proof_url = proof.get("product_js_url")
    if not isinstance(proof_url, str) or proof_url not in {
        product_js_url(url) for url in product_urls if url
    }:
        return False
    try:
        proof_origin = urlparse(proof_url)
        proof_host = proof_origin.hostname
        proof_port = proof_origin.port
    except ValueError:
        return False
    if (proof_origin.scheme != "https" or proof_port not in (None, 443)
            or proof_host != str(shop_domain or "").strip().lower()):
        return False
    try:
        checked_at = datetime.fromisoformat(str(proof.get("checked_at") or ""))
    except ValueError:
        return False
    if checked_at.tzinfo is None:
        return False
    current = now or datetime.now(timezone.utc)
    if checked_at > current or current - checked_at > CART_PROOF_MAX_AGE:
        return False
    return True


def sole_verified_cart_variant_id(
    seed_data: Any, *, product_urls: List[str], shop_domain: str,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Accept a mirrored cart id only with a fresh, same-fetch sole-storefront proof.

    A snapshot with one stamped entry is insufficient: a label match can stamp that entry
    while Shopify's live product has several variants. The dedicated proof is written only
    from a successful products.js response by the backfill, never inferred from seed labels.

    THE SOLE-VARIANT RULE: an UNSCOPED proof (no `scope` key -- a named-variant proof is never
    read as a sole one, even when its informational count happens to be 1), live_variant_count
    exactly the int 1, its variant the seed's sole stamped entry, and the shared fetch rule.
    """
    proof = _cart_proof(seed_data)
    if proof is None or "scope" in proof:
        return None
    if type(proof.get("live_variant_count")) is not int or proof["live_variant_count"] != 1:
        return None
    variant_id = sole_stamped_variant_id(seed_data)
    if not variant_id or _numeric_id(proof.get("variant_id")) != variant_id:
        return None
    if not _cart_proof_fetch_is_trusted(
        proof, product_urls=product_urls, shop_domain=shop_domain, now=now,
    ):
        return None
    return variant_id


def _url_named_variant_ids(product_urls: List[str], shop_domain: str) -> Optional[set]:
    """The distinct `variant=` values on the seed's own product URLs on the shop host.

    None means "the URLs name something unreadable" (a non-numeric or blank `variant=`) and
    refuses outright. A URL on another host is not the seed's storefront URL and names nothing.
    """
    host = str(shop_domain or "").strip().lower()
    named: set = set()
    for url in product_urls or []:
        if not url:
            continue
        try:
            parsed = urlparse(str(url))
            url_host = parsed.hostname
        except ValueError:
            return None
        if not host or url_host != host:
            continue
        for value in parse_qs(parsed.query, keep_blank_values=True).get("variant", []):
            if not (value.isascii() and value.isdigit()):
                return None
            named.add(value)
    return named


def named_cart_variant_id(
    seed_data: Any, *, product_urls: List[str], shop_domain: str,
) -> Optional[str]:
    """THE NAMING RULE: the ONE Shopify variant a seed names, or None.

    A seed names a variant through
      * its snapshot: exactly ONE variant entry, stamped with a numeric `shopify_variant_id`
        (`sole_stamped_variant_id`) -- a snapshot with two or more entries is a product-grain
        seed that names no single variant, and names NOTHING here even if a URL does; and/or
      * its own product URL(s) on the shop host: exactly one distinct numeric `variant=`.
    Both present -> they must AGREE, else None. Zero named, two named (two distinct URL values,
    or snapshot and URL disagreeing), or an unreadable `variant=` -> None.

    ACCEPT {1 entry stamped 4981}; {1 entry stamped 4981, url ?variant=4981}; {1 unstamped
    entry, url ?variant=4981}. REFUSE {2 entries}; {stamped 4981, url ?variant=4982};
    {url ?variant=1&variant=2}; {url ?variant=abc}; {1 unstamped entry, no variant= url}.

    Read by the backfill (to decide what to fetch-prove) AND by `verified_cart_variant_id` (to
    check the proof is about the variant the seed names), so the two can never disagree.
    """
    if not isinstance(seed_data, dict):
        return None
    snapshot = seed_data.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    variants = snapshot.get("variants")
    if isinstance(variants, list) and len(variants) > 1:
        return None
    stamped = sole_stamped_variant_id(seed_data)
    from_urls = _url_named_variant_ids(product_urls, shop_domain)
    if from_urls is None or len(from_urls) > 1:
        return None
    from_url = next(iter(from_urls)) if from_urls else None
    if stamped and from_url and stamped != from_url:
        return None
    return stamped or from_url


def named_verified_cart_variant_id(
    seed_data: Any, *, product_urls: List[str], shop_domain: str,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """THE NAMED-VARIANT RULE: a `scope: named_variant` proof whose variant is the one the
    seed names (`named_cart_variant_id`), `available` exactly True on the live products.js, and
    the shared fetch rule. `live_variant_count` is informational and NOT read here: the product
    may have any number of variants, because the seed -- not the product -- names the one to buy.
    """
    proof = _cart_proof(seed_data)
    if proof is None or proof.get("scope") != CART_PROOF_SCOPE_NAMED:
        return None
    named = named_cart_variant_id(seed_data, product_urls=product_urls, shop_domain=shop_domain)
    if not named or _numeric_id(proof.get("variant_id")) != named:
        return None
    if proof.get("available") is not True:
        return None
    if not _cart_proof_fetch_is_trusted(
        proof, product_urls=product_urls, shop_domain=shop_domain, now=now,
    ):
        return None
    return named


class ProvenCartVariant(NamedTuple):
    variant_id: str
    #: `CART_PROOF_SCOPE_SOLE` or `CART_PROOF_SCOPE_NAMED`. Callers gate on it: only a SOLE proof
    #: shows the product has one live variant, so only it may let a product-level (placeholder)
    #: price stand in for the variant's own.
    scope: str


def verified_cart_variant_id(
    seed_data: Any, *, product_urls: List[str], shop_domain: str,
    catalog_variant_id: Optional[str], now: Optional[datetime] = None,
) -> Optional[ProvenCartVariant]:
    """The Shopify variant a mirrored cart link may buy: the sole-variant proof, else the
    named-variant proof. The reap cart-link lane's one authority call.

    `catalog_variant_id` is the catalog's chosen numeric variant (None when the product carries
    only its `::canonical` placeholder). A SOLE proof keeps today's contract -- the caller refuses
    a catalog variant that differs, and a placeholder-only row is bought on the proof alone,
    because there is nothing else to buy. A NAMED proof is for a product that HAS other variants,
    so the catalog must name the SAME one explicitly: a placeholder-only row is refused here,
    since its product-level offer is not a price anybody vouched for on that one shade.

    Returns the variant AND the scope that proved it, or None.
    """
    sole = sole_verified_cart_variant_id(
        seed_data, product_urls=product_urls, shop_domain=shop_domain, now=now,
    )
    if sole:
        return ProvenCartVariant(sole, CART_PROOF_SCOPE_SOLE)
    named = named_verified_cart_variant_id(
        seed_data, product_urls=product_urls, shop_domain=shop_domain, now=now,
    )
    if named and named == catalog_variant_id:
        return ProvenCartVariant(named, CART_PROOF_SCOPE_NAMED)
    return None
