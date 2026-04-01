from __future__ import annotations

import asyncio

import pytest

from readiness.models import (
    CapabilityStatus,
    ChannelCoverageStatus,
    MerchantReadinessSnapshot,
    QualityCoverageSummary,
    ReadyProduct,
    ReadyVariant,
)
from readiness.service import reset_readiness_snapshot_cache_observability
from readiness.summary import (
    _build_platform_admin_url,
    build_readiness_optimization,
    build_readiness_summary,
    reset_readiness_optimization_cache_observability,
    schedule_readiness_optimization_warmup,
    summarize_readiness_snapshot,
)


@pytest.fixture(autouse=True)
def _reset_optimization_cache(monkeypatch):
    reset_readiness_optimization_cache_observability()
    reset_readiness_snapshot_cache_observability()

    async def _no_decisions(*_args, **_kwargs):
        return {}

    async def _no_cache_rows(*_args, **_kwargs):
        return {}

    async def _no_stores(*_args, **_kwargs):
        return []

    async def _no_store_context(*_args, **_kwargs):
        return {}

    async def _no_field_facts(*_args, **_kwargs):
        return {}

    monkeypatch.setattr("readiness.summary.list_source_data_decisions_by_reason_codes", _no_decisions)
    monkeypatch.setattr("readiness.summary._load_cache_rows_for_product_keys", _no_cache_rows)
    monkeypatch.setattr("readiness.summary.get_merchant_active_stores", _no_stores)
    monkeypatch.setattr("readiness.summary._load_store_context", _no_store_context)
    monkeypatch.setattr("readiness.summary._load_catalog_field_facts_for_product_keys", _no_field_facts)


def _snapshot(
    *,
    readiness_score: int,
    checkout_status: str = "ready",
    order_sync_status: str = "ready",
    ready_variant_count: int = 1,
    blocked_variant_count: int = 0,
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
) -> MerchantReadinessSnapshot:
    return MerchantReadinessSnapshot(
        merchant_id="merch_efbc46b4619cfbdf",
        merchant_name="Alpha Merchant",
        channel="ucp",
        generated_at="2026-03-18T00:00:00Z",
        merchant_alpha_mode="real_merchant_alpha",
        readiness_score=readiness_score,
        domain_scores={},
        capability_status={
            "checkout_execution": checkout_status,
            "order_writeback_state_sync": order_sync_status,
        },
        blockers=blockers or [],
        warnings=warnings or [],
        merchant_capabilities=[],
        channel_coverage=[
            ChannelCoverageStatus(
                channel="ucp",
                status="ready" if blocked_variant_count == 0 else "partial",
                ready_variant_count=ready_variant_count,
                blocked_variant_count=blocked_variant_count,
            )
        ],
        source_of_truth={},
        stubbed_capabilities=[],
        audit_notes=[],
        products=[],
    )


def test_summarize_readiness_snapshot_green():
    summary = summarize_readiness_snapshot(_snapshot(readiness_score=88))

    assert summary.tier == "green"
    assert summary.assessment_state == "assessed"
    assert summary.ready_variant_count == 1
    assert summary.blocked_variant_count == 0
    assert "ready for supervised LLM commerce" in str(summary.summary_text)


def test_summarize_readiness_snapshot_yellow_when_variants_blocked():
    summary = summarize_readiness_snapshot(
        _snapshot(
            readiness_score=77,
            ready_variant_count=10,
            blocked_variant_count=2,
            warnings=["inventory_snapshot_stale"],
        )
    )

    assert summary.tier == "yellow"
    assert summary.top_warnings == ["inventory_snapshot_stale"]
    assert "Resolve blocked variants" in str(summary.next_action)
    assert "Most of the catalog is usable" in str(summary.summary_text)


def test_summarize_readiness_snapshot_red_when_checkout_blocked():
    summary = summarize_readiness_snapshot(
        _snapshot(
            readiness_score=82,
            checkout_status="blocked",
            blockers=["merchant_checkout_capability_missing"],
        )
    )

    assert summary.tier == "red"
    assert summary.top_blockers == ["merchant_checkout_capability_missing"]


def test_build_platform_admin_url_supports_additional_store_platforms():
    assert _build_platform_admin_url(
        platform="woocommerce",
        platform_product_id="prod_42",
        store_domains_by_platform={
            "woocommerce": "https://catalog.alpha.test/store",
        },
    ) == "https://catalog.alpha.test/store/wp-admin/post.php?post=prod_42&action=edit"

    assert _build_platform_admin_url(
        platform="wix",
        platform_product_id="00000000-1111-2222-3333-444444444444",
        store_domains_by_platform={
            "wix": "d96ead0c-3448-94f5-3d48-56f91ad87768",
        },
    ) == (
        "https://manage.wix.com/dashboard/"
        "d96ead0c-3448-94f5-3d48-56f91ad87768/store/products/product/"
        "00000000-1111-2222-3333-444444444444"
    )

    assert _build_platform_admin_url(
        platform="bigcommerce",
        platform_product_id="77",
        store_domains_by_platform={
            "bigcommerce": "abc123.mybigcommerce.com",
        },
    ) == "https://abc123.mybigcommerce.com/manage/products/77/edit"


def test_build_platform_admin_url_skips_untrusted_wix_identifier():
    assert (
        _build_platform_admin_url(
            platform="wix",
            platform_product_id="prod_1",
            store_domains_by_platform={
                "wix": "peng652.wixsite.com/aydan-1",
            },
        )
        is None
    )


@pytest.mark.asyncio
async def test_build_readiness_summary_returns_not_assessed_when_non_alpha(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    summary = await build_readiness_summary("merch_not_assessed")

    assert summary.tier == "red"
    assert summary.assessment_state == "not_assessed"
    assert summary.top_blockers == ["merchant_not_assessed_for_readiness_alpha"]
    assert summary.recommended_actions


@pytest.mark.asyncio
async def test_build_readiness_optimization_returns_issue_buckets_and_product_queue(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={
                "checkout_execution": "ready",
                "order_writeback_state_sync": "ready",
            },
            blockers=["merchant_shipping_policy_missing"],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=1,
                    blocked_variant_count=1,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_1",
                    platform="shopify",
                    title="Alpha Product",
                    default_image_url="https://example.com/p.jpg",
                    variants=[
                        ReadyVariant(
                            variant_id="var_1",
                            title="Default",
                            price={"amount": 10, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": ["out_of_stock", "missing_price"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(
                                capability="checkout",
                                status="blocked",
                                score=40,
                                blockers=["out_of_stock", "missing_price"],
                            ),
                            channel_coverage={"ucp": "blocked"},
                        ),
                        ReadyVariant(
                            variant_id="var_2",
                            title="Backup",
                            price={"amount": 10, "currency": "USD"},
                            inventory={"quantity": 5, "availability": "in_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": []},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                            channel_coverage={"ucp": "ready"},
                        ),
                    ],
                )
            ],
        )

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    assert payload.plan.snapshot_id.startswith("rdsnap_")
    assert payload.plan.plan_id.startswith("rdplan_")
    assert payload.plan.workspace_version == "agent_commerce_optimization.v1"
    assert payload.plan.refresh_state in {"fresh", "stale", "expired"}
    assert payload.score_bundle.readiness_score == 77
    assert payload.readiness_summary.tier == "red"
    assert payload.issue_buckets[0].code in {"shipping_returns_setup", "price_currency", "inventory_availability"}
    assert payload.issue_buckets[0].priority_score > 0
    assert payload.product_queue[0].platform == "shopify"
    assert payload.product_queue[0].queue_item_scope == "product"
    assert payload.product_queue[0].queue_item_id.startswith("product:shopify:")
    assert payload.product_queue[0].blocked_variant_count == 0
    assert payload.product_queue[0].agent_push_status == "eligible_for_agent_push"
    assert payload.product_queue[0].eligible_variant_count == 1
    assert payload.product_queue[0].excluded_variant_count == 1
    assert payload.product_queue[0].priority_score > 0
    assert payload.product_queue[0].top_issues
    assert payload.merchant_actions
    assert payload.content_opportunity_count == 0
    assert {lane.reason_code for lane in payload.source_data_lanes} == {
        "missing_price",
        "out_of_stock",
        "missing_primary_image",
        "shipping_delivery_completeness",
        "trust_support_policy_completeness",
        "product_fit_composition_completeness",
    }
    lanes_by_code = {lane.reason_code: lane for lane in payload.source_data_lanes}
    assert lanes_by_code["missing_price"].affected_products == 1
    assert lanes_by_code["missing_price"].affected_variants == 1
    assert lanes_by_code["out_of_stock"].affected_products == 1
    assert lanes_by_code["out_of_stock"].affected_variants == 1
    assert lanes_by_code["missing_primary_image"].affected_products == 0
    assert lanes_by_code["shipping_delivery_completeness"].affected_products == 1
    assert lanes_by_code["trust_support_policy_completeness"].affected_products == 1
    assert lanes_by_code["product_fit_composition_completeness"].affected_products == 1
    assert payload.agent_push_summary.eligible_products == 1
    assert payload.agent_push_summary.excluded_variants == 1


@pytest.mark.asyncio
async def test_build_readiness_optimization_adds_platform_admin_url_for_shopify(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=0,
                    blocked_variant_count=1,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_1",
                    platform="shopify",
                    title="Alpha Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_1",
                            title="Default",
                            price={"amount": None, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": ["missing_price", "out_of_stock"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(
                                capability="checkout",
                                status="blocked",
                                score=40,
                                blockers=["missing_price", "out_of_stock"],
                            ),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                )
            ],
        )

    async def fake_get_merchant_active_stores(_merchant_id: str):
        return [
            {
                "platform": "shopify",
                "domain": "alpha-beauty-demo.myshopify.com",
            }
        ]

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)
    monkeypatch.setattr("readiness.summary.get_merchant_active_stores", fake_get_merchant_active_stores)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    expected_url = "https://alpha-beauty-demo.myshopify.com/admin/products/prod_1"
    assert payload.product_queue[0].platform_admin_url == expected_url
    lanes_by_code = {lane.reason_code: lane for lane in payload.source_data_lanes}
    assert lanes_by_code["missing_price"].next_product.platform_admin_url == expected_url
    assert lanes_by_code["out_of_stock"].next_product.platform_admin_url == expected_url


@pytest.mark.asyncio
async def test_build_readiness_optimization_page_mode_returns_filtered_slice(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp", force_refresh: bool = False):
        assert force_refresh is False
        products = []
        for product_id, title in (
            ("prod_alpha", "Alpha Product"),
            ("prod_bravo", "Bravo Product"),
            ("prod_charlie", "Charlie Product"),
        ):
            products.append(
                ReadyProduct(
                    product_id=product_id,
                    platform="shopify",
                    title=title,
                    variants=[
                        ReadyVariant(
                            variant_id=f"{product_id}_var",
                            title="Default",
                            price={"amount": None, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": ["missing_price"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(
                                capability="checkout",
                                status="blocked",
                                score=40,
                                blockers=["missing_price"],
                            ),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                )
            )

        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=0,
                    blocked_variant_count=3,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=products,
        )

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)

    payload = await build_readiness_optimization(
        "merch_efbc46b4619cfbdf",
        queue_mode="page",
        page=2,
        page_size=1,
    )

    assert payload.product_queue_page is not None
    assert payload.product_queue_page.total_items == 3
    assert payload.product_queue_page.page == 2
    assert payload.product_queue_page.page_size == 1
    assert payload.product_queue_page.total_pages == 3
    assert len(payload.product_queue) == 1
    assert payload.product_queue[0].product_id == "prod_bravo"

    filtered = await build_readiness_optimization(
        "merch_efbc46b4619cfbdf",
        queue_mode="page",
        page=1,
        page_size=50,
        search="charlie",
    )
    assert filtered.product_queue_page is not None
    assert filtered.product_queue_page.total_items == 1
    assert filtered.product_queue[0].product_id == "prod_charlie"

    summary_only = await build_readiness_optimization(
        "merch_efbc46b4619cfbdf",
        queue_mode="none",
    )
    assert summary_only.product_queue == []
    assert summary_only.product_queue_page is not None
    assert summary_only.product_queue_page.total_items == 3


@pytest.mark.asyncio
async def test_build_readiness_optimization_serves_stale_then_refreshes_in_background(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    monotonic = {"value": 0.0}
    build_count = {"value": 0}

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp", force_refresh: bool = False):
        build_count["value"] += 1
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at=f"2026-03-18T00:00:0{build_count['value']}Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=0,
                    blocked_variant_count=1,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_1",
                    platform="shopify",
                    title="Alpha Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_1",
                            title="Default",
                            price={"amount": None, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": ["missing_price"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(
                                capability="checkout",
                                status="blocked",
                                score=40,
                                blockers=["missing_price"],
                            ),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                )
            ],
        )

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)
    monkeypatch.setattr("readiness.summary.time.monotonic", lambda: monotonic["value"])

    first = await build_readiness_optimization(
        "merch_efbc46b4619cfbdf",
        queue_mode="page",
        page=1,
        page_size=50,
    )
    monotonic["value"] = 301.0

    stale = await build_readiness_optimization(
        "merch_efbc46b4619cfbdf",
        queue_mode="page",
        page=1,
        page_size=50,
    )
    assert stale.plan.snapshot_id == first.plan.snapshot_id

    for _ in range(5):
        if build_count["value"] >= 2:
            break
        await asyncio.sleep(0)

    refreshed = await build_readiness_optimization(
        "merch_efbc46b4619cfbdf",
        queue_mode="page",
        page=1,
        page_size=50,
    )
    assert refreshed.plan.snapshot_id != first.plan.snapshot_id
    assert build_count["value"] == 2


@pytest.mark.asyncio
async def test_schedule_readiness_optimization_warmup_primes_cache(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    build_count = {"value": 0}

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp", force_refresh: bool = False):
        build_count["value"] += 1
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at=f"2026-03-18T00:00:0{build_count['value']}Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=0,
                    blocked_variant_count=1,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_1",
                    platform="shopify",
                    title="Alpha Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_1",
                            title="Default",
                            price={"amount": None, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": ["missing_price"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(
                                capability="checkout",
                                status="blocked",
                                score=40,
                                blockers=["missing_price"],
                            ),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                )
            ],
        )

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)

    assert schedule_readiness_optimization_warmup("merch_efbc46b4619cfbdf") is True

    for _ in range(5):
        if build_count["value"] >= 1:
            break
        await asyncio.sleep(0)

    warmed = await build_readiness_optimization(
        "merch_efbc46b4619cfbdf",
        queue_mode="page",
        page=1,
        page_size=50,
    )
    assert warmed.plan.snapshot_id.startswith("rdsnap_")
    assert build_count["value"] == 1


@pytest.mark.asyncio
async def test_build_readiness_optimization_uses_truthful_action_mapping(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=0,
                    blocked_variant_count=2,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_text",
                    platform="shopify",
                    title="Text Fix Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_text",
                            title="Default",
                            price={"amount": 10, "currency": "USD"},
                            inventory={"quantity": 2, "availability": "in_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": ["missing_description"], "checkout": []},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(
                                capability="discovery",
                                status="blocked",
                                score=35,
                                blockers=["missing_description"],
                            ),
                            checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                ),
                ReadyProduct(
                    product_id="prod_catalog",
                    platform="shopify",
                    title="Catalog Fix Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_catalog",
                            title="Default",
                            price={"amount": None, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": ["missing_primary_image"], "checkout": ["missing_price"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(
                                capability="discovery",
                                status="blocked",
                                score=20,
                                blockers=["missing_primary_image"],
                            ),
                            checkout=CapabilityStatus(
                                capability="checkout",
                                status="blocked",
                                score=20,
                                blockers=["missing_price"],
                            ),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                ),
            ],
        )

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    queue_by_product_id = {item.product_id: item for item in payload.product_queue}

    assert queue_by_product_id["prod_text"].recommended_action_type == "run_product_enrichment"
    assert queue_by_product_id["prod_text"].fix_surface == "product_content"
    assert queue_by_product_id["prod_catalog"].recommended_action_type == "review_catalog_data"
    assert queue_by_product_id["prod_catalog"].fix_surface == "catalog_data"


@pytest.mark.asyncio
async def test_build_readiness_optimization_includes_saved_source_data_progress(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=0,
                    blocked_variant_count=2,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_saved",
                    platform="shopify",
                    title="Saved Progress Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_saved",
                            title="Default",
                            price={"amount": None, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": ["missing_primary_image"], "checkout": ["missing_price", "out_of_stock"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(
                                capability="discovery",
                                status="blocked",
                                score=20,
                                blockers=["missing_primary_image"],
                            ),
                            checkout=CapabilityStatus(
                                capability="checkout",
                                status="blocked",
                                score=20,
                                blockers=["missing_price", "out_of_stock"],
                            ),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                )
            ],
        )

    async def fake_list_source_data_decisions(_merchant_id: str, *, reason_codes=None, product_keys=None):
        return {
            "missing_price": {
                "shopify|prod_saved": {"decision_state": "pricing_fix_saved"}
            },
            "missing_primary_image": {
                "shopify|prod_saved": {"decision_state": "image_fix_saved"}
            },
            "out_of_stock": {
                "shopify|prod_saved": {"decision_state": "restock_planned"}
            },
        }

    async def fake_load_cache_rows(_merchant_id: str, product_keys):
        return {
            ("shopify", "prod_saved"): {
                "product_data": {
                    "platform": "shopify",
                    "product_id": "prod_saved",
                    "title": "Saved Progress Product",
                    "image_url": "",
                    "variants": [
                        {
                            "variant_id": "var_saved",
                            "price": 0,
                            "currency": "USD",
                            "inventory_quantity": 0,
                        }
                    ],
                    "status": "active",
                    "orderable": True,
                }
            }
        }

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)
    monkeypatch.setattr("readiness.summary.list_source_data_decisions_by_reason_codes", fake_list_source_data_decisions)
    monkeypatch.setattr("readiness.summary._load_cache_rows_for_product_keys", fake_load_cache_rows)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    lanes_by_code = {lane.reason_code: lane for lane in payload.source_data_lanes}
    assert lanes_by_code["missing_price"].decision_counts[0].count == 1
    assert lanes_by_code["missing_price"].decision_counts[0].key == "pricing_fix_saved"
    assert lanes_by_code["missing_primary_image"].decision_counts[0].count == 1
    assert lanes_by_code["missing_primary_image"].decision_counts[0].key == "image_fix_saved"
    assert lanes_by_code["out_of_stock"].decision_counts[0].count == 1


@pytest.mark.asyncio
async def test_build_readiness_optimization_projects_quality_coverage(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=1,
                    blocked_variant_count=1,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_1",
                    platform="shopify",
                    title="Alpha Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_1",
                            title="Default",
                            price={"amount": 10, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": ["out_of_stock"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="blocked", score=40),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                )
            ],
        )

    async def fake_apply_quality_projection(_merchant_id: str, *, snapshot_products, product_queue, cache_rows_by_key=None):
        product_queue[0].content_quality_score = 83.0
        product_queue[0].model_readiness_score = 71.0
        product_queue[0].quality_source = "preview"
        return product_queue, QualityCoverageSummary(
            total_products=1,
            snapshot_scored_products=0,
            effective_scored_products=1,
            preview_only_products=1,
            unscored_products=0,
            coverage_state="full",
            backfill_recommended=True,
        )

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)
    monkeypatch.setattr("readiness.summary._apply_quality_projection", fake_apply_quality_projection)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    assert payload.product_queue[0].content_quality_score == 83.0
    assert payload.product_queue[0].model_readiness_score == 71.0
    assert payload.product_queue[0].quality_source == "preview"
    assert payload.quality_coverage.effective_scored_products == 1
    assert payload.quality_coverage.preview_only_products == 1
    assert payload.quality_coverage.coverage_state == "full"


@pytest.mark.asyncio
async def test_build_readiness_optimization_filters_content_only_queue_items(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=77,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="partial",
                    ready_variant_count=1,
                    blocked_variant_count=1,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_blocked",
                    platform="shopify",
                    title="Blocked Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_blocked",
                            title="Default",
                            price={"amount": 10, "currency": "USD"},
                            inventory={"quantity": 0, "availability": "out_of_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": ["out_of_stock"]},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="blocked", score=25),
                            channel_coverage={"ucp": "blocked"},
                        )
                    ],
                ),
                ReadyProduct(
                    product_id="prod_content_only",
                    platform="shopify",
                    title="Content Opportunity",
                    variants=[
                        ReadyVariant(
                            variant_id="var_ready",
                            title="Default",
                            price={"amount": 10, "currency": "USD"},
                            inventory={"quantity": 5, "availability": "in_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": []},
                            warnings={"discovery": ["missing_description"], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                            channel_coverage={"ucp": "ready"},
                        )
                    ],
                ),
            ],
        )

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    assert [item.product_id for item in payload.product_queue] == ["prod_blocked", "prod_content_only"]
    assert payload.content_opportunity_count == 1


@pytest.mark.asyncio
async def test_build_readiness_optimization_surfaces_title_suggestion_for_generic_title(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=82,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="ready",
                    ready_variant_count=1,
                    blocked_variant_count=0,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_air",
                    platform="shopify",
                    title="Air Max Special Edition",
                    brand="Nike",
                    category="Sneakers",
                    default_image_url="https://example.com/air.jpg",
                    variants=[
                        ReadyVariant(
                            variant_id="var_air_42",
                            title="Black / White / 42",
                            price={"amount": 129, "currency": "USD"},
                            inventory={"quantity": 4, "availability": "in_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": []},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                            channel_coverage={"ucp": "ready"},
                        ),
                        ReadyVariant(
                            variant_id="var_air_45",
                            title="Black / White / 45",
                            price={"amount": 129, "currency": "USD"},
                            inventory={"quantity": 3, "availability": "in_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": []},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                            channel_coverage={"ucp": "ready"},
                        ),
                    ],
                )
            ],
        )

    async def fake_load_cache_rows(_merchant_id: str, product_keys):
        assert product_keys == [("shopify", "prod_air")]
        return {
            ("shopify", "prod_air"): {
                "product_data": {
                    "id": "prod_air",
                    "product_id": "prod_air",
                    "merchant_id": "merch_efbc46b4619cfbdf",
                    "platform": "shopify",
                    "title": "Air Max Special Edition",
                    "description": "Breathable running sneakers with Max Air cushioning for daily commuting and training.",
                    "vendor": "Nike",
                    "product_type": "Sneakers",
                    "tags": ["men", "air cushion", "breathable"],
                    "price": 129.0,
                    "currency": "USD",
                    "inventory_quantity": 7,
                    "image_url": "https://example.com/air.jpg",
                    "variants": [
                        {
                            "id": "var_air_42",
                            "variant_id": "var_air_42",
                            "title": "Black / White / 42",
                            "price": 129.0,
                            "currency": "USD",
                            "inventory_quantity": 4,
                            "options": {"Color": "Black / White", "Size": "42"},
                        },
                        {
                            "id": "var_air_45",
                            "variant_id": "var_air_45",
                            "title": "Black / White / 45",
                            "price": 129.0,
                            "currency": "USD",
                            "inventory_quantity": 3,
                            "options": {"Color": "Black / White", "Size": "45"},
                        },
                    ],
                }
            }
        }

    async def fake_store_context(_merchant_id: str):
        return {"country": "US"}

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)
    monkeypatch.setattr("readiness.summary._load_cache_rows_for_product_keys", fake_load_cache_rows)
    monkeypatch.setattr("readiness.summary._load_store_context", fake_store_context)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    assert [item.product_id for item in payload.product_queue] == ["prod_air"]
    queue_item = payload.product_queue[0]
    assert queue_item.title_health == "rewrite_candidate"
    assert queue_item.recommended_action_type == "run_product_enrichment"
    assert queue_item.suggestion_language == "en"
    assert queue_item.suggested_title_preview == "Nike Air Max Sneakers Men's Black/White air-cushion, breathable Sizes 42-45"
    assert "generic_low_information_title" in queue_item.content_gap_codes
    assert "Material / ingredient info" in queue_item.missing_attribute_labels


@pytest.mark.asyncio
async def test_build_readiness_optimization_skips_healthy_title_only_content_queue_items(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=82,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="ready",
                    ready_variant_count=1,
                    blocked_variant_count=0,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_air",
                    platform="shopify",
                    title="Nike Air Max Sneakers Men's Black/White air-cushion, breathable Sizes 42-45",
                    brand="Nike",
                    category="Sneakers",
                    default_image_url="https://example.com/air.jpg",
                    variants=[
                        ReadyVariant(
                            variant_id="var_air_42",
                            title="Black / White / 42",
                            price={"amount": 129, "currency": "USD"},
                            inventory={"quantity": 4, "availability": "in_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": []},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                            channel_coverage={"ucp": "ready"},
                        ),
                        ReadyVariant(
                            variant_id="var_air_45",
                            title="Black / White / 45",
                            price={"amount": 129, "currency": "USD"},
                            inventory={"quantity": 3, "availability": "in_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": []},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                            channel_coverage={"ucp": "ready"},
                        ),
                    ],
                )
            ],
        )

    async def fake_load_cache_rows(_merchant_id: str, product_keys):
        assert product_keys == [("shopify", "prod_air")]
        return {
            ("shopify", "prod_air"): {
                "product_data": {
                    "id": "prod_air",
                    "product_id": "prod_air",
                    "merchant_id": "merch_efbc46b4619cfbdf",
                    "platform": "shopify",
                    "title": "Nike Air Max Sneakers Men's Black/White air-cushion, breathable Sizes 42-45",
                    "description": "Breathable running sneakers with Max Air cushioning for daily commuting and training.",
                    "vendor": "Nike",
                    "product_type": "Sneakers",
                    "tags": ["men", "air cushion", "breathable"],
                    "price": 129.0,
                    "currency": "USD",
                    "inventory_quantity": 7,
                    "image_url": "https://example.com/air.jpg",
                    "variants": [
                        {
                            "id": "var_air_42",
                            "variant_id": "var_air_42",
                            "title": "Black / White / 42",
                            "price": 129.0,
                            "currency": "USD",
                            "inventory_quantity": 4,
                            "options": {"Color": "Black / White", "Size": "42"},
                        },
                        {
                            "id": "var_air_45",
                            "variant_id": "var_air_45",
                            "title": "Black / White / 45",
                            "price": 129.0,
                            "currency": "USD",
                            "inventory_quantity": 3,
                            "options": {"Color": "Black / White", "Size": "45"},
                        },
                    ],
                }
            }
        }

    async def fake_store_context(_merchant_id: str):
        return {"country": "US"}

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)
    monkeypatch.setattr("readiness.summary._load_cache_rows_for_product_keys", fake_load_cache_rows)
    monkeypatch.setattr("readiness.summary._load_store_context", fake_store_context)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    assert payload.product_queue == []
    assert payload.content_opportunity_count == 0


@pytest.mark.asyncio
async def test_build_readiness_optimization_hydrates_catalog_health_decision_counts(monkeypatch):
    monkeypatch.setenv("FEATURE_READINESS_AUDIT", "true")
    monkeypatch.setenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "true")
    monkeypatch.setenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")

    async def fake_build_snapshot(_merchant_id: str, *, channel: str = "ucp"):
        return MerchantReadinessSnapshot(
            merchant_id="merch_efbc46b4619cfbdf",
            merchant_name="Alpha Merchant",
            channel=channel,
            generated_at="2026-03-18T00:00:00Z",
            merchant_alpha_mode="real_merchant_alpha",
            readiness_score=70,
            domain_scores={},
            capability_status={},
            blockers=[],
            warnings=[],
            merchant_capabilities=[],
            channel_coverage=[
                ChannelCoverageStatus(
                    channel="ucp",
                    status="ready",
                    ready_variant_count=1,
                    blocked_variant_count=0,
                )
            ],
            source_of_truth={},
            stubbed_capabilities=[],
            audit_notes=[],
            products=[
                ReadyProduct(
                    product_id="prod_policy",
                    platform="shopify",
                    title="Policy Heavy Product",
                    variants=[
                        ReadyVariant(
                            variant_id="var_policy",
                            title="Default",
                            price={"amount": 30, "currency": "USD"},
                            inventory={"quantity": 5, "availability": "in_stock"},
                            freshness={},
                            provenance=[],
                            source_of_truth={},
                            blockers={"discovery": [], "checkout": []},
                            warnings={"discovery": [], "checkout": []},
                            discovery=CapabilityStatus(capability="discovery", status="ready", score=100),
                            checkout=CapabilityStatus(capability="checkout", status="ready", score=100),
                            channel_coverage={"ucp": "ready"},
                        )
                    ],
                )
            ],
        )

    async def fake_list_source_data_decisions(_merchant_id: str, *, reason_codes=None, product_keys=None):
        return {
            "shipping_delivery_completeness": {
                "shopify|prod_policy": {
                    "decision_state": "merchant_fix_in_progress",
                }
            }
        }

    monkeypatch.setattr("readiness.summary.build_readiness_snapshot", fake_build_snapshot)
    monkeypatch.setattr("readiness.summary.list_source_data_decisions_by_reason_codes", fake_list_source_data_decisions)

    payload = await build_readiness_optimization("merch_efbc46b4619cfbdf")

    lanes_by_code = {lane.reason_code: lane for lane in payload.source_data_lanes}
    shipping_lane = lanes_by_code["shipping_delivery_completeness"]
    assert shipping_lane.affected_products == 1
    assert shipping_lane.reason_codes == [
        "merchant_delivery_costs_missing",
        "merchant_return_window_missing",
        "merchant_shipping_destinations_missing",
        "merchant_shipping_sla_missing",
    ]
    assert {
        item.key: item.count
        for item in shipping_lane.decision_counts
    }["merchant_fix_in_progress"] == 1
