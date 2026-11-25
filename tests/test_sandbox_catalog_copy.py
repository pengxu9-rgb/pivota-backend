"""Sandbox catalog copy: the table list, the column rules and where each side may run.

No database here (tests/test_sandbox_catalog_copy_postgres.py runs the copy itself).
"""

from __future__ import annotations

import pytest

from jobs import sandbox_catalog_copy as C
from tests.sandbox_test_addresses import stand_in_addresses  # noqa: F401 (autouse: hashed addresses -> RFC 5737 stand-ins)

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
        C.check_import_environment(dict(good, DATABASE_URL="postgresql://u:p@192.0.2.25/pivota"))


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


def _backend_ddl_columns():
    """Column names per table from this repo's own DDL: migrations, db/*.py Table(...) and CREATE TABLE
    strings, main.py. A parse, not a database: good enough to catch a risky NAME the spec misses."""
    import glob
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    files = sorted(glob.glob(str(root / "db/migrations/*.sql"))) + glob.glob(str(root / "db/*.py")) + [str(root / "main.py")]
    cols = {}
    for path in files:
        text = Path(path).read_text(errors="ignore")
        for m in re.finditer(r'CREATE TABLE(?: IF NOT EXISTS)?\s+(?:public\.)?"?([a-z_][a-z0-9_]*)"?\s*\((.*?)\n\s*\)\s*;?', text, re.S | re.I):
            for line in m.group(2).split("\n"):
                mm = re.match(r'\s*"?([a-z_][a-z0-9_]*)"?\s+[A-Za-z]', line)
                if mm and mm.group(1).upper() not in ("PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "CHECK", "EXCLUDE"):
                    cols.setdefault(m.group(1).lower(), set()).add(mm.group(1))
        for m in re.finditer(r'ALTER TABLE(?: IF EXISTS)?\s+(?:public\.)?([a-z_][a-z0-9_]*)\b(.*?);', text, re.S | re.I):
            for mm in re.finditer(r'ADD COLUMN(?: IF NOT EXISTS)?\s+([a-z_][a-z0-9_]*)', m.group(2), re.I):
                cols.setdefault(m.group(1).lower(), set()).add(mm.group(1))
        for m in re.finditer(r'Table\(\s*["\']([a-z_0-9]+)["\'](.*?)\n\)', text, re.S):
            for mm in re.finditer(r'Column\(\s*["\']([a-z_0-9]+)["\']', m.group(2)):
                cols.setdefault(m.group(1), set()).add(mm.group(1))
    return cols


def test_every_copied_table_s_known_columns_pass_the_export_rules():
    """The export refuses a risky-looking column the spec has not classified. Run that rule now on
    every column this repo's DDL declares, so a gap shows up in review, not on the prod export."""
    ddl = _backend_ddl_columns()
    checked = 0
    for spec in C.CATALOG_TABLES:
        columns = sorted(ddl.get(spec.name, ()))
        if columns:
            C.plan_columns(spec, columns)
            checked += 1
    assert checked >= 25  # most copied tables have backend DDL; the gateway-created ones may not


def test_the_staff_and_free_text_columns_found_in_review_stay_dropped():
    spec = {s.name: s for s in C.CATALOG_TABLES}
    assert {"created_by", "revoked_by"} <= set(spec["catalog_source_quarantine"].drop)
    assert "approved_by" in spec["merchant_product_overlay"].drop
    assert "created_by" in spec["pdp_identity_override"].drop
    assert "audit_notes" in spec["product_beauty_attributes"].drop
    assert "verified_by" in spec["aurora_dupe_kb"].drop
    assert "updated_by" in spec["merchant_category_mapping"].drop and "updated_by" in spec["ops_category_override"].drop


def test_the_import_refuses_a_target_that_is_not_positively_the_sandbox_s(clean_env):
    from tests.test_sandbox_guard import GOOD

    good = dict(GOOD, CLOUD_RUN_JOB="sandbox-catalog-import")
    for dsn in ("postgresql://u:p@203.0.113.7/pivota?host=/cloudsql/pivota-sandbox:us-west1:pg",
                "postgresql://u:p@203.0.113.70/pivota", "postgresql://u:p@db.internal/pivota"):
        with pytest.raises(C.CopyRefused, match="private range"):
            C.check_import_environment(dict(good, DATABASE_URL=dsn))


async def test_the_export_never_lists_the_drop(tmp_path):
    """S2: the export's SA holds objectCreator only; a list would be a 403. Create-only uploads are the
    overwrite guard."""
    class CreateOnly(C.LocalDirStore):
        def list(self, prefix=""):
            raise AssertionError("the export listed the bucket")

    class FakeConn:
        def transaction(self, **kw):
            class T:
                async def __aenter__(self): return None
                async def __aexit__(self, *a): return False
            return T()

        async def execute(self, *a): return "SET"

        async def fetch(self, *a): return [{"column_name": "merchant_id"}]

        async def fetchval(self, *a): return 1

        async def copy_from_query(self, query, output, format):
            output.write(b"m1\n")

    spec = (C.TableSpec("catalog_merchants", "x"),)
    manifest = await C.export_catalog(FakeConn(), CreateOnly(tmp_path), stamp="20261125T0100Z", specs=spec)
    assert manifest["tables"][0]["rows"] == 1
    with pytest.raises(C.CopyRefused, match="never overwrites"):
        await C.export_catalog(FakeConn(), CreateOnly(tmp_path), stamp="20261125T0100Z", specs=spec)


def test_gcs_errors_are_refusals_with_the_status_only(monkeypatch):
    import httpx

    class R:
        def __init__(self, code):
            self.status_code = code
            self.content = b"secret body"

        def json(self):
            return {"access_token": "t"}

    store = C.GcsStore("gs://pivota-sandbox-catalog-drop/catalog")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R(200))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R(403))
    with pytest.raises(C.CopyRefused, match="HTTP 403 for create") as excinfo:
        store.put("20261125T0100Z/x.csv.gz", b"data")
    assert "secret body" not in str(excinfo.value)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R(412))
    with pytest.raises(C.CopyRefused, match="never overwrites"):
        store.put("20261125T0100Z/x.csv.gz", b"data")


@pytest.mark.parametrize("name", sorted(C.NEVER_COPY_TABLES) + [
    "buyer_addresses", "partner_tokens", "agent_session_log", "oauth_grants", "merchant_wallets",
    "refund_requests", "stored_cards", "otp_codes", "x_ledger", "booking_notes", "auth_events", "secrets",
])
def test_identity_key_order_and_buyer_tables_are_never_copied(name):
    assert C.never_copy(name)
    assert name not in {s.name for s in C.CATALOG_TABLES}


def test_no_copied_table_looks_like_one():
    assert [s.name for s in C.CATALOG_TABLES if C.never_copy(s.name)] == []


def test_the_importer_refuses_a_manifest_naming_one_even_with_a_spec_for_it():
    spec = (C.TableSpec("catalog_offers", "x"),)
    manifest = {"format_version": C.FORMAT_VERSION, "tables": [
        {"name": "catalog_offers", "columns": ["offer_id"]}, {"name": "api_keys", "columns": ["key_hash"]}]}
    with pytest.raises(C.CopyRefused, match="never copied: api_keys"):
        C.validate_manifest(manifest, spec)
    with pytest.raises(C.CopyRefused, match="never copied: agents"):
        C.validate_manifest({"format_version": C.FORMAT_VERSION, "tables": []}, (C.TableSpec("agents", "x"),))


async def test_the_export_refuses_a_spec_naming_one_before_reading_anything(tmp_path):
    class NoDb:
        def __getattr__(self, name):
            raise AssertionError("read prod")

    with pytest.raises(C.CopyRefused, match="never copied: agents,orders"):
        await C.export_catalog(NoDb(), C.LocalDirStore(tmp_path), stamp="20261125T0100Z",
                               specs=(C.TableSpec("orders", "x"), C.TableSpec("agents", "x")))


@pytest.mark.parametrize("uri", ["gs://other-bucket/catalog", "gs://pivota-sandbox-catalog-drop-2/x",
                                 "gs://pivota-sandbox-catalog-drop/../x", "s3://pivota-sandbox-catalog-drop/x"])
def test_the_gcs_store_takes_only_the_pinned_drop_bucket(uri):
    with pytest.raises(C.CopyRefused):
        C.GcsStore(uri)


def test_a_gcs_transport_error_is_a_refusal(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(C.CopyRefused, match="unreachable for the metadata token"):
        C.GcsStore("gs://pivota-sandbox-catalog-drop/catalog").get("x")
