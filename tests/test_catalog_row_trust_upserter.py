"""Unit tests for ``services.catalog_row_trust_upserter``.

Verifies the I/O wrapper logic without standing up Postgres: the helper
fetches the joined input via :class:`FakeDb`, computes via the real
``derive_trust``, and writes UPSERT params. The policy itself is covered by
the parity suite in test_catalog_trust_policy.py — these tests cover the
glue (row reshape, fire-and-forget contract, quarantine variants).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.catalog_row_trust_upserter import (
    upsert_catalog_row_trust,
    upsert_catalog_row_trust_for_quarantine_match,
    upsert_catalog_row_trust_many,
)


NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeDb:
    """Records every fetch/execute the upserter performs and returns
    canned rows. Same surface as the encode/databases async client used in
    pivota-backend (``fetch_one``, ``fetch_all``, ``execute``).
    """

    def __init__(self, *, joined_rows=None, quarantines=None):
        self._joined_rows = joined_rows or []
        self._quarantines = quarantines or []
        self.executes: list[tuple[str, dict]] = []

    async def fetch_one(self, query, values=None):
        if "catalog_source_quarantine" in query:
            return self._quarantines[0] if self._quarantines else None
        # otherwise the product-by-key driver.
        if not self._joined_rows:
            return None
        pk = (values or {}).get("product_key")
        for row in self._joined_rows:
            if row["product_key"] == pk:
                return row
        return None

    async def fetch_all(self, query, values=None):
        if "catalog_source_quarantine" in query and "FROM catalog_source_quarantine" in query:
            return list(self._quarantines)
        # else: products driver — return joined rows that match the
        # values filter for our toy predicates.
        values = values or {}
        if "product_key = ANY" in query:
            keys = set(values.get("product_keys") or [])
            matched = [r for r in self._joined_rows if r.get("product_key") in keys]
            # Honor the SQL's ORDER BY product_key + LIMIT :limit. The real
            # query truncates — a fake that returns everything masked the
            # bulk-truncation bug (806 rows starved in prod for weeks).
            matched.sort(key=lambda r: str(r.get("product_key")))
            fetch_limit = values.get("limit")
            if fetch_limit is not None:
                matched = matched[: int(fetch_limit)]
            return matched
        if "lower(coalesce" in query:
            target = str(values.get("match_value") or "").lower()
            return [
                r
                for r in self._joined_rows
                if str(
                    r.get("source_domain")
                    or r.get("eps_domain")
                    or r.get("ms_domain")
                    or ""
                ).lower()
                == target
            ]
        if "merchant_id || ':' || cp.platform" in query or "cp.merchant_id || ':' || cp.platform" in query:
            target = values.get("match_value")
            return [
                r
                for r in self._joined_rows
                if f"{r.get('merchant_id')}:{r.get('platform')}" == target
            ]
        if "source_system || ':' || cp.source_ref" in query or "cp.source_system || ':' || cp.source_ref" in query:
            target = values.get("match_value")
            return [
                r
                for r in self._joined_rows
                if f"{r.get('source_system')}:{r.get('source_ref')}" == target
            ]
        return list(self._joined_rows)

    async def execute(self, query, values=None):
        self.executes.append((query, dict(values or {})))
        return None


def make_joined_row(**overrides):
    base = {
        "product_key": "pk_internal_1",
        "content_key": "ck_internal_1",
        "merchant_id": "merch_efbc46b4619cfbdf",
        "platform": "shopify",
        "source_system": "shopify",
        "source_ref": "gid://shopify/Product/1",
        "source_product_id": "1",
        "source_domain": "chydan.myshopify.com",
        "sync_status": "live",
        "suppression_reason": None,
        "last_seen_in_sync_at": NOW,
        # c1.v0.5 renderability input. The real product join ALWAYS selects
        # this column, so the fixture must too — a fake that omits it would
        # silently exercise the tri-state "absent" path and mask the gate.
        "pdp_seed_route_ok": True,
        # IPS
        "serving_eligible": True,
        "pipeline_stage": "serving",
        "blocker_code": None,
        "content_quality_score": 0.8,
        "quality_scored_at": NOW,
        "last_extracted_at": NOW,
        # Identity (approved/healthy)
        "pil_source_listing_ref": "merch_efbc46b4619cfbdf:1",
        "identity_status": "approved",
        "identity_confidence": 0.95,
        "live_read_enabled": True,
        "review_required": False,
        "sellable_item_group_id": "sig_abc",
        "product_line_id": "pl_x",
        "review_family_id": "rf_x",
        # External seed (none for internal merchant)
        "eps_id": None,
        "eps_status": None,
        "eps_domain": None,
        "eps_attached_product_key": None,
        "eps_last_seen_at": None,
        # Merchant store
        "ms_merchant_id": "merch_efbc46b4619cfbdf",
        "ms_platform": "shopify",
        "ms_domain": "chydan.myshopify.com",
        "ms_status": "active",
        "ms_last_sync": NOW,
        # Override
        "override_id": None,
        "override_action_type": None,
        "override_active": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_product_upsert_emits_public_for_healthy_row():
    db = FakeDb(joined_rows=[make_joined_row()])

    ok = await upsert_catalog_row_trust(db=db, product_key="pk_internal_1", now=NOW)

    assert ok is True
    assert len(db.executes) == 1
    _, params = db.executes[0]
    assert params["subject_type"] == "product"
    assert params["subject_key"] == "pk_internal_1"
    assert params["serving_decision"] == "public"
    assert params["policy_version"]  # not empty


@pytest.mark.asyncio
async def test_missing_product_key_skips_silently():
    db = FakeDb()
    ok = await upsert_catalog_row_trust(db=db, product_key="", now=NOW)
    assert ok is False
    assert db.executes == []


@pytest.mark.asyncio
async def test_product_not_found_skips_silently():
    db = FakeDb(joined_rows=[make_joined_row()])
    ok = await upsert_catalog_row_trust(db=db, product_key="pk_does_not_exist", now=NOW)
    assert ok is False
    assert db.executes == []


@pytest.mark.asyncio
async def test_db_error_is_swallowed_not_raised():
    class BadDb:
        async def fetch_one(self, *_a, **_k):
            raise RuntimeError("db down")

        async def fetch_all(self, *_a, **_k):
            return []

        async def execute(self, *_a, **_k):
            return None

    ok = await upsert_catalog_row_trust(db=BadDb(), product_key="pk", now=NOW)
    assert ok is False  # logged, never raises


@pytest.mark.asyncio
async def test_bulk_upserts_all_supplied_keys():
    rows = [
        make_joined_row(product_key="pk_a"),
        make_joined_row(product_key="pk_b"),
        make_joined_row(product_key="pk_c"),
    ]
    db = FakeDb(joined_rows=rows)

    wrote = await upsert_catalog_row_trust_many(
        db=db, product_keys=["pk_a", "pk_b", "pk_c"], now=NOW
    )

    assert wrote == 3
    subjects = sorted(p["subject_key"] for _, p in db.executes)
    assert subjects == ["pk_a", "pk_b", "pk_c"]


@pytest.mark.asyncio
async def test_bulk_processes_every_key_beyond_one_fetch_batch():
    """Regression: the bulk upserter must cover ALL supplied keys, not just
    the first fetch-batch. Pre-fix, a single ``ORDER BY product_key LIMIT``
    fetch dropped every key past ``limit`` — deterministically the same
    alphabetical winners each cron run, so the tail never got a trust row."""
    rows = [make_joined_row(product_key=f"pk_{i:03d}") for i in range(7)]
    db = FakeDb(joined_rows=rows)

    wrote = await upsert_catalog_row_trust_many(
        db=db,
        product_keys=[f"pk_{i:03d}" for i in range(7)],
        now=NOW,
        limit=3,  # force 3 fetch batches: 3 + 3 + 1
    )

    assert wrote == 7
    subjects = sorted(p["subject_key"] for _, p in db.executes)
    assert subjects == [f"pk_{i:03d}" for i in range(7)]


@pytest.mark.asyncio
async def test_bulk_batches_follow_caller_order_not_alphabetical():
    """The cron passes keys stalest-first; batching must chunk that order,
    not re-sort globally. With limit=1 each batch is one key, so the write
    sequence IS the batch sequence."""
    rows = [
        make_joined_row(product_key="pk_zzz_stalest"),
        make_joined_row(product_key="pk_aaa_freshest"),
    ]
    db = FakeDb(joined_rows=rows)

    wrote = await upsert_catalog_row_trust_many(
        db=db,
        product_keys=["pk_zzz_stalest", "pk_aaa_freshest"],
        now=NOW,
        limit=1,
    )

    assert wrote == 2
    write_order = [p["subject_key"] for _, p in db.executes]
    assert write_order == ["pk_zzz_stalest", "pk_aaa_freshest"]


@pytest.mark.asyncio
async def test_bulk_failed_batch_does_not_abort_remaining_batches():
    class FlakyDb(FakeDb):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.any_fetches = 0

        async def fetch_all(self, query, values=None):
            if "product_key = ANY" in query:
                self.any_fetches += 1
                if self.any_fetches == 1:
                    raise RuntimeError("transient db error")
            return await super().fetch_all(query, values)

    rows = [
        make_joined_row(product_key="pk_batch1"),
        make_joined_row(product_key="pk_batch2"),
    ]
    db = FlakyDb(joined_rows=rows)

    wrote = await upsert_catalog_row_trust_many(
        db=db,
        product_keys=["pk_batch1", "pk_batch2"],
        now=NOW,
        limit=1,
    )

    assert wrote == 1  # batch 1 lost (logged), batch 2 still processed
    assert [p["subject_key"] for _, p in db.executes] == ["pk_batch2"]


@pytest.mark.asyncio
async def test_quarantine_domain_match_recomputes_all_affected_rows():
    rows = [
        make_joined_row(product_key="pk_chydan_1", source_domain="chydan.myshopify.com"),
        make_joined_row(product_key="pk_chydan_2", source_domain="chydan.myshopify.com"),
        make_joined_row(product_key="pk_other", source_domain="theordinary.com"),
    ]
    quarantines = [
        {
            "quarantine_id": 99,
            "match_type": "domain",
            "match_value": "chydan.myshopify.com",
            "state": "active",
            "expires_at": None,
        }
    ]
    db = FakeDb(joined_rows=rows, quarantines=quarantines)

    wrote = await upsert_catalog_row_trust_for_quarantine_match(
        db=db,
        match_type="domain",
        match_value="chydan.myshopify.com",
        now=NOW,
    )

    assert wrote == 2  # only the two chydan rows match the predicate
    # Both chydan rows must now flip to blocked with SOURCE_QUARANTINED.
    chydan_writes = [p for _, p in db.executes if p["subject_key"].startswith("pk_chydan_")]
    assert len(chydan_writes) == 2
    for p in chydan_writes:
        assert p["serving_decision"] == "blocked"
        assert p["source_lifecycle_state"] == "quarantined"
        assert "SOURCE_QUARANTINED" in p["serving_reason_codes"]


@pytest.mark.asyncio
async def test_quarantine_unknown_match_type_logs_and_returns_zero():
    db = FakeDb(joined_rows=[make_joined_row()])
    wrote = await upsert_catalog_row_trust_for_quarantine_match(
        db=db,
        match_type="bogus_type",
        match_value="x",
        now=NOW,
    )
    assert wrote == 0
    assert db.executes == []


@pytest.mark.asyncio
async def test_tombstoned_product_writes_blocked_decision():
    db = FakeDb(
        joined_rows=[make_joined_row(suppression_reason="stale_after_sync")],
    )
    ok = await upsert_catalog_row_trust(db=db, product_key="pk_internal_1", now=NOW)
    assert ok is True
    _, params = db.executes[0]
    assert params["serving_decision"] == "blocked"
    assert params["source_lifecycle_state"] == "tombstoned"
    assert "ROW_TOMBSTONED" in params["serving_reason_codes"]


# ---- c1.v0.5 renderability input threading ---------------------------------


@pytest.mark.asyncio
async def test_seed_route_column_reaches_the_policy_as_a_lane_aware_answer(
    monkeypatch,
):
    """The SQL column is the raw seed EXISTS; the LANE test is applied in
    Python. A shopify row is merchant-synced, so it stays renderable even when
    no seed answers — asserting the two halves are actually wired together and
    not, say, passing the raw EXISTS straight through."""
    monkeypatch.setenv("CATALOG_TRUST_RENDERABLE_GATE", "1")
    db = FakeDb(joined_rows=[make_joined_row(pdp_seed_route_ok=False)])

    ok = await upsert_catalog_row_trust(db=db, product_key="pk_internal_1", now=NOW)

    assert ok is True
    _, params = db.executes[0]
    assert params["serving_decision"] == "public"


@pytest.mark.asyncio
async def test_path_c_minted_row_with_no_seed_route_is_blocked_when_gated(
    monkeypatch,
):
    """THE 1,375. A catalog_enrichment_agent_v1 row's seed attaches by
    attached_product_key, so nothing answers on its source_product_id and the
    PDP hard-500s."""
    monkeypatch.setenv("CATALOG_TRUST_RENDERABLE_GATE", "1")
    db = FakeDb(
        joined_rows=[
            make_joined_row(
                merchant_id="external_seed",
                platform="external_seed",
                source_system="catalog_enrichment_agent_v1",
                source_product_id="tower-28-beauty-sunnydays-tinted-spf-30",
                pdp_seed_route_ok=False,
                eps_id=1,
                eps_status="active",
                ms_merchant_id=None,
                ms_platform=None,
                ms_domain=None,
                ms_status=None,
                ms_last_sync=None,
            )
        ]
    )

    ok = await upsert_catalog_row_trust(db=db, product_key="pk_internal_1", now=NOW)

    assert ok is True
    _, params = db.executes[0]
    assert params["serving_decision"] == "blocked"
    assert "PDP_ROUTE_UNRESOLVABLE" in params["serving_reason_codes"]


@pytest.mark.asyncio
async def test_path_c_minted_row_stays_public_with_the_gate_off(monkeypatch):
    """Same row, flag OFF: unchanged from c1.v0.4. This is what prod does today."""
    monkeypatch.delenv("CATALOG_TRUST_RENDERABLE_GATE", raising=False)
    db = FakeDb(
        joined_rows=[
            make_joined_row(
                merchant_id="external_seed",
                platform="external_seed",
                source_system="catalog_enrichment_agent_v1",
                source_product_id="tower-28-beauty-sunnydays-tinted-spf-30",
                pdp_seed_route_ok=False,
                eps_id=1,
                eps_status="active",
                ms_merchant_id=None,
                ms_platform=None,
                ms_domain=None,
                ms_status=None,
                ms_last_sync=None,
            )
        ]
    )

    ok = await upsert_catalog_row_trust(db=db, product_key="pk_internal_1", now=NOW)

    assert ok is True
    _, params = db.executes[0]
    assert params["serving_decision"] == "public"
