"""seed_serving.seed_row_market_sql on real Postgres: the same answers as seed_row_market for the same rows.

The rows are tests/test_seed_serving.SEED_ROWS, which the Python tests and the SQLite CASE test use too, so
this comparison is the parity check. The SQL runs over a VALUES list typed like external_product_seeds
(price_currency text, seed_data jsonb): no table is created, written or read, so the shared gate database is
left exactly as found.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from services import seed_serving as m
from tests.test_seed_serving import SEED_ROWS

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)")

# Extra stored shapes only the SQL sees as stored: seed_data that is not a JSON object, or a snapshot that is a
# JSON string. The Python reading of the same stored value (as the driver hands it back) must agree.
_JSON_NULL = object()
_STORED_ONLY = [
    ("seed_data_array", "US", "USD", ["USD"]),
    ("seed_data_json_null", "US", "SGD", _JSON_NULL),
    ("snapshot_json_null", "US", "SGD", {"snapshot": None}),
]


def _query(rows):
    values = ", ".join(
        f"(CAST(:id{i} AS text), CAST(:m{i} AS text), CAST(:c{i} AS text), CAST(:sd{i} AS jsonb))" for i in range(len(rows))
    )
    params = {}
    for i, (rid, partition, column, seed_data) in enumerate(rows):
        params.update({f"id{i}": rid, f"m{i}": partition, f"c{i}": column,
                       f"sd{i}": None if seed_data is None else json.dumps(None if seed_data is _JSON_NULL else seed_data)})
    sql = (
        f"SELECT eps.id, {m.seed_row_market_sql('eps')} AS seed_market, eps.seed_data AS seed_data"
        f" FROM (VALUES {values}) AS eps(id, market, price_currency, seed_data)"
    )
    return sql, params


def _run(rows):
    from databases import Database

    sql, params = _query(rows)

    async def go():
        db = Database(DATABASE_URL)
        await db.connect()
        try:
            return [dict(r._mapping) for r in await db.fetch_all(sql, params)]
        finally:
            await db.disconnect()

    return asyncio.run(go())


def test_seed_row_market_sql_matches_python_on_the_shared_rows():
    rows = [(r[0], r[1], r[2], r[3]) for r in SEED_ROWS]
    got = {r["id"]: r["seed_market"] for r in _run(rows)}
    want = {rid: market for rid, _p, _c, _sd, _cur, market in SEED_ROWS}
    assert got == want
    python = {rid: m.seed_row_market({"market": p, "price_currency": c, "seed_data": sd}) for rid, p, c, sd in rows}
    assert got == python


def test_the_partition_does_not_reach_the_sql_answer():
    rows = [("sgd_us", "US", "SGD", None), ("usd_sg", "SG", "USD", None), ("none_sg", "SG", None, None)]
    got = {r["id"]: r["seed_market"] for r in _run(rows)}
    assert got == {"sgd_us": "SG", "usd_sg": "US", "none_sg": None}


def test_stored_shapes_agree_with_python_as_the_driver_returns_them():
    result = _run(_STORED_ONLY)
    for r in result:
        rid = r["id"]
        partition, column = next((p, c) for i, p, c, _sd in _STORED_ONLY if i == rid)
        python = m.seed_row_market({"market": partition, "price_currency": column, "seed_data": r["seed_data"]})
        assert r["seed_market"] == python, rid
    got = {r["id"]: r["seed_market"] for r in result}
    # A non-object seed_data hides the page read; JSON null seed_data / snapshot do not.
    assert got == {"seed_data_array": None, "seed_data_json_null": "SG", "snapshot_json_null": "SG"}
