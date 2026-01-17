from __future__ import annotations

import time
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response

from db.database import database
from routes.reviews_invitation_issuer import _invitation_link_base_url

router = APIRouter(tags=["reviews-invitation-shortlink"])


def _build_target_url(*, invitation_token: str) -> str:
    base = _invitation_link_base_url().strip()
    if not base:
        raise HTTPException(status_code=503, detail="INVITATION_LINK_DISABLED")
    b = base.rstrip("#")
    t = (invitation_token or "").strip()
    if not t:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    # Put token in URL fragment to avoid leaking via Referer.
    return f"{b}#invitation_token={quote(t, safe='')}"


@router.get("/r/{code}")
async def resolve_invitation_shortlink(code: str, response: Response) -> Dict[str, Any]:
    """
    Resolve a short invitation link and redirect to the buyer review submission landing page.

    NOTE: token is placed in URL fragment so it isn't sent in HTTP requests or Referer headers.
    """
    c = (code or "").strip()
    if not c or len(c) > 64:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    row = await database.fetch_one(
        """
        SELECT invitation_token, EXTRACT(EPOCH FROM expires_at)::bigint AS exp
        FROM reviews_invitation_shortlinks
        WHERE code = :code
        LIMIT 1
        """,
        {"code": c},
    )
    if not row:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    exp = int(row.get("exp") or 0)
    if exp and int(time.time()) > exp:
        raise HTTPException(status_code=410, detail="LINK_EXPIRED")

    target = _build_target_url(invitation_token=str(row["invitation_token"]))
    response.status_code = 302
    response.headers["Location"] = target
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return {"status": "redirect"}

