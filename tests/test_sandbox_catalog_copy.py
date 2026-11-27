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
    # S3: the names the 5d8e3aa73 review found passing.
    ("emails", True), ("phones", True), ("session_id", True), ("sessions", True), ("user_agent", True),
    ("auth_header", True), ("authorization", True), ("bearer_token", True), ("jwt", True), ("cookies", True),
    ("access_key", True), ("secret_key", True), ("signing_key", True), ("user_name", True), ("username", True),
    ("reviewer", True), ("actor", True), ("passport_no", True), ("card_number", True), ("cvv", True), ("pan", True),
    ("tax_id", True), ("webhook_url", True), ("raw_payload", True), ("headers", True), ("comments", True),
    ("product_key", False), ("content_key", False), ("kb_key", False), ("sku_key", False), ("headline", False),
])
def test_column_risk_names(column, risky):
    assert C.column_is_risky(column) is risky


def test_a_risky_unclassified_column_is_refused():
    spec = C.TableSpec("catalog_products", "x")
    with pytest.raises(C.CopyRefused, match="owner_email"):
        C.plan_columns(spec, ["product_key", "owner_email"])


def test_dropped_columns_are_not_planned():
    spec = next(s for s in C.CATALOG_TABLES if s.name == "external_product_seeds")
    assert [c for c, _ in C.plan_columns(spec, ["id", "domain", "notes", "created_by_employee_id"])] == ["id", "domain"]


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
            from jobs.sandbox_catalog_columns import JSON, column_class

            C.plan_columns(spec, columns, {c: "jsonb" for c in columns if column_class(spec.name, c) == JSON})
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

    from config.sandbox_guard import SandboxGuardRefused

    good = dict(GOOD, CLOUD_RUN_JOB="sandbox-catalog-import")
    for dsn in ("postgresql://u:p@203.0.113.7/pivota?host=/cloudsql/pivota-sandbox:us-west1:pg",
                "postgresql://u:p@203.0.113.70/pivota", "postgresql://u:p@db.internal/pivota"):
        # S1: the startup guard now makes the same positive check, so it refuses first; the import's
        # own check (sandbox_database_refusal) stays as the second lock.
        with pytest.raises((C.CopyRefused, SandboxGuardRefused), match="private range"):
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

        async def fetch(self, *a):
            return [{"column_name": "merchant_id", "type_name": "varchar", "type_kind": "b",
                     "base_name": None, "elem_name": None, "base_elem_name": None}]

        async def fetchval(self, *a): return 1

        async def copy_from_query(self, query, output, **kw):
            await output(b"m1\n")

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


# --- S3: every column classified (jobs/sandbox_catalog_columns.py) ---------------------------------

from jobs import sandbox_catalog_columns as COLS  # noqa: E402


def test_every_copied_table_is_classified_and_nothing_else_is():
    assert set(COLS.COLUMNS) == {s.name for s in C.CATALOG_TABLES}


def test_every_json_column_states_why_it_is_catalog_data():
    for table, columns in COLS.COLUMNS.items():
        for column, entry in columns.items():
            if isinstance(entry, tuple):
                assert entry[0] == COLS.JSON and len(entry[1].strip()) > 8, (table, column)
            else:
                assert entry in (COLS.KEY, COLS.TEXT, COLS.URL, COLS.NUM, COLS.FLAG, COLS.TIME, COLS.REPLACED), (table, column)


def test_the_classification_agrees_with_the_specs():
    for spec in C.CATALOG_TABLES:
        classified = COLS.COLUMNS[spec.name]
        assert not set(classified) & set(spec.drop), spec.name  # a dropped column is not also copied
        if spec.keep is not None:
            assert set(classified) <= set(spec.keep), spec.name
        replaced = {c for c, e in classified.items() if e == COLS.REPLACED}
        assert replaced == set(spec.replace) & set(classified), spec.name


def test_the_json_columns_the_review_named_are_scrubbed():
    for table, column in (("product_evidence", "claims"), ("agent_pdp_view", "evidence_profile"),
                          ("relationship_candidate_labels", "human_review")):
        assert COLS.column_class(table, column) == COLS.JSON, (table, column)


def test_two_names_that_are_not_tables_are_not_copied():
    names = {s.name for s in C.CATALOG_TABLES}
    assert "brands" not in names  # a CTE name in one gateway query, not a table
    assert "product_relationship_edges" not in names and "product_relationship_edges" in C.EXCLUDED_TABLES


def test_ops_free_text_reasons_are_placeholders():
    spec = {s.name: s for s in C.CATALOG_TABLES}
    for table in ("catalog_source_quarantine", "ops_category_override"):
        assert spec[table].replace["reason"] == "'withheld in the sandbox copy'"


@pytest.mark.parametrize("key", [
    "emailAddress", "email_address", "E-mail", "mail", "mobileNumber", "shipping_address", "review_notes",
    "audit_note", "name", "signed_by_name", "handle", "request_id", "trace_id", "reason", "rationale",
    "reviewer_decisions", "tokens", "group_by",
    "reviewed_by", "reviewedBy", "last_approved_by", "createdBy", "email", "Email", "billing_email", "emails",
    "phone", "phone_number", "contact_phone", "password", "api_key", "apiKey", "access_token", "refresh_token",
    "client_secret", "authorization", "cookie", "session_id", "sessionId", "user_id", "userId", "username",
    "user_agent", "customer", "customer_name", "buyer_email", "employee_id", "reviewer", "reviewer_name",
    "actor_id", "owner_email", "author_name", "contact", "first_name", "last_name", "address", "ip",
    "ip_address", "card_number", "cvv", "pan", "iban", "notes", "note", "comments", "raw_payload", "headers",
    "webhook_url",
])
def test_person_keys_are_recognised(key):
    assert C.PERSON_KEY_RE.match(key)


@pytest.mark.parametrize("key", [
    "claim_text", "evidence_grade", "review_status", "review_source_url", "title", "price", "product_key",
    "shade_name", "brand", "ingredients", "market", "currency", "image_url", "source_ref", "decision",
    "display_name", "verdict", "category", "tier",
    # a prefix-matching mutant passed these as person keys (review): whole-key match only
    "panthenol", "ipa", "pantone", "thumbnail", "shade_name", "brand_name", "retail_price", "nail_polish",
])
def test_catalog_keys_are_left_alone(key):
    assert not C.PERSON_KEY_RE.match(key)


def test_person_values_reports_paths_never_values():
    value = {"a": [{"reviewer": {"id": "staff-1"}}, {"email": None}, {"notes": "redacted"}], "userId": 3,
             "note2": "write to a@b.example", "ok": "fine"}
    assert C.person_values(value) == ["$.a[0].reviewer", "$.userId", "$.note2"]


def test_a_json_typed_column_must_be_classified_json():
    spec = next(s for s in C.CATALOG_TABLES if s.name == "catalog_merchants")
    with pytest.raises(C.CopyRefused, match="is classified JSON but its type is text"):
        C.plan_columns(spec, ["merchant_id", "metadata_json"], {"metadata_json": "text"})
    with pytest.raises(C.CopyRefused, match="not classified JSON"):
        C.plan_columns(spec, ["merchant_id", "merchant_name"], {"merchant_name": "jsonb"})
    with pytest.raises(C.CopyRefused, match="type is unknown"):
        C.plan_columns(spec, ["merchant_id", "metadata_json"])


# --- SF3/SF4 (review of 6314c488e) --------------------------------------------------------------


def test_scrub_json_nulls_containers_and_redacts_strings_under_person_keys():
    value = {"human_review": {"decision": "approved", "reason": "per my call with Jo",
                              "reviewer_decisions": [{"by": "staff-1"}]}, "tokens": ["a"], "price": 1}
    new, changed = C.scrub_json(value)
    assert changed and new == {"human_review": {"decision": "approved", "reason": "redacted",
                                                "reviewer_decisions": None}, "tokens": None, "price": 1}
    assert C.person_values(new) == []


def test_product_data_columns_keep_product_names_and_handles():
    value = {"name": "Lip Tint", "handle": "lip-tint", "options": [{"name": "Shade"}], "reviewed_by": "x"}
    new, _ = C.scrub_json(value, COLS.PRODUCT_KEYS)
    assert new == {"name": "Lip Tint", "handle": "lip-tint", "options": [{"name": "Shade"}], "reviewed_by": "redacted"}
    assert COLS.json_keep_keys("catalog_products", "product_payload") == COLS.PRODUCT_KEYS
    assert COLS.json_keep_keys("relationship_candidate_labels", "human_review") == ()


@pytest.mark.parametrize("text,expected", [
    ("mail me at a.b@c.example", "mail me at [redacted]"),
    ("call +1 555 010 0199 now", "call [redacted] now"),
    ("call 010-1234-5678", "call [redacted]"),
    ("call (02) 555-0100", "call [redacted]"),
    ("Size 100 ml, 2026-11-26, GTIN 8809123456789, SPF 50+", "Size 100 ml, 2026-11-26, GTIN 8809123456789, SPF 50+"),
])
def test_contact_details_in_text_are_redacted_and_catalog_text_is_not(text, expected):
    assert C.redact_text(text) == expected


def test_the_ingredient_research_kb_copies_only_ready_short_rows():
    spec = next(s for s in C.CATALOG_TABLES if s.name == "aurora_ingredient_research_kb")
    assert spec.where == "status = 'ready' AND char_length(query_norm) <= 64"


async def test_the_export_query_and_its_count_both_carry_the_where(tmp_path):
    seen = []

    class FakeConn:
        def transaction(self, **kw):
            class T:
                async def __aenter__(self): return None
                async def __aexit__(self, *a): return False
            return T()

        async def execute(self, *a): return "SET"

        async def fetch(self, *a):
            return [{"column_name": "kb_key", "type_name": "text", "type_kind": "b",
                     "base_name": None, "elem_name": None, "base_elem_name": None}]

        async def fetchval(self, query, *a):
            seen.append(query)
            return 1

        async def copy_from_query(self, query, output, **kw):
            seen.append(query)
            await output(b"k1\n")

    spec = (next(s for s in C.CATALOG_TABLES if s.name == "aurora_ingredient_research_kb"),)
    await C.export_catalog(FakeConn(), C.LocalDirStore(tmp_path), stamp="20261126T0100Z", specs=spec)
    assert len(seen) == 2 and all(q.endswith("WHERE status = 'ready' AND char_length(query_norm) <= 64") for q in seen)


# --- review of c91b5a209: value false positives (V1, V2), keys (V3), duplicates (V4), V5, V7 ----------


@pytest.mark.parametrize("text", [
    # V1: retina image names are not emails
    "hero@2x.png", "https://cdn.shopify.com/s/files/1/x_480x480@2x.jpg?v=1", "a.jpg 1x, a@2x.jpg 2x",
    "img/logo@3x.webp", "banner@1.5x.avif",
    # V2: identifiers, years, sizes and prices are digit groups, not phones
    "1234-5678-9012", "ABC-1234-567-890", "Compatible with 2020 2021 2022 models", "250 500 750 ml",
    "Price 1299 1499 1999", "978 316 1484 100", "012 3456 7890", "Model (2020) 2021-2022",
    "contact lens solution 360 ml", "Size 100 ml, 2026-11-26, GTIN 8809123456789, SPF 50+",
])
def test_catalog_text_that_only_looks_like_contact_details_is_left_alone(text):
    assert C.redact_text(text) == text
    assert not C._text_hits(text)


@pytest.mark.parametrize("text,expected", [
    ("a@b.co", "[redacted]"), ("x@y.uk", "[redacted]"),          # the TLD {2,} mutant: two letters count
    ("+44 20 7946 0958", "[redacted]"),                          # the international branch, no phone word
    ("Tel: (02) 555-0100", "Tel: [redacted]"), ("call us at (415) 555-0100", "call us at [redacted]"),
    ("phone number 555 010 0199", "phone number [redacted]"), ("전화 010-1234-5678", "전화 [redacted]"),
    ("電話: 03-1234-5678", "電話: [redacted]"), ("whatsapp +86 138 0013 8000", "whatsapp [redacted]"),
    ("+395809844+0577444923", "[redacted][redacted]"),           # V5: idempotent
])
def test_real_contact_details_are_redacted_and_the_result_is_stable(text, expected):
    assert C.redact_text(text) == expected
    assert C.redact_text(expected) == expected and not C._text_hits(expected)


def test_an_email_or_phone_as_a_json_key_is_redacted_and_checked():
    value = {"votes": {"jane.doe@gmail.com": 1, "x@y.example": 2, "+44 20 7946 0958": 3, "ok": 4}}
    new, changed = C.scrub_json(value)
    assert changed and new == {"votes": {"[redacted]": 1, "[redacted]#2": 2, "[redacted]#3": 3, "ok": 4}}
    assert C.person_values(new) == []
    assert C.person_values(value) == ["$.votes.<key>"] * 3


def test_a_json_cell_with_a_repeated_key_is_always_rewritten():
    raw = '"m1","{""tier"": ""a"", ""tier"": null}"\n'.encode()
    out = C.scrub_csv("catalog_merchants", raw, "__null__", ["merchant_id", "metadata_json"]).decode()
    assert out == '"m1","{""tier"": null}"\n'
    C.assert_scrubbed("catalog_merchants", out.encode(), ["merchant_id", "metadata_json"])
    with pytest.raises(C.CopyRefused, match="repeated key"):
        C.assert_scrubbed("catalog_merchants", raw, ["merchant_id", "metadata_json"])


def test_a_lone_surrogate_stays_an_escape():
    raw = '"m1","{""tier"": ""\\ud800"", ""reviewed_by"": ""x""}"\n'.encode()  # JSON text: "\ud800"
    out = C.scrub_csv("catalog_merchants", raw, "__null__", ["merchant_id", "metadata_json"]).decode()
    assert '\\ud800' in out and '""reviewed_by"": ""redacted""' in out


def test_a_record_with_the_wrong_field_count_is_refused():
    with pytest.raises(C.CopyRefused, match="has 3 fields, expected 2"):
        C.scrub_csv("catalog_merchants", b'"m1","{}","extra"\n', "__null__", ["merchant_id", "metadata_json"])


def test_product_keys_are_kept_only_at_the_product_level_and_option_paths():
    value = {"name": "Lip Tint", "handle": "lip-tint", "options": [{"name": "Shade", "values": [{"name": "Red"}]}],
             "variants": [{"name": "Red / 5g"}], "reviews": [{"name": "Jane D.", "text": "nice"}],
             "seller": {"handle": "@jane", "name": "Jane's shop"}}
    new, _ = C.scrub_json(value, COLS.PRODUCT_KEYS)
    assert new["name"] == "Lip Tint" and new["handle"] == "lip-tint"
    assert new["options"] == [{"name": "Shade", "values": [{"name": "Red"}]}] and new["variants"] == [{"name": "Red / 5g"}]
    assert new["reviews"] == [{"name": "redacted", "text": "nice"}]
    assert new["seller"] == {"handle": "redacted", "name": "redacted"}
    top_list = [{"name": "Home"}, {"name": "Skincare"}]  # a column that IS a list (breadcrumb, offers)
    assert C.scrub_json(top_list, COLS.PRODUCT_KEYS)[0] == top_list


async def test_one_record_past_the_record_cap_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    """S5: the 1 GiB table cap is gone (tables stream); what is bounded is ONE record, the most the
    splitter ever holds."""
    monkeypatch.setattr(C, "MAX_RECORD_BYTES", 4)

    class FakeConn:
        def transaction(self, **kw):
            class T:
                async def __aenter__(self): return None
                async def __aexit__(self, *a): return False
            return T()

        async def execute(self, *a): return "SET"

        async def fetch(self, *a):
            return [{"column_name": "merchant_id", "type_name": "varchar", "type_kind": "b",
                     "base_name": None, "elem_name": None, "base_elem_name": None}]

        async def fetchval(self, *a): return 1

        async def copy_from_query(self, query, output, **kw):
            await output(b"m12345")
            await output(b"67\n")

    store = C.LocalDirStore(tmp_path)
    with pytest.raises(C.CopyRefused, match="longer than 4 bytes"):
        await C.export_catalog(FakeConn(), store, stamp="20261126T0100Z", specs=(C.TableSpec("catalog_merchants", "x"),))
    assert store.list("") == []
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []  # no unfinished upload left either
