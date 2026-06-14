"""Phase 4 (measure & defend): did the merchant WIN the niches they targeted?

Records each audit's per-niche ownership (per merchant + query) so a re-audit can
show the movement on the niches Phase 2 told them to target — the loop-closing
signal. Best-effort: a missing table degrades to "no movement data", never an
error that breaks the audit.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

# ownership_state values, grouped for movement classification.
_WON = {"merchant-owned", "merchant-mentioned"}
_LOST = {"competitor-owned", "retailer-owned", "marketplace-owned", "publisher-owned", "forum-owned"}
_OPEN = {"open-lane"}
# Only these are worth recording for outcome tracking (no-demand/empty are noise).
_MEANINGFUL = _WON | _LOST | _OPEN


def _norm(q: Any) -> str:
    return str(q or "").strip().lower()


def _movement(prior: Optional[str], current: Optional[str]) -> str:
    """Classify the change between two audits' ownership of a niche."""
    cur = current or ""
    prv = prior
    if cur in _WON and (prv is None or prv in _OPEN or prv in _LOST):
        return "won"          # newly owned — the strategy worked
    if cur in _WON:
        return "holding"      # owned in both
    if cur in _LOST and prv in _WON:
        return "lost"         # was owned, now a competitor owns it — DEFEND
    if cur in _OPEN and prv in _OPEN:
        return "still_open"   # keep working it
    if prv is None:
        return "new"          # first time we've measured this niche
    return "shifted"


async def record_niche_outcomes(
    *,
    per_sku_reports: List[Dict[str, Any]],
    merchant_id: str,
    audit_run_id: Optional[str],
    db: Any = None,
) -> int:
    """Record this audit's per-niche ownership (one row per meaningful niche).
    Deduped by query (best opportunity_score kept). Best-effort."""
    if not merchant_id:
        return 0
    best: Dict[str, Dict[str, Any]] = {}
    for r in per_sku_reports or []:
        if not isinstance(r, dict):
            continue
        for row in (r.get("opportunity") or {}).get("per_prompt") or []:
            if not isinstance(row, dict):
                continue
            state = row.get("ownership_state")
            if state not in _MEANINGFUL:
                continue
            q = _norm(row.get("normalized_query") or row.get("query"))
            if not q:
                continue
            score = float(row.get("opportunity_score") or 0)
            prev = best.get(q)
            if prev is None or score > prev["score"]:
                best[q] = {
                    "q": q,
                    "state": state,
                    "score": score,
                    "open_lane": bool(row.get("open_lane")),
                }
    if not best:
        return 0
    write_db = db or database
    written = 0
    for entry in best.values():
        try:
            await write_db.execute(
                """
                INSERT INTO niche_target_outcomes
                  (merchant_id, normalized_query, ownership_state, opportunity_score,
                   open_lane, audit_run_id)
                VALUES (:mid, :q, :state, :score, :open_lane, :run)
                """,
                {
                    "mid": str(merchant_id), "q": entry["q"], "state": entry["state"],
                    "score": round(entry["score"], 2), "open_lane": entry["open_lane"],
                    "run": str(audit_run_id) if audit_run_id else None,
                },
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("niche_outcomes insert failed for %r: %s", entry["q"], str(exc)[:160])
            break  # table likely absent (pre-migration) — stop quietly
    return written


async def niche_movement_for_queries(
    merchant_id: str,
    queries: Iterable[Any],
    *,
    db: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """Map normalized_query -> {movement, current, prior} by comparing the two
    most-recent DISTINCT audits of each query for this merchant. Excludes the
    current audit's just-written row by comparing across distinct audit_run_ids.
    Empty dict on any miss. Best-effort."""
    norm = list({_norm(q) for q in (queries or []) if _norm(q)})
    if not norm or not merchant_id:
        return {}
    read_db = db or database
    try:
        rows = await read_db.fetch_all(
            """
            SELECT normalized_query, ownership_state, audit_run_id, seen_at
            FROM niche_target_outcomes
            WHERE merchant_id = :mid AND normalized_query = ANY(:queries)
            ORDER BY normalized_query, seen_at DESC
            """,
            {"mid": str(merchant_id), "queries": norm},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("niche_outcomes read failed: %s", str(exc)[:160])
        return {}

    by_query: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_query.setdefault(r["normalized_query"], []).append(r)

    out: Dict[str, Dict[str, Any]] = {}
    for q, recs in by_query.items():
        # recs are newest-first; collapse to the latest state per distinct audit run.
        per_run: List[str] = []
        seen_runs = set()
        for rec in recs:
            run = rec["audit_run_id"]
            key = run or f"_norun_{len(seen_runs)}"
            if key in seen_runs:
                continue
            seen_runs.add(key)
            per_run.append(rec["ownership_state"])
            if len(per_run) >= 2:
                break
        current = per_run[0] if per_run else None
        prior = per_run[1] if len(per_run) > 1 else None
        out[q] = {
            "movement": _movement(prior, current),
            "current": current,
            "prior": prior,
        }
    return out
