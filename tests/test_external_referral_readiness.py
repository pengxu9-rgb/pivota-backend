from datetime import datetime, timedelta, timezone

import pytest


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _seed_row(**overrides):
    row = {
        "id": "eps_1",
        "external_product_id": "ext_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://example.com/en-us/product/referral-serum",
        "canonical_url": "https://example.com/en-us/product/referral-serum",
        "domain": "example.com",
        "title": "Referral Serum",
        "image_url": "https://cdn.example.com/referral-serum.jpg",
        "price_amount": 25.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {
            "title": "Referral Serum",
            "description": "A helpful daily serum.",
            "image_url": "https://cdn.example.com/referral-serum.jpg",
            "snapshot": {
                "canonical_url": "https://example.com/en-us/product/referral-serum",
                "title": "Referral Serum",
                "description": "A helpful daily serum.",
                "extracted_at": _iso_days_ago(1),
            },
            "variants": [
                {
                    "variant_id": "v-1",
                    "sku": "SKU-1",
                    "title": "50ml",
                    "price_amount": 25.0,
                    "currency": "USD",
                    "availability": "in_stock",
                }
            ],
        },
        "status": "active",
        "notes": None,
        "created_by_employee_id": None,
        "attached_product_key": None,
        "attached_variant_id": None,
        "created_at": "2026-03-19T00:00:00+00:00",
        "updated_at": "2026-03-19T00:00:00+00:00",
        # A VERIFIED destination is now part of what "a healthy seed" means. The gate used to
        # infer freshness from `updated_at`, which any writer bumps, and passed a seed with no
        # timestamp at all — so a row nobody had ever fetched read as fresh. These three
        # columns are written only by an observation
        # (services/external_seed_destination_liveness); a default row therefore has to carry
        # one, and `_seed_row(destination_checked_at=None)` is how a test asks for the
        # never-verified case.
        "destination_checked_at": _iso_days_ago(1),
        "destination_http_status": 200,
        "destination_verdict": "live",
        "destination_failure_streak": 0,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_fetch_merchant_referral_inventory_collects_attached_and_domain_unattached(monkeypatch):
    from services import external_referral_readiness as module

    attached = _seed_row(id="eps_attached", attached_product_key="merch_1|shopify|prod_1")
    domain_only = _seed_row(id="eps_domain", domain="merchant.example")

    async def fake_fetch_all(query, values=None):
        sql = str(query)
        if "FROM merchant_stores" in sql:
            return [{"domain": "merchant.example"}]
        if "attached_product_key LIKE" in sql:
            return [attached]
        if "attached_product_key IS NULL" in sql:
            return [domain_only]
        return []

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    inventory = await module.fetch_merchant_referral_inventory(merchant_id="merch_1", status="active")

    assert inventory["matched_domains"] == ["merchant.example"]
    assert [row["id"] for row in inventory["rows"]] == ["eps_attached", "eps_domain"]
    assert inventory["matched_via_by_seed"]["eps_attached"] == "attached_product_key"
    assert inventory["matched_via_by_seed"]["eps_domain"] == "merchant_domain"


@pytest.mark.asyncio
async def test_evaluate_external_referral_seed_marks_blockers(monkeypatch):
    from services import external_referral_readiness as module

    stale_row = _seed_row(
        id="eps_stale",
        canonical_url="https://blocked.example/product",
        destination_url="https://blocked.example/product",
        # Staleness is now measured from the DESTINATION CHECK, not from `snapshot.extracted_at`
        # (and certainly not from `updated_at`): the question the gate asks is "when did we last
        # see this URL", which only an observation can answer.
        destination_checked_at=_iso_days_ago(30),
        seed_data={
            "title": "Blocked referral",
            "snapshot": {
                "canonical_url": "https://blocked.example/product",
                "title": "Blocked referral",
                "extracted_at": _iso_days_ago(30),
            },
            "variants": [],
        },
    )

    async def fake_allowed_domains(*, market: str):
        assert market == "US"
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)

    status = await module.evaluate_external_referral_seed(stale_row, matched_via="agent_api")

    assert status.status == "blocked"
    assert "stale_snapshot" in status.blocker_anomaly_types
    assert "zero_variants" in status.blocker_anomaly_types
    assert "destination_domain_not_allowed" in status.blocker_anomaly_types


@pytest.mark.asyncio
async def test_build_external_referral_summary_counts_statuses(monkeypatch):
    from services import external_referral_readiness as module

    blocked = _seed_row(
        id="eps_blocked",
        destination_checked_at=_iso_days_ago(30),
        seed_data={
            "title": "Blocked referral",
            "snapshot": {
                "canonical_url": "https://example.com/en-us/product/blocked",
                "title": "Blocked referral",
                "description": "Blocked referral",
                "extracted_at": _iso_days_ago(30),
            },
            "variants": [],
        },
    )
    review = _seed_row(
        id="eps_review",
        image_url=None,
        seed_data={
            "title": "Review referral",
            "description": "Experience the ultimate luxury with Review referral.",
            "snapshot": {
                "canonical_url": "https://example.com/en-us/product/review",
                "title": "Review referral",
                "description": "Experience the ultimate luxury with Review referral.",
                "extracted_at": _iso_days_ago(1),
            },
            "variants": [
                {
                    "variant_id": "v-1",
                    "sku": "SKU-2",
                    "title": "One size",
                    "price_amount": 22.0,
                    "currency": "USD",
                    "availability": "in_stock",
                }
            ],
        },
    )
    healthy = _seed_row(id="eps_healthy")

    async def fake_inventory(*, merchant_id: str, status: str):
        assert merchant_id == "merch_1"
        assert status == "active"
        return {
            "merchant_id": merchant_id,
            "matched_domains": ["example.com"],
            "attached_rows": [blocked],
            "domain_unattached_rows": [review, healthy],
            "rows": [blocked, review, healthy],
            "matched_via_by_seed": {
                "eps_blocked": "attached_product_key",
                "eps_review": "merchant_domain",
                "eps_healthy": "merchant_domain",
            },
        }

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "fetch_merchant_referral_inventory", fake_inventory)
    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)

    summary = await module.build_external_referral_summary("merch_1")

    assert summary["status"] == "red"
    assert summary["total_active_seeds"] == 3
    assert summary["blocked_seed_count"] == 1
    assert summary["review_seed_count"] == 1
    assert summary["healthy_seed_count"] == 1
    assert any(bucket["issue_type"] == "stale_snapshot" for bucket in summary["issue_buckets"])


@pytest.mark.asyncio
async def test_run_external_referral_refresh_batch_uses_candidate_order(monkeypatch):
    from services import external_referral_readiness as module

    async def fake_candidates(*, limit: int):
        assert limit == 5
        return ["eps_attached", "eps_domain"]

    refreshed = []

    async def fake_refresh(seed_id: str):
        refreshed.append(seed_id)
        return {"status": "success", "seed_id": seed_id}

    monkeypatch.setattr(module, "get_external_referral_refresh_candidate_seed_ids", fake_candidates)

    summary = await module.run_external_referral_refresh_batch(refresh_seed_by_id=fake_refresh, limit=5)

    assert summary["candidate_count"] == 2
    assert summary["refreshed"] == 2
    assert refreshed == ["eps_attached", "eps_domain"]


@pytest.mark.asyncio
async def test_build_platform_fallback_program_summary_counts_global_seed_health(monkeypatch):
    from services import external_referral_readiness as module

    blocked = _seed_row(
        id="eps_blocked",
        attached_product_key="merch_1|shopify|prod_1",
        domain="blocked.example",
        destination_checked_at=_iso_days_ago(30),
        seed_data={
            "title": "Blocked referral",
            "snapshot": {
                "canonical_url": "https://blocked.example/product/blocked",
                "title": "Blocked referral",
                "description": "Blocked referral",
                "extracted_at": _iso_days_ago(30),
            },
            "variants": [],
        },
    )
    review = _seed_row(
        id="eps_review",
        attached_product_key=None,
        domain="review.example",
        image_url=None,
        canonical_url="https://review.example/product/review",
        destination_url="https://review.example/product/review",
        seed_data={
            "title": "Review referral",
            "description": "Experience the ultimate luxury with Review referral.",
            "snapshot": {
                "canonical_url": "https://review.example/product/review",
                "title": "Review referral",
                "description": "Experience the ultimate luxury with Review referral.",
                "extracted_at": _iso_days_ago(1),
            },
            "variants": [
                {
                    "variant_id": "v-1",
                    "sku": "SKU-2",
                    "title": "One size",
                    "price_amount": 22.0,
                    "currency": "USD",
                    "availability": "in_stock",
                }
            ],
        },
    )
    healthy = _seed_row(
        id="eps_healthy",
        attached_product_key="merch_2|shopify|prod_2",
        domain="healthy.example",
        canonical_url="https://healthy.example/product/healthy",
        destination_url="https://healthy.example/product/healthy",
        seed_data={
            "title": "Healthy referral",
            "description": "A helpful daily serum.",
            "snapshot": {
                "canonical_url": "https://healthy.example/product/healthy",
                "title": "Healthy referral",
                "description": "A helpful daily serum.",
                "extracted_at": _iso_days_ago(1),
            },
            "variants": [
                {
                    "variant_id": "v-healthy",
                    "sku": "SKU-3",
                    "title": "Standard",
                    "price_amount": 25.0,
                    "currency": "USD",
                    "availability": "in_stock",
                }
            ],
        },
    )

    async def fake_rows():
        return [blocked, review, healthy]

    async def fake_allowed_domains(*, market: str):
        return ["blocked.example", "review.example", "healthy.example"]

    monkeypatch.setattr(module, "_fetch_all_active_referral_seed_rows", fake_rows)
    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)

    summary = await module.build_platform_fallback_program_summary()

    assert summary["status"] == "red"
    assert summary["total_active_seeds"] == 3
    assert summary["attached_seed_count"] == 2
    assert summary["unattached_seed_count"] == 1
    assert summary["blocked_seed_count"] == 1
    assert summary["review_seed_count"] == 1
    assert summary["runtime_surface_coverage_summary"]["total_surface_eligible_seeds"] == 2
    assert summary["top_domains"][0]["domain"] in {"blocked.example", "healthy.example", "review.example"}
    assert any(bucket["issue_type"] == "stale_snapshot" for bucket in summary["issue_buckets"])


@pytest.mark.asyncio
async def test_build_merchant_commerce_cohort_summary_uses_store_catalog_and_psp_prereqs(monkeypatch):
    from services import external_referral_readiness as module

    async def fake_get_all_merchant_onboardings(include_deleted: bool = False):
        assert include_deleted is False
        return [
            {
                "merchant_id": "merch_valid",
                "business_name": "Valid Merchant",
                "psp_connected": True,
                "psp_type": "stripe",
            },
            {
                "merchant_id": "merch_missing_psp",
                "business_name": "Needs PSP",
                "psp_connected": False,
                "psp_type": None,
            },
            {
                "merchant_id": "merch_missing_catalog",
                "business_name": "Needs Catalog",
                "psp_connected": True,
                "psp_type": "checkout",
            },
        ]

    async def fake_fetch_catalog_counts():
        return {
            "merch_valid": 740,
            "merch_missing_psp": 120,
            "merch_missing_catalog": 0,
        }

    async def fake_fetch_store_domains():
        return {
            "merch_valid": ["valid.example"],
            "merch_missing_psp": ["needs-psp.example"],
            "merch_missing_catalog": ["needs-catalog.example"],
        }

    async def fake_fetch_psps():
        return {
            "merch_valid": ["stripe"],
            "merch_missing_catalog": ["checkout"],
        }

    monkeypatch.setattr(module, "get_all_merchant_onboardings", fake_get_all_merchant_onboardings)
    monkeypatch.setattr(module, "_fetch_catalog_product_counts_by_merchant", fake_fetch_catalog_counts)
    monkeypatch.setattr(module, "_fetch_store_domains_by_merchant", fake_fetch_store_domains)
    monkeypatch.setattr(module, "_fetch_active_psp_providers_by_merchant", fake_fetch_psps)

    summary = await module.build_merchant_commerce_cohort_summary()

    assert summary["total_registered_merchants"] == 3
    assert summary["store_connected_merchants"] == 3
    assert summary["store_connected_with_psp_merchants"] == 2
    assert summary["merchant_valid_count"] == 1
    assert summary["merchant_invalid_count"] == 2
    assert summary["top_invalid_merchants"][0]["merchant_id"] == "merch_missing_psp"
    assert "missing_psp_or_checkout" in summary["top_invalid_merchants"][0]["invalid_reasons"]


@pytest.mark.asyncio
async def test_build_merchant_commerce_readiness_list_distinguishes_red_yellow_green(monkeypatch):
    from services import external_referral_readiness as module

    async def fake_get_all_merchant_onboardings(include_deleted: bool = False):
        assert include_deleted is False
        return [
            {
                "merchant_id": "merch_green",
                "business_name": "Green Merchant",
                "psp_connected": True,
                "psp_type": "stripe",
            },
            {
                "merchant_id": "merch_yellow",
                "business_name": "Yellow Merchant",
                "psp_connected": True,
                "psp_type": "checkout",
            },
            {
                "merchant_id": "merch_red",
                "business_name": "Red Merchant",
                "psp_connected": False,
                "psp_type": None,
            },
        ]

    async def fake_fetch_catalog_counts():
        return {
            "merch_green": 120,
            "merch_yellow": 85,
            "merch_red": 0,
        }

    async def fake_fetch_store_domains():
        return {
            "merch_green": ["green.example"],
            "merch_yellow": ["yellow.example"],
            "merch_red": [],
        }

    async def fake_fetch_psps():
        return {
            "merch_green": ["stripe"],
            "merch_yellow": ["checkout"],
        }

    async def fake_paid_evidence():
        return {
            "merch_green": {"paid_orders_last_30_days": 5, "paid_orders_all_time": 11},
            "merch_yellow": {"paid_orders_last_30_days": 0, "paid_orders_all_time": 0},
        }

    monkeypatch.setattr(module, "get_all_merchant_onboardings", fake_get_all_merchant_onboardings)
    monkeypatch.setattr(module, "_fetch_catalog_product_counts_by_merchant", fake_fetch_catalog_counts)
    monkeypatch.setattr(module, "_fetch_store_domains_by_merchant", fake_fetch_store_domains)
    monkeypatch.setattr(module, "_fetch_active_psp_providers_by_merchant", fake_fetch_psps)
    monkeypatch.setattr(module, "_fetch_paid_order_evidence_by_merchant", fake_paid_evidence)

    summary = await module.build_merchant_commerce_readiness_list()

    assert summary["total_registered_merchants"] == 3
    assert summary["merchant_valid_count"] == 2
    assert summary["rollout_ready_count"] == 1
    assert summary["attention_count"] == 2

    rows = {row["merchant_id"]: row for row in summary["merchants"]}
    assert rows["merch_green"]["status"] == "green"
    assert rows["merch_green"]["rollout_ready"] is True
    assert rows["merch_green"]["paid_orders_last_30_days"] == 5

    assert rows["merch_yellow"]["status"] == "yellow"
    assert rows["merch_yellow"]["merchant_valid"] is True
    assert rows["merch_yellow"]["rollout_ready"] is False
    assert rows["merch_yellow"]["invalid_reasons"] == []

    assert rows["merch_red"]["status"] == "red"
    assert rows["merch_red"]["merchant_valid"] is False
    assert "missing_store_domain" in rows["merch_red"]["invalid_reasons"]


# ---------------------------------------------------------------------------------------
# Drift aggregation. The PR that added these counters argues they are the whole
# justification for scheduling the sweep — "N re-crawled" says nothing about whether the
# index was WRONG — so the counters themselves need pinning, or the number the decision
# rests on is unverified.
# ---------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_batch_counts_each_drift_outcome_separately(monkeypatch):
    from services import external_referral_readiness as module

    outcomes = {
        "s_applied": "applied",
        "s_filled": "filled",
        "s_unchanged": "unchanged",
        "s_unavailable": "unavailable",
        "s_incomplete": "skipped_incomplete_pair",
        "s_mismatch": "skipped_currency_mismatch",
    }

    async def fake_candidates(*, limit: int):
        return list(outcomes)

    async def fake_refresh(seed_id: str):
        return {
            "status": "success",
            "seed_id": seed_id,
            "price_refresh": {"status": outcomes[seed_id]},
            # Only the 'applied' seed also flips stock, so the two counters cannot be
            # accidentally reading each other.
            "availability_refresh": {"status": "applied" if seed_id == "s_applied" else "unchanged"},
        }

    monkeypatch.setattr(module, "get_external_referral_refresh_candidate_seed_ids", fake_candidates)
    summary = await module.run_external_referral_refresh_batch(refresh_seed_by_id=fake_refresh, limit=50)

    assert summary["refreshed"] == 6
    assert summary["price_changed"] == 1, "only 'applied' is drift"
    assert summary["price_filled"] == 1
    assert summary["price_unchanged"] == 1
    assert summary["price_unavailable"] == 1
    assert summary["price_skipped_incomplete_pair"] == 1
    assert summary["price_skipped_currency_mismatch"] == 1
    assert summary["availability_changed"] == 1


@pytest.mark.asyncio
async def test_refresh_batch_survives_a_malformed_drift_report(monkeypatch):
    """A truthy non-dict `price_refresh` used to raise INSIDE the try, landing the same
    seed in BOTH `refreshed` and `failed` — a worse outcome than an uncounted report."""
    from services import external_referral_readiness as module

    async def fake_candidates(*, limit: int):
        return ["s_bad"]

    async def fake_refresh(seed_id: str):
        return {"status": "success", "seed_id": seed_id, "price_refresh": "not-a-dict"}

    monkeypatch.setattr(module, "get_external_referral_refresh_candidate_seed_ids", fake_candidates)
    summary = await module.run_external_referral_refresh_batch(refresh_seed_by_id=fake_refresh, limit=5)

    assert summary["refreshed"] == 1
    assert summary["failed"] == 0, "a seed must never be counted as both refreshed and failed"
    assert summary["status"] == "success"


@pytest.mark.asyncio
async def test_refresh_batch_tolerates_a_result_with_no_drift_report(monkeypatch):
    """Back-compat: a caller that predates the drift reports must still aggregate."""
    from services import external_referral_readiness as module

    async def fake_candidates(*, limit: int):
        return ["s_old"]

    async def fake_refresh(seed_id: str):
        return {"status": "success", "seed_id": seed_id}

    monkeypatch.setattr(module, "get_external_referral_refresh_candidate_seed_ids", fake_candidates)
    summary = await module.run_external_referral_refresh_batch(refresh_seed_by_id=fake_refresh, limit=5)

    assert summary["refreshed"] == 1
    assert summary["price_changed"] == 0
    assert summary["availability_changed"] == 0


# --- the destination gate: a fact, and the closed fail-open ------------------------------------

@pytest.mark.asyncio
async def test_a_never_verified_seed_is_blocked_rather_than_assumed_fresh(monkeypatch):
    """THE FAIL-OPEN, pinned.

    The staleness check used to read `if extracted_dt is not None and extracted_dt < cutoff`,
    and `get_last_extracted_at` fell back to `updated_at`. A row with no observation behind it
    therefore passed the gate — which is every row in the corpus, since nothing had ever
    re-read a destination. NULL now means "never verified" and blocks.
    """
    from services import external_referral_readiness as module

    row = _seed_row(id="eps_unverified", destination_checked_at=None)

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)
    status = await module.evaluate_external_referral_seed(row, matched_via="test")

    assert status.status == "blocked"
    assert "destination_never_verified" in status.blocker_anomaly_types


@pytest.mark.asyncio
async def test_bumping_updated_at_no_longer_makes_a_stale_seed_look_fresh(monkeypatch):
    """The wrong clock, pinned.

    `updated_at` moves whenever ANY writer touches the row — a console PATCH, a backfill, or a
    refresh whose fetch 404'd and fell back to the cached snapshot. Under the old gate that
    cleared `stale_snapshot` without anyone having loaded the page.
    """
    from datetime import datetime, timezone

    from services import external_referral_readiness as module

    row = _seed_row(
        id="eps_touched",
        updated_at=datetime.now(timezone.utc).isoformat(),
        seed_data={
            "title": "Referral Serum",
            "description": "A helpful daily serum.",
            "snapshot": {
                "canonical_url": "https://example.com/en-us/product/referral-serum",
                "title": "Referral Serum",
                "description": "A helpful daily serum.",
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            },
            "variants": [
                {
                    "variant_id": "v-1",
                    "sku": "SKU-1",
                    "title": "50ml",
                    "price_amount": 25.0,
                    "currency": "USD",
                    "availability": "in_stock",
                }
            ],
        },
        destination_checked_at=_iso_days_ago(30),
    )

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)
    status = await module.evaluate_external_referral_seed(row, matched_via="test")

    assert status.status == "blocked"
    # `destination_stale`, not `stale_snapshot`: these are now separate facts. This row's
    # CONTENT was extracted recently; what has gone stale is the LINK check. Reporting
    # `stale_snapshot` here would be answering the wrong question — and collapsing the two
    # is what would have let ~11.3k rows with a 99-day-old price serve the moment the first
    # destination sweep stamped them.
    assert "destination_stale" in status.blocker_anomaly_types
    assert "stale_snapshot" not in status.blocker_anomaly_types


@pytest.mark.asyncio
async def test_a_confirmed_dead_destination_blocks_on_the_fact_not_the_clock(monkeypatch):
    from services import external_referral_readiness as module

    row = _seed_row(
        id="eps_dead",
        destination_checked_at=_iso_days_ago(1),
        destination_verdict="dead_404",
        destination_http_status=404,
        destination_failure_streak=2,
    )

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)
    status = await module.evaluate_external_referral_seed(row, matched_via="test")

    assert status.status == "blocked"
    assert "destination_dead" in status.blocker_anomaly_types
    assert "stale_snapshot" not in status.blocker_anomaly_types, (
        "the seed was verified yesterday — it is dead, not stale, and the two need "
        "different operator actions"
    )


@pytest.mark.asyncio
async def test_one_dead_observation_is_not_enough_to_block(monkeypatch):
    """Same hysteresis as retirement. A single bad probe must not withdraw a live product."""
    from services import external_referral_readiness as module

    row = _seed_row(
        id="eps_maybe_dead",
        destination_checked_at=_iso_days_ago(1),
        destination_verdict="dead_404",
        destination_http_status=404,
        destination_failure_streak=1,
    )

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)
    status = await module.evaluate_external_referral_seed(row, matched_via="test")

    assert "destination_dead" not in status.blocker_anomaly_types
    assert status.status == "healthy"


@pytest.mark.asyncio
async def test_a_live_but_delisted_destination_still_serves(monkeypatch):
    """Absent from products.json is not a broken link — 26 of 490 delisted URLs were live."""
    from services import external_referral_readiness as module

    row = _seed_row(
        id="eps_delisted",
        destination_checked_at=_iso_days_ago(1),
        destination_verdict="live_delisted",
        destination_http_status=200,
        destination_failure_streak=0,
    )

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)
    status = await module.evaluate_external_referral_seed(row, matched_via="test")

    assert status.status == "healthy"


@pytest.mark.asyncio
async def test_an_unverifiable_destination_never_blocks_on_deadness(monkeypatch):
    """A bot challenge must not be able to withdraw a merchant's whole catalogue."""
    from services import external_referral_readiness as module

    row = _seed_row(
        id="eps_blind",
        destination_checked_at=_iso_days_ago(1),
        destination_verdict="unverifiable",
        destination_http_status=429,
        destination_failure_streak=99,
    )

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)
    status = await module.evaluate_external_referral_seed(row, matched_via="test")

    assert "destination_dead" not in status.blocker_anomaly_types
    assert status.status == "healthy"


@pytest.mark.asyncio
async def test_a_verified_link_does_not_buy_freshness_for_a_stale_price(monkeypatch):
    """THE DELAYED REGRESSION. Pinning the two facts apart.

    The first version of this change replaced the content-age check with the destination
    check, which reads as a strict improvement and is not one. The sweep stamps
    `destination_checked_at` from a catalogue read WITHOUT ever fetching a price — so on the
    day the first full pass completed, every row carrying a median-56-to-99-day-old price
    would have flipped from blocked to healthy. A serving regression created by the change
    meant to close one, and invisible to any measurement taken before the sweep ran.
    """
    from services import external_referral_readiness as module

    row = _seed_row(id="eps_link_ok_price_old")
    row["seed_data"]["snapshot"]["extracted_at"] = _iso_days_ago(400)
    row["destination_checked_at"] = _iso_days_ago(0)
    row["destination_verdict"] = "live"
    row["destination_failure_streak"] = 0

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)
    status = await module.evaluate_external_referral_seed(row, matched_via="test")

    assert status.status == "blocked"
    assert "stale_snapshot" in status.blocker_anomaly_types
    assert "destination_stale" not in status.blocker_anomaly_types
    assert "destination_never_verified" not in status.blocker_anomaly_types


@pytest.mark.asyncio
async def test_a_fresh_price_does_not_buy_a_pass_for_an_unverified_link(monkeypatch):
    """And the converse — otherwise the old fail-open comes back through the other door."""
    from services import external_referral_readiness as module

    row = _seed_row(id="eps_price_ok_link_never")
    row["seed_data"]["snapshot"]["extracted_at"] = _iso_days_ago(0)
    row["destination_checked_at"] = None

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)
    status = await module.evaluate_external_referral_seed(row, matched_via="test")

    assert status.status == "blocked"
    assert "destination_never_verified" in status.blocker_anomaly_types
    assert "stale_snapshot" not in status.blocker_anomaly_types


@pytest.mark.asyncio
async def test_a_seed_with_no_extraction_timestamp_at_all_is_blocked(monkeypatch):
    """The original fail-open, from the content side.

    The old check was guarded on `extracted_dt is not None`, so a row with no extraction
    recorded passed. `get_content_extracted_at` has no `updated_at` fallback, so the absence
    is now visible — and it must read as a blocker, not as a pass.
    """
    from services import external_referral_readiness as module

    row = _seed_row(id="eps_no_extraction")
    row["seed_data"]["snapshot"].pop("extracted_at", None)
    row["destination_checked_at"] = _iso_days_ago(0)
    row["destination_verdict"] = "live"

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)
    status = await module.evaluate_external_referral_seed(row, matched_via="test")

    assert status.status == "blocked"
    assert "stale_snapshot" in status.blocker_anomaly_types


@pytest.mark.asyncio
async def test_both_clocks_fresh_and_the_link_alive_serves(monkeypatch):
    """The lane has to be able to reach healthy, or the gate is just an off switch."""
    from services import external_referral_readiness as module

    row = _seed_row(id="eps_all_good")
    row["seed_data"]["snapshot"]["extracted_at"] = _iso_days_ago(1)
    row["destination_checked_at"] = _iso_days_ago(1)
    row["destination_verdict"] = "live"
    row["destination_failure_streak"] = 0

    async def fake_allowed_domains(*, market: str):
        return ["example.com"]

    monkeypatch.setattr(module, "get_allowed_domains_for_market", fake_allowed_domains)
    status = await module.evaluate_external_referral_seed(row, matched_via="test")

    assert status.status == "healthy", status.blocker_anomaly_types
