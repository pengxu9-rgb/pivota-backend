"""
Provider-aware audit dashboard — the merchant-facing "wedge".

Read-only aggregation over what the operator already persisted:
  - scan snapshots (`citation_scan_runs`)  -> visibility split by assistant + trend
  - targets (`citation_targets`)           -> where you're absent, by channel
  - actions/outcomes (`citation_actions`)  -> the action funnel + confirmed lifts

These endpoints NEVER probe (viewing the dashboard must be free), so they're cheap
reads. Everything is scoped to one merchant_id.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)


def _coerce_jsonb(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            return _json.loads(value)
        except (ValueError, TypeError):
            return {}
    return {}


async def overview(*, merchant_id: str) -> Dict[str, Any]:
    """Latest scan snapshot per provider: visibility, channel mix, gap count.

    'Your AI visibility, split by assistant' — the headline view. Gemini and
    ChatGPT cite different sources, so each gets its own line + playbook hint.
    """
    rows = await database.fetch_all(
        """
        SELECT DISTINCT ON (provider)
            provider, runs, merchant_cited_runs, visibility_score,
            channel_mix_jsonb, gap_count, created_at
        FROM citation_scan_runs
        WHERE merchant_id = :mid
        ORDER BY provider, created_at DESC
        """,
        {"mid": merchant_id},
    )
    providers: Dict[str, Any] = {}
    for r in rows:
        d = dict(r)
        providers[d["provider"]] = {
            "visibility_score": d.get("visibility_score"),
            "runs": d.get("runs"),
            "merchant_cited_runs": d.get("merchant_cited_runs"),
            "channel_mix": _coerce_jsonb(d.get("channel_mix_jsonb")),
            "gap_count": d.get("gap_count"),
            "scanned_at": d.get("created_at"),
        }
    return {"providers": providers, "has_data": bool(providers)}


async def trend(*, merchant_id: str, provider: str, limit: int = 30) -> Dict[str, Any]:
    """Visibility over time for one assistant (chronological points)."""
    rows = await database.fetch_all(
        """
        SELECT visibility_score, gap_count, created_at
        FROM citation_scan_runs
        WHERE merchant_id = :mid AND provider = :p
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"mid": merchant_id, "p": provider, "limit": int(limit)},
    )
    points = [dict(r) for r in rows][::-1]  # oldest -> newest for charting
    return {"provider": provider, "points": points}


async def progress(*, merchant_id: str) -> Dict[str, Any]:
    """The action funnel + confirmed citation lifts — the value proof.

    funnel: count of actions by status (draft -> measuring -> confirmed/no_effect).
    confirmed: actions where a re-probe showed the assistant now cites the merchant.
    """
    funnel_rows = await database.fetch_all(
        "SELECT status, count(*) AS n FROM citation_actions "
        "WHERE merchant_id = :mid GROUP BY status",
        {"mid": merchant_id},
    )
    funnel = {dict(r)["status"]: dict(r)["n"] for r in funnel_rows}
    confirmed_rows = await database.fetch_all(
        """
        SELECT action_id, action_type, target_id, posted_url, updated_at
        FROM citation_actions
        WHERE merchant_id = :mid AND status = 'confirmed'
        ORDER BY updated_at DESC
        LIMIT 20
        """,
        {"mid": merchant_id},
    )
    confirmed = [dict(r) for r in confirmed_rows]
    confirmed_count = funnel.get("confirmed", 0)
    return {
        "funnel": funnel,
        "confirmed_count": confirmed_count,            # full count
        "confirmed": confirmed,                        # most-recent sample (<= 20)
        "confirmed_truncated": confirmed_count > len(confirmed),
    }
