"""What of an external-seed product's `seed_data` may leave an agent-facing response.

WHY. `_build_external_seed_product` (routes/agent_api.py) attaches the stored external-seed blob to the
product it builds (`"seed_data": seed_data`). That blob is the row's internal working state: price-read
bookkeeping, variant refresh state, canonical resets, read provenance, review aggregates, recall
internals, attribution templates. In-process readers of the built product may need it, so it is not
stripped at build time; it is projected at the RESPONSE BOUNDARY of each route that serves the product.

WHAT. An ALLOWLIST, not a denylist, mirroring the gateway's invoke egress projection
(PIVOTA-Agent src/services/seedDataEgressProjection.js): a key added to seed_data later is dropped here
unless someone adds it below on purpose. Kept are only the fields a known consumer reads off a response:
  - image fallbacks: image_url / imageUrl / image / images / image_urls, at the top level and on
    snapshot (agent-ui card + PDP image fallbacks);
  - snapshot.description and derived.recall.retrieval_summary (agent-ui card text fallbacks);
  - variant identity + stock on variants[] and snapshot.variants[] (agent-ui variant badge reads
    option_name; the gateway's warm-handoff resolver reads ids + stock).
Values are reduced to SCALARS: a variant field must be a str/int/float/bool; a text field must be a
string; an image is a URL string, or an object cut down to its string URL keys. Nested objects under
an allowlisted name (a `stock` dict carrying its read source, say) are dropped.

COPY-ON-WRITE. The projection builds a new dict and never mutates its input, so the stored row and any
cache holding the built product keep the full blob. When nothing allowlisted is present the caller drops
the key rather than sending `{}`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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


def _scalar(value: Any) -> Optional[Any]:
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, _SCALAR_TYPES):
        return value
    return None


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _image_item(value: Any) -> Optional[Any]:
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, dict):
        kept = {k: value[k] for k in SEED_IMAGE_OBJECT_URL_FIELDS if _text(value.get(k)) is not None}
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
    """One seed_data value -> its allowlisted projection (a NEW dict), or None when nothing
    allowlisted is present (including a seed_data that is not an object at all)."""
    if not isinstance(seed_data, dict):
        return None
    out: Dict[str, Any] = {}
    _pick_images(seed_data, out)

    variants = _project_variants(seed_data.get("variants"))
    if variants:
        out["variants"] = variants

    snapshot_in = seed_data.get("snapshot")
    if isinstance(snapshot_in, dict):
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
    """A product dict -> a shallow copy whose `seed_data` is the allowlisted projection (dropped when
    empty). A product with no `seed_data` key is returned as the same object."""
    if not isinstance(product, dict) or "seed_data" not in product:
        return product
    out = dict(product)
    projected = project_seed_data(product.get("seed_data"))
    if projected is None:
        out.pop("seed_data", None)
    else:
        out["seed_data"] = projected
    return out
