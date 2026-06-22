"""Unit tests for fix-location queue segments on the readiness optimization payload.

Segments power the merchant "Catalog health" work queue chips
(Fix here / In your store / Other). They mirror the frontend precedence in
getProductActionLabel so a clicked chip resolves to exactly one segment.
"""

from readiness import summary as S
from readiness.models import ProductReadinessQueueItem


def _item(qid: str, **kw) -> ProductReadinessQueueItem:
    return ProductReadinessQueueItem(
        queue_item_id=qid,
        product_id=qid,
        platform="shopify",
        title=qid,
        **kw,
    )


def test_segment_for_queue_item_precedence():
    # run_product_enrichment always wins, regardless of fix_surface.
    assert (
        S._segment_for_queue_item(
            _item("a", recommended_action_type="run_product_enrichment", fix_surface="product_content")
        )
        == "fix_here"
    )
    assert (
        S._segment_for_queue_item(
            _item("d", recommended_action_type="run_product_enrichment", fix_surface="catalog_data")
        )
        == "fix_here"
    )
    # catalog_data source data routes to the store.
    assert (
        S._segment_for_queue_item(
            _item("b", recommended_action_type="review_catalog_data", fix_surface="catalog_data")
        )
        == "in_store"
    )
    # Everything else (integrations/policy/pivota_managed, or non-executable
    # product_content) falls to "other".
    assert (
        S._segment_for_queue_item(
            _item("c", recommended_action_type="review_catalog_data", fix_surface="integrations")
        )
        == "other"
    )
    assert (
        S._segment_for_queue_item(
            _item("e", recommended_action_type=None, fix_surface="product_content")
        )
        == "other"
    )


def test_compute_segment_counts_partition():
    items = [
        _item("a", recommended_action_type="run_product_enrichment", fix_surface="product_content"),
        _item("b", recommended_action_type="review_catalog_data", fix_surface="catalog_data"),
        _item("c", recommended_action_type="review_catalog_data", fix_surface="integrations"),
        _item("d", recommended_action_type="run_product_enrichment", fix_surface="catalog_data"),
        _item("e", recommended_action_type=None, fix_surface="product_content"),
    ]
    counts = S._compute_segment_counts(items)
    assert (counts.all, counts.fix_here, counts.in_store, counts.other) == (5, 2, 1, 2)
    # The three segments partition the total exactly (no item double-counted or dropped).
    assert counts.fix_here + counts.in_store + counts.other == counts.all


def test_normalize_segment():
    assert S._normalize_segment(None) == "all"
    assert S._normalize_segment("") == "all"
    assert S._normalize_segment("  FIX_HERE ") == "fix_here"
    assert S._normalize_segment("in_store") == "in_store"
    assert S._normalize_segment("other") == "other"
    assert S._normalize_segment("bogus") == "all"


def _render_payload(segment: str):
    """Drive the real render path in-memory (aux_context=None skips DB projection)."""
    import asyncio

    from readiness.models import (
        MerchantReadinessOptimizationPayload,
        OptimizationPlan,
        ReadinessSummary,
    )

    payload = MerchantReadinessOptimizationPayload(
        plan=OptimizationPlan(plan_id="plan_x", snapshot_id="snap_x"),
        readiness_summary=ReadinessSummary(
            tier="yellow", label="Needs work", assessment_state="assessed"
        ),
        product_queue=[
            _item("a", recommended_action_type="run_product_enrichment", fix_surface="product_content"),
            _item("b", recommended_action_type="review_catalog_data", fix_surface="catalog_data"),
            _item("c", recommended_action_type="review_catalog_data", fix_surface="integrations"),
            _item("d", recommended_action_type="run_product_enrichment", fix_surface="catalog_data"),
            _item("e", recommended_action_type=None, fix_surface="product_content"),
        ],
    )
    return asyncio.run(
        S._render_readiness_optimization_payload(
            payload,
            aux_context=None,
            queue_mode="full",
            page=1,
            page_size=50,
            search=None,
            issue_bucket=None,
            push_status="all",
            blocked_only=False,
            low_quality_only=False,
            sort_by="default",
            segment=segment,
        )
    )


def test_render_segment_all_returns_full_queue_with_counts():
    out = _render_payload("all")
    assert len(out.product_queue) == 5
    c = out.queue_segment_counts
    assert (c.all, c.fix_here, c.in_store, c.other) == (5, 2, 1, 2)
    assert out.product_queue_page.applied_filters.segment == "all"


def test_render_segment_narrows_queue_but_counts_stay_full():
    out = _render_payload("in_store")
    # Only the catalog_data item remains in the rendered queue...
    assert [i.queue_item_id for i in out.product_queue] == ["b"]
    # ...but the chip counts still reflect the full pre-segment set.
    c = out.queue_segment_counts
    assert (c.all, c.fix_here, c.in_store, c.other) == (5, 2, 1, 2)
    assert out.product_queue_page.applied_filters.segment == "in_store"

    out_fix = _render_payload("fix_here")
    assert sorted(i.queue_item_id for i in out_fix.product_queue) == ["a", "d"]
