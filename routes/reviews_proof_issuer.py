"""
Internal Reviews Proof Issuer (standalone service)

This router mints a short-lived proof token. The proof token is exchanged by pivota-backend
for a submission_token via:
  POST /buyer/reviews/v1/verification/exchange

Security:
- This endpoint is internal-only and protected by X-Internal-Key.
- Do not expose X-Internal-Key to browsers; call it server-side only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

router = APIRouter(prefix="/internal/reviews/v1", tags=["internal-reviews-proof"])


def _now_ts() -> int:
    return int(time.time())


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _canonical_json(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _proof_signing_secret() -> bytes:
    raw = (os.getenv("REVIEWS_BUYER_PROOF_SIGNING_SECRET") or "").strip()
    if not raw:
        raise HTTPException(status_code=503, detail="PROOF_SIGNING_SECRET_MISSING")
    return raw.encode("utf-8")


def _require_internal_key(x_internal_key: Optional[str]) -> None:
    expected = (os.getenv("REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="PROOF_ISSUER_DISABLED")
    got = (x_internal_key or "").strip()
    if not got or not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")


def _merchant_allowed(merchant_id: str) -> bool:
    raw = (os.getenv("REVIEWS_BUYER_SUBMIT_MERCHANT_ALLOWLIST") or "").strip()
    if not raw:
        return True
    allow = {x.strip() for x in raw.split(",") if x.strip()}
    return (merchant_id or "").strip() in allow


class SubjectRef(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    platform: str = Field(..., min_length=1)
    platform_product_id: str = Field(..., min_length=1)
    variant_id: Optional[str] = None


class IssueProofRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    subjects: List[SubjectRef] = Field(..., min_length=1)
    verification: str = Field("unverified", min_length=1)
    ttl_seconds: int = Field(600, ge=60, le=3600)


@router.post("/proof/issue")
async def issue_proof_token(
    body: IssueProofRequest,
    response: Response,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> Dict[str, Any]:
    _require_internal_key(x_internal_key)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"

    merchant_id = (body.merchant_id or "").strip()
    if not _merchant_allowed(merchant_id):
        raise HTTPException(status_code=403, detail="NOT_ALLOWED")

    now = _now_ts()
    ttl = max(60, min(int(body.ttl_seconds or 600), 3600))
    exp = now + ttl

    subjects: List[Dict[str, Any]] = []
    for s in body.subjects:
        mid = (s.merchant_id or "").strip()
        platform = (s.platform or "").strip().lower()
        pp = (s.platform_product_id or "").strip()
        vid = (str(s.variant_id).strip() if s.variant_id is not None else "").strip()
        if not mid or not platform or not pp:
            continue
        if mid != merchant_id:
            raise HTTPException(status_code=400, detail="SUBJECT_MERCHANT_MISMATCH")
        payload_subject: Dict[str, Any] = {
            "merchant_id": mid,
            "platform": platform,
            "platform_product_id": pp,
        }
        if vid:
            payload_subject["variant_id"] = vid
        subjects.append(payload_subject)
    if not subjects:
        raise HTTPException(status_code=400, detail="MISSING_SUBJECT")

    payload = {
        "v": 1,
        "exp": exp,
        "jti": _b64u(os.urandom(18)),
        "merchant_id": merchant_id,
        "verification": (body.verification or "unverified").strip().lower(),
        "subjects": subjects,
    }
    msg = _canonical_json(payload)
    sig = hmac.new(_proof_signing_secret(), msg, hashlib.sha256).digest()
    token = f"{_b64u(msg)}.{_b64u(sig)}"
    return {"status": "success", "expires_at": exp, "proof_token": token}

