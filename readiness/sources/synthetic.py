from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from models.standard_product import StandardProduct
from readiness.models import MerchantSourceDataset


_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_demo_merchant.json"


class SyntheticMerchantSource:
    merchant_id = "synthetic-demo-merchant"

    async def load(self, merchant_id: str) -> MerchantSourceDataset:
        if merchant_id != self.merchant_id:
            raise KeyError(f"Unsupported synthetic merchant: {merchant_id}")

        payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
        merchant = payload.get("merchant") or {}
        raw_products = payload.get("products") or []
        products = [StandardProduct(**product) for product in raw_products]
        source_of_truth = dict(merchant.get("source_of_truth") or {})
        source_of_truth.setdefault("reviews_confidence", "readiness.synthetic_reviews.none.v1")
        payment_capabilities = dict(merchant.get("payment_capabilities") or {})
        stub_checkout_supported = bool(
            payment_capabilities.get("ucp_checkout_stub_supported")
            or payment_capabilities.get("merchant_native_checkout_supported")
        )
        payment_capabilities.setdefault("ucp_checkout_supported", stub_checkout_supported)
        payment_capabilities.setdefault("acp_checkout_supported", stub_checkout_supported)
        payment_capabilities.setdefault("merchant_platform_writeback_supported", False)

        product_diagnostics: Dict[str, Dict[str, Any]] = {}
        variant_diagnostics: Dict[str, Dict[str, Any]] = {}
        for product in raw_products:
            product_id = str(product.get("id") or "").strip()
            if not product_id:
                continue
            diagnostics = product.get("readiness") if isinstance(product.get("readiness"), dict) else {}
            product_diagnostics[product_id] = diagnostics
            for variant in product.get("variants") or []:
                variant_id = str(variant.get("id") or "").strip()
                if not variant_id:
                    continue
                variant_diagnostics[variant_id] = (
                    variant.get("readiness") if isinstance(variant.get("readiness"), dict) else {}
                )

        return MerchantSourceDataset(
            merchant_id=str(merchant.get("merchant_id") or self.merchant_id),
            merchant_name=str(merchant.get("display_name") or "Synthetic Demo Merchant"),
            evaluation_reference_time=str(merchant.get("evaluation_reference_time") or "2026-03-17T12:00:00Z"),
            merchant_alpha_mode="synthetic_fixture",
            source_of_truth=source_of_truth,
            capability_status={
                "merchant_adapter": "ready",
                "checkout": "stubbed",
                "order_sync": "stubbed",
                "channel_export": "ready",
                "reviews_confidence": "blocked",
            },
            merchant_blockers=[],
            merchant_warnings=["synthetic_fixture_mode"],
            stubbed_capabilities=merchant.get("stubbed_capabilities") or [],
            merchant_policy=merchant.get("merchant_policy") or {},
            payment_capabilities=payment_capabilities,
            review_diagnostics={
                "integration_status": "blocked",
                "observed_at": None,
                "products_with_reviews": 0,
                "grouped_products_with_reviews": 0,
                "products_without_reviews": len(products),
            },
            products=products,
            product_diagnostics=product_diagnostics,
            variant_diagnostics=variant_diagnostics,
            audit_notes=merchant.get("audit_notes") or [],
        )


async def load_synthetic_merchant_dataset(merchant_id: str) -> MerchantSourceDataset:
    return await SyntheticMerchantSource().load(merchant_id)
