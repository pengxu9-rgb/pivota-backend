"""
BD Channel Graph — cross-merchant cited-host demand rollup (read-only).

Surfaces `services/host_recurrence.py` to the employee portal so BD can see, in
one ranked view, which third-party channels (editorial desks, review sites,
retailers, communities) the frontier models cite across Pivota's whole merchant
base — and therefore which channels are worth a relationship ("partner once,
unlock citations for N brands").

Routes (all `Depends(get_current_employee)` — this is cross-merchant intel, never
merchant-facing):

  GET /api/agent-center/bd/channel-graph
      ?limit=100&min_merchants=1&host_type=&provider=&since_days=&exclude_first_party=false
      -> { channels: [...], count, filters, caveat }

  GET /api/agent-center/bd/channel-graph/detail?host=forbes.com
      -> per-host drill-down (per-provider, per-merchant, sample queries)

Data-completeness caveat (returned in the payload so the UI shows it): rows exist
only for *depositable* audited products, so counts are "over depositable audits",
not total coverage. Do not quote them to a channel as the whole base.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from services.host_recurrence import channel_detail, channel_recurrence
from utils.auth import get_current_employee

router = APIRouter(prefix="/api/agent-center/bd", tags=["agent-center", "bd"])
logger = logging.getLogger(__name__)

_CAVEAT = (
    "Counts are over DEPOSITABLE audited products only (citation_observations is "
    "written for resolved content_keys), not total coverage. Treat as an internal "
    "demand signal — do not quote to a channel as the whole merchant base."
)


@router.get("/channel-graph")
async def get_channel_graph(
    limit: int = Query(100, ge=1, le=1000),
    min_merchants: int = Query(1, ge=1, le=10000),
    host_type: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    since_days: Optional[int] = Query(None, ge=1, le=3650),
    exclude_first_party: bool = Query(False),
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Channels ranked by cross-merchant demand (distinct_merchants) + model
    coverage. Read-only; best-effort (empty list if the table is absent)."""
    channels = await channel_recurrence(
        limit=limit,
        min_merchants=min_merchants,
        host_type=host_type,
        provider=provider,
        since_days=since_days,
        exclude_first_party=exclude_first_party,
    )
    return {
        "channels": channels,
        "count": len(channels),
        "filters": {
            "limit": limit,
            "min_merchants": min_merchants,
            "host_type": host_type,
            "provider": provider,
            "since_days": since_days,
            "exclude_first_party": exclude_first_party,
        },
        "caveat": _CAVEAT,
    }


@router.get("/channel-graph/detail")
async def get_channel_detail(
    host: str = Query(..., min_length=1),
    merchant_limit: int = Query(200, ge=1, le=2000),
    query_limit: int = Query(50, ge=1, le=500),
    current_user: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Per-host drill-down: per-provider coverage, per-merchant breakdown, and
    sample queries the host was cited for."""
    detail = await channel_detail(
        host,
        merchant_limit=merchant_limit,
        query_limit=query_limit,
    )
    detail["caveat"] = _CAVEAT
    return detail
