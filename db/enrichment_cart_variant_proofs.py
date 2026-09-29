"""Storefront proofs for ENRICHMENT catalog skus: one row per (product_key, sku_key).

WHY THIS EXISTS (option 2, 2026-09-29). The Reap cart-link lane buys a Shopify variant through a
cart permalink. A `catalog_enrichment_agent_v1` row names its variant only in the catalog (the
sku's `source_variant_id`, and `sku_payload.source_handle` for folded MAC shades), and nothing in
the catalog says that variant is still live on the storefront. This table holds that fact, read
from the brand's own storefront by the proof job (PR B), and the purchase lane (PR C) checks it
with `services.reap_enrichment_cart_proof.verify_enrichment_cart_proof` before it builds a cart.

INERT IN PR A. This module only declares the table. Nothing imports it yet: `ensure_table()` has
no caller, so no environment creates the table until PR B's writer calls it.

Migrations do not self-apply in prod; `ensure_table()` runs the same CREATE at first use, exactly
like db/merchant_purchasability_cart_mint_scans.py. db/migrations/248_enrichment_cart_variant_proofs.sql
is the record, and what the SQL gates plan against. tests/test_enrichment_cart_variant_proofs_postgres.py
checks through the catalog that the two build the same table.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from db._ddl_guard import apply_ddl_statements
from db.schema_guard import guarded_statements

logger = logging.getLogger(__name__)

TABLE = "enrichment_cart_variant_proofs"

#: The one outcome a purchase may rely on. Every other outcome is a recorded refusal.
OUTCOME_OK = "ok"

_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

# Mirrors db/migrations/248_enrichment_cart_variant_proofs.sql, character for character inside the
# statement (tests/test_enrichment_cart_variant_proofs.py compares them).
_DDL_STATEMENTS = guarded_statements([
    """
    CREATE TABLE IF NOT EXISTS enrichment_cart_variant_proofs (
      product_key         TEXT NOT NULL,
      sku_key             TEXT NOT NULL,
      shop_host           TEXT NOT NULL,
      handle              TEXT NOT NULL,
      shopify_product_id  TEXT,
      variant_id          TEXT,
      live_variant_count  INTEGER CHECK (live_variant_count IS NULL OR live_variant_count >= 0),
      available           BOOLEAN,
      live_price_minor    BIGINT CHECK (live_price_minor IS NULL OR live_price_minor >= 0),
      currency            TEXT CHECK (currency IS NULL OR (length(currency) = 3 AND currency = upper(currency))),
      source              TEXT NOT NULL,
      checked_at          TIMESTAMPTZ NOT NULL,
      outcome             TEXT NOT NULL,
      created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      CONSTRAINT ck_enrichment_cart_variant_proofs_ok_has_evidence CHECK (
        outcome <> 'ok' OR (
          variant_id IS NOT NULL AND live_variant_count IS NOT NULL AND available IS NOT NULL
          AND live_price_minor IS NOT NULL AND currency IS NOT NULL
        )
      ),
      PRIMARY KEY (product_key, sku_key)
    )
    """,
])


async def ensure_table() -> bool:
    """Create the table if it is missing. True once it is known to exist. Memoizes only after
    the statement succeeded, so a failure retries on a later call."""
    global _DDL_READY
    if _DDL_READY:
        return True
    async with _DDL_LOCK:
        if _DDL_READY:
            return True
        from db.database import database

        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_enrichment_cart_variant_proofs",
            logger=logger,
            execute=database.execute,
        )
    return _DDL_READY


def _reset_for_tests() -> None:
    global _DDL_READY
    _DDL_READY = False


__all__: List[str] = ["TABLE", "OUTCOME_OK", "ensure_table"]
