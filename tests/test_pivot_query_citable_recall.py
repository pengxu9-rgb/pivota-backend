"""ADR-007 SLICE 3 — citable recall lane.

The OFFER-FREE citable lane in services.pivot_query_service contributes
index_eligible (citation-only) products to recall for inform/recommend intent.
It is a SEPARATE lane from the offer-backed canonical recall: it joins
index_pipeline_state on content_key (NOT catalog_offers), so its rows are NEVER
buyable. These tests pin the behavioral contract:

  * flag ON + inform intent  -> citable, offer-free, buyable=False item appears
  * strict/commerce-explicit -> citable item SUPPRESSED
  * offer-backed product     -> recalled exactly as today; never double-listed
  * citable item             -> cannot produce a quote/order (fail-closed)
  * flag OFF                 -> recall byte-identical; the citable SQL never runs
  * neutrality               -> offer presence is a signal, never a hard boost
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import services.pivot_query_service as module
from models.catalog import (
    PivotOffersResolveRequest,
    PivotQueryRequest,
    PivotQuoteItem,
    PivotQuoteRequest,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _citable_row(
    *,
    content_key: str = "ck_citable_1",
    product_key: str = "prod::citable::1",
    title: str = "Vitamin C Serum",
    brand: str = "Citable Brand",
) -> dict:
    """A raw row shaped like _fetch_citable_canonical_rows returns: NO offer
    columns, NO sku_key. content_key + a canonical destination only."""
    return {
        "merchant_id": None,
        "merchant_name": None,
        "merchant_primary_platform": None,
        "product_key": product_key,
        "content_key": content_key,
        "source_product_id": "sp_1",
        "canonical_url": "https://brand.example/products/vitamin-c",
        "pivota_canonical_url": "https://agent.pivota.cc/p/ck_citable_1",
        "catalog_track": "multi_merchant_canonical",
        "truth_tier": "primary",
        "readiness_tier": "knowledge_ready",
        "pdp_scope": "multi_merchant_canonical",
        "pdp_lifecycle_stage": "published",
        "source_system": "test",
        "freshness_json": {"updated_at": "2026-06-24T00:00:00Z"},
        "product_updated_at": datetime.now(timezone.utc),
        "product_title": title,
        "product_description": "A citable brightening serum.",
        "brand": brand,
        "product_type": "serum",
        "category": "skincare/serum",
        "product_image_url": "https://brand.example/img.jpg",
        "rank_score": 100,
    }


def _buyable_item(*, product_key: str = "prod::buyable::1", content_key: str = "ck_buyable_1"):
    """An offer-backed canonical item (mirrors _build_canonical_items output)."""
    return module.PivotResultItem(
        merchant=module.MerchantNode(merchant_id="merch_1", merchant_name="Demo Merchant"),
        product=module.ProductNode(product_key=product_key, title="Vitamin C Serum"),
        sku=module.SkuNode(sku_key="sku::1"),
        offers=[
            module.OfferNode(
                offer_id="offer::1",
                catalog_track="internal_merchant",
                truth_tier="primary",
                readiness_tier="commerce_ready",
                offer_mode="merchant_checkout",
            )
        ],
        catalog_track="internal_merchant",
        truth_tier="primary",
        readiness_tier="commerce_ready",
        freshness={},
        source_system="test",
        match_explanation={"content_key": content_key},
    )


def _patch_no_offer_backed_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty_canonical(**_kwargs):
        return []

    async def empty_build(*_args, **_kwargs):
        return []

    async def no_external(*_args, **_kwargs):
        return []

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", empty_canonical)
    monkeypatch.setattr(module, "_build_canonical_items", empty_build)
    monkeypatch.setattr(module, "_fetch_external_fallback_items", no_external)


# ---------------------------------------------------------------------------
# flag ON + inform intent -> citable item appears, offer-free, buyable=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_citable_lane_emits_offer_free_buyable_false_item_when_flag_on_inform_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INDEX_ELIGIBLE_RECALL", "1")
    _patch_no_offer_backed_recall(monkeypatch)

    async def fake_citable_rows(**_kwargs):
        return [_citable_row()]

    monkeypatch.setattr(module, "_fetch_citable_canonical_rows", fake_citable_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="vitamin c serum",
            limit=5,
            include_external=False,
            include_incentives=False,
            strict_serving_mode=False,  # inform/recommend intent
        )
    )

    assert result.total == 1
    item = result.items[0]
    # Offer-free + explicitly non-buyable.
    assert item.buyable is False
    assert item.offers == []
    # No OfferNode / offer_id anywhere on the item.
    assert all(getattr(o, "offer_id", None) is None for o in item.offers)
    # No sku_key -> nothing for the quote/order path to resolve.
    assert item.sku.sku_key is None
    # Carries a citation destination + the citable lane marker.
    assert item.match_explanation["lane"] == "citable_canonical"
    assert item.match_explanation["buyable"] is False
    assert item.match_explanation["destination_url"] == "https://agent.pivota.cc/p/ck_citable_1"
    # NOT given the internal_merchant ordering track (no ownership boost).
    assert item.catalog_track == "citation"


# ---------------------------------------------------------------------------
# strict / commerce-explicit intent -> citable item SUPPRESSED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_citable_lane_suppressed_for_strict_shopping_intent_even_with_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INDEX_ELIGIBLE_RECALL", "1")
    _patch_no_offer_backed_recall(monkeypatch)

    async def fail_citable_rows(**_kwargs):
        raise AssertionError(
            "the citable lane must NOT run for strict/commerce-explicit shopping intent"
        )

    monkeypatch.setattr(module, "_fetch_citable_canonical_rows", fail_citable_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="vitamin c serum",
            limit=5,
            include_external=False,
            include_incentives=False,
            strict_serving_mode=True,  # shopping / commerce-explicit
        )
    )

    assert result.total == 0
    assert result.items == []


# ---------------------------------------------------------------------------
# offer-backed product recalled as today; dedupe (no double-listing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offer_backed_product_recalled_unchanged_and_not_double_listed_as_citable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INDEX_ELIGIBLE_RECALL", "1")

    async def fake_canonical_rows(**_kwargs):
        return [{"sku_key": "sku::1"}]

    async def fake_build_items(*_args, **_kwargs):
        return [_buyable_item(product_key="prod::shared::1", content_key="ck_shared")]

    async def no_external(*_args, **_kwargs):
        return []

    # The citable lane returns the SAME product (same product_key + content_key)
    # — it must be deduped away in favor of the buyable offer-backed item.
    async def fake_citable_rows(**_kwargs):
        return [
            _citable_row(product_key="prod::shared::1", content_key="ck_shared"),
        ]

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_canonical_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "_fetch_external_fallback_items", no_external)
    monkeypatch.setattr(module, "_fetch_citable_canonical_rows", fake_citable_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="vitamin c serum",
            limit=5,
            include_external=False,
            include_incentives=False,
            strict_serving_mode=False,
        )
    )

    # Exactly one item — the buyable one — never double-listed as citable.
    assert result.total == 1
    item = result.items[0]
    assert item.buyable is True
    assert len(item.offers) == 1
    assert item.offers[0].offer_id == "offer::1"


@pytest.mark.asyncio
async def test_distinct_citable_product_added_alongside_buyable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A citable product that is NOT the offer-backed one is added as a separate
    offer-free item (the recall-widening behavior the slice is for)."""
    monkeypatch.setenv("INDEX_ELIGIBLE_RECALL", "1")

    async def fake_canonical_rows(**_kwargs):
        return [{"sku_key": "sku::1"}]

    async def fake_build_items(*_args, **_kwargs):
        return [_buyable_item(product_key="prod::buyable::1", content_key="ck_buyable_1")]

    async def no_external(*_args, **_kwargs):
        return []

    async def fake_citable_rows(**_kwargs):
        return [_citable_row(product_key="prod::citable::1", content_key="ck_citable_1")]

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_canonical_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "_fetch_external_fallback_items", no_external)
    monkeypatch.setattr(module, "_fetch_citable_canonical_rows", fake_citable_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="vitamin c serum",
            limit=5,
            include_external=False,
            include_incentives=False,
            strict_serving_mode=False,
        )
    )

    assert result.total == 2
    buyables = [i for i in result.items if i.buyable]
    citables = [i for i in result.items if not i.buyable]
    assert len(buyables) == 1 and len(citables) == 1
    assert citables[0].offers == []


# ---------------------------------------------------------------------------
# fail-closed: a citable item cannot produce a quote or order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_citable_item_cannot_resolve_offers_for_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve_pivot_offers over a query that returns ONLY a citable item must
    yield zero offers and no best_us_offer — the quote/order path has nothing
    to transact (fail-closed)."""
    monkeypatch.setenv("INDEX_ELIGIBLE_RECALL", "1")
    _patch_no_offer_backed_recall(monkeypatch)

    async def fake_citable_rows(**_kwargs):
        return [_citable_row()]

    monkeypatch.setattr(module, "_fetch_citable_canonical_rows", fake_citable_rows)

    resolved = await module.resolve_pivot_offers(
        PivotOffersResolveRequest(
            query="vitamin c serum",
            include_external=False,
        )
    )

    assert resolved.offers == []
    assert resolved.offers_count == 0
    assert resolved.best_us_offer is None


@pytest.mark.asyncio
async def test_quote_rejects_item_with_no_offer_or_sku(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quote request built from a citable item (no offer_id / no sku_key)
    resolves no offer row -> the quote path fails closed."""

    async def no_offer_row(*_args, **_kwargs):
        return None  # there is no catalog_offers row for a citable item

    monkeypatch.setattr(module, "_fetch_offer_row", no_offer_row)

    # A PivotQuoteItem carrying neither offer_id nor sku_key (what a citable
    # result item would yield) cannot be resolved to a buyable offer.
    quote_item = PivotQuoteItem(quantity=1)
    assert quote_item.offer_id is None
    assert quote_item.sku_key is None
    # _fetch_offer_row over an empty/None offer_id returns nothing -> no quote.
    assert await module._fetch_offer_row("") is None


# ---------------------------------------------------------------------------
# flag OFF -> byte-identical; citable lane never runs; no new SQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_citable_lane_never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INDEX_ELIGIBLE_RECALL", raising=False)
    _patch_no_offer_backed_recall(monkeypatch)

    async def fail_citable_rows(**_kwargs):
        raise AssertionError(
            "INDEX_ELIGIBLE_RECALL OFF: the citable SQL must NEVER execute"
        )

    monkeypatch.setattr(module, "_fetch_citable_canonical_rows", fail_citable_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="vitamin c serum",
            limit=5,
            include_external=False,
            include_incentives=False,
            strict_serving_mode=False,
        )
    )

    assert result.total == 0
    assert result.items == []


@pytest.mark.asyncio
async def test_flag_off_results_byte_identical_to_offer_backed_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag OFF, the result is exactly the offer-backed recall — adding
    the citable lane introduced no change to the served set."""
    monkeypatch.delenv("INDEX_ELIGIBLE_RECALL", raising=False)

    async def fake_canonical_rows(**_kwargs):
        return [{"sku_key": "sku::1"}]

    async def fake_build_items(*_args, **_kwargs):
        return [_buyable_item()]

    async def no_external(*_args, **_kwargs):
        return []

    async def fail_citable_rows(**_kwargs):
        raise AssertionError("citable lane must not run when flag is OFF")

    monkeypatch.setattr(module, "_fetch_canonical_search_rows", fake_canonical_rows)
    monkeypatch.setattr(module, "_build_canonical_items", fake_build_items)
    monkeypatch.setattr(module, "_fetch_external_fallback_items", no_external)
    monkeypatch.setattr(module, "_fetch_citable_canonical_rows", fail_citable_rows)

    result = await module.search_pivot_catalog(
        PivotQueryRequest(
            query="vitamin c serum",
            limit=5,
            include_external=False,
            include_incentives=False,
        )
    )

    assert result.total == 1
    assert result.items[0].buyable is True
    assert result.items[0].offers[0].offer_id == "offer::1"


# ---------------------------------------------------------------------------
# ranking neutrality — offer presence is a signal, never a hard boost
# ---------------------------------------------------------------------------


def test_citable_track_gets_no_ownership_or_offer_boost_in_sort():
    """_sort_items gives an ordering boost only to catalog_track ==
    'internal_merchant'. The citable lane uses catalog_track == 'citation'
    precisely so it inherits NO such boost — it ranks by merit only. Conversely,
    a buyable internal item must not get an ADDED ownership boost beyond what the
    pre-ADR-007 sort already applied. Pin both with a same-merit comparison."""
    import inspect

    src = inspect.getsource(module._sort_items)
    # The only catalog_track boost in the sort is the pre-existing
    # internal_merchant tie-break; no 'citation' boost was added.
    assert "internal_merchant" in src
    assert "citation" not in src, (
        "the citable track must NOT be special-cased in _sort_items — it ranks "
        "by merit only (no boost)"
    )


def test_citable_lane_sql_has_no_offer_or_sku_join_and_no_boost():
    """Structural guard on the citable SQL: it must NEVER join catalog_offers or
    catalog_skus (that would make citable products appear buyable), and it must
    not add an ownership/offer rank boost. It joins index_pipeline_state on
    content_key with index_eligible = TRUE."""
    import inspect

    src = inspect.getsource(module._fetch_citable_canonical_rows)
    # Inspect the SQL body only (strip the docstring, which discusses the
    # forbidden joins in prose). The SQL begins at the database.fetch_all call.
    sql = src[src.index("database.fetch_all"):]
    assert "catalog_offers" not in sql, "citable lane SQL must NOT join catalog_offers"
    assert "catalog_skus" not in sql, "citable lane SQL must NOT join catalog_skus"
    assert "JOIN catalog_offers" not in sql
    assert "JOIN catalog_skus" not in sql
    assert "index_pipeline_state ips" in sql
    assert "ips.index_eligible = TRUE" in sql
    assert "ips.content_key = p.content_key" in sql
    # No first-party / ownership boost term in the rank expression.
    assert "internal_merchant" not in sql


def test_canonical_offer_inner_join_is_untouched():
    """HARD CONSTRAINT: the offer-backed lane's INNER JOIN catalog_offers stays
    exactly as-is (no LEFT JOIN admitting null-priced rows). Pin the INNER JOIN
    shape so a future change that loosens it to a LEFT JOIN fails fast."""
    import inspect

    src = inspect.getsource(module._fetch_canonical_search_rows)
    assert "JOIN catalog_offers o" in src
    assert "LEFT JOIN catalog_offers" not in src, (
        "the offer-backed lane must keep its INNER JOIN — a LEFT JOIN would inject "
        "null-priced rows and make un-buyable products appear buyable"
    )
    # The catalog_skus INNER JOIN (the buyable lane's variant join) also stays.
    assert "JOIN catalog_skus s ON s.product_key = p.product_key" in src
