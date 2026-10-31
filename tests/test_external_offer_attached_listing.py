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
    """No currency the seed can vouch for: refused before any row is planned, and counted with the
    mirror lane's refusals (`refused_currency` is a structural skip)."""
    plan = _plan(_seed(price_currency=None), [_offer("of_canon")])
    assert (plan["status"], plan["writes"]) == ("refused_currency", [])


def _projected(offer_id, source_system="variant_identity_backfill_v1", **kw):
    row = _offer(offer_id, **kw)
    row["source_system"] = source_system
    return row


# skintific.com's shape on the sync path: column USD, page read AUD (projected), and the readers'
# own answer, projected: the seed states none.
_DISAGREEING = {"price_currency": "USD", "snapshot_price_currency": "AUD", "seed_currency": None}


@pytest.mark.parametrize("source_system", [
    "external_product_seeds_mirror_v1", "variant_identity_backfill_v1", "variant_offer_projection_v1",
])
def test_rows_the_readers_withhold_are_not_repriced(source_system):
    """PR controller review of #2657 (H1): a seed-projected row of a seed the readers cannot price was
    re-priced and stamped fresh every night. Every live row is such a row here, so the seed is refused."""
    plan = _plan(_seed(**_DISAGREEING), [_projected("of_canon", source_system)])
    assert (plan["status"], plan["writes"], plan["skips"]) == ("refused_currency", [], {"refused_currency": 1})


@pytest.mark.parametrize("source_system", ["catalog_enrichment_agent_v1", "variant_identity_backfill_v1", None])
def test_a_read_reprices_no_row_of_a_seed_with_no_currency(source_system):
    """Peng 2026-10-22, after #2657 (M2): a READ of a seed whose currency nobody can state (us.oiolab.co:
    column USD, page EUR) re-prices no row, whoever wrote it."""
    plan = _plan(_seed(**_DISAGREEING), [_projected("of_canon", source_system)], source="refresh")
    assert (plan["status"], plan["writes"], plan["skips"]) == ("refused_currency", [], {"refused_currency": 1})


def test_an_employee_edit_reprices_the_rows_another_writer_owns():
    """Peng 2026-10-22: an explicit employee edit is a person vouching for the column's currency and
    amount, so it re-prices the listing's enrichment row as on main; the seed-projected row stays refused
    (#2657). The same seed under a refresh writes nothing (above)."""
    rows = [_projected("of_canon", "catalog_enrichment_agent_v1"),
            _projected("of_copy", sku=f"{PK}::canonical")]
    plan = _plan(_seed(**_DISAGREEING), rows, source="employee_edit", currency_read=False)
    assert plan["status"] == "planned"
    assert plan["writes"] == [{"offer_id": "of_canon", "price": 22.7, "currency": "USD"}]
    assert plan["skips"] == {"refused_currency": 1}


def test_an_employee_edit_never_reprices_seed_projected_rows():
    plan = _plan(_seed(**_DISAGREEING), [_projected("of_copy")], source="employee_edit", currency_read=False)
    assert (plan["status"], plan["writes"], plan["skips"]) == ("refused_currency", [], {"refused_currency": 1})


def test_the_refusal_counts_live_rows_only():
    """A suppressed row of the listing is not one the lane would have written, so it is not counted
    as refused (PR controller review of #2665, M10)."""
    rows = [_projected("of_canon", "catalog_enrichment_agent_v1"),
            _offer("of_gone", sku=f"{PK}::v:47761881301179", vid="47761881301179", suppressed=True)]
    plan = _plan(_seed(**_DISAGREEING), rows)
    assert (plan["status"], plan["skips"]) == ("refused_currency", {"refused_currency": 1})


def test_a_mixed_listing_is_refused_whole_by_a_read():
    rows = [_projected("of_canon", "catalog_enrichment_agent_v1"),
            _projected("of_copy", sku=f"{PK}::canonical")]
    plan = _plan(_seed(**_DISAGREEING), rows, source="refresh")
    assert (plan["status"], plan["writes"], plan["skips"]) == ("refused_currency", [], {"refused_currency": 2})


def test_an_agreeing_seed_writes_its_projected_rows():
    plan = _plan(_seed(price_currency="USD", snapshot_price_currency="USD", seed_currency="USD"),
                 [_projected("of_canon")])
    assert plan["writes"] == [{"offer_id": "of_canon", "price": 22.7, "currency": "USD"}]


def test_a_blank_column_writes_nothing_even_with_a_page_read():
    """M1 of the #2657 review: main required a column currency, and a lone page read must not start
    writing seeds this lane never wrote."""
    for row in (_offer("of_canon", currency="AUD"), _projected("of_canon", currency="AUD")):
        plan = _plan(_seed(price_currency=None, snapshot_price_currency="AUD", seed_currency="AUD", price_amount=56), [row])
        assert (plan["status"], plan["writes"]) == ("refused_currency", [])


def test_a_row_currency_is_read_as_the_readers_read_it():
    """'USD\t' is not 'USD' to SQL's trim() (spaces only), so the row is not this seed's currency."""
    plan = _plan(_seed(), [_offer("of_canon", currency="USD\t")])
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

    def __init__(self, *, seed, mirror=None, offers=(), mirror_offers=(), guard_matches=True):
        self.seed, self.mirror, self.offers = seed, mirror, list(offers)
        self.mirror_offers = list(mirror_offers)
        self.reads: List[str] = []
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
        assert "FROM catalog_offers" in str(sql)
        self.reads.append(params["product_key"])
        if self.mirror and params == {"product_key": self.mirror["product_key"]}:
            return [dict(o) for o in self.mirror_offers]
        assert params == {"product_key": PK}
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
                             "read_offer": "{}", "read_sku": "{}", "read_product": "{}",
                             # who vouched for the read (offer_payload.price_read_source)
                             "read_source": "refresh"}]
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
    # The mirror has no variant row: its product-level row is the mirror upsert's alone.
    assert result["variant_status"] == "no_variant_offer"
    assert fake.updates == [] and len(fake.executed) == 1
    assert fake.reads == [fake.mirror["product_key"]], "never the attached canonical's rows"
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


def test_a_column_with_a_trailing_newline_no_longer_writes_an_enrichment_row():
    """PR controller review of #2657 (B6). Main compared `.strip()`-ed codes, so a column of 'USD\\n' re-priced a USD row. The lane now
    reads a code as SQL's trim() does (spaces only), as the readers do: 'USD\\n' is not 'USD', and
    the row is a currency_mismatch, the withholding direction."""
    plan = _plan(_seed(price_currency="USD\n"), [_offer("of_canon")])
    assert (plan["status"], plan["writes"], plan["skips"]) == ("planned", [], {"currency_mismatch": 1})


def test_an_employee_edits_refused_rows_are_logged(monkeypatch, caplog):
    import logging

    fake = FakeDB(seed=_seed(**_DISAGREEING),
                  offers=[_projected("of_canon", "catalog_enrichment_agent_v1"),
                          _projected("of_copy", sku=f"{PK}::canonical")])
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        result = _sync(monkeypatch, fake, source="employee_edit", currency_read=False)
    assert (result["status"], result["offers_written"], result["offer_skips"]) == ("synced", 1, {"refused_currency": 1})
    assert [u["offer_id"] for u in fake.updates] == ["of_canon"]
    assert any("refused_currency" in str(r.msg) and "'rows_refused': 1" in str(r.msg) for r in caplog.records)


def test_a_refused_seed_is_logged_and_stamps_nothing(monkeypatch, caplog):
    """Through the real sync: a mixed listing of a seed with no currency of its own writes no row,
    enrichment row included, and the refusal gets its warning line."""
    import logging

    fake = FakeDB(seed=_seed(**_DISAGREEING),
                  offers=[_projected("of_canon", "catalog_enrichment_agent_v1"),
                          _projected("of_copy", sku=f"{PK}::canonical")])
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        result = _sync(monkeypatch, fake)
    assert result["status"] == "refused_currency"
    assert result["status"] in mod.OFFER_SYNC_STRUCTURAL_SKIP_STATUSES
    assert fake.updates == []
    assert any("external_offer_attached_refused" in str(r.msg) and "refused_currency" in str(r.msg)
               and "'rows_refused': 2" in str(r.msg) for r in caplog.records)


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


def test_the_lane_refuses_an_unknown_price_source():
    """jsonb_set is strict: a NULL or unknown marker must never reach the write (the check runs before any read)."""
    for bad in (None, "", "crawl", "EMPLOYEE_EDIT"):
        with pytest.raises(ValueError):
            asyncio.run(mod.sync_attached_listing_offers({"id": "s", "attached_product_key": "pk"}, source=bad))


# ------------------------------------------------------------ the mirror's variant rows (2026-10-31)
#
# A seed WITH a mirror product took only the mirror's product-level row; the variant rows on the
# same mirror (the variant backfill's, the projection's, the mirror's older per-variant rows) were
# never re-priced, and the PDP prices a variant from those. The real SQL is pinned in
# test_external_offer_attached_listing_postgres.py; these pin the routing and the reporting.

MIRROR = {"product_key": "prod::external_seed::external_seed::ext_74e14bd6", "merchant_id": "merch_obs_bb92"}
MPK = MIRROR["product_key"]


def _mirror_offer(offer_id, *, sku, vid, price=13.6, source_ref=DEST, payload_seed=None, source_product_id="ext_74e14bd6",
                  merchant=None, market="US"):
    return {**_offer(offer_id, sku=sku, vid=vid, merchant=merchant or MIRROR["merchant_id"], source_ref=source_ref,
                     payload_seed=payload_seed, price=price),
            "market": market, "read_product": json.dumps({"source_product_id": source_product_id})}


def _mirror_fake(**over):
    from unittest.mock import AsyncMock
    offers = over.pop("mirror_offers", [
        _mirror_offer("of_mirror_canon", sku=f"{MPK}::canonical", vid=MPK, source_ref="seed:x"),
        _mirror_offer("of_backfill", sku=f"{MPK}::v:47761881301179", vid="47761881301179"),
    ])
    return FakeDB(seed=_seed(**over), mirror=MIRROR, mirror_offers=offers), AsyncMock(
        return_value={"planned": 0, "inserted": 0, "skips": {}})


def _patch_projection(monkeypatch, project):
    from services import catalog_variant_offer_projection
    monkeypatch.setattr(catalog_variant_offer_projection, "project_missing_variant_offers", project)


def test_a_refresh_reprices_the_mirrors_variant_rows_beside_its_own_row(monkeypatch):
    fake, project = _mirror_fake()
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake)
    assert result["status"] == "synced" and result["target"] == "mirror"
    assert result["variant_status"] == "synced" and result["variant_offers_written"] == 1
    assert len(fake.executed) == 1, "the product-level row is still the mirror upsert's"
    assert [(u["offer_id"], u["price"], u["read_source"]) for u in fake.updates] == [("of_backfill", 22.7, "refresh")]


@pytest.mark.parametrize("source", [None, "employee_edit", "seed_data_merge"])
def test_only_a_refresh_reprices_the_mirrors_variant_rows(monkeypatch, source):
    """An employee edit vouches for the product's price, not for the variants the last refresh left."""
    fake, project = _mirror_fake()
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake, source=source, currency_read=False)
    assert result["status"] == "synced" and "variant_status" not in result
    assert fake.updates == [] and fake.reads == []


@pytest.mark.parametrize("spelling", ["0", "false", "off", "no", " OFF "])
def test_the_kill_switch_leaves_the_mirror_as_it_was(monkeypatch, spelling):
    fake, project = _mirror_fake()
    _patch_projection(monkeypatch, project)
    monkeypatch.setenv(mod.MIRROR_VARIANT_REPRICE_ENV, spelling)
    result = _sync(monkeypatch, fake)
    assert result["status"] == "synced" and "variant_status" not in result and fake.updates == []


def test_the_mirror_variant_rows_keep_the_attached_lanes_rules(monkeypatch):
    """Not re-read: the variant row keeps its price, and the seed still reports its mirror write."""
    fake, project = _mirror_fake(variant_refresh_status="not_all_re_read")
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake)
    assert result["status"] == "synced"
    assert result["variant_status"] == "listing_offer_not_written"
    # Its own bucket: never summed with the attached lane's `variant_not_re_read`.
    assert result["offer_skips"] == {"mirror_variants_variant_not_re_read": 1} and fake.updates == []


def test_a_failing_variant_half_never_unwrites_the_mirror(monkeypatch):
    fake, project = _mirror_fake()
    _patch_projection(monkeypatch, project)

    async def boom(*args, **kwargs):
        raise RuntimeError("variant read failed")

    monkeypatch.setattr(mod, "sync_mirror_variant_offers", boom)
    result = _sync(monkeypatch, fake)
    assert result["status"] == "synced" and result["variant_status"] == "error"


def test_the_mirrors_prefixed_variant_id_is_its_native_id():
    row = _mirror_offer("of_old", sku="ext_74e14bd6:47761881301180", vid="ext_74e14bd6:47761881301180")
    assert mod._native_variant_row(row)["source_variant_id"] == "47761881301180"


@pytest.mark.parametrize("vid", [
    "ext_OTHER:47761881301180",   # another product's prefix
    "ext_74e14bd6:123",           # not a merchant-issued id
    "ext_74e14bd6:47761881301180:x",
    "47761881301180",             # already native
])
def test_only_this_products_prefix_onto_a_native_id_is_stripped(vid):
    row = _mirror_offer("of_old", sku="s", vid=vid)
    assert mod._native_variant_row(row)["source_variant_id"] == vid


def test_a_prefixed_mirror_variant_row_takes_its_variants_price(monkeypatch):
    fake, project = _mirror_fake(mirror_offers=[
        _mirror_offer("of_old", sku="ext_74e14bd6:47761881301180", vid="ext_74e14bd6:47761881301180",
                      source_ref="seed:catalog_enrichment_agent_v1:2bed56bf52daa7d4",
                      payload_seed="seed:catalog_enrichment_agent_v1:2bed56bf52daa7d4"),
    ])
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake)
    assert result["variant_status"] == "synced"
    assert [(u["offer_id"], u["price"]) for u in fake.updates] == [("of_old", 38.0)]


def test_the_refresh_hook_reports_the_mirror_variant_half_without_unprojecting_the_seed(monkeypatch):
    import routes.employee_products as ep

    outcomes = iter([
        {"status": "synced", "target": "mirror", "variant_status": "synced", "variant_offers_written": 2},
        {"status": "synced", "target": "mirror", "variant_status": "listing_offer_not_written",
         "offer_skips": {"variant_not_re_read": 3}},  # the hook passes reasons through as they come
        {"status": "synced", "target": "mirror", "variant_status": "error"},
    ])

    async def fake_sync(seed_id, **kwargs):
        return next(outcomes)

    async def fake_pdp(**kwargs):
        return "refreshed"

    monkeypatch.setattr("services.external_offer_dual_write.dual_write_enabled", lambda: True)
    monkeypatch.setattr("services.external_offer_dual_write.sync_offer_for_seed", fake_sync)
    monkeypatch.setattr("services.seed_data_writer.refresh_agent_pdp_view_for_seed", fake_pdp)
    wrote, held, failed = (asyncio.run(ep._project_refreshed_seed_to_serving_surfaces("eps_1")) for _ in range(3))
    assert wrote["projected"] == 1 and wrote["wrote_mirror"] == 1 and wrote["wrote_mirror_variants"] == 1
    assert wrote["errored"] == 0
    assert held["projected"] == 1 and held["offer_skip_variant_not_re_read"] == 3
    assert held["offer_skip_mirror_variants_listing_offer_not_written"] == 1
    # A raise in the variant half is an ERROR the run verdict reads, not a skip that can fall out of the
    # top-8 histogram; the mirror row it rode beside is still counted as projected.
    assert failed["projected"] == 1 and failed["errored"] == 1 and failed["mirror_variants_errored"] == 1
    assert "offer_skip_mirror_variants_error" not in failed
    for counts in (wrote, held, failed):
        assert not any(k.startswith("skip_") for k in counts), counts


# ----------------------------------------------- the two checks the planner cannot make here (review)


def test_a_row_under_another_seller_than_the_mirrors_is_never_written(monkeypatch):
    """The product-level row (the mirror's seller) is not in the planner's input, so its one-seller rule
    sees only the variant rows. A row under any other seller is refused by the lane itself."""
    fake, project = _mirror_fake(mirror_offers=[
        _mirror_offer("of_mine", sku=f"{MPK}::v:47761881301179", vid="47761881301179"),
        _mirror_offer("of_theirs", sku=f"{MPK}::v:47761881301180", vid="47761881301180", merchant="merch_obs_OTHER"),
    ])
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake)
    assert [u["offer_id"] for u in fake.updates] == ["of_mine"]
    assert result["offer_skips"] == {"mirror_variants_mirror_other_seller": 1}


def test_only_another_sellers_rows_write_nothing_and_say_why(monkeypatch):
    fake, project = _mirror_fake(mirror_offers=[
        _mirror_offer("of_theirs", sku=f"{MPK}::v:47761881301179", vid="47761881301179", merchant="merch_obs_OTHER"),
    ])
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake)
    assert result["variant_status"] == "listing_offer_not_written" and fake.updates == []
    assert result["offer_skips"] == {"mirror_variants_mirror_other_seller": 1}


def test_a_row_in_another_market_is_never_written(monkeypatch):
    """The mirror upsert refuses a market mismatch (`catalog_offers.market = EXCLUDED.market`); the
    variant rows get the same refusal, even when the currency agrees (EC prices in USD)."""
    fake, project = _mirror_fake(mirror_offers=[
        _mirror_offer("of_us", sku=f"{MPK}::v:47761881301179", vid="47761881301179"),
        _mirror_offer("of_ec", sku=f"{MPK}::v:47761881301180", vid="47761881301180", market="EC"),
    ])
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake)
    assert [u["offer_id"] for u in fake.updates] == ["of_us"]
    assert result["offer_skips"] == {"mirror_variants_mirror_market_mismatch": 1}


def test_the_mirrors_own_offer_on_another_sku_is_left_to_the_upsert(monkeypatch):
    """ON CONFLICT (offer_id) never moves sku_key, so the mirror's offer can sit on a variant sku. It
    takes the upsert's price; the lane must not price it a second time in the same refresh."""
    fake, project = _mirror_fake(mirror_offers=[
        _mirror_offer(mod.derive_mirror_offer_id(MPK), sku=f"{MPK}::v:47761881301179", vid="47761881301179"),
    ])
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake)
    assert result["variant_status"] == "no_variant_offer" and fake.updates == []


def test_a_mirror_refresh_whose_page_named_no_currency_writes_no_variant_row(monkeypatch):
    """The KRW-as-USD guard: a read whose currency the reader filled in is not a reading."""
    fake, project = _mirror_fake()
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake, currency_read=False)
    assert result["status"] == "synced" and result["variant_status"] == "currency_not_read"
    assert fake.updates == []


def test_the_ratio_band_holds_on_the_mirror_lane(monkeypatch):
    fake, project = _mirror_fake(mirror_offers=[
        _mirror_offer("of_backfill", sku=f"{MPK}::v:47761881301179", vid="47761881301179", price=1.0),
    ])
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake)
    assert result["variant_status"] == "listing_offer_not_written" and fake.updates == []
    assert result["offer_skips"] == {"mirror_variants_price_ratio_out_of_bounds": 1}


def test_a_failure_part_way_reports_what_was_written(monkeypatch):
    fake, project = _mirror_fake(mirror_offers=[
        _mirror_offer("of_v1", sku=f"{MPK}::v:47761881301179", vid="47761881301179"),
        _mirror_offer("of_v2", sku=f"{MPK}::v:47761881301180", vid="47761881301180", price=40.0),
    ])
    _patch_projection(monkeypatch, project)
    real = fake.fetch_one

    async def second_update_fails(sql, params=None):
        if str(sql).strip().startswith("UPDATE catalog_offers") and fake.updates:
            raise RuntimeError("connection reset")
        return await real(sql, params)

    fake.fetch_one = second_update_fails
    result = _sync(monkeypatch, fake)
    assert result["status"] == "synced" and result["variant_status"] == "error"
    assert result["variant_offers_written"] == 1


@pytest.mark.parametrize("source_product_id", ["", None])
def test_an_empty_source_product_id_strips_nothing(source_product_id):
    """`":" + vid` would otherwise match the bare-colon prefix an empty product id makes."""
    row = _mirror_offer("of_old", sku="s", vid=":47761881301180", source_product_id=source_product_id)
    assert mod._native_variant_row(row)["source_variant_id"] == ":47761881301180"


def test_a_suppressed_mirror_variant_row_is_never_written(monkeypatch):
    """A human or a quarantine withdrew the row: the refresh does not bring it back to life with a price."""
    suppressed = {**_mirror_offer("of_held", sku=f"{MPK}::v:47761881301180", vid="47761881301180"), "suppressed": True}
    fake, project = _mirror_fake(mirror_offers=[
        _mirror_offer("of_live", sku=f"{MPK}::v:47761881301179", vid="47761881301179"), suppressed,
    ])
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake)
    assert [u["offer_id"] for u in fake.updates] == ["of_live"], result


def test_the_lanes_own_refusals_are_reported_even_when_the_planner_declines_the_rest(monkeypatch):
    """Another seller's row (refused by the lane) beside a row for another listing (refused by the
    planner as no_listing_offer): both reasons reach the report, not only the planner's status."""
    fake, project = _mirror_fake(mirror_offers=[
        _mirror_offer("of_theirs", sku=f"{MPK}::v:47761881301179", vid="47761881301179", merchant="merch_obs_OTHER"),
        _mirror_offer("of_50ml", sku=f"{MPK}::v:47761881301180", vid="47761881301180", source_ref=DEST + "-50ml"),
    ])
    _patch_projection(monkeypatch, project)
    result = _sync(monkeypatch, fake)
    assert result["variant_status"] == "no_listing_offer" and fake.updates == []
    assert result["offer_skips"] == {"mirror_variants_mirror_other_seller": 1}
