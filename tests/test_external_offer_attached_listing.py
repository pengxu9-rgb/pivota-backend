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
           payload_dest=DEST, payload_seed=None, suppressed=False, price=25.0) -> Dict[str, Any]:
    return {
        "list_price": price,
        "merchant_effective_price": price,
        "offer_id": offer_id,
        "sku_key": sku or f"{PK}::canonical",
        "merchant_id": merchant,
        "currency": currency,
        "source_ref": source_ref,
        "payload_destination_url": payload_dest,
        "payload_seed_id": payload_seed,
        "suppressed": suppressed,
        "source_variant_id": vid,
        "read_offer": "{}", "read_sku": "{}", "read_product": "{}",
    }


def _plan(seed, offers, *, source="refresh", currency_read=True):
    return mod.plan_attached_listing_offer_writes(
        seed, offers, source=source, currency_read=currency_read, max_ratio=3.0
    )


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


def _sync(monkeypatch, fake, *, source="refresh", currency_read=True) -> Dict[str, Any]:
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    monkeypatch.setattr(mod, "database", fake)
    return asyncio.run(mod.sync_offer_for_seed(
        fake.seed["id"], attached_price_source=source, currency_read=currency_read
    ))


def test_a_caller_that_did_not_re_read_the_price_never_stamps_the_listing(monkeypatch):
    """seed_data_writer's merge and the mirror reconciler rewrite a seed without reading its
    price. `updated_at = NOW()` on the canonical's row would claim a read nobody made."""
    fake = FakeDB(seed=_seed(), offers=[_offer("of_canon")])
    assert _sync(monkeypatch, fake, source=None)["status"] == "no_mirror_product"
    assert _sync(monkeypatch, fake, source="seed_data_merge")["status"] == "no_mirror_product"
    assert fake.updates == []


def test_the_refresh_hook_passes_the_price_source_through(monkeypatch):
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
    asyncio.run(ep._project_refreshed_seed_to_serving_surfaces(
        "eps_1", price_source="refresh", currency_read=True))
    assert seen == {"attached_price_source": "refresh", "currency_read": True}
    seen.clear()
    asyncio.run(ep._project_refreshed_seed_to_serving_surfaces("eps_1"))
    assert seen == {"attached_price_source": None, "currency_read": False}, (
        "a caller that names no price source must not open the attached lane")


def test_an_attached_seed_without_a_mirror_writes_its_listing_row(monkeypatch):
    fake = FakeDB(seed=_seed(), offers=[_offer("of_canon"), _offer("of_ulta", merchant="u",
                  source_ref="https://www.ulta.com/p", payload_dest="https://www.ulta.com/p")])
    result = _sync(monkeypatch, fake)
    assert result["status"] == "synced" and result["target"] == "attached"
    assert result["status"] in mod.OFFER_SYNC_WRITTEN_STATUSES
    assert fake.updates == [{"offer_id": "of_canon", "price": 22.7, "currency": "USD",
                             "read_offer": "{}", "read_sku": "{}", "read_product": "{}"}]
    assert fake.executed == [], "the attached lane never upserts: it only prices existing rows"


def test_the_mirror_still_wins_when_the_seed_has_one(monkeypatch):
    from unittest.mock import AsyncMock
    from services import catalog_variant_offer_projection

    project = AsyncMock(return_value={"planned": 0, "inserted": 0, "skips": {}})
    monkeypatch.setattr(catalog_variant_offer_projection, "project_missing_variant_offers", project)
    fake = FakeDB(seed=_seed(), mirror={"product_key": "prod::merch_obs_x::external_seed::e",
                                        "merchant_id": "merch_obs_x"}, offers=[_offer("of_canon")])
    result = _sync(monkeypatch, fake)
    assert result["status"] == "synced" and result["target"] == "mirror"
    assert fake.updates == [] and len(fake.executed) == 1
    project.assert_awaited_once_with(fake.mirror["product_key"], apply=True, db=fake)


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


# ------------------------------------------------------------------ price sanity (controller review)


def test_a_refresh_read_the_currency_of_which_was_defaulted_writes_nothing():
    """`resolve_external_offer` stores the market's currency when the page names none."""
    plan = _plan(_seed(), [_offer("of_canon")], currency_read=False)
    assert plan["status"] == "currency_not_read" and plan["writes"] == []
    assert "currency_not_read" not in mod.OFFER_SYNC_STRUCTURAL_SKIP_STATUSES


def test_a_comma_decimal_misread_is_refused_and_reported_not_written():
    """`_parse_price` keeps digits and dots: a page's "28,80" arrives as 2880."""
    plan = _plan(_seed(price_amount=2880.0, seed_variants=None), [_offer("of_canon", price=28.8)])
    assert plan["writes"] == [] and plan["skips"] == {"price_ratio_out_of_bounds": 1}
    assert plan["refused"] == [{"offer_id": "of_canon", "sku_key": f"{PK}::canonical",
                                "current": 28.8, "read": 2880.0, "currency": "USD"}]


@pytest.mark.parametrize("current,read,ok", [
    (30.0, 90.0, True), (30.0, 10.0, True),      # the bound is inclusive
    (30.0, 90.5, False), (30.0, 9.9, False),
    (None, 500.0, True),                          # nothing to compare against: first price
])
def test_the_ratio_bound(current, read, ok):
    offer = _offer("of_canon", price=current)
    plan = _plan(_seed(price_amount=read, seed_variants=None), [offer])
    assert bool(plan["writes"]) is ok


def test_the_ratio_bound_applies_to_variant_rows_too():
    offers = [_offer("of_v2", sku=f"{PK}::v:47761881301180", vid="47761881301180", price=3.8)]
    plan = _plan(_seed(), offers)
    assert plan["writes"] == [] and plan["skips"] == {"price_ratio_out_of_bounds": 1}


def test_an_employee_edit_is_not_bounded_and_moves_only_the_product_row():
    """Correcting a 100x row is exactly what the edit is for; the seed's variants are the last
    refresh's, not the employee's claim."""
    offers = [_offer("of_canon", price=2880.0),
              _offer("of_v1", sku=f"{PK}::v:47761881301179", vid="47761881301179")]
    plan = _plan(_seed(price_amount=28.8), offers, source="employee_edit", currency_read=False)
    assert plan["writes"] == [{"offer_id": "of_canon", "price": 28.8, "currency": "USD"}]
    assert plan["skips"] == {"variant_not_edited": 1}


def test_refused_rows_are_logged_for_review(monkeypatch, caplog):
    import logging

    fake = FakeDB(seed=_seed(price_amount=2880.0, seed_variants=None),
                  offers=[_offer("of_canon", price=28.8)])
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        result = _sync(monkeypatch, fake)
    assert result["status"] == "listing_offer_not_written"
    assert result["offer_skips"] == {"price_ratio_out_of_bounds": 1}
    assert fake.updates == []
    assert any("price_ratio_out_of_bounds" in str(r.msg) and "of_canon" in str(r.msg)
               for r in caplog.records)


def test_the_ratio_env_knob_refuses_nonsense(monkeypatch):
    monkeypatch.setenv("EXTERNAL_OFFER_PROJECTION_MAX_PRICE_RATIO", "5")
    assert mod.max_price_ratio() == 5.0
    for bad in ("0.5", "1", "abc"):
        monkeypatch.setenv("EXTERNAL_OFFER_PROJECTION_MAX_PRICE_RATIO", bad)
        assert mod.max_price_ratio() == 3.0

@pytest.mark.parametrize('variants,reason', [
    ([{'variant_id':'47761881301180','price_amount':38,'price_currency':'EUR'}], 'variant_currency_mismatch'),
    ([{'variant_id':'47761881301180','price_amount':38,'price_currency':'USD','currency':'EUR'}], 'variant_currency_mismatch'),
    ([{'variant_id':'47761881301180','price_amount':38,'price':39}], 'variant_price_alias_conflict'),
    ([{'variant_id':'47761881301180','price_amount':38}, {'variant_id':'47761881301180','price_amount':38}], 'ambiguous_variant_identity'),
    ([{'variant_id':'47761881301180','id':'47761881301181','price_amount':38}], 'variant_identity_conflict'),
    ([{'variant_id':'47761881301180','price_amount':float('inf')}], 'no_variant_price'),
])
def test_contradictory_variant_evidence_never_prices_an_offer(variants,reason):
    offer = _offer('of_v2',sku=f'{PK}::v:47761881301180',vid='47761881301180')
    plan = _plan(_seed(seed_variants=variants),[offer])
    assert plan['writes'] == [] and plan['skips'] == {reason:1}

@pytest.mark.parametrize('price',[float('inf'),float('nan'),1e11,0.0001])
def test_unrepresentable_seed_money_is_never_stamped_fresh(price):
    plan = _plan(_seed(price_amount=price),[_offer('of_canon')])
    assert plan['writes'] == [] and plan['skips'] == {'no_seed_price':1}

@pytest.mark.parametrize('source',['refresh','employee_edit'])
def test_listing_projection_cannot_supersede_reviewed_native_money(source):
    offer = _offer('of_canon'); offer['price_repaired'] = True
    plan = _plan(_seed(),[offer],source=source)
    assert plan['writes'] == [] and plan['skips'] == {'reviewed_price_repair':1}

def test_an_offer_whose_sku_currency_disagrees_is_not_repriced():
    offer = _offer('of_canon'); offer['sku_currency'] = 'EUR'
    plan = _plan(_seed(),[offer])
    assert plan['writes'] == [] and plan['skips'] == {'sku_currency_mismatch':1}

@pytest.mark.parametrize('raw',['inf','nan','-inf'])
def test_nonfinite_projection_ratio_uses_the_conservative_default(monkeypatch,raw):
    monkeypatch.setenv('EXTERNAL_OFFER_PROJECTION_MAX_PRICE_RATIO',raw)
    assert mod.max_price_ratio() == 3

@pytest.mark.parametrize('change',[
    {'payload_dest':DEST+'-other','source_ref':None},
    {'source_ref':DEST+'-other','payload_dest':DEST},
    {'payload_seed':'another-seed'},
])
def test_seed_identity_does_not_override_a_conflicting_listing(change):
    seed=_seed()
    offer=_offer('old-listing',payload_seed=seed['id'])
    offer.update({'payload_destination_url' if k=='payload_dest' else 'payload_seed_id' if k=='payload_seed' else k:v
                  for k,v in change.items()})
    assert _plan(seed,[offer])['status'] == 'no_listing_offer'


def test_equivalent_numeric_and_gid_aliases_name_the_same_variant():
    seed=_seed(seed_variants=[{'variant_id':'47761881301180',
                             'id':'gid://shopify/ProductVariant/47761881301180',
                             'price_amount':38,'price_currency':'USD'}])
    offer=_offer('of_v2',sku=f'{PK}::v:47761881301180',vid='47761881301180')
    assert _plan(seed,[offer])['writes'] == [{'offer_id':'of_v2','price':38,'currency':'USD'}]

@pytest.mark.parametrize('source_ref',['HTTPS://missha.us/products/other','  https://missha.us/products/other  '])
def test_url_formatting_cannot_hide_a_conflicting_listing_claim(source_ref):
    seed=_seed();offer=_offer('other',source_ref=source_ref,payload_dest=None,payload_seed=seed['id'])
    assert _plan(seed,[offer])['status'] == 'no_listing_offer'
