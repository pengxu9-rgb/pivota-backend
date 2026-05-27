"""Postgres integration coverage for catalog_products.content_changed_at.

Opt in with:

  CATALOG_CONTENT_CHANGED_AT_DB_URL=postgresql://...
  CATALOG_CONTENT_CHANGED_AT_DROP_SCHEMA_OK=true

or reuse the migration harness variables:

  MIGRATION_HARNESS_DB_URL=postgresql://...
  MIGRATION_HARNESS_DROP_SCHEMA_OK=true
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest


_DB_URL = os.getenv("CATALOG_CONTENT_CHANGED_AT_DB_URL") or os.getenv(
    "MIGRATION_HARNESS_DB_URL"
)
_DROP_OK = (
    os.getenv("CATALOG_CONTENT_CHANGED_AT_DROP_SCHEMA_OK", "").lower() == "true"
    or os.getenv("MIGRATION_HARNESS_DROP_SCHEMA_OK", "").lower() == "true"
)

pytestmark = pytest.mark.skipif(
    not (_DB_URL and _DROP_OK),
    reason=(
        "Set CATALOG_CONTENT_CHANGED_AT_DB_URL + "
        "CATALOG_CONTENT_CHANGED_AT_DROP_SCHEMA_OK=true to run"
    ),
)


def test_catalog_products_content_changed_at_trigger() -> None:
    psycopg2 = pytest.importorskip("psycopg2")
    migration_sql = (
        Path(__file__).resolve().parents[1]
        / "db"
        / "migrations"
        / "138_catalog_products_content_changed_at.sql"
    ).read_text()

    schema = f"content_changed_at_{uuid4().hex}"
    conn = psycopg2.connect(_DB_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}", public')
            cur.execute(
                """
                CREATE TABLE catalog_products (
                  product_key TEXT PRIMARY KEY,
                  merchant_id TEXT NOT NULL,
                  platform TEXT NOT NULL,
                  source_product_id TEXT NOT NULL,
                  content_key TEXT,
                  title TEXT NOT NULL,
                  description TEXT,
                  brand TEXT,
                  image_url TEXT,
                  product_payload JSONB,
                  category TEXT,
                  category_path TEXT,
                  product_type TEXT,
                  price_tier TEXT,
                  material TEXT,
                  care TEXT,
                  size_guide JSONB,
                  tags JSONB,
                  use_case_tags JSONB,
                  lifestyle_tags JSONB,
                  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                INSERT INTO catalog_products (
                  product_key, merchant_id, platform, source_product_id,
                  content_key, title, updated_at
                )
                VALUES (
                  'pk_1', 'merchant_1', 'shopify', 'sp_1',
                  'ck_old', 'Original title', TIMESTAMP '2026-01-01 00:00:00'
                )
                """
            )

            cur.execute(migration_sql)

            cur.execute(
                """
                SELECT content_changed_at, updated_at
                FROM catalog_products
                WHERE product_key = 'pk_1'
                """
            )
            backfilled_content_changed_at, updated_at = cur.fetchone()
            assert backfilled_content_changed_at == updated_at

            cur.execute(
                """
                UPDATE catalog_products
                SET content_key = 'ck_new'
                WHERE product_key = 'pk_1'
                """
            )
            cur.execute(
                """
                SELECT content_changed_at
                FROM catalog_products
                WHERE product_key = 'pk_1'
                """
            )
            after_content_key_update = cur.fetchone()[0]
            assert after_content_key_update == backfilled_content_changed_at

            cur.execute(
                """
                UPDATE catalog_products
                SET title = 'Changed public title'
                WHERE product_key = 'pk_1'
                """
            )
            cur.execute(
                """
                SELECT content_changed_at
                FROM catalog_products
                WHERE product_key = 'pk_1'
                """
            )
            after_title_update = cur.fetchone()[0]
            assert after_title_update > after_content_key_update
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.close()
