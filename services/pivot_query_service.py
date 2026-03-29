from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from db.database import database
from models.catalog import (
    BeautyVerticalPayload,
    IncentiveNode,
    MerchantNode,
    OfferNode,
    PivotOffersResolveRequest,
    PivotOffersResolveResponse,
    PivotPaymentContext,
    PivotPricing,
    PivotQueryRequest,
    PivotQueryResponse,
    PivotQuoteRequest,
    PivotQuoteResponse,
    PivotResultItem,
    ProductNode,
    SkuNode,
)
from services.catalog_sync_service import store_catalog_quote_snapshot
from services.external_seed_search import (
    dedupe_external_seed_rows,
    ensure_json_obj,
    fetch_external_seed_rows,
    seed_search_terms,
    stable_external_product_id,
)
from services.quote_service import QuoteService


logger = logging.getLogger(__name__)
_PIVOT_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "from",
    "in",
    "my",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def _row_dict(row: Any) -> Dict[str, Any]:
    return dict(row) if row else {}


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


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _normalize_query(query: Optional[str]) -> str:
    return " ".join(str(query or "").strip().lower().split())


def _strip_accents(text: str) -> str:
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def _tokenize_relevance(text: str) -> List[str]:
    if not text:
        return []
    normalized = _strip_accents(text.lower())
    return [t for t in re.split(r"[^a-z0-9]+", normalized) if len(t) > 2]


def _filter_relevance_terms(terms: List[str]) -> List[str]:
    if not terms:
        return []
    deduped: List[str] = []
    for term in terms:
        if not term or term in _PIVOT_QUERY_STOPWORDS:
            continue
        if term not in deduped:
            deduped.append(term)
    return deduped


def _vertical_intent(query: str) -> bool:
    lowered = _normalize_query(query)
    if not lowered:
        return False
    vertical_tokens = (
        "ingredient",
        "ingredients",
        "inci",
        "shade",
        "swatch",
        "tutorial",
        "routine",
        "how to use",
        "how-to-use",
        "how to",
        "application",
        "apply",
    )
    return any(token in lowered for token in vertical_tokens)


def _payment_context_matches(incentive: Dict[str, Any], payment_context: Optional[PivotPaymentContext]) -> bool:
    if payment_context is None:
        return True
    checks = (
        ("payment_method_type", payment_context.payment_method_type),
        ("card_network", payment_context.card_network),
        ("issuer_name", payment_context.issuer_name),
        ("wallet_type", payment_context.wallet_type),
        ("installment_provider", payment_context.installment_provider),
    )
    for key, requested in checks:
        configured = str(incentive.get(key) or "").strip().lower()
        if configured and requested and configured != str(requested).strip().lower():
            return False
    return True


def _apply_incentive_to_price(price: Optional[Decimal], incentive: Dict[str, Any]) -> Optional[Decimal]:
    if price is None:
        return None
    benefit_kind = str(incentive.get("benefit_kind") or "").strip().lower()
    benefit_value = _to_decimal(incentive.get("benefit_value"))
    if benefit_value is None:
        return price
    if benefit_kind in {"percentage_off", "percent_off", "discount_percentage", "discount_rate"}:
        discounted = price * (Decimal("1") - (benefit_value / Decimal("100")))
        return max(discounted, Decimal("0"))
    if benefit_kind in {"amount_off", "fixed_amount_off", "discount_amount"}:
        return max(price - benefit_value, Decimal("0"))
    if benefit_kind in {"fixed_price", "set_price"}:
        return max(benefit_value, Decimal("0"))
    return price


def _build_incentive_node(incentive: Dict[str, Any]) -> IncentiveNode:
    return IncentiveNode(
        incentive_id=str(incentive.get("incentive_id") or ""),
        label=str(incentive.get("label") or "Incentive"),
        incentive_type=str(incentive.get("incentive_type") or "payment_incentive"),
        benefit_kind=str(incentive.get("benefit_kind") or "unknown"),
        benefit_value=_to_decimal(incentive.get("benefit_value")),
        benefit_currency=incentive.get("benefit_currency"),
        funding_source=incentive.get("funding_source"),
        payment_method_type=incentive.get("payment_method_type"),
        card_network=incentive.get("card_network"),
        issuer_name=incentive.get("issuer_name"),
        wallet_type=incentive.get("wallet_type"),
        installment_provider=incentive.get("installment_provider"),
        market=incentive.get("market"),
        eligibility_confidence=_to_decimal(incentive.get("eligibility_confidence")),
        source_system=incentive.get("source_system"),
    )


async def _fetch_offer_incentives(
    offer_ids: Iterable[str],
    *,
    payment_context: Optional[PivotPaymentContext],
) -> Dict[str, List[IncentiveNode]]:
    normalized_offer_ids = [str(item or "").strip() for item in offer_ids if str(item or "").strip()]
    if not normalized_offer_ids:
        return {}

    params: Dict[str, Any] = {}
    placeholders: List[str] = []
    for idx, offer_id in enumerate(normalized_offer_ids):
        key = f"offer_id_{idx}"
        params[key] = offer_id
        placeholders.append(f":{key}")

    rows = await database.fetch_all(
        f"""
        SELECT
            l.offer_id,
            i.incentive_id,
            i.label,
            i.incentive_type,
            i.benefit_kind,
            i.benefit_value,
            i.benefit_currency,
            i.funding_source,
            i.payment_method_type,
            i.card_network,
            i.issuer_name,
            i.wallet_type,
            i.installment_provider,
            i.market,
            i.eligibility_confidence,
            i.source_system
        FROM catalog_offer_incentive_links l
        JOIN catalog_payment_incentives i ON i.incentive_id = l.incentive_id
        WHERE l.offer_id IN ({", ".join(placeholders)})
          AND COALESCE(i.status, 'active') = 'active'
        ORDER BY l.offer_id, l.priority ASC, i.updated_at DESC
        """,
        params,
    )

    incentives_by_offer: Dict[str, List[IncentiveNode]] = {}
    for row in rows:
        data = _row_dict(row)
        if not _payment_context_matches(data, payment_context):
            continue
        offer_id = str(data.get("offer_id") or "").strip()
        if not offer_id:
            continue
        incentives_by_offer.setdefault(offer_id, []).append(_build_incentive_node(data))
    return incentives_by_offer


async def _fetch_beauty_vertical_payload(product_key: str, sku_key: Optional[str]) -> Dict[str, Any]:
    profile = _row_dict(
        await database.fetch_one(
            """
            SELECT taxonomy_json, concerns_json, claims_json, routine_phase, benefits_json
            FROM beauty_product_profiles
            WHERE product_key = :product_key
            LIMIT 1
            """,
            {"product_key": product_key},
        )
    )
    ingredient_row = _row_dict(
        await database.fetch_one(
            """
            SELECT raw_inci, normalized_ingredients_json, active_ingredients_json
            FROM beauty_sku_ingredients
            WHERE sku_key = :sku_key
            LIMIT 1
            """,
            {"sku_key": sku_key},
        )
    ) if sku_key else {}
    usage_row = _row_dict(
        await database.fetch_one(
            """
            SELECT how_to_use_text, steps_json
            FROM beauty_usage_guides
            WHERE product_key = :product_key
              AND (:sku_key IS NULL OR sku_key = :sku_key)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            {"product_key": product_key, "sku_key": sku_key},
        )
    )
    shade_rows = [
        _row_dict(row)
        for row in await database.fetch_all(
            """
            SELECT shade_id, shade_name, shade_code, shade_family, undertone, finish, swatch_refs_json, media_refs_json
            FROM beauty_shades
            WHERE product_key = :product_key
              AND (:sku_key IS NULL OR sku_key = :sku_key)
            ORDER BY updated_at DESC, shade_name ASC
            """,
            {"product_key": product_key, "sku_key": sku_key},
        )
    ]
    asset_rows = [
        _row_dict(row)
        for row in await database.fetch_all(
            """
            SELECT asset_id, asset_type, title, url, thumbnail_url, sort_order
            FROM beauty_content_assets
            WHERE product_key = :product_key
              AND (:sku_key IS NULL OR sku_key = :sku_key)
            ORDER BY sort_order ASC, updated_at DESC
            """,
            {"product_key": product_key, "sku_key": sku_key},
        )
    ]
    compatibility_rows = [
        _row_dict(row)
        for row in await database.fetch_all(
            """
            SELECT rule_type, subject_ingredients_json, related_ingredients_json, verdict, rationale, evidence_refs_json
            FROM beauty_compatibility_rules
            WHERE product_key = :product_key
              AND (:sku_key IS NULL OR sku_key = :sku_key)
            ORDER BY updated_at DESC
            """,
            {"product_key": product_key, "sku_key": sku_key},
        )
    ]

    payload = BeautyVerticalPayload(
        taxonomy=_json_dict(profile.get("taxonomy_json")),
        concerns=[str(item or "").strip() for item in _json_list(profile.get("concerns_json")) if str(item or "").strip()],
        claims=[str(item or "").strip() for item in _json_list(profile.get("claims_json")) if str(item or "").strip()],
        routine_phase=profile.get("routine_phase"),
        benefits=[str(item or "").strip() for item in _json_list(profile.get("benefits_json")) if str(item or "").strip()],
        ingredients=[str(item or "").strip() for item in _json_list(ingredient_row.get("normalized_ingredients_json")) if str(item or "").strip()],
        active_ingredients=[item for item in _json_list(ingredient_row.get("active_ingredients_json")) if isinstance(item, dict)],
        how_to_use=str(usage_row.get("how_to_use_text") or "").strip() or None,
        usage_steps=[str(item or "").strip() for item in _json_list(usage_row.get("steps_json")) if str(item or "").strip()],
        shades=shade_rows,
        tutorials=asset_rows,
        compatibility_rules=compatibility_rows,
    )
    return payload.model_dump()


def _estimate_best_price(base_price: Optional[Decimal], incentives: List[IncentiveNode]) -> Optional[Decimal]:
    best = base_price
    for incentive in incentives:
        candidate = _apply_incentive_to_price(base_price, incentive.model_dump())
        if candidate is not None and (best is None or candidate < best):
            best = candidate
    return best


def _build_canonical_offer_node(row: Dict[str, Any], incentives: List[IncentiveNode]) -> OfferNode:
    base_price = _to_decimal(row.get("merchant_effective_price"))
    estimated_best_price = _to_decimal(row.get("estimated_best_price"))
    computed_best = _estimate_best_price(base_price, incentives)
    if computed_best is not None and (estimated_best_price is None or computed_best < estimated_best_price):
        estimated_best_price = computed_best
    return OfferNode(
        offer_id=str(row.get("offer_id") or ""),
        catalog_track=str(row.get("offer_catalog_track") or row.get("catalog_track") or "internal_merchant"),
        truth_tier=str(row.get("offer_truth_tier") or row.get("truth_tier") or "primary"),
        readiness_tier=str(row.get("offer_readiness_tier") or row.get("readiness_tier") or "commerce_ready"),
        offer_mode=str(row.get("offer_mode") or "merchant_checkout"),
        source_system=row.get("offer_source_system") or row.get("source_system"),
        availability=row.get("availability"),
        inventory_quantity=row.get("inventory_quantity"),
        pricing=PivotPricing(
            currency=row.get("currency"),
            list_price=_to_decimal(row.get("list_price")),
            merchant_effective_price=base_price,
            estimated_best_price=estimated_best_price,
            exact_quote_price=None,
            price_confidence=_to_decimal(row.get("price_confidence")),
        ),
        incentives=incentives,
    )


def _canonical_match_reason(row: Dict[str, Any], query: str) -> Dict[str, Any]:
    lowered = _normalize_query(query)
    exact = lowered and lowered in {
        _normalize_query(row.get("product_title")),
        _normalize_query(row.get("brand")),
        _normalize_query(row.get("merchant_name")),
        _normalize_query(row.get("sku")),
        _normalize_query(row.get("source_variant_id")),
        _normalize_query(row.get("source_product_id")),
    }
    return {
        "lane": "exact_lookup" if exact else "catalog_discovery",
        "query": query,
        "matched_on": {
            "merchant_name": row.get("merchant_name"),
            "product_title": row.get("product_title"),
            "sku": row.get("sku"),
            "source_variant_id": row.get("source_variant_id"),
        },
        "exact_match": bool(exact),
    }


async def _fetch_canonical_search_rows(
    *,
    query: str,
    merchant_id: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    lowered = _normalize_query(query)
    if not lowered:
        return []
    normalized_limit = max(1, int(limit or 20))
    candidate_limit = min(max(normalized_limit * 4, 25), 200)
    row_limit = min(max(normalized_limit * 6, 50), 500)
    vertical_search = _vertical_intent(query)
    params: Dict[str, Any] = {
        "query_exact": lowered,
        "query_like": f"%{lowered}%",
        "candidate_limit": candidate_limit,
        "row_limit": row_limit,
    }
    merchant_clause = ""
    if merchant_id:
        merchant_clause = "AND p.merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    vertical_where = ""
    vertical_score = ""
    if vertical_search:
        vertical_where = """
            OR LOWER(COALESCE(CAST(s.visible_option_labels AS TEXT), '')) LIKE :query_like OR
            LOWER(COALESCE(CAST(s.ingredient_ids AS TEXT), '')) LIKE :query_like
        """
        vertical_score = """
            + CASE WHEN LOWER(COALESCE(CAST(s.visible_option_labels AS TEXT), '')) LIKE :query_like THEN 20 ELSE 0 END
            + CASE WHEN LOWER(COALESCE(CAST(s.ingredient_ids AS TEXT), '')) LIKE :query_like THEN 15 ELSE 0 END
        """

    rows = await database.fetch_all(
        f"""
        WITH candidate_skus AS (
            SELECT
                m.merchant_id AS merchant_id,
                m.merchant_name AS merchant_name,
                m.primary_platform AS merchant_primary_platform,
                p.product_key,
                p.source_product_id,
                p.title AS product_title,
                p.description AS product_description,
                p.brand,
                p.product_type,
                p.category,
                p.canonical_url,
                p.image_url AS product_image_url,
                p.catalog_track,
                p.truth_tier,
                p.readiness_tier,
                p.source_system,
                p.freshness_json,
                p.updated_at AS product_updated_at,
                s.sku_key,
                s.source_variant_id,
                s.sku,
                s.barcode,
                s.title AS sku_title,
                s.visible_attributes,
                s.visible_option_labels,
                s.ingredient_ids,
                s.image_url AS sku_image_url,
                (
                    CASE WHEN LOWER(COALESCE(s.sku, '')) = :query_exact THEN 120 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(s.source_variant_id, '')) = :query_exact THEN 110 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(p.source_product_id, '')) = :query_exact THEN 105 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(p.title, '')) = :query_exact THEN 100 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(m.merchant_name, '')) = :query_exact THEN 90 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(p.brand, '')) = :query_exact THEN 80 ELSE 0 END
                    {vertical_score}
                ) AS rank_score
            FROM catalog_products p
            JOIN catalog_skus s ON s.product_key = p.product_key
            LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
            WHERE (
                LOWER(COALESCE(p.title, '')) LIKE :query_like OR
                LOWER(COALESCE(p.brand, '')) LIKE :query_like OR
                LOWER(COALESCE(m.merchant_name, '')) LIKE :query_like OR
                LOWER(COALESCE(s.sku, '')) LIKE :query_like OR
                LOWER(COALESCE(s.title, '')) LIKE :query_like OR
                LOWER(COALESCE(s.source_variant_id, '')) LIKE :query_like OR
                LOWER(COALESCE(p.source_product_id, '')) LIKE :query_like
                {vertical_where}
            )
            {merchant_clause}
            ORDER BY rank_score DESC, p.updated_at DESC, s.updated_at DESC
            LIMIT :candidate_limit
        )
        SELECT
            c.merchant_id,
            c.merchant_name,
            c.merchant_primary_platform,
            c.product_key,
            c.source_product_id,
            c.product_title,
            c.product_description,
            c.brand,
            c.product_type,
            c.category,
            c.canonical_url,
            c.product_image_url,
            c.catalog_track,
            c.truth_tier,
            c.readiness_tier,
            c.source_system,
            c.freshness_json,
            c.product_updated_at,
            c.sku_key,
            c.source_variant_id,
            c.sku,
            c.barcode,
            c.sku_title,
            c.visible_attributes,
            c.visible_option_labels,
            c.ingredient_ids,
            c.sku_image_url,
            o.offer_id,
            o.catalog_track AS offer_catalog_track,
            o.truth_tier AS offer_truth_tier,
            o.readiness_tier AS offer_readiness_tier,
            o.offer_mode,
            o.availability,
            o.inventory_quantity,
            o.currency,
            o.list_price,
            o.merchant_effective_price,
            o.estimated_best_price,
            o.price_confidence,
            o.source_system AS offer_source_system,
            o.offer_payload,
            c.rank_score + CASE WHEN o.catalog_track = 'internal_merchant' THEN 10 ELSE 0 END AS rank_score
        FROM candidate_skus c
        JOIN catalog_offers o ON o.sku_key = c.sku_key
        ORDER BY rank_score DESC, c.product_updated_at DESC, o.updated_at DESC
        LIMIT :row_limit
        """,
        params,
    )
    return [_row_dict(row) for row in rows]


async def _fetch_canonical_rows_for_product(product_key: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT
            m.merchant_id AS merchant_id,
            m.merchant_name AS merchant_name,
            m.primary_platform AS merchant_primary_platform,
            p.product_key,
            p.source_product_id,
            p.title AS product_title,
            p.description AS product_description,
            p.brand,
            p.product_type,
            p.category,
            p.canonical_url,
            p.image_url AS product_image_url,
            p.catalog_track,
            p.truth_tier,
            p.readiness_tier,
            p.source_system,
            p.freshness_json,
            p.updated_at AS product_updated_at,
            s.sku_key,
            s.source_variant_id,
            s.sku,
            s.barcode,
            s.title AS sku_title,
            s.visible_attributes,
            s.visible_option_labels,
            s.ingredient_ids,
            s.image_url AS sku_image_url,
            o.offer_id,
            o.catalog_track AS offer_catalog_track,
            o.truth_tier AS offer_truth_tier,
            o.readiness_tier AS offer_readiness_tier,
            o.offer_mode,
            o.availability,
            o.inventory_quantity,
            o.currency,
            o.list_price,
            o.merchant_effective_price,
            o.estimated_best_price,
            o.price_confidence,
            o.source_system AS offer_source_system,
            o.offer_payload
        FROM catalog_products p
        JOIN catalog_skus s ON s.product_key = p.product_key
        JOIN catalog_offers o ON o.sku_key = s.sku_key
        LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
        WHERE p.product_key = :product_key
        ORDER BY o.updated_at DESC
        """,
        {"product_key": product_key},
    )
    return [_row_dict(row) for row in rows]


async def _fetch_canonical_rows_for_sku(sku_key: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT
            m.merchant_id AS merchant_id,
            m.merchant_name AS merchant_name,
            m.primary_platform AS merchant_primary_platform,
            p.product_key,
            p.source_product_id,
            p.title AS product_title,
            p.description AS product_description,
            p.brand,
            p.product_type,
            p.category,
            p.canonical_url,
            p.image_url AS product_image_url,
            p.catalog_track,
            p.truth_tier,
            p.readiness_tier,
            p.source_system,
            p.freshness_json,
            p.updated_at AS product_updated_at,
            s.sku_key,
            s.source_variant_id,
            s.sku,
            s.barcode,
            s.title AS sku_title,
            s.visible_attributes,
            s.visible_option_labels,
            s.ingredient_ids,
            s.image_url AS sku_image_url,
            o.offer_id,
            o.catalog_track AS offer_catalog_track,
            o.truth_tier AS offer_truth_tier,
            o.readiness_tier AS offer_readiness_tier,
            o.offer_mode,
            o.availability,
            o.inventory_quantity,
            o.currency,
            o.list_price,
            o.merchant_effective_price,
            o.estimated_best_price,
            o.price_confidence,
            o.source_system AS offer_source_system,
            o.offer_payload
        FROM catalog_skus s
        JOIN catalog_products p ON p.product_key = s.product_key
        JOIN catalog_offers o ON o.sku_key = s.sku_key
        LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
        WHERE s.sku_key = :sku_key
        ORDER BY o.updated_at DESC
        """,
        {"sku_key": sku_key},
    )
    return [_row_dict(row) for row in rows]


def _group_canonical_rows(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        sku_key = str(row.get("sku_key") or "").strip()
        if not sku_key:
            continue
        grouped.setdefault(sku_key, []).append(row)
    return grouped


async def _build_canonical_items(
    rows: List[Dict[str, Any]],
    *,
    query: Optional[str],
    payment_context: Optional[PivotPaymentContext],
    include_vertical_payload: bool,
    include_incentives: bool,
) -> List[PivotResultItem]:
    if not rows:
        return []

    grouped = _group_canonical_rows(rows)
    offer_ids = [str(row.get("offer_id") or "").strip() for row in rows if str(row.get("offer_id") or "").strip()]
    incentives_by_offer = (
        await _fetch_offer_incentives(offer_ids, payment_context=payment_context)
        if include_incentives
        else {}
    )
    items: List[PivotResultItem] = []

    for sku_key, sku_rows in grouped.items():
        first = sku_rows[0]
        offers: List[OfferNode] = []
        for row in sku_rows:
            offer_id = str(row.get("offer_id") or "").strip()
            offer_incentives = incentives_by_offer.get(offer_id, [])
            offers.append(_build_canonical_offer_node(row, offer_incentives))
        verticals: Dict[str, Any] = {}
        if include_vertical_payload:
            vertical_payload = await _fetch_beauty_vertical_payload(
                str(first.get("product_key") or ""),
                sku_key,
            )
            if any(vertical_payload.values()):
                verticals["beauty"] = vertical_payload

        items.append(
            PivotResultItem(
                merchant=MerchantNode(
                    merchant_id=first.get("merchant_id"),
                    merchant_name=first.get("merchant_name"),
                    primary_platform=first.get("merchant_primary_platform"),
                ),
                product=ProductNode(
                    product_key=first.get("product_key"),
                    source_product_id=first.get("source_product_id"),
                    title=first.get("product_title"),
                    description=first.get("product_description"),
                    brand=first.get("brand"),
                    product_type=first.get("product_type"),
                    category=first.get("category"),
                    canonical_url=first.get("canonical_url"),
                    image_url=first.get("product_image_url"),
                ),
                sku=SkuNode(
                    sku_key=sku_key,
                    source_variant_id=first.get("source_variant_id"),
                    sku=first.get("sku"),
                    barcode=first.get("barcode"),
                    title=first.get("sku_title"),
                    visible_attributes=_json_dict(first.get("visible_attributes")),
                    visible_option_labels=[str(item or "").strip() for item in _json_list(first.get("visible_option_labels")) if str(item or "").strip()],
                    ingredient_ids=[str(item or "").strip() for item in _json_list(first.get("ingredient_ids")) if str(item or "").strip()],
                ),
                offers=offers,
                catalog_track=str(first.get("catalog_track") or "internal_merchant"),
                truth_tier=str(first.get("truth_tier") or "primary"),
                readiness_tier=str(first.get("readiness_tier") or "commerce_ready"),
                freshness=_json_dict(first.get("freshness_json")) or {
                    "updated_at": str(first.get("product_updated_at") or ""),
                },
                source_system=first.get("source_system"),
                match_explanation=_canonical_match_reason(first, query or ""),
                verticals=verticals,
            )
        )
    return items


def _readiness_from_seed(seed_data: Dict[str, Any]) -> str:
    has_ingredients = bool(seed_data.get("pdp_ingredients_raw") or seed_data.get("ingredients"))
    has_how_to_use = bool(seed_data.get("pdp_how_to_use_raw") or seed_data.get("how_to_use"))
    if has_ingredients and has_how_to_use:
        return "knowledge_ready"
    if has_ingredients or has_how_to_use:
        return "vertical_ready"
    return "commerce_ready"


def _external_relevance_score(row: Dict[str, Any]) -> int:
    try:
        return max(0, int(row.get("brand_term_hit") or 0))
    except Exception:
        return 0


def _external_seed_ingredient_blob(seed_data: Dict[str, Any]) -> str:
    values: List[str] = []
    for key in (
        "ingredient_ids",
        "reviewed_ingredient_ids",
        "canonical_ingredient_ids",
        "ingredients",
    ):
        raw = seed_data.get(key)
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list):
            values.extend(str(item or "") for item in raw)
    return " ".join(v for v in values if v).strip()


def _external_text_relevance_score(row: Dict[str, Any], query: str) -> float:
    seed_data = ensure_json_obj(row.get("seed_data"))
    q_ascii = _strip_accents(_normalize_query(query))
    title = _strip_accents(
        str(row.get("title") or seed_data.get("title") or "").strip().lower()
    )
    description = _strip_accents(
        str(seed_data.get("description") or seed_data.get("pdp_description_raw") or "").strip().lower()
    )
    product_type = _strip_accents(
        str(
            seed_data.get("product_type")
            or seed_data.get("category")
            or row.get("category")
            or ""
        ).strip().lower()
    )
    brand = _strip_accents(str(seed_data.get("brand") or "").strip().lower())
    domain = _strip_accents(str(row.get("domain") or "").strip().lower())
    canonical_url = _strip_accents(str(row.get("canonical_url") or "").strip().lower())
    destination_url = _strip_accents(str(row.get("destination_url") or "").strip().lower())
    ingredient_blob = _strip_accents(_external_seed_ingredient_blob(seed_data).lower())
    blob = " ".join(
        part
        for part in [
            title,
            description,
            product_type,
            brand,
            domain,
            canonical_url,
            destination_url,
            ingredient_blob,
        ]
        if part
    ).strip()
    title_compact = re.sub(r"[^a-z0-9]+", "", title)
    blob_compact = re.sub(r"[^a-z0-9]+", "", blob)
    q_compact = re.sub(r"[^a-z0-9]+", "", q_ascii)

    if q_ascii:
        if q_ascii in title:
            return 1.0 if q_ascii == title else 0.9
        if q_ascii in description:
            return 0.7
        if q_compact and len(q_compact) >= 4 and q_compact in title_compact:
            return 0.85
        if q_compact and len(q_compact) >= 4 and q_compact in blob_compact:
            return 0.8

    query_terms = _filter_relevance_terms(_tokenize_relevance(q_ascii))
    if not query_terms and q_compact and len(q_compact) > 2:
        query_terms = [q_compact]
    if not query_terms:
        return 0.12

    title_matches = sum(
        1 for term in query_terms if term and (term in title or term in title_compact)
    )
    blob_matches = sum(
        1 for term in query_terms if term and (term in blob or term in blob_compact)
    )
    if blob_matches == 0:
        return 0.12

    if title_matches == len(query_terms):
        score = 0.88
    elif title_matches > 0:
        score = 0.56 + (title_matches / len(query_terms)) * 0.24
    else:
        score = 0.5 + (blob_matches / len(query_terms)) * 0.22

    if "cleanser" in query_terms and "cleanser" in title:
        score += 0.05
    if "moisturizer" in query_terms and "moisturizer" in title:
        score += 0.05
    if "sunscreen" in query_terms and "sunscreen" in title:
        score += 0.05
    if "serum" in query_terms and "serum" in title:
        score += 0.05
    if "gentle" in query_terms and "gentle" in title:
        score += 0.08
    if "acne" in query_terms and ("acne" in title or "acne" in canonical_url or "acne" in destination_url):
        score += 0.08
    if (
        ("fragrance" in query_terms or "free" in query_terms)
        and ("fragrance free" in blob or "fragrance-free" in blob)
    ):
        score += 0.08

    for anchor in ("cleanser", "moisturizer", "sunscreen", "serum"):
        if anchor in query_terms and anchor not in blob:
            score -= 0.2

    if "eye cream" in title and "eye" not in query_terms:
        score -= 0.18

    bundle_markers = (" routine", "duo", " set", " kit")
    if any(marker in f" {blob}" for marker in bundle_markers) and not any(
        marker.strip() in query_terms for marker in ("duo", "set", "kit", "routine")
    ):
        score -= 0.12

    return round(max(0.12, min(score, 1.0)), 4)


def _build_external_item(row: Dict[str, Any], query: str, *, source_order: int) -> PivotResultItem:
    seed_data = ensure_json_obj(row.get("seed_data"))
    destination_url = str(row.get("destination_url") or "").strip()
    canonical_url = str(row.get("canonical_url") or "").strip() or destination_url
    brand_term_hit = _external_relevance_score(row)
    text_relevance_score = _external_text_relevance_score(row, query)
    # Keep SQL term-hit ranking as a light tie-breaker. The gateway's external
    # main path is still primarily text-driven, and letting brand_term_hit
    # dominate turns too many rows into indistinguishable 2/3-point ties.
    relevance_score = round(
        text_relevance_score + min(0.08, brand_term_hit * 0.02),
        4,
    )
    external_product_id = (
        str(row.get("external_product_id") or "").strip()
        or seed_data.get("external_product_id")
        or stable_external_product_id(canonical_url)
    )
    pricing = PivotPricing(
        currency=row.get("price_currency"),
        list_price=_to_decimal(row.get("price_amount")),
        merchant_effective_price=_to_decimal(row.get("price_amount")),
        estimated_best_price=_to_decimal(row.get("price_amount")),
        exact_quote_price=None,
        price_confidence=Decimal("0.6"),
    )
    return PivotResultItem(
        merchant=MerchantNode(
            merchant_id=None,
            merchant_name=str(seed_data.get("brand") or row.get("domain") or "").strip() or None,
            primary_platform="external_referral",
        ),
        product=ProductNode(
            product_key=None,
            source_product_id=external_product_id,
            title=str(row.get("title") or seed_data.get("title") or "").strip() or None,
            description=str(seed_data.get("description") or seed_data.get("pdp_description_raw") or "").strip() or None,
            brand=str(seed_data.get("brand") or row.get("domain") or "").strip() or None,
            product_type=str(seed_data.get("product_type") or "").strip() or None,
            category=str(seed_data.get("category") or "").strip() or None,
            canonical_url=canonical_url or None,
            image_url=row.get("image_url"),
        ),
        sku=SkuNode(
            sku_key=None,
            source_variant_id=None,
            sku=str(seed_data.get("sku") or "").strip() or None,
            barcode=None,
            title=str(row.get("title") or "").strip() or None,
            visible_attributes={},
            visible_option_labels=[],
            ingredient_ids=[],
        ),
        offers=[
            OfferNode(
                offer_id=f"external::{external_product_id}",
                catalog_track="external_referral",
                truth_tier="fallback",
                readiness_tier=_readiness_from_seed(seed_data),
                offer_mode="redirect",
                source_system="external_product_seeds",
                availability=row.get("availability"),
                inventory_quantity=None,
                pricing=pricing,
                incentives=[],
            )
        ],
        catalog_track="external_referral",
        truth_tier="fallback",
        readiness_tier=_readiness_from_seed(seed_data),
        freshness={"updated_at": row.get("updated_at"), "created_at": row.get("created_at")},
        source_system="external_product_seeds",
        match_explanation={
            "lane": "external_fallback",
            "query": query,
            "destination_url": destination_url,
            "relevance_score": relevance_score,
            "text_relevance_score": text_relevance_score,
            "brand_term_hit": brand_term_hit,
            "source_order": source_order,
        },
        verticals={},
    )


def _sort_items(items: List[PivotResultItem]) -> List[PivotResultItem]:
    def sort_key(item: PivotResultItem) -> tuple[int, int, float, int, Decimal]:
        internal_boost = 1 if item.catalog_track == "internal_merchant" else 0
        exact_boost = 1 if item.match_explanation.get("exact_match") else 0
        relevance_boost = 0.0
        try:
            relevance_boost = float(item.match_explanation.get("relevance_score") or 0.0)
        except Exception:
            relevance_boost = 0.0
        source_order = 999999
        if item.catalog_track == "external_referral":
            try:
                raw_source_order = item.match_explanation.get("source_order")
                if raw_source_order is not None:
                    source_order = int(raw_source_order)
            except Exception:
                source_order = 999999
        best_price = None
        for offer in item.offers:
            candidate = offer.pricing.estimated_best_price or offer.pricing.merchant_effective_price or offer.pricing.list_price
            if candidate is not None and (best_price is None or candidate < best_price):
                best_price = candidate
        return (
            -exact_boost,
            -internal_boost,
            -relevance_boost,
            source_order,
            best_price if best_price is not None else Decimal("999999"),
        )

    return sorted(items, key=sort_key)


async def _fetch_external_fallback_items(request: PivotQueryRequest) -> List[PivotResultItem]:
    query_terms = seed_search_terms(request.query)
    external_limit = min(max(request.limit * 2, 30), 200)
    stage_a_result = await fetch_external_seed_rows(
        database=database,
        market=request.market,
        query=request.query,
        limit=external_limit,
        include_seed_data_text_match=False,
        only_unattached=False,
        query_timeout_seconds=0.9,
        required_terms=None,
        prefer_terms=query_terms or None,
        scope="default",
        use_required_terms_filter=False,
        include_total_count=False,
    )
    external_rows = stage_a_result.get("rows") or []
    if not external_rows and _normalize_query(request.query):
        stage_b_result = await fetch_external_seed_rows(
            database=database,
            market=request.market,
            query=request.query,
            limit=external_limit,
            include_seed_data_text_match=True,
            only_unattached=False,
            query_timeout_seconds=1.6,
            required_terms=None,
            prefer_terms=query_terms or None,
            scope="default",
            use_required_terms_filter=False,
            include_total_count=False,
        )
        external_rows = stage_b_result.get("rows") or []
    deduped_rows = dedupe_external_seed_rows(external_rows, limit=external_limit)
    return [
        _build_external_item(row, request.query, source_order=idx)
        for idx, row in enumerate(deduped_rows)
    ]


async def search_pivot_catalog(request: PivotQueryRequest) -> PivotQueryResponse:
    started = time.perf_counter()
    canonical_rows = await _fetch_canonical_search_rows(
        query=request.query,
        merchant_id=request.merchant_id,
        limit=request.limit,
    )
    canonical_items = await _build_canonical_items(
        canonical_rows,
        query=request.query,
        payment_context=request.payment_context,
        include_vertical_payload=_vertical_intent(request.query),
        include_incentives=request.include_incentives,
    )

    external_items: List[PivotResultItem] = []
    if request.include_external and len(canonical_items) < max(3, request.limit):
        external_items = await _fetch_external_fallback_items(request)

    items = _sort_items((canonical_items + external_items)[: request.limit * 2])[: request.limit]
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    if elapsed_ms >= 3000:
        logger.warning(
            "pivot_search_slow",
            extra={
                "query": request.query,
                "merchant_id": request.merchant_id,
                "elapsed_ms": elapsed_ms,
                "canonical_rows": len(canonical_rows),
                "canonical_items": len(canonical_items),
                "external_items": len(external_items),
                "limit": request.limit,
            },
        )
    return PivotQueryResponse(query=request.query, total=len(items), items=items)


async def get_pivot_product(product_key: str) -> Optional[PivotResultItem]:
    rows = await _fetch_canonical_rows_for_product(product_key)
    items = await _build_canonical_items(
        rows,
        query=product_key,
        payment_context=None,
        include_vertical_payload=True,
        include_incentives=True,
    )
    return items[0] if items else None


async def get_pivot_sku(sku_key: str) -> Optional[PivotResultItem]:
    rows = await _fetch_canonical_rows_for_sku(sku_key)
    items = await _build_canonical_items(
        rows,
        query=sku_key,
        payment_context=None,
        include_vertical_payload=True,
        include_incentives=True,
    )
    return items[0] if items else None


async def resolve_pivot_offers(request: PivotOffersResolveRequest) -> PivotOffersResolveResponse:
    started = time.perf_counter()
    items: List[PivotResultItem] = []
    if request.sku_key:
        item = await get_pivot_sku(request.sku_key)
        if item:
            items = [item]
    elif request.product_key:
        item = await get_pivot_product(request.product_key)
        if item:
            items = [item]
    elif request.query:
        search_result = await search_pivot_catalog(
            PivotQueryRequest(
                query=request.query,
                merchant_id=request.merchant_id,
                market=request.market,
                limit=10,
                include_external=request.include_external,
                include_incentives=True,
                payment_context=request.payment_context,
            )
        )
        items = search_result.items

    flattened_offers: List[OfferNode] = []
    resolved_product_key: Optional[str] = request.product_key
    resolved_sku_key: Optional[str] = request.sku_key
    for item in items:
        if not resolved_product_key:
            resolved_product_key = item.product.product_key
        if not resolved_sku_key:
            resolved_sku_key = item.sku.sku_key
        flattened_offers.extend(item.offers)

    response = PivotOffersResolveResponse(
        merchant_id=request.merchant_id,
        product_key=resolved_product_key,
        sku_key=resolved_sku_key,
        offers=flattened_offers,
        offers_count=len(flattened_offers),
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    if elapsed_ms >= 3000:
        logger.warning(
            "pivot_offers_resolve_slow",
            extra={
                "query": request.query,
                "merchant_id": request.merchant_id,
                "product_key": request.product_key,
                "sku_key": request.sku_key,
                "elapsed_ms": elapsed_ms,
                "offers_count": len(flattened_offers),
            },
        )
    return response


async def _fetch_offer_row(offer_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT
            o.offer_id,
            o.sku_key,
            o.product_key,
            o.merchant_id,
            o.currency,
            o.list_price,
            o.merchant_effective_price,
            o.estimated_best_price,
            o.offer_payload,
            s.source_variant_id,
            p.source_product_id
        FROM catalog_offers o
        JOIN catalog_skus s ON s.sku_key = o.sku_key
        JOIN catalog_products p ON p.product_key = o.product_key
        WHERE o.offer_id = :offer_id
        LIMIT 1
        """,
        {"offer_id": offer_id},
    )
    return _row_dict(row) if row else None


async def _fetch_default_offer_for_sku(sku_key: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT
            o.offer_id,
            o.sku_key,
            o.product_key,
            o.merchant_id,
            o.currency,
            o.list_price,
            o.merchant_effective_price,
            o.estimated_best_price,
            o.offer_payload,
            s.source_variant_id,
            p.source_product_id
        FROM catalog_offers o
        JOIN catalog_skus s ON s.sku_key = o.sku_key
        JOIN catalog_products p ON p.product_key = o.product_key
        WHERE o.sku_key = :sku_key
        ORDER BY o.updated_at DESC
        LIMIT 1
        """,
        {"sku_key": sku_key},
    )
    return _row_dict(row) if row else None


async def _fetch_default_offer_for_product(product_key: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT
            o.offer_id,
            o.sku_key,
            o.product_key,
            o.merchant_id,
            o.currency,
            o.list_price,
            o.merchant_effective_price,
            o.estimated_best_price,
            o.offer_payload,
            s.source_variant_id,
            p.source_product_id
        FROM catalog_offers o
        JOIN catalog_skus s ON s.sku_key = o.sku_key
        JOIN catalog_products p ON p.product_key = o.product_key
        WHERE o.product_key = :product_key
        ORDER BY o.updated_at DESC
        LIMIT 1
        """,
        {"product_key": product_key},
    )
    return _row_dict(row) if row else None


async def preview_pivot_quote(request: PivotQuoteRequest) -> PivotQuoteResponse:
    resolved_rows: List[Dict[str, Any]] = []
    quote_items: List[Dict[str, Any]] = []

    for item in request.items:
        row: Optional[Dict[str, Any]] = None
        if item.offer_id:
            row = await _fetch_offer_row(item.offer_id)
        elif item.sku_key:
            row = await _fetch_default_offer_for_sku(item.sku_key)
        elif item.product_key:
            row = await _fetch_default_offer_for_product(item.product_key)
        elif item.product_id and item.variant_id:
            row = {
                "offer_id": None,
                "sku_key": item.sku_key,
                "product_key": item.product_key,
                "merchant_id": request.merchant_id,
                "currency": None,
                "list_price": None,
                "merchant_effective_price": None,
                "estimated_best_price": None,
                "source_product_id": item.product_id,
                "source_variant_id": item.variant_id,
            }

        if not row:
            continue

        offer_payload = _json_dict(row.get("offer_payload"))
        product_id = str(offer_payload.get("product_id") or row.get("source_product_id") or item.product_id or "").strip()
        variant_id = str(offer_payload.get("variant_id") or row.get("source_variant_id") or item.variant_id or "").strip()
        if not product_id or not variant_id:
            continue

        resolved_rows.append(row)
        quote_items.append(
            {
                "product_id": product_id,
                "variant_id": variant_id,
                "quantity": item.quantity,
            }
        )

    if not quote_items:
        return PivotQuoteResponse(
            merchant_id=request.merchant_id,
            pricing=PivotPricing(),
            incentives=[],
            quote_payload={"error": "NO_RESOLVABLE_ITEMS"},
        )

    quote_service = QuoteService()
    quote_result = await quote_service.preview_quote(
        merchant_id=request.merchant_id,
        agent_id=None,
        items=quote_items,
        discount_codes=request.discount_codes,
        customer_email=request.customer_email,
        shipping_address=request.shipping_address,
        selected_delivery_option=request.selected_delivery_option,
        brief_id=None,
        brief_schema_version=None,
    )

    offer_incentives = await _fetch_offer_incentives(
        [str(row.get("offer_id") or "").strip() for row in resolved_rows if str(row.get("offer_id") or "").strip()],
        payment_context=request.payment_context,
    )
    flattened_incentives: List[IncentiveNode] = []
    for nodes in offer_incentives.values():
        flattened_incentives.extend(nodes)

    subtotal = _to_decimal(quote_result.get("pricing", {}).get("subtotal"))
    discount_total = _to_decimal(quote_result.get("pricing", {}).get("discount_total")) or Decimal("0")
    shipping_fee = _to_decimal(quote_result.get("pricing", {}).get("shipping_fee")) or Decimal("0")
    tax = _to_decimal(quote_result.get("pricing", {}).get("tax")) or Decimal("0")
    total = _to_decimal(quote_result.get("pricing", {}).get("total"))
    merchant_effective_price = subtotal - discount_total if subtotal is not None else None
    estimated_best_price = _estimate_best_price(total, flattened_incentives) if total is not None else None

    for row in resolved_rows:
        offer_id = str(row.get("offer_id") or "").strip() or None
        incentives = [node.model_dump() for node in offer_incentives.get(offer_id or "", [])]
        await store_catalog_quote_snapshot(
            quote_id=str(quote_result.get("quote_id") or ""),
            merchant_id=request.merchant_id,
            offer_id=offer_id,
            sku_key=row.get("sku_key"),
            product_key=row.get("product_key"),
            currency=quote_result.get("currency"),
            list_price=_to_decimal(row.get("list_price")),
            merchant_effective_price=_to_decimal(row.get("merchant_effective_price")),
            estimated_best_price=_to_decimal(row.get("estimated_best_price")),
            exact_quote_price=total,
            incentives=incentives,
            quote_payload=quote_result,
            expires_at=quote_result.get("expires_at"),
        )

    return PivotQuoteResponse(
        quote_id=str(quote_result.get("quote_id") or ""),
        merchant_id=request.merchant_id,
        pricing=PivotPricing(
            currency=quote_result.get("currency"),
            list_price=subtotal,
            merchant_effective_price=merchant_effective_price,
            estimated_best_price=estimated_best_price or total,
            exact_quote_price=total,
            price_confidence=Decimal("1.0"),
        ),
        incentives=flattened_incentives,
        quote_payload=quote_result,
    )
