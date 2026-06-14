"""Data-driven creator directory (Phase 3 — the creator API data layer).

The creator matcher / scoring / action / UI already exist; the only gap is that
the directory is empty. This makes the directory a DB table that any source can
populate (BD curation, an external creator-discovery API, or a harvest) and the
matcher reads immediately. Category-indexed so a merchant category resolves to
creators. All reads/writes are best-effort: a missing table degrades to "no
directory creators" (the JSON matcher still works), never an error.

Returned rows match the matcher's expected shape (creator_id, display_name,
platform, platform_url, category_tags, audience_size_band, recent_coverage,
contact_method, contact_url, sample_brief_template), so creator_matcher can
score them alongside the JSON rows.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from db.database import database

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
    return []


async def upsert_creators(
    creators: Iterable[Dict[str, Any]],
    *,
    source: str = "bd_curated",
    db: Any = None,
) -> int:
    """Add/update creators in the directory. Each creator needs at least a
    creator_id + category_tags to be matchable. Returns the count upserted.
    Best-effort (stops quietly if the table is absent)."""
    write_db = db or database
    n = 0
    for c in creators or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("creator_id") or "").strip()
        cats = _as_list(c.get("category_tags"))
        if not cid or not cats:
            continue
        try:
            await write_db.execute(
                """
                INSERT INTO discovered_creators (
                    creator_id, display_name, platform, platform_url, category_tags,
                    audience_size_band, recent_coverage, contact_method, contact_url,
                    sample_brief_template, source, last_verified_at, updated_at
                ) VALUES (
                    :cid, :name, :platform, :url, CAST(:cats AS jsonb),
                    :band, CAST(:coverage AS jsonb), :cmethod, :curl,
                    :brief, :source, :verified, NOW()
                )
                ON CONFLICT (creator_id) DO UPDATE SET
                    display_name = COALESCE(EXCLUDED.display_name, discovered_creators.display_name),
                    platform = COALESCE(EXCLUDED.platform, discovered_creators.platform),
                    platform_url = COALESCE(EXCLUDED.platform_url, discovered_creators.platform_url),
                    category_tags = EXCLUDED.category_tags,
                    audience_size_band = COALESCE(EXCLUDED.audience_size_band, discovered_creators.audience_size_band),
                    recent_coverage = EXCLUDED.recent_coverage,
                    contact_method = COALESCE(EXCLUDED.contact_method, discovered_creators.contact_method),
                    contact_url = COALESCE(EXCLUDED.contact_url, discovered_creators.contact_url),
                    sample_brief_template = COALESCE(EXCLUDED.sample_brief_template, discovered_creators.sample_brief_template),
                    source = EXCLUDED.source,
                    last_verified_at = COALESCE(EXCLUDED.last_verified_at, discovered_creators.last_verified_at),
                    updated_at = NOW()
                """,
                {
                    "cid": cid,
                    "name": c.get("display_name"),
                    "platform": c.get("platform"),
                    "url": c.get("platform_url"),
                    "cats": json.dumps(cats),
                    "band": c.get("audience_size_band"),
                    "coverage": json.dumps(_as_list(c.get("recent_coverage"))),
                    "cmethod": c.get("contact_method"),
                    "curl": c.get("contact_url"),
                    "brief": c.get("sample_brief_template"),
                    "source": source,
                    "verified": c.get("last_verified_at"),
                },
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("creator_directory upsert failed for %r: %s", cid, str(exc)[:160])
            break  # table likely absent (pre-migration) — stop quietly
    return n


async def load_creators_for_category(
    category: Optional[str],
    *,
    db: Any = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Directory creators whose category_tags include `category`, in the matcher
    row shape. Empty list on any miss. Best-effort."""
    if not category or not str(category).strip():
        return []
    read_db = db or database
    try:
        rows = await read_db.fetch_all(
            """
            SELECT creator_id, display_name, platform, platform_url, category_tags,
                   audience_size_band, recent_coverage, contact_method, contact_url,
                   sample_brief_template
            FROM discovered_creators
            WHERE category_tags @> CAST(:cat AS jsonb)
            ORDER BY last_verified_at DESC NULLS LAST, updated_at DESC
            LIMIT :limit
            """,
            {"cat": json.dumps([str(category).strip().lower()]), "limit": int(limit)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("creator_directory read failed: %s", str(exc)[:160])
        return []

    def _coerce(v: Any) -> List[str]:
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:  # noqa: BLE001
                return []
        return _as_list(v)

    return [
        {
            "creator_id": r["creator_id"],
            "display_name": r["display_name"],
            "platform": r["platform"],
            "platform_url": r["platform_url"],
            "category_tags": _coerce(r["category_tags"]),
            "audience_size_band": r["audience_size_band"],
            "recent_coverage": _coerce(r["recent_coverage"]),
            "contact_method": r["contact_method"],
            "contact_url": r["contact_url"],
            "sample_brief_template": r["sample_brief_template"],
        }
        for r in rows
    ]
