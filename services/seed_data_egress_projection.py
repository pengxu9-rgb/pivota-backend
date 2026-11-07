"""What of an external-seed product's `seed_data` may leave an agent-facing response.

WHY. `_build_external_seed_product` (routes/agent_api.py) attaches the stored external-seed blob to the
product it builds (`"seed_data": seed_data`). Part of that blob is the row's internal working state:
price-read bookkeeping, variant refresh state, canonical resets, read provenance, review/audit state,
manual overrides, attribution templates, seller refs. In-process readers of the built product may need
it, so it is not stripped at build time; it is projected at the RESPONSE BOUNDARY of each route that
serves the product. The two routes use two DIFFERENT rules, on purpose:

1. THE PRODUCT DETAIL ROUTE (GET /agent/v1/products/external_seed/{id}): `project_product_seed_data`,
   an ALLOWLIST. No gateway gate reads this route's seed_data (the gateway's external-seed detail lane
   reads its own DB rows), so only what a known response consumer reads is kept: gw#2548's key
   allowlist (PIVOTA-Agent src/services/seedDataEgressProjection.js) with STRICTER VALUES:
     - image fallbacks: image_url / imageUrl / image / images / image_urls, top level and snapshot
       (agent-ui card + PDP); an image is a URL string, or an object cut to its string URL keys;
     - snapshot.description and derived.recall.retrieval_summary (agent-ui card text), strings only;
     - variant identity + stock on variants[] and snapshot.variants[] (agent-ui variant badge, the
       gateway's warm-handoff resolver), scalars only.
   A key added to seed_data later is dropped unless someone adds it here on purpose.

2. THE SEARCH ROUTES (agent_search_products' three returns): `strip_products_internal_seed_data`, a
   DENYLIST. The gateway calls this search (invoke find_products, under the CALLER's key, so the backend
   cannot tell the gateway's hop from a partner's) and runs its gates over the results BEFORE its own
   egress projection (gw#2548): the retinoid/pregnancy safety net, the fragrance-free and category gates
   (seed_data.category_path / catalog_category_path), the "X-free" ingredient exclusion, the hold
   contracts (transaction_readiness_blocker_v1, non_merch_terminal_hold_v1, source_unavailable_v1), and
   readers of every string a variant carries. An allowlist here kept missing inputs those gates read and
   turned gated products into served ones (be#2727 review, P1-1/P1-2/P2-1). So search keeps EVERYTHING
   except INTERNAL_STATE_KEYS below, removed at every depth (seed_data, snapshot, variants[], any nested
   object). scripts/ops/seed_data_gateway_gate_equivalence.py runs the gateway's own gate functions on
   the full and the stripped product and fails unless every decision is identical.
   URLs are KEPT AS STORED. The gateway's retinoid net reads seed_data's external_url / canonical_url /
   destination_url as text (a slug, a relative path or a query can name the retinoid), so cutting them
   would weaken it (be#2730 review P1-1); and they publish nothing the product's own top-level
   destination_url / external_url (the same merchant URL, with its query) does not already. The one
   attribution secret, utm_template, is denied.
   A snapshot stored as JSON text is stripped inside the text and re-serialised ASCII-escaped (its type is
   kept, so the gateway, which never parses a string snapshot, reads it exactly as before minus the denied
   keys); text that is not a JSON object is dropped.

LONE SURROGATES. jsonb cannot hold a raw lone surrogate, but JSON TEXT inside it can hold the escape
(`\\ud83d`, what JS writes for a split emoji), and parsing that text yields one. A raw lone surrogate in a
response makes the JSON response fail to ENCODE after the route has returned -- a 500 for the whole page,
past every handler and fallback (be#2730 review P2-1). So every string either rule emits has lone
surrogates replaced (U+FFFD), and the re-serialised text snapshot is ASCII-escaped.

COPY-ON-WRITE. Both rules build new containers and never mutate their input, so the stored row and any
cache holding the built product (the external-seed search cache shares product objects across requests)
keep the full blob. When nothing is left the key is dropped rather than sent as `{}`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# =========================================================================================================
# 1. Detail route: allowlist
# =========================================================================================================

SEED_IMAGE_FIELDS = ("image_url", "imageUrl", "image", "images", "image_urls")
SEED_IMAGE_OBJECT_URL_FIELDS = ("url", "image_url", "imageUrl", "src")
SEED_SNAPSHOT_TEXT_FIELDS = ("description",)
SEED_VARIANT_FIELDS = (
    "variant_id",
    "variant_gid",
    "selected_variant_id",
    "id",
    "sku",
    "option_name",
    "stock",
    "availability",
    "availability_status",
)

_SCALAR_TYPES = (str, int, float, bool)


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _safe_str(value: str) -> str:
    """The string with every lone surrogate replaced by one U+FFFD, so the response can always encode.
    (Python never holds a VALID pair as two surrogates: json.loads joins an escaped pair into one char.)"""
    return _LONE_SURROGATE.sub("\ufffd", value)


def _object(value: Any) -> Optional[Dict[str, Any]]:
    """A dict, or a JSON-object string parsed into one (routes.agent_api._ensure_json_obj's rule: ANY
    parse failure -- a ValueError, or a RecursionError on pathologically deep text -- is "not an
    object", never an exception that would fail the whole response)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _scalar(value: Any) -> Optional[Any]:
    if isinstance(value, str):
        return _safe_str(value) if value.strip() else None
    if isinstance(value, _SCALAR_TYPES):
        return value
    return None


def _text(value: Any) -> Optional[str]:
    return _safe_str(value) if isinstance(value, str) and value.strip() else None


def _image_item(value: Any) -> Optional[Any]:
    if isinstance(value, str):
        return _safe_str(value) if value.strip() else None
    if isinstance(value, dict):
        kept = {k: _text(value.get(k)) for k in SEED_IMAGE_OBJECT_URL_FIELDS if _text(value.get(k)) is not None}
        return kept or None
    return None


def _image_value(value: Any) -> Optional[Any]:
    if isinstance(value, list):
        items = [item for item in (_image_item(v) for v in value) if item is not None]
        return items or None
    return _image_item(value)


def _pick_images(source: Dict[str, Any], out: Dict[str, Any]) -> None:
    for field in SEED_IMAGE_FIELDS:
        if field in source:
            kept = _image_value(source[field])
            if kept is not None:
                out[field] = kept


def _project_variants(variants: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(variants, list):
        return None
    projected: List[Dict[str, Any]] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        kept = {}
        for field in SEED_VARIANT_FIELDS:
            if field in variant:
                value = _scalar(variant[field])
                if value is not None:
                    kept[field] = value
        if kept:
            projected.append(kept)
    return projected or None


def project_seed_data(seed_data: Any) -> Optional[Dict[str, Any]]:
    """DETAIL ROUTE. One seed_data value -> its allowlisted projection (a NEW dict), or None when nothing
    allowlisted is present (including a seed_data that is not an object at all)."""
    if not isinstance(seed_data, dict):
        return None
    out: Dict[str, Any] = {}
    _pick_images(seed_data, out)

    variants = _project_variants(seed_data.get("variants"))
    if variants:
        out["variants"] = variants

    snapshot_in = _object(seed_data.get("snapshot"))
    if snapshot_in is not None:
        snapshot: Dict[str, Any] = {}
        _pick_images(snapshot_in, snapshot)
        for field in SEED_SNAPSHOT_TEXT_FIELDS:
            text = _text(snapshot_in.get(field))
            if text is not None:
                snapshot[field] = text
        snapshot_variants = _project_variants(snapshot_in.get("variants"))
        if snapshot_variants:
            snapshot["variants"] = snapshot_variants
        if snapshot:
            out["snapshot"] = snapshot

    derived = seed_data.get("derived")
    recall = derived.get("recall") if isinstance(derived, dict) else None
    summary = _text(recall.get("retrieval_summary")) if isinstance(recall, dict) else None
    if summary is not None:
        out["derived"] = {"recall": {"retrieval_summary": summary}}

    return out or None


def project_product_seed_data(product: Any) -> Any:
    """DETAIL ROUTE. A product dict -> a shallow copy whose `seed_data` is the allowlisted projection
    (dropped when empty). A product with no `seed_data` key is returned as the same object."""
    if not isinstance(product, dict) or "seed_data" not in product:
        return product
    out = dict(product)
    projected = project_seed_data(product.get("seed_data"))
    if projected is None:
        out.pop("seed_data", None)
    else:
        out["seed_data"] = projected
    return out


# =========================================================================================================
# 2. Search routes: denylist
# =========================================================================================================

#: Keys that carry the row's internal working state, removed at EVERY depth of a search response's
#: seed_data. Each is written by a backend writer and read by no gateway gate on the search hop:
#:   price-read bookkeeping   price_read, price_read_source, price_currency_source, price_currency_proof,
#:                            price_writeback (reap_price_writeback: carries a purchase id)
#:   refresh / read state     variant_refresh, variant_refresh_status, variant_refresh_last_write,
#:                            variant_census, canonical_reset, read_provenance, read_source,
#:                            refresh_status, refreshed_at, extracted_at, fetched_at
#:                            (routes/employee_products refresh + services/external_seed_catalogue_refresh)
#:   provenance stamps        description_source, description_refused_source, image_urls_source,
#:                            storefront_platform_source
#:   review / audit state     review_summary, review_status, audit, audit_quarantine
#:                            (employee_products._SEED_DATA_REVIEW_PRESERVE_KEYS)
#:   operator / attribution   manual_overrides, utm_template, seller_ref
#:   cart / matcher proofs    shopify_cart_proof, shopify_cart_variant_proofs (backfill_shopify_variant_ids),
#:                            variant_id_provenance, url_source (employee_products variant writers),
#:                            pdp_matcher (services/pdp_matcher/runner)
#: KNOWN, NOT DENIED: gateway-written state (external_seed_snapshot_contract, pdp_field_quality_summary,
#: provenance, review_required, identity_status, *_v1 contracts, reviewed_*_patch_v1, ...) -- some of it is
#: read by gateway gates, so each needs the equivalence check before it may be denied.
#: Adding a key here is safe only if no gateway gate reads it: the gate-equivalence script proves that
#: for the gates it runs.
INTERNAL_STATE_KEYS = frozenset(
    {
        "price_read",
        "price_read_source",
        "price_currency_source",
        "price_currency_proof",
        "price_writeback",
        "variant_refresh",
        "variant_refresh_status",
        "variant_refresh_last_write",
        "variant_census",
        "canonical_reset",
        "read_provenance",
        "read_source",
        "refresh_status",
        "refreshed_at",
        "extracted_at",
        "fetched_at",
        "description_source",
        "description_refused_source",
        "image_urls_source",
        "storefront_platform_source",
        "review_summary",
        "review_status",
        "audit",
        "audit_quarantine",
        "manual_overrides",
        "utm_template",
        "seller_ref",
        "shopify_cart_proof",
        "shopify_cart_variant_proofs",
        "variant_id_provenance",
        "url_source",
        "pdp_matcher",
    }
)
TEXT_JSON_FIELDS = frozenset({"snapshot"})

# A bound on nesting, far beyond any real seed_data, so a pathological blob can never exhaust the stack
# (and fail the whole search page). A subtree below it is dropped: nothing reads that deep.
_MAX_STRIP_DEPTH = 64


_DROP = object()


def _strip(value: Any, depth: int) -> Any:
    """A NEW value with every INTERNAL_STATE_KEYS key removed at any depth and URL fields cut; _DROP when
    the value itself must go."""
    if depth > _MAX_STRIP_DEPTH:
        return _DROP
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if key in INTERNAL_STATE_KEYS:
                continue
            if isinstance(key, str):
                key = _safe_str(key)
            if key in TEXT_JSON_FIELDS and isinstance(item, str):
                parsed = _object(item)
                if parsed is None:
                    continue  # text that is not a JSON object: nothing can be stripped inside it
                stripped = _strip(parsed, depth + 1)
                if stripped is not _DROP:
                    out[key] = json.dumps(stripped)  # ASCII-escaped: no raw lone surrogate
                continue
            stripped = _strip(item, depth + 1)
            if stripped is not _DROP:
                out[key] = stripped
        return out
    if isinstance(value, str):
        return _safe_str(value)
    if isinstance(value, list):
        items = []
        for item in value:
            stripped = _strip(item, depth + 1)
            if stripped is not _DROP:
                items.append(stripped)
        return items
    return value


def strip_internal_seed_data(seed_data: Any) -> Optional[Dict[str, Any]]:
    """SEARCH. One seed_data value -> a NEW dict without its internal state (see INTERNAL_STATE_KEYS), or
    None when it is not an object or nothing is left."""
    if not isinstance(seed_data, dict):
        return None
    stripped = _strip(seed_data, 0)
    return stripped if isinstance(stripped, dict) and stripped else None


def strip_product_internal_seed_data(product: Any) -> Any:
    """SEARCH. A product dict -> a shallow copy whose `seed_data` has no internal state (dropped when
    nothing is left). A product with no `seed_data` key is returned as the same object."""
    if not isinstance(product, dict) or "seed_data" not in product:
        return product
    out = dict(product)
    stripped = strip_internal_seed_data(product.get("seed_data"))
    if stripped is None:
        out.pop("seed_data", None)
    else:
        out["seed_data"] = stripped
    return out


def strip_products_internal_seed_data(products: Any) -> Any:
    """SEARCH. A list of products -> a new list, each product's `seed_data` stripped (copy-on-write per
    product; products without `seed_data` are the same objects). Anything not a list is returned
    unchanged. Search results come from the external-seed cache, whose product objects are shared with
    later requests, so the input list and its products are never mutated."""
    if not isinstance(products, list):
        return products
    return [strip_product_internal_seed_data(product) for product in products]
