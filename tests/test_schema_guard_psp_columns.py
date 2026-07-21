import pytest


@pytest.mark.asyncio
async def test_ensure_required_schema_light_adds_merchant_psp_columns_in_fast_mode(monkeypatch):
    from db import schema_guard as sg

    executed = []

    class DummyDB:
        async def execute(self, query):
            executed.append(str(query))

    async def noop_connect():
        return None

    monkeypatch.setattr(sg, "IS_POSTGRES", True)
    monkeypatch.setattr(sg, "IS_SQLITE", False)
    monkeypatch.setattr(sg, "database", DummyDB())
    monkeypatch.setattr(sg, "_ensure_database_connected", noop_connect)

    await sg.ensure_required_schema_light()

    assert any("ALTER TABLE IF EXISTS orders" in stmt for stmt in executed)
    merchant_stmt = next(stmt for stmt in executed if "ALTER TABLE IF EXISTS merchant_psps" in stmt)
    assert "ADD COLUMN IF NOT EXISTS environment" in merchant_stmt
    assert "ADD COLUMN IF NOT EXISTS provider_config" in merchant_stmt
    assert "ADD COLUMN IF NOT EXISTS validation_status" in merchant_stmt

    # Pivota canonical PDP columns (migration 071) must be ensured by
    # the same fast-mode startup guard, otherwise the canonical
    # resolver + audit URL fallback both 500 with UndefinedColumnError
    # in production (deploy-skipped-migrations failure mode).
    catalog_stmt = next(
        stmt for stmt in executed if "ALTER TABLE IF EXISTS catalog_products" in stmt
    )
    assert "ADD COLUMN IF NOT EXISTS pivota_signature_id" in catalog_stmt
    assert "ADD COLUMN IF NOT EXISTS pivota_canonical_url" in catalog_stmt
    assert any(
        "idx_catalog_products_pivota_signature" in stmt for stmt in executed
    ), "partial unique index for pivota_signature_id not created"
    # Phase O-1 (mig 075). Without `tags`, ingest_standard_products will
    # error on insert because the SQLAlchemy mapping in db/catalog.py
    # declares the column. Same deploy-skipped-migrations failure mode
    # as the canonical PDP columns above.
    assert "ADD COLUMN IF NOT EXISTS tags" in catalog_stmt
    # Phase O-2 (mig 076). All four typed taxonomy columns gated by the
    # same fail-safe — db/catalog.py mapping declares them, so writes
    # would error if the columns are missing post-deploy.
    assert "ADD COLUMN IF NOT EXISTS price_tier" in catalog_stmt
    assert "ADD COLUMN IF NOT EXISTS use_case_tags" in catalog_stmt
    assert "ADD COLUMN IF NOT EXISTS lifestyle_tags" in catalog_stmt
    assert "ADD COLUMN IF NOT EXISTS demographic" in catalog_stmt
    # Phase O-4 (mig 077). pdp_lifecycle_stage column + partial index
    # on (validated, published) — without the column, all 3 ingestion
    # paths would error on insert; without the index, recall live-stage
    # filtering would seq-scan in O-5.
    assert "ADD COLUMN IF NOT EXISTS pdp_lifecycle_stage" in catalog_stmt
    assert any(
        "idx_catalog_products_lifecycle_live" in stmt for stmt in executed
    ), "partial index for pdp_lifecycle_stage live stages not created"
    # PR-13 APM cols (mig 089). Self-heal block was added after the
    # production outage where PR #494 deployed without applying the
    # migration → every audit failed with "column merchant_onboarding
    # .apm_enabled does not exist". Re-introducing the same gap by
    # editing schema_guard should be caught by these assertions.
    apm_stmt = next(
        (s for s in executed
         if "ALTER TABLE IF EXISTS merchant_onboarding" in s
            and "apm_enabled" in s),
        None,
    )
    assert apm_stmt is not None, (
        "merchant_onboarding APM columns not self-healed in schema_guard"
    )
    assert "ADD COLUMN IF NOT EXISTS apm_enabled" in apm_stmt
    assert "ADD COLUMN IF NOT EXISTS apm_cadence_days" in apm_stmt
    assert "ADD COLUMN IF NOT EXISTS apm_scope_jsonb" in apm_stmt
    assert "ADD COLUMN IF NOT EXISTS apm_configured_at" in apm_stmt
    assert "ADD COLUMN IF NOT EXISTS apm_last_run_at" in apm_stmt
    assert any(
        "merchant_onboarding_apm_cadence_days_chk" in s for s in executed
    ), "apm cadence check constraint not ensured"
    assert any(
        "idx_merchant_onboarding_apm_due" in s for s in executed
    ), "apm due partial index not ensured"


@pytest.mark.asyncio
async def test_schema_guard_apm_block_matches_migration_089():
    """Sentinel: schema_guard's APM self-heal must stay in lockstep
    with the canonical migration 089. If a future edit drifts one
    side, this test fails and reminds the author to update both."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    migration_text = (
        repo_root / "db" / "migrations"
        / "089_merchant_onboarding_apm_config.sql"
    ).read_text()
    guard_text = (repo_root / "db" / "schema_guard.py").read_text()
    for token in (
        "apm_enabled",
        "apm_cadence_days",
        "apm_scope_jsonb",
        "apm_configured_at",
        "apm_last_run_at",
        "merchant_onboarding_apm_cadence_days_chk",
        "idx_merchant_onboarding_apm_due",
    ):
        assert token in migration_text, f"{token} missing from migration 089"
        assert token in guard_text, (
            f"{token} present in migration 089 but missing from "
            "schema_guard.ensure_required_schema_light — would "
            "re-introduce the PR-13 outage gap"
        )
