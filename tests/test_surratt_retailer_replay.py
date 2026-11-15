from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock

import pytest

from services import intake_identity
from services.catalog_enrichment_agent import surratt_retailer_apply as replay
from services.surratt_retailer_pilot import CANONICAL_CONTENT_KEY, CANONICAL_GROUP_ID, CANONICAL_PRODUCT_KEY, PROOF_KEY, RETAILERS


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [None, {"price_minor": 3500}, {"availability": "out_of_stock"}])
async def test_same_observation_replay_requires_exact_persisted_proof(monkeypatch, tamper):
    key, retailer = next(iter(RETAILERS.items()))
    captured = datetime.now(timezone.utc).isoformat()
    plan = {"pdps": [{"product_key": key}], "listing_spellings": {"status": "read"}, "offers": []}
    rows = []
    for native, (canonical, gtin) in retailer.variants.items():
        proof = {"version": 1, "canonical_product_key": CANONICAL_PRODUCT_KEY, "canonical_native_variant_id": canonical,
                 "retailer_native_variant_id": native, "gtin14": gtin, "observed_at": captured,
                 "availability": "in_stock", "price_minor": 3400,
                 "retailer_landing_url": f"{retailer.family_url}?variant={native}"}
        plan["offers"].append({"offer_id": native, "list_price": 34.0, "availability": "in_stock",
                               "offer_payload": json.dumps({PROOF_KEY: proof})})
        rows.append({"offer_id": native, "proof": dict(proof), "merchant_id": retailer.merchant_id,
                     "sku_seller": retailer.merchant_id, "product_seller": retailer.merchant_id,
                     "source_ref": retailer.family_url, "market": "US", "currency": "USD",
                     "offer_type": "retailer", "offer_mode": "redirect", "content_key": CANONICAL_CONTENT_KEY,
                     "product_group_id": CANONICAL_GROUP_ID, "suppressed_at": None, "suppression_reason": None,
                     "pivota_signature_id": None,
                     "source_variant_id": native, "barcode": gtin, "availability": "in_stock", "list_price": 34.0})
    if tamper:
        rows[0]["proof"].update(tamper)

    monkeypatch.setattr(intake_identity, "intake_identity_enabled", lambda _: True)
    monkeypatch.setattr(intake_identity, "variant_barcode_match_enabled", lambda: True)
    monkeypatch.setattr(replay.enrichment, "plan_with_current_listings", AsyncMock(return_value=plan))
    monkeypatch.setattr(replay, "scope_reviewed_plan", lambda value, *, cohort: value)
    monkeypatch.setattr(replay.enrichment, "find_legacy_retailer_listing_owners", AsyncMock(return_value=[]))
    write = AsyncMock()
    monkeypatch.setattr(replay.enrichment, "apply_ingest_plan", write)
    monkeypatch.setattr(replay, "canonical_witness", AsyncMock(return_value=[]))
    monkeypatch.setattr(replay, "retailer_witness", AsyncMock(return_value=rows))

    class DB:
        fetch_one = AsyncMock(side_effect=[{"row_json": "{}"}, {
            "merchant_id": retailer.merchant_id, "canonical_url": retailer.family_url,
            "content_key": CANONICAL_CONTENT_KEY, "suppressed_at": None, "suppression_reason": None,
            "pivota_signature_id": None,
        }])
        fetch_val = AsyncMock(return_value=False)

    if tamper:
        with pytest.raises(ValueError, match="retailer offer owner, native shade, group or refresh anchor changed"):
            await replay.apply_reviewed_retailer(DB(), {}, write=True)
    else:
        assert (await replay.apply_reviewed_retailer(DB(), {}, write=True))["replayed"] is True
    write.assert_not_awaited()
