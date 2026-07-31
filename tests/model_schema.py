"""Test schema DERIVED from the `db/` SQLAlchemy models, never hand-written.

Six test modules hand-write `CREATE TABLE` DDL for the catalog core. A
hand-written fixture is wrong in both directions, and both have already cost us
a red CI:

  LAXER than production -> it passes in isolation, because the fixture's own DDL
  wins the create race, and dies in CI's full-suite `sweep` when the real DDL
  wins instead. Three defects shipped exactly this way (#1653/#1655):
  `merchant_stores.name NOT NULL`, `catalog_skus.merchant_id NOT NULL`, and a
  test asserting `catalog_merchants.indexable IS NULL` — a state
  `db/catalog.py:28` (`nullable=False`) forbids.

  NARROWER than production -> whichever module wins the race POISONS its
  neighbours. `tests/test_recall_offer_seller_and_sku_gates.py`'s 26-column
  `catalog_products` omitted `pivota_signature_id`, so
  `pytest tests/test_recall_offer_seller_and_sku_gates.py
  tests/test_index_pipeline_state_service.py` produced 27 failures with
  `no column named pivota_signature_id`. Alphabetical collection hid that from
  CI; `--lf` or any `-k` subset does not.

Deriving from the model makes both impossible: the fixture IS the model, so it
can be neither laxer nor richer than what production runs.

This lives in its own module rather than in `tests/conftest.py` because pytest
imports conftest as top-level `conftest`, so a test module doing
`from tests.conftest import ...` would execute that file a second time.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence


def _sqlite_default_literal(column: Any) -> Optional[str]:
    """The `DEFAULT` literal for `column` in a SQLite `ADD COLUMN`, or None.

    Two SQLite restrictions shape this, and each surfaces as a bare syntax
    error rather than anything actionable:

    1. A string `server_default` must be QUOTED. `db/catalog.py` writes
       `server_default="primary"`, which renders as `DEFAULT primary` and fails
       with `near "primary"`. Booleans and numbers must NOT be quoted — a
       quoted `'true'` in a BOOLEAN column stores the four-character string,
       and `COALESCE(bm.indexable, TRUE) IS TRUE` is then FALSE for it.
    2. A non-constant default is rejected outright: `server_default=func.now()`
       fails with `near "("`. Those columns get no default at all — and, in
       `_add_column_sql` below, no NOT NULL either.
    """
    server_default = getattr(column, "server_default", None)
    if server_default is None:
        return None
    arg = getattr(server_default, "arg", None)
    if not isinstance(arg, str):
        # func.now() and friends — restriction 2.
        return None
    literal = arg.strip()
    if literal.lower() in ("true", "false", "null"):
        return literal.lower()
    if re.fullmatch(r"-?\d+(\.\d+)?", literal):
        return literal
    return "'" + literal.replace("'", "''") + "'"


def _add_column_sql(table_name: str, column: Any) -> str:
    from sqlalchemy.dialects import sqlite as _sqlite

    parts = [column.name, column.type.compile(_sqlite.dialect())]
    default = _sqlite_default_literal(column)
    if default is not None:
        parts.append(f"DEFAULT {default}")
        # SQLite accepts NOT NULL on ADD COLUMN only when a default backfills
        # the rows already in the table. Without one the column goes in
        # nullable. That is the one place this helper can be laxer than the
        # model, and it only applies to a table some OTHER module created — the
        # `create_all` path below carries every NOT NULL verbatim.
        if not column.nullable:
            parts.append("NOT NULL")
    return f"ALTER TABLE {table_name} ADD COLUMN {' '.join(parts)}"


async def ensure_model_tables(tables: Sequence[Any]) -> None:
    """Create/patch `tables` in the test DB from their `db/` model definitions.

    Two steps, because neither alone is sufficient — verified on the recall-gate
    module, where each step alone fixes one collection order and breaks the
    other:

    * `create_all(checkfirst=True)` builds any table nobody has created yet,
      with production's exact DDL — every NOT NULL, every default.
    * `checkfirst=True` SKIPS a table another module already created, and those
      hand-written DDLs are narrower, so the columns a lane SELECTs are still
      absent. Each existing table is therefore patched up to the model column by
      column.

    The patch list is DERIVED from `table.columns`, never hardcoded. A hardcoded
    `ALTER TABLE ADD COLUMN` list is a mask: it guarantees every column the lane
    SELECTs exists regardless of the real schema, so a migration dropping e.g.
    `catalog_offers.why_buy_direct` would 500 in prod on `o.why_buy_direct`
    while the test quietly ALTERs it back and stays green.

    `db.*` is imported lazily: `tests/conftest.py` pins DATABASE_URL at import
    time and `db.database` binds its singleton to whatever is set when it is
    first imported.
    """
    from sqlalchemy import create_engine

    from db.database import IS_SQLITE, database, metadata, sync_url

    engine = create_engine(sync_url)
    try:
        metadata.create_all(engine, tables=list(tables), checkfirst=True)
    finally:
        engine.dispose()

    if not IS_SQLITE:
        # The patch step is SQLite-only (PRAGMA table_info, plus the ADD COLUMN
        # restrictions above). A postgres test DB is a real migrated schema and
        # has nothing to reconcile.
        return

    for table in tables:
        rows = await database.fetch_all(f"PRAGMA table_info({table.name})")
        present = {str(dict(row)["name"]) for row in rows}
        for column in table.columns:
            if column.name not in present:
                await database.execute(_add_column_sql(table.name, column))
