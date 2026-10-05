"""Persistence for the Reap rail's PRICE WITNESS (migration 258): the buy-intent preflight quote,
the merchant's live price when a quote disagrees with ours, and a corroborated lower price.

Owner decision 2026-10-05. Every statement here runs ONLY while a dial is armed
(`REAP_AGENTIC_PREFLIGHT_MODE` shadow|enforce, `REAP_AGENTIC_PRICE_CORROBORATION` on; see
services/reap_agentic_purchase.py). With both off no caller reaches this module, and the
ledger's own statements (`create_purchase`, `transition`) never name these columns -- so a
database whose mig-258 heal failed still opens and advances purchases exactly as before. That is
deliberately the OPPOSITE of mig 247's choice (name the columns in every write so a missing heal
is loud): these columns are dark-dial evidence, and a failed heal must not stop the live rail.

FENCED LIKE EVERY POLLER WRITE. Each UPDATE carries `claimed_by = :worker` and the source state,
and RETURNS the id (property 1 of db/reap_agentic_ledger.py: an UPDATE's `execute` has no
rowcount on Postgres). None = "the lease moved or the row left the state" = lost claim.

THE READS ARE NARROW AND LOCAL. `enrichment_proofs_for_variant` and `mirror_seed_for_product`
read rows our own storefront-proof writers produced; no network, no crawl, no Reap call.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from db.database import IS_POSTGRES, database
import db.reap_agentic_ledger as ledger

PREFLIGHT_PENDING = "pending"
PREFLIGHT_OUTCOMES = frozenset({"ok", "price_changed", "refused", "unverified"})
LIVE_PRICE_STAGES = frozenset({"preflight", "approval"})
CORROBORATION_SOURCES = frozenset({"enrichment_proof", "mirror_proof"})

#: Every column mig 258 adds, in the migration's order (the self-heal adds them in this order,
#: so the whole-table parity test compares like with like). Name -> SQLite type.
COLUMNS: Dict[str, str] = {
    "preflight_outcome": "VARCHAR(16)",
    "preflight_error_code": "VARCHAR(64)",
    "preflight_checked_at": "TIMESTAMP",
    "preflight_items_subtotal_minor": "BIGINT",
    "preflight_shipping_minor": "BIGINT",
    "preflight_tax_minor": "BIGINT",
    "preflight_tax_included": "BOOLEAN",
    "preflight_total_minor": "BIGINT",
    "live_unit_price_minor": "BIGINT",
    "live_items_subtotal_minor": "BIGINT",
    "live_quoted_total_minor": "BIGINT",
    "live_price_stage": "VARCHAR(16)",
    "price_rebound_from_minor": "BIGINT",
    "price_rebound_to_minor": "BIGINT",
    "price_corroboration_source": "VARCHAR(32)",
    "price_corroborated_at": "TIMESTAMP",
}

_ADD_COLUMNS_PG = """
    ALTER TABLE IF EXISTS reap_agentic_purchases
        ADD COLUMN IF NOT EXISTS preflight_outcome VARCHAR(16),
        ADD COLUMN IF NOT EXISTS preflight_error_code VARCHAR(64),
        ADD COLUMN IF NOT EXISTS preflight_checked_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS preflight_items_subtotal_minor BIGINT,
        ADD COLUMN IF NOT EXISTS preflight_shipping_minor BIGINT,
        ADD COLUMN IF NOT EXISTS preflight_tax_minor BIGINT,
        ADD COLUMN IF NOT EXISTS preflight_tax_included BOOLEAN,
        ADD COLUMN IF NOT EXISTS preflight_total_minor BIGINT,
        ADD COLUMN IF NOT EXISTS live_unit_price_minor BIGINT,
        ADD COLUMN IF NOT EXISTS live_items_subtotal_minor BIGINT,
        ADD COLUMN IF NOT EXISTS live_quoted_total_minor BIGINT,
        ADD COLUMN IF NOT EXISTS live_price_stage VARCHAR(16),
        ADD COLUMN IF NOT EXISTS price_rebound_from_minor BIGINT,
        ADD COLUMN IF NOT EXISTS price_rebound_to_minor BIGINT,
        ADD COLUMN IF NOT EXISTS price_corroboration_source VARCHAR(32),
        ADD COLUMN IF NOT EXISTS price_corroborated_at TIMESTAMPTZ
"""


async def ensure_price_witness_schema() -> None:
    """Self-heal parity with migration 258 (production deploys skip db/migrations/)."""
    if IS_POSTGRES:
        # GUARDED, like every boot-time heal (db/schema_guard.guarded_add_columns): the ALTER runs
        # only while a column is missing, read from pg_attribute without taking a lock, so a
        # healed table never sees an ACCESS EXCLUSIVE request on every boot.
        from db.schema_guard import guarded_add_columns

        for statement in guarded_add_columns(_ADD_COLUMNS_PG):
            await database.execute(statement)
        return
    present = {r["name"] for r in await database.fetch_all("PRAGMA table_info(reap_agentic_purchases)")}
    if not present:
        return
    for name, kind in COLUMNS.items():
        if name not in present:
            await database.execute(f"ALTER TABLE reap_agentic_purchases ADD COLUMN {name} {kind}")


# ── the preflight witness ────────────────────────────────────────────────────────────────────

#: INTENT FIRST. Written before the partner call, fenced on the exact claim (holder AND
#: claimed_at, like the dispatch fence) and on "no witness yet": a second tick, a second worker or
#: a replayed step can never begin a second witness for this attempt.
_BEGIN_PREFLIGHT_SQL = """
    UPDATE reap_agentic_purchases
       SET preflight_outcome = 'pending', preflight_checked_at = clock_timestamp()
     WHERE id = :id AND state = 'resolving' AND claimed_by = :worker AND claimed_at = :claimed_at
       AND preflight_outcome IS NULL
    RETURNING id
"""
_BEGIN_PREFLIGHT_SQL_SQLITE = _BEGIN_PREFLIGHT_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")

#: A local stop (`rc.ProviderOperationStopped`) is raised BEFORE the request leaves the process,
#: so nothing was quoted: the marker is withdrawn and a later tick may take the witness.
_WITHDRAW_PREFLIGHT_SQL = """
    UPDATE reap_agentic_purchases
       SET preflight_outcome = NULL, preflight_checked_at = NULL
     WHERE id = :id AND state = 'resolving' AND claimed_by = :worker
       AND preflight_outcome = 'pending'
    RETURNING id
"""

#: The witness's outcome, written once over the 'pending' marker. Every field is written as
#: bound (no COALESCE): this is the whole picture of one quote.
_RECORD_PREFLIGHT_SQL = """
    UPDATE reap_agentic_purchases
       SET preflight_outcome = :outcome,
           preflight_error_code = :error_code,
           preflight_checked_at = clock_timestamp(),
           preflight_items_subtotal_minor = :subtotal,
           preflight_shipping_minor = :shipping,
           preflight_tax_minor = :tax,
           preflight_tax_included = :tax_included,
           preflight_total_minor = :total,
           live_unit_price_minor = :live_unit,
           live_items_subtotal_minor = :live_subtotal,
           live_quoted_total_minor = :live_total,
           live_price_stage = :live_stage,
           price_rebound_from_minor = :rebound_from,
           price_rebound_to_minor = :rebound_to,
           price_corroboration_source = :source,
           price_corroborated_at = :corroborated_at
     WHERE id = :id AND state = 'resolving' AND claimed_by = :worker
       AND preflight_outcome = 'pending'
    RETURNING id
"""
_RECORD_PREFLIGHT_SQL_SQLITE = _RECORD_PREFLIGHT_SQL.replace("clock_timestamp()", "CURRENT_TIMESTAMP")

#: The approval quote's price picture (stage 'approval'), in 'quoting'. Written whenever the
#: corroboration dial is on and the picture changed, including back to all-NULL when a later
#: quote matched our price exactly.
_RECORD_LIVE_PRICE_SQL = """
    UPDATE reap_agentic_purchases
       SET live_unit_price_minor = :live_unit,
           live_items_subtotal_minor = :live_subtotal,
           live_quoted_total_minor = :live_total,
           live_price_stage = :live_stage,
           price_rebound_from_minor = :rebound_from,
           price_rebound_to_minor = :rebound_to,
           price_corroboration_source = :source,
           price_corroborated_at = :corroborated_at
     WHERE id = :id AND state = 'quoting' AND claimed_by = :worker
    RETURNING id
"""


def _price_binds(picture: Mapping[str, Any]) -> Dict[str, Any]:
    stage = picture.get("live_stage")
    source = picture.get("source")
    if stage is not None and stage not in LIVE_PRICE_STAGES:
        raise ValueError("live_price_stage is not a known stage")
    if source is not None and source not in CORROBORATION_SOURCES:
        raise ValueError("price_corroboration_source is not a known source")
    for key in ("live_unit", "live_subtotal", "live_total", "rebound_from", "rebound_to"):
        value = picture.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{key} must be a non-negative int or None")
    return {
        "live_unit": picture.get("live_unit"),
        "live_subtotal": picture.get("live_subtotal"),
        "live_total": picture.get("live_total"),
        "live_stage": stage,
        "rebound_from": picture.get("rebound_from"),
        "rebound_to": picture.get("rebound_to"),
        "source": source,
        "corroborated_at": ledger._bind_dt(picture.get("corroborated_at")),
    }


async def begin_preflight(row: Mapping[str, Any], worker_id: str) -> bool:
    values = {"id": str(row["id"]), "worker": worker_id,
              "claimed_at": ledger._bind_dt(row.get("claimed_at"))}
    if IS_POSTGRES:
        found = await database.fetch_one(_BEGIN_PREFLIGHT_SQL, values)
    else:
        found = await database.fetch_one(_BEGIN_PREFLIGHT_SQL_SQLITE, values)
    return found is not None


async def withdraw_preflight(row: Mapping[str, Any], worker_id: str) -> bool:
    found = await database.fetch_one(
        _WITHDRAW_PREFLIGHT_SQL, {"id": str(row["id"]), "worker": worker_id}
    )
    return found is not None


async def record_preflight(
    row: Mapping[str, Any],
    worker_id: str,
    *,
    outcome: str,
    error_code: Optional[str],
    totals: Optional[Mapping[str, Any]] = None,
    picture: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Replace the 'pending' marker with the witness's outcome. False = lost claim."""
    if outcome not in PREFLIGHT_OUTCOMES:
        raise ValueError("preflight_outcome is not a known outcome")
    code = ledger._require_error_code(error_code)
    totals = dict(totals or {})
    tax_included = totals.get("tax_included")
    if tax_included is not None and not isinstance(tax_included, bool):
        raise ValueError("preflight_tax_included must be a bool or None")
    values = {
        "id": str(row["id"]), "worker": worker_id, "outcome": outcome, "error_code": code,
        "subtotal": totals.get("subtotal"), "shipping": totals.get("shipping"),
        "tax": totals.get("tax"), "tax_included": tax_included, "total": totals.get("total"),
        **_price_binds(picture or {}),
    }
    if IS_POSTGRES:
        found = await database.fetch_one(_RECORD_PREFLIGHT_SQL, values)
    else:
        found = await database.fetch_one(_RECORD_PREFLIGHT_SQL_SQLITE, values)
    return found is not None


async def record_live_price(row: Mapping[str, Any], worker_id: str, picture: Mapping[str, Any]) -> bool:
    """Write the approval quote's price picture on a 'quoting' row we hold. False = lost claim."""
    values = {"id": str(row["id"]), "worker": worker_id, **_price_binds(picture)}
    found = await database.fetch_one(_RECORD_LIVE_PRICE_SQL, values)
    return found is not None


# ── the independent store reads (corroboration) ──────────────────────────────────────────────

#: Our storefront proofs for ONE live Shopify variant of one enrichment product, every sku
#: spelling of it (bounded). A cart-link row stores `variant_key = 'shopify:<id>'`, never the sku
#: key, so the proof is found by the variant it NAMES -- the one in the purchase's own cart URL.
_ENRICHMENT_PROOFS_FOR_VARIANT_SQL = """
    SELECT product_key, sku_key, shop_host, handle, variant_id, available, live_price_minor,
           currency, source, checked_at, outcome
      FROM enrichment_cart_variant_proofs
     WHERE product_key = :product_key AND variant_id = :variant_id
     ORDER BY sku_key
     LIMIT 20
"""

#: The mirror row's provenance (catalog_products.source_ref is the seed id).
_MIRROR_PRODUCT_SQL = """
    SELECT p.source_ref, p.source_system
      FROM catalog_products p
     WHERE p.product_key = :product_key
       AND p.source_system = 'external_product_seeds_mirror_v1'
       AND p.suppression_reason IS NULL AND p.suppressed_at IS NULL
"""

#: The active same-market seed attached to that product, exactly as the cart-link route reads it.
_MIRROR_SEED_SQL = """
    SELECT e.seed_data, e.destination_url, e.canonical_url
      FROM external_product_seeds e
     WHERE e.id = :seed_id AND e.status = 'active'
       AND e.attached_product_key = :product_key
       AND upper(e.market) = :market_country
"""


async def enrichment_proofs_for_variant(product_key: str, variant_id: str) -> List[Dict[str, Any]]:
    """Proof rows for (product_key, Shopify variant id). [] when the table is absent/unreadable:
    a proof nobody can read is no corroboration."""
    from db import enrichment_cart_variant_proofs as proofs

    if not await proofs.ensure_table():
        return []
    rows = await database.fetch_all(
        _ENRICHMENT_PROOFS_FOR_VARIANT_SQL,
        {"product_key": product_key, "variant_id": variant_id},
    )
    return [dict(r) for r in rows]


async def mirror_seed_for_product(product_key: str, market_country: str) -> Optional[Dict[str, Any]]:
    """The active attached seed of a MIRROR catalog row, or None (not a mirror row, no seed, or the
    seed tables are not readable here)."""
    try:
        product = await database.fetch_one(_MIRROR_PRODUCT_SQL, {"product_key": product_key})
        if product is None or not str(product["source_ref"] or "").strip():
            return None
        seed = await database.fetch_one(
            _MIRROR_SEED_SQL,
            {"seed_id": str(product["source_ref"]), "product_key": product_key,
             "market_country": str(market_country or "").strip().upper()},
        )
    except Exception:  # noqa: BLE001 -- an unreadable seed table is no corroboration
        return None
    return dict(seed) if seed is not None else None


__all__ = [
    "COLUMNS",
    "CORROBORATION_SOURCES",
    "LIVE_PRICE_STAGES",
    "PREFLIGHT_OUTCOMES",
    "PREFLIGHT_PENDING",
    "begin_preflight",
    "enrichment_proofs_for_variant",
    "ensure_price_witness_schema",
    "mirror_seed_for_product",
    "record_live_price",
    "record_preflight",
    "withdraw_preflight",
]
