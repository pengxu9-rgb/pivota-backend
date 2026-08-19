"""
Minimal FastAPI app for the Reviews Proof Issuer service.

This is intended to be deployed as a separate Railway service using the same repo, without
pulling in the full pivota-backend monolith.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI

from config.platform import (
    commit_sha,
    deployment_id,
    git_branch,
    platform_metadata,
    raw_environment_label,
    require_platform_env,
    service_id,
)

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from routes.reviews_proof_issuer import router as reviews_proof_issuer_router

# FAIL AT BOOT, not per-request — the same assertion main.py makes in its
# lifespan. This is a SECOND FastAPI service deployed from this repo, so it
# gets the guarantee independently: config.platform fails CLOSED to
# "production" when it cannot resolve the environment on a managed host, and a
# service that came up on that guess is a service whose every prod/staging
# guard was decided by a guess. On Cloud Run without PIVOTA_ENV this raises
# instead. Local dev and tests (no K_SERVICE, no Railway deployment marker)
# resolve to "development" and pass straight through.
#
# It runs at import time rather than in a lifespan hook deliberately: this
# module has no lifespan, and a misconfigured revision should die before the
# ASGI server binds a port and starts passing health checks.
_RESOLVED_ENV = require_platform_env()

app = FastAPI(
    title="Reviews Proof Issuer",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(reviews_proof_issuer_router)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.get("/__build")
async def __build() -> Dict[str, Any]:
    return {
        "service": "reviews-proof-issuer",
        "timestamp": time.time(),
        "git": {
            "commit_sha": commit_sha() or "",
            "branch": git_branch() or "",
        },
        # Key kept as "railway" for the existing consumers of this probe; the
        # values resolve on Cloud Run too.
        "railway": {
            "environment": raw_environment_label() or "",
            "deployment_id": deployment_id() or "",
            "service_id": service_id() or "",
        },
        "platform": platform_metadata(),
    }

