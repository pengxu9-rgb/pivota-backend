from __future__ import annotations

import json
import logging
import re
import os
import time
import unicodedata
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from db.database import database
from models.catalog import (
    BeautyVerticalPayload,
    EvidenceProfile,
    IncentiveNode,
    MerchantNode,
    OfferNode,
    RequiredDisclaimer,
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
from services.claim_safety import (
    CATEGORY_HAIRCARE,
    required_disclaimers_for_category,
    substantiated_product_claims,
)
from services import haircare_attributes
from services.beauty_enrichment import extract_key_actives, infer_concerns
from services.skincare_attributes import (
    detect_fragrance_free,
    extract_format,
    extract_spf_value,
    merge_concentration_into_actives,
)
from services.offer_buyability import annotate_offer_nodes
from services.offer_classification import (
    classify_offer_type,
    is_first_party_track,
    select_best_us_offer,
)
from services.offer_seller_identity import derive_offer_seller_identity
from services.pdp_category_classifier import category_path_prefix_for_query
from services.canonical_sitemap_candidates import (
    electable_sig_exists as _electable_sig_exists,
)
from services.pdp_renderability import (
    compile_pg as _compile_pg,
    sig_pdp_will_render_sql as _sig_pdp_will_render_sql,
)
from sqlalchemy import String as _SAString, and_ as _and_, column as _sacolumn
from sqlalchemy import literal_column as _literal_column, select as _select, table as _satable

# Migration 181, local Core handle (same pattern as the canonical routes).
_cce = _satable(
    "content_canonical_election",
    _sacolumn("content_key", _SAString),
    _sacolumn("canonical_sig_id", _SAString),
)
from services.beauty_external_ranking import (
    BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION,
    RankedExternalBeautyCandidate,
    build_ranked_external_beauty_candidate,
    rank_external_seed_rows,
    score_external_beauty_candidate,
)
from services.external_seed_search import (
    ensure_json_obj,
    fetch_external_seed_rows,
    seed_search_terms,
)
from services.payment_offer_evidence_service import (
    PaymentOfferTarget,
    empty_payment_offer_evidence,
    resolve_payment_offer_evidence_for_targets,
)
from services.savings_presentation_service import build_savings_presentation
from services.query_semantic_class import classify_query_semantic_class
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


_CATEGORY_REFINEMENT_TERMS = frozenset(
    {
        "blush",
        "blushes",
        "cleanser",
        "cleansers",
        "cosmetic",
        "cosmetics",
        "face",
        "find",
        "foundation",
        "foundations",
        "gloss",
        "glosses",
        "lip",
        "lipstick",
        "lipsticks",
        "looking",
        "makeup",
        "need",
        "moisturiser",
        "moisturisers",
        "moisturizer",
        "moisturizers",
        "only",
        "please",
        "product",
        "products",
        "recommend",
        "search",
        "searching",
        "serum",
        "serums",
        "show",
        "toner",
        "toners",
        "want",
    }
)


# Bounds for a caller-supplied brand anchor. See _fetch_canonical_search_rows for why.
_BRAND_ANCHOR_TERM_MAX_COUNT = 8
_BRAND_ANCHOR_TERM_MAX_LEN = 64
# Letters, digits, and the separators real brand tokens carry. Deliberately excludes the LIKE
# wildcards '%' and '_'.
_BRAND_ANCHOR_TERM_RE = re.compile(r"[a-z0-9&+.'\-]+", re.IGNORECASE)


def _category_brand_anchor_terms(query: str) -> List[str]:
    """Return a conservative possible-brand anchor for category queries.

    Category recall intentionally broadens the candidate WHERE.  Without a
    separate anchor, that broad lane can fill the candidate limit with generic
    category rows before a non-contiguous brand phrase (``knight unicorn
    blush``) is ever ranked.  Two residual terms are required so common
    one-word descriptors do not become accidental brand gates.
    """
    if not category_path_prefix_for_query(query):
        return []
    terms = _filter_relevance_terms(_tokenize_relevance(query))
    residual = [term for term in terms if term not in _CATEGORY_REFINEMENT_TERMS]
    return residual[:4] if len(residual) >= 2 else []


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
            SELECT bpp.taxonomy_json, bpp.concerns_json, bpp.claims_json,
                   bpp.routine_phase, bpp.benefits_json, bpp.profile_payload,
                   bpp.evidence_profile, bpp.required_disclaimers,
                   cp.category_kind,
                   cp.title AS cp_title, cp.product_type AS cp_product_type,
                   cp.category_path AS cp_category_path,
                   cp.description AS cp_description
            -- Anchor on catalog_products, NOT beauty_product_profiles: the
            -- durable category_kind (+ title/type/path the attribute derivers
            -- read) lives on cp, and 2,195 of 2,198 categorized products have NO
            -- bpp row. Gating on bpp dropped the whole payload -> category_kind
            -- never reached the agent record (the decision-grade `find`
            -- dimension could never pass). LEFT JOIN keeps the authored bpp
            -- enrichment when it exists.
            FROM catalog_products cp
            LEFT JOIN beauty_product_profiles bpp ON bpp.product_key = cp.product_key
            WHERE cp.product_key = :product_key
            LIMIT 1
            """,
            {"product_key": product_key},
        )
    )
    ingredient_row = _row_dict(
        await database.fetch_one(
            """
            SELECT raw_inci, normalized_ingredients_json, active_ingredients_json,
                   concentration_notes_json
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
            -- `sku_key IS NULL` is the PRODUCT-level marker: both writers
            -- (catalog ingest and `beauty_field_authoring`) write one guide row
            -- per product with a NULL sku_key. Testing only the PARAMETER for
            -- NULL hid those rows from every per-SKU caller — which is all of
            -- them here, since `_fetch_beauty_vertical_payload` is called with a
            -- concrete sku_key. The equality arm still serves per-SKU rows.
            WHERE product_key = :product_key
              AND (
                sku_key IS NULL
                OR CAST(:sku_key AS text) IS NULL
                OR sku_key = CAST(:sku_key AS text)
              )
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
              AND (CAST(:sku_key AS text) IS NULL OR sku_key = CAST(:sku_key AS text))
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
            -- Same product-level marker as the usage-guide read above:
            -- `platform_metadata.tutorials` describes the product, so ingest
            -- writes ONE asset row with a NULL sku_key. Without the IS NULL arm
            -- a per-SKU read returned that tutorial on no variant at all.
            WHERE product_key = :product_key
              AND (
                sku_key IS NULL
                OR CAST(:sku_key AS text) IS NULL
                OR sku_key = CAST(:sku_key AS text)
              )
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
              AND (CAST(:sku_key AS text) IS NULL OR sku_key = CAST(:sku_key AS text))
            ORDER BY updated_at DESC
            """,
            {"product_key": product_key, "sku_key": sku_key},
        )
    ]

    # Serve gate (parity with the direct PDP route): the search surface must emit
    # ONLY substantiated claims, never raw/unverified evidence — routed through the
    # shared substantiated_product_claims filter so the two surfaces can't drift.
    evidence_raw = _json_dict(profile.get("evidence_profile"))
    _substantiated = substantiated_product_claims(evidence_raw) if evidence_raw else []
    evidence_profile = (
        EvidenceProfile(
            claims=_substantiated,
            review_state=str(evidence_raw.get("review_state") or "observed"),
        )
        if _substantiated
        else None
    )
    category_kind = (profile.get("category_kind") or "").strip() or None
    required_disclaimers = [
        RequiredDisclaimer(**item)
        for item in _json_list(profile.get("required_disclaimers"))
        if isinstance(item, dict) and item.get("code") and item.get("text")
    ]
    # Fall back to the category's mandatory disclaimers (e.g. the FDA/DSHEA
    # supplement disclaimer) when none were explicitly authored, so a required
    # disclaimer is never silently missing from the agent surface.
    if not required_disclaimers:
        required_disclaimers = required_disclaimers_for_category(category_kind)

    # Structured skincare attributes: prefer authored profile_payload values,
    # else derive format/spf/fragrance_free from product text.
    profile_payload = _json_dict(profile.get("profile_payload"))
    cp_title = profile.get("cp_title")
    cp_product_type = profile.get("cp_product_type")
    cp_category_path = profile.get("cp_category_path")
    cp_description = profile.get("cp_description")
    skincare_format = (str(profile_payload.get("format") or "").strip() or None) or extract_format(
        cp_title, cp_product_type, cp_category_path
    )
    texture = str(profile_payload.get("texture") or "").strip() or None
    spf_value = profile_payload.get("spf_value")
    if not isinstance(spf_value, int):
        spf_value = extract_spf_value(cp_title, cp_product_type)
    fragrance_free = bool(profile_payload.get("fragrance_free")) or detect_fragrance_free(
        cp_title, cp_product_type
    )
    sensitive_safe = bool(profile_payload.get("sensitive_safe"))
    active_ingredients = merge_concentration_into_actives(
        [item for item in _json_list(ingredient_row.get("active_ingredients_json")) if isinstance(item, dict)],
        _json_list(ingredient_row.get("concentration_notes_json")),
    )
    # Read-time fallback (same deterministic, no-LLM derivation as the format /
    # concern fields above): when no structured ingredient row exists, identify
    # curated key actives from the INCI (authoritative -> source="inci") or, with
    # no INCI, the product text (source="text"). This is the `find` dimension's
    # "key actives" signal for the ~2,195 categorized-but-unprofiled products.
    if not active_ingredients and category_kind:
        active_ingredients = extract_key_actives(
            ingredient_row.get("raw_inci"),
            concentration_notes=ingredient_row.get("concentration_notes_json"),
            fallback_text=" ".join(
                str(t or "") for t in (cp_title, cp_product_type, cp_description)
            ),
        )

    # Structured haircare attributes (haircare records only): format +
    # sulfate/silicone-free flags from text, and VERIFIED-vs-claimed vegan /
    # cruelty-free cert status -- the niche-new, load-bearing signal (a bare
    # lifestyle tag only "claims"; a recognized authority "verifies").
    haircare_format = None
    sulfate_free = False
    silicone_free = False
    vegan_status = None
    cruelty_free_status = None
    if category_kind == CATEGORY_HAIRCARE:
        haircare_format = (
            str(profile_payload.get("format") or "").strip() or None
        ) or haircare_attributes.extract_format(cp_title, cp_product_type, cp_category_path)
        sulfate_free = bool(profile_payload.get("sulfate_free")) or haircare_attributes.detect_sulfate_free(
            cp_title, cp_product_type
        )
        silicone_free = bool(profile_payload.get("silicone_free")) or haircare_attributes.detect_silicone_free(
            cp_title, cp_product_type
        )
        certifications = profile_payload.get("certifications")
        vegan_status = haircare_attributes.classify_vegan(certifications, cp_title, cp_product_type)
        if vegan_status is None and bool(profile_payload.get("vegan")):
            vegan_status = haircare_attributes.CERT_CLAIMED
        cruelty_free_status = haircare_attributes.classify_cruelty_free(
            certifications, cp_title, cp_product_type
        )
        if cruelty_free_status is None and bool(profile_payload.get("cruelty_free")):
            cruelty_free_status = haircare_attributes.CERT_CLAIMED

    # Concerns: prefer authored bpp concerns_json, else deterministically infer
    # from the product text (same read-time derivation pattern as
    # skincare_format above). This is the `find` dimension's fit signal; without
    # it, the 2,195 categorized-but-unprofiled products carry a category_kind but
    # no fit attributes, so `find` still fails. Vocab match against real title /
    # type text -- not fabrication.
    concerns = [
        str(item or "").strip()
        for item in _json_list(profile.get("concerns_json"))
        if str(item or "").strip()
    ]
    if not concerns and category_kind:
        concerns = infer_concerns(category_kind, cp_title, cp_product_type, cp_category_path)

    payload = BeautyVerticalPayload(
        category_kind=category_kind,
        taxonomy=_json_dict(profile.get("taxonomy_json")),
        concerns=concerns,
        claims=[str(item or "").strip() for item in _json_list(profile.get("claims_json")) if str(item or "").strip()],
        evidence_profile=evidence_profile,
        required_disclaimers=required_disclaimers,
        skincare_format=skincare_format,
        texture=texture,
        spf_value=spf_value,
        fragrance_free=fragrance_free,
        sensitive_safe=sensitive_safe,
        haircare_format=haircare_format,
        sulfate_free=sulfate_free,
        silicone_free=silicone_free,
        vegan_status=vegan_status,
        cruelty_free_status=cruelty_free_status,
        routine_phase=profile.get("routine_phase"),
        benefits=[str(item or "").strip() for item in _json_list(profile.get("benefits_json")) if str(item or "").strip()],
        ingredients=[str(item or "").strip() for item in _json_list(ingredient_row.get("normalized_ingredients_json")) if str(item or "").strip()],
        active_ingredients=active_ingredients,
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


def _registrable_host(value: Optional[str]) -> Optional[str]:
    """Lowercased host without scheme / path / leading 'www.' (last two labels)."""
    if not value:
        return None
    host = str(value).strip().lower()
    host = re.sub(r"^[a-z]+://", "", host).split("/")[0].split("?")[0]
    host = re.sub(r"^www\.", "", host)
    if not host:
        return None
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _is_official_brand_source(
    source_domain: Optional[str], canonical_url: Optional[str]
) -> bool:
    """True when two INDEPENDENTLY-SOURCED hosts agree: an offer's serving domain
    and the canonical PDP host.

    ⚠️ CONSULTED ONLY WHEN `OFFICIAL_SOURCE_SELLER_DERIVED` IS OFF — which is
    the default, and therefore prod today. See ADR-019. So if you are chasing a
    false `official_source`, THIS IS THE CODE PRODUCING IT; it becomes dead only
    once that flag is flipped. (An earlier revision of this docstring said "no
    longer consulted", which described the intended end state as though it were
    the current one — the precise failure mode this codebase keeps paying for.)

    The function is not wrong; its INPUTS were. For an external-seed mirror row,
    `catalog_products.canonical_url` is written from the SAME seed record as
    `catalog_offers.source_domain`, so this asks whether a value equals itself.
    Measured on prod 2026-07-27 it was True for 2,646 of 2,646 candidate rows —
    100%, which is what a tautology looks like — including 480 offers the
    seller-identity derivation had explicitly typed `retailer`.

    Kept because the comparison IS meaningful for any future caller holding two
    genuinely independent values. Do not reintroduce it on the offer path: the
    seller question is answered once, at write time, by
    services/offer_seller_identity.derive_offer_seller_identity.
    """
    src = _registrable_host(source_domain)
    canon = _registrable_host(canonical_url)
    return bool(src and canon and src == canon)


def _official_source_seller_derived_enabled() -> bool:
    """ADR-019. When ON, `official_source` IS the stored seller identity and
    nothing else. Default OFF ⇒ byte-identical to today.

    The OFF state is a KNOWN-FALSE signal, not a safe default: it is off only so
    that flipping it is a separate, observable step from shipping the code."""
    return os.getenv("OFFICIAL_SOURCE_SELLER_DERIVED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _brand_direct_reader_enabled() -> bool:
    """P1 flag: when ON, an internal_merchant offer whose SELLER merchant is a
    verified brand (metadata_json.brand_relationship='brand_direct', set by the
    claim flow) is classified offer_type='brand_direct'. Default OFF — ships dark,
    enabled per-env after canary. Does NOT affect ranking (offer_type is
    decision/display metadata, never a rank signal)."""
    return os.getenv("ENABLE_BRAND_DIRECT_OFFER_TYPE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _resolve_offer_type(
    stored_offer_type: Optional[str],
    catalog_track: Optional[str],
    brand_relationship: Optional[str],
    *,
    brand_direct_enabled: bool,
) -> Optional[str]:
    """Resolve an offer's type. Stored value wins. Else fall back by track:
    external_referral -> None ("unknown") — a NULL stored offer_type on an
    external_referral row is AUTHORITATIVE: the write/backfill path already ran
    the domain-based seller-identity derivation (services/offer_seller_identity.py)
    and left it NULL for want of evidence, so the read path must NOT re-guess
    'retailer' from the lane (Fix Plan C: do not guess). internal_merchant ->
    brand_direct ONLY when the seller is a verified brand (classify_offer_type
    enforces the brand_relationship check) AND the reader flag is on — else None
    (never assumed). This is the P1 'wire the reader' step that makes a verified
    brand_direct claim load-bearing."""
    if stored_offer_type is not None:
        return stored_offer_type
    if catalog_track == "external_referral":
        return classify_offer_type(catalog_track)  # None post-Fix-Plan-C
    if brand_direct_enabled and catalog_track == "internal_merchant":
        return classify_offer_type(catalog_track, brand_relationship)
    return None


def _build_canonical_offer_node(
    row: Dict[str, Any],
    incentives: List[IncentiveNode],
    payment_offer_evidence: Optional[Dict[str, Any]] = None,
) -> OfferNode:
    base_price = _to_decimal(row.get("merchant_effective_price"))
    estimated_best_price = _to_decimal(row.get("estimated_best_price"))
    currency = row.get("currency")
    savings_presentation = build_savings_presentation(
        pricing={
            "subtotal": base_price,
            "discount_total": "0",
            "shipping_fee": "0",
            "tax": "0",
            "total": base_price,
        },
        currency=currency,
        payment_offer_evidence=payment_offer_evidence or empty_payment_offer_evidence(),
        max_summary_badges=4,
    )
    catalog_track = str(
        row.get("offer_catalog_track") or row.get("catalog_track") or "internal_merchant"
    )
    # Stored values win; fall back to the deterministic track-based derivation so
    # rows written before mig 149's backfill still classify correctly.
    is_first_party = row.get("offer_is_first_party")
    if is_first_party is None:
        is_first_party = is_first_party_track(catalog_track)
    offer_type = _resolve_offer_type(
        row.get("offer_offer_type"),
        catalog_track,
        row.get("brand_relationship"),
        brand_direct_enabled=_brand_direct_reader_enabled(),
    )
    # official_source — the authenticity signal an agent sees. ADR-019.
    #
    # ON (seller-derived): the stored seller identity IS the answer. It was
    # computed once at write time by offer_seller_identity.derive_offer_seller_
    # identity, which compares the offer domain against the DECLARED BRAND behind
    # a known-retailer list that preempts everything — and which returns "unknown"
    # rather than guessing. This also makes this lane agree with the external-seed
    # lane below (~:1862), which already derives it exactly this way.
    #
    # OFF (legacy): additionally trusts source_domain == canonical PDP host. For
    # an external-seed mirror row BOTH of those are written from the same seed
    # record, so the comparison is a tautology. Measured on prod 2026-07-27 it
    # fired on 2,646 of 2,646 candidate rows (100%), including 480 typed
    # `retailer` and 2,166 the derivation had deliberately left unknown — telling
    # agents a ulta.com offer is served from the brand's own official domain.
    #
    # The cohort the legacy disjunct exists for — "official brand, correctly not
    # is_first_party" — is EMPTY in prod and structurally so: the derivation sets
    # brand_direct and is_first_party together. So it has no legitimate consumer;
    # its whole live effect is the false positives.
    if _official_source_seller_derived_enabled():
        official_source = bool(is_first_party)
    else:
        # The legacy disjunct is only evidence when its two hosts are
        # INDEPENDENTLY sourced. On the external_referral lane they never are:
        # both source_domain and canonical_url are written from the same seed
        # record, so the comparison is the measured 100% tautology above — and
        # source_domain is now stamped on every mirror offer (it used to be
        # NULL on ~4,000 of them, which was the only thing keeping the false
        # positive count at 2,646 instead of the whole lane). So the lane is
        # excluded here, flag state notwithstanding: an observed redirect offer
        # can only be "official" via its stored seller identity
        # (is_first_party), never via a self-comparison.
        official_source = bool(is_first_party) or (
            catalog_track != "external_referral"
            and _is_official_brand_source(
                row.get("offer_source_domain") or row.get("source_domain"),
                row.get("canonical_url"),
            )
        )
    return OfferNode(
        offer_id=str(row.get("offer_id") or ""),
        merchant_id=row.get("offer_merchant_id"),
        merchant_name=row.get("offer_merchant_name"),
        catalog_track=catalog_track,
        truth_tier=str(row.get("offer_truth_tier") or row.get("truth_tier") or "primary"),
        readiness_tier=str(row.get("offer_readiness_tier") or row.get("readiness_tier") or "commerce_ready"),
        offer_mode=str(row.get("offer_mode") or "merchant_checkout"),
        source_system=row.get("offer_source_system") or row.get("source_system"),
        availability=row.get("availability"),
        inventory_quantity=row.get("inventory_quantity"),
        offer_type=offer_type,
        market=str(row.get("offer_market") or "US"),
        is_first_party=bool(is_first_party),
        official_source=official_source,
        why_buy_direct=row.get("offer_why_buy_direct"),
        pricing=PivotPricing(
            currency=row.get("currency"),
            list_price=_to_decimal(row.get("list_price")),
            merchant_effective_price=base_price,
            estimated_best_price=estimated_best_price,
            exact_quote_price=None,
            price_confidence=_to_decimal(row.get("price_confidence")),
        ),
        incentives=incentives,
        payment_offer_evidence=payment_offer_evidence or empty_payment_offer_evidence(),
        savings_presentation=savings_presentation,
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
    raw_rank_score = row.get("rank_score")
    # RECALL_RELEVANCE_V2: rank by TEXT relevance so the +200 canonical structural
    # boost can't saturate candidate_score and bury precise matches. Falls back to
    # rank_score when the lane emits no split (e.g. the citable lane, which has no
    # structural boost, so its rank_score is already ~text).
    score_for_candidate = raw_rank_score
    if _recall_relevance_v2_enabled() and row.get("text_score") is not None:
        score_for_candidate = row.get("text_score")
    try:
        normalized_candidate_score = round(
            max(0.12, min(float(score_for_candidate or 0.0) / 100.0, 1.4)),
            4,
        )
    except Exception:
        normalized_candidate_score = 0.12
    try:
        structure_score = round(float(row.get("structure_score") or 0.0), 4)
    except Exception:
        structure_score = 0.0
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
        "candidate_source": "internal",
        "candidate_score": normalized_candidate_score,
        # Structural/scope quality, kept separate from relevance. _sort_items uses
        # it only as a SECONDARY tie-break when v2 is on (0 / ignored otherwise).
        "structure_score": structure_score,
        "source_boost": 0.0,
        "quality_penalties_total": 0.0,
        "price_tie_break": row.get("estimated_best_price") or row.get("merchant_effective_price") or row.get("list_price"),
    }


async def _fetch_canonical_search_rows(
    *,
    query: str,
    merchant_id: Optional[str],
    limit: int,
    require_signature: bool = False,
    brand_anchor_terms: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    lowered = _normalize_query(query)
    if not lowered:
        return []
    normalized_limit = max(1, int(limit or 20))
    candidate_limit = min(max(normalized_limit * 4, 25), 200)
    row_limit = min(max(normalized_limit * 6, 50), 500)
    vertical_search = _vertical_intent(query)
    # Phase 2b: when the query matches a known category alias, bias the
    # candidate WHERE / score toward catalog_products.category_path matches.
    # This is the PDP-first recall path enabled by mig 069 + the regex
    # ported from PIVOTA-Agent BEAUTY_CATEGORY_PATTERNS. When the query
    # doesn't match a known category, this is a no-op and the existing
    # text-LIKE path runs unchanged.
    category_prefix = category_path_prefix_for_query(query)
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
    # Phase O-5: hard-filter the broad, content-led global recall pool to
    # live lifecycle stages so draft/candidate rows don't surface merely
    # because their title matched. This is *not* a commerce-sellability
    # property, though: a canonical SIG row with a live, unsuppressed offer
    # is already governed by the product/SKU/offer and seller gates below.
    #
    # Merchant synchronisation writes those rows as ``candidate`` until a
    # separate content-enrichment pass adds taxonomy. Applying this content
    # workflow gate to ``canonical_entities_only`` therefore made immediately
    # buyable SIG products disappear after every sync (for example, Knight
    # Unicorn), despite a priced in-stock offer. Canonical product-card recall
    # intentionally bypasses this clause; it still requires a sig identity,
    # sync_status=live, non-suppressed product/SKU/offer and an active,
    # indexable offer seller. Merchant-scoped queries also skip it so a
    # merchant can see its own inventory while LabelAgent ramps.
    lifecycle_clause = ""
    if not merchant_id and not require_signature:
        lifecycle_clause = (
            "AND (p.pdp_lifecycle_stage IN ('validated', 'published') "
            "OR p.pdp_lifecycle_stage IS NULL)"
        )

    # Stage 2a (mig 084): hide stale/archived rows from cross-merchant
    # recall. A row that's been tombstoned by
    # scripts/sweep_stale_catalog_products.py shouldn't show up in the
    # agent's "find me a foundation brush" results. Merchant-scoped
    # queries skip this filter so the merchant can still see their
    # own historical rows (and the operator dashboard can surface
    # them for cleanup). See plans/rosy-mixing-bengio.md Stage 2a.
    sync_status_clause = ""
    if not merchant_id:
        sync_status_clause = "AND p.sync_status = 'live'"

    # Exclude products from DEACTIVATED merchants (catalog_merchants.status =
    # 'inactive') from cross-merchant recall. A retired/deactivated merchant —
    # e.g. a decommissioned test rig whose stores are all retired — must not leak
    # its catalog into "find me a X" results. This semantic-core recall previously
    # text-matched catalog_products with NO merchant-status gate, so a demo
    # merchant's dog leashes surfaced for "leather crossbody bag". COALESCE keeps
    # rows with no catalog_merchants row (external seeds) and non-inactive statuses
    # (active / observed sellers) serving. Merchant-scoped queries skip this so a
    # merchant can still see their own rows in the operator dashboard.
    merchant_status_clause = ""
    if not merchant_id:
        merchant_status_clause = "AND lower(COALESCE(m.status, 'active')) <> 'inactive'"

    # #1648: a suppressed/tombstoned product row must not surface in cross-
    # merchant recall. Recall previously gated suppression on OFFERS only
    # (o.suppressed_at), so a withdrawn catalog_products row kept serving
    # through this lane. BOTH columns are gated: catalog_trust_policy treats
    # suppression_reason alone as tombstoned, and the step5-generation writers
    # set reason without suppressed_at (2,332 such rows backfilled 2026-07-30)
    # — the both-column gate keeps a future reason-only writer from re-opening
    # the leak. Merchant-scoped queries skip this so a merchant still sees
    # their own withdrawn rows in the operator dashboard.
    suppression_clause = ""
    if not merchant_id:
        suppression_clause = (
            "AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL"
        )

    signature_clause = ""
    if require_signature:
        # SUBSTR is supported by both production PostgreSQL and the SQLite
        # integration harness. Keep the exact four-character test rather than
        # LIKE 'sig_%': underscore is a wildcard in LIKE and would admit
        # malformed non-SIG ids.
        signature_clause = "AND SUBSTR(p.pivota_signature_id, 1, 4) = 'sig_'"

    # #1648: honor catalog_merchants.indexable in cross-merchant recall. It was
    # the ONE fence set correctly on the retired test rig (migration 139 set
    # indexable=FALSE) and no search lane read it. COALESCE keeps rows with no
    # catalog_merchants row (external seeds) serving, mirroring
    # merchant_status_clause. This is NOT a widening of the gate to IPS
    # serving_eligible — that would be a recall-shrink product decision; see
    # #1648's review notes.
    indexable_clause = ""
    if not merchant_id:
        indexable_clause = "AND COALESCE(m.indexable, TRUE) IS TRUE"

    # H1 (#1648) — gate the OFFER SELLER, not just the product row's merchant.
    # Every gate above joins `m` on p.merchant_id, i.e. whoever OWNS the
    # canonical row. The merchant whose price and availability this lane
    # actually publishes is `o.merchant_id` (alias `bm`), and the two are
    # different for 3,423 of 14,867 unsuppressed offers on prod (2026-07-31).
    #
    # Failure scenario: retired merchant R's offer hangs off a sku under a
    # canonical row owned by `external_seed` (active, indexable) -> every gate
    # above passes on the OWNER, and R's price surfaces in cross-merchant
    # recall. Only `o.suppressed_at IS NULL` stops it, and the rig is safe today
    # only because the 2026-07-30 closure suppressed its offers.
    #
    # NULL-KEEPING COALESCE IS LOAD-BEARING (see the m-clauses above): 741
    # unsuppressed offers have a seller with NO catalog_merchants row at all.
    # A bare `bm.status = 'active'` / `bm.indexable IS TRUE` would delete every
    # one of them from recall. Prior art pinned in
    # tests/test_pivota_canonical_routes.py:674.
    #
    # This must be a WHERE, not extra ON conditions: `bm` is LEFT JOINed, so
    # conditions in the ON would merely null the alias out and let the row
    # through — the gate would read as present and filter nothing.
    offer_seller_where = ""
    if not merchant_id:
        offer_seller_where = (
            "WHERE lower(COALESCE(bm.status, 'active')) <> 'inactive' "
            "AND COALESCE(bm.indexable, TRUE) IS TRUE"
        )

    # H3 (#1648) — the recall CTE joins catalog_skus and ignores its suppression
    # columns. No live gap today (38 suppressed skus on prod, 0 of them under an
    # unsuppressed product, because the sole sku-suppression writer —
    # scripts/merge_duplicate_canonicals.py — also suppresses the loser product).
    # Belt-and-braces so a sku-only retirement writer cannot re-open the leak;
    # BOTH columns for the same reason the product clause gates both.
    #
    # KNOWN DIVERGENCE, deliberately accepted: this makes the recall lane the
    # ONLY reader of catalog_skus suppression in the codebase.
    # services/agent_pdp_view_assembler.py (fetch_skus_for_keys, which populates
    # the PDP variant list) and routes/audit_runs_routes.py read catalog_skus
    # with no suppression filter. So a suppressed sku is hidden from recall while
    # still rendering as a PDP variant — a lane divergence of the same CLASS as
    # #1648 itself, though in the safe direction (recall shows less, not more)
    # and affecting 0 rows today (38 suppressed skus, none under an unsuppressed
    # product). Converging those readers is tracked on #1648; it is not done here
    # because touching the PDP assembler carries the same 404-flip blast radius
    # as H2 and needs its own door re-verify.
    sku_suppression_clause = ""
    if not merchant_id:
        sku_suppression_clause = (
            "AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL"
        )
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
    category_where = ""
    category_score = ""
    if category_prefix:
        params["category_path_prefix"] = f"{category_prefix}%"
        category_where = """
            OR (p.category_path IS NOT NULL AND p.category_path LIKE :category_path_prefix)
        """
        # Score boost positions a category-path hit just above an exact
        # brand match (80) but below an exact source_product_id match (105),
        # mirroring how merchants intend "show me lipsticks" to surface PDPs
        # whose taxonomy path is the lip family.
        category_score = """
            + CASE WHEN p.category_path IS NOT NULL AND p.category_path LIKE :category_path_prefix THEN 90 ELSE 0 END
        """

    # A multi-token residual next to a known category is a possible brand
    # anchor.  This is deliberately independent of the broad token-recall flag:
    # it does not widen arbitrary queries, and category recall already admits
    # these rows.  It only ensures a real brand match is not truncated behind a
    # large set of same-category rows before the gateway can validate it.
    # The caller's anchor wins when it supplied one. `_category_brand_anchor_terms`
    # needs >= 2 residual tokens, so it can never boost a SINGLE-WORD brand — Murad,
    # CeraVe, NARS. The gateway resolves those against the catalog brand dictionary
    # and now threads the answer down, because a post-filter can only keep what recall
    # already returned: with the boost missing, "show me Murad products" truncated
    # every Murad row below the candidate limit and the gateway anchored on an empty
    # set (`brand_category_anchor_matched: false`) while a LIZUSH bath bomb survived.
    # None (no opinion from the caller) keeps the original behaviour for every other
    # caller of this function.
    brand_anchor_score = ""
    brand_anchor_terms = (
        brand_anchor_terms
        if brand_anchor_terms is not None
        else _category_brand_anchor_terms(query)
    )
    # The field is client-supplied on POST /v1/pivot/query, and every term becomes three more LIKE
    # predicates over the catalog join. Unbounded, 2000 terms produced a 719KB statement with 12k
    # predicates — a statement-timeout shaped exactly like the pool incidents this service has had.
    # A term carrying LIKE wildcards is worse than useless: '%' alone becomes LIKE '%%%', which is
    # true for every row and hands the entire candidate set +180, flattening the ranking. Terms are
    # always BOUND (only the integer index is interpolated), so this is not an injection surface —
    # it is a denial-of-service and ranking-distortion surface, and the sibling fields on this model
    # are all bounded already.
    brand_anchor_terms = [
        t
        for t in (brand_anchor_terms or [])
        if isinstance(t, str) and 0 < len(t) <= _BRAND_ANCHOR_TERM_MAX_LEN and _BRAND_ANCHOR_TERM_RE.fullmatch(t)
    ][:_BRAND_ANCHOR_TERM_MAX_COUNT]
    if brand_anchor_terms:
        # A SINGLE-token anchor matches identity fields ONLY, never the title.
        #
        # Every guard that lets a token become an anchor is an exact-span equality test — dictionary
        # membership, the stopword list, `category_path_prefix_for_query(span)` — but the value they
        # approve is consumed here as an UNANCHORED substring. For a 4-character brand those are not
        # the same question. `lush` is a real catalog brand and `category_path_prefix_for_query`
        # correctly refuses `blush`, and then `%lush%` matches "Soft Pinch Liquid B-LUSH", "Orgasm
        # Powder B-LUSH", "Baked B-LUSH Luminoso" — six rows boosted, one of them actually LUSH.
        # At +180 that outranks every legitimate text signal (exact title 100 + title LIKE 90), and
        # under RECALL_RELEVANCE_V2 text_score IS the serving order, so "tula cleanser" would put a
        # silicone spa-TULA at the top of a cleanser search: the same failure class this boost was
        # extended to fix.
        #
        # Identity-only also makes recall agree with the gateway post-filter, which matches
        # brand + merchant_name and nothing else. A title-only hit was being boosted into a 40-80 row
        # candidate window and then discarded — spending the very slots real brand rows needed.
        #
        # MULTI-token anchors keep the title clause. They are ANDed, so "%knight% AND %unicorn%" is
        # enormously more selective, and the title is where a two-word brand survives a missing
        # `brand` column. That path is unchanged.
        anchor_fields = ["p.brand", "m.merchant_name"]
        if len(brand_anchor_terms) > 1:
            anchor_fields.append("p.title")
        anchor_matches = []
        for anchor_index, anchor_term in enumerate(brand_anchor_terms):
            param_name = f"brand_anchor_{anchor_index}"
            params[param_name] = f"%{anchor_term}%"
            anchor_matches.append(
                "("
                + " OR ".join(
                    f"LOWER(COALESCE({field}, '')) LIKE :{param_name}" for field in anchor_fields
                )
                + ")"
            )
        anchor_expression = " AND ".join(anchor_matches)
        brand_anchor_score = (
            "\n                    + CASE WHEN ("
            + anchor_expression
            + ") THEN 180 ELSE 0 END\n"
        )

    # Token-overlap recall (Part A). ADDITIVE: the whole-phrase `LIKE :query_like`
    # clause above only matches the verbatim phrase, so multi-word queries whose
    # words appear non-contiguously ("hydrating cleanser", "snail mucin essence")
    # returned zero. When >=2 significant tokens are present, ALSO match a row
    # whose title/brand/sku-title/product_type contains >=ceil(n/2) (min 2) of
    # them, ranked by overlap (x25). The >=2 floor keeps single-common-token junk
    # out; the existing precision gate downstream still applies. Flag-gated
    # (PIVOT_CANONICAL_TOKEN_MATCH, default OFF) ⇒ token_where/token_score stay
    # empty ⇒ byte-identical SQL. Skipped for merchant-scoped queries (they
    # already see all their own rows).
    token_where = ""
    token_score = ""
    if not merchant_id and _canonical_token_match_enabled():
        _tokens = _citable_query_tokens(lowered)
        if len(_tokens) >= 2:
            _overlap_terms = []
            for _i, _tok in enumerate(_tokens):
                _pname = f"cctok{_i}"
                params[_pname] = f"%{_tok}%"
                _overlap_terms.append(
                    "(CASE WHEN LOWER(COALESCE(p.title, '')) LIKE :" + _pname
                    + " OR LOWER(COALESCE(p.brand, '')) LIKE :" + _pname
                    + " OR LOWER(COALESCE(s.title, '')) LIKE :" + _pname
                    + " OR LOWER(COALESCE(p.product_type, '')) LIKE :" + _pname
                    + " THEN 1 ELSE 0 END)"
                )
            _overlap_expr = " + ".join(_overlap_terms)
            params["cctok_min"] = max(2, (len(_tokens) + 1) // 2)
            token_where = f"\n                OR (({_overlap_expr}) >= :cctok_min)\n"
            token_score = f"\n                    + (({_overlap_expr}) * 25)\n"

    rows = await database.fetch_all(
        f"""
        WITH candidate_skus AS (
            SELECT
                m.merchant_id AS merchant_id,
                m.merchant_name AS merchant_name,
                m.primary_platform AS merchant_primary_platform,
                p.product_key,
                p.content_key,
                p.pivota_signature_id,
                p.pivota_canonical_url,
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
                p.pdp_scope,
                p.pdp_lifecycle_stage,
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
                    CASE WHEN LOWER(COALESCE(p.brand, '')) = :query_exact THEN 80 ELSE 0 END +
                    -- Phase 6 (mig 070): rank multi_merchant_canonical PDPs
                    -- strictly above merchant_owned for any matched query.
                    -- The bonus is large enough to dominate every other
                    -- term so a canonical match always wins over a single-
                    -- merchant private listing that happens to overlap
                    -- titles (the MOYU 1216-row pollution case).
                    -- Merchant-scoped queries are unaffected because the
                    -- candidate set is already filtered to one merchant
                    -- whose rows share the same scope.
                    CASE WHEN p.pdp_scope = 'multi_merchant_canonical' THEN 200 ELSE 0 END +
                    -- Phase O-5: tie-break within the live pool.
                    -- "published" is a strict superset of canonical
                    -- scope (Path C agent rows + future manual-approval
                    -- rows also reach published), so the +60 here
                    -- doesn't only repeat the pdp_scope=200 boost —
                    -- it also lifts agent-curated rows that lack the
                    -- multi_merchant_canonical scope flag. Magnitudes
                    -- stay below brand-exact (80) and category-prefix
                    -- (90) so the lifecycle stage acts as a tie-breaker,
                    -- not a dominating signal.
                    CASE WHEN p.pdp_lifecycle_stage = 'published' THEN 60 ELSE 0 END +
                    CASE WHEN p.pdp_lifecycle_stage = 'validated' THEN 20 ELSE 0 END
                    {category_score}
                    {brand_anchor_score}
                    {vertical_score}
                    {token_score}
                ) AS rank_score,
                -- RECALL_RELEVANCE_V2: TEXT relevance only (exact + partial LIKE
                -- + vertical term hits), with NO structural/scope boost. Used to
                -- order results when v2 is on so the +200 canonical boost can't
                -- saturate relevance. rank_score above is UNCHANGED (flag-off
                -- behaviour + candidate selection are byte-identical). The
                -- partial-LIKE bonuses (mirroring the citable lane, #1027) give a
                -- non-exact match a real text score instead of ~0.
                (
                    CASE WHEN LOWER(COALESCE(s.sku, '')) = :query_exact THEN 120 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(s.source_variant_id, '')) = :query_exact THEN 110 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(p.source_product_id, '')) = :query_exact THEN 105 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(p.title, '')) = :query_exact THEN 100 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(m.merchant_name, '')) = :query_exact THEN 90 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(p.brand, '')) = :query_exact THEN 80 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(p.title, '')) LIKE :query_like THEN 90 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(p.brand, '')) LIKE :query_like THEN 70 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(s.title, '')) LIKE :query_like THEN 60 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(m.merchant_name, '')) LIKE :query_like THEN 50 ELSE 0 END +
                    CASE WHEN LOWER(COALESCE(p.source_product_id, '')) LIKE :query_like THEN 40 ELSE 0 END
                    {brand_anchor_score}
                    {vertical_score}
                    {token_score}
                ) AS text_score,
                -- RECALL_RELEVANCE_V2: STRUCTURAL/scope quality, kept separate so
                -- it can act as a SECONDARY tie-break (not a relevance signal).
                (
                    CASE WHEN p.pdp_scope = 'multi_merchant_canonical' THEN 200 ELSE 0 END +
                    CASE WHEN p.pdp_lifecycle_stage = 'published' THEN 60 ELSE 0 END +
                    CASE WHEN p.pdp_lifecycle_stage = 'validated' THEN 20 ELSE 0 END
                    {category_score}
                ) AS structure_score
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
                {category_where}
                {vertical_where}
                {token_where}
            )
            {merchant_clause}
            {lifecycle_clause}
            {sync_status_clause}
            {merchant_status_clause}
            {suppression_clause}
            {signature_clause}
            {indexable_clause}
            {sku_suppression_clause}
            ORDER BY rank_score DESC, p.updated_at DESC, s.updated_at DESC
            LIMIT :candidate_limit
        )
        SELECT
            c.merchant_id,
            c.merchant_name,
            c.merchant_primary_platform,
            c.product_key,
            c.content_key,
            c.pivota_signature_id,
            c.pivota_canonical_url,
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
            c.pdp_scope,
            c.pdp_lifecycle_stage,
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
            o.merchant_id AS offer_merchant_id,
            bm.merchant_name AS offer_merchant_name,
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
            o.offer_type AS offer_offer_type,
            o.market AS offer_market,
            o.is_first_party AS offer_is_first_party,
            o.source_domain AS offer_source_domain,
            o.why_buy_direct AS offer_why_buy_direct,
            o.offer_payload,
            -- P1: the SELLER merchant's verified brand relationship, so the
            -- offer-type reader can classify brand_direct (flag-gated; NOT a
            -- rank signal). NULL for unclaimed merchants -> offer_type stays None.
            bm.metadata_json->>'brand_relationship' AS brand_relationship,
            -- P0.3 neutrality: NO first-party / ownership boost. A first-party
            -- offer must not outrank an equally-relevant third-party offer for
            -- the same product (was: + CASE WHEN catalog_track='internal_merchant'
            -- THEN 10). Ownership is not a ranking signal — merit is. This is
            -- load-bearing for the neutral-index thesis: a margin/ownership-tilted
            -- decision layer is detectable and erodes frontier-model trust.
            c.rank_score AS rank_score,
            c.text_score AS text_score,
            c.structure_score AS structure_score
        FROM candidate_skus c
        JOIN catalog_offers o
          ON o.sku_key = c.sku_key
         AND o.suppressed_at IS NULL
        LEFT JOIN catalog_merchants bm
          ON bm.merchant_id = o.merchant_id
        {offer_seller_where}
        ORDER BY rank_score DESC, c.product_updated_at DESC, o.updated_at DESC
        LIMIT :row_limit
        """,
        params,
    )
    return [_row_dict(row) for row in rows]


# ---------------------------------------------------------------------------
# ADR-008 SLICE 3 — citable recall (offer-free, NEVER buyable)
# ---------------------------------------------------------------------------


def _index_eligible_recall_enabled() -> bool:
    """ADR-008 SLICE 3 flag. When ON, the OFFER-FREE citable lane runs and
    contributes index_eligible (citation-only) products to recall for
    inform/recommend intent. Default OFF ⇒ the lane never runs, no new SQL
    executes, and recall is byte-identical to today (offer-backed lane only)."""
    return (
        (os.getenv("INDEX_ELIGIBLE_RECALL") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )


# Generic terms dropped from the citable token-match so a single common word
# can't loosen the gate. Mirrors the gateway's TOKEN_STOPWORDS.
_CITABLE_TOKEN_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "you", "your", "our", "best",
        "buy", "shop", "store", "review", "reviews", "vs", "top", "new",
        "off", "all", "any", "are", "was", "has", "how", "what", "who",
        "where", "when", "why", "this", "that", "these", "those", "into",
    }
)


def _citable_token_match_enabled() -> bool:
    """Flag for the citable token-overlap match (default OFF ⇒ byte-identical:
    no token clauses are added to the citable SQL). When ON, sharpens citation
    recall/search so an intent phrase whose words appear non-contiguously in a
    title still matches — ported from the gateway's tokenMatch."""
    return (
        (os.getenv("CITABLE_TOKEN_MATCH") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )


def _canonical_token_match_enabled() -> bool:
    """Flag for the token-overlap match on the MAIN cross-merchant recall lane
    (_fetch_canonical_search_rows). Default OFF ⇒ byte-identical SQL. When ON, a
    multi-word query whose words appear NON-CONTIGUOUSLY in a title still
    matches (whole-phrase `LIKE '%a b c%'` only matches the verbatim phrase, so
    'hydrating cleanser'/'snail mucin essence' returned zero). Same shape as the
    citable token match: >=ceil(n/2) (min 2) significant tokens must hit, ranked
    by overlap — the >=2 floor keeps single-common-token junk out."""
    return (
        (os.getenv("PIVOT_CANONICAL_TOKEN_MATCH") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )


def _citable_query_tokens(lowered: str) -> List[str]:
    """Significant tokens of an already-normalized query: len>=3, stopwords
    dropped, deduped, capped at 6 (matches the gateway tokenizer)."""
    seen: set[str] = set()
    out: List[str] = []
    for raw in str(lowered or "").split():
        tok = raw.strip()
        if len(tok) < 3 or tok in _CITABLE_TOKEN_STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= 6:
            break
    return out


def _recall_relevance_v2_enabled() -> bool:
    """Recall relevance v2. When ON, ranking orders by TEXT relevance first and
    treats structural/scope boosts (multi_merchant_canonical, lifecycle,
    category) as a SECONDARY tie-breaker — instead of letting the +200 canonical
    boost saturate candidate_score at its 1.4 cap and bury precise matches under
    same-category junk. Default OFF ⇒ candidate_score is computed from the
    unchanged rank_score and the structure tie-break contributes 0, so ordering
    is byte-identical to today. See docs/recall-relevance-saturation-fix.md."""
    return (
        (os.getenv("RECALL_RELEVANCE_V2") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )


# Compiled once at import: "will the PDP for this sig answer 200?", keyed on the
# citable lane's own sig expression. Module-level so the compile cost is paid at
# import, not per query. See services.pdp_renderability.sig_pdp_will_render.
_SIG_RENDERABLE_SQL = _sig_pdp_will_render_sql(
    "COALESCE(apv.pivota_signature_id, p.pivota_signature_id)"
)

# The content_key's elected canonical, validated against the live electable set —
# correlated to this lane's own `p.content_key`, so it answers per row. Same
# stored election (migration 181) and same validation helper the sitemap feed
# uses; see routes/agent_citation_v1 for why a fourth independent opinion on
# "which sibling holds the URL" is the thing to avoid.
_ELECTED_CANONICAL_SIG_SQL = _compile_pg(
    _select(_cce.c.canonical_sig_id)
    .where(
        _and_(
            _cce.c.content_key == _literal_column("p.content_key"),
            _electable_sig_exists(_cce.c.canonical_sig_id, widen=False),
        )
    )
    .limit(1)
)


async def _fetch_citable_canonical_rows(
    *,
    query: str,
    merchant_id: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """ADR-008 SLICE 3: the OFFER-FREE citable recall lane.

    Mirrors the text-match predicates of `_fetch_canonical_search_rows` but is a
    SEPARATE lane that joins catalog_products -> index_pipeline_state (on
    content_key) WHERE index_eligible = TRUE, and pulls display content from
    agent_pdp_view. It has NO catalog_skus join and NO catalog_offers join, so it
    can NEVER produce a buyable/offer-shaped row. The forbidden move — LEFT JOINing
    catalog_offers into the existing canonical lane — is explicitly NOT what this
    is: the canonical INNER JOIN at `_fetch_canonical_search_rows` is untouched.

    Rows returned here carry an explicit `buyable=False` marker + the content_key
    so the merge can dedupe against offer-backed results. Ranking is by MERIT only
    (the same text-match rank terms the canonical lane uses, MINUS sku/offer
    terms); no offer/ownership boost is applied.
    """
    lowered = _normalize_query(query)
    if not lowered:
        return []
    normalized_limit = max(1, int(limit or 20))
    row_limit = min(max(normalized_limit * 6, 50), 500)
    category_prefix = category_path_prefix_for_query(query)
    params: Dict[str, Any] = {
        "query_exact": lowered,
        "query_like": f"%{lowered}%",
        "row_limit": row_limit,
    }
    merchant_clause = ""
    if merchant_id:
        merchant_clause = "AND p.merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    # Mirror the canonical lane's cross-merchant live filters so the citable
    # surface is held to the same lifecycle/sync floor for global queries.
    lifecycle_clause = ""
    sync_status_clause = ""
    if not merchant_id:
        lifecycle_clause = (
            "AND (p.pdp_lifecycle_stage IN ('validated', 'published') "
            "OR p.pdp_lifecycle_stage IS NULL)"
        )
        sync_status_clause = "AND p.sync_status = 'live'"

    # #1648: this OFFER-FREE lane is exactly where the retired rig's
    # external_seed mirror rows leaked (it joins ips.index_eligible, which was
    # stale, and previously carried NONE of the row-level source gates). Mirror
    # the canonical lane's three gates verbatim: deactivated-merchant status,
    # product-row suppression (BOTH columns — see the canonical lane comment),
    # and catalog_merchants.indexable. All merchant_id-conditional so a
    # merchant still sees their own rows.
    merchant_status_clause = ""
    suppression_clause = ""
    indexable_clause = ""
    if not merchant_id:
        merchant_status_clause = "AND lower(COALESCE(m.status, 'active')) <> 'inactive'"
        suppression_clause = (
            "AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL"
        )
        indexable_clause = "AND COALESCE(m.indexable, TRUE) IS TRUE"
    category_where = ""
    category_score = ""
    if category_prefix:
        params["category_path_prefix"] = f"{category_prefix}%"
        category_where = """
            OR (p.category_path IS NOT NULL AND p.category_path LIKE :category_path_prefix)
        """
        category_score = """
            + CASE WHEN p.category_path IS NOT NULL AND p.category_path LIKE :category_path_prefix THEN 90 ELSE 0 END
        """

    # Token-overlap match (ported from the gateway). ADDITIVE: when the query has
    # >=2 significant tokens, ALSO match a row whose title/brand contains at least
    # ceil(n/2) (min 2) of them, ranked by overlap (×25). The whole-phrase clause
    # is untouched, so this can only ADD matches. Flag-gated (CITABLE_TOKEN_MATCH,
    # default OFF) ⇒ token_where/token_score stay empty ⇒ byte-identical SQL.
    token_where = ""
    token_score = ""
    if _citable_token_match_enabled():
        _tokens = _citable_query_tokens(lowered)
        if len(_tokens) >= 2:
            _overlap_terms = []
            for _i, _tok in enumerate(_tokens):
                _pname = f"ctok{_i}"
                params[_pname] = f"%{_tok}%"
                _overlap_terms.append(
                    "(CASE WHEN LOWER(COALESCE(COALESCE(apv.title, p.title), '')) LIKE :"
                    + _pname
                    + " OR LOWER(COALESCE(COALESCE(apv.brand, p.brand), '')) LIKE :"
                    + _pname
                    + " THEN 1 ELSE 0 END)"
                )
            _overlap_expr = " + ".join(_overlap_terms)
            params["ctok_min"] = max(2, (len(_tokens) + 1) // 2)
            token_where = f"\n            OR (({_overlap_expr}) >= :ctok_min)\n"
            token_score = f"\n                + (({_overlap_expr}) * 25)\n"

    rows = await database.fetch_all(
        f"""
        SELECT
            m.merchant_id AS merchant_id,
            m.merchant_name AS merchant_name,
            m.primary_platform AS merchant_primary_platform,
            p.product_key,
            p.content_key,
            p.source_product_id,
            p.canonical_url,
            p.pivota_canonical_url,
            -- The citable sig, for the CitationItem's attribution.canonical_url
            -- (routes/agent_citation_v1._search_row_to_citation). agent_pdp_view
            -- FIRST because it is content_key-keyed and is the surface the
            -- single-item citation read resolves from — so the search and
            -- single-item endpoints emit the SAME URL for the same content_key,
            -- which they would not if this picked whichever product_key won the
            -- rank. Falls back to the product's own sig for a row with no apv row
            -- yet (this lane reads catalog_products, so that is reachable).
            -- NOTE: this SELECT is an f-string; never write a brace in here.
            COALESCE(apv.pivota_signature_id, p.pivota_signature_id)
                AS pivota_signature_id,
            -- Will the PDP for that exact sig answer 200? Both of get_pdp_v2's
            -- gates. Keyed on the SAME COALESCE expression the URL is built from,
            -- not on p's own sig — otherwise the flag could describe a different
            -- row than the URL we emit, which is the precise class of lie this
            -- signal exists to remove. Free here (this lane already reads
            -- catalog_products); the single-item citation read pays a second
            -- round trip instead because it can absorb one behind its 300s cache,
            -- NOT because agent_pdp_v1 cannot carry the predicate — #1602 measured
            -- it at 0.18-0.57ms and put it inline there. See
            -- routes/agent_citation_v1._sig_renderable for why materialising the
            -- flag was rejected.
            ({_SIG_RENDERABLE_SQL}) AS pdp_renderable,
            -- The content_key's ELECTED canonical sig, intersected with the live
            -- electable set, so a citable hit whose own PDP is dead can still be
            -- given a URL that answers 200. Same stored election + same
            -- validation the sitemap feed uses, so the cited URL is the
            -- advertised URL. NULL today for every row: the table is unseeded.
            ({_ELECTED_CANONICAL_SIG_SQL}) AS elected_canonical_sig,
            p.catalog_track,
            p.truth_tier,
            p.readiness_tier,
            p.pdp_scope,
            p.pdp_lifecycle_stage,
            p.source_system,
            p.freshness_json,
            p.updated_at AS product_updated_at,
            -- Display content is served from the already-assembled agent_pdp_view
            -- (the same denormalized surface the citation PDP renders from).
            COALESCE(apv.title, p.title) AS product_title,
            COALESCE(apv.description, p.description) AS product_description,
            COALESCE(apv.brand, p.brand) AS brand,
            p.product_type,
            COALESCE(apv.category_path, p.category) AS category,
            COALESCE(apv.image_url, p.image_url) AS product_image_url,
            (
                CASE WHEN LOWER(COALESCE(p.source_product_id, '')) = :query_exact THEN 105 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(COALESCE(apv.title, p.title), '')) = :query_exact THEN 100 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(m.merchant_name, '')) = :query_exact THEN 90 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(COALESCE(apv.brand, p.brand), '')) = :query_exact THEN 80 ELSE 0 END +
                -- Partial (substring) match credit. The WHERE already requires a
                -- LIKE match on one of these columns, so without this every
                -- non-exact citable row scored ~0 -> _canonical_match_reason
                -- floored candidate_score at 0.12 -> _sort_items buried it beneath
                -- every external_referral row, even for a branded query the title
                -- plainly contains. Ranked below the exact bonuses so exact still
                -- wins (an exact match also matches LIKE and keeps both).
                CASE WHEN LOWER(COALESCE(COALESCE(apv.title, p.title), '')) LIKE :query_like THEN 90 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(COALESCE(apv.brand, p.brand), '')) LIKE :query_like THEN 70 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(m.merchant_name, '')) LIKE :query_like THEN 50 ELSE 0 END +
                CASE WHEN LOWER(COALESCE(p.source_product_id, '')) LIKE :query_like THEN 40 ELSE 0 END +
                CASE WHEN p.pdp_scope = 'multi_merchant_canonical' THEN 200 ELSE 0 END +
                CASE WHEN p.pdp_lifecycle_stage = 'published' THEN 60 ELSE 0 END +
                CASE WHEN p.pdp_lifecycle_stage = 'validated' THEN 20 ELSE 0 END
                {category_score}{token_score}
            ) AS rank_score
        FROM catalog_products p
        JOIN index_pipeline_state ips
          ON ips.content_key = p.content_key
         AND ips.index_eligible = TRUE
        LEFT JOIN agent_pdp_view apv ON apv.content_key = p.content_key
        LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
        WHERE p.content_key IS NOT NULL
          AND (
            LOWER(COALESCE(COALESCE(apv.title, p.title), '')) LIKE :query_like OR
            LOWER(COALESCE(COALESCE(apv.brand, p.brand), '')) LIKE :query_like OR
            LOWER(COALESCE(m.merchant_name, '')) LIKE :query_like OR
            LOWER(COALESCE(p.source_product_id, '')) LIKE :query_like
            {category_where}{token_where}
          )
          {merchant_clause}
          {lifecycle_clause}
          {sync_status_clause}
          {merchant_status_clause}
          {suppression_clause}
          {indexable_clause}
        ORDER BY rank_score DESC, p.updated_at DESC
        LIMIT :row_limit
        """,
        params,
    )
    return [_row_dict(row) for row in rows]


def _build_citable_items(
    rows: List[Dict[str, Any]],
    *,
    query: Optional[str],
) -> List[PivotResultItem]:
    """Build OFFER-FREE, NON-buyable PivotResultItems from citable rows.

    Each item has offers=[] and buyable=False. The product carries a canonical
    PDP destination (the Pivota canonical URL, falling back to the product's own
    canonical_url) so an agent can CITE it. There is NO sku_key and NO offer_id,
    so the quote/order path resolves nothing and fails closed for these items.
    """
    items: List[PivotResultItem] = []
    seen_content_keys: set[str] = set()
    for row in rows:
        content_key = str(row.get("content_key") or "").strip()
        # Dedupe within the lane: agent_pdp_view is content_key-keyed, but
        # catalog_products can fan out multiple product_keys onto one
        # content_key — collapse to one citable item per content_key.
        if content_key and content_key in seen_content_keys:
            continue
        if content_key:
            seen_content_keys.add(content_key)
        destination_url = (
            str(row.get("pivota_canonical_url") or "").strip()
            or str(row.get("canonical_url") or "").strip()
            or None
        )
        match_reason = _canonical_match_reason(row, query or "")
        match_reason["lane"] = "citable_canonical"
        match_reason["buyable"] = False
        match_reason["candidate_source"] = "index_eligible"
        match_reason["content_key"] = content_key or None
        match_reason["destination_url"] = destination_url
        items.append(
            PivotResultItem(
                merchant=MerchantNode(
                    merchant_id=row.get("merchant_id"),
                    merchant_name=row.get("merchant_name"),
                    primary_platform=row.get("merchant_primary_platform"),
                ),
                product=ProductNode(
                    product_key=row.get("product_key"),
                    source_product_id=row.get("source_product_id"),
                    title=row.get("product_title"),
                    description=row.get("product_description"),
                    brand=row.get("brand"),
                    product_type=row.get("product_type"),
                    category=row.get("category"),
                    canonical_url=destination_url,
                    image_url=row.get("product_image_url"),
                ),
                # No SKU node content — there is no buyable variant to resolve.
                sku=SkuNode(),
                # OFFER-FREE: the lane carries no catalog_offers join, so there is
                # never an OfferNode here. This is what makes the item un-buyable.
                offers=[],
                buyable=False,
                # NOT 'internal_merchant' — that catalog_track gets an ordering
                # boost in _sort_items. A 'citation' track keeps citable rows
                # neutral (no ownership/offer boost; merit-only ranking).
                catalog_track="citation",
                truth_tier=str(row.get("truth_tier") or "primary"),
                readiness_tier=str(row.get("readiness_tier") or "knowledge_ready"),
                freshness=_json_dict(row.get("freshness_json")) or {
                    "updated_at": str(row.get("product_updated_at") or ""),
                },
                source_system=row.get("source_system"),
                match_explanation=match_reason,
                verticals={},
            )
        )
    return items


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
            o.offer_type AS offer_offer_type,
            o.market AS offer_market,
            o.is_first_party AS offer_is_first_party,
            o.source_domain AS offer_source_domain,
            o.why_buy_direct AS offer_why_buy_direct,
            o.offer_payload,
            -- P1: the OFFER SELLER's verified brand relationship (offer-scoped,
            -- joined on o.merchant_id exactly like the canonical search path) so
            -- the reader classifies brand_direct on this product-scoped path too.
            -- Flag-gated (ENABLE_BRAND_DIRECT_OFFER_TYPE); NOT a rank signal.
            bm.metadata_json->>'brand_relationship' AS brand_relationship
        FROM catalog_products p
        JOIN catalog_skus s ON s.product_key = p.product_key
        JOIN catalog_offers o
          ON o.sku_key = s.sku_key
         AND o.suppressed_at IS NULL
        LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
        LEFT JOIN catalog_merchants bm ON bm.merchant_id = o.merchant_id
        WHERE p.product_key = :product_key
          -- H2 (#1648): this by-key door carried NO source gate at all — only
          -- `o.suppressed_at IS NULL`. Search stopped emitting withdrawn keys
          -- after #1650/#1655, but ANY caller holding a key could still resolve
          -- one here. Measured on prod 2026-07-31: 2,045 of 14,749 rows this
          -- lane returns are withdrawn content (1,534 product_keys / 2,040
          -- sku_keys), every one an intentional editorial withdrawal —
          -- step5 dedupe, wrong-brand namesake, retired pilots, test variants.
          --
          -- FOUR of recall's FIVE unscoped legs. Recall also applies
          -- `AND p.sync_status = 'live'` (see the recall lane's sync_status_clause); that leg is deliberately NOT
          -- copied here, because a by-key lookup of a stale-but-not-withdrawn
          -- row is a legitimate read (the caller already holds the key, and
          -- staleness is a freshness signal, not an editorial withdrawal).
          -- Measured on prod 2026-07-31: 60 product_keys survive these gates
          -- and are gated by recall on that leg alone — all `sync_status`
          -- 'stale', all external_seed, 0 serving-eligible. So the doors are
          -- deliberately NOT identical; do not "fix" this by adding the leg
          -- without deciding that question on its merits.
          -- The four copied legs are:
          --   owner status + indexable   (#1650)
          --   product-row suppression, BOTH columns   (#1650)
          --   sku suppression, BOTH columns   (H3, #1655)
          --   OFFER SELLER status + indexable   (H1, #1655)
          -- NULL-keeping COALESCE is load-bearing on both merchant aliases:
          -- external seeds have no catalog_merchants row and must keep
          -- resolving (prior art: tests/test_pivota_canonical_routes.py:674).
          --
          -- Unconditional, with no merchant_id escape hatch, because this lane
          -- has no merchant scope: `/v1/pivot/products/{key}` and `/skus/{key}`
          -- answer any authenticated caller, so there is no "their own rows"
          -- case to preserve — unlike the recall lane's merchant-scoped branch.
          AND lower(COALESCE(m.status, 'active')) <> 'inactive'
          AND COALESCE(m.indexable, TRUE) IS TRUE
          AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
          AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
          AND lower(COALESCE(bm.status, 'active')) <> 'inactive'
          AND COALESCE(bm.indexable, TRUE) IS TRUE
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
            o.offer_type AS offer_offer_type,
            o.market AS offer_market,
            o.is_first_party AS offer_is_first_party,
            o.source_domain AS offer_source_domain,
            o.why_buy_direct AS offer_why_buy_direct,
            o.offer_payload,
            -- P1: the OFFER SELLER's verified brand relationship (offer-scoped,
            -- joined on o.merchant_id exactly like the canonical search path) so
            -- the reader classifies brand_direct on this sku-scoped path too.
            -- Flag-gated (ENABLE_BRAND_DIRECT_OFFER_TYPE); NOT a rank signal.
            bm.metadata_json->>'brand_relationship' AS brand_relationship
        FROM catalog_skus s
        JOIN catalog_products p ON p.product_key = s.product_key
        JOIN catalog_offers o
          ON o.sku_key = s.sku_key
         AND o.suppressed_at IS NULL
        LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
        LEFT JOIN catalog_merchants bm ON bm.merchant_id = o.merchant_id
        WHERE s.sku_key = :sku_key
          -- H2 (#1648): this by-key door carried NO source gate at all — only
          -- `o.suppressed_at IS NULL`. Search stopped emitting withdrawn keys
          -- after #1650/#1655, but ANY caller holding a key could still resolve
          -- one here. Measured on prod 2026-07-31: 2,045 of 14,749 rows this
          -- lane returns are withdrawn content (1,534 product_keys / 2,040
          -- sku_keys), every one an intentional editorial withdrawal —
          -- step5 dedupe, wrong-brand namesake, retired pilots, test variants.
          --
          -- The four legs mirror the recall lane exactly, so the two doors
          -- cannot drift apart again:
          --   owner status + indexable   (#1650)
          --   product-row suppression, BOTH columns   (#1650)
          --   sku suppression, BOTH columns   (H3, #1655)
          --   OFFER SELLER status + indexable   (H1, #1655)
          -- NULL-keeping COALESCE is load-bearing on both merchant aliases:
          -- external seeds have no catalog_merchants row and must keep
          -- resolving (prior art: tests/test_pivota_canonical_routes.py:674).
          --
          -- Unconditional, with no merchant_id escape hatch, because this lane
          -- has no merchant scope: `/v1/pivot/products/{key}` and `/skus/{key}`
          -- answer any authenticated caller, so there is no "their own rows"
          -- case to preserve — unlike the recall lane's merchant-scoped branch.
          AND lower(COALESCE(m.status, 'active')) <> 'inactive'
          AND COALESCE(m.indexable, TRUE) IS TRUE
          AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
          AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
          AND lower(COALESCE(bm.status, 'active')) <> 'inactive'
          AND COALESCE(bm.indexable, TRUE) IS TRUE
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
    market: Optional[str] = None,
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
    payment_evidence_by_offer: Dict[str, Dict[str, Any]] = {}
    targets_by_merchant: Dict[str, List[PaymentOfferTarget]] = {}
    if include_incentives:
        for row in rows:
            offer_id = str(row.get("offer_id") or "").strip()
            merchant_id = str(row.get("merchant_id") or "").strip()
            if not offer_id or not merchant_id:
                continue
            targets_by_merchant.setdefault(merchant_id, []).append(
                PaymentOfferTarget(
                    target_id=offer_id,
                    merchant_id=merchant_id,
                    product_id=str(row.get("source_product_id") or "").strip() or None,
                    variant_id=str(row.get("source_variant_id") or "").strip() or None,
                    offer_id=offer_id,
                    amount=_to_decimal(row.get("merchant_effective_price")),
                    currency=row.get("currency"),
                    market=market,
                )
            )
        for merchant_id, targets in targets_by_merchant.items():
            payment_evidence_by_offer.update(
                await resolve_payment_offer_evidence_for_targets(
                    merchant_id=merchant_id,
                    targets=targets,
                    payment_context=payment_context,
                    market=market,
                )
            )
    items: List[PivotResultItem] = []

    for sku_key, sku_rows in grouped.items():
        first = sku_rows[0]
        offers: List[OfferNode] = []
        for row in sku_rows:
            offer_id = str(row.get("offer_id") or "").strip()
            offer_incentives = incentives_by_offer.get(offer_id, [])
            offers.append(
                _build_canonical_offer_node(
                    row,
                    offer_incentives,
                    payment_offer_evidence=payment_evidence_by_offer.get(offer_id),
                )
            )
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
                    pivota_signature_id=first.get("pivota_signature_id"),
                    source_product_id=first.get("source_product_id"),
                    title=first.get("product_title"),
                    description=first.get("product_description"),
                    brand=first.get("brand"),
                    product_type=first.get("product_type"),
                    category=first.get("category"),
                    canonical_url=(
                        first.get("pivota_canonical_url")
                        or first.get("canonical_url")
                    ),
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


def _external_text_relevance_score(row: Dict[str, Any], query: str) -> float:
    candidate = score_external_beauty_candidate(
        build_ranked_external_beauty_candidate(row, source_order=0),
        query=query,
    )
    return float(candidate.candidate_score)


def _external_visible_option_labels(candidate: RankedExternalBeautyCandidate) -> List[str]:
    labels: List[str] = []
    for variant in candidate.filter_product.variants or []:
        for label in variant.visible_option_labels or []:
            normalized = str(label or "").strip()
            if normalized and normalized not in labels:
                labels.append(normalized)
    return labels


def _external_seed_offer_identity(
    candidate: RankedExternalBeautyCandidate,
) -> Dict[str, Any]:
    """Fix Plan C: derive WHO SELLS a directly-served external seed.

    The external-seed fallback lane serves rows straight from
    external_product_seeds and never touches catalog_offers, so these offers
    have no domain-derived offer_type written for them. Classify by the seed's
    OWN domain via the shared derivation (the same module the write path uses):
    a known-retailer host -> 'retailer', a brand-owned host -> 'brand_direct'
    (first-party / official), everything else -> None ("unknown"). This replaces
    the old blanket 'retailer' guess, which mislabeled every brand-D2C seed.

    No official_domain is available on this lane, so brand_direct is recognized
    only via the brand-token-in-domain rule; a brand seed on an unrelated host
    honestly resolves to unknown rather than being guessed either way.

    Brand MUST come from the seed's DECLARED fields, never candidate.brand:
    beauty_external_ranking falls candidate.brand back to the domain host when a
    seed carries no brand, and passing that host as `brand` would make
    brand_owns_domain(host, host) trivially true -> a brandless reseller seed
    would be over-claimed as brand_direct/first-party/official. A seed with no
    declared brand passes brand=None and correctly resolves to retailer (rule 0)
    or unknown (rule 3), never brand_direct."""
    seed_data = candidate.seed_data or {}
    declared_brand = (
        seed_data.get("vendor")
        or seed_data.get("brand")
        or seed_data.get("manufacturer")
    )
    return derive_offer_seller_identity(
        domain=candidate.domain,
        canonical_url=candidate.canonical_url or candidate.destination_url,
        brand=declared_brand,
    )


def _build_external_item_from_candidate(
    candidate: RankedExternalBeautyCandidate,
    *,
    query: str,
) -> PivotResultItem:
    row = candidate.row
    seed_data = candidate.seed_data
    seller_identity = _external_seed_offer_identity(candidate)
    pricing = PivotPricing(
        currency=candidate.price_currency or row.get("price_currency"),
        list_price=_to_decimal(candidate.price_amount if candidate.price_amount is not None else row.get("price_amount")),
        merchant_effective_price=_to_decimal(candidate.price_amount if candidate.price_amount is not None else row.get("price_amount")),
        estimated_best_price=_to_decimal(candidate.price_amount if candidate.price_amount is not None else row.get("price_amount")),
        exact_quote_price=None,
        price_confidence=Decimal("0.6"),
    )
    return PivotResultItem(
        merchant=MerchantNode(
            merchant_id=None,
            merchant_name=str(candidate.brand or row.get("domain") or "").strip() or None,
            primary_platform="external_referral",
        ),
        product=ProductNode(
            product_key=None,
            source_product_id=candidate.external_product_id,
            title=str(candidate.title or "").strip() or None,
            description=str(candidate.description or "").strip() or None,
            brand=str(candidate.brand or row.get("domain") or "").strip() or None,
            product_type=str(candidate.product_type or "").strip() or None,
            category=str(candidate.category or candidate.product_type or "").strip() or None,
            canonical_url=str(candidate.canonical_url or candidate.destination_url or "").strip() or None,
            image_url=row.get("image_url"),
        ),
        sku=SkuNode(
            sku_key=None,
            source_variant_id=None,
            sku=str(seed_data.get("sku") or "").strip() or None,
            barcode=None,
            title=str(candidate.title or row.get("title") or "").strip() or None,
            visible_attributes=dict(candidate.filter_product.visible_attributes or {}),
            visible_option_labels=_external_visible_option_labels(candidate),
            ingredient_ids=list(candidate.filter_product.ingredient_ids or []),
        ),
        offers=[
            OfferNode(
                offer_id=f"external::{candidate.external_product_id}",
                catalog_track="external_referral",
                truth_tier="fallback",
                readiness_tier=_readiness_from_seed(seed_data),
                offer_mode="redirect",
                source_system="external_product_seeds",
                availability=candidate.availability or row.get("availability"),
                inventory_quantity=None,
                offer_type=seller_identity["offer_type"],
                market=str(seed_data.get("market") or row.get("market") or "US"),
                is_first_party=bool(seller_identity["is_first_party"]),
                # A brand-owned external seed is served from the brand's OWN
                # domain -> official_source (the trust signal), mirroring the
                # canonical offer node. Retailer/unknown seeds stay False.
                official_source=bool(seller_identity["is_first_party"]),
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
            "candidate_source": "external_seed",
            "destination_url": candidate.destination_url,
            "canonical_url": candidate.canonical_url,
            "ranking_audit_version": BEAUTY_EXTERNAL_RANKING_AUDIT_VERSION,
            "candidate_score": candidate.candidate_score,
            "relevance_score": candidate.candidate_score,
            "text_relevance_score": candidate.ranking_score_breakdown.get("text_relevance"),
            "source_boost": candidate.source_boost,
            "quality_penalties": candidate.ranking_score_breakdown.get("quality_penalties"),
            "quality_penalties_total": candidate.quality_penalties_total,
            "ranking_features": candidate.ranking_features,
            "ranking_score_breakdown": candidate.ranking_score_breakdown,
            "ranking_drop_reason": list(candidate.ranking_drop_reason),
            "brand_term_hit": candidate.brand_term_hit,
            "source_order": candidate.source_order,
        },
        verticals={},
    )


def _build_external_item(row: Dict[str, Any], query: str, *, source_order: int) -> PivotResultItem:
    candidate = score_external_beauty_candidate(
        build_ranked_external_beauty_candidate(row, source_order=source_order),
        query=query,
    )
    return _build_external_item_from_candidate(candidate, query=query)


def _sort_items(items: List[PivotResultItem]) -> List[PivotResultItem]:
    def sort_key(item: PivotResultItem) -> tuple[int, int, float, float, int, Decimal]:
        internal_boost = 1 if item.catalog_track == "internal_merchant" else 0
        exact_boost = 1 if item.match_explanation.get("exact_match") else 0
        relevance_boost = 0.0
        source_boost = 0.0
        try:
            relevance_boost = float(
                item.match_explanation.get("candidate_score")
                or item.match_explanation.get("relevance_score")
                or 0.0
            )
        except Exception:
            relevance_boost = 0.0
        try:
            source_boost = float(item.match_explanation.get("source_boost") or 0.0)
        except Exception:
            source_boost = 0.0
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
        # RECALL_RELEVANCE_V2: structure (scope/lifecycle/category) is a SECONDARY
        # tie-break AFTER text relevance — so a multi_merchant_canonical PDP wins
        # ties among similarly-relevant rows but can't leapfrog a more relevant
        # one. 0 when v2 is off ⇒ this key is inert and ordering is unchanged.
        structure_boost = 0.0
        if _recall_relevance_v2_enabled():
            try:
                structure_boost = float(item.match_explanation.get("structure_score") or 0.0)
            except Exception:
                structure_boost = 0.0
        return (
            -exact_boost,
            -internal_boost,
            -(relevance_boost + source_boost),
            -structure_boost,
            source_order,
            best_price if best_price is not None else Decimal("999999"),
        )

    return sorted(items, key=sort_key)


async def _fetch_external_fallback_items(request: PivotQueryRequest) -> List[PivotResultItem]:
    # Phase 2b note: the plan calls for a LEFT JOIN catalog_products on
    # eps.attached_product_key here so external seeds inherit PDP-level
    # category/brand for ranking. That JOIN is deferred until Phase 3 lands
    # the seed→PDP matcher — today attached_product_key is NULL on
    # ~all rows, so the JOIN would be a no-op. Once Phase 3 populates
    # attached_product_key, wire the decoration in _decorate_external_rows
    # below (planned helper) and surface pdp.category_path into
    # rank_external_seed_rows.
    query_terms = seed_search_terms(request.query)
    external_limit = min(max(request.limit * 2, 30), 200)
    # PR: stage_a / stage_b seed query budgets were 0.9s / 1.6s. Recall probe
    # v6 (pivota-agent-ui main reports/recall_v1/recall_v6_*) showed 12
    # shopping_agent queries (~22pp pass-rate) hitting query_timeout at the
    # ~5–6s mark — past the seed query alone, but the cumulative
    # stage_a + stage_b budget plus the build pass eats most of the
    # outer find_products_multi budget. Bumping to env-overridable defaults
    # gives the SQL more headroom for non-trivial WHERE clauses (multi-term
    # ILIKE on external_product_seeds is the dominant cost).
    stage_a_timeout_s = float(os.environ.get("PIVOT_STAGE_A_SEED_QUERY_TIMEOUT_S") or 1.5)
    stage_b_timeout_s = float(os.environ.get("PIVOT_STAGE_B_SEED_QUERY_TIMEOUT_S") or 2.5)
    stage_a_result = await fetch_external_seed_rows(
        database=database,
        market=request.market,
        query=request.query,
        limit=external_limit,
        include_seed_data_text_match=False,
        only_unattached=False,
        query_timeout_seconds=stage_a_timeout_s,
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
            query_timeout_seconds=stage_b_timeout_s,
            required_terms=None,
            prefer_terms=query_terms or None,
            scope="default",
            use_required_terms_filter=False,
            include_total_count=False,
        )
        external_rows = stage_b_result.get("rows") or []
    ranked_candidates = rank_external_seed_rows(
        external_rows,
        query=request.query,
        limit=external_limit,
    )
    return [
        _build_external_item_from_candidate(candidate, query=request.query)
        for candidate in ranked_candidates
    ]


async def search_pivot_catalog(request: PivotQueryRequest) -> PivotQueryResponse:
    started = time.perf_counter()
    query_semantic_class = classify_query_semantic_class(request.query)
    canonical_rows = await _fetch_canonical_search_rows(
        query=request.query,
        merchant_id=request.merchant_id,
        limit=request.limit,
        require_signature=request.canonical_entities_only,
        brand_anchor_terms=request.brand_anchor_terms,
    )
    canonical_items = await _build_canonical_items(
        canonical_rows,
        query=request.query,
        payment_context=request.payment_context,
        market=request.market,
        include_vertical_payload=_vertical_intent(request.query),
        include_incentives=request.include_incentives,
    )

    external_items: List[PivotResultItem] = []
    if (
        request.include_external
        and not request.canonical_entities_only
        and query_semantic_class in {"beauty", "fragrance"}
        and len(canonical_items) < max(3, request.limit)
    ):
        external_items = await _fetch_external_fallback_items(request)

    # ADR-008 SLICE 3: the OFFER-FREE citable lane. Contributes index_eligible
    # (citation-only, NEVER buyable) rows for inform/recommend intent only.
    #
    # Hard gates (ALL must hold for the lane to even run):
    #   (a) INDEX_ELIGIBLE_RECALL flag ON — default OFF ⇒ no new SQL, the lane
    #       never executes, and recall is byte-identical to today.
    #   (b) NOT strict_serving_mode — when the surface is commerce-explicit
    #       (shopping intent), citation-only rows are SUPPRESSED.
    #
    # Best-effort: any failure in the citable lane is swallowed so it can never
    # break the offer-backed recall above.
    citable_items: List[PivotResultItem] = []
    if _index_eligible_recall_enabled() and not request.strict_serving_mode:
        try:
            citable_rows = await _fetch_citable_canonical_rows(
                query=request.query,
                merchant_id=request.merchant_id,
                limit=request.limit,
            )
            built_citable = _build_citable_items(citable_rows, query=request.query)
            # Dedupe against the offer-backed results: if a product is already
            # present via a buyable offer, prefer that — never double-list it as
            # citable. Dedupe on content_key (then product_key as a fallback) of
            # the buyable items.
            buyable_content_keys: set[str] = set()
            buyable_product_keys: set[str] = set()
            for buyable_item in canonical_items + external_items:
                bck = str(
                    (buyable_item.match_explanation or {}).get("content_key") or ""
                ).strip()
                if bck:
                    buyable_content_keys.add(bck)
                bpk = str(buyable_item.product.product_key or "").strip()
                if bpk:
                    buyable_product_keys.add(bpk)
            for citable_item in built_citable:
                cck = str(
                    (citable_item.match_explanation or {}).get("content_key") or ""
                ).strip()
                cpk = str(citable_item.product.product_key or "").strip()
                if cck and cck in buyable_content_keys:
                    continue
                if cpk and cpk in buyable_product_keys:
                    continue
                citable_items.append(citable_item)
        except Exception:
            logger.warning("pivot_citable_lane_failed", exc_info=True)
            citable_items = []

    items = _sort_items(
        (canonical_items + external_items + citable_items)[: request.limit * 2]
    )[: request.limit]
    # Market-aware buyability: tag each offer domestic/cross_border + is_buy_pick
    # against the request's market so a cross-border listing (e.g. a KRW/market=KR
    # brand-direct offer answered to a US query) isn't presented as a domestic buy.
    for item in items:
        annotate_offer_nodes(item.offers, request.market)
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
                "citable_items": len(citable_items),
                "limit": request.limit,
            },
        )
    return PivotQueryResponse(query=request.query, total=len(items), items=items)


async def get_pivot_product(
    product_key: str,
    *,
    payment_context: Optional[PivotPaymentContext] = None,
    market: Optional[str] = None,
) -> Optional[PivotResultItem]:
    rows = await _fetch_canonical_rows_for_product(product_key)
    items = await _build_canonical_items(
        rows,
        query=product_key,
        payment_context=payment_context,
        market=market,
        include_vertical_payload=True,
        include_incentives=True,
    )
    return items[0] if items else None


async def get_pivot_sku(
    sku_key: str,
    *,
    payment_context: Optional[PivotPaymentContext] = None,
    market: Optional[str] = None,
) -> Optional[PivotResultItem]:
    rows = await _fetch_canonical_rows_for_sku(sku_key)
    items = await _build_canonical_items(
        rows,
        query=sku_key,
        payment_context=payment_context,
        market=market,
        include_vertical_payload=True,
        include_incentives=True,
    )
    return items[0] if items else None


async def resolve_pivot_offers(request: PivotOffersResolveRequest) -> PivotOffersResolveResponse:
    started = time.perf_counter()
    items: List[PivotResultItem] = []
    if request.sku_key:
        item = await get_pivot_sku(
            request.sku_key,
            payment_context=request.payment_context,
            market=request.market,
        )
        if item:
            items = [item]
    elif request.product_key:
        item = await get_pivot_product(
            request.product_key,
            payment_context=request.payment_context,
            market=request.market,
        )
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
        best_us_offer=select_best_us_offer(flattened_offers),
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
        LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
        LEFT JOIN catalog_merchants bm ON bm.merchant_id = o.merchant_id
        WHERE o.offer_id = :offer_id
          AND o.suppressed_at IS NULL
          -- H2 (#1648): the QUOTE door needs the same gates as the read doors.
          -- Closing get_product/get_sku while leaving this open would be half a
          -- fix on the half that matters less: this lane backs
          -- `POST /v1/pivot/quote`, so an ungated withdrawn key here does not
          -- merely leak a description — it builds a real merchant quote for
          -- content we have editorially withdrawn. Review confirmed by
          -- construction that a product gated on all three read routes still
          -- quoted successfully through here.
          AND lower(COALESCE(m.status, 'active')) <> 'inactive'
          AND COALESCE(m.indexable, TRUE) IS TRUE
          AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
          AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
          AND lower(COALESCE(bm.status, 'active')) <> 'inactive'
          AND COALESCE(bm.indexable, TRUE) IS TRUE
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
        LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
        LEFT JOIN catalog_merchants bm ON bm.merchant_id = o.merchant_id
        WHERE o.sku_key = :sku_key
          AND o.suppressed_at IS NULL
          -- H2 (#1648): the QUOTE door needs the same gates as the read doors.
          -- Closing get_product/get_sku while leaving this open would be half a
          -- fix on the half that matters less: this lane backs
          -- `POST /v1/pivot/quote`, so an ungated withdrawn key here does not
          -- merely leak a description — it builds a real merchant quote for
          -- content we have editorially withdrawn. Review confirmed by
          -- construction that a product gated on all three read routes still
          -- quoted successfully through here.
          AND lower(COALESCE(m.status, 'active')) <> 'inactive'
          AND COALESCE(m.indexable, TRUE) IS TRUE
          AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
          AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
          AND lower(COALESCE(bm.status, 'active')) <> 'inactive'
          AND COALESCE(bm.indexable, TRUE) IS TRUE
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
        LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
        LEFT JOIN catalog_merchants bm ON bm.merchant_id = o.merchant_id
        WHERE o.product_key = :product_key
          AND o.suppressed_at IS NULL
          -- H2 (#1648): the QUOTE door needs the same gates as the read doors.
          -- Closing get_product/get_sku while leaving this open would be half a
          -- fix on the half that matters less: this lane backs
          -- `POST /v1/pivot/quote`, so an ungated withdrawn key here does not
          -- merely leak a description — it builds a real merchant quote for
          -- content we have editorially withdrawn. Review confirmed by
          -- construction that a product gated on all three read routes still
          -- quoted successfully through here.
          AND lower(COALESCE(m.status, 'active')) <> 'inactive'
          AND COALESCE(m.indexable, TRUE) IS TRUE
          AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
          AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
          AND lower(COALESCE(bm.status, 'active')) <> 'inactive'
          AND COALESCE(bm.indexable, TRUE) IS TRUE
        ORDER BY o.updated_at DESC
        LIMIT 1
        """,
        {"product_key": product_key},
    )
    return _row_dict(row) if row else None


async def _source_ids_are_withdrawn(
    merchant_id: Optional[str], product_id: Optional[str], variant_id: Optional[str]
) -> bool:
    """Does a catalog row exist for these RAW PLATFORM IDs, and is it withdrawn?

    `preview_pivot_quote` has a fourth branch that takes `product_id` +
    `variant_id` (source ids) and fabricates an item with NO DB lookup at all.
    That branch is legitimate — it exists so a caller can quote a variant the
    index has never ingested, priced against the merchant's own store — but it
    was also a complete bypass of every gate this PR adds: review confirmed by
    construction that a product refused by all three read doors AND all three
    quote fetchers still quoted successfully through it. Both source ids are
    returned by `/v1/pivot/query`, `/products/{key}`, `/skus/{key}` and
    `/offers/resolve` (ProductNode.source_product_id, SkuNode.source_variant_id),
    so any caller who saw the product before it was withdrawn holds exactly what
    the bypass needs.

    The rule is deliberately narrow, because the obvious fix over-filters: NOT
    "refuse unless a clean catalog row exists" — that would break the un-indexed
    case the branch exists for — but "refuse only when a catalog row for these
    ids EXISTS and every one of them is gated". No row at all => not our
    content, not our call, allow.

    TWO KNOWN GAPS, both unreachable on prod today and recorded so the next
    reader does not mistake them for intent:
      * the lookup is scoped to rows the REQUESTER owns (`p.merchant_id`), so a
        withdrawn row owned by someone else but sold by the requester is not
        found. Every such row on prod belongs to external_seed / agent_seed::*,
        which have no connected store to quote against.
      * there is no offer-seller leg — this branch carries no offer. 0 of the
        2,045 gated offers on prod are gated by the seller leg alone.
    Both rest on an assumption nothing in this file enforces: that
    `request.merchant_id` is a connected store.

    Returns True only when we hold rows for these ids and all of them are
    withdrawn.

    A LOOKUP FAILURE PROPAGATES — it is not swallowed. An earlier version
    returned False on error, reasoning that a failed lookup must not fail the
    quote closed. That was wrong on two counts. It conflates "no row" (allow,
    correct) with "could not look" (unknown, not the same thing); and it is
    inconsistent with every OTHER branch of preview_pivot_quote, where the same
    DB failure propagates and the request fails closed. The asymmetry was
    exploitable: one induced lookup failure — and the Railway proxy drops
    queries under load — turned a hard refusal into a successful quote for
    withdrawn content, on the sell path.
    """
    mid = (merchant_id or "").strip()
    pid = (product_id or "").strip()
    vid = (variant_id or "").strip()
    # Short-circuit only — with blank ids the query below matches nothing and
    # returns False anyway, so a mutation that deletes this guard survives the
    # suite. That is correct rather than a coverage gap: the contract ("blank
    # ids are never withdrawn") holds either way, and manufacturing a row with
    # an empty source_product_id to make the guard observable would be pinning
    # a shape no writer produces.
    if not (mid and pid and vid):
        return False
    row = await database.fetch_one(
        """
            SELECT
                count(*) AS total,
                count(*) FILTER (
                    WHERE p.suppressed_at IS NULL AND p.suppression_reason IS NULL
                      AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL
                      AND lower(COALESCE(m.status, 'active')) <> 'inactive'
                      AND COALESCE(m.indexable, TRUE) IS TRUE
                ) AS serving
            FROM catalog_products p
            JOIN catalog_skus s ON s.product_key = p.product_key
            LEFT JOIN catalog_merchants m ON m.merchant_id = p.merchant_id
            WHERE p.merchant_id = :merchant_id
              AND p.source_product_id = :product_id
              AND s.source_variant_id = :variant_id
        """,
        {"merchant_id": mid, "product_id": pid, "variant_id": vid},
    )
    if row is None:
        return False
    data = dict(row)
    total = int(data.get("total") or 0)
    serving = int(data.get("serving") or 0)
    return total > 0 and serving == 0


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
            # Raw-source-id branch: no key, no DB row — see
            # _source_ids_are_withdrawn for why this needs its own check and why
            # the check is "all known rows are gated", not "a clean row exists".
            if await _source_ids_are_withdrawn(
                request.merchant_id, item.product_id, item.variant_id
            ):
                continue
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
        payment_context=request.payment_context,
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
    estimated_best_price = total

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

    payment_offer_evidence = quote_result.get("payment_offer_evidence") or empty_payment_offer_evidence()
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
        payment_offer_evidence=payment_offer_evidence,
        savings_presentation=quote_result.get("savings_presentation") or {},
        quote_payload=quote_result,
    )
