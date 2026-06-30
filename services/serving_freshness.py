"""Honest serving-freshness signal for agent-facing read surfaces.

The agent PDP serving projection (``agent_pdp_view``) bakes offers/prices into
the row at assembly time. Because every rebuild re-reads ``catalog_offers``, the
served price is never older than the row's ``refreshed_at`` -- so ``refreshed_at``
is a faithful *upper bound* on the age of the baked price. Until now the route
served ``refreshed_at`` raw and let agents guess; the TTLs that the catalog sync
writes alongside each price fact (``fresh_until``) were never read on the serve
path.

This module turns ``refreshed_at`` into an honest freshness block
(``observed_at`` / ``fresh_until`` / ``is_stale``) so an agent can decide whether
to trust a baked price or re-fetch -- without us ever *withholding* the data.

The TTL mirrors the catalog-sync price-fact ``fresh_until`` (``observed_at`` + 1h;
see ``services.catalog_sync_service``) so the serve-side staleness window matches
the write-side freshness window. Keep the two in sync if either moves.

The emitted block is shaped to be consumable by
``services.agent_center_bd_report_service._freshness_current`` (it reads
``fresh_until``), so a stale serving row reads as not-current there too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# Mirrors the catalog-sync price-fact TTL (fresh_until = observed_at + 1h).
PRICE_FRESHNESS_TTL = timedelta(hours=1)


def _as_naive_utc(value: Any) -> Optional[datetime]:
    """Coerce a datetime/ISO-string to naive UTC, or None if unparseable.

    The serving tables store naive UTC (asyncpg rejects aware datetimes for
    those columns), but tests and other callers may hand us aware datetimes or
    ISO strings -- normalize everything to naive UTC so comparisons are sound.
    """
    if value is None:
        return None
    dt = value
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def serving_freshness(
    refreshed_at: Any,
    *,
    ttl: timedelta = PRICE_FRESHNESS_TTL,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Honest freshness block for a baked serving-projection row.

    ``refreshed_at`` is when ``agent_pdp_view`` assembled the row (and thus
    re-read the offers it baked). Returns ``observed_at`` / ``fresh_until`` /
    ``is_stale`` / ``ttl_seconds``. A missing/unparseable ``refreshed_at`` is
    treated as stale -- we cannot vouch for its age.
    """
    observed = _as_naive_utc(refreshed_at)
    current = _as_naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    ttl_seconds = int(ttl.total_seconds())

    if observed is None:
        return {
            "observed_at": None,
            "fresh_until": None,
            "is_stale": True,
            "ttl_seconds": ttl_seconds,
        }

    fresh_until = observed + ttl
    return {
        "observed_at": observed.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "is_stale": current > fresh_until,
        "ttl_seconds": ttl_seconds,
    }
