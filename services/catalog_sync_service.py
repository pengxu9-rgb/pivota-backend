from __future__ import annotations

import asyncio
import hashlib
import os
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select

from db.catalog import (
    beauty_compatibility_rules,
    beauty_content_assets,
    beauty_product_profiles,
    beauty_shades,
    beauty_sku_ingredients,
    beauty_usage_guides,
    catalog_field_facts,
    catalog_inventory_snapshots,
    catalog_merchants,
    catalog_offer_incentive_links,
    catalog_offers,
    catalog_payment_incentives,
    catalog_price_snapshots,
    catalog_products,
    catalog_quote_snapshots,
    catalog_skus,
    catalog_sync_events,
    catalog_sync_jobs,
    catalog_incentive_rules,
)
from db.database import database
from db.merchant_onboarding import merchant_onboarding
from db.products import products_cache
from models.catalog import PaymentIncentiveInput
from models.standard_product import StandardProduct, StandardProductVariant
from services.catalog_identity import make_content_key
from services.category_kind import resolve_category_kind
from services.vertical_profiles import (
    is_vertical_unresolved,
    normalize_category,
    resolve_vertical,
    summarize_unresolved_vertical,
)
from services.product_group_autogrouper import ensure_singleton_group_membership
from services.fashion_field_extractor import (
    EXTRACTION_SOURCE_LLM,
    batch_extract_fashion_fields,
)
from services.fashion_field_payload_extractor import (
    extract_care_from_payload,
    extract_material_from_payload,
    extract_size_guide_from_payload,
)
from services.pdp_category_classifier import (
    fold_category_from_variants,
    fold_category_with_llm_fallback,
)
from services.catalog_offer_writer_guard import (
    WriterAuditAccumulator,
    make_batch_id,
    validate_catalog_offer_rows,
    write_writer_audit_log,
)
from services.pdp_lifecycle import compute_lifecycle_stage
from services.pdp_taxonomy import derive_taxonomy_v1
from services.catalog_row_trust_upserter import (
    upsert_catalog_row_trust,
    upsert_catalog_row_trust_many,
)
from services.commerce_index_delta_service import record_field_change_and_publications
from services.commerce_index_v2 import (
    FieldObservation,
    commerce_index_v2_enabled_for_merchant,
)
from services.commerce_index_source_service import resolve_active_catalog_source
from services.strong_identifier import (

    MPN_CAPTURED_AS_BARCODE,
    NO_STRONG_IDENTIFIER,
    StrongIdentifier,
    extract_strong_identifier,
)


logger = logging.getLogger(__name__)

# ADR-024: the value written to catalog_offers.market when we do NOT know the
# store's country. Spelled as a constant so the write site, its provenance key
# and the tests all name the same thing, and so a future real-market derivation
# has one place to replace.
MARKET_UNKNOWN_DEFAULT = "US"

# Marker written into catalog_offers.offer_payload alongside the above. Its
# PRESENCE means "market is the column default, not an observation".
#
# Note on the constants: inlining either literal at the write site is an
# EQUIVALENT mutant (same bytes written) -- they buy one place to change when a
# real market derivation lands, not behavior, so no test pins their use.
MARKET_PROVENANCE_PLATFORM_DEFAULT = "platform_default_unknown"


# Bounds concurrent in-flight live-ingest fashion enrichment LLM calls so a
# big-merchant sync (e.g. 1000 fashion products) doesn't fan out into 1000
# parallel Deepseek requests. 8 is conservative; raise after staging load
# test confirms headroom. Per feedback_llm_call_multipliers.md, new LLM
# call sources need bounded blast radius before going live.
_FASHION_ENRICH_SEM = asyncio.Semaphore(8)

# Trust gate floor — values below this confidence won't be persisted into
# the merchant-facing fashion columns. Mirrors the PIVOTA-Agent gateway's
# pdpBuilder.pickFashionMeta cutoff (0.6). Keeping the write-side at the
# same threshold prevents low-confidence rows from polluting the DB.
_FASHION_ENRICH_MIN_CONFIDENCE = 0.6
GUARDED_OFFER_WRITERS = {
    "shopify_products_sync",
    # Wix catalog sync currently routes through routes.universal_product_sync.
    "universal_product_sync",
}
STALE_AFTER_SYNC = "stale_after_sync"
CATALOG_SYNC_PRUNE_WRITER = "catalog_sync_service_prune"
SUPPRESSION_FIELDS = ("suppression_reason", "suppressed_at", "suppression_metadata")


async def _async_fashion_enrich(
    *,
    product_key: str,
    title: Optional[str],
    description: Optional[str],
    html_blob: Optional[str],
    category_path: Optional[str],
    payload_filled: Dict[str, bool],
) -> None:
    """Fire-and-forget LLM enrichment for a newly upserted fashion row.

    Runs the batched material+care+size_guide extractor and UPDATEs only
    fields that (a) the merchant payload didn't already provide and (b)
    were extracted with confidence >= the trust gate. The UPDATE has
    `<field> IS NULL` guards so a concurrent backfill or re-sync can't
    be overwritten.

    Never raises — every exception path is logged. Bounded by the module
    semaphore so a 1k-product fashion sync doesn't fan out unbounded.
    """
    try:
        async with _FASHION_ENRICH_SEM:
            results = await batch_extract_fashion_fields(
                title=title,
                description=description,
                html_blob=html_blob,
                category_path=category_path,
            )
        set_clauses: List[str] = []
        where_clauses: List[str] = ["product_key = :key"]
        params: Dict[str, Any] = {"key": product_key}

        def _maybe_add(field: str, value_param: Any):
            r = results.get(field)
            if r is None or r.value is None:
                return False
            if r.confidence < _FASHION_ENRICH_MIN_CONFIDENCE:
                return False
            # Don't clobber a merchant_payload write that just landed on
            # the same row — the upsert already set those columns; the
            # NULL guard below also prevents it, but skipping the SET
            # clause keeps the UPDATE narrow.
            if payload_filled.get(field):
                return False
            set_clauses.extend([
                f"{field} = :{field}",
                f"{field}_source = :{field}_source",
                f"{field}_confidence = :{field}_confidence",
            ])
            params[field] = value_param
            params[f"{field}_source"] = EXTRACTION_SOURCE_LLM
            params[f"{field}_confidence"] = r.confidence
            where_clauses.append(f"{field} IS NULL")
            return True

        _maybe_add("material", results["material"].value if results.get("material") else None)
        _maybe_add("care", results["care"].value if results.get("care") else None)
        # size_guide column is JSONB; wrap plain string in {raw: ...} so the
        # gateway can distinguish a flat string from a structured chart later.
        sg = results.get("size_guide")
        if sg is not None and sg.value is not None and sg.confidence >= _FASHION_ENRICH_MIN_CONFIDENCE and not payload_filled.get("size_guide"):
            set_clauses.extend([
                "size_guide = :size_guide",
                "size_guide_source = :size_guide_source",
                "size_guide_confidence = :size_guide_confidence",
            ])
            params["size_guide"] = json.dumps({"raw": sg.value})
            params["size_guide_source"] = EXTRACTION_SOURCE_LLM
            params["size_guide_confidence"] = sg.confidence
            where_clauses.append("size_guide IS NULL")

        if not set_clauses:
            logger.debug(
                "fashion_enrich.no_writes product_key=%s category=%s",
                product_key, category_path,
            )
            return
        sql = (
            "UPDATE catalog_products SET "
            + ", ".join(set_clauses)
            + " WHERE "
            + " AND ".join(where_clauses)
        )
        await database.execute(sql, params)
        logger.info(
            "fashion_enrich.applied product_key=%s fields=%s",
            product_key,
            [s.split(" = ")[0] for s in set_clauses if not s.endswith("_source") and not s.endswith("_confidence")],
        )
    except Exception as exc:  # noqa: BLE001 — must not propagate; runs detached
        logger.warning(
            "fashion_enrich.failed product_key=%s err=%s",
            product_key, exc,
        )


def _schedule_fashion_enrichment(
    *,
    product_key: str,
    title: Optional[str],
    description: Optional[str],
    html_blob: Optional[str],
    category_path: Optional[str],
    payload_filled: Dict[str, bool],
) -> None:
    """Spawn fashion-enrichment as a detached task. Sync doesn't wait.

    Early-outs if ALL three fields were already filled by the merchant
    payload (nothing left to enrich). The batch_extract_fashion_fields
    callee does its own flag + category + haystack gating, so this
    function intentionally stays a thin scheduler — kicking the task
    even when gates may reject keeps the gating logic single-sourced.
    """
    if payload_filled.get("material") and payload_filled.get("care") and payload_filled.get("size_guide"):
        return
    try:
        asyncio.create_task(
            _async_fashion_enrich(
                product_key=product_key,
                title=title,
                description=description,
                html_blob=html_blob,
                category_path=category_path,
                payload_filled=payload_filled,
            )
        )
    except RuntimeError as exc:
        # No running event loop (shouldn't happen in normal request flow,
        # but a script-driven entry without asyncio.run() could trip this).
        logger.warning("fashion_enrich.schedule_no_loop err=%s", exc)


def _utcnow() -> datetime:
    # The catalog tables created by migration 058 use timestamp columns without
    # timezone metadata. asyncpg rejects aware datetimes for those columns, so
    # persist naive UTC consistently here.
    return datetime.utcnow()


def _stable_key(prefix: str, *parts: Any) -> str:
    normalized = "::".join(str(part or "").strip() for part in parts)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _safe_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def make_catalog_product_key(merchant_id: str, platform: str, source_product_id: str) -> str:
    return f"prod::{merchant_id}::{platform}::{source_product_id}"


def make_catalog_sku_key(product_key: str, source_variant_id: str) -> str:
    return f"sku::{product_key}::{source_variant_id}"


# ---------------------------------------------------------------------------
# Pivota canonical PDP — every onboarded merchant product gets a stable
# agent.pivota.cc/products/<sig_id> URL. sig_id is a deterministic
# 32-hex hash of the product's identity tuple so:
#   - Same product across re-syncs gets the same sig (idempotent)
#   - Different merchants' products NEVER collide (merchant_id in input)
#   - Doesn't leak the product_key format to public URLs (hash, not literal)
# ---------------------------------------------------------------------------

import os as _os

from services.vertical_profiles import DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD


def _unresolved_vertical_fail_threshold(env_var: str) -> float:
    """Fix Plan B T3: read the configurable intake brake threshold (fraction,
    e.g. 0.20) from ``env_var``. Falls back to the shared default when unset,
    unparseable, or out of the [0, 1] range."""
    raw = _os.getenv(env_var)
    if raw is None:
        return DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD
    if 0.0 <= val <= 1.0:
        return val
    return DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD


def make_pivota_signature_id(
    merchant_id: str, platform: str, source_product_id: str,
) -> str:
    """sig_<32hex> deterministic from the product's identity tuple.
    Same inputs → same sig forever; different merchants' products
    can't collide (merchant_id is in the hash input)."""
    if not merchant_id or not platform or not source_product_id:
        raise ValueError(
            "merchant_id, platform, and source_product_id are all required "
            f"to mint a Pivota signature; got "
            f"({merchant_id!r}, {platform!r}, {source_product_id!r})"
        )
    raw = f"{merchant_id}::{platform}::{source_product_id}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:32]
    return f"sig_{digest}"


def pivota_canonical_pdp_url(signature_id: str) -> str:
    """Build the public canonical PDP URL for a given sig. Reads
    CHECKOUT_UI_BASE_URL (mirrors the convention used by order_routes,
    buyer_api, agent_payment_sdk, agent_checkout_intents) so dev /
    staging environments stay self-consistent."""
    base = (
        _os.getenv("CHECKOUT_UI_BASE_URL") or "https://agent.pivota.cc"
    ).rstrip("/")
    return f"{base}/products/{signature_id}"


def make_pivota_canonical_fields(
    merchant_id: str, platform: str, source_product_id: str,
) -> Dict[str, Any]:
    """Convenience: return all three Pivota canonical fields ready to
    splat into a catalog_products upsert. `pivota_signature_minted_at`
    is set to the call time so audit reports can compute the indexing
    arc phase from a real minted timestamp (vs static caveat). See
    services/pivota_indexing_arc.py for arc consumers."""
    from datetime import datetime as _dt, timezone as _tz
    sig = make_pivota_signature_id(merchant_id, platform, source_product_id)
    return {
        "pivota_signature_id": sig,
        "pivota_canonical_url": pivota_canonical_pdp_url(sig),
        "pivota_signature_minted_at": _dt.now(_tz.utc),
    }


def make_catalog_offer_id(sku_key: str, channel: str, catalog_track: str) -> str:
    return f"offer::{catalog_track}::{channel}::{sku_key}"


def _coerce_variant(product: StandardProduct) -> StandardProductVariant:
    variant_id = str(product.sku or product.id or "default").strip() or "default"
    return StandardProductVariant(
        id=variant_id,
        title=product.title,
        variant_id=variant_id,
        sku=product.sku,
        barcode=product.barcode,
        price=product.price,
        compare_at_price=product.compare_at_price,
        inventory_quantity=product.inventory_quantity or 0,
        image_url=product.image_url,
        visible_option_labels=[],
        platform_metadata=product.platform_metadata,
    )


def _iter_variants(product: StandardProduct) -> List[StandardProductVariant]:
    variants = list(product.variants or [])
    return variants or [_coerce_variant(product)]


def _raw_variant_payloads(raw_product: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_variants = raw_product.get("variants")
    if isinstance(raw_variants, list):
        return [item for item in raw_variants if isinstance(item, dict)]
    if isinstance(raw_variants, dict):
        for key in ("variants", "items", "results"):
            inner = raw_variants.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
    return []


def _raw_variant_payload(
    raw_product: Dict[str, Any],
    variant: StandardProductVariant,
    source_variant_id: str,
) -> Dict[str, Any]:
    targets = {
        str(source_variant_id or "").strip(),
        str(variant.variant_id or "").strip(),
        str(variant.id or "").strip(),
    }
    targets.discard("")
    for raw_variant in _raw_variant_payloads(raw_product):
        raw_body = raw_variant.get("variant") if isinstance(raw_variant.get("variant"), dict) else {}
        candidate_ids = {
            str(raw_variant.get("id") or "").strip(),
            str(raw_variant.get("variant_id") or "").strip(),
            str(raw_variant.get("source_variant_id") or "").strip(),
            str(raw_body.get("id") or "").strip(),
            str(raw_body.get("variant_id") or "").strip(),
            str(raw_body.get("source_variant_id") or "").strip(),
        }
        candidate_ids.discard("")
        if targets & candidate_ids:
            return raw_variant
    return {}


def _record_identifier_audit(audit: Optional[WriterAuditAccumulator], identifier: Optional[StrongIdentifier]) -> None:
    if audit is None:
        return
    if identifier is None:
        audit.record_info({NO_STRONG_IDENTIFIER: 1})
    elif identifier.kind == "mpn":
        audit.record_info({MPN_CAPTURED_AS_BARCODE: 1})


def _extract_metadata_values(metadata: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in metadata and metadata.get(key) not in (None, "", [], {}):
            return metadata.get(key)
    return None


def _split_text_steps(text: str) -> List[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = [part.strip(" -•\n\r\t") for part in re.split(r"(?:\r?\n|\. +|\d+\.\s+|•)", raw) if part.strip(" -•\n\r\t")]
    deduped: List[str] = []
    for part in parts:
        if part and part not in deduped:
            deduped.append(part)
    return deduped


def _normalize_shade_name(label: str) -> str:
    normalized = str(label or "").strip().replace("_", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.title() if normalized else ""


def _shade_family_from_name(name: str) -> Optional[str]:
    lowered = str(name or "").strip().lower()
    if not lowered:
        return None
    for family in ("red", "pink", "berry", "brown", "coral", "peach", "nude", "orange", "gold", "bronze"):
        if family in lowered:
            return family
    return None


def _extract_how_to_use(metadata: Dict[str, Any]) -> Tuple[Optional[str], List[str]]:
    raw = _extract_metadata_values(
        metadata,
        "how_to_use",
        "howToUse",
        "usage",
        "usage_text",
        "directions",
        "directions_text",
    )
    if not raw:
        return None, []
    if isinstance(raw, list):
        steps = [str(item or "").strip() for item in raw if str(item or "").strip()]
        text = " ".join(steps).strip() or None
        return text, steps
    text = str(raw).strip()
    return text or None, _split_text_steps(text)


def _extract_claims(metadata: Dict[str, Any]) -> List[str]:
    claims = _json_list(_extract_metadata_values(metadata, "claims", "benefit_claims", "claim_labels"))
    if claims:
        return [str(item or "").strip() for item in claims if str(item or "").strip()]
    text = _extract_metadata_values(metadata, "claims_text", "benefits_text")
    if text:
        return _split_text_steps(str(text))
    return []


def _extract_benefits(product: StandardProduct, metadata: Dict[str, Any]) -> List[str]:
    benefits = _json_list(_extract_metadata_values(metadata, "benefits", "benefit_labels"))
    normalized = [str(item or "").strip() for item in benefits if str(item or "").strip()]
    if normalized:
        return normalized
    derived: List[str] = []
    for labels in (product.visible_attributes or {}).values():
        for label in labels or []:
            value = str(label or "").strip().replace("_", " ")
            if value and value not in derived:
                derived.append(value)
    return derived


def _extract_active_ingredients(metadata: Dict[str, Any], ingredient_ids: List[str]) -> List[Dict[str, Any]]:
    raw = _extract_metadata_values(metadata, "active_ingredients", "activeIngredients")
    if isinstance(raw, list):
        normalized: List[Dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                normalized.append(item)
            else:
                label = str(item or "").strip()
                if label:
                    normalized.append({"label": label})
        return normalized
    if ingredient_ids:
        return [{"label": ingredient_id} for ingredient_id in ingredient_ids[:5]]
    return []


def _extract_raw_inci(metadata: Dict[str, Any], ingredient_ids: List[str]) -> Optional[str]:
    raw = _extract_metadata_values(
        metadata,
        "ingredients",
        "ingredients_text",
        "inci",
        "raw_inci",
        "pdp_ingredients_raw",
    )
    if raw:
        if isinstance(raw, list):
            return ", ".join(str(item or "").strip() for item in raw if str(item or "").strip()) or None
        return str(raw).strip() or None
    if ingredient_ids:
        return ", ".join(ingredient_ids)
    return None


def _extract_tutorial_assets(product: StandardProduct, metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    tutorials = _json_list(_extract_metadata_values(metadata, "tutorials", "tutorial_assets", "media_assets"))
    assets: List[Dict[str, Any]] = []
    for idx, item in enumerate(tutorials):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("href") or "").strip()
        if not url:
            continue
        assets.append(
            {
                "asset_id": item.get("asset_id") or _stable_key("beauty_asset", product.id, idx, url),
                "asset_type": str(item.get("asset_type") or item.get("type") or "tutorial"),
                "title": item.get("title"),
                "url": url,
                "thumbnail_url": item.get("thumbnail_url") or item.get("thumbnail"),
                "sort_order": idx,
                "metadata_json": item,
            }
        )
    return assets


def _extract_shades(product: StandardProduct, variant: StandardProductVariant) -> List[Dict[str, Any]]:
    shades: List[Dict[str, Any]] = []
    labels = list(variant.visible_option_labels or [])
    for idx, label in enumerate(labels):
        if not str(label).startswith("shade_"):
            continue
        shade_name = _normalize_shade_name(str(label)[len("shade_"):])
        if not shade_name:
            continue
        shades.append(
            {
                "shade_id": _stable_key("beauty_shade", product.id, variant.variant_id or variant.id, shade_name),
                "shade_name": shade_name,
                "shade_code": None,
                "shade_family": _shade_family_from_name(shade_name),
                "undertone": None,
                "finish": None,
                "swatch_refs_json": [],
                "media_refs_json": [],
            }
        )
    return shades


def _beauty_taxonomy(product: StandardProduct) -> Dict[str, Any]:
    return {
        "product_type": product.product_type,
        "visible_attributes": product.visible_attributes or {},
        "tags": list(product.tags or []),
    }


def _beauty_is_candidate(product: StandardProduct) -> bool:
    if product.ingredient_ids:
        return True
    if product.visible_attributes:
        return True
    for variant in _iter_variants(product):
        if any(str(label or "").startswith("shade_") for label in variant.visible_option_labels or []):
            return True
    product_type = str(product.product_type or "").lower()
    return any(token in product_type for token in ("serum", "cleanser", "toner", "foundation", "lip", "cream", "spf"))


def _compatibility_rules_from_ingredients(ingredient_ids: List[str], merchant_id: str, product_key: str, sku_key: str) -> List[Dict[str, Any]]:
    ingredient_set = set(ingredient_ids)
    rules: List[Dict[str, Any]] = []
    if "retinol" in ingredient_set and "salicylic_acid" in ingredient_set:
        rules.append(
            {
                "compatibility_rule_id": _stable_key("beauty_compat", sku_key, "retinol", "salicylic_acid"),
                "product_key": product_key,
                "sku_key": sku_key,
                "merchant_id": merchant_id,
                "rule_type": "ingredient_conflict",
                "subject_ingredients_json": ["retinol"],
                "related_ingredients_json": ["salicylic_acid"],
                "verdict": "caution",
                "rationale": "Retinoids and exfoliating acids often require staggered use in the same routine.",
                "evidence_refs_json": ["deterministic_rule:retinol_salicylic_acid"],
            }
        )
    if "retinol" in ingredient_set and "benzoyl_peroxide" in ingredient_set:
        rules.append(
            {
                "compatibility_rule_id": _stable_key("beauty_compat", sku_key, "retinol", "benzoyl_peroxide"),
                "product_key": product_key,
                "sku_key": sku_key,
                "merchant_id": merchant_id,
                "rule_type": "ingredient_conflict",
                "subject_ingredients_json": ["retinol"],
                "related_ingredients_json": ["benzoyl_peroxide"],
                "verdict": "caution",
                "rationale": "Retinoids and benzoyl peroxide commonly require schedule separation to reduce irritation risk.",
                "evidence_refs_json": ["deterministic_rule:retinol_benzoyl_peroxide"],
            }
        )
    return rules


def _readiness_tier_for_product(product: StandardProduct) -> str:
    commerce_ready = bool(product.title and product.price is not None and _iter_variants(product))
    if not commerce_ready:
        return "identity_only"
    if _beauty_is_candidate(product):
        how_to_use, _ = _extract_how_to_use(_json_dict(product.platform_metadata))
        if product.ingredient_ids and how_to_use:
            return "knowledge_ready"
        return "vertical_ready"
    return "commerce_ready"


async def _fetch_one_by_pk(table: Any, pk_name: str, pk_value: Any) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(select(table).where(getattr(table.c, pk_name) == pk_value))
    return dict(row) if row else None


def _is_stale_after_sync_tombstone(row: Optional[Dict[str, Any]]) -> bool:
    if not row:
        return False
    return bool(row.get("suppressed_at") and row.get("suppression_reason") == STALE_AFTER_SYNC)


def _preserve_non_stale_suppression(existing: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> None:
    if not existing or _is_stale_after_sync_tombstone(existing):
        return
    if not any(field in payload for field in SUPPRESSION_FIELDS):
        return
    if not existing.get("suppressed_at") and not existing.get("suppression_reason"):
        return
    for field in SUPPRESSION_FIELDS:
        if field in payload:
            payload[field] = existing.get(field)


SCOPE_FIELDS = ("pdp_scope", "pdp_scope_source", "pdp_scope_set_at")


def _preserve_existing_scope(existing: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> None:
    """pdp_scope is stamped at BIRTH only; on UPDATE the existing values win.

    The Path-A payload carries `pdp_scope='merchant_owned'` unconditionally, and
    this upsert applies the full payload on UPDATE — so before this guard, every
    re-sync silently reverted any promotion (the D3 cron's, the recovery
    writer's) back to `merchant_owned`, an affirmed state outside the
    `WHERE pdp_scope='unverified'` gate every promotion writer checks: the
    promotion would never come back. Driven on the real `_upsert_by_pk` in the
    PR #1680 round-11 review. Scope transitions after birth belong exclusively
    to the governance writers. (docs/PDP_SCOPE_REDESIGN.md's invariant is
    stricter still — lanes may seed ONLY the DB default 'unverified'; Path A's
    merchant_owned birth stamp remains a stated form-exception like the doc's
    Path-C note. This guard narrows the violation to birth only; it does not
    close that exception.) Same preservation shape as
    `_preserve_non_stale_suppression` above."""
    if not existing:
        return
    for field in SCOPE_FIELDS:
        if field in payload:
            payload[field] = existing.get(field)


async def _resolve_catalog_sku_key(
    *,
    merchant_id: str,
    platform: str,
    product_key: str,
    source_variant_id: str,
) -> str:
    """Preserve existing SKU primary keys across key-shape migrations."""
    row = await database.fetch_one(
        select(catalog_skus.c.sku_key)
        .where(catalog_skus.c.merchant_id == merchant_id)
        .where(catalog_skus.c.platform == platform)
        .where(catalog_skus.c.product_key == product_key)
        .where(catalog_skus.c.source_variant_id == source_variant_id)
        .limit(1)
    )
    if row:
        existing_key = str(dict(row).get("sku_key") or "").strip()
        if existing_key:
            return existing_key
    return make_catalog_sku_key(product_key, source_variant_id)


def _table_debug_name(table: Any) -> str:
    return str(getattr(table, "name", None) or type(table).__name__)


async def _upsert_by_pk(table: Any, pk_name: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    table_name = _table_debug_name(table)
    try:
        pk_value = values[pk_name]
        existing = await _fetch_one_by_pk(table, pk_name, pk_value)
        payload = dict(values)
        _preserve_non_stale_suppression(existing, payload)
        _preserve_existing_scope(existing, payload)
        payload["updated_at"] = _utcnow()
        if existing:
            await database.execute(
                table.update().where(getattr(table.c, pk_name) == pk_value).values(**payload)
            )
            return existing
        payload.setdefault("created_at", _utcnow())
        await database.execute(table.insert().values(**payload))
        return None
    except Exception as exc:
        raise RuntimeError(
            f"catalog upsert failed table={table_name} pk={pk_name}: {exc}"
        ) from exc


async def _replace_child_rows(table: Any, match_column: str, match_value: Any, rows: Iterable[Dict[str, Any]]) -> int:
    await database.execute(table.delete().where(getattr(table.c, match_column) == match_value))
    count = 0
    for row in rows:
        payload = dict(row)
        payload.setdefault("created_at", _utcnow())
        payload.setdefault("updated_at", _utcnow())
        await database.execute(table.insert().values(**payload))
        count += 1
    return count


async def _replace_child_rows_multi(table: Any, where_clauses: List[Any], rows: Iterable[Dict[str, Any]]) -> int:
    stmt = table.delete()
    for clause in where_clauses:
        stmt = stmt.where(clause)
    await database.execute(stmt)
    count = 0
    for row in rows:
        payload = dict(row)
        payload.setdefault("created_at", _utcnow())
        payload.setdefault("updated_at", _utcnow())
        await database.execute(table.insert().values(**payload))
        count += 1
    return count


async def _append_snapshot(table: Any, values: Dict[str, Any]) -> None:
    payload = dict(values)
    payload.setdefault("observed_at", _utcnow())
    offer_id = str(payload.get("offer_id") or "").strip()
    source_system = str(payload.get("source_system") or "").strip()
    if table in (catalog_inventory_snapshots, catalog_price_snapshots) and offer_id:
        delete_stmt = table.delete().where(table.c.offer_id == offer_id)
        if source_system:
            delete_stmt = delete_stmt.where(table.c.source_system == source_system)
        await database.execute(delete_stmt)
    await database.execute(table.insert().values(**payload))


async def _upsert_field_fact(
    *,
    entity_type: str,
    entity_id: str,
    field_family: str,
    field_key: str,
    source_system: str,
    source_ref: Optional[str],
    value: Any,
    observed_at: Optional[datetime] = None,
    fresh_until: Optional[datetime] = None,
    confidence: Optional[Decimal] = None,
    review_state: str = "observed",
    merchant_id: Optional[str] = None,
    commerce_index_source_id: Optional[str] = None,
    commerce_index_source_kind: Optional[str] = None,
) -> None:
    fact_id = _stable_key("fact", entity_type, entity_id, field_family, field_key, source_system)
    # Source identity and merchant scoping are mandatory for v2 publication.
    # Legacy canonical fact ingestion remains unchanged when the v2 canary is
    # disabled or the merchant has not granted source consent.
    v2_enabled = bool(
        merchant_id
        and commerce_index_source_id
        and commerce_index_source_kind
        and commerce_index_v2_enabled_for_merchant(merchant_id)
    )
    # Migration-safe: legacy sync behavior remains a single fact upsert until
    # the Commerce Index v2 tables are deployed and the feature is enabled.
    previous = await _fetch_one_by_pk(catalog_field_facts, "fact_id", fact_id) if v2_enabled else None
    await database.execute(
        catalog_field_facts.delete()
        .where(catalog_field_facts.c.entity_type == entity_type)
        .where(catalog_field_facts.c.entity_id == entity_id)
        .where(catalog_field_facts.c.field_family == field_family)
        .where(catalog_field_facts.c.field_key == field_key)
        .where(catalog_field_facts.c.source_system == source_system)
        .where(catalog_field_facts.c.fact_id != fact_id)
    )
    await _upsert_by_pk(
        catalog_field_facts,
        "fact_id",
        {
            "fact_id": fact_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "field_family": field_family,
            "field_key": field_key,
            "source_system": source_system,
            "source_ref": source_ref,
            "value_json": value,
            "observed_at": observed_at or _utcnow(),
            "fresh_until": fresh_until,
            "confidence": confidence,
            "review_state": review_state,
        },
    )
    if v2_enabled:
        await record_field_change_and_publications(
            merchant_id=merchant_id,
            observation=FieldObservation(
                entity_type=entity_type,
                entity_id=entity_id,
                field_family=field_family,
                field_key=field_key,
                value=value,
                source_system=source_system,
                source_kind=str(commerce_index_source_kind),
                source_ref=source_ref,
                observed_at=observed_at or _utcnow(),
                fresh_until=fresh_until,
                confidence=float(confidence) if confidence is not None else 0.0,
            ),
            previous_value=previous.get("value_json") if previous else None,
            source_id=str(commerce_index_source_id),
        )


async def _resolve_merchant_name(merchant_id: str) -> Optional[str]:
    candidate_keys = [
        key
        for key in ("merchant_name", "business_name", "store_name")
        if hasattr(merchant_onboarding.c, key)
    ]
    if not candidate_keys:
        return None
    row = await database.fetch_one(
        select(*(getattr(merchant_onboarding.c, key) for key in candidate_keys))
        .where(merchant_onboarding.c.merchant_id == merchant_id)
        .limit(1)
    )
    if not row:
        return None
    data = dict(row)
    for key in candidate_keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return None


async def upsert_catalog_merchant(
    *,
    merchant_id: str,
    merchant_name: Optional[str],
    primary_platform: Optional[str],
    source_system: str,
    source_ref: Optional[str],
    metadata_json: Optional[Dict[str, Any]] = None,
    status: str = "active",
) -> None:
    # `status` defaults to 'active' so every existing caller (synced tenants,
    # url_audit intake) is unchanged. Observed sellers minted at ingestion pass
    # status='observed' (ADR-009 D2) — first-class but NOT servable as the public
    # citation artifact until graduation (pivota_canonical_routes gates on
    # status='active'). See services/seller_identity.py.
    if not merchant_name:
        merchant_name = await _resolve_merchant_name(merchant_id)
    await _upsert_by_pk(
        catalog_merchants,
        "merchant_id",
        {
            "merchant_id": merchant_id,
            "merchant_name": merchant_name,
            "primary_platform": primary_platform,
            "status": status,
            "source_system": source_system,
            "source_ref": source_ref,
            "metadata_json": metadata_json or {},
        },
    )


async def ingest_standard_products(
    *,
    merchant_id: str,
    platform: str,
    product_payloads: List[Dict[str, Any]],
    source_system: str,
    source_ref: Optional[str] = None,
    job_id: Optional[str] = None,
    source_domain: Optional[str] = None,
) -> Dict[str, Any]:
    source_domain_value = str(source_domain or "").strip() or None
    stats = {
        "products_scanned": len(product_payloads or []),
        "products_ingested": 0,
        "products_failed": 0,
        "skus_ingested": 0,
        "offers_ingested": 0,
        "offers_skipped": 0,
        "offer_skip_reasons": {},
        "products_recovered_after_stale": 0,
        "beauty_profiles_upserted": 0,
        "beauty_ingredient_rows_upserted": 0,
        "beauty_usage_guides_upserted": 0,
        "beauty_shades_upserted": 0,
        "beauty_content_assets_upserted": 0,
        "beauty_compatibility_rules_upserted": 0,
        "brand_conflicts_flagged": 0,
        "commerce_index_v2_withheld": False,
    }
    commerce_index_source: Optional[Dict[str, Any]] = None
    if commerce_index_v2_enabled_for_merchant(merchant_id):
        # `platform`, not the generic writer label, establishes the source
        # contract. This keeps portal universal sync as merchant API data and
        # prevents a free-form `source_system` from granting authority.
        commerce_index_source = await resolve_active_catalog_source(
            merchant_id=merchant_id,
            provider=platform,
        )
        if commerce_index_source is None:
            # For an allowlisted v2 merchant, do not let an unconsented or
            # unsupported source mutate canonical price/stock before the
            # authority gate runs. Legacy merchants remain on the unchanged
            # pre-v2 path because they never enter this branch.
            logger.warning(
                "Commerce Index v2 catalog intake withheld: no active consented source merchant=%s provider=%s",
                merchant_id,
                platform,
            )
            stats["commerce_index_v2_withheld"] = True
            return stats
    # ADR-008 prevent-at-intake (convergence P1.4): guard once per distinct
    # brand per ingest run — a merchant's catalog is usually one brand, so
    # this is ~1 extra lookup per sync, not per product.
    _brand_guard_seen: set = set()
    # Fix Plan B T3: per-run vertical-structure accounting for the intake brake.
    _vertical_rows_considered = 0
    _vertical_rows_unresolved = 0
    audit = (
        WriterAuditAccumulator(
            writer_name=source_system,
            batch_id=job_id or make_batch_id(source_system, source_ref),
        )
        if source_system in GUARDED_OFFER_WRITERS
        else None
    )

    async with database.transaction():
        await upsert_catalog_merchant(
            merchant_id=merchant_id,
            merchant_name=None,
            primary_platform=platform,
            source_system=source_system,
            source_ref=source_ref,
            metadata_json={"ingested_from": source_system},
        )

    for raw_product in product_payloads or []:
        try:
            product = StandardProduct(**raw_product)
        except Exception as exc:
            stats["products_failed"] += 1
            logger.warning("Catalog ingest: failed to parse StandardProduct merchant=%s platform=%s err=%s", merchant_id, platform, exc)
            continue

        # Classify the category BEFORE opening the transaction.
        #
        # This await can make an LLM call. Inside the transaction it pinned a
        # pooled connection and held an open Postgres transaction — row locks
        # included — for the whole round-trip, once PER PRODUCT, on a path
        # reachable from routes/universal_product_sync.py. A sync of N products
        # therefore serialised N LLM calls each under an open transaction.
        #
        # There is a second, quieter hazard: `databases==0.7.0` parks the
        # `Connection` in a ContextVar, so a task spawned from a context that
        # already touched the DB shares this one. Such a sibling's queries JOIN
        # the open transaction — measured: a sibling read the holder's
        # uncommitted row — and lose their writes if the holder rolls back.
        #
        # (This is NOT the explanation for the 2026-08-18 wedge; that remains
        # open. An earlier version of this comment said it was. The holder does
        # not block siblings: a sibling query ran in 0.000s while a holder slept
        # 1.0s inside its transaction.)
        #
        # The fold is pure — regex, then httpx — touches no tables, and reads
        # only off `product`, so it hoists with no behaviour change.
        _description_for_ingest = product.description_text or product.description
        _category_fold = await fold_category_with_llm_fallback(
            merchant_id=merchant_id,
            category=product.product_type,
            product_type=product.product_type,
            title=product.title,
            description=_description_for_ingest,
            variants=product.variants,
        )

        async with database.transaction():
            source_pid = str(product.product_id or product.id)
            product_key = make_catalog_product_key(merchant_id, platform, source_pid)
            metadata = _json_dict(product.platform_metadata)
            readiness_tier = _readiness_tier_for_product(product)
            canonical_url = str(metadata.get("canonical_url") or metadata.get("url") or "").strip() or None
            brand = str(product.vendor or metadata.get("brand") or "").strip() or None
            content_key = make_content_key(brand, product.title, product.barcode)
            from services.intake_identity import (
                ACTION_FLAG as _IDENTITY_FLAG,
                DOOR_CATALOG_SYNC as _DOOR_SYNC,
                canonical_gtin as _canonical_gtin,
                intake_identity_enabled as _intake_identity_enabled,
                resolve_or_attach_content_identity as _resolve_or_attach,
            )

            if _intake_identity_enabled(_DOOR_SYNC):
                # ADR-011 resolve-or-attach (flag-gated; composes the ADR-008
                # brand guard, replacing the legacy standalone call below when
                # ON). FIRST-PARTY door semantics: NEVER blocked — conflicts
                # FLAG (review enqueued) and the sync proceeds. The memo keeps
                # the brand guard at once-per-distinct-brand per run.
                _ident = await _resolve_or_attach(
                    brand=brand,
                    title=product.title,
                    gtin=product.barcode,
                    canonical_url=canonical_url,
                    source_product_id=source_pid,
                    door=_DOOR_SYNC,
                    merchant_ctx={
                        "merchant_id": merchant_id,
                        "platform": platform,
                        "source_domain": source_domain_value,
                        "product_key": product_key,
                        "brand_guard_memo": _brand_guard_seen,
                    },
                )
                content_key = _ident.get("content_key") or content_key
                if _ident.get("action") == _IDENTITY_FLAG:
                    stats["brand_conflicts_flagged"] += 1
                    logger.info(
                        "catalog_sync.identity_flag merchant=%s brand=%r "
                        "matcher=%s (review enqueued, sync proceeds)",
                        merchant_id, brand,
                        (_ident.get("evidence") or {}).get("matcher"),
                    )
            # ADR-008 prevent-at-intake (convergence P1.4) — FIRST-PARTY door
            # semantics: the connected merchant's own catalog ALWAYS proceeds
            # (higher truth than any observed row); a brand+host already
            # canonical under a DIFFERENT merchant is flagged for
            # reconciliation instead (reconcile-at-connect, never
            # block-at-connect). Best-effort; once per distinct brand per run.
            elif brand and source_domain_value and brand.lower() not in _brand_guard_seen:
                _brand_guard_seen.add(brand.lower())
                try:
                    from services.audit_index_intake import (
                        apply_intake_brand_fragmentation_guard,
                    )

                    _guard = await apply_intake_brand_fragmentation_guard(
                        merchant_id,
                        {
                            "product_key": product_key,
                            "brand": brand,
                            "source_domain": source_domain_value,
                            "canonical_url": canonical_url,
                            "content_key": content_key,
                        },
                        door="catalog_sync",
                        block_on_conflict=False,
                    )
                    if _guard.get("action") == "flag":
                        stats["brand_conflicts_flagged"] += 1
                        logger.info(
                            "catalog_sync.brand_guard_flag merchant=%s brand=%r host=%r "
                            "conflict_merchant=%s (review enqueued, sync proceeds)",
                            merchant_id, brand, source_domain_value,
                            _guard.get("conflict_merchant_id"),
                        )
                except Exception:  # noqa: BLE001 — guard must never break a sync
                    logger.debug("catalog_sync brand guard failed", exc_info=True)
            # Pivota canonical PDP fields (sig_id + agent.pivota.cc URL)
            # — every onboarded merchant product gets one. Deterministic
            # so re-syncs are idempotent.
            pivota_fields = make_pivota_canonical_fields(
                merchant_id, platform, source_pid,
            )

            _tags_for_ingest = list(product.tags or [])
            _taxonomy_v1 = derive_taxonomy_v1(
                price=product.price,
                title=product.title,
                description=_description_for_ingest,
                tags=_tags_for_ingest,
            )
            # Phase O-5 (variant-canonical fold-up): classify category_path
            # inline at sync time. Regex first (fast, free, deterministic);
            # LLM fallback for the long-tail when regex misses AND
            # LLM_CATEGORY_CLASSIFIER_ENABLED=true. Source on the LLM
            # path is 'llm_category_v1' with a per-product_type cache
            # so a merchant's catalog of 600 "Dog Harness" rows triggers
            # one LLM call. See plans/let-s-build-a-full-breezy-taco.md.
            # (_category_fold computed above, outside the transaction)
            if _category_fold is not None:
                (_cat_label, _cat_path), _cat_source, _cat_confidence = _category_fold
            else:
                _cat_label, _cat_path, _cat_source, _cat_confidence = None, None, None, None
            # Phase O-5b (#3): merchant-published fashion fields read from
            # Shopify standard metafields (or top-level platform_metadata
            # for legacy/admin-injected shapes). Authoritative path —
            # source=merchant_payload confidence=1.0. When absent, the
            # LLM extractor v2 (services/fashion_field_extractor.py) is
            # the fallback (gated by FASHION_EXTRACT_ENABLED).
            # NOTE: shopify_real_adapter doesn't fetch metafields yet, so
            # this is a no-op for the Shopify fetch path today. Manual
            # admin injection + future GraphQL adapter upgrade will both
            # populate these.
            _payload_material = extract_material_from_payload(product.platform_metadata)
            _payload_care = extract_care_from_payload(product.platform_metadata)
            _payload_size_guide = extract_size_guide_from_payload(product.platform_metadata)
            # Phase O-4: compute lifecycle stage from the same shape
            # the row will be UPSERTed with. Path A stamps
            # pdp_scope='merchant_owned' AT BIRTH ONLY — on UPDATE of an
            # existing row `_preserve_existing_scope` keeps the row's current
            # scope, so a promotion by the governance writers (D3 cron,
            # identity recovery) survives re-syncs. Before that guard, this
            # payload silently reverted promotions on every re-sync.
            _stage_input = {
                "title": product.title,
                "description": _description_for_ingest,
                "image_url": product.image_url,
                "category_path": _cat_path,
                "tags": _tags_for_ingest,
                "demographic": _taxonomy_v1.get("demographic"),
                "use_case_tags": _taxonomy_v1.get("use_case_tags"),
                "lifestyle_tags": _taxonomy_v1.get("lifestyle_tags"),
                "pdp_scope": "merchant_owned",
                "source_system": source_system,
            }

            # Fix Plan B T4: case/trim-normalize the free-text category before it
            # is written (no semantic renames). NULL stays NULL.
            _normalized_category = normalize_category(product.product_type)
            # Fix Plan B T1/T3: resolve the durable top-level vertical (mig 173)
            # once, into a local, so we both persist it and count the rows that
            # carried no machine-readable structure at all (the intake brake).
            _vertical_signals = {
                "product_type": product.product_type,
                "category": _normalized_category,
                "category_path": _cat_path,
            }
            _resolved_vertical = resolve_vertical(
                _vertical_signals,
                title=" ".join(
                    str(part)
                    for part in (
                        product.title,
                        _description_for_ingest,
                        *(_tags_for_ingest or []),
                    )
                    if part
                ),
            )
            _vertical_rows_considered += 1
            if is_vertical_unresolved(_resolved_vertical, _vertical_signals):
                _vertical_rows_unresolved += 1

            existing_product = await _upsert_by_pk(
                catalog_products,
                "product_key",
                {
                    "product_key": product_key,
                    "merchant_id": merchant_id,
                    "platform": platform,
                    "source_product_id": source_pid,
                    "catalog_track": "internal_merchant",
                    "truth_tier": "primary",
                    "readiness_tier": readiness_tier,
                    "source_system": source_system,
                    "source_ref": source_ref,
                    "source_domain": source_domain_value,
                    "suppression_reason": None,
                    "suppressed_at": None,
                    "suppression_metadata": None,
                    "title": product.title,
                    "description": _description_for_ingest,
                    "brand": brand,
                    "product_type": product.product_type,
                    # Fix Plan B T4: case/trim-normalized (no semantic rename).
                    "category": _normalized_category,
                    # Phase O-5: category_path classified inline at sync time
                    # via services/pdp_category_classifier.fold_category_from_variants.
                    # Source enum + confidence let downstream agents trust-gate.
                    "category_path": _cat_path,
                    "category_label_source": _cat_source,
                    "category_confidence": _cat_confidence,
                    # Durable category_kind (mig 151): skincare/haircare from the
                    # path, supplement from conservative ingestible detection.
                    "category_kind": resolve_category_kind(
                        _cat_path,
                        product.product_type,
                        product.title,
                        _tags_for_ingest,
                    ),
                    # Durable top-level vertical (mig 173): resolved once at
                    # intake so both this repo and the Node serving layer read the
                    # same value. The title signal includes tags + description so a
                    # SKU whose vertical only shows in its tags (e.g. a supplement
                    # with a noisy fetched product_type) resolves consistently with
                    # the report path (_resolved_vertical_for_ctx), which reads this
                    # persisted column first. See services.vertical_profiles.
                    # Computed once above into _resolved_vertical so the intake
                    # brake (T3) counts the same value that is persisted.
                    "resolved_vertical": _resolved_vertical,
                    # Phase O-5b (#3): merchant-published fashion fields
                    # (Shopify metafields / admin-injected). Only set when
                    # actually populated — NULL stays NULL so the LLM
                    # extractor v2 (fallback) can fill in later without
                    # racing the merchant_payload write.
                    **({"material": _payload_material,
                        "material_source": "merchant_payload",
                        "material_confidence": 1.0} if _payload_material else {}),
                    **({"care": _payload_care,
                        "care_source": "merchant_payload",
                        "care_confidence": 1.0} if _payload_care else {}),
                    **({"size_guide": _payload_size_guide,
                        "size_guide_source": "merchant_payload",
                        "size_guide_confidence": 1.0} if _payload_size_guide else {}),
                    # Phase O-1: persist merchant-supplied tags. Always write
                    # a list (possibly empty) on this path so future operators
                    # can tell "ingest saw the feed and it was empty" from
                    # NULL ("row predates the column"). See
                    # docs/PDP_ONBOARDING_PLAYBOOK.md gap #2 + mig 075.
                    "tags": _tags_for_ingest,
                    # Phase O-2: derived taxonomy v1 — price_tier (deterministic
                    # from product.price), use_case_tags / lifestyle_tags
                    # (conservative keyword extraction), demographic (NULL if
                    # ambiguous). Pure function in services/pdp_taxonomy.py.
                    # See docs/PDP_ONBOARDING_PLAYBOOK.md + mig 076.
                    **_taxonomy_v1,
                    # Phase O-4: compute lifecycle stage. mig 077.
                    "pdp_lifecycle_stage": compute_lifecycle_stage(_stage_input),
                    "canonical_url": canonical_url,
                    "image_url": product.image_url,
                    "product_payload": raw_product,
                    "pdp_scope": "merchant_owned",
                    "pdp_scope_source": "merchant_sync",
                    "pdp_scope_set_at": _utcnow(),
                    "pivota_signature_id": pivota_fields["pivota_signature_id"],
                    "pivota_canonical_url": pivota_fields["pivota_canonical_url"],
                    "pivota_signature_minted_at": pivota_fields["pivota_signature_minted_at"],
                    # Stage 1 of the PDP architecture roadmap (mig 083):
                    # content-derived identity. Same physical product
                    # across merchants/paths gets the same content_key,
                    # which Stage 2 uses to auto-group product_group_members.
                    # See services/catalog_identity.py + plan.
                    "content_key": content_key,
                    # ADR-011 (mig 178): the barcode as a GTIN match-attribute,
                    # canonicalized, never folded into content_key (SPU model).
                    # Written regardless of the identity flag so the match
                    # corpus builds ahead of rollout.
                    "gtin": _canonical_gtin(product.barcode),
                    # Stage 2a (mig 084): mark this row as freshly seen
                    # in a Path A sync. The nightly sweep
                    # (scripts/sweep_stale_catalog_products.py) compares
                    # this against catalog_merchants.last_full_sync_at
                    # to detect rows whose upstream Shopify products
                    # were deleted (the MOYU cohort pattern).
                    "last_seen_in_sync_at": _utcnow(),
                    # Default stays 'live' on insert. Reasserted here
                    # on UPDATE so a row that the sweep previously
                    # marked 'stale' returns to 'live' once the merchant
                    # restores it upstream.
                    "sync_status": "live",
                    "freshness_json": {
                        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
                        "observed_at": _utcnow().isoformat(),
                    },
                },
            )
            if _is_stale_after_sync_tombstone(existing_product):
                stats["products_recovered_after_stale"] += 1
                if audit is not None:
                    audit.record_info({"recovered_after_stale": 1})
            stats["products_ingested"] += 1

            # ADR-009 ratified decision 1 (no-fallback): stamp the deterministic
            # SINGLETON product_group_id at ingestion so every product carries a
            # pg and the offer path keys on pg with ZERO branching. ON CONFLICT
            # DO NOTHING — a row already in a real/curated group is untouched
            # (no auto-merge). content_key NULL → left pg-NULL + observable log.
            await ensure_singleton_group_membership(
                merchant_id=merchant_id,
                platform=platform,
                source_product_id=source_pid,
                content_key=content_key,
            )

            # Phase O-5b live-ingest enrichment: spawn a fire-and-forget
            # LLM batched extraction for material/care/size_guide on rows
            # the merchant_payload couldn't cover. Gated upstream by
            # FASHION_EXTRACT_ENABLED + fashion category prefix; bounded
            # by a module-level semaphore so a 1k-product sync can't fan
            # out unbounded. Closes the "backfill-only" gap that left
            # newly-onboarded wix products at 0% extraction.
            _schedule_fashion_enrichment(
                product_key=product_key,
                title=product.title,
                description=_description_for_ingest,
                html_blob=None,
                category_path=_cat_path,
                payload_filled={
                    "material": bool(_payload_material),
                    "care": bool(_payload_care),
                    "size_guide": bool(_payload_size_guide),
                },
            )

            await _upsert_field_fact(
                entity_type="product",
                entity_id=product_key,
                field_family="identity",
                field_key="title",
                source_system=source_system,
                source_ref=source_ref,
                value=product.title,
                fresh_until=_utcnow() + timedelta(hours=24),
                confidence=Decimal("1.0"),
                merchant_id=merchant_id,
                commerce_index_source_id=(commerce_index_source or {}).get("source_id"),
                commerce_index_source_kind=(commerce_index_source or {}).get("field_source_kind"),
            )
            if brand:
                await _upsert_field_fact(
                    entity_type="product",
                    entity_id=product_key,
                    field_family="identity",
                    field_key="brand",
                    source_system=source_system,
                    source_ref=source_ref,
                    value=brand,
                    fresh_until=_utcnow() + timedelta(days=7),
                    confidence=Decimal("1.0"),
                    merchant_id=merchant_id,
                    commerce_index_source_id=(commerce_index_source or {}).get("source_id"),
                    commerce_index_source_kind=(commerce_index_source or {}).get("field_source_kind"),
                )

            variants = _iter_variants(product)
            beauty_usage_rows: List[Dict[str, Any]] = []
            beauty_shade_rows: List[Dict[str, Any]] = []
            beauty_asset_rows: List[Dict[str, Any]] = []
            beauty_compat_rows: List[Dict[str, Any]] = []
            ingredient_row_upserts = 0

            for variant in variants:
                source_variant_id = str(
                    variant.variant_id
                    or variant.id
                    or product_key  # no variant id: collide only within this product
                )
                sku_key = await _resolve_catalog_sku_key(
                    merchant_id=merchant_id,
                    platform=platform,
                    product_key=product_key,
                    source_variant_id=source_variant_id,
                )
                variant_price = _safe_decimal(variant.price if variant.price is not None else product.price)
                compare_at = _safe_decimal(
                    variant.compare_at_price if variant.compare_at_price is not None else product.compare_at_price
                )
                inventory_quantity = _safe_int(
                    variant.inventory_quantity if variant.inventory_quantity is not None else product.inventory_quantity
                )
                raw_variant = _raw_variant_payload(raw_product, variant, source_variant_id)
                strong_identifier = extract_strong_identifier(
                    raw_variant,
                    getattr(variant, "platform_metadata", None),
                    variant.model_dump(mode="json"),
                    raw_product,
                    metadata,
                    {"barcode": product.barcode},
                )
                _record_identifier_audit(audit, strong_identifier)

                await _upsert_by_pk(
                    catalog_skus,
                    "sku_key",
                    {
                        "sku_key": sku_key,
                        "product_key": product_key,
                        "merchant_id": merchant_id,
                        "platform": platform,
                        "source_product_id": str(product.product_id or product.id),
                        "source_variant_id": source_variant_id,
                        "source_domain": source_domain_value,
                        "suppression_reason": None,
                        "suppressed_at": None,
                        "suppression_metadata": None,
                        "sku": variant.sku or product.sku,
                        "barcode": strong_identifier.value if strong_identifier else None,
                        "title": variant.title or product.title,
                        "currency": product.currency,
                        "image_url": variant.image_url or product.image_url,
                        "visible_attributes": product.visible_attributes or {},
                        "visible_option_labels": list(variant.visible_option_labels or []),
                        "ingredient_ids": list(product.ingredient_ids or []),
                        "sku_payload": variant.model_dump(mode="json"),
                        "readiness_tier": readiness_tier,
                    },
                )
                stats["skus_ingested"] += 1

                offer_id = make_catalog_offer_id(sku_key, "default", "internal_merchant")
                list_price = compare_at if compare_at and variant_price and compare_at > variant_price else variant_price
                merchant_effective_price = variant_price
                availability = "in_stock" if (inventory_quantity or 0) > 0 else "out_of_stock"
                offer_mode = "merchant_checkout" if product.orderable is not False else "merchant_view_only"

                offer_values = {
                    "offer_id": offer_id,
                    "sku_key": sku_key,
                    "product_key": product_key,
                    "merchant_id": merchant_id,
                    "catalog_track": "internal_merchant",
                    "truth_tier": "primary",
                    "readiness_tier": readiness_tier,
                    "offer_mode": offer_mode,
                    "channel": "default",
                    # Internal merchant storefront = first-party.
                    # offer_type/why_buy_direct stay unset here -- brand_direct is
                    # only assigned to verified brand merchants (see mig 149
                    # backfill / offer_classification).
                    "is_first_party": True,
                    # `market` here is NOT a claim about geography. The column is
                    # NOT NULL DEFAULT 'US' (mig 149, "refine to real per-offer geo
                    # when modeled") and we hold no country for these stores:
                    # merchant_stores carries store_id/platform/domain/api_key and
                    # no locale, and nothing in this sync path fetches one from the
                    # platform. So this writes the column's own default and RECORDS
                    # that it did, in offer_payload below.
                    #
                    # Why that matters (ADR-024): a defaulted 'US' is
                    # indistinguishable from a known 'US', which is exactly why
                    # index_pipeline_state derives US-buyability from CURRENCY and
                    # says market "carries no signal". Measured 2026-07-29 on the
                    # Wix pilot merch_e68c20b0189746d0 ("Tsingtao Bear"): 433
                    # EUR-priced offers stamped market='US' by this line -- honest
                    # currency, fabricated market. The currency below istruly from the
                    # platform payload; the market is not, and now says so.
                    "market": MARKET_UNKNOWN_DEFAULT,
                    "availability": availability,
                    "inventory_quantity": inventory_quantity,
                    "currency": product.currency,
                    "list_price": list_price,
                    "merchant_effective_price": merchant_effective_price,
                    "estimated_best_price": merchant_effective_price,
                    "price_confidence": Decimal("1.0"),
                    "source_system": source_system,
                    "source_ref": source_ref,
                    "source_domain": source_domain_value,
                    "suppression_reason": None,
                    "suppressed_at": None,
                    "suppression_metadata": None,
                    "offer_payload": {
                        "product_id": str(product.product_id or product.id),
                        "variant_id": source_variant_id,
                        "sku": variant.sku or product.sku,
                        # Provenance for `market` above. Deriving a real market
                        # needs a per-platform store-locale fetch we do not do
                        # yet; until then this key is what lets a reader tell a
                        # DEFAULTED market from a known one. ADR-024 Phase 2a/2b
                        # consumers should treat its presence as "market unknown".
                        "market_provenance": MARKET_PROVENANCE_PLATFORM_DEFAULT,
                    },
                }
                if audit is not None:
                    accepted_offers, skip_reasons, _rejected = validate_catalog_offer_rows(
                        [offer_values],
                        existing_sku_keys={sku_key},
                    )
                    if skip_reasons:
                        audit.record_skips(skip_reasons)
                        stats["offers_skipped"] += sum(skip_reasons.values())
                        for reason, count in skip_reasons.items():
                            stats["offer_skip_reasons"][reason] = stats["offer_skip_reasons"].get(reason, 0) + count
                    if not accepted_offers:
                        continue
                    offer_values = accepted_offers[0]

                await _upsert_by_pk(
                    catalog_offers,
                    "offer_id",
                    offer_values,
                )
                stats["offers_ingested"] += 1
                if audit is not None:
                    audit.record_applied(1)

                await _append_snapshot(
                    catalog_inventory_snapshots,
                    {
                        "offer_id": offer_id,
                        "sku_key": sku_key,
                        "merchant_id": merchant_id,
                        "inventory_quantity": inventory_quantity,
                        "availability": availability,
                        "source_system": source_system,
                        "source_ref": source_ref,
                    },
                )
                await _append_snapshot(
                    catalog_price_snapshots,
                    {
                        "offer_id": offer_id,
                        "sku_key": sku_key,
                        "merchant_id": merchant_id,
                        "currency": product.currency,
                        "list_price": list_price,
                        "merchant_effective_price": merchant_effective_price,
                        "estimated_best_price": merchant_effective_price,
                        "source_system": source_system,
                        "source_ref": source_ref,
                    },
                )

                await _upsert_field_fact(
                    entity_type="offer",
                    entity_id=offer_id,
                    field_family="pricing",
                    field_key="merchant_effective_price",
                    source_system=source_system,
                    source_ref=source_ref,
                    value={
                        "amount": str(merchant_effective_price) if merchant_effective_price is not None else None,
                        "currency": product.currency,
                    },
                    fresh_until=_utcnow() + timedelta(hours=1),
                    confidence=Decimal("1.0"),
                    merchant_id=merchant_id,
                    commerce_index_source_id=(commerce_index_source or {}).get("source_id"),
                    commerce_index_source_kind=(commerce_index_source or {}).get("field_source_kind"),
                )
                await _upsert_field_fact(
                    entity_type="offer",
                    entity_id=offer_id,
                    field_family="inventory",
                    field_key="availability",
                    source_system=source_system,
                    source_ref=source_ref,
                    value={"availability": availability, "inventory_quantity": inventory_quantity},
                    fresh_until=_utcnow() + timedelta(minutes=15),
                    confidence=Decimal("1.0"),
                    merchant_id=merchant_id,
                    commerce_index_source_id=(commerce_index_source or {}).get("source_id"),
                    commerce_index_source_kind=(commerce_index_source or {}).get("field_source_kind"),
                )

                if _beauty_is_candidate(product):
                    raw_inci = _extract_raw_inci(metadata, list(product.ingredient_ids or []))
                    active_ingredients = _extract_active_ingredients(metadata, list(product.ingredient_ids or []))
                    await _upsert_by_pk(
                        beauty_sku_ingredients,
                        "sku_key",
                        {
                            "sku_key": sku_key,
                            "product_key": product_key,
                            "merchant_id": merchant_id,
                            "raw_inci": raw_inci,
                            "normalized_ingredients_json": list(product.ingredient_ids or []),
                            "active_ingredients_json": active_ingredients,
                            "concentration_notes_json": _json_list(
                                _extract_metadata_values(metadata, "concentrations", "concentration_notes")
                            ),
                            "allergen_flags_json": _json_list(_extract_metadata_values(metadata, "allergens", "allergen_flags")),
                            "evidence_refs_json": [source_ref] if source_ref else [],
                            "source_system": source_system,
                        },
                    )
                    ingredient_row_upserts += 1
                    await _upsert_field_fact(
                        entity_type="sku",
                        entity_id=sku_key,
                        field_family="beauty_knowledge",
                        field_key="ingredient_ids",
                        source_system=source_system,
                        source_ref=source_ref,
                        value=list(product.ingredient_ids or []),
                        fresh_until=_utcnow() + timedelta(days=30),
                        confidence=Decimal("0.8"),
                        merchant_id=merchant_id,
                        commerce_index_source_id=(commerce_index_source or {}).get("source_id"),
                        commerce_index_source_kind=(commerce_index_source or {}).get("field_source_kind"),
                    )

                    how_to_use_text, steps = _extract_how_to_use(metadata)
                    if how_to_use_text or steps:
                        beauty_usage_rows.append(
                            {
                                "guide_id": _stable_key("beauty_usage", product_key, sku_key or "product"),
                                "product_key": product_key,
                                "sku_key": sku_key,
                                "merchant_id": merchant_id,
                                "how_to_use_text": how_to_use_text,
                                "steps_json": steps,
                                "frequency": str(metadata.get("usage_frequency") or "").strip() or None,
                                "time_of_day": str(metadata.get("usage_time_of_day") or metadata.get("am_pm") or "").strip()
                                or None,
                                "application_order": _safe_int(metadata.get("application_order")),
                                "warnings_json": _json_list(_extract_metadata_values(metadata, "warnings", "usage_warnings")),
                                "evidence_refs_json": [source_ref] if source_ref else [],
                            }
                        )
                        await _upsert_field_fact(
                            entity_type="sku",
                            entity_id=sku_key,
                            field_family="beauty_knowledge",
                            field_key="how_to_use",
                            source_system=source_system,
                            source_ref=source_ref,
                            value={"text": how_to_use_text, "steps": steps},
                            fresh_until=_utcnow() + timedelta(days=30),
                            confidence=Decimal("0.8"),
                            merchant_id=merchant_id,
                            commerce_index_source_id=(commerce_index_source or {}).get("source_id"),
                            commerce_index_source_kind=(commerce_index_source or {}).get("field_source_kind"),
                        )

                    beauty_shade_rows.extend(
                        [
                            {
                                **shade,
                                "sku_key": sku_key,
                                "product_key": product_key,
                                "merchant_id": merchant_id,
                            }
                            for shade in _extract_shades(product, variant)
                        ]
                    )
                    beauty_asset_rows.extend(
                        [
                            {
                                **asset,
                                "product_key": product_key,
                                "sku_key": sku_key,
                                "merchant_id": merchant_id,
                            }
                            for asset in _extract_tutorial_assets(product, metadata)
                        ]
                    )
                    beauty_compat_rows.extend(
                        _compatibility_rules_from_ingredients(list(product.ingredient_ids or []), merchant_id, product_key, sku_key)
                    )

            if _beauty_is_candidate(product):
                claims = _extract_claims(metadata)
                benefits = _extract_benefits(product, metadata)
                await _upsert_by_pk(
                    beauty_product_profiles,
                    "product_key",
                    {
                        "product_key": product_key,
                        "merchant_id": merchant_id,
                        "taxonomy_json": _beauty_taxonomy(product),
                        "concerns_json": list((product.visible_attributes or {}).get("skin_concern") or []),
                        "claims_json": claims,
                        "routine_phase": str(metadata.get("routine_phase") or metadata.get("usage_stage") or "").strip() or None,
                        "benefits_json": benefits,
                        "profile_payload": {
                            "product_type": product.product_type,
                            "tags": list(product.tags or []),
                        },
                    },
                )
                stats["beauty_profiles_upserted"] += 1

                stats["beauty_ingredient_rows_upserted"] += ingredient_row_upserts
                stats["beauty_usage_guides_upserted"] += await _replace_child_rows_multi(
                    beauty_usage_guides,
                    [beauty_usage_guides.c.product_key == product_key],
                    beauty_usage_rows,
                )
                stats["beauty_shades_upserted"] += await _replace_child_rows_multi(
                    beauty_shades,
                    [beauty_shades.c.product_key == product_key],
                    beauty_shade_rows,
                )
                stats["beauty_content_assets_upserted"] += await _replace_child_rows_multi(
                    beauty_content_assets,
                    [beauty_content_assets.c.product_key == product_key],
                    beauty_asset_rows,
                )
                stats["beauty_compatibility_rules_upserted"] += await _replace_child_rows_multi(
                    beauty_compatibility_rules,
                    [beauty_compatibility_rules.c.product_key == product_key],
                    beauty_compat_rows,
                )

        if content_key:
            # Build the denormalized agent_pdp_view row from the catalog rows we
            # just wrote, BEFORE recompute. Without this, a freshly-synced internal
            # merchant product has no APV row yet (APV was historically only built
            # on seed writes / agent-context requests), so recompute below blocks it
            # at no_seed / no_image even though title+image+description are present
            # on catalog_products. Best-effort: agent_pdp_view is a cache, so a build
            # failure here must never break the source-of-truth ingest commit.
            try:
                from services.agent_pdp_view_assembler import (
                    refresh_agent_pdp_view_for_content_key,
                )

                await refresh_agent_pdp_view_for_content_key(
                    content_key,
                    refresh_source="catalog_sync",
                    db=database,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning({
                    "event": "agent_pdp_view_build_failed",
                    "site": "ingest_standard_products",
                    "content_key": content_key,
                    "error": str(exc),
                })

            # Reap the stale agent_pdp_view row when this product was RE-KEYED:
            # content_key = make_content_key(brand, title, barcode) and a changed
            # title/barcode moves catalog_products to the new content_key in place.
            # Without this, the OLD content_key's view row is orphaned and keeps
            # squatting on the still-live pivota_signature_id (a latent
            # /products/sig_* mis-serve + it blocks the live row from materializing).
            # Ordered AFTER the new-key refresh so the live row exists first.
            # Best-effort: the view is a cache; a reap failure must not break ingest.
            old_content_key = (
                dict(existing_product).get("content_key") if existing_product else None
            )
            if old_content_key and old_content_key != content_key:
                try:
                    from services.agent_pdp_view_assembler import (
                        delete_agent_pdp_view_if_orphaned,
                    )

                    await delete_agent_pdp_view_if_orphaned(
                        old_content_key, db=database
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning({
                        "event": "agent_pdp_view_orphan_reap_failed",
                        "site": "ingest_standard_products",
                        "old_content_key": old_content_key,
                        "new_content_key": content_key,
                        "error": str(exc),
                    })

            try:
                from services.index_pipeline_state_service import recompute_serving_eligibility

                await recompute_serving_eligibility(
                    content_key,
                    reason="catalog_sync",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning({
                    "event": "serving_eligibility_hook_failed",
                    "site": "ingest_standard_products",
                    "content_key": content_key,
                    "error": str(exc),
                })

        # C1 Phase 2: dual-write trust row. Fire-and-forget — a failure here
        # never breaks ingest; the defensive cron (phase 2d) catches drift.
        await upsert_catalog_row_trust(db=database, product_key=product_key)

    if job_id:
        await _upsert_by_pk(
            catalog_sync_jobs,
            "job_id",
            {
                "job_id": job_id,
                "merchant_id": merchant_id,
                "connector": platform,
                "mode": "sync",
                "scope_json": {"platform": platform},
                "status": "completed",
                "stats_json": stats,
                "completed_at": _utcnow(),
            },
        )

    if audit is not None:
        await write_writer_audit_log(audit, db=database)

    # Stage 2a (mig 084): bump catalog_merchants.last_full_sync_at on
    # successful sync completion. The sweep
    # (scripts/sweep_stale_catalog_products.py) uses this as the
    # ground-truth "merchant synced at" timestamp to compare each
    # row's last_seen_in_sync_at against. Without it, the sweep can't
    # tell "merchant hasn't synced lately" from "upstream deleted
    # this row." Stamped only when the function reaches this line —
    # any earlier exception leaves the previous timestamp intact, so
    # a partial-failure sync doesn't trigger spurious tombstoning.
    await database.execute(
        catalog_merchants.update()
        .where(catalog_merchants.c.merchant_id == merchant_id)
        .values(last_full_sync_at=_utcnow())
    )

    # Fix Plan B T3 — intake structure brake. Summarize the share of rows this
    # sync ingested that carried no machine-readable vertical at all, and surface
    # it (with a should_fail verdict) in the returned stats. Threshold is
    # configurable via CATALOG_INGEST_UNRESOLVED_VERTICAL_FAIL_THRESHOLD. Unlike
    # the offline mirror CLI (which turns should_fail into a non-zero exit), a
    # LIVE merchant sync must NOT raise mid-write — so here we surface + log
    # loudly and let the caller act, never rolling back a committed sync.
    _vertical_threshold = _unresolved_vertical_fail_threshold(
        "CATALOG_INGEST_UNRESOLVED_VERTICAL_FAIL_THRESHOLD"
    )
    _vertical_guard = summarize_unresolved_vertical(
        _vertical_rows_unresolved,
        _vertical_rows_considered,
        threshold=_vertical_threshold,
    )
    stats["vertical_guard"] = _vertical_guard
    if _vertical_guard["should_fail"]:
        logger.warning(
            "Catalog ingest intake brake TRIPPED merchant=%s platform=%s %s "
            "(threshold=%.1f%%) — batch has too little machine-readable vertical structure",
            merchant_id, platform, _vertical_guard["summary"], _vertical_threshold * 100,
        )
    else:
        logger.info(
            "Catalog ingest merchant=%s platform=%s %s",
            merchant_id, platform, _vertical_guard["summary"],
        )
    return stats


# ADR: the tombstone prune below has never once executed — its three
# statements failed to PREPARE from #666 (2026-05-26) until the fix in this
# branch, so `POST /sync/{merchant_id}` 500ed after ingest had already
# committed. Making it plannable does not merely repair it, it switches on a
# writer that will tombstone ~2.5 months of accumulated upstream-deleted rows on
# the FIRST operator sync, and `upsert_catalog_row_trust_many` flips those rows
# out of serving inline rather than waiting for cron.
#
# So it ships INERT. Enable deliberately, after checking the count the
# suppressed path logs, and prefer the sanctioned dry-run
# (`scripts/sweep_stale_catalog_products.py --apply`) for the first pass.
_PRUNE_TOMBSTONE_FLAG = "CATALOG_SYNC_PRUNE_TOMBSTONE_ENABLED"
_PRUNE_TOMBSTONE_CAP_VAR = "CATALOG_SYNC_PRUNE_MAX_ROWS"
_PRUNE_TOMBSTONE_DEFAULT_CAP = 500


def _prune_tombstone_enabled() -> bool:
    """Read at call time, not import time, so ops can flip it without a deploy."""
    return str(os.getenv(_PRUNE_TOMBSTONE_FLAG, "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _prune_tombstone_cap() -> int:
    """0 disables the cap. A cap is not a substitute for the flag: it bounds the
    blast radius of a run someone has already decided to make."""
    try:
        return max(0, int(str(os.getenv(_PRUNE_TOMBSTONE_CAP_VAR, "")).strip()
                          or _PRUNE_TOMBSTONE_DEFAULT_CAP))
    except ValueError:
        return _PRUNE_TOMBSTONE_DEFAULT_CAP


async def prune_missing_catalog_products_for_source(
    *,
    merchant_id: str,
    platform: str,
    valid_source_product_ids: List[str],
    source_system: str,
    source_domain: Optional[str] = None,
    sync_run_id: Optional[str] = None,
) -> Dict[str, int]:
    """Tombstone catalog rows that are no longer present in a completed source sync.

    This is scoped to one merchant/platform/source_system/source_domain. Shopify
    refreshes products_cache as the live source of truth, so catalog rows from
    the same store's older completed sync must stop surfacing after the upstream
    product ids disappear. Rows are retained for audit and automatic recovery.
    """
    normalized_ids = [str(item or "").strip() for item in valid_source_product_ids or [] if str(item or "").strip()]
    source_domain_value = str(source_domain or "").strip() or None
    if not source_domain_value:
        logger.warning(
            "Catalog source prune skipped merchant=%s platform=%s source_system=%s reason=missing_source_domain",
            merchant_id,
            platform,
            source_system,
        )
        return {
            "catalog_products": 0,
            "catalog_skus": 0,
            "catalog_offers": 0,
            "skipped_missing_source_domain": 1,
        }

    prune_all = not normalized_ids
    run_id = sync_run_id or make_batch_id("catalog_sync_prune", source_system)
    stats: Dict[str, int] = {
        "catalog_products": 0,
        "catalog_skus": 0,
        "catalog_offers": 0,
    }
    all_stale_product_keys: List[str] = []

    async with database.transaction():
        await database.execute(
            """
            CREATE TEMP TABLE stale_catalog_products ON COMMIT DROP AS
            SELECT product_key, source_product_id
            FROM catalog_products
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND source_system = :source_system
              AND source_domain = :source_domain
              AND (
                :prune_all
                OR source_product_id <> ALL(CAST(:valid_source_product_ids AS text[]))
              )
            """,
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "source_system": source_system,
                "source_domain": source_domain_value,
                "prune_all": prune_all,
                "valid_source_product_ids": normalized_ids,
            },
        )
        await database.execute(
            """
            CREATE TEMP TABLE stale_catalog_skus ON COMMIT DROP AS
            SELECT sku_key, product_key
            FROM catalog_skus
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND source_domain = :source_domain
              AND product_key IN (SELECT product_key FROM stale_catalog_products)
            """,
            {
                "merchant_id": merchant_id,
                "platform": platform,
                "source_domain": source_domain_value,
            },
        )
        await database.execute(
            """
            CREATE TEMP TABLE stale_catalog_offers ON COMMIT DROP AS
            SELECT offer_id, product_key, sku_key
            FROM catalog_offers
            WHERE merchant_id = :merchant_id
              AND source_domain = :source_domain
              AND (
                product_key IN (SELECT product_key FROM stale_catalog_products)
                OR sku_key IN (SELECT sku_key FROM stale_catalog_skus)
              )
            """,
            {
                "merchant_id": merchant_id,
                "source_domain": source_domain_value,
            },
        )

        stale_product_count = int(
            await database.fetch_val("SELECT count(*) FROM stale_catalog_products") or 0
        )
        # Fetch all stale keys inside the transaction (temp table ON COMMIT DROP).
        # First 10 go to the audit log; all keys feed the trust-row recompute
        # after the transaction commits.
        all_stale_rows = await database.fetch_all(
            """
            SELECT product_key
            FROM stale_catalog_products
            ORDER BY product_key
            """
        )
        all_stale_product_keys = [
            str(dict(row).get("product_key") or "").strip()
            for row in all_stale_rows or []
            if str(dict(row).get("product_key") or "").strip()
        ]
        product_key_sample = all_stale_product_keys[:10]

        tombstone_params = {
            "suppression_reason": STALE_AFTER_SYNC,
            "sync_run_id": run_id,
            "pruned_by": "catalog_sync_service",
            "source_domain": source_domain_value,
        }
        update_statements: List[Tuple[str, str]] = [
            (
                "catalog_products",
                """
                WITH tombstoned AS (
                  UPDATE catalog_products
                  SET suppression_reason = :suppression_reason,
                      suppressed_at = NOW(),
                      suppression_metadata = jsonb_build_object(
                        'sync_run_id', CAST(:sync_run_id AS text),
                        'pruned_by', CAST(:pruned_by AS text),
                        'source_domain', CAST(:source_domain AS text)
                      ),
                      updated_at = NOW()
                  WHERE product_key IN (SELECT product_key FROM stale_catalog_products)
                    AND suppressed_at IS NULL
                  RETURNING 1
                )
                SELECT count(*) FROM tombstoned
                """,
            ),
            (
                "catalog_skus",
                """
                WITH tombstoned AS (
                  UPDATE catalog_skus
                  SET suppression_reason = :suppression_reason,
                      suppressed_at = NOW(),
                      suppression_metadata = jsonb_build_object(
                        'sync_run_id', CAST(:sync_run_id AS text),
                        'pruned_by', CAST(:pruned_by AS text),
                        'source_domain', CAST(:source_domain AS text)
                      ),
                      updated_at = NOW()
                  WHERE sku_key IN (SELECT sku_key FROM stale_catalog_skus)
                    AND suppressed_at IS NULL
                  RETURNING 1
                )
                SELECT count(*) FROM tombstoned
                """,
            ),
            (
                "catalog_offers",
                """
                WITH tombstoned AS (
                  UPDATE catalog_offers
                  SET suppression_reason = :suppression_reason,
                      suppressed_at = NOW(),
                      suppression_metadata = jsonb_build_object(
                        'sync_run_id', CAST(:sync_run_id AS text),
                        'pruned_by', CAST(:pruned_by AS text),
                        'source_domain', CAST(:source_domain AS text)
                      ),
                      updated_at = NOW()
                  WHERE offer_id IN (SELECT offer_id FROM stale_catalog_offers)
                    AND suppressed_at IS NULL
                  RETURNING 1
                )
                SELECT count(*) FROM tombstoned
                """,
            ),
        ]

        # The kill-switch. Everything above this point is READ-ONLY: the stale
        # set is computed into temp tables, counted and logged, so an operator
        # can see exactly what a run would do before enabling it.
        cap = _prune_tombstone_cap()
        # len(all_stale_product_keys), not stale_product_count: the key list is
        # what the writes and the trust recompute actually act on.
        stale_rows = len(all_stale_product_keys)
        suppressed = None
        if not _prune_tombstone_enabled():
            suppressed = "flag_disabled"
        elif cap and stale_rows > cap:
            suppressed = "over_cap"

        if suppressed:
            stats["stale_detected"] = stale_rows
            stats["tombstone_suppressed"] = 1
            logger.warning(
                "catalog prune tombstone SUPPRESSED (%s) merchant=%s platform=%s "
                "source_domain=%s stale_rows=%s cap=%s — set %s=true to enable "
                "(sample=%s)",
                suppressed, merchant_id, platform, source_domain_value,
                stale_rows, cap, _PRUNE_TOMBSTONE_FLAG,
                product_key_sample,
            )
            # Nothing downstream should recompute trust for rows we did not touch.
            all_stale_product_keys = []
        else:
            for table_name, sql in update_statements:
                tombstoned = await database.fetch_val(sql, tombstone_params)
                stats[table_name] = int(tombstoned or 0)

        if not suppressed and stats["catalog_products"] > 0:
            audit = WriterAuditAccumulator(
                writer_name=CATALOG_SYNC_PRUNE_WRITER,
                batch_id=run_id,
            )
            audit.record_skips({STALE_AFTER_SYNC: stats["catalog_products"]})
            audit.reasons["tombstoned_product_keys_sample"] = product_key_sample
            await write_writer_audit_log(audit, db=database)

    # C1 Phase 2: recompute trust for tombstoned rows so serving_decision flips
    # to 'blocked' with ROW_TOMBSTONED immediately instead of waiting for cron.
    if all_stale_product_keys:
        await upsert_catalog_row_trust_many(
            db=database, product_keys=all_stale_product_keys
        )

    logger.info(
        "Catalog source prune completed merchant=%s platform=%s source_system=%s source_domain=%s stale_products=%s products_tombstoned=%s offers_tombstoned=%s",
        merchant_id,
        platform,
        source_system,
        source_domain_value,
        stale_product_count,
        stats.get("catalog_products", 0),
        stats.get("catalog_offers", 0),
    )
    return stats


async def create_catalog_sync_job(
    *,
    merchant_id: str,
    connector: str,
    mode: str,
    scope: Optional[Dict[str, Any]] = None,
    requested_by: Optional[str] = None,
) -> Dict[str, Any]:
    job_id = _stable_key("catalog_job", merchant_id, connector, mode, uuid.uuid4().hex)
    row = {
        "job_id": job_id,
        "merchant_id": merchant_id,
        "connector": connector,
        "mode": mode,
        "scope_json": scope or {},
        "status": "pending",
        "requested_by": requested_by,
        "stats_json": {},
        "error_message": None,
        "started_at": None,
        "completed_at": None,
    }
    await _upsert_by_pk(catalog_sync_jobs, "job_id", row)
    created = await _fetch_one_by_pk(catalog_sync_jobs, "job_id", job_id)
    return created or row


async def get_catalog_sync_job(job_id: str) -> Optional[Dict[str, Any]]:
    return await _fetch_one_by_pk(catalog_sync_jobs, "job_id", job_id)


async def record_catalog_sync_event(
    *,
    merchant_id: str,
    connector: str,
    event_type: str,
    topic: Optional[str],
    payload_json: Dict[str, Any],
    source_ref: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    event_id = _stable_key("catalog_event", merchant_id, connector, event_type, source_ref or uuid.uuid4().hex)
    row = {
        "event_id": event_id,
        "merchant_id": merchant_id,
        "connector": connector,
        "event_type": event_type,
        "topic": topic,
        "status": "pending",
        "payload_json": payload_json,
        "source_ref": source_ref,
        "error_message": None,
        "occurred_at": occurred_at,
        "processed_at": None,
    }
    await _upsert_by_pk(catalog_sync_events, "event_id", row)
    created = await _fetch_one_by_pk(catalog_sync_events, "event_id", event_id)
    return created or row


async def mark_catalog_sync_event_processed(event_id: str, *, status: str = "processed", error_message: Optional[str] = None) -> None:
    existing = await _fetch_one_by_pk(catalog_sync_events, "event_id", event_id)
    if not existing:
        return
    existing["status"] = status
    existing["error_message"] = error_message
    existing["processed_at"] = _utcnow()
    await _upsert_by_pk(catalog_sync_events, "event_id", existing)


async def sync_products_cache_to_catalog(
    *,
    merchant_id: str,
    platform: Optional[str],
    limit: int = 500,
    include_expired: bool = True,
    source_system: str = "products_cache",
    source_ref: Optional[str] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    if platform:
        query = """
            SELECT product_data
            FROM products_cache
            WHERE merchant_id = :merchant_id
              AND platform = :platform
        """
        params: Dict[str, Any] = {"merchant_id": merchant_id, "platform": platform}
    else:
        query = """
            SELECT product_data
            FROM products_cache
            WHERE merchant_id = :merchant_id
        """
        params = {"merchant_id": merchant_id}

    if not include_expired:
        query += " AND expires_at > NOW()"
    query += " ORDER BY cached_at DESC"
    if limit > 0:
        query += " LIMIT :limit"
        params["limit"] = limit

    rows = await database.fetch_all(query, params)
    payloads = [dict(row).get("product_data") for row in rows if dict(row).get("product_data")]
    normalized_payloads = []
    seen: set[str] = set()
    for payload in payloads:
        obj = _json_dict(payload) if not isinstance(payload, dict) else payload
        product_id = str(obj.get("product_id") or obj.get("id") or "").strip()
        platform_value = str(obj.get("platform") or platform or "").strip()
        dedupe_key = f"{platform_value}:{product_id}"
        if not product_id or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized_payloads.append(obj)

    return await ingest_standard_products(
        merchant_id=merchant_id,
        platform=platform or "shopify",
        product_payloads=normalized_payloads,
        source_system=source_system,
        source_ref=source_ref,
        job_id=job_id,
    )


async def run_catalog_sync_job(job_id: str) -> Dict[str, Any]:
    job = await get_catalog_sync_job(job_id)
    if not job:
        raise RuntimeError(f"Catalog sync job not found: {job_id}")

    scope = _json_dict(job.get("scope_json"))
    connector = str(job.get("connector") or "shopify")
    merchant_id = str(job.get("merchant_id") or "").strip()
    mode = str(job.get("mode") or "reconcile")

    await _upsert_by_pk(
        catalog_sync_jobs,
        "job_id",
        {
            **job,
            "status": "running",
            "started_at": _utcnow(),
            "error_message": None,
        },
    )

    try:
        stats = await sync_products_cache_to_catalog(
            merchant_id=merchant_id,
            platform=str(scope.get("platform") or connector or "shopify"),
            limit=int(scope.get("limit") or 500),
            include_expired=bool(scope.get("include_expired", True)),
            source_system=str(scope.get("source_system") or "products_cache"),
            source_ref=scope.get("source_ref") or job_id,
            job_id=job_id,
        )
        # Onboarding→audit readiness: the catalog is now populated, so enqueue a
        # quality backfill for this merchant. The scheduler's quality-drain tick
        # processes it (deterministic, no LLM), populating
        # product_quality_snapshot — without which a freshly-synced merchant's
        # first v3 audit comes back blocked (content_richness 25 + the
        # serving-eligibility gate depend on it). Best-effort: never fail the
        # catalog sync on this hook.
        try:
            from db.product_quality_backfill_jobs import create_quality_backfill_job
            await create_quality_backfill_job(
                merchant_id=merchant_id,
                platform=str(scope.get("platform") or connector or "shopify"),
                requested_by="catalog_sync_autodrain",
                force_refresh=False,
                missing_only=True,
            )
        except Exception as exc:  # noqa: BLE001 - readiness hook is best-effort
            logger.warning(
                "catalog_sync: quality-backfill enqueue failed merchant=%s: %s",
                merchant_id, exc,
            )
        updated = await get_catalog_sync_job(job_id)
        if updated:
            return updated
        return {
            "job_id": job_id,
            "merchant_id": merchant_id,
            "connector": connector,
            "mode": mode,
            "status": "completed",
            "stats_json": stats,
        }
    except Exception as exc:
        await _upsert_by_pk(
            catalog_sync_jobs,
            "job_id",
            {
                **job,
                "status": "failed",
                "error_message": str(exc),
                "completed_at": _utcnow(),
            },
        )
        raise


async def rebuild_beauty_verticals_for_merchant(
    *,
    merchant_id: str,
    platform: Optional[str] = "shopify",
    limit: int = 1000,
) -> Dict[str, Any]:
    await database.execute(beauty_product_profiles.delete().where(beauty_product_profiles.c.merchant_id == merchant_id))
    await database.execute(beauty_sku_ingredients.delete().where(beauty_sku_ingredients.c.merchant_id == merchant_id))
    await database.execute(beauty_usage_guides.delete().where(beauty_usage_guides.c.merchant_id == merchant_id))
    await database.execute(beauty_shades.delete().where(beauty_shades.c.merchant_id == merchant_id))
    await database.execute(beauty_compatibility_rules.delete().where(beauty_compatibility_rules.c.merchant_id == merchant_id))
    await database.execute(beauty_content_assets.delete().where(beauty_content_assets.c.merchant_id == merchant_id))
    stats = await sync_products_cache_to_catalog(
        merchant_id=merchant_id,
        platform=platform,
        limit=limit,
        include_expired=True,
        source_system="beauty_rebuild",
        source_ref=f"beauty_rebuild:{merchant_id}",
    )
    return {"merchant_id": merchant_id, "rebuild_stats": stats, "rebuild_at": _utcnow()}


async def reconcile_catalog_incentives_for_merchant(
    *,
    merchant_id: str,
    payment_incentives: Optional[List[PaymentIncentiveInput]] = None,
    source_system: str = "merchant_config",
) -> Dict[str, Any]:
    payment_incentives_synced = 0
    offer_links_synced = 0
    offer_rows = await database.fetch_all(select(catalog_offers).where(catalog_offers.c.merchant_id == merchant_id))
    for item in payment_incentives or []:
        incentive_id = item.incentive_id or _stable_key(
            "pay_incentive",
            merchant_id,
            item.label,
            item.incentive_type,
            item.card_network or "",
            item.issuer_name or "",
        )
        await _upsert_by_pk(
            catalog_payment_incentives,
            "incentive_id",
            {
                "incentive_id": incentive_id,
                "merchant_id": merchant_id,
                "incentive_type": item.incentive_type,
                "funding_source": item.funding_source,
                "payment_method_type": item.payment_method_type,
                "card_network": item.card_network,
                "issuer_name": item.issuer_name,
                "wallet_type": item.wallet_type,
                "installment_provider": item.installment_provider,
                "label": item.label,
                "benefit_kind": item.benefit_kind,
                "benefit_value": item.benefit_value,
                "benefit_currency": item.benefit_currency,
                "market": item.market,
                "eligibility_confidence": item.eligibility_confidence,
                "source_system": item.source_system,
                "source_ref": item.source_ref,
                "status": item.status,
                "starts_at": item.starts_at,
                "ends_at": item.ends_at,
                "metadata_json": item.metadata,
            },
        )
        await _upsert_by_pk(
            catalog_incentive_rules,
            "rule_id",
            {
                "rule_id": _stable_key("incentive_rule", incentive_id),
                "incentive_id": incentive_id,
                "merchant_id": merchant_id,
                "rule_type": "payment_eligibility",
                "scope_json": item.rule_scope,
                "conditions_json": item.rule_conditions,
                "schedule_json": item.schedule,
                "human_rule": item.human_rule,
            },
        )
        payment_incentives_synced += 1
        for offer in offer_rows:
            offer_dict = dict(offer)
            link_id = _stable_key("offer_incentive_link", offer_dict.get("offer_id"), incentive_id)
            await _upsert_by_pk(
                catalog_offer_incentive_links,
                "link_id",
                {
                    "link_id": link_id,
                    "offer_id": offer_dict.get("offer_id"),
                    "incentive_id": incentive_id,
                    "merchant_id": merchant_id,
                    "relationship_type": "eligible",
                    "priority": 0,
                },
            )
            offer_links_synced += 1

    return {
        "merchant_id": merchant_id,
        "source_system": source_system,
        "payment_incentives_synced": payment_incentives_synced,
        "offer_links_synced": offer_links_synced,
        "reconciled_at": _utcnow(),
    }


async def store_catalog_quote_snapshot(
    *,
    quote_id: str,
    merchant_id: str,
    offer_id: Optional[str],
    sku_key: Optional[str],
    product_key: Optional[str],
    currency: Optional[str],
    list_price: Optional[Decimal],
    merchant_effective_price: Optional[Decimal],
    estimated_best_price: Optional[Decimal],
    exact_quote_price: Optional[Decimal],
    incentives: List[Dict[str, Any]],
    quote_payload: Dict[str, Any],
    expires_at: Optional[datetime],
) -> None:
    quote_snapshot_id = _stable_key("quote_snapshot", merchant_id, quote_id, offer_id or sku_key or "")
    await _upsert_by_pk(
        catalog_quote_snapshots,
        "quote_snapshot_id",
        {
            "quote_snapshot_id": quote_snapshot_id,
            "quote_id": quote_id,
            "merchant_id": merchant_id,
            "offer_id": offer_id,
            "sku_key": sku_key,
            "product_key": product_key,
            "currency": currency,
            "list_price": list_price,
            "merchant_effective_price": merchant_effective_price,
            "estimated_best_price": estimated_best_price,
            "exact_quote_price": exact_quote_price,
            "incentives_json": incentives,
            "quote_payload_json": quote_payload,
            "expires_at": expires_at,
        },
    )
