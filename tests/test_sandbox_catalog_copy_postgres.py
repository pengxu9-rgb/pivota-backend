"""Sandbox catalog copy (jobs/sandbox_catalog_copy.py) against REAL Postgres.

Picked up by .github/workflows/postgres-dialect-gate.yml via the `tests/test_*_postgres.py` glob.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_sandbox_copy_test \\
        .venv/bin/python -m pytest tests/test_sandbox_catalog_copy_postgres.py

Does not import `main` (gate-wide rule). Two schemas in one throwaway database stand in for prod
("src_copy") and the sandbox ("dst_copy"); a directory stands in for the bucket. The table specs are
the REAL ones from CATALOG_TABLES, filtered to the tables built here, so the drop / keep / replace
rules under test are the ones prod will run.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL (see docstring)")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip("refusing to drop schemas in database %r - throwaway only" % dbname)


if _IS_PG:
    _assert_throwaway_database()

from jobs import sandbox_catalog_copy as C  # noqa: E402

SRC, DST = "src_copy", "dst_copy"
SECRETS = ("shpat_SECRET_TOKEN", "merchant-api-key-SECRET", "owner@realbrand.example",
           "+1-555-0100", "kyc-passport-SECRET", "store-api-SECRET", "support@realstore.example",
           "staff-42", "internal note SECRET", "buyer@person.example")

TABLES = ("catalog_merchants", "catalog_products", "catalog_offers", "external_product_seeds",
          "merchant_onboarding", "merchant_stores")
SPECS = [s for s in C.CATALOG_TABLES if s.name in TABLES]

#: The stand-in tables below are smaller than prod's and carry a few columns of their own; classify
#: those for these tests only. Every other column is classified by the REAL jobs/sandbox_catalog_columns.py.
from jobs import sandbox_catalog_columns as _COLS  # noqa: E402

_TEST_ONLY_CLASSES = {
    "catalog_products": {"product_id": _COLS.KEY, "tags": _COLS.TEXT, "attrs": (_COLS.JSON, "test attributes")},
    "catalog_offers": {"product_id": _COLS.KEY, "price": _COLS.NUM, "stock": _COLS.NUM},
    "external_product_seeds": {"url": _COLS.URL},
}


@pytest.fixture(autouse=True)
def _stand_in_classes(monkeypatch):
    merged = {t: dict(c) for t, c in _COLS.COLUMNS.items()}
    for table, extra in _TEST_ONLY_CLASSES.items():
        merged[table].update(extra)
    monkeypatch.setattr(_COLS, "COLUMNS", merged)
    yield merged

DDL = """
CREATE TABLE {s}.merchant_onboarding (
  id SERIAL PRIMARY KEY, merchant_id VARCHAR(50) UNIQUE, business_name VARCHAR(255) NOT NULL,
  store_url VARCHAR(500), operating_mode VARCHAR(32) NOT NULL DEFAULT 'storefront', website VARCHAR(500),
  region VARCHAR(50), signup_source VARCHAR(64), contact_email VARCHAR(255) NOT NULL, contact_phone VARCHAR(50),
  status VARCHAR(50), psp_connected BOOLEAN DEFAULT FALSE, psp_type VARCHAR(50), psp_sandbox_key TEXT,
  mcp_connected BOOLEAN DEFAULT FALSE, mcp_platform VARCHAR(50), mcp_shop_domain VARCHAR(255),
  mcp_access_token TEXT, api_key VARCHAR(255) UNIQUE, api_key_hash VARCHAR(255), kyc_documents JSON,
  rejection_reason TEXT, created_at TIMESTAMP DEFAULT now(), updated_at TIMESTAMP DEFAULT now(),
  verified_at TIMESTAMP, is_test BOOLEAN NOT NULL DEFAULT FALSE);
CREATE TABLE {s}.merchant_stores (
  store_id VARCHAR(50) PRIMARY KEY, merchant_id VARCHAR(50) NOT NULL, platform VARCHAR(50) NOT NULL,
  name VARCHAR(255) NOT NULL, domain VARCHAR(255), api_key TEXT, status VARCHAR(50) DEFAULT 'connected',
  connected_at TIMESTAMPTZ, last_sync TIMESTAMPTZ, product_count INTEGER DEFAULT 0, is_primary BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now(), realtime_enabled BOOLEAN DEFAULT FALSE, api_endpoint VARCHAR(500),
  query_ttl_seconds INTEGER DEFAULT 600, support_email VARCHAR(255), order_writeback_canary_order_id TEXT);
CREATE TABLE {s}.catalog_merchants (
  merchant_id VARCHAR(64) PRIMARY KEY, merchant_name VARCHAR(255), status VARCHAR(32) NOT NULL DEFAULT 'active',
  indexable BOOLEAN NOT NULL DEFAULT TRUE, metadata_json JSONB);
CREATE TABLE {s}.catalog_products (
  product_id TEXT PRIMARY KEY, merchant_id VARCHAR(64) NOT NULL REFERENCES {s}.catalog_merchants(merchant_id),
  title TEXT NOT NULL, description TEXT, tags TEXT[], attrs JSONB, updated_at TIMESTAMPTZ,
  title_lower TEXT GENERATED ALWAYS AS (lower(title)) STORED);
CREATE TABLE {s}.catalog_offers (
  offer_id VARCHAR(255) PRIMARY KEY, product_id TEXT NOT NULL REFERENCES {s}.catalog_products(product_id) ON DELETE CASCADE,
  merchant_id VARCHAR(64) NOT NULL, price NUMERIC(12,4) NOT NULL, currency CHAR(3) NOT NULL, stock INTEGER);
CREATE TABLE {s}.external_product_seeds (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, url TEXT NOT NULL, market TEXT,
  price_amount NUMERIC, notes TEXT, created_by_employee_id TEXT);
"""


async def _connect():
    import asyncpg

    return await asyncpg.connect(DATABASE_URL)


async def _reset(conn) -> None:
    for schema in (SRC, DST):
        await conn.execute("DROP SCHEMA IF EXISTS %s CASCADE; CREATE SCHEMA %s" % (schema, schema))
        await conn.execute(DDL.format(s=schema))
    # Prod-only tables that must never travel.
    await conn.execute(
        "CREATE TABLE %s.orders (order_id TEXT PRIMARY KEY, customer_email TEXT, merchant_id TEXT)" % SRC)
    await conn.execute("INSERT INTO %s.orders VALUES ('o1', 'buyer@person.example', 'm1')" % SRC)
    # A sandbox-local table hanging off a copied one (as orders / wallets do in the real schema).
    await conn.execute(
        "CREATE TABLE %s.sandbox_local (id SERIAL PRIMARY KEY, merchant_id VARCHAR(50) "
        "REFERENCES %s.merchant_onboarding(merchant_id) ON DELETE CASCADE)" % (DST, DST))


async def _seed(conn) -> None:
    await conn.execute(f"""
    INSERT INTO {SRC}.merchant_onboarding (merchant_id, business_name, store_url, contact_email, contact_phone,
        status, mcp_access_token, api_key, api_key_hash, kyc_documents, rejection_reason, psp_sandbox_key, is_test)
    VALUES ('m1', 'Brand One', 'https://one.example', 'owner@realbrand.example', '+1-555-0100', 'approved',
        'shpat_SECRET_TOKEN', 'merchant-api-key-SECRET', 'hash', '{{"doc": "kyc-passport-SECRET"}}', NULL, 'x', false),
           ('m2', 'Brand, "Two"', NULL, 'owner2@realbrand.example', NULL, 'approved', NULL, NULL, NULL, NULL,
        'internal note SECRET', NULL, true);
    INSERT INTO {SRC}.merchant_stores (store_id, merchant_id, platform, name, domain, api_key, realtime_enabled,
        support_email, api_endpoint, order_writeback_canary_order_id)
    VALUES ('s1', 'm1', 'shopify', 'One', 'one.myshopify.com', 'store-api-SECRET', true,
        'support@realstore.example', 'https://one.myshopify.com/api', 'order-123');
    INSERT INTO {SRC}.catalog_merchants VALUES ('m1', 'Brand One', 'active', true, '{{"tier": "a"}}'),
                                                ('m2', 'Brand Two', 'active', false, NULL);
    INSERT INTO {SRC}.catalog_products (product_id, merchant_id, title, description, tags, attrs, updated_at)
    VALUES ('p1', 'm1', 'Lip Tint', E'multi\\nline, "quoted"', ARRAY['a','b,c'], '{{"k": [1, 2.50]}}', '2026-11-25T01:02:03.456789Z'),
           ('p2', 'm2', 'Serum', '', NULL, NULL, NULL);
    INSERT INTO {SRC}.catalog_offers VALUES ('o-p1', 'p1', 'm1', 12.3400, 'USD', 5), ('o-p2', 'p2', 'm2', 0.0100, 'KRW', NULL);
    INSERT INTO {SRC}.external_product_seeds (url, market, price_amount, notes, created_by_employee_id)
    VALUES ('https://shop.example/x', 'US', 19.99, 'internal note SECRET', 'staff-42'),
           ('https://shop.example/y', 'US', NULL, NULL, NULL);
    """)


@pytest.fixture
async def db():
    conn = await _connect()
    await _reset(conn)
    await _seed(conn)
    try:
        yield conn
    finally:
        for schema in (SRC, DST):
            await conn.execute("DROP SCHEMA IF EXISTS %s CASCADE" % schema)
        await conn.close()


async def _rows(conn, schema, table, cols, order):
    return [tuple(r) for r in await conn.fetch(
        "SELECT %s FROM %s.%s ORDER BY %s" % (", ".join(cols), schema, table, order))]


async def _export(conn, store, stamp="20261125T0100Z"):
    return await C.export_catalog(conn, store, stamp=stamp, commit="abc123", specs=SPECS, schema=SRC)


async def _import(conn, store, **kw):
    return await C.import_catalog(conn, store, specs=SPECS, schema=DST, **kw)


async def test_round_trip_copies_catalog_values_exactly(db, tmp_path):
    store = C.LocalDirStore(tmp_path)
    manifest = await _export(db, store)
    assert [t["name"] for t in manifest["tables"]] == [s.name for s in SPECS]
    await _import(db, store)
    cols = "product_id, merchant_id, title, description, tags, attrs, updated_at, title_lower"
    assert await _rows(db, DST, "catalog_products", [cols], "product_id") == \
        await _rows(db, SRC, "catalog_products", [cols], "product_id")
    assert await _rows(db, DST, "catalog_offers", ["*"], "offer_id") == \
        await _rows(db, SRC, "catalog_offers", ["*"], "offer_id")
    assert await _rows(db, DST, "catalog_merchants", ["*"], "merchant_id") == \
        await _rows(db, SRC, "catalog_merchants", ["*"], "merchant_id")
    # NULL and '' stay distinct; prices keep their exact values.
    p2 = await db.fetchrow("SELECT description, tags FROM %s.catalog_products WHERE product_id='p2'" % DST)
    assert p2["description"] == "" and p2["tags"] is None
    assert str(await db.fetchval("SELECT price FROM %s.catalog_offers WHERE offer_id='o-p1'" % DST)) == "12.3400"


async def test_no_personal_data_or_secret_leaves_prod(db, tmp_path):
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    blob = b""
    for name in store.list(""):
        data = store.get(name)
        blob += gzip.decompress(data) if name.endswith(".gz") else data
    for secret in SECRETS:
        assert secret.encode() not in blob, secret
    assert not any("orders" in n for n in store.list(""))
    await _import(db, store)
    mo = await db.fetchrow("SELECT * FROM %s.merchant_onboarding WHERE merchant_id='m1'" % DST)
    assert mo["contact_email"] == "sandbox-merchant@example.invalid"
    for column in ("contact_phone", "mcp_access_token", "api_key", "api_key_hash", "kyc_documents",
                   "rejection_reason", "psp_sandbox_key", "signup_source"):
        assert mo[column] is None, column
    assert mo["business_name"] == "Brand One" and mo["store_url"] == "https://one.example"
    st = await db.fetchrow("SELECT * FROM %s.merchant_stores WHERE store_id='s1'" % DST)
    assert st["api_key"] is None and st["support_email"] is None and st["api_endpoint"] is None
    assert st["order_writeback_canary_order_id"] is None
    assert st["realtime_enabled"] is False  # prod's true is overridden: no live store fetch
    assert st["domain"] == "one.myshopify.com"
    seed = await db.fetchrow("SELECT * FROM %s.external_product_seeds WHERE url='https://shop.example/x'" % DST)
    assert seed["notes"] is None and seed["created_by_employee_id"] is None
    assert str(seed["price_amount"]) == "19.99"


async def test_sequences_are_bumped_past_the_copied_ids(db, tmp_path):
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    await _import(db, store)
    await db.execute("INSERT INTO %s.external_product_seeds (url) VALUES ('https://local.example')" % DST)
    await db.execute("INSERT INTO %s.merchant_onboarding (merchant_id, business_name, contact_email) "
                     "VALUES ('local', 'Local', 'x@example.invalid')" % DST)


async def test_a_second_copy_mirrors_prod_and_keeps_sandbox_rows_of_kept_merchants(db, tmp_path):
    store = C.LocalDirStore(tmp_path)
    await _export(db, store, "20261125T0100Z")
    await _import(db, store)
    await db.execute("INSERT INTO %s.sandbox_local (merchant_id) VALUES ('m1'), ('m2')" % DST)
    await db.execute("INSERT INTO %s.catalog_merchants (merchant_id) VALUES ('sandbox-only')" % DST)
    # Prod changes: m2 goes away, a price moves, an offer appears.
    await db.execute(f"""
        DELETE FROM {SRC}.catalog_offers WHERE merchant_id='m2'; DELETE FROM {SRC}.catalog_products WHERE merchant_id='m2';
        DELETE FROM {SRC}.catalog_merchants WHERE merchant_id='m2'; DELETE FROM {SRC}.merchant_onboarding WHERE merchant_id='m2';
        UPDATE {SRC}.catalog_offers SET price = 11.0000 WHERE offer_id='o-p1';
        INSERT INTO {SRC}.catalog_offers VALUES ('o-p1-b', 'p1', 'm1', 13.5000, 'USD', 1);""")
    await _export(db, store, "20261126T0100Z")
    report = await _import(db, store)
    assert report["stamp"] == "20261126T0100Z"
    for table, order in (("catalog_offers", "offer_id"), ("catalog_merchants", "merchant_id"),
                         ("catalog_products", "product_id")):
        assert await _rows(db, DST, table, ["*"], order) == await _rows(db, SRC, table, ["*"], order), table
    assert await db.fetchval("SELECT count(*) FROM %s.merchant_onboarding" % DST) == 1
    # Sandbox rows of the merchant prod kept survive; the removed merchant's cascade with it.
    assert [r["merchant_id"] for r in await db.fetch("SELECT merchant_id FROM %s.sandbox_local" % DST)] == ["m1"]


async def test_an_unfinished_export_is_never_imported(db, tmp_path):
    store = C.LocalDirStore(tmp_path)
    await _export(db, store, "20261125T0100Z")
    store.put("20261126T0100Z/catalog_offers.csv.gz", b"partial")  # newer, no manifest
    report = await _import(db, store)
    assert report["stamp"] == "20261125T0100Z"


async def test_an_export_never_overwrites_a_stamp(db, tmp_path):
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    with pytest.raises(C.CopyRefused, match="never overwrites"):
        await _export(db, store)


def _rewrite_manifest(tmp_path, stamp, mutate):
    path = tmp_path / stamp / C.MANIFEST_NAME
    manifest = json.loads(path.read_text())
    mutate(manifest)
    path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("tamper,match", [
    ("sha", "sha256"),
    ("rows", "row count"),
    ("dropped_column", "dropped column"),
    ("keep_column", "keep list"),
    ("unknown_table", "not in CATALOG_TABLES"),
    ("never_copy_table", "never copied: orders"),
    ("missing_table", "missing table"),
])
async def test_a_tampered_copy_is_refused_and_the_sandbox_is_untouched(db, tmp_path, tamper, match):
    store = C.LocalDirStore(tmp_path)
    stamp = "20261125T0100Z"
    await _export(db, store, stamp)

    def mutate(m):
        offers = next(t for t in m["tables"] if t["name"] == "catalog_offers")
        if tamper == "sha":
            offers["sha256"] = "0" * 64
        elif tamper == "rows":
            offers["rows"] += 1
        elif tamper == "dropped_column":
            next(t for t in m["tables"] if t["name"] == "external_product_seeds")["columns"].append("notes")
        elif tamper == "keep_column":
            next(t for t in m["tables"] if t["name"] == "merchant_onboarding")["columns"].append("api_key")
        elif tamper == "unknown_table":
            m["tables"].append({"name": "mystery_table", "columns": ["id"], "rows": 1,
                                "file": "%s/mystery_table.csv.gz" % stamp, "sha256": "x"})
        elif tamper == "never_copy_table":
            m["tables"].append({"name": "orders", "columns": ["order_id"], "rows": 1,
                                "file": "%s/orders.csv.gz" % stamp, "sha256": "x"})
        elif tamper == "missing_table":
            m["tables"] = [t for t in m["tables"] if t["name"] != "catalog_offers"]

    _rewrite_manifest(tmp_path, stamp, mutate)
    with pytest.raises(C.CopyRefused, match=match):
        await _import(db, store)
    assert await db.fetchval("SELECT count(*) FROM %s.catalog_offers" % DST) == 0


async def test_import_rolls_back_whole_when_the_sandbox_schema_lags(db, tmp_path):
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    await db.execute("ALTER TABLE %s.catalog_offers DROP COLUMN stock" % DST)
    with pytest.raises(C.CopyRefused, match="lacks column"):
        await _import(db, store)
    assert await db.fetchval("SELECT count(*) FROM %s.catalog_merchants" % DST) == 0


async def test_export_refuses_an_unclassified_risky_column_and_writes_nothing(db, tmp_path):
    await db.execute("ALTER TABLE %s.catalog_products ADD COLUMN owner_email TEXT" % SRC)
    store = C.LocalDirStore(tmp_path)
    with pytest.raises(C.CopyRefused, match="catalog_products column owner_email"):
        await _export(db, store)
    assert store.list("") == []


async def test_export_runs_in_a_read_only_snapshot(db, tmp_path):
    """A select expression that tries to write fails: the export cannot write to prod."""
    import asyncpg
    from dataclasses import replace

    await db.execute(f"""
        CREATE TABLE {SRC}.writes (n int);
        CREATE FUNCTION {SRC}.sneaky() RETURNS text LANGUAGE sql VOLATILE AS
          $$ INSERT INTO {SRC}.writes VALUES (1); SELECT 'x'::text $$;""")
    specs = [replace(s, replace={"merchant_name": "%s.sneaky()" % SRC}) if s.name == "catalog_merchants" else s
             for s in SPECS]
    with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
        await C.export_catalog(db, C.LocalDirStore(tmp_path), stamp="20261125T0100Z", specs=specs, schema=SRC)
    assert await db.fetchval("SELECT count(*) FROM %s.writes" % SRC) == 0


async def test_the_import_applies_the_overrides_itself(db, tmp_path):
    """An export that forgot an override (realtime_enabled=false, the contact placeholder) still
    lands safe: the importer re-applies the spec's replacements on its own side."""
    from dataclasses import replace

    lax = [replace(s, replace={}) if s.name in ("merchant_stores", "merchant_onboarding") else s for s in SPECS]
    lax = [replace(s, allow_risky={"contact_email": "test only"}) if s.name == "merchant_onboarding" else s
           for s in lax]
    store = C.LocalDirStore(tmp_path)
    await C.export_catalog(db, store, stamp="20261125T0100Z", specs=lax, schema=SRC)
    await _import(db, store)
    assert await db.fetchval("SELECT realtime_enabled FROM %s.merchant_stores WHERE store_id='s1'" % DST) is False
    assert await db.fetchval(
        "SELECT contact_email FROM %s.merchant_onboarding WHERE merchant_id='m1'" % DST
    ) == "sandbox-merchant@example.invalid"


async def test_import_rolls_back_when_the_sandbox_ends_up_with_other_rows_than_the_copy(db, tmp_path):
    """The final per-table count check: something on the sandbox side (here a trigger) that leaves
    a table holding rows the copy did not bring rolls the whole import back."""
    await db.execute(f"""
        CREATE FUNCTION {DST}.ghost() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          INSERT INTO {DST}.catalog_merchants (merchant_id) VALUES ('ghost') ON CONFLICT DO NOTHING;
          RETURN NULL;
        END $$;
        CREATE TRIGGER ghost AFTER INSERT ON {DST}.catalog_merchants
          FOR EACH STATEMENT WHEN (pg_trigger_depth() = 0) EXECUTE FUNCTION {DST}.ghost();""")
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    with pytest.raises(C.CopyRefused, match="rows after import"):
        await _import(db, store)
    assert await db.fetchval("SELECT count(*) FROM %s.catalog_merchants" % DST) == 0


# --- S3: every column classified, JSON scrubbed at both ends ----------------------------------------


async def test_json_person_keys_are_redacted_before_they_leave_prod(db, tmp_path):
    await db.execute(f"""UPDATE {SRC}.catalog_merchants SET metadata_json =
        '{{"tier": "a", "reviewed_by": "staff-42", "items": [{{"email": "owner@realbrand.example", "n": 1}}],
           "userId": 7}}' WHERE merchant_id = 'm1';
        UPDATE {SRC}.catalog_products SET attrs = '{{"claims": [{{"claim_text": "hydrating",
           "reviewedBy": "staff-42", "notes": "internal note SECRET"}}]}}' WHERE product_id = 'p1';""")
    store = C.LocalDirStore(tmp_path)
    manifest = await _export(db, store)
    blob = b"".join(gzip.decompress(store.get(n)) for n in store.list("") if n.endswith(".gz"))
    for secret in ("staff-42", "owner@realbrand.example", "internal note SECRET"):
        assert secret.encode() not in blob, secret
    merchants = next(t for t in manifest["tables"] if t["name"] == "catalog_merchants")
    assert merchants["json_columns"] == ["metadata_json"]
    await _import(db, store)
    meta = json.loads(await db.fetchval("SELECT metadata_json FROM %s.catalog_merchants WHERE merchant_id='m1'" % DST))
    assert meta == {"tier": "a", "reviewed_by": "redacted", "items": [{"email": "redacted", "n": 1}], "userId": None}
    attrs = json.loads(await db.fetchval("SELECT attrs FROM %s.catalog_products WHERE product_id='p1'" % DST))
    assert attrs == {"claims": [{"claim_text": "hydrating", "reviewedBy": "redacted", "notes": "redacted"}]}


async def test_a_person_key_holding_an_object_or_array_is_nulled_and_the_export_goes_on(db, tmp_path):
    """SF4: real data has objects and arrays under person keys (human_review.reviewer_decisions,
    {"tokens": [...]}, {"group_by": [...]}); the whole value is nulled instead of refusing the export."""
    await db.execute(f"""UPDATE {SRC}.catalog_merchants SET metadata_json =
        '{{"reviewer": {{"id": "staff-42"}}, "reviewer_decisions": [{{"by": "staff-42", "ok": true}}],
           "tokens": ["a", "b"], "group_by": ["brand"], "tier": "a"}}' WHERE merchant_id = 'm1'""")
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    blob = b"".join(gzip.decompress(store.get(n)) for n in store.list("") if n.endswith(".gz"))
    assert b"staff-42" not in blob
    await _import(db, store)
    meta = json.loads(await db.fetchval("SELECT metadata_json FROM %s.catalog_merchants WHERE merchant_id='m1'" % DST))
    assert meta == {"reviewer": None, "reviewer_decisions": None, "tokens": None, "group_by": None, "tier": "a"}


async def test_the_keys_the_review_found_surviving_are_scrubbed(db, tmp_path):
    """SF3: each key below survived the first scrub on PG15; and a contact detail under a catalog-named
    key (note2) is caught by the value check."""
    survivors = {"emailAddress": "x", "email_address": "x", "E-mail": "x", "mail": "x", "mobileNumber": "x",
                 "shipping_address": {"line1": "1 Main St"}, "review_notes": "x", "audit_note": "x", "name": "x",
                 "signed_by_name": "x", "handle": "x", "request_id": "x", "trace_id": "x", "reason": "x",
                 "rationale": "x"}
    meta = dict(survivors, note2="write to someone@person.example", panthenol="5%", ipa=True, keep="Lip Tint")
    await db.execute("UPDATE %s.catalog_merchants SET metadata_json = $1::jsonb WHERE merchant_id = 'm1'" % SRC,
                     json.dumps(meta))
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    blob = b"".join(gzip.decompress(store.get(n)) for n in store.list("") if n.endswith(".gz"))
    assert b"someone@person.example" not in blob and b"1 Main St" not in blob
    await _import(db, store)
    got = json.loads(await db.fetchval("SELECT metadata_json FROM %s.catalog_merchants WHERE merchant_id='m1'" % DST))
    for key, value in survivors.items():
        assert got[key] == ("redacted" if isinstance(value, str) else None), key
    assert got["note2"] == "write to [redacted]"
    assert got["panthenol"] == "5%" and got["ipa"] is True and got["keep"] == "Lip Tint"  # catalog keys pass


async def test_contact_details_in_text_are_redacted_and_numbers_in_a_rewritten_cell_keep_their_text(db, tmp_path):
    await db.execute(f"""UPDATE {SRC}.catalog_products SET
        description = 'Questions? help@brand.example or +1 555 010 0199. Size 100 ml.',
        attrs = '{{"price": 2.50, "reviewed_by": "staff-42", "n": 10}}' WHERE product_id = 'p1'""")
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    await _import(db, store)
    row = await db.fetchrow("SELECT description, attrs::text AS attrs FROM %s.catalog_products WHERE product_id='p1'" % DST)
    assert row["description"] == "Questions? [redacted] or [redacted]. Size 100 ml."
    assert json.loads(row["attrs"]) == {"price": 2.5, "reviewed_by": "redacted", "n": 10}
    assert '"price": 2.50' in row["attrs"]


async def test_an_email_in_a_key_or_url_column_stops_the_export(db, tmp_path):
    await db.execute("UPDATE %s.external_product_seeds SET url = 'mailto:owner@realbrand.example' WHERE url = 'https://shop.example/x'" % SRC)
    store = C.LocalDirStore(tmp_path)
    with pytest.raises(C.CopyRefused, match="external_product_seeds column url holds an email-shaped value") as excinfo:
        await _export(db, store)
    assert "owner@realbrand.example" not in str(excinfo.value)
    assert store.list("") == []


@pytest.mark.parametrize("ddl", [
    "ALTER TABLE {s}.catalog_merchants ADD COLUMN extra JSONB[]",
    "CREATE DOMAIN {s}.jdom AS JSONB; ALTER TABLE {s}.catalog_merchants ADD COLUMN extra {s}.jdom",
])
async def test_a_json_container_type_is_refused(db, tmp_path, _stand_in_classes, ddl):
    await db.execute(ddl.format(s=SRC))
    _stand_in_classes["catalog_merchants"]["extra"] = (_COLS.JSON, "test container")
    store = C.LocalDirStore(tmp_path)
    with pytest.raises(C.CopyRefused, match="catalog_merchants column extra is a JSON container type"):
        await _export(db, store)
    assert store.list("") == []


async def test_a_column_prod_gains_unclassified_stops_the_export(db, tmp_path):
    await db.execute("ALTER TABLE %s.catalog_products ADD COLUMN shiny_new_field TEXT" % SRC)
    store = C.LocalDirStore(tmp_path)
    with pytest.raises(C.CopyRefused, match="catalog_products column shiny_new_field is not classified"):
        await _export(db, store)
    assert store.list("") == []


async def test_a_json_column_classified_as_text_is_refused(db, tmp_path, _stand_in_classes):
    _stand_in_classes["catalog_merchants"]["metadata_json"] = _COLS.TEXT
    store = C.LocalDirStore(tmp_path)
    with pytest.raises(C.CopyRefused, match="metadata_json is jsonb but not classified JSON"):
        await _export(db, store)
    assert store.list("") == []


async def test_the_import_refuses_a_file_whose_json_was_not_scrubbed(db, tmp_path):
    """The importer does not trust the export's scrub: a file carrying a person value (sha and row
    count made to match) is refused before the database is touched."""
    import hashlib

    store = C.LocalDirStore(tmp_path)
    stamp = "20261125T0100Z"
    await _export(db, store, stamp)
    name = "%s/catalog_merchants.csv.gz" % stamp
    raw = gzip.decompress(store.get(name)).replace(b'{""tier"": ""a""}', b'{""tier"": ""a"", ""reviewed_by"": ""staff-42""}')
    assert b"staff-42" in raw
    data = gzip.compress(raw)
    (tmp_path / name).write_bytes(data)
    _rewrite_manifest(tmp_path, stamp, lambda m: next(t for t in m["tables"] if t["name"] == "catalog_merchants")
                      .update(sha256=hashlib.sha256(data).hexdigest()))
    with pytest.raises(C.CopyRefused, match="survived the scrub"):
        await _import(db, store)
    assert await db.fetchval("SELECT count(*) FROM %s.catalog_merchants" % DST) == 0


@pytest.mark.parametrize("tamper,match", [
    ("format_1", "format_version is not 2"),
    ("json_columns", "JSON columns are not the classified ones"),
    ("unclassified", "unclassified column"),
    ("email_like", "reports email-shaped values"),
])
async def test_the_import_re_applies_the_s3_rules_to_the_manifest(db, tmp_path, tamper, match):
    store = C.LocalDirStore(tmp_path)
    stamp = "20261125T0100Z"
    await _export(db, store, stamp)

    def mutate(m):
        merchants = next(t for t in m["tables"] if t["name"] == "catalog_merchants")
        if tamper == "format_1":
            m["format_version"] = 1
        elif tamper == "json_columns":
            merchants["json_columns"] = []
        elif tamper == "unclassified":
            merchants["columns"].append("shiny_new_field")
        elif tamper == "email_like":
            merchants["email_like_values"] = 3

    _rewrite_manifest(tmp_path, stamp, mutate)
    with pytest.raises(C.CopyRefused, match=match):
        await _import(db, store)
    assert await db.fetchval("SELECT count(*) FROM %s.catalog_merchants" % DST) == 0


async def test_retina_image_urls_and_catalog_digit_groups_survive_the_export(db, tmp_path):
    """Review V1/V2: the value pass must not refuse or damage catalog data."""
    await db.execute(f"""UPDATE {SRC}.external_product_seeds SET url = 'https://cdn.shopify.com/s/files/1/x_480x480@2x.jpg?v=1'
        WHERE url = 'https://shop.example/x';
        UPDATE {SRC}.catalog_products SET description = 'Compatible with 2020 2021 2022 models, 250 500 750 ml',
          attrs = '{{"srcset": "a.jpg 1x, a@2x.jpg 2x", "barcode": "012 3456 7890"}}' WHERE product_id = 'p1'""")
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    await _import(db, store)
    assert await db.fetchval("SELECT count(*) FROM %s.external_product_seeds WHERE url LIKE '%%@2x.jpg%%'" % DST) == 1
    row = await db.fetchrow("SELECT description, attrs FROM %s.catalog_products WHERE product_id='p1'" % DST)
    assert row["description"] == "Compatible with 2020 2021 2022 models, 250 500 750 ml"
    assert json.loads(row["attrs"]) == {"srcset": "a.jpg 1x, a@2x.jpg 2x", "barcode": "012 3456 7890"}


async def test_an_email_used_as_a_json_key_does_not_leave_prod(db, tmp_path):
    await db.execute(f"""UPDATE {SRC}.catalog_merchants SET metadata_json =
        '{{"votes": {{"jane.doe@person.example": 1}}}}' WHERE merchant_id = 'm1'""")
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    blob = b"".join(gzip.decompress(store.get(n)) for n in store.list("") if n.endswith(".gz"))
    assert b"jane.doe@person.example" not in blob


# --- S5: streaming, prod's limits and the deadline ------------------------------------------------


def _files(tmp_path):
    return [p for p in tmp_path.rglob("*") if p.is_file()]


def _with_where(name, where):
    import dataclasses

    return [dataclasses.replace(s, where=where) if s.name == name else s for s in SPECS]


async def _big_products(conn, n=4000, width=2000):
    """~8 MB of products whose text holds newlines, quotes and commas, so records span COPY chunks."""
    await conn.execute(
        "INSERT INTO %s.catalog_products (product_id, merchant_id, title, description, attrs) "
        "SELECT 'big-' || i, 'm1', 'Tint ' || i, repeat(E'line, \"q\"\\n', %d), "
        "jsonb_build_object('k', i, 'blob', repeat('x', 200)) FROM generate_series(1, %d) i"
        % (SRC, width // 10, n))


async def test_a_table_bigger_than_one_copy_chunk_streams_and_round_trips_exactly(db, tmp_path, monkeypatch):
    await _big_products(db)
    calls = []
    real = C._ExportSink.__call__

    async def counting(self, chunk):
        calls.append(len(chunk))
        return await real(self, chunk)

    monkeypatch.setattr(C._ExportSink, "__call__", counting)
    monkeypatch.setattr(C, "READ_CHUNK_BYTES", 4096)  # the import reads the file in small pieces too
    monkeypatch.setattr(C, "INFLATE_STEP_BYTES", 1 << 16)
    store = C.LocalDirStore(tmp_path)
    manifest = await _export(db, store)
    assert len(calls) > 20 and max(calls) < 1 << 20  # asyncpg hands COPY over in pieces
    assert next(t for t in manifest["tables"] if t["name"] == "catalog_products")["rows"] == 4002
    await _import(db, store)
    cols = "product_id, merchant_id, title, description, tags, attrs, updated_at, title_lower"
    assert await _rows(db, DST, "catalog_products", [cols], "product_id") == \
        await _rows(db, SRC, "catalog_products", [cols], "product_id")


async def test_a_refusal_late_in_a_big_table_does_not_stall_the_connection(db, tmp_path):
    """asyncpg 0.31 stalls a connection whose COPY sink raises mid-stream; the sink keeps the refusal
    instead, and the export raises it after the COPY. The connection is fine afterwards."""
    await _big_products(db)
    await db.execute("ALTER TABLE %s.catalog_products DROP CONSTRAINT catalog_products_merchant_id_fkey" % SRC)
    await db.execute("UPDATE %s.catalog_products SET merchant_id = 'mailto:owner@realbrand.example' "
                     "WHERE product_id = 'big-3990'" % SRC)
    import asyncio

    store = C.LocalDirStore(tmp_path)
    with pytest.raises(C.CopyRefused, match="catalog_products column merchant_id holds an email-shaped value"):
        await asyncio.wait_for(_export(db, store), timeout=60)
    assert await db.fetchval("SELECT 1") == 1
    assert not db.is_in_transaction()
    assert _files(tmp_path) == []


async def test_the_limits_are_local_to_the_export_transaction(db, tmp_path):
    names = ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout",
             "client_connection_check_interval")
    before = [await db.fetchval("SHOW %s" % n) for n in names]
    await _export(db, C.LocalDirStore(tmp_path))
    assert [await db.fetchval("SHOW %s" % n) for n in names] == before
    assert await db.fetchval("SHOW application_name") != "sandbox_catalog_export"


async def test_the_export_does_not_queue_behind_a_migrations_lock(db, tmp_path):
    other = await _connect()
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("LOCK TABLE %s.catalog_offers IN ACCESS EXCLUSIVE MODE" % SRC)
        with pytest.raises(C.CopyRefused, match="hit its lock_timeout on the source; nothing was written"):
            await C.export_catalog(db, C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=SPECS,
                                   schema=SRC, lock_timeout="200ms")
        await tx.rollback()
    finally:
        await other.close()
    assert _files(tmp_path) == []
    assert await db.fetchval("SELECT 1") == 1


async def test_a_statement_past_its_timeout_is_refused(db, tmp_path):
    specs = _with_where("catalog_offers", "(SELECT true FROM pg_sleep(2))")
    with pytest.raises(C.CopyRefused, match="hit its statement_timeout on the source"):
        await C.export_catalog(db, C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=specs,
                               schema=SRC, statement_timeout="200ms")
    assert _files(tmp_path) == []
    assert await db.fetchval("SELECT 1") == 1


async def test_the_deadline_ends_the_session_on_the_server(db, tmp_path):
    import asyncio
    import time

    victim = await _connect()
    pid = victim.get_server_pid()
    specs = _with_where("catalog_offers", "(SELECT true FROM pg_sleep(30))")
    started = time.monotonic()
    try:
        with pytest.raises(C.CopyRefused, match="passed its 0.5s deadline; nothing was written"):
            await C.export_catalog(victim, C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=specs,
                                   schema=SRC, deadline_seconds=0.5)
        assert time.monotonic() - started < 10
        assert victim.is_closed()
        # client_connection_check_interval (5s) makes the server notice the closed socket mid-statement.
        for _ in range(150):
            if not await db.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid = $1", pid):
                break
            await asyncio.sleep(0.1)
        assert await db.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid = $1", pid) == 0
    finally:
        if not victim.is_closed():
            await victim.close()
    assert _files(tmp_path) == []


async def test_a_file_changed_between_the_check_and_the_load_rolls_the_import_back(db, tmp_path):
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    await _import(db, store)
    before = await _rows(db, DST, "catalog_offers", ["*"], "offer_id")
    await db.execute("UPDATE %s.catalog_offers SET price = 99 WHERE offer_id = 'o-p1'" % SRC)
    await _export(db, store, stamp="20261126T0100Z")

    class Swapped(C.LocalDirStore):
        """The load (second read) of catalog_offers gets other bytes than the check (first read)."""
        reads = {}

        def open_reader(self, name, generation):
            self.reads[name] = self.reads.get(name, 0) + 1
            if name.endswith("catalog_offers.csv.gz") and self.reads[name] == 2:
                yield gzip.compress(b'"o-p1","p1","m1","1.0000","USD","5"\n"o-p2","p2","m2","0.0100","KRW",\n')
                return
            yield from super().open_reader(name, generation)

    with pytest.raises(C.CopyRefused, match="catalog_offers: the file changed between the check and the load"):
        await _import(db, Swapped(tmp_path))
    assert await _rows(db, DST, "catalog_offers", ["*"], "offer_id") == before
    assert await db.fetchval("SELECT 1") == 1 and not db.is_in_transaction()


async def test_the_import_refuses_a_cut_off_file_whose_manifest_matches_it(db, tmp_path):
    import hashlib

    store = C.LocalDirStore(tmp_path)
    await _export(db, store)
    name = "20261125T0100Z/catalog_offers.csv.gz"
    cut = store.get(name)[:-6]
    (tmp_path / name).write_bytes(cut)
    path = tmp_path / "20261125T0100Z" / C.MANIFEST_NAME
    manifest = json.loads(path.read_text())
    next(t for t in manifest["tables"] if t["name"] == "catalog_offers")["sha256"] = hashlib.sha256(cut).hexdigest()
    path.write_text(json.dumps(manifest))
    with pytest.raises(C.CopyRefused, match="catalog_offers: the file's gzip stream is cut off"):
        await _import(db, store)
    assert await db.fetchval("SELECT count(*) FROM %s.catalog_offers" % DST) == 0


async def test_both_reads_of_a_file_name_the_generation_the_import_pinned(db, tmp_path):
    store = C.LocalDirStore(tmp_path)
    await _export(db, store)

    class Pinned(C.LocalDirStore):
        seen = []

        def generation(self, name):
            return "gen-" + name

        def open_reader(self, name, generation):
            self.seen.append((name, generation))
            yield from super().open_reader(name, generation)

    pinned = Pinned(tmp_path)
    await _import(db, pinned)
    assert len(pinned.seen) == 2 * len(SPECS)
    assert all(generation == "gen-" + name for name, generation in pinned.seen)


async def test_a_long_run_without_an_at_sign_exports_well_within_the_deadline(db, tmp_path):
    """The review's probe: one 150k-character title held the event loop for 33.8 s, past a 0.5 s
    deadline. The email scan is linear now, so a 2M-character text exports in time and exactly."""
    import time

    await db.execute(
        "INSERT INTO %s.catalog_products (product_id, merchant_id, title, description) VALUES "
        "('long-1', 'm1', repeat('a', 150000), repeat('a.', 1000000)), "
        "('long-2', 'm1', repeat('img@2x.png+', 20000), repeat('b', 2000000) || '@')" % SRC)
    store = C.LocalDirStore(tmp_path)
    started = time.monotonic()
    await C.export_catalog(db, store, stamp="20261127T0100Z", commit="abc123", specs=SPECS, schema=SRC,
                           deadline_seconds=5)
    assert time.monotonic() - started < 5
    await _import(db, store)
    cols = "product_id, title, description"
    assert await _rows(db, DST, "catalog_products", [cols], "product_id") == \
        await _rows(db, SRC, "catalog_products", [cols], "product_id")


class _TerminateSpy:
    """A real connection whose terminate() is timed (asyncpg's Connection has __slots__)."""

    def __init__(self, conn):
        self._conn = conn
        self.terminated_at = None

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def terminate(self):
        import time

        self.terminated_at = time.monotonic()
        self._conn.terminate()


async def test_the_deadline_terminates_on_time_while_a_block_is_being_scrubbed(db, tmp_path, monkeypatch):
    import time

    real_scrub = C._TableScrub.scrub

    def slow_scrub(self, text, null_token):
        if self.table == "catalog_products":
            time.sleep(3)  # a block that takes long to scrub
        return real_scrub(self, text, null_token)

    monkeypatch.setattr(C._TableScrub, "scrub", slow_scrub)
    victim = await _connect()
    spy = _TerminateSpy(victim)
    started = time.monotonic()
    try:
        with pytest.raises(C.CopyRefused, match="passed its 0.5s deadline; nothing was written"):
            await C.export_catalog(spy, C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=SPECS,
                                   schema=SRC, deadline_seconds=0.5)
        assert spy.terminated_at is not None and spy.terminated_at - started < 1.5
        assert victim.is_closed()
    finally:
        if not victim.is_closed():
            await victim.close()
    assert _files(tmp_path) == []


async def test_the_source_killing_an_idle_export_is_a_named_refusal(db, tmp_path, monkeypatch):
    import asyncio

    real_columns = C._table_columns
    calls = []

    async def stall_before_the_second_table(conn, schema, table, **kw):
        calls.append(table)
        if len(calls) == 2:
            await asyncio.sleep(2.5)  # idle in the transaction, past its 1s idle_in_transaction timeout
        return await real_columns(conn, schema, table, **kw)

    monkeypatch.setattr(C, "_table_columns", stall_before_the_second_table)
    victim = await _connect()
    try:
        with pytest.raises(C.CopyRefused, match="source ended the export's session"):
            await C.export_catalog(victim, C.LocalDirStore(tmp_path), stamp="20261127T0100Z", specs=SPECS,
                                   schema=SRC, idle_timeout="1s")
    finally:
        if not victim.is_closed():
            await victim.close()
    assert _files(tmp_path) == []


async def test_nothing_reaches_an_upload_after_the_deadline_on_a_real_connection(db, tmp_path, monkeypatch):
    """The review's probe: a forced 4 s scrub on a 0.5 s deadline wrote its block into the unfinished
    upload before the abort. Now the block is dropped when its worker thread returns."""
    import time

    real_scrub = C._TableScrub.scrub

    def slow_scrub(self, text, null_token):
        if self.table == "catalog_products":
            time.sleep(2)
        return real_scrub(self, text, null_token)

    monkeypatch.setattr(C._TableScrub, "scrub", slow_scrub)
    writes = []

    class Store(C.LocalDirStore):
        def open_writer(self, name):
            writer = super().open_writer(name)
            real_write = writer.write

            def write(data):
                writes.append((time.monotonic(), name))
                real_write(data)

            writer.write = write
            return writer

    victim = await _connect()
    spy = _TerminateSpy(victim)
    try:
        with pytest.raises(C.CopyRefused, match="passed its 0.5s deadline; nothing was written"):
            await C.export_catalog(spy, Store(tmp_path), stamp="20261127T0100Z", specs=SPECS, schema=SRC,
                                   deadline_seconds=0.5)
    finally:
        if not victim.is_closed():
            await victim.close()
    assert spy.terminated_at is not None
    assert [w for w in writes if w[0] >= spy.terminated_at] == []
    assert not any(name.endswith("/catalog_products.csv.gz") for _, name in writes)
    assert _files(tmp_path) == []


async def test_the_export_needs_select_on_the_catalog_tables_and_nothing_else(db, tmp_path):
    """The prod export runs as a catalog-only role (infra Q3): SELECT on the catalog tables that
    exist, USAGE on their schema, a read-only default, nothing else. This export, run as such a
    role, gives byte-identical files to one run as the owner; and the role reads no other table."""
    import secrets as _secrets
    import urllib.parse

    import asyncpg

    role = "sbx_catalog_export_probe_%s" % _secrets.token_hex(4)
    password = _secrets.token_hex(16)
    present = [s.name for s in SPECS
               if await db.fetchval("SELECT to_regclass($1) IS NOT NULL", "%s.%s" % (SRC, s.name))]
    await db.execute("CREATE ROLE %s LOGIN NOINHERIT PASSWORD '%s'" % (role, password))
    probe = None
    try:
        await db.execute("ALTER ROLE %s SET default_transaction_read_only = on" % role)
        await db.execute("GRANT USAGE ON SCHEMA %s TO %s" % (SRC, role))
        for name in present:
            await db.execute("GRANT SELECT ON %s.%s TO %s" % (SRC, name, role))
        url = urllib.parse.urlsplit(DATABASE_URL)
        netloc = "%s:%s@%s" % (role, password, url.hostname + (":%d" % url.port if url.port else ""))
        probe = await asyncpg.connect(urllib.parse.urlunsplit(url._replace(netloc=netloc)))
        assert await probe.fetchval("SHOW default_transaction_read_only") == "on"
        assert not await probe.fetchval("SELECT has_table_privilege(current_user, $1, 'SELECT')", "%s.orders" % SRC)

        as_role = await C.export_catalog(probe, C.LocalDirStore(tmp_path / "role"), stamp="20261127T0100Z",
                                         commit="abc123", specs=SPECS, schema=SRC)
        as_owner = await C.export_catalog(db, C.LocalDirStore(tmp_path / "owner"), stamp="20261127T0100Z",
                                          commit="abc123", specs=SPECS, schema=SRC)
        key = [(t["name"], t["rows"], t["sha256"], tuple(t["columns"])) for t in as_role["tables"]]
        assert key == [(t["name"], t["rows"], t["sha256"], tuple(t["columns"])) for t in as_owner["tables"]]
        assert as_role["absent_optional_tables"] == as_owner["absent_optional_tables"]
    finally:
        if probe is not None:
            await probe.close()
        await db.execute("DROP OWNED BY %s" % role)
        await db.execute("DROP ROLE %s" % role)


async def test_without_select_an_optional_catalog_table_is_silently_recorded_absent(db, tmp_path):
    """Why the per-run grant must cover EVERY catalog table that exists (the role design's check 9):
    information_schema hides a table the role cannot read, so the export records an optional one
    as absent and carries on."""
    import dataclasses
    import secrets as _secrets
    import urllib.parse

    import asyncpg

    hidden = "catalog_offers"
    specs = [dataclasses.replace(s, optional=True) if s.name == hidden else s for s in SPECS]
    role = "sbx_catalog_export_probe_%s" % _secrets.token_hex(4)
    password = _secrets.token_hex(16)
    await db.execute("CREATE ROLE %s LOGIN NOINHERIT PASSWORD '%s'" % (role, password))
    probe = None
    try:
        await db.execute("GRANT USAGE ON SCHEMA %s TO %s" % (SRC, role))
        for spec in specs:
            if spec.name != hidden and await db.fetchval("SELECT to_regclass($1) IS NOT NULL",
                                                         "%s.%s" % (SRC, spec.name)):
                await db.execute("GRANT SELECT ON %s.%s TO %s" % (SRC, spec.name, role))
        url = urllib.parse.urlsplit(DATABASE_URL)
        netloc = "%s:%s@%s" % (role, password, url.hostname + (":%d" % url.port if url.port else ""))
        probe = await asyncpg.connect(urllib.parse.urlunsplit(url._replace(netloc=netloc)))
        manifest = await C.export_catalog(probe, C.LocalDirStore(tmp_path), stamp="20261127T0100Z",
                                          commit="abc123", specs=specs, schema=SRC)
        assert hidden in manifest["absent_optional_tables"]
        assert hidden not in [t["name"] for t in manifest["tables"]]
        assert await db.fetchval("SELECT count(*) FROM %s.%s" % (SRC, hidden)) > 0  # it exists, with rows
    finally:
        if probe is not None:
            await probe.close()
        await db.execute("DROP OWNED BY %s" % role)
        await db.execute("DROP ROLE %s" % role)
