from __future__ import annotations

from readiness.models import ChannelReadinessReport, MerchantReadinessSnapshot


def build_ucp_export(snapshot: MerchantReadinessSnapshot) -> ChannelReadinessReport:
    offers = []
    for product in snapshot.products:
        for variant in product.variants:
            if variant.channel_coverage.get("ucp") != "ready":
                continue

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
                    "offer_id": f"ucp:{snapshot.merchant_id}:{product.product_id}:{variant.variant_id}",
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
                        "mode": "merchant_native_alpha" if snapshot.merchant_alpha_mode == "real_merchant_alpha" else "stubbed",
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
                }
            )

    validation_warnings = list(snapshot.warnings)
    validation_warnings.append("review ingestion is absent from the readiness model today")
    if snapshot.merchant_alpha_mode != "real_merchant_alpha":
        validation_warnings.append("checkout execution is stubbed for this thin slice")
        validation_warnings.append("merchant write-back is stubbed for this thin slice")

    return ChannelReadinessReport(
        merchant_id=snapshot.merchant_id,
        channel="ucp",
        generated_at=snapshot.generated_at,
        merchant_alpha_mode=snapshot.merchant_alpha_mode,
        readiness_score=next(
            (coverage.ready_variant_count * 100 // max(1, coverage.ready_variant_count + coverage.blocked_variant_count)
             for coverage in snapshot.channel_coverage if coverage.channel == "ucp"),
            0,
        ),
        capability_status=snapshot.capability_status,
        blockers=snapshot.blockers,
        warnings=snapshot.warnings,
        source_of_truth=snapshot.source_of_truth,
        validation_warnings=validation_warnings,
        stubbed_capabilities=snapshot.stubbed_capabilities,
        offers=offers,
    )
