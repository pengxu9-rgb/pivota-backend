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

Deriving from the model closes both: the COLUMN SET is the model's, so nothing
can be missing and nothing can be invented.

It is not a full guarantee of nullability, and the limit is worth stating
precisely because the create_all path and the patch path differ:

  * create_all path (this module created the table) — exact production DDL:
    every NOT NULL, every default, every index.
  * patch path (someone else created it first) — MISSING COLUMNS ONLY. SQLite
    cannot express `ADD COLUMN ... NOT NULL` without a constant default, so a
    NOT NULL column that has no server_default (catalog_products.title,
    catalog_skus.merchant_id — 30 such across the tables used here) or a
    func.now() one (created_at, updated_at — 11 more) is added NULLABLE. Nor
    are indexes, UNIQUE constraints, CHECKs or the PRIMARY KEY built: a table
    missing its PK column gets an ordinary nullable non-PK column, so an
    `ON CONFLICT (product_key)` upsert against it fails outright. And a column
    the neighbour ALREADY created is never reconciled — wrong type, wrong
    nullability and wrong default all survive verbatim, so the resulting
    fixture can still be a superset of the model.

So a fixture patched onto a neighbour's table is still laxer than production on
nullability. That is strictly better than the hand-written DDL it replaces —
which was laxer on nullability AND missing columns outright — but it is not
"the fixture IS the model", and a test that depends on a NOT NULL firing must
not rely on this helper for it.

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

    1. A string `server_default` must be QUOTED for a TEXT column.
       `db/catalog.py` writes `server_default="primary"`, which renders as
       `DEFAULT primary` and fails with `near "primary"`.
    2. A non-constant default is rejected outright: `server_default=func.now()`
       fails with `near "("`. Those columns get no default at all — and, in
       `_add_column_sql` below, no NOT NULL either.

    Quoting keys off the COLUMN'S TYPE, never off the literal's shape. Keying
    off the shape is what produced the bug this helper's own PR fixed in
    `db/catalog.py`: `server_default="true"` on a Boolean must render as the
    unquoted constant, but `server_default="true"` on a String is the
    four-character word and must stay quoted. The same reasoning covers
    `"007"` (a String default that must NOT become `7`) and `"null"` (a String
    default that must NOT become SQL NULL) — both of which a shape-based rule
    silently corrupts, and neither of which `create_all` gets wrong.

    `expression.true()` / `expression.false()` are SQL elements rather than
    strings but ARE constants, so they are compiled through the dialect (to
    `1` / `0` on SQLite) instead of being discarded under restriction 2.
    """
    from sqlalchemy import Boolean, Integer, Numeric
    from sqlalchemy.dialects import sqlite as _sqlite
    from sqlalchemy.sql import expression
    from sqlalchemy.sql.elements import TextClause

    server_default = getattr(column, "server_default", None)
    if server_default is None:
        return None
    arg = getattr(server_default, "arg", None)
    if isinstance(arg, (expression.True_, expression.False_)):
        return str(arg.compile(dialect=_sqlite.dialect()))
    if isinstance(arg, TextClause):
        # `server_default=text("0")` is the canonical SQLAlchemy idiom and IS a
        # constant — rejecting it by Python type would drop the default (and,
        # for a NOT NULL column, the NOT NULL with it) where create_all emits
        # `DEFAULT 0`.
        #
        # It is raw SQL, so it passes through VERBATIM — never re-quoted. Running
        # `text("'z'")` through the string rules below would emit `DEFAULT '''z'''`
        # where create_all emits `DEFAULT 'z'`. Anything that is not plainly a
        # constant (`text("now()")`) is still dropped under restriction 2, since
        # SQLite rejects it in ADD COLUMN.
        raw = arg.text.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?|'(?:[^']|'')*'|TRUE|FALSE|NULL", raw, re.I):
            return raw
        return None
    if not isinstance(arg, str):
        # func.now() and friends — restriction 2.
        return None
    # `.strip()` is for COMPARISON ONLY. Quoting must preserve the value
    # verbatim: create_all stores `server_default=" x "` as `' x '`, and
    # silently trimming it here would reintroduce the same corruption class as
    # `"007"` -> `7`.
    literal = arg.strip()
    # `_type_affinity` rather than `isinstance(column.type, ...)` so a
    # TypeDecorator / with_variant wrapper resolves to what it actually stores.
    # db/ already has 43 Variant-typed columns; a Boolean one carrying a string
    # default would otherwise render `DEFAULT 'true'` — the exact bug this
    # module exists to prevent.
    affinity = getattr(column.type, "_type_affinity", type(column.type))
    if affinity is not None and issubclass(affinity, Boolean):
        # Only a Boolean column may render the bare keywords. SQLite stores
        # TRUE/FALSE as 1/0; a quoted 'true' would store the word and fail
        # every `IS TRUE` check downstream.
        if literal.lower() in ("true", "false"):
            return "1" if literal.lower() == "true" else "0"
    if (
        affinity is not None
        and issubclass(affinity, (Integer, Numeric))
        and re.fullmatch(r"-?\d+(\.\d+)?", literal)
    ):
        return literal
    return "'" + arg.replace("'", "''") + "'"


def _quote_ident(name: str) -> str:
    """SQLite identifier quoting. `db/` already has four tables with a column
    named `query`, and `order` / `group` / `index` / `default` / `values` are
    hard syntax errors unquoted — a one-character-per-side cost to make this
    helper safe for any model it is later pointed at."""
    return '"' + name.replace('"', '""') + '"'


def _add_column_sql(table_name: str, column: Any) -> str:
    from sqlalchemy.dialects import sqlite as _sqlite

    parts = [_quote_ident(column.name), column.type.compile(_sqlite.dialect())]
    default = _sqlite_default_literal(column)
    if default is not None:
        parts.append(f"DEFAULT {default}")
        # SQLite accepts NOT NULL on ADD COLUMN only when a default backfills
        # the rows already in the table. Without one the column goes in
        # nullable — see the nullability limit spelled out in the module
        # docstring. The `create_all` path carries every NOT NULL verbatim.
        if not column.nullable:
            parts.append("NOT NULL")
    return f"ALTER TABLE {_quote_ident(table_name)} ADD COLUMN {' '.join(parts)}"


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

    A STRING `server_default` on a Boolean is a trap in the `create_all` half:
    SQLAlchemy renders it quoted, so `server_default="true"` becomes
    `DEFAULT 'true'` and SQLite — no native boolean — stores the four-character
    string for any INSERT that omits the column, making
    `COALESCE(indexable, TRUE) IS TRUE` evaluate to 0. `db/catalog.py` now uses
    `expression.true()` / `expression.false()`, which render as the dialect's
    own constant. Eleven boolean columns in OTHER `db/` modules still carry a
    string default; none is created by this helper today, but check before
    adding one to the `tables` list.
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
        rows = await database.fetch_all(f"PRAGMA table_info({_quote_ident(table.name)})")
        present = {str(dict(row)["name"]) for row in rows}
        for column in table.columns:
            if column.name not in present:
                await database.execute(_add_column_sql(table.name, column))
