from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from db.database import database
from services import index_pipeline_state_service as svc


_PREFIX = "vis2"


async def _connect_if_needed() -> bool:
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    return was_connected


async def _ensure_schema() -> None:
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS catalog_products (
            product_key TEXT PRIMARY KEY,
            merchant_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            source_product_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            brand TEXT,
            pdp_scope TEXT DEFAULT 'unverified',
            sync_status TEXT DEFAULT 'live',
            canonical_url TEXT,
            pivota_signature_id TEXT,
            content_key TEXT,
            updated_at TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS product_quality_snapshot (
            id INTEGER PRIMARY KEY,
            merchant_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            platform_product_id TEXT NOT NULL,
            content_quality_score REAL,
            model_readiness_score REAL,
            conversion_potential_score REAL,
            rules_version TEXT,
            snapshot_date TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_pdp_view (
            content_key TEXT PRIMARY KEY,
            pivota_signature_id TEXT,
            title TEXT NOT NULL,
            description TEXT,
            image_url TEXT,
            sync_status TEXT,
            pdp_lifecycle_stage TEXT,
            refreshed_at TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS external_product_seeds (
            id TEXT PRIMARY KEY,
            external_product_id TEXT,
            market TEXT NOT NULL DEFAULT 'US',
            destination_url TEXT NOT NULL DEFAULT 'https://example.test',
            canonical_url TEXT,
            domain TEXT,
            title TEXT,
            seed_data TEXT NOT NULL DEFAULT '{}',
            attached_product_key TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            updated_at TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS catalog_offers (
            offer_id TEXT PRIMARY KEY,
            sku_key TEXT,
            product_key TEXT,
            merchant_id TEXT,
            list_price REAL,
            currency TEXT DEFAULT 'USD',
            suppressed_at TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS external_offer_snapshots (
            id TEXT PRIMARY KEY,
            market TEXT NOT NULL DEFAULT 'US',
            canonical_url TEXT,
            url_hash TEXT,
            domain TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS product_group_members (
            product_group_id TEXT,
            merchant_id TEXT,
            platform TEXT,
            platform_product_id TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS domain_extractor_baselines (
            domain TEXT PRIMARY KEY,
            alert_state TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS index_pipeline_state (
            content_key TEXT PRIMARY KEY,
            pivota_signature_id TEXT,
            merchant_id TEXT,
            pipeline_stage TEXT NOT NULL DEFAULT 'discovered',
            blocker_code TEXT NOT NULL DEFAULT 'none',
            blocker_detail TEXT,
            serving_eligible BOOLEAN NOT NULL DEFAULT FALSE,
            index_eligible BOOLEAN NOT NULL DEFAULT FALSE,
            content_quality_score REAL,
            model_readiness_score REAL,
            conversion_potential_score REAL,
            quality_rules_version TEXT,
            quality_scored_at TIMESTAMP,
            extraction_content_hash TEXT,
            extractor_version TEXT,
            last_extracted_at TIMESTAMP,
            seed_audit_status TEXT,
            identity_resolved BOOLEAN NOT NULL DEFAULT FALSE,
            product_group_id TEXT,
            has_image BOOLEAN NOT NULL DEFAULT FALSE,
            has_price BOOLEAN NOT NULL DEFAULT FALSE,
            description_length INTEGER,
            consolidation_version TEXT,
            last_consolidated_at TIMESTAMP
        )
        """,
    ]
    for statement in ddl:
        await database.execute(statement)
    offer_columns = await database.fetch_all("PRAGMA table_info(catalog_offers)")
    if "suppressed_at" not in {dict(row).get("name") for row in offer_columns}:
        await database.execute("ALTER TABLE catalog_offers ADD COLUMN suppressed_at TIMESTAMP")
    # has_us_offer is derived from currency; backstop for test DBs created before
    # the column existed (mirrors the suppressed_at backstop above).
    if "currency" not in {dict(row).get("name") for row in offer_columns}:
        await database.execute(
            "ALTER TABLE catalog_offers ADD COLUMN currency TEXT DEFAULT 'USD'"
        )
    # Row suppression: backstop for test DBs created before
    # catalog_products.suppressed_at existed (mirrors the offer backstop above).
    cp_columns = await database.fetch_all("PRAGMA table_info(catalog_products)")
    if "suppressed_at" not in {dict(row).get("name") for row in cp_columns}:
        await database.execute(
            "ALTER TABLE catalog_products ADD COLUMN suppressed_at TIMESTAMP"
        )
    # ADR-008 SLICE 1: backstop for test DBs created before index_eligible
    # existed (mirrors the suppressed_at backstop above).
    ips_columns = await database.fetch_all("PRAGMA table_info(index_pipeline_state)")
    if "index_eligible" not in {dict(row).get("name") for row in ips_columns}:
        await database.execute(
            "ALTER TABLE index_pipeline_state "
            "ADD COLUMN index_eligible BOOLEAN NOT NULL DEFAULT FALSE"
        )


async def _cleanup() -> None:
    statements = [
        "DELETE FROM index_pipeline_state WHERE content_key LIKE 'ck_vis2_%%'",
        "DELETE FROM agent_pdp_view WHERE content_key LIKE 'ck_vis2_%%'",
        "DELETE FROM catalog_offers WHERE product_key LIKE 'pk_vis2_%%'",
        "DELETE FROM external_product_seeds WHERE id LIKE 'seed_vis2_%%' OR attached_product_key LIKE 'pk_vis2_%%'",
        "DELETE FROM product_group_members WHERE merchant_id LIKE 'm_vis2_%%'",
        "DELETE FROM product_quality_snapshot WHERE merchant_id LIKE 'm_vis2_%%'",
        "DELETE FROM external_offer_snapshots WHERE canonical_url LIKE 'https://vis2-%%'",
        "DELETE FROM domain_extractor_baselines WHERE domain LIKE 'vis2-%%'",
        "DELETE FROM catalog_products WHERE content_key LIKE 'ck_vis2_%%' OR product_key LIKE 'pk_vis2_%%'",
    ]
    for statement in statements:
        await database.execute(statement)


async def _prepare_db() -> bool:
    was_connected = await _connect_if_needed()
    await _ensure_schema()
    await _cleanup()
    return was_connected


async def _disconnect_if_needed(was_connected: bool) -> None:
    if not was_connected and database.is_connected:
        await database.disconnect()


def _ids(suffix: str) -> Dict[str, str]:
    return {
        "content_key": f"ck_vis2_{suffix}",
        "product_key": f"pk_vis2_{suffix}",
        "merchant_id": f"m_vis2_{suffix}",
        "source_product_id": f"sp_vis2_{suffix}",
        "seed_id": f"seed_vis2_{suffix}",
        "domain": f"vis2-{suffix}.example",
        "signature_id": f"sig_vis2_{suffix}",
    }


def _numeric_id(suffix: str, *, offset: int = 900_000_000) -> int:
    return offset + sum((idx + 1) * ord(char) for idx, char in enumerate(suffix))


async def _insert_product(
    suffix: str,
    *,
    sync_status: str = "live",
    seed: bool = True,
    seed_title: Optional[str] = "VIS2 Product",
    seed_review_status: str = "no_issues_found",
    apv: bool = True,
    apv_title: str = "VIS2 Product",
    description: str = "A detailed product description long enough for serving eligibility.",
    image_url: Optional[str] = "https://cdn.example/vis2.jpg",
    quality_score: Optional[float] = 82.0,
    has_price: bool = True,
    identity: bool = True,
    pdp_scope: str = "merchant_owned",
    regression: bool = False,
    suppressed_at: Optional[datetime] = None,
) -> Dict[str, str]:
    ids = _ids(suffix)
    now = datetime.now(timezone.utc)
    canonical_url = f"https://{ids['domain']}/products/{suffix}"

    await database.execute(
        """
        INSERT INTO catalog_products (
            product_key, merchant_id, platform, source_product_id, title,
            description, brand, pdp_scope, sync_status, canonical_url,
            pivota_signature_id, content_key, updated_at, suppressed_at
        ) VALUES (
            :product_key, :merchant_id, 'shopify', :source_product_id, 'VIS2 Product',
            :description, 'VIS2', :pdp_scope, :sync_status, :canonical_url,
            :signature_id, :content_key, :updated_at, :suppressed_at
        )
        """,
        {
            "product_key": ids["product_key"],
            "merchant_id": ids["merchant_id"],
            "source_product_id": ids["source_product_id"],
            "signature_id": ids["signature_id"],
            "content_key": ids["content_key"],
            "description": description,
            "pdp_scope": pdp_scope,
            "sync_status": sync_status,
            "canonical_url": canonical_url,
            "updated_at": now,
            "suppressed_at": suppressed_at,
        },
    )

    if seed:
        seed_data: Dict[str, Any] = {
            "title": seed_title,
            "review_summary": {"review_status": seed_review_status},
        }
        await database.execute(
            """
            INSERT INTO external_product_seeds (
                id, external_product_id, market, destination_url, canonical_url,
                domain, title, seed_data, attached_product_key, status, updated_at
            ) VALUES (
                :seed_id, :seed_id, 'US', :canonical_url, :canonical_url,
                :domain, :seed_title, :seed_data, :product_key, 'active', :updated_at
            )
            """,
            {
                "seed_id": ids["seed_id"],
                "product_key": ids["product_key"],
                "domain": ids["domain"],
                "canonical_url": canonical_url,
                "seed_title": seed_title,
                "seed_data": json.dumps(seed_data),
                "updated_at": now,
            },
        )

    if apv:
        await database.execute(
            """
            INSERT INTO agent_pdp_view (
                content_key, pivota_signature_id, title, description, image_url,
                sync_status, pdp_lifecycle_stage, refreshed_at
            ) VALUES (
                :content_key, :signature_id, :apv_title, :description, :image_url,
                :sync_status, 'published', :refreshed_at
            )
            """,
            {
                "content_key": ids["content_key"],
                "signature_id": ids["signature_id"],
                "apv_title": apv_title,
                "description": description,
                "image_url": image_url,
                "sync_status": sync_status,
                "refreshed_at": now,
            },
        )

    if quality_score is not None:
        await database.execute(
            """
            INSERT INTO product_quality_snapshot (
                id, merchant_id, platform, platform_product_id, content_quality_score,
                model_readiness_score, conversion_potential_score, rules_version, snapshot_date
            ) VALUES (
                :snapshot_id, :merchant_id, 'shopify', :source_product_id, :quality_score,
                72.0, NULL, 'v1-lite', :snapshot_date
            )
            """,
            {
                "merchant_id": ids["merchant_id"],
                "source_product_id": ids["source_product_id"],
                "snapshot_id": _numeric_id(suffix),
                "quality_score": quality_score,
                "snapshot_date": now,
            },
        )

    if has_price:
        await database.execute(
            """
            -- currency is set EXPLICITLY, not left to a DDL default: in a
            -- full-suite run catalog_offers is created from the SQLAlchemy
            -- metadata in db/catalog.py (currency String(16), no server_default),
            -- so the CREATE TABLE IF NOT EXISTS below no-ops and a NULL currency
            -- would make has_us_offer false for every baseline row.
            INSERT INTO catalog_offers (
                offer_id, sku_key, product_key, merchant_id, list_price, currency
            ) VALUES (
                :offer_id, :sku_key, :product_key, :merchant_id, 19.99, 'USD'
            )
            """,
            {
                "product_key": ids["product_key"],
                "merchant_id": ids["merchant_id"],
                "offer_id": f"offer_{ids['product_key']}",
                "sku_key": f"sku_{ids['product_key']}",
            },
        )

    if identity:
        await database.execute(
            """
            INSERT INTO product_group_members (
                product_group_id, merchant_id, platform, platform_product_id
            ) VALUES (
                :product_group_id, :merchant_id, 'shopify', :source_product_id
            )
            """,
            {
                "merchant_id": ids["merchant_id"],
                "source_product_id": ids["source_product_id"],
                "product_group_id": f"pg_{ids['content_key']}",
            },
        )

    await database.execute(
        """
        INSERT INTO external_offer_snapshots (id, market, canonical_url, url_hash, domain)
        VALUES (:snapshot_id, 'US', :canonical_url, :url_hash, :domain)
        """,
        {
            "snapshot_id": f"eos_{ids['content_key']}",
            "canonical_url": canonical_url,
            "url_hash": f"hash_{ids['content_key']}",
            "domain": ids["domain"],
        },
    )

    if regression:
        await database.execute(
            """
            INSERT INTO domain_extractor_baselines (domain, alert_state)
            VALUES (:domain, 'regression')
            """,
            {"domain": ids["domain"]},
        )

    return ids


async def _ips_row(content_key: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT serving_eligible, index_eligible, blocker_code, pipeline_stage,
               content_quality_score
        FROM index_pipeline_state
        WHERE content_key = :content_key
        """,
        {"content_key": content_key},
    )
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_recompute_happy_path_upserts_serving_eligible_true() -> None:
    was_connected = await _prepare_db()
    try:
        ids = await _insert_product("happy")

        result = await svc.recompute_serving_eligibility(
            ids["content_key"],
            reason="unit",
        )

        row = await _ips_row(ids["content_key"])
        assert result is True
        assert row is not None
        assert bool(row["serving_eligible"]) is True
        assert row["blocker_code"] == "none"
        assert row["pipeline_stage"] == "public_indexed"
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("suffix", "overrides", "expected_blocker"),
    [
        ("not_live", {"sync_status": "archived"}, "not_live"),
        ("no_seed", {"seed": False, "apv": False}, "no_seed"),
        ("low_quality", {"quality_score": 64.9}, "low_quality"),
        ("no_image", {"image_url": ""}, "no_image"),
        ("no_price", {"has_price": False}, "no_price"),
        ("short_desc", {"description": "too short"}, "short_description"),
        (
            "entity_unresolved",
            {"identity": False, "pdp_scope": "unverified"},
            "entity_unresolved",
        ),
        (
            "seed_audit_fail",
            {"seed_review_status": "issues_unresolved"},
            "seed_audit_fail",
        ),
        ("extractor_regression", {"regression": True}, "extractor_regression"),
        (
            "suppressed",
            {"suppressed_at": datetime(2026, 7, 1, tzinfo=timezone.utc)},
            "suppressed",
        ),
    ],
)
async def test_recompute_blocker_transitions(
    suffix: str,
    overrides: Dict[str, Any],
    expected_blocker: str,
) -> None:
    was_connected = await _prepare_db()
    try:
        ids = await _insert_product(suffix, **overrides)

        result = await svc.recompute_serving_eligibility(ids["content_key"])

        row = await _ips_row(ids["content_key"])
        assert result is False
        assert row is not None
        assert bool(row["serving_eligible"]) is False
        assert row["blocker_code"] == expected_blocker
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
async def test_suppressed_row_clears_index_eligible_too() -> None:
    """Suppression is an editorial withdrawal: it must fail BOTH gates —
    serving_eligible (commerce) and index_eligible (offer-free citation)."""
    was_connected = await _prepare_db()
    try:
        ids = await _insert_product(
            "suppboth",
            suppressed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )

        result = await svc.recompute_serving_eligibility(ids["content_key"])

        row = await _ips_row(ids["content_key"])
        assert result is False
        assert row is not None
        assert bool(row["serving_eligible"]) is False
        assert bool(row["index_eligible"]) is False
        assert row["blocker_code"] == "suppressed"
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
async def test_recompute_is_idempotent() -> None:
    was_connected = await _prepare_db()
    try:
        ids = await _insert_product("idempotent")

        first = await svc.recompute_serving_eligibility(ids["content_key"])
        second = await svc.recompute_serving_eligibility(ids["content_key"])

        row = await _ips_row(ids["content_key"])
        assert first is True
        assert second is True
        assert row is not None
        assert bool(row["serving_eligible"]) is True
        assert row["blocker_code"] == "none"
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
async def test_recompute_handles_concurrent_partial_quality_data() -> None:
    was_connected = await _prepare_db()
    try:
        ids = await _insert_product("partial_quality", quality_score=None)

        first = await svc.recompute_serving_eligibility(ids["content_key"])
        first_row = await _ips_row(ids["content_key"])

        await database.execute(
            """
            INSERT INTO product_quality_snapshot (
                id, merchant_id, platform, platform_product_id, content_quality_score,
                model_readiness_score, conversion_potential_score, rules_version, snapshot_date
            ) VALUES (
                :snapshot_id, :merchant_id, 'shopify', :source_product_id, 82.0,
                72.0, NULL, 'v1-lite', :snapshot_date
            )
            """,
            {
                "merchant_id": ids["merchant_id"],
                "source_product_id": ids["source_product_id"],
                "snapshot_id": _numeric_id("partial_quality_second"),
                "snapshot_date": datetime.now(timezone.utc),
            },
        )

        second = await svc.recompute_serving_eligibility(ids["content_key"])
        second_row = await _ips_row(ids["content_key"])

        assert first is False
        assert first_row is not None
        assert first_row["blocker_code"] == "low_quality"
        assert second is True
        assert second_row is not None
        assert bool(second_row["serving_eligible"]) is True
        assert second_row["blocker_code"] == "none"
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
async def test_recompute_failure_mode_returns_false_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_fetch_all(*_args: Any, **_kwargs: Any) -> list:
        raise RuntimeError("database unavailable")

    caplog.set_level(logging.WARNING, logger="services.index_pipeline_state_service")
    monkeypatch.setattr(svc.database, "fetch_all", fail_fetch_all)

    result = await svc.recompute_serving_eligibility("ck_vis2_failure", reason="unit")

    assert result is False
    assert "serving_eligibility_recompute_failed" in caplog.text
    assert "database unavailable" in caplog.text


@pytest.mark.asyncio
async def test_recompute_nonexistent_content_key_returns_false() -> None:
    was_connected = await _prepare_db()
    try:
        result = await svc.recompute_serving_eligibility("ck_vis2_missing")

        row = await _ips_row("ck_vis2_missing")
        assert result is False
        assert row is None
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
async def test_audit_serving_contract_violations_reuses_canonical_classifier(monkeypatch) -> None:
    was_connected = await _prepare_db()
    try:
        ids = await _insert_product(
            "contract_violation",
            identity=False,
            pdp_scope="unverified",
        )
        good_ids = await _insert_product("contract_good")
        await database.execute(
            """
            INSERT INTO index_pipeline_state (
                content_key, pivota_signature_id, merchant_id, pipeline_stage,
                blocker_code, serving_eligible, identity_resolved, has_image,
                has_price, description_length
            ) VALUES (
                :content_key, :signature_id, :merchant_id, 'public_indexed',
                'none', TRUE, TRUE, TRUE, TRUE, 120
            )
            """,
            {
                "content_key": ids["content_key"],
                "signature_id": ids["signature_id"],
                "merchant_id": ids["merchant_id"],
            },
        )
        await database.execute(
            """
            INSERT INTO index_pipeline_state (
                content_key, pivota_signature_id, merchant_id, pipeline_stage,
                blocker_code, serving_eligible, identity_resolved, has_image,
                has_price, description_length
            ) VALUES (
                :content_key, :signature_id, :merchant_id, 'public_indexed',
                'none', TRUE, TRUE, TRUE, TRUE, 120
            )
            """,
            {
                "content_key": good_ids["content_key"],
                "signature_id": good_ids["signature_id"],
                "merchant_id": good_ids["merchant_id"],
            },
        )

        batch_calls = []
        original_batch_fetch = svc._fetch_eligibility_inputs_for_content_keys

        async def spy_batch_fetch(content_keys):
            batch_calls.append(list(content_keys))
            return await original_batch_fetch(content_keys)

        monkeypatch.setattr(
            svc,
            "_fetch_eligibility_inputs_for_content_keys",
            spy_batch_fetch,
        )

        violations = await svc.audit_serving_contract_violations(
            limit=10,
            batch_size=2,
        )

        assert len(violations) == 1
        assert violations[0]["content_key"] == ids["content_key"]
        assert violations[0]["expected_blocker_code"] == "entity_unresolved"
        assert violations[0]["identity_resolved"] is False
        assert violations[0]["_expected_state"]["content_key"] == ids["content_key"]
        assert violations[0]["_expected_state"]["blocker_code"] == "entity_unresolved"
        assert len(batch_calls) == 1
        assert set(batch_calls[0]) == {
            ids["content_key"],
            good_ids["content_key"],
        }
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
async def test_audit_serving_contract_violations_accepts_resolved_pdp_scope_without_identity_row() -> None:
    was_connected = await _prepare_db()
    try:
        ids = await _insert_product(
            "scope_resolved_without_identity",
            identity=False,
            pdp_scope="merchant_owned",
        )
        await database.execute(
            """
            INSERT INTO index_pipeline_state (
                content_key, pivota_signature_id, merchant_id, pipeline_stage,
                blocker_code, serving_eligible, identity_resolved, has_image,
                has_price, description_length
            ) VALUES (
                :content_key, :signature_id, :merchant_id, 'public_indexed',
                'none', TRUE, TRUE, TRUE, TRUE, 120
            )
            """,
            {
                "content_key": ids["content_key"],
                "signature_id": ids["signature_id"],
                "merchant_id": ids["merchant_id"],
            },
        )

        violations = await svc.audit_serving_contract_violations(
            limit=10,
            batch_size=2,
        )

        assert violations == []
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
async def test_fail_close_index_pipeline_state_demotes_orphan_serving_row() -> None:
    was_connected = await _prepare_db()
    try:
        await database.execute(
            """
            INSERT INTO index_pipeline_state (
                content_key, pipeline_stage, blocker_code, serving_eligible,
                identity_resolved, has_image, has_price
            ) VALUES (
                'ck_vis2_orphan_ips', 'public_indexed', 'none', TRUE,
                TRUE, TRUE, TRUE
            )
            """
        )

        await svc.fail_close_index_pipeline_state(
            "ck_vis2_orphan_ips",
            blocker_code="not_live",
            blocker_detail="no catalog_products rows found during unit test",
        )

        row = await _ips_row("ck_vis2_orphan_ips")
        assert row is not None
        assert bool(row["serving_eligible"]) is False
        assert row["blocker_code"] == "not_live"
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


# ---------------------------------------------------------------------------
# ADR-008 SLICE 1: index_eligible persistence + lifecycle (DB-backed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recompute_persists_index_eligible_true_for_offer_free_product() -> None:
    """An offer-free product (no catalog_offers row) meeting quality+image+
    identity+description is index_eligible=TRUE but serving_eligible=FALSE, and
    both flags are persisted to index_pipeline_state."""
    was_connected = await _prepare_db()
    try:
        ids = await _insert_product("idx_offer_free", has_price=False)

        result = await svc.recompute_serving_eligibility(
            ids["content_key"], reason="unit"
        )

        row = await _ips_row(ids["content_key"])
        assert result is False  # serving_eligible (the return value) stays False
        assert row is not None
        assert bool(row["serving_eligible"]) is False
        assert bool(row["index_eligible"]) is True
        assert row["blocker_code"] == "no_price"
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
async def test_recompute_persists_index_eligible_false_when_non_price_predicate_fails() -> None:
    was_connected = await _prepare_db()
    try:
        # Offer-free AND unresolved identity: blocker short-circuits at no_price,
        # but index_eligible must still persist FALSE (raw-predicate derivation).
        ids = await _insert_product(
            "idx_bad_identity",
            has_price=False,
            identity=False,
            pdp_scope="unverified",
        )

        await svc.recompute_serving_eligibility(ids["content_key"], reason="unit")

        row = await _ips_row(ids["content_key"])
        assert row is not None
        assert bool(row["serving_eligible"]) is False
        assert bool(row["index_eligible"]) is False
        assert row["blocker_code"] == "no_price"
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
async def test_recompute_persists_both_eligible_for_full_product() -> None:
    was_connected = await _prepare_db()
    try:
        ids = await _insert_product("idx_full")

        result = await svc.recompute_serving_eligibility(ids["content_key"])

        row = await _ips_row(ids["content_key"])
        assert result is True
        assert row is not None
        assert bool(row["serving_eligible"]) is True
        assert bool(row["index_eligible"]) is True
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


@pytest.mark.asyncio
async def test_fail_close_also_clears_index_eligible() -> None:
    """M6 lifecycle: when a delisted/orphaned product is failed-closed, both
    serving_eligible and index_eligible must drop to FALSE so it stops being
    citable."""
    was_connected = await _prepare_db()
    try:
        await database.execute(
            """
            INSERT INTO index_pipeline_state (
                content_key, pipeline_stage, blocker_code, serving_eligible,
                index_eligible, identity_resolved, has_image, has_price
            ) VALUES (
                'ck_vis2_idx_failclose', 'public_indexed', 'none', FALSE,
                TRUE, TRUE, TRUE, FALSE
            )
            """
        )

        await svc.fail_close_index_pipeline_state(
            "ck_vis2_idx_failclose",
            blocker_code="not_live",
            blocker_detail="source rows gone during unit test",
        )

        row = await _ips_row("ck_vis2_idx_failclose")
        assert row is not None
        assert bool(row["serving_eligible"]) is False
        assert bool(row["index_eligible"]) is False
        assert row["blocker_code"] == "not_live"
    finally:
        await _cleanup()
        await _disconnect_if_needed(was_connected)


# --- has_us_offer is derived from currency (real SQL, both directions) --------
#
# These exercise the ACTUAL eligibility SQL against the DB. The string-level
# assertions live in tests/test_has_us_offer_from_currency.py; without these the
# negative direction (a non-USD offer) is never proven against real SQL.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,currency,expected_us_offer",
    [
        (0, "USD", True),
        (1, "usd", True),      # case-insensitive
        (2, " USD ", True),    # padded — writers persist crawl output verbatim
        (3, "GBP", False),
        (4, "INR", False),
        (5, None, False),      # NULL is schema-legal; fail closed
        (6, "", False),
    ],
)
async def test_has_us_offer_derived_from_currency_via_sql(
    case: int, currency: Optional[str], expected_us_offer: bool
) -> None:
    was_connected = await _prepare_db()
    try:
        # keyed on the case index: deriving it from the value collapsed
        # "USD"/"usd"/" USD " onto one product_key
        suffix = f"cur{case}"
        ids = await _insert_product(suffix)
        await database.execute(
            "UPDATE catalog_offers SET currency = :cur WHERE product_key = :pk",
            {"cur": currency, "pk": ids["product_key"]},
        )
        rows = await svc._fetch_eligibility_inputs(ids["content_key"])
        assert rows, "eligibility query returned no rows"
        assert bool(rows[0].get("has_us_offer")) is expected_us_offer
    finally:
        await _cleanup()
        if not was_connected:
            await database.disconnect()


@pytest.mark.asyncio
async def test_non_usd_offer_blocks_serving_when_gate_enabled(monkeypatch) -> None:
    """End-to-end at the DB level: a GBP-only row must not serve to US shoppers,
    but must remain index_eligible (ADR-008 citation floor needs no US offer)."""
    import services.agent_decision_gates as gates

    # Both references must be patched: _classify_product calls the name imported
    # into its own module, and evaluate_agent_decision_gates re-checks the flag
    # through services.agent_decision_gates.
    monkeypatch.setattr(svc, "agent_decision_gates_enabled", lambda: True)
    monkeypatch.setattr(gates, "agent_decision_gates_enabled", lambda: True)
    monkeypatch.setattr(gates, "evidence_gates_enabled", lambda: False)

    was_connected = await _prepare_db()
    try:
        ids = await _insert_product("gbp_only")
        await database.execute(
            "UPDATE catalog_offers SET currency = 'GBP' WHERE product_key = :pk",
            {"pk": ids["product_key"]},
        )
        await svc.recompute_serving_eligibility(ids["content_key"], reason="unit")
        row = await _ips_row(ids["content_key"])
        assert not row["serving_eligible"]   # sqlite stores booleans as 0/1
        assert row["blocker_code"] == gates.BLOCKER_NO_US_OFFER
        assert bool(row["index_eligible"]) is True
    finally:
        await _cleanup()
        if not was_connected:
            await database.disconnect()
