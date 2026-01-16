from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response


router = APIRouter(tags=["metrics"])


def _metrics_enabled() -> bool:
    return os.getenv("PROMETHEUS_METRICS_ENABLED", "true").lower() == "true"


def _require_token(request: Request) -> None:
    token = os.getenv("METRICS_BEARER_TOKEN")
    if not token:
        # No token configured: assume metrics endpoint is protected by network policy.
        return
    auth = request.headers.get("authorization") or ""
    if auth.strip() != f"Bearer {token}":
        raise HTTPException(status_code=403, detail="METRICS_FORBIDDEN")


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """
    Prometheus scrape endpoint.

    Security:
    - Controlled by PROMETHEUS_METRICS_ENABLED (default true).
    - If METRICS_BEARER_TOKEN is set, requires Authorization: Bearer <token>.
    - Otherwise, rely on internal network / ingress protection.
    """
    if not _metrics_enabled():
        raise HTTPException(status_code=404, detail="METRICS_DISABLED")
    _require_token(request)

    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # type: ignore

        data: bytes = generate_latest()
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)
    except ImportError:
        raise HTTPException(status_code=500, detail="PROMETHEUS_CLIENT_NOT_INSTALLED")

