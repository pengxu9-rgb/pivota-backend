"""Admin intake for the data-driven creator directory (Phase 3 creator API).

The matcher reads `discovered_creators`; this route populates it. The directory
is source-agnostic by design — BD curation, an external creator-discovery API
sync, or a harvest all POST the same creator rows here. Idempotent upsert by
creator_id. Admin-gated.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import ADMIN_ROLES, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/creators", tags=["Admin Creators"])


class CreatorRow(BaseModel):
    creator_id: str  # stable slug, e.g. "tiktok:glowwithval"
    display_name: Optional[str] = None
    platform: Optional[str] = None
    platform_url: Optional[str] = None
    category_tags: List[str] = Field(default_factory=list)
    audience_size_band: Optional[str] = None
    recent_coverage: List[str] = Field(default_factory=list)
    contact_method: Optional[str] = None
    contact_url: Optional[str] = None
    sample_brief_template: Optional[str] = None
    last_verified_at: Optional[str] = None


class LoadCreatorsRequest(BaseModel):
    creators: List[CreatorRow]
    source: str = "bd_curated"  # bd_curated | external_api | harvest


@router.post("/load")
async def load_creators(
    body: LoadCreatorsRequest,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Bulk-upsert creators into the directory. The matcher picks them up
    immediately (category-indexed). Real creators only — no fabricated rows."""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")
    from services.creator_directory import upsert_creators

    rows = [c.model_dump() for c in body.creators]
    n = await upsert_creators(rows, source=body.source)
    return {"status": "success", "upserted": n, "source": body.source}
