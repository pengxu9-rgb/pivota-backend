"""Undo one retailer-ingest run's shade-fold retire: the offers it suppressed `fold_superseded`.

The retire (catalog_enrichment_agent.apply._retire_offers) stamps every offer it takes with
`suppression_metadata.fold_retire.run_id` = the drain run that applied it. A revert lifts exactly those rows:
still under `fold_superseded` AND still carrying that run id. A row a later run retired again, a row the offer
upsert already revived, or a row another lane has since suppressed for its own reason is never touched.

Dry run by default (lists the rows); `apply=True` writes, in one statement, and returns the offer_ids it lifted.
A lifted offer is live again exactly as it was -- if the crawl that retired it was right, the cohort's next fold
run retires it again; a revert is for a retire that was wrong.
"""

from __future__ import annotations

from typing import Any, Dict, List

#: What a run retired, as it stands.
FOLD_RETIRED_BY_RUN_SQL = """
SELECT offer_id, product_key, source_ref, source_system
FROM catalog_offers
WHERE suppression_reason = :reason
  AND suppression_metadata -> 'fold_retire' ->> 'run_id' = :run_id
ORDER BY offer_id
"""

#: The revert: the same predicate, read back.
REVERT_FOLD_RETIRE_SQL = """
UPDATE catalog_offers
   SET suppressed_at = NULL,
       suppression_reason = NULL,
       suppression_metadata = suppression_metadata - 'fold_retire'
 WHERE suppression_reason = :reason
   AND suppression_metadata -> 'fold_retire' ->> 'run_id' = :run_id
RETURNING offer_id
"""


async def revert_fold_retire(run_id: str, *, apply: bool = False, db: Any = None) -> Dict[str, Any]:
    """{run_id, apply, offers: [the rows still retired by that run], reverted: [offer_ids lifted, when apply]}."""
    from services.catalog_enrichment_agent.apply import OFFER_FOLD_SUPERSEDED_REASON

    run_id = str(run_id or "").strip()
    if not run_id:
        raise ValueError("a fold retire revert needs the run id the retire was stamped with")
    if db is None:
        from db.database import database as db
    values = {"reason": OFFER_FOLD_SUPERSEDED_REASON, "run_id": run_id}
    rows = [dict(r) for r in await db.fetch_all(FOLD_RETIRED_BY_RUN_SQL, values) or []]
    out: Dict[str, Any] = {"run_id": run_id, "apply": bool(apply), "offers": rows, "reverted": []}
    if apply and rows:
        reverted: List[str] = sorted(str(r["offer_id"]) for r in await db.fetch_all(REVERT_FOLD_RETIRE_SQL, values) or [])
        out["reverted"] = reverted
    return out
