"""Shape of migrations 260/261 (catalog_products.name_norm / own_name_norm; PIVOTA-Agent #2404).

The whole point of the column is that it equals what the gateway folds at query time, so the
first pin is the fold EXPRESSION, character for character, against the gateway's `identitySql`
(PIVOTA-Agent src/services/canonicalSearchQualitySql.js). The gateway is another repository, so
the expression is pinned here as the literal the two sides agreed on; a change on either side
has to change this constant, and the Postgres gate (tests/test_catalog_products_own_name_norm_
postgres.py) proves the column equals the expression on real rows.

SQLite-safe: reads files, never a database.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UP_260 = REPO / "db" / "migrations" / "260_catalog_products_own_name_norm.sql"
UP_261 = REPO / "db" / "migrations" / "261_catalog_products_name_norm_trgm_index.sql"
DOWN_260 = REPO / "db" / "migrations" / "down" / "260_catalog_products_own_name_norm_down.sql"
DOWN_261 = REPO / "db" / "migrations" / "down" / "261_catalog_products_name_norm_trgm_index_down.sql"

# PIVOTA-Agent src/services/canonicalSearchQualitySql.js `identitySql(expression)`, with the
# gateway's `coalesce(${expression}, '')` argument replaced by the function's parameter.
IDENTITY_ACCENTED = "ÀÁÂÃÄÅÈÉÊËÌÍÎÏÒÓÔÕÖÙÚÛÜÝàáâãäåèéêëìíîïòóôõöùúûüýÿ"
IDENTITY_FOLDED = "AAAAAAEEEEIIIIOOOOOUUUUYaaaaaaeeeeiiiiooooouuuuyy"
GATEWAY_IDENTITY_SQL = (
    "trim(regexp_replace(lower(translate(regexp_replace(coalesce(expression, ''), '[·•]', '', 'g'), "
    f"'{IDENTITY_ACCENTED}', '{IDENTITY_FOLDED}')), '[^[:alnum:]]+', ' ', 'g'))"
)
# The gateway's own-name inputs (canonicalSearchQualitySql.js `ownName`) and the carrier CTE's
# (`carriesAll`: title + product_type only).
OWN_NAME_INPUTS = "concat_ws(' ', NEW.title, NEW.product_type,\n              NEW.product_payload->>'canonical_title', NEW.product_payload->>'canonical_name')"
CARRIER_INPUTS = "concat_ws(' ', NEW.title, NEW.product_type)"


def _sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_fold_function_is_the_gateways_identity_sql_character_for_character() -> None:
    sql = _sql(UP_260)
    assert "CREATE OR REPLACE FUNCTION catalog_products_identity_fold(expression TEXT)" in sql
    body = re.search(r"AS \$\$\s*SELECT (.*?)\s*\$\$;", sql, re.DOTALL)
    assert body, "the fold function body is not a single SELECT"
    assert body.group(1).strip() == GATEWAY_IDENTITY_SQL
    # Pure, so an index on it or a generated column could use it; one definition on both sides.
    assert "IMMUTABLE" in sql and "PARALLEL SAFE" in sql
    assert len(IDENTITY_ACCENTED) == len(IDENTITY_FOLDED)  # translate() maps position by position


def test_both_columns_are_stamped_by_one_trigger_over_the_gateways_inputs_and_backfilled() -> None:
    sql = _sql(UP_260)
    assert "ADD COLUMN IF NOT EXISTS name_norm TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS own_name_norm TEXT" in sql
    assert f"NEW.name_norm := catalog_products_identity_fold({CARRIER_INPUTS});" in sql
    assert "NEW.own_name_norm := catalog_products_identity_fold(\n    " + OWN_NAME_INPUTS + ");" in sql
    # Unconditional: every insert and update re-stamps, so a NULL that schema_guard's column add
    # left behind is repaired by the row's next write.
    assert "BEFORE INSERT OR UPDATE ON catalog_products" in sql
    assert "FOR EACH ROW" in sql
    assert "DROP TRIGGER IF EXISTS trg_catalog_products_stamp_name_norm ON catalog_products;" in sql
    # No backfill in the migration: existing rows are stamped by the bounded, resumable, throttled
    # script (below), through this same trigger, so the file stays a short transaction.
    assert "UPDATE catalog_products" not in sql
    # No CONCURRENTLY here: this file must run in ONE transaction (function + columns + trigger). The
    # runner classifies a file onto its statement-at-a-time autocommit path on that word ANYWHERE in
    # the file, comments included.
    assert "CONCURRENTLY" not in sql
    from db.sql_migrations import needs_autocommit

    assert needs_autocommit(sql) is False
    assert needs_autocommit(_sql(UP_261)) is True


def test_the_backfill_script_is_dry_run_by_default_bounded_and_never_restates_the_fold() -> None:
    src = (REPO / "scripts" / "backfill_catalog_products_name_norm.py").read_text(encoding="utf-8")
    assert '"--apply"' in src and 'action="store_true"' in src  # dry-run unless asked
    for flag in ('"--batch-size"', '"--max-batches"', '"--sleep-ms"'):
        assert flag in src, flag
    # The script touches rows so the TRIGGER stamps them; the fold rule has exactly one owner, so
    # none of the fold's SQL appears here (naming the function in prose is fine; restating it is not).
    for fragment in ("regexp_replace", "translate(", "[^[:alnum:]]", IDENTITY_ACCENTED, "SET name_norm", "SET own_name_norm"):
        assert fragment not in src, fragment
    assert "SET title = title" in src
    assert "name_norm IS NULL OR own_name_norm IS NULL" in src
    # It refuses to run when the trigger is absent (otherwise `SET title = title` would stamp nothing).
    assert "trg_catalog_products_stamp_name_norm" in src


def test_the_index_is_its_own_concurrent_file_on_the_carrier_column() -> None:
    sql = _sql(UP_261)
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_catalog_products_name_norm_trgm" in sql
    assert "ON catalog_products USING GIN (name_norm gin_trgm_ops)" in sql
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm;" in sql
    # Index-only files are exempt from the schema-guard coverage gate, and say so.
    assert "schema-guard-exempt" in sql
    # Nothing else in a CONCURRENTLY file: the runner sends it statement-at-a-time on autocommit.
    assert "ALTER TABLE" not in sql and "UPDATE " not in sql


def test_down_files_keep_the_columns_and_drop_only_what_the_up_created() -> None:
    down = _sql(DOWN_260)
    assert "DROP TRIGGER IF EXISTS trg_catalog_products_stamp_name_norm ON catalog_products;" in down
    assert "DROP FUNCTION IF EXISTS catalog_products_stamp_name_norm();" in down
    assert "DROP FUNCTION IF EXISTS catalog_products_identity_fold(TEXT);" in down
    assert "DROP COLUMN" not in down
    assert "DROP INDEX CONCURRENTLY IF EXISTS idx_catalog_products_name_norm_trgm;" in _sql(DOWN_261)


def test_the_model_and_the_schema_guard_name_both_columns() -> None:
    from db import catalog as catalog_models

    columns = {c.name: c for c in catalog_models.catalog_products.columns}
    for name in ("name_norm", "own_name_norm"):
        assert name in columns, name
        assert columns[name].nullable is True
        assert columns[name].type.python_type is str

    guard = (REPO / "db" / "schema_guard.py").read_text(encoding="utf-8")
    heal = re.search(
        r"ALTER TABLE IF EXISTS catalog_products\s+ADD COLUMN IF NOT EXISTS name_norm TEXT,\s+"
        r"ADD COLUMN IF NOT EXISTS own_name_norm TEXT;",
        guard,
    )
    assert heal, "schema_guard must self-heal both columns (the coverage gate reads this source)"
    from db.schema_guard import REQUIRED_SCHEMA

    required = next(t for t in REQUIRED_SCHEMA if t.table == "catalog_products").columns
    assert {"name_norm", "own_name_norm"} <= set(required)
