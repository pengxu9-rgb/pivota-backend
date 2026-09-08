"""Cascade a product's suppression to that product's offers, AT THE WRITE.

THE DEFECT. `catalog_products.suppressed_at` is the gate column for the PRODUCT.
It is not the gate column for the product's OFFERS: `catalog_offers` carries its
own `suppressed_at`, and every offer-grain read lane
(`services/priced_offer_sql`, `agent_pdp_view_assembler.fetch_offers_for_keys`,
the recall candidate CTE) filters on the OFFER's column, not the product's. So a
writer that tombstones a product and stops there leaves live offers behind —
rows that still price a product nothing else will serve.

Measured on prod 2026-09-08: **2,171 suppressed products carried unsuppressed
offers**. Every product-suppressing writer in this repo except
`scripts/withdraw_catalog_rows.py` (which already walks
products/skus/offers together) produced some of them.

WHY A SHARED HELPER AND NOT A TRIGGER. The five call sites select their products
differently — by `source_ref` + `source_system`, by `product_key = ANY(...)`, by
seed id — and three of them are dry-run-able scripts whose plan must be able to
report what the cascade WOULD touch. A helper that takes product keys and hands
back the offer ids it moved satisfies both: each writer resolves its own rows
(with `RETURNING product_key`, because `databases`+asyncpg gives no rowcount from
`execute()`), then calls in here.

BOTH COLUMNS, ALWAYS. `suppressed_at` is what every serving gate reads and
`suppression_reason` is what `catalog_trust_policy._derive_source_lifecycle`
reads; a row with one and not the other is simultaneously withdrawn and clean,
which is the `suppression_reason_without_timestamp` invariant's whole subject.
The two statements below set and clear them together, in one UPDATE.

THE REVERT IS GUARDED ON OUR OWN REASON. `revert_offer_suppression` only clears
offers whose `suppression_reason` is the one it was asked to undo, so an offer a
different lane tombstoned before we arrived keeps its gate and its label. This is
the rule `scripts/withdraw_catalog_rows.py::_revert` already applies per table;
without it, reverting a product would resurrect offers nobody asked to bring
back.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

#: The label a cascaded offer suppression carries. Distinct from whatever reason
#: the PRODUCT carries on purpose: the offer was not independently judged, it was
#: gated because its product was, and the revert path keys on exactly this.
PRODUCT_SUPPRESSED_REASON = "product_suppressed"

#: Module-level literals so the repo's PREPARE sweep
#: (tests/test_repo_sql_prepare_postgres.py) can follow them from the call site
#: and ask Postgres to plan them. A statement built inside the function body is
#: invisible to that sweep.
CASCADE_OFFERS_SQL = """
    UPDATE catalog_offers
       SET suppressed_at = NOW(),
           suppression_reason = CAST(:reason AS text),
           updated_at = NOW()
     WHERE product_key = ANY(:product_keys)
       AND suppressed_at IS NULL
    RETURNING offer_id
"""

REVERT_OFFERS_SQL = """
    UPDATE catalog_offers
       SET suppressed_at = NULL,
           suppression_reason = NULL,
           updated_at = NOW()
     WHERE product_key = ANY(:product_keys)
       AND suppressed_at IS NOT NULL
       AND suppression_reason = CAST(:reason AS text)
    RETURNING offer_id
"""

#: Read-only: what a cascade WOULD suppress. The dry-run half of every caller,
#: so a plan can name the offers instead of promising a number it never checked.
LIVE_OFFERS_FOR_PRODUCTS_SQL = """
    SELECT offer_id
      FROM catalog_offers
     WHERE product_key = ANY(:product_keys)
       AND suppressed_at IS NULL
     ORDER BY offer_id
"""


def _normalize(product_keys: Iterable[str]) -> List[str]:
    """De-duplicated, sorted, blank-free. Sorted so a batch's audit trail is
    reproducible and two runs over the same set compare equal."""
    return sorted({str(key or "").strip() for key in (product_keys or [])} - {""})


async def cascade_offer_suppression(
    product_keys: Iterable[str],
    *,
    reason: str = PRODUCT_SUPPRESSED_REASON,
    db: Any = None,
) -> List[str]:
    """Suppress every LIVE offer on these products. Returns the offer ids moved.

    `RETURNING offer_id` rather than a rowcount: `databases` + asyncpg returns no
    rowcount from `execute()` at all (it does on SQLite, which is exactly how a
    caller comes to believe it has one), so the count of moved rows is only
    available by projecting the ids.

    Idempotent: `suppressed_at IS NULL` means a second call over the same
    products returns `[]` rather than re-stamping timestamps a first run set.
    """
    keys = _normalize(product_keys)
    if not keys:
        return []
    write_db = db or database
    rows = await write_db.fetch_all(
        CASCADE_OFFERS_SQL, {"product_keys": keys, "reason": reason}
    )
    offer_ids = [str(row["offer_id"]) for row in (rows or [])]
    if offer_ids:
        logger.info(
            "catalog_offer_suppression: cascaded %d offer(s) across %d product(s) "
            "reason=%s",
            len(offer_ids), len(keys), reason,
        )
    return offer_ids


async def revert_offer_suppression(
    product_keys: Iterable[str],
    *,
    reason: str = PRODUCT_SUPPRESSED_REASON,
    db: Any = None,
) -> List[str]:
    """Undo OUR cascade on these products. Returns the offer ids restored.

    Only offers carrying `reason` are cleared. An offer suppressed by the merge
    lane, by the reconciler's `duplicate_offer` pass, or by a currency
    quarantine keeps both of its columns — a revert that resurrected those would
    be un-reverting somebody else's decision.
    """
    keys = _normalize(product_keys)
    if not keys:
        return []
    write_db = db or database
    rows = await write_db.fetch_all(
        REVERT_OFFERS_SQL, {"product_keys": keys, "reason": reason}
    )
    return [str(row["offer_id"]) for row in (rows or [])]


async def live_offers_for_products(
    product_keys: Iterable[str],
    *,
    db: Any = None,
) -> List[str]:
    """The dry-run twin of :func:`cascade_offer_suppression` — read-only."""
    keys = _normalize(product_keys)
    if not keys:
        return []
    read_db = db or database
    rows = await read_db.fetch_all(LIVE_OFFERS_FOR_PRODUCTS_SQL, {"product_keys": keys})
    return [str(row["offer_id"]) for row in (rows or [])]


async def cascade_for_suppressed_product_keys(
    product_keys: Optional[Iterable[str]],
    *,
    apply: bool,
    reason: str = PRODUCT_SUPPRESSED_REASON,
    db: Any = None,
) -> List[str]:
    """One entry point for a writer that is dry-run-able: plan or act.

    Callers that already branch on `--apply` get the same shape either way — the
    offer ids that were (or would be) suppressed — so their report cannot say
    one thing in a plan and another in a run.
    """
    if apply:
        return await cascade_offer_suppression(product_keys or [], reason=reason, db=db)
    return await live_offers_for_products(product_keys or [], db=db)
