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

WHAT THIS MODULE IS. The pure, testable half: URL derivation, `.js` parsing, and the
matching rule. The I/O half (selection, pacing, writes) is
`scripts/backfill_shopify_variant_ids.py`. Splitting them is deliberate — the matching rule
is where a wrong answer silently sends a buyer to the wrong size, and it should be provable
without a database or a network.

WHAT IT WRITES, AND WHERE. A NEW key, `shopify_variant_id`, on each seed variant. It does
NOT touch the existing `variant_id`, which is read by at least five services
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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

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

    if len(seed_variants) <= 1 and len(live) == 1:
        return {0: live[0]["shopify_variant_id"]}, "sole_variant"

    if not seed_variants:
        # No seed variants to attach to, but several live ones: the caller decides whether
        # to CREATE entries. Not this function's call — it only maps.
        return {}, "seed_has_no_variants"

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


def apply_variant_ids(
    seed_data: Dict[str, Any],
    live_variants: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (new_seed_data, report). Pure — never mutates its argument.

    Two behaviours, both additive:
      * seed already has variants -> stamp `shopify_variant_id` on the ones that matched.
      * seed has NO variants at all -> create entries from the storefront. This is the
        majority case (72% of serving rows carry no variant), and it is pure gain: there is
        no curated content to overwrite.

    An existing `shopify_variant_id` is never overwritten — a value already there was
    either verified or hand-set, and a later crawl is not better evidence than that.
    """
    data = dict(seed_data or {})
    existing = [dict(v) for v in (data.get("variants") or []) if isinstance(v, dict)]

    if not existing and live_variants:
        created = [
            {
                "shopify_variant_id": item["shopify_variant_id"],
                "title": item.get("title"),
                **({"sku": item["sku"]} if item.get("sku") else {}),
                **({"price_amount": item["price_amount"]} if item.get("price_amount") is not None else {}),
                "availability": "in_stock" if item.get("available") else "out_of_stock",
            }
            for item in live_variants[:MAX_VARIANTS]
        ]
        data["variants"] = created
        return data, {"action": "created", "reason": "seed_had_no_variants", "stamped": len(created)}

    mapping, reason = match_variants(existing, live_variants)
    stamped = 0
    for index, numeric in mapping.items():
        if existing[index].get("shopify_variant_id"):
            continue
        existing[index]["shopify_variant_id"] = numeric
        stamped += 1
    if stamped:
        data["variants"] = existing
    return data, {
        "action": "stamped" if stamped else "unchanged",
        "reason": reason,
        "stamped": stamped,
    }
