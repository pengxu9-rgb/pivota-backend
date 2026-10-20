"""The external_product_seeds columns the seed-currency offer rule reads, for the shared gate database.

services/seed_offer_currency's predicate correlates every offer read on the purchase path with the
seeds attached to its listing, so a Postgres test that drives offers.resolve's catalog-offers arm
needs the table even when it plants no seed. The dialect gate runs every file in ONE database, and
other files create `external_product_seeds (id text)` with IF NOT EXISTS, so the table can exist
without these columns. Added idempotently; nothing is dropped or narrowed.
"""

from __future__ import annotations

SEED_CURRENCY_COLUMNS = (
    ("attached_product_key", "text"),
    ("status", "text"),
    ("price_currency", "text"),
    ("seed_data", "jsonb"),
    ("title", "text"),
    ("updated_at", "timestamptz"),
    # Not read by the rule; migration 044 declares both NOT NULL, so a row planted in its table must
    # carry them, and a narrower table needs them to take the same INSERT.
    ("market", "text"),
    ("destination_url", "text"),
)


def ensure_seed_currency_columns(conn) -> None:
    """`conn`: a SQLAlchemy connection in a transaction (engine.begin())."""
    from sqlalchemy import text

    conn.execute(text("CREATE TABLE IF NOT EXISTS external_product_seeds (id text PRIMARY KEY)"))
    for column, column_type in SEED_CURRENCY_COLUMNS:
        conn.execute(text(f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {column} {column_type}"))
