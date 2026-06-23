from __future__ import annotations

from readiness.flags import (
    readiness_alpha_merchant_id,
    readiness_real_merchant_alpha_enabled,
    storeless_brand_catalog_enabled,
)
from readiness.models import MerchantSourceDataset
from readiness.sources.shopify_live import load_shopify_live_merchant_dataset
from readiness.sources.synthetic import load_synthetic_merchant_dataset


async def load_merchant_source_dataset(merchant_id: str) -> MerchantSourceDataset:
    if merchant_id == "synthetic-demo-merchant":
        return await load_synthetic_merchant_dataset(merchant_id)
    alpha_merchant_id = readiness_alpha_merchant_id()
    if readiness_real_merchant_alpha_enabled() and alpha_merchant_id and merchant_id == alpha_merchant_id:
        return await load_shopify_live_merchant_dataset(merchant_id)
    # Store-less brand-authored catalog — flag-gated, default OFF. Only routes
    # here when the flag is on AND this merchant actually has brand_authored
    # catalog_products rows; otherwise the trailing KeyError path is preserved
    # byte-for-byte for everyone (incl. all existing merchants when OFF).
    if storeless_brand_catalog_enabled():
        from readiness.sources.brand_authored import (
            count_brand_authored_products,
            load_brand_authored_merchant_dataset,
        )

        if await count_brand_authored_products(merchant_id) > 0:
            return await load_brand_authored_merchant_dataset(merchant_id)
    raise KeyError(merchant_id)


def supported_merchant_ids() -> list[str]:
    ids = ["synthetic-demo-merchant"]
    alpha_merchant_id = readiness_alpha_merchant_id()
    if readiness_real_merchant_alpha_enabled() and alpha_merchant_id:
        ids.append(alpha_merchant_id)
    return ids
