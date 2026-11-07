"""Retention of crawled discount codes (ADR-027 Q18, peng 2026-11-07), enforced at the end of every refresh run.

Applies ONLY to rows the crawl writer created (services/deal_code_capture_writer.py): `source = 'merchant_published'`
AND a deal_id that starts with the writer's own prefix (CRAWL_DEAL_ID_PREFIX, "dlc_"), and to their verification rows.
Feed, automatic and channel rows, and any merchant_published row entered another way (by hand, from a connected
store), are never touched. The rules:
  1. a seller on the opt-out denylist: every one of its rows is deleted at once. The denied hosts are read once per
     run by the writer's `denied_hosts` (an id as a host or its www. form, trimmed and case-insensitive; or the catalog
     hosts of an id as a merchant id, never a placeholder id and never one spanning many hosts) (Q20);
  2. found and never applied or refused, not seen again: deleted 14 days after it was last seen (`observed_at`);
  3. refused by the store and never applied for any variant: deleted 7 days after its last refusal;
  4. applied: kept while a verification is valid; deleted 30 days after the last valid-until across its verification
     history, whatever state a lapsed verification is in now (`expired`, or back to `unverified`);
  5. the bar text (`terms_text`): cleared once a check recorded an outcome, or 14 days after the row was created.
Every DELETE re-checks its rule's condition, so a verification that lands between the read and the delete keeps the row.

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
from db.deal_lane import observed_store_offer_verifications as v, observed_store_offers as o
from services.deal_code_capture_writer import CRAWL_DEAL_ID_PREFIX, SHARED_PLATFORM_HOSTS, crawl_rows, denied_hosts

logger = logging.getLogger(__name__)

UNVERIFIED_DAYS = 14
REFUSED_DAYS = 7
APPLIED_GRACE_DAYS = 30
TERMS_TEXT_DAYS = 14
BATCH = 500
MAX_ROWS_PER_RULE = 5000


def paused() -> bool:
    return str(os.environ.get("DEAL_CODE_RETENTION_PAUSED", "")).strip().lower() == "true"


def _ours():
    return crawl_rows()


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")


def applied_history():
    """A verification row recording that the code applied at checkout, whatever its state is now (review of be#2735,
    P2-2): a lapsed verification may go to `expired` or back to `unverified` (ADR-027), and it still counts."""
    return or_(v.c.verified_at.isnot(None), v.c.state.in_(("checkout_verified", "expired")))


def checked_verification():
    """A verification row that records a check's outcome, not one merely queued (P3-2)."""
    return or_(v.c.state != "unverified", v.c.verified_at.isnot(None))


def _has_state(state: str):
    return exists().where(and_(v.c.deal_id == o.c.deal_id, v.c.state == state))


def _was_applied():
    return exists().where(and_(v.c.deal_id == o.c.deal_id, applied_history()))


async def _delete_ids(ids: List[str], condition) -> int:
    """Delete these offers, re-checking the rule's condition in the DELETE itself (P3-5), then any verification rows
    left without their offer (on Postgres the FK cascade already removed them). Returns how many are gone."""
    ids = [i for i in ids if str(i).startswith(CRAWL_DEAL_ID_PREFIX)]  # belt and braces: never another writer's id
    if not ids:
        return 0
    await database.execute(o.delete().where(and_(o.c.deal_id.in_(ids), _ours(), condition)))
    await database.execute(v.delete().where(and_(
        v.c.deal_id.in_(ids), v.c.deal_id.notin_(select(o.c.deal_id).where(o.c.deal_id.in_(ids))),
    )))
    left = await database.fetch_all(select(o.c.deal_id).where(o.c.deal_id.in_(ids)))
    return len(ids) - len(left)


async def _delete_where(condition, cap: Optional[int] = None) -> int:
    """Delete our rows matching `condition`, in batches, up to `cap` (MAX_ROWS_PER_RULE). Returns the rows deleted."""
    cap = MAX_ROWS_PER_RULE if cap is None else cap
    deleted = 0
    while deleted < cap:
        limit = min(BATCH, cap - deleted)
        rows = await database.fetch_all(select(o.c.deal_id).where(and_(_ours(), condition)).limit(limit))
        ids = [r["deal_id"] for r in rows]
        if not ids:
            break
        gone = await _delete_ids(ids, condition)
        deleted += gone
        if gone == 0 or len(ids) < limit:  # no progress (P3-10) or the last batch
            break
    return deleted


async def _delete_opted_out() -> int:
    """Every crawl row whose seller host the opt-out denylist covers. The denied host set is read ONCE per run,
    uncorrelated (P2-1); then rows are deleted by membership. No opt-out rows: no further query."""
    hosts, notes = await denied_hosts()
    if notes:
        logger.warning("deal code retention: opt-out ids not applied to the catalog: %s", notes)
    if not hosts:
        return 0
    ordered = sorted(hosts)
    seller = func.lower(func.trim(o.c.seller_id))
    deleted = 0
    for k in range(0, len(ordered), BATCH):
        if deleted >= MAX_ROWS_PER_RULE:
            break
        chunk = ordered[k:k + BATCH]
        # The host itself (bare or www.), or a subdomain of it — never through a shared platform host (writer `covers`).
        parents = [h for h in chunk if "." in h and not h.startswith("www.") and h not in SHARED_PLATFORM_HOSTS]
        condition = or_(seller.in_(chunk), *[seller.like("%." + _like_escape(h), escape="\\") for h in parents])
        deleted += await _delete_where(condition, MAX_ROWS_PER_RULE - deleted)
    return deleted


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
    # 2. never applied and never refused, not seen for 14 days.
    await step("deleted_unverified", _delete_where(and_(
        o.c.observed_at < now - timedelta(days=UNVERIFIED_DAYS),
        not_(_was_applied()), not_(_has_state("failed")),
    )))
    # 3. refused 7 days ago or more, and never applied for any variant.
    last_refusal = (
        select(func.max(v.c.updated_at)).where(and_(v.c.deal_id == o.c.deal_id, v.c.state == "failed")).scalar_subquery()
    )
    await step("deleted_refused", _delete_where(and_(
        _has_state("failed"), not_(_was_applied()),
        last_refusal < now - timedelta(days=REFUSED_DAYS),
    )))
    # 4. applied: 30 days after the last valid-until across its verification history, whatever the state is now.
    valid_until = (
        select(func.max(func.coalesce(v.c.expires_at, v.c.verified_at, v.c.updated_at)))
        .where(and_(v.c.deal_id == o.c.deal_id, applied_history())).scalar_subquery()
    )
    await step("deleted_applied_expired", _delete_where(and_(
        _was_applied(), valid_until < now - timedelta(days=APPLIED_GRACE_DAYS),
    )))

    # 5. bar text: cleared once a check recorded an outcome, or 14 days after creation.
    async def clear_terms():
        cond = and_(_ours(), o.c.terms_text.isnot(None), or_(
            exists().where(and_(v.c.deal_id == o.c.deal_id, checked_verification())),
            o.c.created_at < now - timedelta(days=TERMS_TEXT_DAYS),
        ))
        cleared = 0
        while cleared < MAX_ROWS_PER_RULE:
            rows = await database.fetch_all(select(o.c.deal_id).where(cond).limit(min(BATCH, MAX_ROWS_PER_RULE - cleared)))
            ids = [r["deal_id"] for r in rows if str(r["deal_id"]).startswith(CRAWL_DEAL_ID_PREFIX)]
            if not ids:
                break
            await database.execute(o.update().where(and_(o.c.deal_id.in_(ids), cond)).values(terms_text=None, updated_at=now))
            cleared += len(ids)
        return cleared

    await step("cleared_terms_text", clear_terms())
    if any(counts.get(k) for k in counts):
        logger.warning("deal code retention: %s", {k: n for k, n in counts.items() if n})
    return counts
