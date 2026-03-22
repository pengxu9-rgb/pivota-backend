from __future__ import annotations

from readiness.models import ChannelReadinessReport, MerchantReadinessSnapshot


def build_acp_export(snapshot: MerchantReadinessSnapshot) -> ChannelReadinessReport:
    offers = []
    servable_product_ids = set()
    excluded_product_ids = set()
    servable_variant_count = 0
    excluded_variant_count = 0
    for product in snapshot.products:
        product_has_servable = False
        product_has_excluded = False
        for variant in product.variants:
            if variant.channel_coverage.get("acp") != "ready":
                excluded_variant_count += 1
                product_has_excluded = True
                continue
            servable_variant_count += 1
            product_has_servable = True

            shipping_policy = next(
                (
                    provenance.notes
                    for provenance in variant.provenance
                    if provenance.field == "shipping_policy" and provenance.notes
                ),
                None,
            )
            returns_policy = next(
                (
                    provenance.notes
                    for provenance in variant.provenance
                    if provenance.field == "return_policy" and provenance.notes
                ),
                None,
            )
            offers.append(
                {
                    "offer_id": f"acp:{snapshot.merchant_id}:{product.product_id}:{variant.variant_id}",
                    "merchant_id": snapshot.merchant_id,
                    "product_id": product.product_id,
                    "variant_id": variant.variant_id,
                    "title": product.title,
                    "variant_title": variant.title,
                    "description": product.description,
                    "brand": product.brand,
                    "category": product.category,
                    "image_url": variant.price.get("image_url") or product.default_image_url,
                    "price": {
                        "amount": f"{float(variant.price.get('amount') or 0):.2f}",
                        "currency": variant.price.get("currency") or "USD",
                    },
                    "availability": variant.inventory.get("availability") or "out_of_stock",
                    "inventory_quantity": variant.inventory.get("quantity") or 0,
                    "attributes": variant.attributes,
                    "shipping_summary": shipping_policy,
                    "returns_summary": returns_policy,
                    "checkout_capability": {
                        "mode": "acp" if snapshot.merchant_alpha_mode == "real_merchant_alpha" else "stubbed",
                        "supported": variant.checkout.status == "ready",
                    },
                    "readiness": {
                        "discovery_status": variant.discovery.status,
                        "checkout_status": variant.checkout.status,
                        "blockers": variant.blockers,
                        "warnings": variant.checkout.warnings,
                    },
                    "source_of_truth": {
                        family: decision.source for family, decision in variant.source_of_truth.items()
                    },
                    "freshness": {
                        family: freshness.model_dump() if hasattr(freshness, "model_dump") else freshness.dict()
                        for family, freshness in variant.freshness.items()
                    },
                    "reviews": (
                        variant.reviews.model_dump() if hasattr(variant.reviews, "model_dump") else variant.reviews.dict()
                    )
                    if variant.reviews
                    else None,
                }
            )
        if product_has_servable:
            servable_product_ids.add(product.product_id)
        if product_has_excluded:
            excluded_product_ids.add(product.product_id)

    validation_warnings = list(snapshot.warnings)
    if snapshot.capability_status.get("reviews_confidence") == "blocked":
        validation_warnings.append("review summaries are unavailable for the readiness model")
    elif any(not offer.get("reviews") or not offer["reviews"].get("has_reviews") for offer in offers):
        validation_warnings.append("review coverage is partial across exported offers")
    if snapshot.merchant_alpha_mode != "real_merchant_alpha":
        validation_warnings.append("checkout execution is stubbed for this thin slice")
        validation_warnings.append("merchant write-back is stubbed for this thin slice")

    return ChannelReadinessReport(
        export_version="readiness_acp_export.v1",
        merchant_id=snapshot.merchant_id,
        channel="acp",
        generated_at=snapshot.generated_at,
        merchant_alpha_mode=snapshot.merchant_alpha_mode,
        readiness_score=next(
            (
                coverage.ready_variant_count * 100 // max(1, coverage.ready_variant_count + coverage.blocked_variant_count)
                for coverage in snapshot.channel_coverage
                if coverage.channel == "acp"
            ),
            0,
        ),
        capability_status=snapshot.capability_status,
        blockers=snapshot.blockers,
        warnings=snapshot.warnings,
        source_of_truth=snapshot.source_of_truth,
        validation_warnings=validation_warnings,
        stubbed_capabilities=snapshot.stubbed_capabilities,
        servable_product_count=len(servable_product_ids),
        servable_variant_count=servable_variant_count,
        excluded_product_count=len(excluded_product_ids),
        excluded_variant_count=excluded_variant_count,
        offers=offers,
    )
