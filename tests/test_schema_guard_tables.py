"""The contract of `tests/schema_guard_tables.schema_guard_tables`: it leaves the schema as it found it.

Written against whatever this process's test DB already holds, never against a fresh one, so it
holds in a sweep shard where an earlier file left tables behind as well as alone.
"""

from __future__ import annotations

import pytest

from tests.schema_guard_tables import _schema_objects, schema_guard_tables


def _engine():
    from sqlalchemy import create_engine

    from db.database import sync_url

    return create_engine(sync_url)


@pytest.fixture(autouse=True)
def _sqlite_only():
    from db.database import IS_SQLITE

    if not IS_SQLITE:
        pytest.skip("the helper builds nothing on Postgres")


async def test_the_schema_after_the_block_is_the_schema_before_it():
    from db.database import database

    engine = _engine()
    try:
        before = _schema_objects(engine)
        was_connected = bool(database.is_connected)
        async with schema_guard_tables(required=["reap_agentic_purchases", "conversion_click_claims"]):
            inside = _schema_objects(engine)
            assert ("table", "reap_agentic_purchases") in inside
            assert ("table", "conversion_click_claims") in inside
        assert _schema_objects(engine) == before
        assert bool(database.is_connected) == was_connected
    finally:
        engine.dispose()


async def test_a_table_the_block_found_keeps_its_rows_and_stays():
    from sqlalchemy import text

    from db.database import database

    engine = _engine()
    try:
        async with schema_guard_tables(required=["conversion_click_claims"]):
            outer = _schema_objects(engine)
            # One row that lives only inside this test: removed below even if an assert fails,
            # so a table an earlier file built is left exactly as it was.
            await database.execute(
                "INSERT INTO conversion_click_claims (click_id, claimed_by, external_order_id) "
                "VALUES ('clk_helper_contract', 'merchant_order', 'ord_helper_contract')"
            )
            try:
                async with schema_guard_tables(required=["conversion_click_claims"]):
                    pass
                # The inner block found every table, so it built and dropped nothing.
                assert _schema_objects(engine) == outer
                with engine.connect() as conn:
                    count = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM conversion_click_claims "
                            "WHERE click_id = 'clk_helper_contract'"
                        )
                    ).scalar()
                assert count == 1
            finally:
                await database.execute(
                    "DELETE FROM conversion_click_claims WHERE click_id = 'clk_helper_contract'"
                )
    finally:
        engine.dispose()


async def test_a_required_table_the_heal_does_not_build_fails_the_block():
    engine = _engine()
    try:
        before = _schema_objects(engine)
        with pytest.raises(AssertionError, match="no_such_table_from_the_heal"):
            async with schema_guard_tables(required=["no_such_table_from_the_heal"]):
                pass
        # Even a block that failed on entry takes back what the heal built.
        assert _schema_objects(engine) == before
    finally:
        engine.dispose()
