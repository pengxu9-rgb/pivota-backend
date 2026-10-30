"""Storefront proofs for ENRICHMENT catalog skus: one row per (product_key, sku_key).

WHY THIS EXISTS (option 2, 2026-09-29). The Reap cart-link lane buys a Shopify variant through a
cart permalink. A `catalog_enrichment_agent_v1` row names its variant only in the catalog (the
sku's `source_variant_id`, and `sku_payload.source_handle` for folded MAC shades), and nothing in
the catalog says that variant is still live on the storefront. This table holds that fact, read
from the brand's own storefront by the proof job (PR B), and the purchase lane (PR C) checks it
with `services.reap_enrichment_cart_proof.verify_enrichment_cart_proof` before it builds a cart.
The writer's full contract is in that module's docstring ("THE PROOF CONTRACT").

WHO CREATES IT. PR B's writer calls `ensure_table()`. PR C's reader, `fetch_proof()`, calls it too,
and only from the cart-link route's enrichment branch, which runs only while
REAP_AGENTIC_CART_LINK_ENRICHMENT_ENABLED (and the cart-link dial) is on: with the flag off no
request path creates the table. An empty table reads as "no proof", which refuses the purchase.

Migrations do not self-apply in prod; `ensure_table()` runs the same CREATE at first use, exactly
like db/merchant_purchasability_cart_mint_scans.py. db/migrations/248_enrichment_cart_variant_proofs.sql
is the record, and what the SQL gates plan against. tests/test_enrichment_cart_variant_proofs_postgres.py
checks through the catalog that the two build the same table.

TWO DIALECT DIFFERENCES, AND ONLY TWO. The currency CHECK is a regex (`~ '^[A-Z]{3}$'`) on
Postgres, which SQLite cannot parse; the SQLite build (tests only) spells the same rule with
GLOB. And migration 249's `variant_title` column: Postgres runs the migration's own
`ADD COLUMN IF NOT EXISTS` after the CREATE (so a table created before 249 gains it), which
SQLite cannot parse; the SQLite build declares the column in its CREATE instead, in the same
position (last). Everything else is character-for-character the migrations, and a test pins that
the two builds differ in exactly those two places.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from db._ddl_guard import apply_ddl_statements, reset_ddl_state
from db.database import IS_POSTGRES
from db.schema_guard import guarded_statements

logger = logging.getLogger(__name__)

TABLE = "enrichment_cart_variant_proofs"

#: The one outcome a purchase may rely on. Every other outcome is a recorded refusal.
OUTCOME_OK = "ok"
#: What the proof job may record as `source`: the store-wide `/products.json` page, or one handle's
#: `/products/<handle>.js`. The table's CHECK and the verifier both hold exactly this set.
PROOF_SOURCES = ("products_json_v1", "products_js_v1")

_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

#: The currency rule, per dialect. Postgres: the migration's own regex.
POSTGRES_CURRENCY_CHECK = "currency ~ '^[A-Z]{3}$'"
#: SQLite has no regex operator; GLOB is case-sensitive and anchored to the whole value.
SQLITE_CURRENCY_CHECK = "currency GLOB '[A-Z][A-Z][A-Z]'"

# Mirrors db/migrations/248_enrichment_cart_variant_proofs.sql, character for character inside the
# statement (tests/test_enrichment_cart_variant_proofs.py compares them).
_CREATE_POSTGRES = """
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
      currency            TEXT CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
      source              TEXT NOT NULL,
      checked_at          TIMESTAMPTZ NOT NULL,
      outcome             TEXT NOT NULL,
      created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      CONSTRAINT ck_enrichment_cart_variant_proofs_source CHECK (
        source IN ('products_json_v1', 'products_js_v1')
      ),
      CONSTRAINT ck_enrichment_cart_variant_proofs_ok_has_evidence CHECK (
        outcome <> 'ok' OR (
          variant_id IS NOT NULL AND available IS NOT NULL AND currency IS NOT NULL
          AND live_variant_count IS NOT NULL AND live_variant_count >= 1
          AND live_price_minor IS NOT NULL AND live_price_minor > 0
        )
      ),
      PRIMARY KEY (product_key, sku_key)
    )
    """
#: Migration 249, character for character inside the statement: the live variant's title, for
#: display (the job writes it cleaned by services.shopify_variant_identity.clean_variant_title).
_ADD_VARIANT_TITLE_POSTGRES = "ALTER TABLE enrichment_cart_variant_proofs ADD COLUMN IF NOT EXISTS variant_title TEXT;"
#: Where the SQLite build declares migration 249's column instead: after the last column, as the ALTER
#: appends it on Postgres.
_LAST_COLUMN = "      updated_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
_SQLITE_VARIANT_TITLE_COLUMN = "      variant_title       TEXT,\n"
_CREATE_SQLITE = (
    _CREATE_POSTGRES.replace(POSTGRES_CURRENCY_CHECK, SQLITE_CURRENCY_CHECK)
    .replace(_LAST_COLUMN, _LAST_COLUMN + _SQLITE_VARIANT_TITLE_COLUMN)
)

if _CREATE_SQLITE.count("variant_title") != 1:  # the anchor above moved: fail at import, not in a test DB
    raise RuntimeError("the SQLite build lost migration 249's variant_title column")

_DDL_STATEMENTS = guarded_statements(
    [_CREATE_POSTGRES, _ADD_VARIANT_TITLE_POSTGRES] if IS_POSTGRES else [_CREATE_SQLITE]
)


_DDL_LABEL = "ensure_enrichment_cart_variant_proofs"

#: READ-ONLY CONTEXT (selected prepare, 2026-10-04). `POST /purchases/prepare` reads the proof inside
#: `SET TRANSACTION ... READ ONLY`, and PostgreSQL refuses `CREATE TABLE IF NOT EXISTS` there EVEN
#: WHEN THE TABLE EXISTS (PG 15: "cannot execute CREATE TABLE in a read-only transaction"). Run
#: there, the DDL failed, armed both cooldowns (60s reader, 300s `_ddl_guard`), and a cold instance
#: refused every enrichment prepare -- and, for minutes after, every create. So, before any DDL,
#: one catalog read: the table, migration 249's column, and whether this transaction is read-only.
#: Both present: ready, no DDL. Read-only and incomplete: NO DDL is attempted and nothing is armed.
_PROBE_POSTGRES = f"""
    SELECT to_regclass('{TABLE}') IS NOT NULL AS table_exists,
           EXISTS (SELECT 1 FROM pg_attribute
                    WHERE attrelid = to_regclass('{TABLE}') AND attname = 'variant_title'
                      AND attnum > 0 AND NOT attisdropped) AS complete,
           current_setting('transaction_read_only') = 'on' AS read_only
"""

#: `_ensure()` outcomes. READABLE / ABSENT happen only inside a read-only transaction (Postgres).
_READY, _READABLE, _ABSENT, _FAILED = "ready", "readable", "absent", "failed"


async def _ensure() -> str:
    """READY once the table and its 249 column are known to exist (memoized). Inside a read-only
    transaction, an incomplete table is READABLE (the table exists, the SELECT can run) or ABSENT,
    and no DDL runs. FAILED: the DDL pass did not complete."""
    global _DDL_READY
    if _DDL_READY:
        return _READY
    async with _DDL_LOCK:
        if _DDL_READY:
            return _READY
        from db.database import database

        if IS_POSTGRES:
            probe = await database.fetch_one(_PROBE_POSTGRES)
            if probe["complete"]:
                _DDL_READY = True
                return _READY
            if probe["read_only"]:
                return _READABLE if probe["table_exists"] else _ABSENT
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label=_DDL_LABEL,
            logger=logger,
            execute=database.execute,
        )
    return _READY if _DDL_READY else _FAILED


async def ensure_table() -> bool:
    """Create the table if it is missing. True once it is known to exist. Memoizes only after
    the statement succeeded (or the read-only probe found it complete), so a failure retries on a
    later call. Inside a read-only transaction it never runs DDL: an incomplete table is False."""
    return await _ensure() == _READY


#: The ONE read of a proof (PR C): exactly the row for this (product_key, sku_key), every column
#: the verifier reads, nothing inferred. The primary key makes it at most one row.
_SELECT_PROOF_SQL = """
    SELECT product_key, sku_key, shop_host, handle, shopify_product_id, variant_id,
           live_variant_count, available, live_price_minor, currency, source, checked_at, outcome
      FROM enrichment_cart_variant_proofs
     WHERE product_key = :product_key AND sku_key = :sku_key
"""


#: After `ensure_table()` FAILS on the request path, how long the reader answers "no proof"
#: without trying the DDL again (review of #2465, F3). Without it, a role that cannot CREATE would
#: re-run a failing CREATE, under `_DDL_LOCK`, on every flag-on purchase request.
FETCH_DDL_RETRY_SECONDS = 60.0
#: `time.monotonic()` of the reader's last failed `ensure_table()`, or None.
_FETCH_DDL_FAILED_AT: Optional[float] = None


async def fetch_proof(product_key: str, sku_key: str) -> Optional[Dict[str, Any]]:
    """The proof row for exactly `(product_key, sku_key)` as a dict, or None.

    None when there is no row, AND when the table cannot be created (`ensure_table()` False): a
    proof nobody can read is a missing proof, which `verify_enrichment_cart_proof` refuses
    (`proof_missing`). A failed `ensure_table()` is REMEMBERED for `FETCH_DDL_RETRY_SECONDS`: in
    that window this answers None without touching the DDL or its lock, then tries once more --
    except that on Postgres the read-only probe still runs, so a table someone else completed in
    the meantime (the writer job, prepare's warm-up) is read at once.
    Inside a read-only transaction no DDL runs (`_PROBE_POSTGRES`): an existing table is read, a
    missing one is None, and neither arms the window. A database error on the probe or the SELECT
    itself propagates, as every other read on the purchase path does.
    Returned as a plain dict because the verifier takes a Mapping and a `databases` Record is not
    one.
    """
    global _DDL_READY, _FETCH_DDL_FAILED_AT
    from db.database import database

    if (_FETCH_DDL_FAILED_AT is not None and not _DDL_READY
            and time.monotonic() - _FETCH_DDL_FAILED_AT < FETCH_DDL_RETRY_SECONDS):
        # In the window: no DDL and no lock. Only a COMPLETE table (no DDL owed) ends it early.
        if not IS_POSTGRES or not (await database.fetch_one(_PROBE_POSTGRES))["complete"]:
            return None
        _DDL_READY = True
    state = await _ensure()
    if state == _FAILED:
        _FETCH_DDL_FAILED_AT = time.monotonic()
        return None
    if state == _ABSENT:  # read-only and no table: no proof, and no DDL failure to remember
        return None
    _FETCH_DDL_FAILED_AT = None
    row = await database.fetch_one(
        _SELECT_PROOF_SQL, {"product_key": product_key, "sku_key": sku_key}
    )
    return dict(row) if row is not None else None


def _reset_for_tests() -> None:
    global _DDL_READY, _FETCH_DDL_FAILED_AT
    _DDL_READY = False
    _FETCH_DDL_FAILED_AT = None
    reset_ddl_state(_DDL_LABEL)


__all__: List[str] = ["TABLE", "OUTCOME_OK", "PROOF_SOURCES", "ensure_table", "fetch_proof"]
