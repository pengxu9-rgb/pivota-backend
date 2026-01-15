"""
Minimal FastAPI app for the Reviews Proof Issuer service.

This is intended to be deployed as a separate Railway service using the same repo, without
pulling in the full pivota-backend monolith.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from routes.reviews_proof_issuer import router as reviews_proof_issuer_router

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
            "commit_sha": os.getenv("RAILWAY_GIT_COMMIT_SHA") or "",
            "branch": os.getenv("RAILWAY_GIT_BRANCH") or "",
        },
        "railway": {
            "environment": os.getenv("RAILWAY_ENVIRONMENT_NAME") or "",
            "deployment_id": os.getenv("RAILWAY_DEPLOYMENT_ID") or "",
            "service_id": os.getenv("RAILWAY_SERVICE_ID") or "",
        },
    }

