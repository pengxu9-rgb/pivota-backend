"""Sandbox catalog copy: the table list, the column rules and where each side may run.

No database here (tests/test_sandbox_catalog_copy_postgres.py runs the copy itself).
"""

from __future__ import annotations

import pytest

from jobs import sandbox_catalog_copy as C

_PLATFORM_KEYS = ("PIVOTA_PLATFORM", "PIVOTA_ENV", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
                  "K_SERVICE", "K_REVISION", "K_CONFIGURATION", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION")


def test_every_table_has_a_reason_and_appears_once():
    names = [s.name for s in C.CATALOG_TABLES]
    assert len(names) == len(set(names))
    assert all(s.why.strip() for s in C.CATALOG_TABLES)


def test_no_excluded_table_is_copied():
    assert not set(C.EXCLUDED_TABLES) & {s.name for s in C.CATALOG_TABLES}


@pytest.mark.parametrize("name", ["orders", "agents", "buyer_identity_links", "agent_purchase_consents",
                                  "reap_agentic_purchases", "merchant_psps", "agent_identity_issuers"])
def test_the_brief_s_never_list_is_excluded(name):
    assert name in C.EXCLUDED_TABLES
    assert name not in {s.name for s in C.CATALOG_TABLES}


def _merchant_onboarding_columns():
    from db.merchant_onboarding import merchant_onboarding

    # Plus the columns migrations add outside the Table definition (023 x402, 282 is_test).
    return [c.name for c in merchant_onboarding.columns] + ["is_test", "x402_enabled"]


def test_merchant_onboarding_copies_no_secret_or_contact_column():
    spec = next(s for s in C.CATALOG_TABLES if s.name == "merchant_onboarding")
    plan = dict(C.plan_columns(spec, _merchant_onboarding_columns()))
    for column in ("mcp_access_token", "api_key", "api_key_hash", "psp_sandbox_key", "contact_phone",
                   "kyc_documents", "rejection_reason", "signup_source", "x402_enabled"):
        assert column not in plan, column
    assert plan["contact_email"].startswith("'sandbox-merchant@example.invalid'")


def test_merchant_onboarding_keeps_every_not_null_column_without_a_default():
    from db.merchant_onboarding import merchant_onboarding

    spec = next(s for s in C.CATALOG_TABLES if s.name == "merchant_onboarding")
    for column in merchant_onboarding.columns:
        if not column.nullable and column.server_default is None and not column.primary_key:
            assert column.name in spec.keep, column.name


def test_merchant_stores_never_copies_the_store_credential_and_turns_live_fetch_off():
    columns = ["store_id", "merchant_id", "platform", "name", "domain", "api_key", "status", "connected_at",
               "last_sync", "product_count", "is_primary", "order_writeback_status",
               "order_writeback_canary_order_id", "created_at", "realtime_enabled", "api_endpoint",
               "query_ttl_seconds", "support_email", "upstream_probe_status"]
    spec = next(s for s in C.CATALOG_TABLES if s.name == "merchant_stores")
    plan = dict(C.plan_columns(spec, columns))
    for column in ("api_key", "api_endpoint", "support_email", "order_writeback_canary_order_id"):
        assert column not in plan, column
    assert plan["realtime_enabled"].startswith("false")


@pytest.mark.parametrize("column,risky", [
    ("contact_email", True), ("updated_by_email", True), ("decided_by", True), ("created_by_user_id", True),
    ("mcp_access_token", True), ("api_key", True), ("challenge_token", True), ("notes", True),
    ("first_name", True), ("shipping_address", True), ("customer_id", True),
    ("title", False), ("price_minor", False), ("card_network", False), ("extractor_version", False),
    ("merchant_id", False), ("keyword", False), ("by_line", True),
])
def test_column_risk_names(column, risky):
    assert C.column_is_risky(column) is risky


def test_a_risky_unclassified_column_is_refused():
    spec = C.TableSpec("catalog_products", "x")
    with pytest.raises(C.CopyRefused, match="owner_email"):
        C.plan_columns(spec, ["product_id", "owner_email"])


def test_dropped_columns_are_not_planned():
    spec = next(s for s in C.CATALOG_TABLES if s.name == "external_product_seeds")
    assert [c for c, _ in C.plan_columns(spec, ["id", "url", "notes", "created_by_employee_id"])] == ["id", "url"]


@pytest.fixture
def clean_env(monkeypatch):
    for key in _PLATFORM_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("env,ok", [
    ({"PIVOTA_ENV": "production", "CLOUD_RUN_JOB": "sandbox-catalog-export"}, True),
    ({"PIVOTA_ENV": "sandbox", "CLOUD_RUN_JOB": "j"}, False),
    ({"PIVOTA_ENV": "staging", "CLOUD_RUN_JOB": "j"}, False),
    ({}, False),
])
def test_the_export_runs_only_in_prod(clean_env, env, ok):
    if ok:
        C.check_export_environment(env)
    else:
        with pytest.raises(C.CopyRefused):
            C.check_export_environment(env)


def test_the_import_runs_only_in_a_sandbox_that_passes_the_guard(clean_env):
    from config.sandbox_guard import SandboxGuardRefused
    from tests.test_sandbox_guard import GOOD

    for env in ({"PIVOTA_ENV": "production", "CLOUD_RUN_JOB": "j"}, {"PIVOTA_ENV": "staging"}, {}):
        with pytest.raises(C.CopyRefused):
            C.check_import_environment(env)
    good = dict(GOOD, CLOUD_RUN_JOB="sandbox-catalog-import")
    C.check_import_environment(good)
    with pytest.raises(SandboxGuardRefused, match="DATABASE_URL"):
        C.check_import_environment(dict(good, DATABASE_URL="postgresql://u:p@10.25.0.2/pivota"))


def test_main_reports_a_refusal_with_exit_code_2(clean_env, capsys, monkeypatch):
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    assert C.main(["import"]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_gcs_store_parses_the_uri():
    store = C.GcsStore("gs://pivota-sandbox-catalog-drop/catalog/")
    assert store.bucket == "pivota-sandbox-catalog-drop" and store.prefix == "catalog"
    with pytest.raises(C.CopyRefused):
        C.GcsStore("s3://x")


@pytest.mark.parametrize("name", ["catalog_row_trust", "catalog_offers", "catalog_products", "external_product_seeds",
                                  "agent_pdp_view", "merchant_onboarding"])
def test_the_tables_the_serving_lanes_join_are_copied_whole_and_required(name):
    """catalog_row_trust above all: the gateway's lanes INNER JOIN it on serving_decision='public', so
    without it the sandbox serves an empty catalog. Required (not optional): a prod export without
    it must fail, not ship an empty sandbox."""
    spec = next(s for s in C.CATALOG_TABLES if s.name == name)
    assert spec.optional is False
