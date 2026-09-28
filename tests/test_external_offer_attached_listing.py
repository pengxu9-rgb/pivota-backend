"""An attached seed's re-read price reaches its listing's offer rows on the canonical.

The refresh projected a re-read seed only onto a MIRROR product (catalog_products.source_ref =
seed id). The enrichment agent's attached seeds have none, so every one ended `no_mirror_product`
(09-27: 2,220 of 2,220 origin reads) while the canonical kept the price captured at ingest, and
the PDP served it (PIVOTA-Agent #2215: seed S$28.80, offer S$28.20).

The offer rows below are shaped like the ones the producers actually write:
  * enrichment (`ingestion._build_offer_inserts`): source_ref = the listing URL, payload
    destination_url = the same, sku `<pk>::canonical`;
  * variant rows (`ingestion` / `scripts/backfill_variant_identity_skus`): sku `<pk>::v:<vid>`
    with `catalog_skus.source_variant_id` = the merchant's variant id;
  * the mirror (`upsert_catalog_offer_from_seed_row`): source_ref = the seed id.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from services import external_offer_dual_write as mod

PK = "ext:missha-pdrn-peel-shot::3595c15f"
DEST = "https://missha.us/products/pdrn-peel-shot"
SELLER = "agent_seed::missha"


def _seed(**over) -> Dict[str, Any]:
    seed = {
        "id": "seed:catalog_enrichment_agent_v1:2bed56bf52daa7d4",
        "external_product_id": "missha:2bed56bf52daa7d4",
        "attached_product_key": PK,
        "destination_url": DEST,
        "price_amount": 22.7,
        "price_currency": "USD",
        "market": "US",
        "seed_variants": json.dumps([
            {"variant_id": "47761881301179", "price_amount": 22.7},
            {"variant_id": "47761881301180", "price_amount": 38.0},
        ]),
        "snapshot_variants": None,
        "variant_refresh_status": "all_re_read",
    }
    seed.update(over)
    return seed


def _offer(offer_id, *, sku=None, vid=None, currency="USD", merchant=SELLER, source_ref=DEST,
           payload_dest=DEST, payload_seed=None, suppressed=False) -> Dict[str, Any]:
    return {
        "offer_id": offer_id,
        "sku_key": sku or f"{PK}::canonical",
        "merchant_id": merchant,
        "currency": currency,
        "source_ref": source_ref,
        "payload_destination_url": payload_dest,
        "payload_seed_id": payload_seed,
        "suppressed": suppressed,
        "source_variant_id": vid,
    }


def _plan(seed, offers):
    return mod.plan_attached_listing_offer_writes(seed, offers)


def test_the_product_level_row_takes_the_seed_price():
    plan = _plan(_seed(), [_offer("of_canon")])
    assert plan["status"] == "planned"
    assert plan["writes"] == [{"offer_id": "of_canon", "price": 22.7, "currency": "USD"}]


def test_another_sellers_listing_on_the_same_canonical_is_never_written():
    """A canonical carries every seller's offer. Only this listing's rows are the seed's."""
    other = _offer("of_ulta", merchant="agent_seed::retailer::ulta.com",
                   source_ref="https://www.ulta.com/p/pdrn", payload_dest="https://www.ulta.com/p/pdrn")
    plan = _plan(_seed(), [_offer("of_canon"), other])
    assert [w["offer_id"] for w in plan["writes"]] == ["of_canon"]


def test_same_seller_other_listing_is_not_this_listing():
    """One seller can list 30 ml and 50 ml on two URLs: seller identity alone is not the key."""
    sibling = _offer("of_50ml", source_ref=DEST + "-50ml", payload_dest=DEST + "-50ml")
    plan = _plan(_seed(), [sibling])
    assert plan["status"] == "no_listing_offer"


def test_the_listing_matches_on_the_served_url_rule_www_and_apex_alike():
    www = DEST.replace("https://", "https://www.")
    plan = _plan(_seed(), [_offer("of_canon", source_ref=www, payload_dest=www)])
    assert [w["offer_id"] for w in plan["writes"]] == ["of_canon"]


def test_the_mirror_and_backfill_shape_matches_on_the_seed_id():
    seed = _seed()
    offer = _offer("of_mirror", source_ref=seed["id"], payload_dest=None, payload_seed=seed["id"])
    assert [w["offer_id"] for w in _plan(seed, [offer])["writes"]] == ["of_mirror"]


def test_a_variant_row_takes_its_own_variant_price_never_the_products():
    offers = [
        _offer("of_canon"),
        _offer("of_v1", sku=f"{PK}::v:47761881301179", vid="47761881301179"),
        _offer("of_v2", sku=f"{PK}::v:47761881301180", vid="47761881301180"),
    ]
    writes = {w["offer_id"]: w["price"] for w in _plan(_seed(), offers)["writes"]}
    assert writes == {"of_canon": 22.7, "of_v1": 22.7, "of_v2": 38.0}


def test_variant_rows_wait_until_every_stored_variant_was_re_read():
    """A sibling the page did not list keeps its row and its clock; the product row still moves."""
    offers = [_offer("of_canon"), _offer("of_v2", sku=f"{PK}::v:47761881301180", vid="47761881301180")]
    plan = _plan(_seed(variant_refresh_status="not_all_re_read"), offers)
    assert [w["offer_id"] for w in plan["writes"]] == ["of_canon"]
    assert plan["skips"] == {"variant_not_re_read": 1}


def test_a_variant_the_seed_does_not_hold_is_skipped():
    offers = [_offer("of_v9", sku=f"{PK}::v:999", vid="999")]
    plan = _plan(_seed(), offers)
    assert plan["writes"] == [] and plan["skips"] == {"variant_not_on_seed": 1}


def test_the_served_variant_list_falls_back_to_the_snapshot():
    seed = _seed(seed_variants=None, snapshot_variants=[{"id": "47761881301180", "price": "41.00"}])
    offers = [_offer("of_v2", sku=f"{PK}::v:47761881301180", vid="47761881301180")]
    assert _plan(seed, offers)["writes"] == [{"offer_id": "of_v2", "price": 41.0, "currency": "USD"}]


def test_a_row_in_another_currency_is_refused_not_converted():
    """Measured 2026-09-28: 17 served product-level rows hold GBP/CAD against a USD seed read."""
    plan = _plan(_seed(), [_offer("of_gb", currency="GBP")])
    assert plan["writes"] == [] and plan["skips"] == {"currency_mismatch": 1}


def test_the_sg_row_takes_an_sgd_read_in_its_own_currency():
    """#2215's row: SGD offer, SGD seed. The number moves; currency and market are not written."""
    plan = _plan(_seed(price_amount=28.8, price_currency="sgd", seed_variants=None),
                 [_offer("of_jsm", currency="SGD")])
    assert plan["writes"] == [{"offer_id": "of_jsm", "price": 28.8, "currency": "SGD"}]


def test_a_seed_with_no_currency_writes_nothing():
    plan = _plan(_seed(price_currency=None), [_offer("of_canon")])
    assert plan["writes"] == [] and plan["skips"] == {"currency_mismatch": 1}


@pytest.mark.parametrize("price", [None, 0, -1, "n/a"])
def test_no_positive_seed_price_writes_nothing(price):
    plan = _plan(_seed(price_amount=price), [_offer("of_canon")])
    assert plan["writes"] == [] and plan["skips"] == {"no_seed_price": 1}


def test_suppressed_listing_rows_are_structural_not_written():
    plan = _plan(_seed(), [_offer("of_canon", suppressed=True)])
    assert plan["status"] == "listing_offer_suppressed" and plan["writes"] == []
    assert "listing_offer_suppressed" in mod.OFFER_SYNC_STRUCTURAL_SKIP_STATUSES


def test_a_listing_whose_rows_disagree_on_the_seller_writes_nothing():
    offers = [_offer("of_a"), _offer("of_b", sku=f"{PK}::v:47761881301179", vid="47761881301179",
                                      merchant="agent_seed::someone-else")]
    assert _plan(_seed(), offers)["status"] == "ambiguous_listing_seller"


def test_the_banned_bucket_is_never_a_seller_we_write_under():
    assert _plan(_seed(), [_offer("of_x", merchant="external_seed")])["status"] == "ambiguous_listing_seller"


# ---------------------------------------------------------------- sync_offer_for_seed, end to end


class FakeDB:
    """The reads sync_offer_for_seed makes, routed on SQL substrings, plus the guarded UPDATE."""

    def __init__(self, *, seed, mirror=None, offers=(), guard_matches=True):
        self.seed, self.mirror, self.offers = seed, mirror, list(offers)
        self.guard_matches = guard_matches
        self.updates: List[Dict[str, Any]] = []
        self.executed: List[str] = []

    async def fetch_one(self, sql, params=None):
        s = str(sql)
        if "FROM external_product_seeds" in s:
            return dict(self.seed) if self.seed else None
        if "FROM catalog_products" in s:
            return dict(self.mirror) if self.mirror else None
        if s.strip().startswith("UPDATE catalog_offers"):
            self.updates.append(dict(params))
            return {"offer_id": params["offer_id"]} if self.guard_matches else None
        raise AssertionError(f"unexpected fetch_one: {s[:80]}")

    async def fetch_all(self, sql, params=None):
        assert "FROM catalog_offers" in str(sql) and params == {"product_key": PK}
        return [dict(o) for o in self.offers]

    async def execute(self, sql, params=None):
        self.executed.append(str(sql))


def _sync(monkeypatch, fake, *, vouched=True) -> Dict[str, Any]:
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    monkeypatch.setattr(mod, "database", fake)
    return asyncio.run(mod.sync_offer_for_seed(fake.seed["id"], project_attached_listing=vouched))


def test_a_caller_that_did_not_re_read_the_price_never_stamps_the_listing(monkeypatch):
    """seed_data_writer's merge and the mirror reconciler rewrite a seed without reading its
    price. `updated_at = NOW()` on the canonical's row would claim a read nobody made."""
    fake = FakeDB(seed=_seed(), offers=[_offer("of_canon")])
    assert _sync(monkeypatch, fake, vouched=False)["status"] == "no_mirror_product"
    assert fake.updates == []


def test_the_refresh_hook_vouches_for_the_price(monkeypatch):
    import routes.employee_products as ep

    seen = {}

    async def fake_sync(seed_id, **kwargs):
        seen.update(kwargs)
        return {"status": "synced", "target": "attached"}

    async def fake_pdp(**kwargs):
        return "refreshed"

    monkeypatch.setattr("services.external_offer_dual_write.dual_write_enabled", lambda: True)
    monkeypatch.setattr("services.external_offer_dual_write.sync_offer_for_seed", fake_sync)
    monkeypatch.setattr("services.seed_data_writer.refresh_agent_pdp_view_for_seed", fake_pdp)
    asyncio.run(ep._project_refreshed_seed_to_serving_surfaces("eps_1"))
    assert seen == {"project_attached_listing": True}


def test_an_attached_seed_without_a_mirror_writes_its_listing_row(monkeypatch):
    fake = FakeDB(seed=_seed(), offers=[_offer("of_canon"), _offer("of_ulta", merchant="u",
                  source_ref="https://www.ulta.com/p", payload_dest="https://www.ulta.com/p")])
    result = _sync(monkeypatch, fake)
    assert result["status"] == "synced" and result["target"] == "attached"
    assert result["status"] in mod.OFFER_SYNC_WRITTEN_STATUSES
    assert fake.updates == [{"offer_id": "of_canon", "price": 22.7, "currency": "USD"}]
    assert fake.executed == [], "the attached lane never upserts: it only prices existing rows"


def test_the_mirror_still_wins_when_the_seed_has_one(monkeypatch):
    fake = FakeDB(seed=_seed(), mirror={"product_key": "prod::merch_obs_x::external_seed::e",
                                        "merchant_id": "merch_obs_x"}, offers=[_offer("of_canon")])
    result = _sync(monkeypatch, fake)
    assert result["status"] == "synced" and result["target"] == "mirror"
    assert fake.updates == [] and len(fake.executed) == 1


def test_an_unattached_seed_without_a_mirror_is_still_no_mirror_product(monkeypatch):
    fake = FakeDB(seed=_seed(attached_product_key=None))
    assert _sync(monkeypatch, fake)["status"] == "no_mirror_product"


def test_no_listing_row_is_structural(monkeypatch):
    fake = FakeDB(seed=_seed(), offers=[])
    result = _sync(monkeypatch, fake)
    assert result["status"] == "no_listing_offer"
    assert result["status"] in mod.OFFER_SYNC_STRUCTURAL_SKIP_STATUSES


def test_rows_refused_row_by_row_are_not_written_and_say_why(monkeypatch):
    fake = FakeDB(seed=_seed(), offers=[_offer("of_gb", currency="GBP")])
    result = _sync(monkeypatch, fake)
    assert result["status"] == "listing_offer_not_written"
    assert result["status"] not in mod.OFFER_SYNC_STRUCTURAL_SKIP_STATUSES
    assert result["offer_skips"] == {"currency_mismatch": 1}
    assert fake.updates == []


def test_a_row_that_changed_since_the_read_is_not_counted_as_written(monkeypatch):
    fake = FakeDB(seed=_seed(), offers=[_offer("of_canon")], guard_matches=False)
    result = _sync(monkeypatch, fake)
    assert result["status"] == "listing_offer_not_written"
    assert result["offer_skips"] == {"changed_since_read": 1}


def test_the_refresh_hook_reports_the_attached_write_and_row_skips(monkeypatch):
    """Through the real helper the refresh calls, so the counters are the ones the batch sums."""
    import routes.employee_products as ep

    async def fake_sync(seed_id, **kwargs):
        return {"seed_id": seed_id, "status": "synced", "target": "attached",
                "offers_written": 1, "offer_skips": {"variant_not_re_read": 2}}

    async def fake_pdp(**kwargs):
        return "refreshed"

    monkeypatch.setattr("services.external_offer_dual_write.dual_write_enabled", lambda: True)
    monkeypatch.setattr("services.external_offer_dual_write.sync_offer_for_seed", fake_sync)
    monkeypatch.setattr("services.seed_data_writer.refresh_agent_pdp_view_for_seed", fake_pdp)
    counts = asyncio.run(ep._project_refreshed_seed_to_serving_surfaces("eps_1"))
    assert counts["projected"] == 1 and counts["wrote_attached"] == 1
    assert counts["offer_skip_variant_not_re_read"] == 2
