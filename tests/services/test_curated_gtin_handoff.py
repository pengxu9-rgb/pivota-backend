"""Captured retailer PDP -> pure plan -> mocked apply gate, without DB/network."""
import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import text

from services import curated_brand_feed as feed
from services.catalog_enrichment_agent import ingestion as ing
from services.catalog_enrichment_agent.apply import _PDP_UPSERT_SQL, _apply_pdp_identity_gate
from services import intake_identity as identity


def captured_records():
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" /
                          "retailer_lip_oil_public_observations.json").read_text())
    records = []
    for observed in fixture["observations"]:
        source = urlparse(observed["url"])
        # The observed .js prices are USD minor units; the bulk-feed mapper's
        # input contract uses major units. Currency was proved on each host.
        product = {
            **observed,
            "handle": source.path.rsplit("/", 1)[-1].removesuffix(".js"),
            "variants": [{**variant, "price": str(Decimal(variant["raw_minor_price"]) / 100)}
                         for variant in observed["variants"]],
        }
        records.append(feed.shopify_product_to_record(
            product, domain=source.hostname, category_path="beauty",
            currency=observed["currency"], source_role="retailer", emit_native_variants=True,
        ))
    return records


@pytest.mark.asyncio
async def test_captured_two_retailer_gtin_reaches_identity_gate_without_rekeying(monkeypatch):
    calls = []

    async def attach(**kwargs):
        calls.append(kwargs)
        return {"action": identity.ACTION_ATTACH, "content_key": "ck_fixture_shared_identity"}

    monkeypatch.setattr(identity, "resolve_or_attach_content_identity", attach)
    records = captured_records()
    assert {r["pdp"]["barcode"] for r in records} == {"8809530070499"}
    plan = ing.ingest_validated_jsonl(records)
    assert not calls  # Pure planning does not resolve against DB identity state.
    assert plan["skipped"] == 0
    assert {key: len(plan[key]) for key in ("pdps", "skus", "offers", "seeds")} == {
        "pdps": 2, "skus": 4, "offers": 4, "seeds": 2,
    }
    pdps = plan["pdps"]
    assert {r["gtin"] for r in pdps} == {"08809530070499"}
    assert {r["category_path"] for r in pdps} == {"beauty/makeup/lip/oil"}
    assert len({r["product_key"] for r in pdps}) == 2
    assert len({r["content_key"] for r in pdps}) == 2
    assert len({r["pivota_signature_id"] for r in pdps}) == 2
    assert {o["merchant_id"] for o in plan["offers"]} == {
        "agent_seed::retailer::asianbeautyessentials.com", "agent_seed::retailer::eyurs.com",
    }
    native = [r for r in plan["skus"] if "::v:" in r["sku_key"]]
    assert {r["source_variant_id"] for r in native} == {"43603819692287", "41807436316855"}
    assert len({r["merchant_id"] for r in native}) == 2
    assert {r["barcode"] for r in native} == {"8809530070499"}
    original_identity = [(r["product_key"], r["pivota_signature_id"], r["merchant_id"]) for r in pdps]
    for row in pdps:
        assert "barcode" not in row  # Input alias is not a SQL bind.
        # SQLAlchemy rejects unused row keys. The actual writer's existing SQL
        # must still accept the entire planned row, with no new column/schema.
        text(_PDP_UPSERT_SQL).bindparams(**row)
        assert await _apply_pdp_identity_gate(row, identity_gate_on=True)
    assert [call["gtin"] for call in calls] == ["08809530070499"] * 2
    assert {call["door"] for call in calls} == {identity.DOOR_CATALOG_ENRICHMENT}
    assert {call["merchant_ctx"]["source_domain"] for call in calls} == {
        "asianbeautyessentials.com", "eyurs.com",
    }
    assert {r["content_key"] for r in pdps} == {"ck_fixture_shared_identity"}
    assert [(r["product_key"], r["pivota_signature_id"], r["merchant_id"]) for r in pdps] == original_identity


@pytest.mark.asyncio
@pytest.mark.parametrize("source_gtin,expected", [
    (None, None), ("", None), ("00000000000000", None), ("8809530070498", None), ("7", None), ("not-a-barcode", None), ("123456789012345", None),
    ("8809530070499", "08809530070499"), ("08809530070499", "08809530070499"),
])
async def test_plan_uses_existing_gtin_contract_and_flag_off_preserves_it(monkeypatch, source_gtin, expected):
    record = captured_records()[0]
    record["pdp"]["barcode"] = source_gtin
    record["pdp"].pop("gtin", None)
    payload = ing._build_pdp_payload(record)
    row = ing._build_pdp_insert(pdp_payload=payload, offers=record["offers"], source_jsonl=None,
                                seller={"merchant_id": "fixture-retailer"})
    assert row["gtin"] == expected

    async def forbidden(**kwargs):
        raise AssertionError("Disabled gate must not resolve identity")

    monkeypatch.setattr(identity, "resolve_or_attach_content_identity", forbidden)
    before_key = row["content_key"]
    assert await _apply_pdp_identity_gate(row, identity_gate_on=False)
    assert row["gtin"] == expected
    assert row["content_key"] == before_key
