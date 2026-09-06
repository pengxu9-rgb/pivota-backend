"""Retention for the canonical commerce ledger.

Nothing has ever deleted from `commerce_interaction_events` or
`commerce_interactions`. The ops telemetry canary writes an eight-event chain
per run and those rows accumulate forever, so the first thing this module does
is give the probe rows — and only the probe rows — a way out.

TWO SHAPES OF PROBE ROW, and both are swept:

* `synthetic IS TRUE` — the column migration 213 added and every ingress now
  stamps. Migration 214 built the partial index
  `idx_commerce_interaction_events_synthetic ON (merchant_id, occurred_at DESC)
  WHERE synthetic IS TRUE` precisely so this sweep never scans real history.
* `surface = 'ops_canary'` with `synthetic` false or NULL — the pre-column
  shape, written by every canary run before 213 shipped. This sweep treats
  that surface as synthetic. `surface` is a caller-supplied string, so this is
  a deliberate widening: a merchant who labels its own real traffic
  `ops_canary` has its rows deleted by this job. That surface is already
  excluded from every default funnel read for the same reason, so such rows
  are invisible to the merchant either way, and leaving the pre-213 probe rows
  permanently undeletable is the worse outcome.

REAL COMMERCE HISTORY IS NEVER DELETED HERE. `report_ledger_retention` exists
so the policy for real rows can be decided with numbers instead of a guess;
this module has no code path that deletes a non-probe row.

`commerce_interactions` has no `synthetic` column, so a probe interaction is
defined by its events: an interaction is deleted only when it has NO
`commerce_interaction_events` row left at all. An interaction that somehow
carries one real event and one synthetic event keeps its row and keeps the
real event.

`database.execute()` returns no rowcount for a DELETE under asyncpg (SQLite
does return one), so nothing here branches on it: every batch selects the ids
first, counts what it selected, deletes by that id list, and verifies with a
follow-up count.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, not_, or_, select

from db.commerce_interactions import commerce_interaction_events, commerce_interactions
from db.database import database


OPS_CANARY_SURFACE = "ops_canary"

DEFAULT_OLDER_THAN_DAYS = 7
DEFAULT_BATCH_SIZE = 1000
MAX_BATCH_SIZE = 10000
DEFAULT_MAX_BATCHES = 1000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Aware UTC only.

    asyncpg binds a naive datetime with the CLIENT PROCESS timezone, so a
    naive cutoff would move this job's blast radius by the deploy's offset.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    return str(value)


def _synthetic_predicate():
    """What this sweep counts as a probe row.

    The stamped column is authoritative; the surface match covers rows written
    before that column existed. A NULL `synthetic` is not on its own evidence
    of a probe — the surface has to say so.

    NULL-FREE ON PURPOSE. `not_()` of this predicate is what decides whether a
    real event protects its interaction from deletion, and SQL three-valued
    logic makes `NOT NULL` unknown, not true. `synthetic IS TRUE` is already
    false for a NULL; the surface comparison needs COALESCE or a legacy row
    with a NULL surface would fail to protect the interaction it belongs to.
    """
    return or_(
        commerce_interaction_events.c.synthetic.is_(True),
        func.coalesce(commerce_interaction_events.c.surface, "") == OPS_CANARY_SURFACE,
    )


def _sweep_predicate(cutoff: datetime):
    return and_(
        commerce_interaction_events.c.occurred_at < cutoff,
        _synthetic_predicate(),
    )


def _cursor_predicate(cursor: Optional[Tuple[datetime, str]]):
    """Keyset pagination on (occurred_at, event_id).

    A dry run deletes nothing, so an OFFSET-free scan would hand back the same
    batch forever. The same cursor is used on the apply path, where it is
    simply harmless.
    """
    if cursor is None:
        return None
    last_occurred_at, last_event_id = cursor
    return or_(
        commerce_interaction_events.c.occurred_at > last_occurred_at,
        and_(
            commerce_interaction_events.c.occurred_at == last_occurred_at,
            commerce_interaction_events.c.event_id > last_event_id,
        ),
    )


async def _select_batch(
    *, cutoff: datetime, batch_size: int, cursor: Optional[Tuple[datetime, str]]
) -> List[Dict[str, Any]]:
    query = select(
        commerce_interaction_events.c.event_id,
        commerce_interaction_events.c.interaction_id,
        commerce_interaction_events.c.merchant_id,
        commerce_interaction_events.c.occurred_at,
    ).where(_sweep_predicate(cutoff))
    cursor_clause = _cursor_predicate(cursor)
    if cursor_clause is not None:
        query = query.where(cursor_clause)
    query = query.order_by(
        commerce_interaction_events.c.occurred_at.asc(),
        commerce_interaction_events.c.event_id.asc(),
    ).limit(batch_size)
    return [dict(row._mapping) for row in await database.fetch_all(query)]


async def _orphan_interaction_ids(interaction_ids: Sequence[str]) -> List[str]:
    """Interactions with no `commerce_interaction_events` row left at all.

    Used on the APPLY path, after the batch's events are gone. A mixed
    interaction — one real event, one synthetic — still has its real event, so
    it is not in this list and survives.
    """
    if not interaction_ids:
        return []
    surviving = (
        select(commerce_interaction_events.c.event_id)
        .where(commerce_interaction_events.c.interaction_id == commerce_interactions.c.interaction_id)
        .correlate(commerce_interactions)
        .exists()
    )
    query = (
        select(commerce_interactions.c.interaction_id)
        .where(commerce_interactions.c.interaction_id.in_(list(interaction_ids)))
        .where(not_(surviving))
    )
    return [row._mapping["interaction_id"] for row in await database.fetch_all(query)]


async def _deletable_interaction_ids(
    *, cutoff: datetime, interaction_ids: Sequence[str]
) -> List[str]:
    """The same rule, evaluated BEFORE the delete, for the dry run.

    "No event left after the sweep" == "no event that the sweep would not
    delete". A mixed interaction has an event outside the sweep set and so is
    not reported as deletable.
    """
    if not interaction_ids:
        return []
    surviving = (
        select(commerce_interaction_events.c.event_id)
        .where(
            commerce_interaction_events.c.interaction_id == commerce_interactions.c.interaction_id
        )
        .where(not_(_sweep_predicate(cutoff)))
        .correlate(commerce_interactions)
        .exists()
    )
    query = (
        select(commerce_interactions.c.interaction_id)
        .where(commerce_interactions.c.interaction_id.in_(list(interaction_ids)))
        .where(not_(surviving))
    )
    return [row._mapping["interaction_id"] for row in await database.fetch_all(query)]


async def _remaining_event_count(event_ids: Sequence[str]) -> int:
    if not event_ids:
        return 0
    return int(
        await database.fetch_val(
            select(func.count())
            .select_from(commerce_interaction_events)
            .where(commerce_interaction_events.c.event_id.in_(list(event_ids)))
        )
        or 0
    )


async def _remaining_interaction_count(interaction_ids: Sequence[str]) -> int:
    if not interaction_ids:
        return 0
    return int(
        await database.fetch_val(
            select(func.count())
            .select_from(commerce_interactions)
            .where(commerce_interactions.c.interaction_id.in_(list(interaction_ids)))
        )
        or 0
    )


async def sweep_synthetic_events(
    *,
    older_than_days: int = DEFAULT_OLDER_THAN_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    apply: bool = False,
    max_batches: int = DEFAULT_MAX_BATCHES,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Delete probe rows older than `older_than_days`. Dry run by default.

    Returns, on every path:
      dry_run, events_deleted, interactions_deleted, batches, oldest, newest,
      by_merchant, plus cutoff/older_than_days/batch_size/truncated.

    On a dry run the *_deleted counts are what WOULD be deleted; nothing is
    written. Each batch runs in its own transaction — one transaction is never
    held across batches, so a long sweep cannot pin a snapshot or block
    ledger writers for its whole duration.
    """
    older_than_days = max(0, int(older_than_days))
    batch_size = max(1, min(int(batch_size), MAX_BATCH_SIZE))
    max_batches = max(1, int(max_batches))
    cutoff = _as_utc(now or _utc_now()) - timedelta(days=older_than_days)

    events_seen = 0
    batches = 0
    truncated = False
    oldest: Optional[datetime] = None
    newest: Optional[datetime] = None
    by_merchant: Dict[str, Dict[str, int]] = {}
    interactions_removed = 0
    dry_run_candidates: set[str] = set()
    cursor: Optional[Tuple[datetime, str]] = None

    while True:
        if batches >= max_batches:
            truncated = True
            break
        rows = await _select_batch(cutoff=cutoff, batch_size=batch_size, cursor=cursor)
        if not rows:
            break
        batches += 1
        event_ids = [str(row["event_id"]) for row in rows]
        interaction_ids = sorted({str(row["interaction_id"]) for row in rows if row["interaction_id"]})
        events_seen += len(event_ids)
        for row in rows:
            occurred_at = row.get("occurred_at")
            if isinstance(occurred_at, datetime):
                normalized = _as_utc(occurred_at)
                oldest = normalized if oldest is None or normalized < oldest else oldest
                newest = normalized if newest is None or normalized > newest else newest
            bucket = by_merchant.setdefault(
                str(row.get("merchant_id") or "unknown"), {"events": 0, "interactions": 0}
            )
            bucket["events"] += 1

        last = rows[-1]
        cursor = (last["occurred_at"], str(last["event_id"]))

        if not apply:
            deletable = await _deletable_interaction_ids(
                cutoff=cutoff, interaction_ids=interaction_ids
            )
            dry_run_candidates.update(deletable)
            continue

        # One transaction per batch, never one across batches: a long sweep
        # must not pin a snapshot or hold locks for its whole duration.
        async with database.transaction():
            await database.execute(
                commerce_interaction_events.delete().where(
                    commerce_interaction_events.c.event_id.in_(event_ids)
                )
            )
            orphans = await _orphan_interaction_ids(interaction_ids)
            if orphans:
                await database.execute(
                    commerce_interactions.delete().where(
                        commerce_interactions.c.interaction_id.in_(orphans)
                    )
                )
        # Verify by counting, never by reading a DELETE's return value:
        # `databases` + asyncpg reports no rowcount for an UPDATE/DELETE.
        still_there = await _remaining_event_count(event_ids)
        if still_there:
            raise RuntimeError(
                f"synthetic sweep failed to delete {still_there} of {len(event_ids)} events"
            )
        if orphans:
            interactions_left = await _remaining_interaction_count(orphans)
            if interactions_left:
                raise RuntimeError(
                    f"synthetic sweep failed to delete {interactions_left} of "
                    f"{len(orphans)} orphaned interactions"
                )
            interactions_removed += len(orphans)
            for merchant_id, count in _merchants_from_batch(rows, orphans).items():
                bucket = by_merchant.setdefault(
                    merchant_id, {"events": 0, "interactions": 0}
                )
                bucket["interactions"] += count

    if not apply and dry_run_candidates:
        interactions_removed = len(dry_run_candidates)
        owners = await _merchants_for_interactions(sorted(dry_run_candidates))
        for merchant_id, count in owners.items():
            bucket = by_merchant.setdefault(merchant_id, {"events": 0, "interactions": 0})
            bucket["interactions"] += count

    return {
        "dry_run": not apply,
        "older_than_days": older_than_days,
        "cutoff": _iso(cutoff),
        "batch_size": batch_size,
        "batches": batches,
        "truncated": truncated,
        "events_deleted": events_seen,
        "interactions_deleted": interactions_removed,
        "oldest": _iso(oldest),
        "newest": _iso(newest),
        "by_merchant": {key: dict(value) for key, value in sorted(by_merchant.items())},
    }


def _merchants_from_batch(
    rows: Sequence[Dict[str, Any]], interaction_ids: Sequence[str]
) -> Dict[str, int]:
    """Merchant attribution for interactions deleted in THIS batch.

    Their `commerce_interactions` row is already gone by the time this runs,
    so the merchant is read from the events that named them — the same
    merchant, since an interaction's events all carry its merchant_id.
    """
    wanted = set(interaction_ids)
    owner_by_interaction: Dict[str, str] = {}
    for row in rows:
        interaction_id = str(row.get("interaction_id") or "")
        if interaction_id in wanted and interaction_id not in owner_by_interaction:
            owner_by_interaction[interaction_id] = str(row.get("merchant_id") or "unknown")
    counts: Dict[str, int] = {}
    for merchant_id in owner_by_interaction.values():
        counts[merchant_id] = counts.get(merchant_id, 0) + 1
    return counts


async def _merchants_for_interactions(interaction_ids: Sequence[str]) -> Dict[str, int]:
    if not interaction_ids:
        return {}
    rows = await database.fetch_all(
        select(
            commerce_interactions.c.merchant_id,
            func.count().label("total"),
        )
        .where(commerce_interactions.c.interaction_id.in_(list(interaction_ids)))
        .group_by(commerce_interactions.c.merchant_id)
    )
    return {
        str(row._mapping["merchant_id"] or "unknown"): int(row._mapping["total"] or 0)
        for row in rows
    }


async def report_ledger_retention(*, horizon_days: int) -> Dict[str, Any]:
    """How much REAL commerce history sits behind a horizon. Reads only.

    PR-0.9 deliberately deletes no real row. This exists so the retention
    policy for real history can be decided against measured volume instead of
    a guess: per merchant, the events and interactions older than the horizon
    and the oldest `occurred_at` still on file.
    """
    horizon_days = max(0, int(horizon_days))
    cutoff = _utc_now() - timedelta(days=horizon_days)

    event_rows = await database.fetch_all(
        select(
            commerce_interaction_events.c.merchant_id,
            func.count().label("events"),
            func.min(commerce_interaction_events.c.occurred_at).label("oldest"),
        )
        .where(commerce_interaction_events.c.occurred_at < cutoff)
        .group_by(commerce_interaction_events.c.merchant_id)
    )
    # An interaction is "older than the horizon" when its LAST activity is.
    # `last_occurred_at` is nullable, so fall back to the row's created_at
    # rather than dropping such interactions from the count.
    interaction_age = func.coalesce(
        commerce_interactions.c.last_occurred_at, commerce_interactions.c.created_at
    )
    interaction_rows = await database.fetch_all(
        select(
            commerce_interactions.c.merchant_id,
            func.count().label("interactions"),
        )
        .where(interaction_age < cutoff)
        .group_by(commerce_interactions.c.merchant_id)
    )

    by_merchant: Dict[str, Dict[str, Any]] = {}
    for row in event_rows:
        mapping = row._mapping
        by_merchant.setdefault(
            str(mapping["merchant_id"] or "unknown"),
            {"events": 0, "interactions": 0, "oldest": None},
        ).update(
            {
                "events": int(mapping["events"] or 0),
                "oldest": _iso(mapping["oldest"]),
            }
        )
    for row in interaction_rows:
        mapping = row._mapping
        bucket = by_merchant.setdefault(
            str(mapping["merchant_id"] or "unknown"),
            {"events": 0, "interactions": 0, "oldest": None},
        )
        bucket["interactions"] = int(mapping["interactions"] or 0)

    return {
        "horizon_days": horizon_days,
        "cutoff": _iso(cutoff),
        "events_total": sum(int(value["events"]) for value in by_merchant.values()),
        "interactions_total": sum(int(value["interactions"]) for value in by_merchant.values()),
        "oldest": min(
            (value["oldest"] for value in by_merchant.values() if value["oldest"]),
            default=None,
        ),
        "by_merchant": {key: dict(value) for key, value in sorted(by_merchant.items())},
    }
