"""One-time fresh schema under guard; no create_all/startup healing or identity seed."""

import os
from pathlib import Path
from .guard import (
    BASE_COMMIT,
    FIXTURE_ID,
    MARKER,
    ORM_TABLES,
    TABLES,
    CATALOG_TABLES,
    WRITE_TABLES,
    TARGET,
    run_guard,
    validate_connection,
)

MIGRATIONS = (
    "044_buyer_identity_links.sql",
    "224_reap_agentic_ledger.sql",
    "225_reap_agentic_purchase_hints.sql",
    "226_reap_agentic_routes.sql",
    "227_reap_agentic_buyer_consent.sql",
    "228_tierb_cart_link_eligibility.sql",
    "229_reap_agentic_purchase_item_source.sql",
    "230_conversion_click_claims.sql",
    "231_merchant_purchasability.sql",
    "232_tierb_verdict_vocabulary.sql",
    "233_reap_agentic_purchase_consent.sql",
    "247_reap_agentic_purchase_offer_code.sql",
    "252_reap_agentic_enrollments_one_pending.sql",
    "044_external_product_seeds.sql",
)


async def migrate(target=TARGET):
    await run_guard("migrate", target=target)  # before sqlalchemy/db/model imports and DDL
    import asyncpg
    from sqlalchemy.schema import CreateTable, CreateIndex
    from sqlalchemy.dialects import postgresql
    from db.database import metadata, DATABASE_URL
    import db.agents, db.catalog, db.commerce_attribution, db.commerce_interactions, db.buyer_vault

    if DATABASE_URL != os.environ["DATABASE_URL"]:
        raise RuntimeError("migration_database_configuration_changed")
    conn = await asyncpg.connect(
        DATABASE_URL,
        timeout=5,
        command_timeout=10,
        server_settings={"lock_timeout": "1000", "statement_timeout": "8000"},
    )
    try:
        async with conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock(2026100201)"):
                raise RuntimeError("migration_lock_busy")
            await validate_connection(conn, "migrate", target)  # repeat target/empty check inside DDL transaction
            dialect = postgresql.dialect()
            for name in ORM_TABLES:
                if name in CATALOG_TABLES:
                    continue
                table = metadata.tables[name]
                await conn.execute(str(CreateTable(table).compile(dialect=dialect)))
                for index in sorted(table.indexes, key=lambda i: i.name):
                    await conn.execute(str(CreateIndex(index).compile(dialect=dialect)))
            await conn.execute(Path(__file__).with_name("catalog_schema.sql").read_text())
            for name in MIGRATIONS:
                await conn.execute((Path(__file__).resolve().parents[1] / "db/migrations" / name).read_text())
            await conn.execute(
                "CREATE TABLE api_keys(id SERIAL PRIMARY KEY,agent_id VARCHAR(255) NOT NULL,name VARCHAR(255) NOT NULL,key_hash VARCHAR(255) NOT NULL UNIQUE,key_prefix VARCHAR(20) NOT NULL,status VARCHAR(50) DEFAULT 'active',created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,last_used TIMESTAMP,usage_count INTEGER DEFAULT 0)"
            )
            await conn.execute(
                f"""CREATE TABLE {MARKER}(singleton INTEGER PRIMARY KEY CHECK(singleton=1),fixture_id TEXT NOT NULL,source_base_commit TEXT NOT NULL,stage TEXT NOT NULL CHECK(stage IN ('prepared','owned')),fixture_agent_id TEXT,fixture_owner_hash TEXT,fixture_buyer_id TEXT,fixture_buyer_ref TEXT)"""
            )
            await conn.execute(
                f"INSERT INTO {MARKER}(singleton,fixture_id,source_base_commit,stage) VALUES(1,$1,$2,'prepared')",
                FIXTURE_ID,
                BASE_COMMIT,
            )
            for name in (*TABLES, MARKER):
                await conn.execute("REVOKE ALL ON TABLE " + name + " FROM PUBLIC")
                await conn.execute("GRANT SELECT ON TABLE " + name + " TO " + target.runtime)
            write = WRITE_TABLES
            for name in sorted(write):
                await conn.execute("GRANT INSERT,UPDATE,DELETE ON TABLE " + name + " TO " + target.runtime)
            await conn.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO " + target.runtime)
        print('{"migration":"complete","identity_seeded":false,"enrollment_seeded":false}')
    finally:
        await conn.close()
