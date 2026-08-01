"""A boolean server_default must survive the round-trip, not just the DDL.

`Column("indexable", Boolean, server_default="true")` looks correct and reviews
correct. SQLAlchemy renders a STRING server_default as a quoted literal, so it
emitted `indexable BOOLEAN DEFAULT 'true' NOT NULL`, and SQLite — which has no
native boolean — stored the four-character string `'true'` for any INSERT that
omitted the column. `COALESCE(m.indexable, TRUE) IS TRUE`, the gate every
cross-merchant recall lane runs, is FALSE for that value, so the merchant drops
out of search while the column reads as set.

Nothing caught it because every seed in the suite passes `indexable`
explicitly. A test that asserts the DDL text would not have caught it either —
the DDL was valid. Only inserting a row and re-reading it through the real gate
expression does.
"""

from __future__ import annotations

import db.catalog as catalog_models
import pytest
from sqlalchemy import Boolean, Table

from db.catalog import catalog_merchants, catalog_offers
from db.database import database
from tests.model_schema import ensure_model_tables


_PREFIX = "booldef"


@pytest.fixture
async def _db():
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_model_tables((catalog_merchants, catalog_offers))
    await database.execute(
        "DELETE FROM catalog_merchants WHERE merchant_id LIKE :p", {"p": f"{_PREFIX}%"}
    )
    try:
        yield
    finally:
        await database.execute(
            "DELETE FROM catalog_merchants WHERE merchant_id LIKE :p", {"p": f"{_PREFIX}%"}
        )
        if not was_connected:
            await database.disconnect()


async def test_omitted_indexable_defaults_to_a_value_the_recall_gate_accepts(_db):
    """The end-to-end shape: INSERT without the column, read via the real gate."""
    await database.execute(
        "INSERT INTO catalog_merchants (merchant_id, merchant_name) VALUES (:m, :m)",
        {"m": f"{_PREFIX}_default"},
    )
    row = await database.fetch_one(
        "SELECT indexable, COALESCE(indexable, TRUE) IS TRUE AS passes_gate "
        "FROM catalog_merchants WHERE merchant_id = :m",
        {"m": f"{_PREFIX}_default"},
    )
    assert row is not None
    assert bool(dict(row)["passes_gate"]), (
        "a merchant inserted without an explicit `indexable` must still pass "
        f"`COALESCE(m.indexable, TRUE) IS TRUE` — stored {dict(row)['indexable']!r}. "
        "A string server_default renders quoted and stores the STRING 'true', "
        "which fails this gate and silently removes the merchant from recall."
    )


def test_no_boolean_column_in_db_catalog_uses_a_string_server_default():
    """Guards every OTHER boolean in the module, including ones added later."""
    offenders = [
        f"{table.name}.{column.name} = {column.server_default.arg!r}"
        for table in vars(catalog_models).values()
        if isinstance(table, Table)
        for column in table.columns
        if isinstance(column.type, Boolean)
        and column.server_default is not None
        and isinstance(getattr(column.server_default, "arg", None), str)
    ]
    assert not offenders, (
        "use expression.true() / expression.false(), not the strings 'true' / "
        "'false': a string server_default renders QUOTED, so SQLite stores the "
        f"literal text and every `IS TRUE` check on it fails. Offenders: {offenders}"
    )
