"""Retention of crawled discount codes (ADR-027 Q18, peng 2026-11-07), enforced at the end of every refresh run.

Applies ONLY to rows the crawl writer created (services/deal_code_capture_writer.py): `source = 'merchant_published'`
AND a deal_id that starts with the writer's own prefix (CRAWL_DEAL_ID_PREFIX, "dlc_"), and to their verification rows.
Feed, automatic and channel rows, and any merchant_published row entered another way (by hand, from a connected
store), are never touched. The rules:
  1. a seller on the opt-out denylist: every one of its rows is deleted at once, by the writer's own opt-out predicate
     (the host or its www. form, or a merchant id the catalog holds for the host; case-insensitive, no limit) (Q20);
  2. found and never verified, not seen again: deleted 14 days after it was last seen (`observed_at`);
  3. refused by the store: deleted 7 days after its last refusal, unless a verification of it is still applied;
  4. applied: kept while a verification is valid; deleted 30 days after the last one stopped being valid;
  5. the bar text (`terms_text`): cleared once any verification exists, or 14 days after the row was created.

INDEPENDENT OF THE WRITE FLAG on purpose: rows written while DEAL_CODE_CAPTURE_WRITE was on must still expire after it is
turned off (a rollback). It runs on every refresh run unless DEAL_CODE_RETENTION_PAUSED is "true" (a kill switch,
default off: retention runs). With no merchant_published rows it costs a few indexed reads.

BOUNDED AND IDEMPOTENT: each rule deletes in batches of `BATCH` ids, at most `MAX_ROWS_PER_RULE` rows per run; a run
that stops at the cap resumes on the next run, and a row already deleted is simply not found again. Counts only are
returned and logged — never a code, a seller or bar text. Every failure is swallowed per rule and counted: retention
can never fail the refresh job.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import and_, exists, func, not_, or_, select

from db.database import database
from db.deal_lane import deal_lane_optout, observed_store_offer_verifications as v, observed_store_offers as o
from services.deal_code_capture_writer import CRAWL_DEAL_ID_PREFIX, optout_predicate_sql

logger = logging.getLogger(__name__)

UNVERIFIED_DAYS = 14
REFUSED_DAYS = 7
APPLIED_GRACE_DAYS = 30
TERMS_TEXT_DAYS = 14
BATCH = 500
MAX_ROWS_PER_RULE = 5000
_SOURCE = "merchant_published"


def paused() -> bool:
    return str(os.environ.get("DEAL_CODE_RETENTION_PAUSED", "")).strip().lower() == "true"


def _ours():
    # The crawl writer's own rows only: its source AND its deal_id prefix ("_" escaped: it is a LIKE wildcard).
    escaped = CRAWL_DEAL_ID_PREFIX.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
    return and_(o.c.source == _SOURCE, o.c.deal_id.like(escaped + "%", escape="\\"))


def _has_state(state: str):
    return exists().where(and_(v.c.deal_id == o.c.deal_id, v.c.state == state))


async def _delete_ids(ids: List[str]) -> int:
    ids = [i for i in ids if str(i).startswith(CRAWL_DEAL_ID_PREFIX)]  # belt and braces: never another writer's id
    if not ids:
        return 0
    await database.execute(v.delete().where(v.c.deal_id.in_(ids)))
    await database.execute(o.delete().where(and_(o.c.deal_id.in_(ids), _ours())))
    return len(ids)


async def _delete_where(condition) -> int:
    """Delete our rows matching `condition`, in batches, up to MAX_ROWS_PER_RULE. Returns the rows deleted."""
    deleted = 0
    while deleted < MAX_ROWS_PER_RULE:
        limit = min(BATCH, MAX_ROWS_PER_RULE - deleted)
        rows = await database.fetch_all(select(o.c.deal_id).where(and_(_ours(), condition)).limit(limit))
        ids = [r["deal_id"] for r in rows]
        if not ids:
            break
        deleted += await _delete_ids(ids)
        if len(ids) < limit:
            break
    return deleted


async def _delete_opted_out() -> int:
    """Every crawl row whose seller is on the opt-out denylist, by the writer's own predicate (no limit, case-insensitive,
    host / www. form / the catalog's merchant ids for the host)."""
    return await _delete_where(exists().where(optout_predicate_sql(o.c.seller_id)).correlate_except(deal_lane_optout))


async def run(now: Optional[datetime] = None) -> Dict[str, int]:
    """Apply the rules once. Never raises. Returns counts per rule (and `errors`)."""
    counts: Dict[str, int] = {}
    if paused():
        return {"paused": 1}
    now = now or datetime.now(timezone.utc)

    async def step(name, coro):
        try:
            counts[name] = await coro
        except Exception as exc:  # noqa: BLE001 - one rule's failure never costs the others or the job
            counts["errors"] = counts.get("errors", 0) + 1
            logger.warning("deal code retention %s failed: %s", name, type(exc).__name__)

    await step("deleted_optout", _delete_opted_out())
    # 2. never verified, not seen for 14 days: no verification row in a final state.
    await step("deleted_unverified", _delete_where(and_(
        o.c.observed_at < now - timedelta(days=UNVERIFIED_DAYS),
        not_(_has_state("checkout_verified")), not_(_has_state("failed")),
    )))
    # 3. refused 7 days ago or more, and not applied anywhere.
    last_refusal = (
        select(func.max(v.c.updated_at)).where(and_(v.c.deal_id == o.c.deal_id, v.c.state == "failed")).scalar_subquery()
    )
    await step("deleted_refused", _delete_where(and_(
        _has_state("failed"), not_(_has_state("checkout_verified")),
        last_refusal < now - timedelta(days=REFUSED_DAYS),
    )))
    # 4. applied, but no verification valid for the last 30 days.
    still_valid_recently = exists().where(and_(
        v.c.deal_id == o.c.deal_id, v.c.state == "checkout_verified",
        or_(v.c.expires_at.is_(None), v.c.expires_at >= now - timedelta(days=APPLIED_GRACE_DAYS)),
    ))
    await step("deleted_applied_expired", _delete_where(and_(_has_state("checkout_verified"), not_(still_valid_recently))))

    # 5. bar text: cleared once checked, or 14 days after creation.
    async def clear_terms():
        cond = and_(_ours(), o.c.terms_text.isnot(None), or_(
            exists().where(v.c.deal_id == o.c.deal_id),
            o.c.created_at < now - timedelta(days=TERMS_TEXT_DAYS),
        ))
        cleared = 0
        while cleared < MAX_ROWS_PER_RULE:
            rows = await database.fetch_all(select(o.c.deal_id).where(cond).limit(min(BATCH, MAX_ROWS_PER_RULE - cleared)))
            ids = [r["deal_id"] for r in rows]
            if not ids:
                break
            await database.execute(o.update().where(and_(o.c.deal_id.in_(ids), _ours())).values(terms_text=None, updated_at=now))
            cleared += len(ids)
        return cleared

    await step("cleared_terms_text", clear_terms())
    if any(counts.get(k) for k in counts):
        logger.warning("deal code retention: %s", {k: n for k, n in counts.items() if n})
    return counts
