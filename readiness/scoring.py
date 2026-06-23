from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from models.standard_product import StandardProduct, StandardProductVariant
from readiness.models import (
    CapabilityStatus,
    ChannelCoverageStatus,
    MerchantReadinessSnapshot,
    MerchantSourceDataset,
    ReadyProduct,
    ReviewSummary,
    ReadyVariant,
)
from readiness.models import FieldFamilyStatus, FieldFreshness, FieldProvenance
from readiness.source_of_truth import FIELD_FAMILY_RULES, build_field_family_status


# Non-blocking N/A status for a field family that doesn't apply in the current
# mode (e.g. commerce families for store-less brand-authored merchants — they
# have no store, so there is no price/inventory/checkout/order source of truth).
# status="not_applicable" is non-blocking by construction: empty blockers AND
# empty warnings, so it never penalizes the discovery/checkout scores and never
# appears in a variant's blocker list.
def _not_applicable_field_status(
    family: str, note: str,
) -> Tuple[FieldFamilyStatus, FieldFreshness, FieldProvenance]:
    rule = FIELD_FAMILY_RULES[family]
    status = FieldFamilyStatus(
        family=family,
        status="not_applicable",
        canonical_source=rule.canonical_source,
        source=rule.canonical_source,
        fallback_source=rule.fallback_source,
        observed_at=None,
        max_age_hours=rule.max_age_hours,
        stale=False,
        blockers=[],
        warnings=[],
        notes=[note],
    )
    freshness = FieldFreshness(
        field=family,
        observed_at=None,
        max_age_hours=rule.max_age_hours,
        stale=False,
        source=rule.canonical_source,
    )
    provenance = FieldProvenance(
        field=family,
        source=rule.canonical_source,
        observed_at=None,
        source_of_truth=True,
        fallback_source=rule.fallback_source,
        notes=note,
    )
    return status, freshness, provenance


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _iso(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _score_from_penalties(base: int, penalties: Iterable[int]) -> int:
    score = base
    for penalty in penalties:
        score -= penalty
    return max(0, min(100, score))


def _build_variant_readiness(
    *,
    dataset: MerchantSourceDataset,
    product: StandardProduct,
    variant: StandardProductVariant,
    reference_time: datetime,
    channel: str,
) -> ReadyVariant:
    synthetic_mode = dataset.merchant_alpha_mode != "real_merchant_alpha"
    # Store-less brand-authored mode: the merchant has no connected store, so the
    # COMMERCE field families (price, inventory, checkout_capability, order_status,
    # fulfillment_policy) have no source of truth and must resolve to a
    # non-blocking N/A — never emit commerce blockers. Content/discovery families
    # (catalog) stay as-is. Strictly gated so real Shopify merchants are unaffected.
    brand_authored_mode = dataset.merchant_alpha_mode == "brand_authored"
    legacy_max_age = {
        "catalog": 168.0,
        "price": 48.0,
        "inventory": 24.0,
        "fulfillment_policy": 24.0 * 30,
        "checkout_capability": 1.0,
        "order_status": 1.0,
    }
    product_diag = dataset.product_diagnostics.get(product.id, {})
    variant_diag = dataset.variant_diagnostics.get(variant.id, {})
    field_sources = {}
    field_sources.update(product_diag.get("field_sources") or {})
    field_sources.update(variant_diag.get("field_sources") or {})

    catalog_observed_at = variant_diag.get("catalog_last_refreshed_at") or product_diag.get("catalog_last_refreshed_at")
    media_observed_at = variant_diag.get("media_last_refreshed_at") or product_diag.get("media_last_refreshed_at")
    price_observed_at = variant_diag.get("price_last_refreshed_at") or product_diag.get("price_last_refreshed_at")
    inventory_observed_at = variant_diag.get("inventory_last_refreshed_at") or product_diag.get("inventory_last_refreshed_at")
    policy_observed_at = dataset.merchant_policy.get("last_reviewed_at")

    image_url = variant.image_url or product.image_url or (product.images[0] if product.images else None)
    inventory_quantity = int(variant.inventory_quantity or 0)
    shipping_profile = variant_diag.get("shipping_profile")
    review_payload = dataset.variant_review_summaries.get(variant.id) or dataset.product_review_summaries.get(product.id) or {}
    review_summary = ReviewSummary(**review_payload) if review_payload else None

    catalog_blockers: List[str] = []
    catalog_warnings: List[str] = []
    if not product.title:
        catalog_blockers.append("missing_title")
    if not image_url:
        catalog_blockers.append("missing_primary_image")
    if not product.description:
        catalog_warnings.append("missing_description")
    media_observed = _parse_datetime(media_observed_at)
    if media_observed is not None:
        media_age_hours = (reference_time - media_observed.astimezone(timezone.utc)).total_seconds() / 3600
        if media_age_hours > 72:
            catalog_warnings.append("media_snapshot_stale")
    catalog_status, catalog_freshness, catalog_provenance = build_field_family_status(
        family="catalog",
        source=(field_sources.get("catalog") or {}).get("source") or dataset.source_of_truth.get("catalog"),
        fallback_source=(field_sources.get("catalog") or {}).get("fallback_source"),
        observed_at=catalog_observed_at,
        reference_time=reference_time,
        max_age_hours_override=legacy_max_age["catalog"] if synthetic_mode else None,
        blockers=catalog_blockers,
        warnings=catalog_warnings,
        notes=["catalog/title/description/media readiness for discovery"],
    )

    if brand_authored_mode:
        price_status, price_freshness, price_provenance = _not_applicable_field_status(
            "price", "brand_authored: no store price source of truth (N/A)"
        )
    else:
        price_blockers: List[str] = []
        price_warnings: List[str] = []
        if variant.price is None or float(variant.price) <= 0:
            price_blockers.append("missing_price")
        if not product.currency:
            price_blockers.append("missing_currency")
        price_status, price_freshness, price_provenance = build_field_family_status(
            family="price",
            source=(field_sources.get("price") or {}).get("source") or dataset.source_of_truth.get("price"),
            fallback_source=(field_sources.get("price") or {}).get("fallback_source"),
            observed_at=price_observed_at,
            reference_time=reference_time,
            max_age_hours_override=legacy_max_age["price"] if synthetic_mode else None,
            blockers=price_blockers,
            warnings=price_warnings,
            notes=["canonical price/currency selection for export and checkout"],
        )

    if brand_authored_mode:
        inventory_status, inventory_freshness, inventory_provenance = _not_applicable_field_status(
            "inventory", "brand_authored: no store inventory source of truth (N/A)"
        )
    else:
        inventory_blockers: List[str] = []
        inventory_warnings: List[str] = []
        if inventory_quantity <= 0:
            inventory_blockers.append("out_of_stock")
        inventory_status, inventory_freshness, inventory_provenance = build_field_family_status(
            family="inventory",
            source=(field_sources.get("inventory") or {}).get("source") or dataset.source_of_truth.get("inventory"),
            fallback_source=(field_sources.get("inventory") or {}).get("fallback_source"),
            observed_at=inventory_observed_at,
            reference_time=reference_time,
            max_age_hours_override=legacy_max_age["inventory"] if synthetic_mode else None,
            blockers=inventory_blockers,
            warnings=inventory_warnings,
            notes=["checkout prefers live Shopify inventory when available"],
        )

    if brand_authored_mode:
        policy_status, policy_freshness, policy_provenance = _not_applicable_field_status(
            "fulfillment_policy",
            "brand_authored: no store fulfillment/shipping/returns source of truth (N/A)",
        )
    else:
        policy_blockers: List[str] = []
        policy_warnings: List[str] = []
        if not shipping_profile:
            policy_blockers.append("missing_shipping_profile")
        if not dataset.merchant_policy.get("shipping_supported"):
            policy_blockers.append("merchant_shipping_policy_missing")
        if not dataset.merchant_policy.get("returns_supported"):
            policy_blockers.append("merchant_return_policy_missing")
        policy_status, policy_freshness, policy_provenance = build_field_family_status(
            family="fulfillment_policy",
            source=dataset.source_of_truth.get("fulfillment_policy") or dataset.source_of_truth.get("policy") or dataset.source_of_truth.get("fulfillment"),
            observed_at=policy_observed_at,
            reference_time=reference_time,
            max_age_hours_override=legacy_max_age["fulfillment_policy"] if synthetic_mode else None,
            blockers=policy_blockers,
            warnings=policy_warnings,
            notes=[
                str(dataset.merchant_policy.get("shipping_summary") or ""),
                str(dataset.merchant_policy.get("returns_summary") or ""),
            ],
        )

    if brand_authored_mode:
        checkout_capability_status, checkout_capability_freshness, checkout_capability_provenance = _not_applicable_field_status(
            "checkout_capability",
            "brand_authored: no merchant checkout capability (N/A)",
        )
    else:
        checkout_capability_blockers: List[str] = []
        checkout_capability_warnings: List[str] = []
        merchant_checkout_supported = bool(dataset.payment_capabilities.get("merchant_native_checkout_supported"))
        checkout_stub_supported = bool(dataset.payment_capabilities.get("ucp_checkout_stub_supported"))
        if dataset.merchant_alpha_mode == "real_merchant_alpha" and not merchant_checkout_supported:
            checkout_capability_blockers.append("merchant_checkout_capability_missing")
        elif dataset.merchant_alpha_mode != "real_merchant_alpha" and not checkout_stub_supported:
            checkout_capability_blockers.append("checkout_stub_missing")
        if dataset.merchant_alpha_mode != "real_merchant_alpha" and not merchant_checkout_supported:
            checkout_capability_warnings.append("payment_execution_stubbed")
        checkout_capability_status, checkout_capability_freshness, checkout_capability_provenance = build_field_family_status(
            family="checkout_capability",
            source=dataset.source_of_truth.get("checkout_capability") or dataset.source_of_truth.get("checkout"),
            observed_at=dataset.evaluation_reference_time,
            reference_time=reference_time,
            max_age_hours_override=legacy_max_age["checkout_capability"] if synthetic_mode else None,
            blockers=checkout_capability_blockers,
            warnings=checkout_capability_warnings,
            notes=[
                f"merchant_alpha_mode={dataset.merchant_alpha_mode}",
                f"psp_provider={dataset.payment_capabilities.get('psp_provider') or 'unknown'}",
            ],
        )

    if brand_authored_mode:
        order_status_status, order_status_freshness, order_status_provenance = _not_applicable_field_status(
            "order_status", "brand_authored: no store order-state source of truth (N/A)"
        )
    else:
        order_status_blockers: List[str] = []
        order_status_warnings: List[str] = []
        if dataset.merchant_alpha_mode != "real_merchant_alpha":
            order_status_warnings.append("order_sync_stubbed")
        if dataset.merchant_alpha_mode == "real_merchant_alpha" and not dataset.payment_capabilities.get("merchant_platform_writeback_supported"):
            order_status_blockers.append("merchant_writeback_unavailable")
        order_status_status, order_status_freshness, order_status_provenance = build_field_family_status(
            family="order_status",
            source=dataset.source_of_truth.get("order_status") or dataset.source_of_truth.get("order_state"),
            observed_at=dataset.evaluation_reference_time,
            reference_time=reference_time,
            max_age_hours_override=legacy_max_age["order_status"] if synthetic_mode else None,
            blockers=order_status_blockers,
            warnings=order_status_warnings,
            notes=["canonical outward order state comes from readiness order-sync journal"],
        )

    reviews_blockers: List[str] = []
    reviews_warnings: List[str] = []
    reviews_notes: List[str] = []
    review_integration_status = str(dataset.review_diagnostics.get("integration_status") or "blocked")
    if review_integration_status == "blocked":
        reviews_blockers.append("reviews_summary_unavailable")
    elif review_summary is None or not review_summary.has_reviews:
        reviews_warnings.append("no_reviews_available")
    elif review_summary.default_view != "group":
        reviews_warnings.append("cross_merchant_review_group_unresolved")
    if review_summary is not None:
        reviews_notes.extend(
            [
                f"default_view={review_summary.default_view}",
                f"review_count={review_summary.review_count}",
                f"verified_review_count={review_summary.verified_review_count}",
            ]
        )
        if review_summary.group_id is not None:
            reviews_notes.append(f"group_id={review_summary.group_id}")
        if review_summary.group_confidence is not None:
            reviews_notes.append(f"group_confidence={review_summary.group_confidence}")
    reviews_status, reviews_freshness, reviews_provenance = build_field_family_status(
        family="reviews_confidence",
        source=(review_payload or {}).get("source") or dataset.source_of_truth.get("reviews_confidence"),
        fallback_source="reviews_center.product_reviews.v1",
        observed_at=(review_payload or {}).get("latest_review_at") or dataset.review_diagnostics.get("observed_at"),
        reference_time=reference_time,
        blockers=reviews_blockers,
        warnings=reviews_warnings,
        notes=reviews_notes or ["product-level reviews summary for readiness diagnostics"],
    )

    discovery_blockers = list(catalog_status.blockers) + list(price_status.blockers)
    discovery_warnings = list(catalog_status.warnings) + list(price_status.warnings)

    checkout_blockers = (
        list(price_status.blockers)
        + list(inventory_status.blockers)
        + list(policy_status.blockers)
        + list(checkout_capability_status.blockers)
    )
    checkout_warnings = (
        list(price_status.warnings)
        + list(inventory_status.warnings)
        + list(policy_status.warnings)
        + list(checkout_capability_status.warnings)
    )
    if inventory_freshness.stale:
        checkout_blockers.append("inventory_stale")

    discovery_score = _score_from_penalties(
        100,
        [25 for _ in discovery_blockers] + [5 for _ in discovery_warnings],
    )
    checkout_score = _score_from_penalties(
        100,
        [25 for _ in checkout_blockers] + [8 for _ in checkout_warnings],
    )

    discovery_status = "ready" if not discovery_blockers else "blocked"
    checkout_status = "ready" if not checkout_blockers else "blocked"
    protocol_checkout_supported = False
    if channel == "ucp":
        protocol_checkout_supported = bool(
            dataset.payment_capabilities.get("ucp_checkout_supported")
            or dataset.payment_capabilities.get("merchant_native_checkout_supported")
        )
    elif channel == "acp":
        protocol_checkout_supported = bool(
            dataset.payment_capabilities.get("acp_checkout_supported")
            or dataset.payment_capabilities.get("merchant_native_checkout_supported")
        )

    channel_status = (
        "ready"
        if channel in {"ucp", "acp"} and protocol_checkout_supported and discovery_status == "ready" and checkout_status == "ready"
        else "blocked"
    )

    return ReadyVariant(
        variant_id=variant.id,
        sku=variant.sku,
        title=variant.title,
        attributes={str(k): str(v) for k, v in (variant.options or {}).items()},
        visible_option_labels=list(getattr(variant, "visible_option_labels", None) or []),
        price={
            "amount": float(variant.price or 0),
            "currency": product.currency,
            "compare_at_amount": float(variant.compare_at_price) if variant.compare_at_price is not None else None,
            "image_url": image_url,
        },
        inventory={
            "quantity": inventory_quantity,
            "availability": "in_stock" if inventory_quantity > 0 else "out_of_stock",
        },
        freshness={
            "catalog": catalog_freshness,
            "price": price_freshness,
            "inventory": inventory_freshness,
            "fulfillment_policy": policy_freshness,
            "checkout_capability": checkout_capability_freshness,
            "order_status": order_status_freshness,
            "reviews_confidence": reviews_freshness,
        },
        provenance=[
            catalog_provenance,
            price_provenance,
            inventory_provenance,
            policy_provenance,
            checkout_capability_provenance,
            order_status_provenance,
            reviews_provenance,
        ],
        source_of_truth={
            "catalog": catalog_status,
            "price": price_status,
            "inventory": inventory_status,
            "fulfillment_policy": policy_status,
            "checkout_capability": checkout_capability_status,
            "order_status": order_status_status,
            "reviews_confidence": reviews_status,
        },
        reviews=review_summary,
        blockers={"discovery": discovery_blockers, "checkout": checkout_blockers},
        warnings={"discovery": discovery_warnings, "checkout": checkout_warnings},
        discovery=CapabilityStatus(
            capability="discovery",
            status=discovery_status,
            score=discovery_score,
            blockers=discovery_blockers,
            warnings=discovery_warnings,
        ),
        checkout=CapabilityStatus(
            capability="checkout",
            status=checkout_status,
            score=checkout_score,
            blockers=checkout_blockers,
            warnings=checkout_warnings,
            notes=list(checkout_capability_status.notes),
        ),
        channel_coverage={channel: channel_status},
    )


def _product_variants(product: StandardProduct) -> List[StandardProductVariant]:
    if product.variants:
        return list(product.variants)
    return [
        StandardProductVariant(
            id=f"{product.id}-default",
            title=product.title,
            sku=product.sku,
            barcode=product.barcode,
            price=product.price,
            compare_at_price=product.compare_at_price,
            inventory_quantity=product.inventory_quantity,
            image_url=product.image_url,
            options={},
        )
    ]


def build_merchant_snapshot(dataset: MerchantSourceDataset, channel: str = "ucp") -> MerchantReadinessSnapshot:
    reference_time = _parse_datetime(dataset.evaluation_reference_time) or datetime.now(timezone.utc)

    ready_products: List[ReadyProduct] = []
    all_variants: List[ReadyVariant] = []
    for product in dataset.products:
        variants = [
            _build_variant_readiness(
                dataset=dataset,
                product=product,
                variant=variant,
                reference_time=reference_time,
                channel=channel,
            )
            for variant in _product_variants(product)
        ]
        all_variants.extend(variants)
        ready_products.append(
            ReadyProduct(
                product_id=product.id,
                platform=product.platform,
                title=product.title,
                description=product.description,
                brand=product.vendor,
                category=product.product_type,
                default_image_url=product.image_url or (product.images[0] if product.images else None),
                visible_attributes=dict(product.visible_attributes or {}),
                ingredient_ids=list(getattr(product, "ingredient_ids", None) or []),
                reviews=ReviewSummary(**dataset.product_review_summaries[product.id])
                if dataset.product_review_summaries.get(product.id)
                else None,
                variants=variants,
            )
        )

    if all_variants:
        catalog_score = round(sum(v.discovery.score for v in all_variants) / len(all_variants))
        offers_inventory_score = round(sum(v.checkout.score for v in all_variants) / len(all_variants))
        variants_score = round(sum(100 if v.attributes else 70 for v in all_variants) / len(all_variants))
        channel_score = round(
            sum(100 if v.channel_coverage.get(channel) == "ready" else 0 for v in all_variants) / len(all_variants)
        )
    else:
        catalog_score = offers_inventory_score = variants_score = channel_score = 0

    fulfillment_score = 85 if dataset.merchant_policy.get("shipping_supported") and dataset.merchant_policy.get("returns_supported") else 20
    if dataset.merchant_alpha_mode == "real_merchant_alpha":
        checkout_score = 80 if dataset.payment_capabilities.get("merchant_native_checkout_supported") else 25
        order_sync_score = 80 if dataset.payment_capabilities.get("merchant_platform_writeback_supported") else 25
    else:
        checkout_score = 45 if dataset.payment_capabilities.get("ucp_checkout_stub_supported") else 0
        order_sync_score = 45
    observability_score = 30
    security_score = 35
    total_products = len(dataset.products)
    review_products_with_reviews = int(dataset.review_diagnostics.get("products_with_reviews") or 0)
    grouped_review_products = int(dataset.review_diagnostics.get("grouped_products_with_reviews") or 0)
    if str(dataset.review_diagnostics.get("integration_status") or "blocked") == "blocked":
        reviews_score = 0
    elif total_products <= 0:
        reviews_score = 0
    else:
        review_coverage_ratio = review_products_with_reviews / float(total_products)
        grouped_review_ratio = grouped_review_products / float(total_products)
        reviews_score = min(100, round(25 + (50 * review_coverage_ratio) + (25 * grouped_review_ratio)))

    domain_scores = {
        "catalog": catalog_score,
        "offers_inventory": offers_inventory_score,
        "variants": variants_score,
        "reviews_confidence": reviews_score,
        "fulfillment_policy": fulfillment_score,
        "checkout_payment": checkout_score,
        "order_sync": order_sync_score,
        "channel_ucp": channel_score,
        "observability": observability_score,
        "security_operational": security_score,
    }

    readiness_score = round(sum(domain_scores.values()) / len(domain_scores)) if domain_scores else 0
    ready_variant_count = sum(1 for variant in all_variants if variant.channel_coverage.get(channel) == "ready")
    blocked_variant_count = max(0, len(all_variants) - ready_variant_count)

    merchant_capabilities = [
        CapabilityStatus(
            capability="catalog_normalization",
            status="partial" if catalog_score < 100 else "ready",
            score=catalog_score,
            warnings=["thin-slice uses a synthetic merchant fixture"] if dataset.merchant_alpha_mode == "synthetic_fixture" else list(dataset.merchant_warnings),
        ),
        CapabilityStatus(
            capability="ucp_channel_export",
            status="partial" if blocked_variant_count else "ready",
            score=channel_score,
            blockers=["blocked_variants_present"] if blocked_variant_count else [],
            warnings=["export is internal-only and not public certification"],
        ),
        CapabilityStatus(
            capability="checkout_execution",
            status=dataset.capability_status.get("checkout", "blocked"),
            score=checkout_score,
            blockers=[] if dataset.capability_status.get("checkout") != "blocked" else list(dataset.merchant_blockers or ["checkout_missing"]),
            warnings=["payment execution is stubbed"] if dataset.merchant_alpha_mode != "real_merchant_alpha" else [],
        ),
        CapabilityStatus(
            capability="order_writeback_state_sync",
            status=dataset.capability_status.get("order_sync", "partial"),
            score=order_sync_score,
            warnings=["order write-back is stubbed and does not call merchant APIs"] if dataset.merchant_alpha_mode != "real_merchant_alpha" else [],
        ),
        CapabilityStatus(
            capability="reviews_confidence",
            status=dataset.capability_status.get("reviews_confidence", "blocked"),
            score=reviews_score,
            blockers=["reviews_summary_unavailable"]
            if dataset.capability_status.get("reviews_confidence") == "blocked"
            else [],
            warnings=["review_coverage_partial"]
            if dataset.capability_status.get("reviews_confidence") != "blocked"
            and review_products_with_reviews < max(1, total_products)
            else [],
            notes=[
                f"products_with_reviews={review_products_with_reviews}",
                f"grouped_products_with_reviews={grouped_review_products}",
            ],
        ),
    ]

    audit_notes = list(dataset.audit_notes)
    if dataset.merchant_alpha_mode == "synthetic_fixture":
        audit_notes.extend(
            [
                "Google Merchant Center export is absent in the audited codebase.",
                "ChatGPT product-feed adapter is absent in the audited codebase.",
                "Synthetic thin slice does not claim merchant-native payment execution.",
            ]
        )

    channel_coverage = [
        ChannelCoverageStatus(
            channel=channel,
            status="partial" if blocked_variant_count else "ready",
            ready_variant_count=ready_variant_count,
            blocked_variant_count=blocked_variant_count,
        )
    ]

    return MerchantReadinessSnapshot(
        merchant_id=dataset.merchant_id,
        merchant_name=dataset.merchant_name,
        channel=channel,
        generated_at=_iso(reference_time) or dataset.evaluation_reference_time,
        merchant_alpha_mode=dataset.merchant_alpha_mode,
        readiness_score=readiness_score,
        domain_scores=domain_scores,
        capability_status=dataset.capability_status,
        blockers=dataset.merchant_blockers,
        warnings=dataset.merchant_warnings,
        merchant_capabilities=merchant_capabilities,
        channel_coverage=channel_coverage,
        source_of_truth=dataset.source_of_truth,
        stubbed_capabilities=dataset.stubbed_capabilities,
        audit_notes=audit_notes,
        products=ready_products,
    )


def find_ready_variant(snapshot: MerchantReadinessSnapshot, variant_id: str) -> Tuple[Optional[ReadyProduct], Optional[ReadyVariant]]:
    for product in snapshot.products:
        for variant in product.variants:
            if variant.variant_id == variant_id:
                return product, variant
    return None, None
