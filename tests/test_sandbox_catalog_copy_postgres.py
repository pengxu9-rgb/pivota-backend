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


async def test_a_person_key_holding_an_object_stops_the_export_before_any_upload(db, tmp_path):
    await db.execute(f"""UPDATE {SRC}.catalog_merchants SET metadata_json =
        '{{"reviewer": {{"id": "staff-42"}}}}' WHERE merchant_id = 'm1'""")
    store = C.LocalDirStore(tmp_path)
    with pytest.raises(C.CopyRefused, match=r"catalog_merchants column metadata_json: a person key survived the scrub at \$\.reviewer") as excinfo:
        await _export(db, store)
    assert "staff-42" not in str(excinfo.value)
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

    _rewrite_manifest(tmp_path, stamp, mutate)
    with pytest.raises(C.CopyRefused, match=match):
        await _import(db, store)
    assert await db.fetchval("SELECT count(*) FROM %s.catalog_merchants" % DST) == 0
