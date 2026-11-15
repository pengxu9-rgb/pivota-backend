from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest

from services import intake_identity
from services.catalog_enrichment_agent import surratt_retailer_apply as replay
from services.surratt_retailer_pilot import (
    CANONICAL_CONTENT_KEY, CANONICAL_GROUP_ID, CANONICAL_PRODUCT_KEY,
    KIOKII_EXPANSION_FLAG, KIOKII_INITIAL_OFFER_IDS, KIOKII_INITIAL_RETAILER_COHORT, KIOKII_RETAILER_COHORT,
    PROOF_KEY, RETAILERS,
)


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


@pytest.fixture
def kiokii_expansion(monkeypatch):
    cohort = KIOKII_RETAILER_COHORT
    (key, retailer), = cohort.listings.items()
    now = datetime.now(timezone.utc)
    plan = {"pdps": [{"product_key": key}], "listing_spellings": {"status": "read"}, "offers": [], "skus": []}
    previous, additions = [], []
    offer_ids = [*sorted(KIOKII_INITIAL_OFFER_IDS), *(f"offer:addition-{i}" for i in range(4))]
    for (native, (canonical, gtin)), offer_id in zip(retailer.variants.items(), offer_ids, strict=True):
        proof = {"version": 1, "canonical_product_key": cohort.canonical_product_key,
                 "canonical_native_variant_id": canonical, "retailer_native_variant_id": native, "gtin14": gtin,
                 "observed_at": now.isoformat(), "availability": "in_stock", "price_minor": 1899,
                 "retailer_landing_url": f"{retailer.family_url}?variant={native}"}
        sku_key = f"sku:{native}"
        plan["offers"].append({"offer_id": offer_id, "sku_key": sku_key, "list_price": 18.99,
                               "availability": "in_stock", "offer_payload": json.dumps({PROOF_KEY: proof})})
        plan["skus"].append({"sku_key": sku_key})
        original = offer_id in KIOKII_INITIAL_OFFER_IDS
        saved_proof = {**proof, "observed_at": (now - timedelta(minutes=20)).isoformat(),
                       "price_minor": 1499} if original else proof
        row = {"offer_id": offer_id, "proof": saved_proof, "merchant_id": retailer.merchant_id,
               "sku_seller": retailer.merchant_id, "product_seller": retailer.merchant_id,
               "source_ref": retailer.family_url, "market": "US", "currency": "USD", "offer_type": "retailer",
               "offer_mode": "redirect", "content_key": cohort.canonical_content_key,
               "product_group_id": cohort.canonical_group_id, "suppressed_at": None, "suppression_reason": None,
               "pivota_signature_id": None, "source_variant_id": native, "barcode": gtin, "availability": "in_stock",
               "list_price": saved_proof["price_minor"] / 100, "offer_row": "unchanged", "sku_row": "unchanged"}
        (previous if original else additions).append(row)

    monkeypatch.setenv(KIOKII_EXPANSION_FLAG, "1")
    monkeypatch.setattr(intake_identity, "intake_identity_enabled", lambda _: True)
    monkeypatch.setattr(intake_identity, "variant_barcode_match_enabled", lambda: True)
    monkeypatch.setattr(replay.enrichment, "plan_with_current_listings", AsyncMock(return_value=plan))
    monkeypatch.setattr(replay, "scope_reviewed_plan", lambda value, *, cohort: value)
    monkeypatch.setattr(replay.enrichment, "find_legacy_retailer_listing_owners", AsyncMock(return_value=[]))
    monkeypatch.setattr(replay, "canonical_witness", AsyncMock(return_value=[{"canonical": "unchanged"}]))
    stock_apply = AsyncMock(return_value={"pdps": 1, "skus": 4, "offers": 4, "seeds": 0})
    monkeypatch.setattr(replay.enrichment, "apply_ingest_plan", stock_apply)
    witness = AsyncMock()
    monkeypatch.setattr(replay, "retailer_witness", witness)

    class DB:
        execute = AsyncMock()
        fetch_one = AsyncMock(side_effect=[{"row_json": "{}"}, {
            "merchant_id": retailer.merchant_id, "canonical_url": retailer.family_url,
            "content_key": cohort.canonical_content_key, "suppressed_at": None, "suppression_reason": None,
            "pivota_signature_id": None,
        }])
        fetch_val = AsyncMock(side_effect=["serializable", False, False, False, "{}"])

    return DB(), witness, stock_apply, previous, additions


@pytest.mark.asyncio
async def test_kiokii_exact_expansion_writes_only_four_additions(kiokii_expansion):
    db, witness, stock_apply, previous, additions = kiokii_expansion
    witness.side_effect = [previous, previous + additions]
    receipt = await replay.apply_reviewed_retailer(db, {}, write=True, cohort=KIOKII_RETAILER_COHORT, expand_kiokii=True)
    assert receipt["existing"] == 2 and not receipt["replayed"]
    write_plan = stock_apply.await_args.args[0]
    assert len(write_plan["offers"]) == len(write_plan["skus"]) == 4
    assert {o["offer_id"] for o in write_plan["offers"]} == {o["offer_id"] for o in additions}
    assert all(receipt["offers"][old["offer_id"]] == old["proof"] for old in previous)
    db.execute.assert_awaited_once_with("SELECT pg_advisory_xact_lock(1160308171, 2)")


@pytest.mark.asyncio
async def test_kiokii_expansion_replays_mixed_timestamp_complete_set(kiokii_expansion):
    db, witness, stock_apply, previous, additions = kiokii_expansion
    witness.return_value = previous + additions
    receipt = await replay.apply_reviewed_retailer(db, {}, write=True, cohort=KIOKII_RETAILER_COHORT, expand_kiokii=True)
    assert receipt["existing"] == 6 and receipt["replayed"]
    stock_apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_kiokii_expansion_refuses_partial_prior_state(kiokii_expansion):
    db, witness, stock_apply, previous, additions = kiokii_expansion
    witness.return_value = previous + additions[:1]
    with pytest.raises(ValueError, match="exactly the prior two or the complete six"):
        await replay.apply_reviewed_retailer(db, {}, write=True, cohort=KIOKII_RETAILER_COHORT, expand_kiokii=True)
    stock_apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_kiokii_expansion_rejects_changed_original_sku(kiokii_expansion):
    db, witness, _, previous, additions = kiokii_expansion
    witness.side_effect = [previous, [{**previous[0], "sku_row": "changed"}, previous[1], *additions]]
    with pytest.raises(ValueError, match="changed an original SKU or quote"):
        await replay.apply_reviewed_retailer(db, {}, write=True, cohort=KIOKII_RETAILER_COHORT, expand_kiokii=True)


@pytest.mark.asyncio
async def test_kiokii_expansion_requires_independent_opt_in(monkeypatch, kiokii_expansion):
    db, _, stock_apply, _, _ = kiokii_expansion
    monkeypatch.delenv(KIOKII_EXPANSION_FLAG)
    with pytest.raises(ValueError, match="requires its opt-in"):
        await replay.apply_reviewed_retailer(db, {}, write=True, cohort=KIOKII_RETAILER_COHORT, expand_kiokii=True)
    stock_apply.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_kiokii_refresh_selects_only_complete_persisted_cohorts(monkeypatch, kiokii_expansion):
    from scripts.ops import kiokii_us_retailer_offers as refresh

    db, _, _, previous, additions = kiokii_expansion
    monkeypatch.setattr(refresh, "retailer_witness", AsyncMock(side_effect=[previous, previous + additions,
                                                                         previous + additions[:1]]))
    assert await refresh._persisted_cohort(db) is KIOKII_INITIAL_RETAILER_COHORT
    assert await refresh._persisted_cohort(db) is KIOKII_RETAILER_COHORT
    with pytest.raises(ValueError, match="complete original two or expanded six"):
        await refresh._persisted_cohort(db)
