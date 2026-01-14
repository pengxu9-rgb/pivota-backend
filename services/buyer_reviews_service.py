from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from fastapi import HTTPException, Request

from db.database import database
from db.reviews_center import buyer_review_idempotency_keys, buyer_review_ownership, buyer_review_submission_jtis, media_assets, product_reviews
from services.reviews_service import VARIANT_ID_SENTINEL, _reviews_media_s3_put, build_product_key, build_sku_key


def _now_ts() -> int:
    return int(time.time())


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode((s or "") + pad)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_16(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    if isinstance(row, Mapping):
        try:
            return row.get(key)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            return row[key]
        except Exception:
            return None
    try:
        return getattr(row, key)
    except Exception:
        return None


def _as_iso_datetime(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        try:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        except Exception:
            return None
    if isinstance(value, str):
        return value
    return None


def _signing_secret() -> bytes:
    raw = (os.getenv("REVIEWS_BUYER_SUBMIT_SIGNING_SECRET") or "").strip()
    if not raw:
        # Keep staging usable but discourage accidental prod use.
        raw = "dev-insecure-buyer-submit-secret"
    return raw.encode("utf-8")


def buyer_submit_enabled() -> bool:
    return (os.getenv("REVIEWS_BUYER_SUBMIT_ENABLED") or "").strip().lower() == "true"


def _new_media_public_id() -> str:
    # Keep consistent with import pipeline.
    return uuid4().hex


def _guess_media_type(filename: str, content_type: str) -> str:
    ct = (content_type or "").strip().lower()
    if ct.startswith("video/"):
        return "video"
    ext = (os.path.splitext(filename)[1] or "").lower()
    if ext in {".mp4", ".mov", ".webm"}:
        return "video"
    return "image"


def _is_allowed_content_type(content_type: str) -> bool:
    ct = (content_type or "").strip().lower()
    if not ct:
        return False
    if ct.startswith("image/"):
        return ct in {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if ct.startswith("video/"):
        return ct in {"video/mp4", "video/webm", "video/quicktime"}
    return False


async def attach_buyer_review_media(
    *,
    request: Request,
    token: str,
    review_id: int,
    filename: str,
    content_type: str,
    blob: bytes,
) -> Dict[str, Any]:
    if not buyer_submit_enabled():
        raise HTTPException(status_code=404, detail="BUYER_SUBMIT_DISABLED")

    verified = verify_submission_token(token)
    rid = int(review_id)

    owner = await database.fetch_one(buyer_review_ownership.select().where(buyer_review_ownership.c.review_id == rid))
    if not owner or str(owner["token_jti_hash"] or "") != verified.jti_hash:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    row = await database.fetch_one(product_reviews.select().where(product_reviews.c.id == rid))
    if not row:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    status = str(_row_get(row, "status") or "")
    if status not in {"under_review", "active"}:
        raise HTTPException(status_code=400, detail="REVIEW_STATUS_INVALID")

    name = (filename or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="MISSING_FILENAME")

    ct = (content_type or "").strip() or (mimetypes.guess_type(name)[0] or "").strip() or "application/octet-stream"
    if not _is_allowed_content_type(ct):
        raise HTTPException(status_code=400, detail="UNSUPPORTED_MEDIA_TYPE")

    max_bytes = int(os.getenv("REVIEWS_BUYER_MEDIA_MAX_BYTES") or "10485760")  # 10MB
    if max_bytes > 0 and len(blob) > max_bytes:
        raise HTTPException(status_code=413, detail="MEDIA_TOO_LARGE")

    public_id = _new_media_public_id()
    media_type = _guess_media_type(name, ct)
    url = f"/agent/shop/v1/review-media/{public_id}"
    file_hash = _sha256_hex(blob)

    s3_uri = _reviews_media_s3_put(public_id, filename=name, blob=blob, content_type=ct)
    if not s3_uri:
        raise HTTPException(status_code=503, detail="MEDIA_STORAGE_UNAVAILABLE")

    now_dt = datetime.now(timezone.utc)
    media_id = await database.execute(
        media_assets.insert().values(
            review_id=rid,
            type=media_type,
            public_id=public_id,
            url=url,
            file_path=s3_uri,
            file_hash=file_hash,
            status="active",
            created_at=now_dt,
        )
    )

    await database.execute(
        product_reviews.update()
        .where(product_reviews.c.id == rid)
        .values(media_count=(product_reviews.c.media_count + 1), updated_at=now_dt)
    )

    return {
        "status": "success",
        "review_id": rid,
        "media": {"id": int(media_id), "public_id": public_id, "type": media_type},
    }


def _issuer_key() -> str:
    return (os.getenv("REVIEWS_BUYER_SUBMIT_ISSUER_KEY") or "").strip()


def _require_issuer_key(request: Request) -> None:
    expected = _issuer_key()
    if not expected:
        raise HTTPException(status_code=503, detail="BUYER_SUBMIT_ISSUER_DISABLED")
    got = (request.headers.get("x-buyer-issuer-key") or "").strip()
    if not got or not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")


def _token_payload_canonical(payload: Dict[str, Any]) -> bytes:
    # Stable json encoding for signing (no whitespace, sorted keys).
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def issue_submission_token(
    *,
    request: Request,
    merchant_id: str,
    subjects: Sequence[Dict[str, Any]],
    verification: str,
    ttl_seconds: int,
) -> Dict[str, Any]:
    if not buyer_submit_enabled():
        raise HTTPException(status_code=404, detail="BUYER_SUBMIT_DISABLED")
    _require_issuer_key(request)

    ttl = max(60, min(int(ttl_seconds or 900), 3600))
    now = _now_ts()
    exp = now + ttl
    jti = _b64u_encode(os.urandom(16))

    normalized_subjects: List[Dict[str, Any]] = []
    for s in subjects or []:
        mid = (s.get("merchant_id") or "").strip()
        platform = (s.get("platform") or "").strip().lower()
        pp = (s.get("platform_product_id") or "").strip()
        vid = s.get("variant_id")
        vid = (str(vid).strip() if vid is not None else "")
        normalized_subjects.append(
            {
                "merchant_id": mid,
                "platform": platform,
                "platform_product_id": pp,
                "variant_id": vid,
            }
        )

    if not normalized_subjects:
        raise HTTPException(status_code=400, detail="MISSING_SUBJECTS")

    payload: Dict[str, Any] = {
        "v": 1,
        "exp": exp,
        "jti": jti,
        "merchant_id": (merchant_id or "").strip(),
        "verification": (verification or "unverified").strip().lower(),
        "subjects": normalized_subjects,
    }

    msg = _token_payload_canonical(payload)
    sig = _b64u_encode(hmac.new(_signing_secret(), msg, hashlib.sha256).digest())
    token = f"{_b64u_encode(msg)}.{sig}"

    return {"status": "success", "expires_at": exp, "submission_token": token}


@dataclass(frozen=True)
class VerifiedSubmission:
    exp: int
    jti: str
    merchant_id: str
    verification: str
    subjects: Tuple[Dict[str, Any], ...]

    @property
    def jti_hash(self) -> str:
        return _sha256_16(self.jti)


def verify_submission_token(token: str) -> VerifiedSubmission:
    if not token:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")
    try:
        b64_payload, b64_sig = token.split(".", 1)
    except Exception:
        raise HTTPException(status_code=401, detail="INVALID_TOKEN")

    try:
        msg = _b64u_decode(b64_payload)
        sig = _b64u_decode(b64_sig)
    except Exception:
        raise HTTPException(status_code=401, detail="INVALID_TOKEN")

    expected = hmac.new(_signing_secret(), msg, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="INVALID_TOKEN")

    try:
        payload = json.loads(msg.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="INVALID_TOKEN")

    exp = int(payload.get("exp") or 0)
    if exp <= _now_ts():
        raise HTTPException(status_code=401, detail="TOKEN_EXPIRED")

    jti = str(payload.get("jti") or "").strip()
    if not jti:
        raise HTTPException(status_code=401, detail="INVALID_TOKEN")

    merchant_id = str(payload.get("merchant_id") or "").strip()
    if not merchant_id:
        raise HTTPException(status_code=401, detail="INVALID_TOKEN")

    verification = str(payload.get("verification") or "unverified").strip().lower()
    subjects_raw = payload.get("subjects") or []
    if not isinstance(subjects_raw, list) or not subjects_raw:
        raise HTTPException(status_code=401, detail="INVALID_TOKEN")

    subjects: List[Dict[str, Any]] = []
    for s in subjects_raw:
        if not isinstance(s, dict):
            continue
        mid = str(s.get("merchant_id") or "").strip()
        platform = str(s.get("platform") or "").strip().lower()
        pp = str(s.get("platform_product_id") or "").strip()
        vid = str(s.get("variant_id") or "").strip()
        if not mid or not platform or not pp:
            continue
        subjects.append(
            {
                "merchant_id": mid,
                "platform": platform,
                "platform_product_id": pp,
                "variant_id": vid,
            }
        )
    if not subjects:
        raise HTTPException(status_code=401, detail="INVALID_TOKEN")

    return VerifiedSubmission(exp=exp, jti=jti, merchant_id=merchant_id, verification=verification, subjects=tuple(subjects))


def _client_ip_hash(request: Request) -> Optional[str]:
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    ip = (xff.split(",")[0].strip() if xff else "") or (request.client.host if request.client else "")
    if not ip:
        return None
    return _sha256_16(ip)


def _request_hash_for_idempotency(fields: Dict[str, Any]) -> str:
    canonical = json.dumps(fields, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _sha256_hex(canonical)


async def _claim_idempotency_key(
    *,
    merchant_id: str,
    idempotency_key: str,
    request_hash: str,
) -> Tuple[Optional[int], bool]:
    """
    Returns (existing_review_id, is_conflict).
    - existing_review_id set if key already exists and request_hash matches.
    - is_conflict true if key exists but request_hash differs.
    """
    key_hash = _sha256_hex(idempotency_key.encode("utf-8"))
    existing = await database.fetch_one(
        buyer_review_idempotency_keys.select().where(
            (buyer_review_idempotency_keys.c.merchant_id == merchant_id)
            & (buyer_review_idempotency_keys.c.idempotency_key_hash == key_hash)
        )
    )
    if existing:
        if str(existing["request_hash"] or "") != request_hash:
            return None, True
        rid = existing.get("review_id")
        return (int(rid) if rid is not None else None), False

    await database.execute(
        buyer_review_idempotency_keys.insert().values(
            merchant_id=merchant_id,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
            review_id=None,
            created_at=datetime.now(timezone.utc),
        )
    )
    return None, False


async def _bind_idempotency_to_review(
    *,
    merchant_id: str,
    idempotency_key: str,
    review_id: int,
) -> None:
    key_hash = _sha256_hex(idempotency_key.encode("utf-8"))
    await database.execute(
        buyer_review_idempotency_keys.update()
        .where(
            (buyer_review_idempotency_keys.c.merchant_id == merchant_id)
            & (buyer_review_idempotency_keys.c.idempotency_key_hash == key_hash)
        )
        .values(review_id=int(review_id))
    )


async def _consume_token_jti(*, merchant_id: str, jti_hash: str, exp: int) -> None:
    # Single-use token enforcement. Unique constraint on jti_hash prevents replay.
    try:
        await database.execute(
            buyer_review_submission_jtis.insert().values(
                merchant_id=merchant_id,
                jti_hash=jti_hash,
                expires_at=datetime.fromtimestamp(int(exp), tz=timezone.utc),
                created_at=datetime.now(timezone.utc),
            )
        )
    except Exception:
        raise HTTPException(status_code=409, detail="REPLAY_DETECTED")


def _subject_allowed(v: VerifiedSubmission, *, merchant_id: str, platform: str, platform_product_id: str, variant_id: Optional[str]) -> bool:
    want_vid = (str(variant_id).strip() if variant_id is not None else "").strip()
    for s in v.subjects:
        if s["merchant_id"] != merchant_id:
            continue
        if s["platform"] != (platform or "").strip().lower():
            continue
        if s["platform_product_id"] != platform_product_id:
            continue
        allowed_vid = (s.get("variant_id") or "").strip()
        # If token subject has blank variant_id, it allows product-level submission (any variant or none).
        if not allowed_vid:
            return True
        if allowed_vid == want_vid:
            return True
    return False


async def create_buyer_review(
    *,
    request: Request,
    token: str,
    idempotency_key: str,
    merchant_id: str,
    platform: str,
    platform_product_id: str,
    variant_id: Optional[str],
    rating: int,
    title: Optional[str],
    body: Optional[str],
) -> Dict[str, Any]:
    if not buyer_submit_enabled():
        raise HTTPException(status_code=404, detail="BUYER_SUBMIT_DISABLED")

    verified = verify_submission_token(token)

    if verified.merchant_id != merchant_id:
        raise HTTPException(status_code=403, detail="NOT_ALLOWED")

    p = (platform or "").strip().lower()
    pp = (platform_product_id or "").strip()
    vid = (str(variant_id).strip() if variant_id is not None else "").strip()
    if not p or not pp:
        raise HTTPException(status_code=400, detail="MISSING_SUBJECT")

    if not _subject_allowed(verified, merchant_id=merchant_id, platform=p, platform_product_id=pp, variant_id=vid):
        raise HTTPException(status_code=403, detail="NOT_ALLOWED")

    try:
        rating_int = int(rating)
    except Exception:
        raise HTTPException(status_code=400, detail="INVALID_RATING")
    if rating_int < 1 or rating_int > 5:
        raise HTTPException(status_code=400, detail="INVALID_RATING")

    if not idempotency_key:
        raise HTTPException(status_code=400, detail="MISSING_IDEMPOTENCY_KEY")

    # Idempotency: avoid duplicate writes on retries.
    req_hash = _request_hash_for_idempotency(
        {
            "merchant_id": merchant_id,
            "platform": p,
            "platform_product_id": pp,
            "variant_id": vid,
            "rating": rating_int,
            "title": (title or "").strip(),
            "body": (body or "").strip(),
        }
    )
    existing_review_id, is_conflict = await _claim_idempotency_key(
        merchant_id=merchant_id, idempotency_key=idempotency_key, request_hash=req_hash
    )
    if is_conflict:
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    if existing_review_id is not None:
        row = await database.fetch_one(product_reviews.select().where(product_reviews.c.id == int(existing_review_id)))
        if row:
            return {
                "status": "success",
                "review_id": int(existing_review_id),
                "moderation_state": str(_row_get(row, "status") or ""),
                "idempotent_replay": True,
            }

    # Replay prevention: token is single-use.
    await _consume_token_jti(merchant_id=merchant_id, jti_hash=verified.jti_hash, exp=verified.exp)

    product_key = build_product_key(merchant_id=merchant_id, platform=p, platform_product_id=pp)
    sku_key = build_sku_key(merchant_id=merchant_id, platform=p, platform_product_id=pp, variant_id=vid or None)

    # Persist review as under_review (not visible to PDP read path until employee sets active).
    now_dt = datetime.now(timezone.utc)
    review_id = await database.execute(
        product_reviews.insert().values(
            product_key=product_key,
            sku_key=sku_key,
            merchant_id=merchant_id,
            platform=p,
            platform_product_id=pp,
            variant_id=(vid if vid and vid != VARIANT_ID_SENTINEL else None),
            group_id=None,
            author_user_id=None,
            source_type="native",
            source_system="buyer",
            external_review_id=None,
            dedupe_key=_sha256_hex(f"{merchant_id}|{sku_key}|{verified.jti_hash}".encode("utf-8")),
            verification=(verified.verification or "unverified"),
            rating=rating_int,
            title=(title or "").strip() or None,
            body=(body or "").strip() or None,
            media_count=0,
            risk_flags={"source": "buyer", "ip_hash": _client_ip_hash(request)},
            status="under_review",
            created_at=now_dt,
            updated_at=now_dt,
        )
    )

    await database.execute(
        buyer_review_ownership.insert().values(
            review_id=int(review_id),
            token_jti_hash=verified.jti_hash,
            created_at=now_dt,
        )
    )

    await _bind_idempotency_to_review(merchant_id=merchant_id, idempotency_key=idempotency_key, review_id=int(review_id))

    return {"status": "success", "review_id": int(review_id), "moderation_state": "under_review"}


async def get_buyer_review_status(*, token: str, review_id: int) -> Dict[str, Any]:
    if not buyer_submit_enabled():
        raise HTTPException(status_code=404, detail="BUYER_SUBMIT_DISABLED")

    verified = verify_submission_token(token)
    rid = int(review_id)

    owner = await database.fetch_one(buyer_review_ownership.select().where(buyer_review_ownership.c.review_id == rid))
    if not owner:
        raise HTTPException(status_code=404, detail="NOT_FOUND")
    if str(owner["token_jti_hash"] or "") != verified.jti_hash:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    row = await database.fetch_one(product_reviews.select().where(product_reviews.c.id == rid))
    if not row:
        raise HTTPException(status_code=404, detail="NOT_FOUND")

    return {
        "status": "success",
        "review_id": rid,
        "moderation_state": str(_row_get(row, "status") or ""),
        "created_at": _as_iso_datetime(_row_get(row, "created_at")),
        "updated_at": _as_iso_datetime(_row_get(row, "updated_at")),
    }
