"""PR-5: per-channel + per-stage funnel event persistence.

Append-only table — every meaningful interaction with a Pivota-tracked
product gets a row. Aggregation queries read from here for stage-level
conversion rates per channel.

Lifecycle:
  - record_funnel_event(merchant_id, source_channel, stage, ...)
    → inserts a row with a fresh UUID. Best-effort: DB failure logs
    + degrades silently. Funnel events are diagnostic data; losing one
    must NOT block the user-facing API call that triggered it.

Read helpers live in services/funnel_analytics.py — separation of
concerns: db/ knows the schema; services/ does the rollup math.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db._jsonb_safe import _json_safe
from db.database import database, metadata

logger = logging.getLogger(__name__)


# Constants — kept here so callers + tests can validate against the
# same source of truth.
SOURCE_CHANNELS = frozenset({
    "ai_grounded_search",
    "ai_agent",
    "social_own",
    "social_kol",
    "editorial",
    "seo_organic",
    "retail",
    "direct",
    "unknown",
})

STAGES = frozenset({
    "impression",
    "profile_visit",
    "click",
    "pdp_view",
    "add_to_cart",
    "conversion",
})


funnel_events = Table(
    "funnel_events",
    metadata,
    Column("event_id", UUID(as_uuid=False), primary_key=True),
    Column("merchant_id", Text, nullable=False),
    Column("product_key", Text, nullable=True),
    Column("source_channel", Text, nullable=False),
    Column("stage", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("attribution_jsonb", JSONB, nullable=True),
    Index("idx_funnel_events_rollup", "merchant_id", "source_channel", "stage", "occurred_at"),
    Index("idx_funnel_events_merchant_time", "merchant_id", "occurred_at"),
    Index("idx_funnel_events_product", "merchant_id", "product_key", "occurred_at"),
    extend_existing=True,
)


_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS funnel_events (
      event_id          UUID PRIMARY KEY,
      merchant_id       TEXT NOT NULL,
      product_key       TEXT NULL,
      source_channel    TEXT NOT NULL,
      stage             TEXT NOT NULL,
      occurred_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      attribution_jsonb JSONB NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_funnel_events_rollup "
    "ON funnel_events (merchant_id, source_channel, stage, occurred_at);",
    "CREATE INDEX IF NOT EXISTS idx_funnel_events_merchant_time "
    "ON funnel_events (merchant_id, occurred_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_funnel_events_product "
    "ON funnel_events (merchant_id, product_key, occurred_at DESC) "
    "WHERE product_key IS NOT NULL;",
]


async def ensure_funnel_events_table() -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        try:
            for stmt in _DDL_STATEMENTS:
                await database.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ensure_funnel_events_table failed (best-effort): %s",
                str(exc)[:200],
            )
            return
        _DDL_READY = True


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def record_funnel_event(
    *,
    merchant_id: str,
    source_channel: str,
    stage: str,
    product_key: Optional[str] = None,
    attribution: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Insert a funnel_events row. Returns the event_id (UUID string)
    or None on persistence failure.

    Best-effort: DB failure logs at INFO level (high-volume — don't
    spam WARNING) and returns None. Caller MUST treat None as
    non-fatal — the user-facing API call that triggered this event
    has already succeeded; funnel persistence is observability, not
    critical path.

    Validates source_channel + stage against the documented enums.
    Invalid values are coerced to safe defaults (source_channel →
    'unknown', stage → 'impression') with a WARNING log so dashboards
    flag the bad caller.
    """
    if not merchant_id:
        return None  # silent: records without a merchant are useless
    if source_channel not in SOURCE_CHANNELS:
        logger.warning(
            "record_funnel_event: unknown source_channel=%r; coercing to 'unknown'",
            source_channel,
        )
        source_channel = "unknown"
    if stage not in STAGES:
        logger.warning(
            "record_funnel_event: unknown stage=%r; coercing to 'impression'",
            stage,
        )
        stage = "impression"

    await ensure_funnel_events_table()
    event_id = str(uuid.uuid4())
    try:
        await database.execute(
            funnel_events.insert().values(
                event_id=event_id,
                merchant_id=merchant_id,
                product_key=product_key,
                source_channel=source_channel,
                stage=stage,
                occurred_at=_now_utc(),
                # JSONB write boundary — coerce UUID/datetime/Decimal.
                attribution_jsonb=_json_safe(attribution) if attribution else attribution,
            )
        )
        return event_id
    except Exception as exc:  # noqa: BLE001
        # INFO not WARNING: this writer is high-volume; logging at
        # WARNING would noise-flood Railway when the table doesn't
        # exist yet (early in deploy).
        logger.info(
            "record_funnel_event failed for merchant=%s channel=%s stage=%s: %s",
            merchant_id, source_channel, stage, str(exc)[:200],
        )
        return None
