from __future__ import annotations

from readiness.flags import readiness_alpha_merchant_id, readiness_real_merchant_alpha_enabled
from readiness.models import MerchantSourceDataset
from readiness.sources.shopify_live import load_shopify_live_merchant_dataset
from readiness.sources.synthetic import load_synthetic_merchant_dataset


async def load_merchant_source_dataset(merchant_id: str) -> MerchantSourceDataset:
    if merchant_id == "synthetic-demo-merchant":
        return await load_synthetic_merchant_dataset(merchant_id)
    if readiness_real_merchant_alpha_enabled() and merchant_id == readiness_alpha_merchant_id():
        return await load_shopify_live_merchant_dataset(merchant_id)
    raise KeyError(merchant_id)


def supported_merchant_ids() -> list[str]:
    ids = ["synthetic-demo-merchant"]
    if readiness_real_merchant_alpha_enabled():
        ids.append(readiness_alpha_merchant_id())
    return ids
