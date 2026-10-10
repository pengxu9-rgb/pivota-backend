"""Migration 260 against REAL Postgres: the stored own name equals the gateway's query-time fold.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs
`tests/test_*_postgres.py`).

What is proven here, on the migration's real DDL (split by the repo's own splitter):
  * for a table of names -- middle dots and bullets, Latin accents (NFC), punctuation runs,
    NULL product_type, a payload with and without canonical_title / canonical_name, an empty title --
    `own_name_norm` and `name_norm` equal the SAME expression the gateway runs per row
    (`identitySql`), evaluated live by Postgres on the same row;
  * the trigger re-stamps on UPDATE of title and of product_payload (the payload fields reach
    own_name_norm only);
  * rows that existed before the trigger (schema_guard pre-creates the columns on a boot) are
    filled by scripts/backfill_catalog_products_name_norm.py in bounded, resumable batches through
    the trigger (dry run writes nothing; the script refuses to run without the trigger), and
    re-applying the file changes nothing;
  * the carrier CTE's `LIKE '%token%'` prefilter on name_norm stays a superset of the regex it
    guards, and the trigram index statement of migration 261 builds on the column (pg_trgm).

Runs in its own scratch schema against a throwaway database:
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_catalog_products_own_name_norm_postgres.py
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_MIGRATION_260 = Path(__file__).resolve().parent.parent / "db/migrations/260_catalog_products_own_name_norm.sql"
_MIGRATION_261 = Path(__file__).resolve().parent.parent / "db/migrations/261_catalog_products_name_norm_trgm_index.sql"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"own_name_norm_test_{os.getpid()}"

# The gateway's expression over a row alias `p` (PIVOTA-Agent canonicalSearchQualitySql.js:
# `identitySql(...)` over `ownName`'s inputs and over the carrier CTE's `concat_ws(' ', title,
# product_type)`), evaluated by Postgres itself next to the stored column.
_ACCENTED = "ÀÁÂÃÄÅÈÉÊËÌÍÎÏÒÓÔÕÖÙÚÛÜÝàáâãäåèéêëìíîïòóôõöùúûüýÿ"
_FOLDED = "AAAAAAEEEEIIIIOOOOOUUUUYaaaaaaeeeeiiiiooooouuuuyy"


def _identity_sql(expression: str) -> str:
    return (
        "trim(regexp_replace(lower(translate(regexp_replace(coalesce(" + expression + ", ''), '[·•]', '', 'g'), "
        f"'{_ACCENTED}', '{_FOLDED}')), '[^[:alnum:]]+', ' ', 'g'))"
    )


OWN_NAME_EXPR = _identity_sql(
    "concat_ws(' ', p.title, p.product_type, p.product_payload->>'canonical_title', p.product_payload->>'canonical_name')"
)
CARRIER_EXPR = _identity_sql("concat_ws(' ', p.title, p.product_type)")

ROWS = [
    # product_key, title, product_type, payload
    ("k_plain", "Barrier Repair Cream", "Moisturizer", None),
    ("k_dots", "Rouge·Allure • Velvet", "Lipstick", {"canonical_title": "Rouge Allure Velvet"}),
    ("k_accents", "Crème Lancôme Rénergie", "Soin visage", {"canonical_name": "Lancome Renergie"}),
    ("k_punct", "TXA Booster Shot, 5% TXA & 10 Peptides (1.01 fl. oz.)", None, {"canonical_title": "TXA Booster Shot"}),
    ("k_null_type", "Vitamin C Glutathione Essence", None, None),
    ("k_payload_only_names", "Serum", "", {"canonical_title": "Red Collagen + Peptide Smoothie Serum", "canonical_name": "Smoothie Serum"}),
    ("k_empty_title", "", "Toner", {}),
    ("k_mixed", "ÉCLAT  Doré—Highlighter!!", "Face · Make-up", {"canonical_name": "Eclat Dore"}),
]


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _async_url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def _apply_260(scoped) -> None:
    from db.sql_migrations import split_statements

    for statement in split_statements(_MIGRATION_260.read_text(encoding="utf-8")):
        if statement.strip():
            await scoped.execute(statement)


async def _insert(scoped, rows) -> None:
    import json

    for key, title, product_type, payload in rows:
        await scoped.execute(
            "INSERT INTO catalog_products (product_key, title, product_type, product_payload) "
            "VALUES (:k, :t, :pt, CAST(:pl AS JSONB))",
            {"k": key, "t": title, "pt": product_type, "pl": None if payload is None else json.dumps(payload)},
        )


async def _mismatches(scoped):
    return await scoped.fetch_all(
        "SELECT p.product_key, p.name_norm, p.own_name_norm, "
        f"{CARRIER_EXPR} AS expect_name, {OWN_NAME_EXPR} AS expect_own "
        "FROM catalog_products p "
        f"WHERE p.name_norm IS DISTINCT FROM {CARRIER_EXPR} OR p.own_name_norm IS DISTINCT FROM {OWN_NAME_EXPR} "
        "ORDER BY p.product_key"
    )


@pytest.fixture(autouse=True)
async def _scratch_db():
    import databases

    _assert_throwaway_database()
    admin = databases.Database(_async_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    # The scratch schema FIRST (the table, the function, the trigger live there), then public: the
    # trigram operator class lives wherever pg_trgm was installed (public, by 051, on any database
    # that already carries it), and `gin_trgm_ops` must resolve through the path.
    scoped = databases.Database(_async_url(), server_settings={"search_path": f"{_SCHEMA}, public"})
    await scoped.connect()
    try:
        # The columns the migration and the gateway's expression touch, in the scratch schema ONLY
        # (the gate shares one database across files; see the sibling gate tests).
        await scoped.execute(
            "CREATE TABLE catalog_products ("
            " product_key TEXT PRIMARY KEY, title TEXT NOT NULL, product_type TEXT, product_payload JSONB)"
        )
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


async def test_the_stored_columns_equal_the_gateways_fold_on_every_shape_of_name(_scratch_db):
    scoped = _scratch_db
    await _apply_260(scoped)
    await _insert(scoped, ROWS)
    assert await _mismatches(scoped) == []
    rows = {r["product_key"]: (r["name_norm"], r["own_name_norm"]) for r in await scoped.fetch_all(
        "SELECT product_key, name_norm, own_name_norm FROM catalog_products"
    )}
    # Spot values, so the fold is the one meant (not merely self-consistent):
    assert rows["k_dots"] == ("rougeallure velvet lipstick", "rougeallure velvet lipstick rouge allure velvet")
    assert rows["k_accents"] == ("creme lancome renergie soin visage", "creme lancome renergie soin visage lancome renergie")
    assert rows["k_punct"][0] == "txa booster shot 5 txa 10 peptides 1 01 fl oz"
    assert rows["k_null_type"] == ("vitamin c glutathione essence", "vitamin c glutathione essence")
    assert rows["k_payload_only_names"] == ("serum", "serum red collagen peptide smoothie serum smoothie serum")
    assert rows["k_empty_title"] == ("toner", "toner")
    assert rows["k_mixed"] == ("eclat dore highlighter face make up", "eclat dore highlighter face make up eclat dore")


async def test_the_trigger_restamps_on_title_and_payload_updates_and_only_own_name_reads_the_payload(_scratch_db):
    scoped = _scratch_db
    await _apply_260(scoped)
    await _insert(scoped, [("k_u", "Old Name", "Serum", None)])
    await scoped.execute("UPDATE catalog_products SET title = 'Nëw Name' WHERE product_key = 'k_u'")
    row = await scoped.fetch_one("SELECT name_norm, own_name_norm FROM catalog_products WHERE product_key = 'k_u'")
    assert (row["name_norm"], row["own_name_norm"]) == ("new name serum", "new name serum")
    await scoped.execute(
        "UPDATE catalog_products SET product_payload = CAST(:pl AS JSONB) WHERE product_key = 'k_u'",
        {"pl": '{"canonical_title": "Canonical · Name"}'},
    )
    row = await scoped.fetch_one("SELECT name_norm, own_name_norm FROM catalog_products WHERE product_key = 'k_u'")
    assert row["name_norm"] == "new name serum"  # the carrier column never reads the payload
    assert row["own_name_norm"] == "new name serum canonical name"
    assert await _mismatches(scoped) == []


async def test_rows_that_predate_the_trigger_are_backfilled_by_the_script_in_bounded_resumable_batches(_scratch_db):
    import scripts.backfill_catalog_products_name_norm as backfill

    scoped = _scratch_db
    # schema_guard's boot lands the COLUMNS alone; rows written then carry NULL ...
    await scoped.execute("ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS name_norm TEXT, ADD COLUMN IF NOT EXISTS own_name_norm TEXT")
    await _insert(scoped, ROWS)
    assert await scoped.fetch_val("SELECT count(*) FROM catalog_products WHERE own_name_norm IS NULL") == len(ROWS)
    # ... and the migration (function + trigger) does NOT touch them: it is a short transaction.
    await _apply_260(scoped)
    assert await scoped.fetch_val("SELECT count(*) FROM catalog_products WHERE own_name_norm IS NULL") == len(ROWS)
    # Dry run: counts and the plan, no writes.
    dry = await backfill.run_backfill(apply=False, batch_size=3, db=scoped)
    assert dry == {"apply": False, "batch_size": 3, "max_batches": 10, "sleep_ms": 500, "unstamped_before": len(ROWS), "batches_needed": 3, "batches_run": 0, "rows_touched": 0, "remaining": len(ROWS)}
    assert await scoped.fetch_val("SELECT count(*) FROM catalog_products WHERE own_name_norm IS NULL") == len(ROWS)
    # Bounded: one batch of three, five left, in product_key order.
    one = await backfill.run_backfill(apply=True, batch_size=3, max_batches=1, sleep_ms=0, db=scoped)
    assert (one["batches_run"], one["rows_touched"], one["remaining"]) == (1, 3, len(ROWS) - 3)
    stamped = [r["product_key"] for r in await scoped.fetch_all("SELECT product_key FROM catalog_products WHERE own_name_norm IS NOT NULL ORDER BY 1")]
    assert stamped == sorted(k for k, *_ in ROWS)[:3]
    # Resumable: the next run takes the rest and every row equals the gateway's fold.
    rest = await backfill.run_backfill(apply=True, batch_size=3, max_batches=10, sleep_ms=0, db=scoped)
    assert (rest["batches_run"], rest["rows_touched"], rest["remaining"]) == (2, len(ROWS) - 3, 0)
    assert await _mismatches(scoped) == []
    # Done means done: a further run touches nothing, and a second apply of the file changes nothing.
    again = await backfill.run_backfill(apply=True, batch_size=3, max_batches=10, sleep_ms=0, db=scoped)
    assert (again["batches_run"], again["rows_touched"], again["remaining"]) == (0, 0, 0)
    before = await scoped.fetch_all("SELECT product_key, name_norm, own_name_norm FROM catalog_products ORDER BY 1")
    await _apply_260(scoped)  # idempotent: CREATE OR REPLACE, IF NOT EXISTS, DROP TRIGGER IF EXISTS
    after = await scoped.fetch_all("SELECT product_key, name_norm, own_name_norm FROM catalog_products ORDER BY 1")
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    triggers = await scoped.fetch_val(
        "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE t.tgname = 'trg_catalog_products_stamp_name_norm' AND c.relname = 'catalog_products' AND n.nspname = :schema AND NOT t.tgisinternal",
        {"schema": _SCHEMA},
    )
    assert triggers == 1


async def test_the_script_refuses_to_run_without_the_trigger(_scratch_db):
    import scripts.backfill_catalog_products_name_norm as backfill

    scoped = _scratch_db
    await scoped.execute("ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS name_norm TEXT, ADD COLUMN IF NOT EXISTS own_name_norm TEXT")
    await _insert(scoped, ROWS[:2])
    with pytest.raises(backfill.TriggerMissing):
        await backfill.run_backfill(apply=False, db=scoped)
    with pytest.raises(backfill.TriggerMissing):
        await backfill.run_backfill(apply=True, db=scoped)
    assert await scoped.fetch_val("SELECT count(*) FROM catalog_products WHERE own_name_norm IS NULL") == 2


async def test_the_like_prefilter_on_name_norm_is_a_superset_of_the_regex_and_the_trigram_index_builds(_scratch_db):
    scoped = _scratch_db
    await _apply_260(scoped)
    await _insert(scoped, ROWS + [("k_barrier", "Barrier Moisturizer SPF", "Cream", None), ("k_moist", "Daily Moisturizer", "Cream", None)])
    # The carrier CTE's rule for the query 'barrier moisturizer': LIKE on each token, then the lookahead regex.
    regex_rows = {r["product_key"] for r in await scoped.fetch_all(
        "SELECT product_key FROM catalog_products WHERE name_norm ~ '^(?=.*(^| )barrier($| ))(?=.*(^| )moisturizer($| ))'"
    )}
    like_rows = {r["product_key"] for r in await scoped.fetch_all(
        "SELECT product_key FROM catalog_products WHERE name_norm LIKE '%barrier%' AND name_norm LIKE '%moisturizer%'"
    )}
    assert regex_rows == {"k_plain", "k_barrier"} or regex_rows == {"k_barrier"}
    assert regex_rows <= like_rows
    # 261's statements, split by the repo's own (comment- and quote-aware) splitter, with CONCURRENTLY
    # removed: it cannot run inside the fixture's transaction, and the index itself is what is tested.
    from db.sql_migrations import split_statements

    for stmt in split_statements(re.sub(r"\bCONCURRENTLY\b", "", _MIGRATION_261.read_text(encoding="utf-8"))):
        body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--")).strip()
        if body:
            await scoped.execute(body)
    indexes = await scoped.fetch_val(
        "SELECT count(*) FROM pg_indexes WHERE schemaname = :schema AND indexname = 'idx_catalog_products_name_norm_trgm'",
        {"schema": _SCHEMA},
    )
    assert indexes == 1
