import asyncio
import io
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from config.platform import is_production
from db.database import database
from routes.agent_auth import AgentContext, get_agent_context
from utils.logger import logger


router = APIRouter(prefix="/photos", tags=["photos"])

async def require_photos_admin(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = os.getenv("ADMIN_API_KEY") or os.getenv("PROMOTIONS_ADMIN_KEY")
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")

def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        val = (os.getenv(name) or "").strip()
        if val:
            return val
    return (default or "").strip()


PHOTO_UPLOAD_BUCKET = _first_env("PHOTO_UPLOAD_BUCKET", "S3_BUCKET", "AWS_S3_BUCKET", default="")
PHOTO_UPLOAD_PREFIX = _first_env("PHOTO_UPLOAD_PREFIX", default="selfies").strip().strip("/")
PHOTO_UPLOAD_REGION = _first_env("PHOTO_UPLOAD_REGION", "AWS_REGION", "AWS_DEFAULT_REGION", default="auto")
PHOTO_UPLOAD_ENDPOINT_URL = (
    _first_env("PHOTO_UPLOAD_ENDPOINT_URL", "AWS_ENDPOINT_URL", "S3_ENDPOINT_URL", default="") or None
)

PHOTO_PRESIGN_TTL_SECONDS = int(os.getenv("PHOTO_PRESIGN_TTL_SECONDS") or "900")
PHOTO_UPLOAD_TTL_HOURS = int(os.getenv("PHOTO_UPLOAD_TTL_HOURS") or "24")
PHOTO_UPLOAD_MAX_BYTES = int(os.getenv("PHOTO_UPLOAD_MAX_BYTES") or str(10 * 1024 * 1024))
PHOTO_DOWNLOAD_TTL_SECONDS = int(os.getenv("PHOTO_DOWNLOAD_TTL_SECONDS") or "300")

PHOTO_CLEANUP_LOOP_ENABLED = (os.getenv("PHOTO_CLEANUP_LOOP_ENABLED") or "").strip().lower() in {"1", "true", "yes", "y"}
PHOTO_CLEANUP_INTERVAL_SECONDS = int(os.getenv("PHOTO_CLEANUP_INTERVAL_SECONDS") or str(15 * 60))
PHOTO_CLEANUP_BATCH_SIZE = int(os.getenv("PHOTO_CLEANUP_BATCH_SIZE") or "200")
PHOTO_CLEANUP_STARTUP_DELAY_SECONDS = int(os.getenv("PHOTO_CLEANUP_STARTUP_DELAY_SECONDS") or "30")
PHOTO_SCHEMA_ENSURE_TIMEOUT_SECONDS = float(os.getenv("PHOTO_SCHEMA_ENSURE_TIMEOUT_SECONDS") or "2.0")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _is_production_env() -> bool:
    env = (os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or "").strip().lower()
    if env:
        return env in {"production", "prod"}
    return is_production()


PHOTO_SCHEMA_ENSURE_ON_REQUEST = _env_bool("PHOTO_SCHEMA_ENSURE_ON_REQUEST", default=not _is_production_env())

_photo_cleanup_task: Optional[asyncio.Task] = None
_photo_schema_ready = not PHOTO_SCHEMA_ENSURE_ON_REQUEST
_photo_schema_lock: Optional[asyncio.Lock] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_photo_schema_lock() -> asyncio.Lock:
    global _photo_schema_lock
    if _photo_schema_lock is None:
        _photo_schema_lock = asyncio.Lock()
    return _photo_schema_lock


async def _ensure_photo_uploads_table_inner() -> None:
    """
    Best-effort portable schema. We avoid JSONB so sqlite dev/test doesn't break.
    """
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_uploads (
          upload_id TEXT PRIMARY KEY,
          agent_id TEXT NULL,
          user_id TEXT NULL,
          consented BOOLEAN NOT NULL DEFAULT false,
          consented_at TIMESTAMP NULL,
          status TEXT NOT NULL DEFAULT 'created',
          qc_status TEXT NULL,
          qc_advice TEXT NULL,
          qc_details TEXT NULL,
          bucket TEXT NULL,
          object_key TEXT NULL,
          content_type TEXT NULL,
          byte_size INTEGER NULL,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          expires_at TIMESTAMP NULL,
          deleted_at TIMESTAMP NULL
        );
        """
    )
    try:
        await database.execute("CREATE INDEX IF NOT EXISTS idx_photo_uploads_expires_at ON photo_uploads(expires_at);")
    except Exception:
        pass
    try:
        await database.execute("CREATE INDEX IF NOT EXISTS idx_photo_uploads_status ON photo_uploads(status);")
    except Exception:
        pass


async def _ensure_photo_uploads_table() -> None:
    """
    Avoid running DDL in the production request hot path.

    Production deploys should create this table via migration/runbook. In dev and
    tests, the historical auto-ensure behavior remains available. When auto-ensure
    is enabled, it is one-shot and bounded so photo presign cannot hang behind a
    long DDL lock.
    """
    global _photo_schema_ready
    if _photo_schema_ready or not PHOTO_SCHEMA_ENSURE_ON_REQUEST:
        return
    async with _get_photo_schema_lock():
        if _photo_schema_ready:
            return
        try:
            await asyncio.wait_for(
                _ensure_photo_uploads_table_inner(),
                timeout=max(0.1, float(PHOTO_SCHEMA_ENSURE_TIMEOUT_SECONDS or 2.0)),
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503, detail="PHOTO_SCHEMA_ENSURE_TIMEOUT")
        _photo_schema_ready = True


def _s3_client():
    try:
        import boto3
        from botocore.client import Config
    except Exception:
        return None
    try:
        config_kwargs: Dict[str, Any] = {"signature_version": "s3v4"}
        endpoint_lc = (PHOTO_UPLOAD_ENDPOINT_URL or "").lower()
        if PHOTO_UPLOAD_ENDPOINT_URL:
            # Most S3-compatible providers (e.g. Cloudflare R2) expect path-style.
            config_kwargs["s3"] = {"addressing_style": "path"}

        access_key_id = _first_env("PHOTO_UPLOAD_ACCESS_KEY_ID", default="")
        secret_access_key = _first_env("PHOTO_UPLOAD_SECRET_ACCESS_KEY", default="")
        session_token = _first_env("PHOTO_UPLOAD_SESSION_TOKEN", default="") or None
        if PHOTO_UPLOAD_ENDPOINT_URL and session_token:
            # Cloudflare R2 does not accept session-token based signing.
            if "cloudflarestorage.com" in endpoint_lc or ".r2." in endpoint_lc:
                session_token = None

        # Fallback to global AWS creds ONLY for S3-compatible endpoints. This also
        # avoids accidentally inheriting AWS_SESSION_TOKEN, which some providers
        # (e.g. Cloudflare R2) reject.
        if PHOTO_UPLOAD_ENDPOINT_URL and not (access_key_id and secret_access_key):
            access_key_id = _first_env("AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY", default=access_key_id)
            secret_access_key = _first_env("AWS_SECRET_ACCESS_KEY", "AWS_SECRET_KEY", default=secret_access_key)

        region_name = PHOTO_UPLOAD_REGION or None
        if PHOTO_UPLOAD_ENDPOINT_URL and ("cloudflarestorage.com" in endpoint_lc or ".r2." in endpoint_lc):
            # Cloudflare R2 requires region='auto' (or one of their region codes).
            region_name = "auto"

        client_kwargs: Dict[str, Any] = {
            "region_name": region_name,
            "endpoint_url": PHOTO_UPLOAD_ENDPOINT_URL,
            "config": Config(**config_kwargs),
        }
        if access_key_id and secret_access_key:
            client_kwargs.update(
                {
                    "aws_access_key_id": access_key_id,
                    "aws_secret_access_key": secret_access_key,
                    **({"aws_session_token": session_token} if session_token else {}),
                }
            )

        return boto3.client(
            "s3",
            **client_kwargs,
        )
    except Exception:
        return None


def _required_setup_ok() -> bool:
    return bool(PHOTO_UPLOAD_BUCKET)


def _photo_upload_credentials_configured() -> bool:
    photo_access_key = _first_env("PHOTO_UPLOAD_ACCESS_KEY_ID", default="")
    photo_secret_key = _first_env("PHOTO_UPLOAD_SECRET_ACCESS_KEY", default="")
    if photo_access_key and photo_secret_key:
        return True

    aws_access_key = _first_env("AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY", default="")
    aws_secret_key = _first_env("AWS_SECRET_ACCESS_KEY", "AWS_SECRET_KEY", default="")
    if aws_access_key and aws_secret_key:
        return True

    if _first_env("AWS_WEB_IDENTITY_TOKEN_FILE", default="") and _first_env("AWS_ROLE_ARN", default=""):
        return True

    if _first_env("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", default="") or _first_env(
        "AWS_CONTAINER_CREDENTIALS_FULL_URI", default=""
    ):
        return True

    return False


def _object_key(upload_id: str, content_type: str) -> str:
    ext = ".jpg"
    ct = (content_type or "").lower()
    if "png" in ct:
        ext = ".png"
    elif "webp" in ct:
        ext = ".webp"
    elif "heic" in ct or "heif" in ct:
        ext = ".heic"
    date = _utcnow().strftime("%Y/%m/%d")
    return f"{PHOTO_UPLOAD_PREFIX}/{date}/{upload_id}{ext}"


def _lighting_tips() -> Dict[str, Any]:
    return {
        "daylight": [
            "Stand near a window with daylight (face the window).",
            "Avoid backlight (window behind you).",
        ],
        "indoor_white": [
            "Use a bright white light (avoid colored RGB lights).",
            "Face the main light source and keep it slightly above eye level.",
        ],
        "general": [
            "Clean the camera lens.",
            "Hold the phone steady and tap to focus on your face.",
        ],
    }


def _qc_advice(status: str) -> Dict[str, Any]:
    tips = _lighting_tips()
    if status == "passed":
        return {
            "summary": "Photo looks good.",
            "suggestions": [],
            "tips": tips,
            "retryable": False,
        }
    if status == "too_dark":
        return {
            "summary": "The photo is too dark.",
            "suggestions": [
                "Move to brighter light (daylight near a window is best).",
                "Avoid strong backlight.",
            ],
            "tips": tips,
            "retryable": True,
        }
    if status == "blurry":
        return {
            "summary": "The photo looks blurry.",
            "suggestions": [
                "Hold the phone steady and tap to focus on your face.",
                "Use a faster shutter by moving to brighter light.",
            ],
            "tips": tips,
            "retryable": True,
        }
    if status == "has_filter":
        return {
            "summary": "The photo seems to have heavy filters or color cast.",
            "suggestions": [
                "Turn off beauty filters and color filters.",
                "Use neutral lighting (daylight or indoor white light).",
            ],
            "tips": tips,
            "retryable": True,
        }
    return {
        "summary": "QC is pending.",
        "suggestions": ["Processing your photo…"],
        "tips": tips,
        "retryable": False,
    }


def _score_blur(gray: list[list[float]]) -> float:
    """
    Simple variance of 4-neighbor Laplacian on a small image.
    """
    h = len(gray)
    w = len(gray[0]) if h else 0
    if h < 3 or w < 3:
        return 0.0
    vals: list[float] = []
    for y in range(1, h - 1):
        row = gray[y]
        up = gray[y - 1]
        dn = gray[y + 1]
        for x in range(1, w - 1):
            c = row[x]
            lap = (up[x] + dn[x] + row[x - 1] + row[x + 1]) - 4.0 * c
            vals.append(lap)
    if not vals:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return var


def _qc_classify_image(blob: bytes) -> tuple[str, Dict[str, Any]]:
    try:
        from PIL import Image
    except Exception:
        return ("passed", {"note": "Pillow not installed; skipping QC"})

    img = Image.open(io.BytesIO(blob))
    img = img.convert("RGB")
    img_small = img.resize((128, 128))
    pixels = list(img_small.getdata())
    if not pixels:
        return ("blurry", {"reason": "empty_pixels"})

    # Brightness (luma)
    lumas = []
    mean_r = mean_g = mean_b = 0.0
    for r, g, b in pixels:
        mean_r += r
        mean_g += g
        mean_b += b
        lumas.append(0.2126 * r + 0.7152 * g + 0.0722 * b)
    n = float(len(pixels))
    mean_r /= n
    mean_g /= n
    mean_b /= n
    avg_luma = sum(lumas) / n

    # Blur score
    gray: list[list[float]] = []
    it = iter(pixels)
    for _y in range(128):
        row: list[float] = []
        for _x in range(128):
            r, g, b = next(it)
            row.append(0.299 * r + 0.587 * g + 0.114 * b)
        gray.append(row)
    blur_var = _score_blur(gray)

    # Simple "filter" heuristic: strong color cast + high saturation
    sat_sum = 0.0
    for r, g, b in pixels:
        mx = float(max(r, g, b))
        mn = float(min(r, g, b))
        if mx <= 1:
            continue
        sat_sum += (mx - mn) / mx
    avg_sat = sat_sum / n
    max_dev = max(abs(mean_r - mean_g), abs(mean_r - mean_b), abs(mean_g - mean_b))

    details = {
        "avg_luma": avg_luma,
        "blur_var": blur_var,
        "avg_sat": avg_sat,
        "mean_rgb": [mean_r, mean_g, mean_b],
    }

    # Priority: too_dark -> blurry -> has_filter -> passed
    if avg_luma < 60:
        return ("too_dark", details)
    if blur_var < 40:
        return ("blurry", details)
    if max_dev > 25 and avg_sat > 0.35:
        return ("has_filter", details)
    return ("passed", details)


async def _load_upload(upload_id: str) -> Optional[Dict[str, Any]]:
    await _ensure_photo_uploads_table()
    row = await database.fetch_one("SELECT * FROM photo_uploads WHERE upload_id = :id", {"id": upload_id})
    return dict(row) if row else None


async def _update_upload(upload_id: str, values: Dict[str, Any]) -> None:
    await _ensure_photo_uploads_table()
    set_clause = ", ".join([f"{k} = :{k}" for k in values.keys()])
    await database.execute(
        f"UPDATE photo_uploads SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE upload_id = :upload_id",
        {**values, "upload_id": upload_id},
    )


async def _delete_storage_object(bucket: str, key: str) -> None:
    client = _s3_client()
    if not client:
        return
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception:
        return


async def _cleanup_expired_uploads(*, limit: int, dry_run: bool) -> Dict[str, Any]:
    await _ensure_photo_uploads_table()
    now = _utcnow().replace(tzinfo=None)
    safe_limit = max(1, min(int(limit), 1000))

    rows = await database.fetch_all(
        f"""
        SELECT upload_id, bucket, object_key, status, expires_at
        FROM photo_uploads
        WHERE deleted_at IS NULL
          AND expires_at IS NOT NULL
          AND expires_at <= :now
        ORDER BY expires_at ASC
        LIMIT {safe_limit}
        """,
        {"now": now},
    )

    candidates = [dict(r) for r in (rows or [])]
    if dry_run:
        return {
            "status": "success",
            "dry_run": True,
            "candidates": len(candidates),
            "would_delete_upload_ids": [r.get("upload_id") for r in candidates if r.get("upload_id")],
        }

    deleted = 0
    storage_deletes_attempted = 0
    for row in candidates:
        upload_id = str(row.get("upload_id") or "")
        if not upload_id:
            continue
        bucket = str(row.get("bucket") or "")
        key = str(row.get("object_key") or "")
        if bucket and key:
            storage_deletes_attempted += 1
            await _delete_storage_object(bucket, key)
        await _update_upload(upload_id, {"deleted_at": now, "status": "deleted"})
        deleted += 1

    return {
        "status": "success",
        "dry_run": False,
        "candidates": len(candidates),
        "deleted": deleted,
        "storage_deletes_attempted": storage_deletes_attempted,
    }


async def _photo_cleanup_loop() -> None:
    await asyncio.sleep(max(0, PHOTO_CLEANUP_STARTUP_DELAY_SECONDS))
    while True:
        try:
            if getattr(database, "is_connected", False):
                result = await _cleanup_expired_uploads(limit=PHOTO_CLEANUP_BATCH_SIZE, dry_run=False)
                if result.get("deleted"):
                    logger.info(
                        "photo_cleanup_deleted=%s",
                        {
                            "deleted": result.get("deleted"),
                            "candidates": result.get("candidates"),
                            "storage_deletes_attempted": result.get("storage_deletes_attempted"),
                        },
                    )
        except Exception as exc:
            logger.warning(f"photo cleanup loop failed: {str(exc)[:200]}")
        await asyncio.sleep(max(30, PHOTO_CLEANUP_INTERVAL_SECONDS))


def start_photo_cleanup_loop() -> None:
    """
    Optional background cleanup loop.
    Enable by setting PHOTO_CLEANUP_LOOP_ENABLED=true.
    """
    global _photo_cleanup_task
    if not PHOTO_CLEANUP_LOOP_ENABLED:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _photo_cleanup_task and not _photo_cleanup_task.done():
        return
    # Fresh context => own `databases` Connection (issue #1754).
    from services.scheduler_job_runner import spawn_isolated
    _photo_cleanup_task = spawn_isolated(_photo_cleanup_loop(), name="photo_cleanup_loop")


async def _run_qc(upload_id: str) -> None:
    row = await _load_upload(upload_id)
    if not row:
        return
    if row.get("deleted_at"):
        return
    status = str(row.get("status") or "")
    if status not in {"uploaded", "qc_pending"}:
        return
    expires_at = row.get("expires_at")
    if expires_at:
        try:
            # DB may return str in some environments
            expires_at_dt = (
                datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if isinstance(expires_at, str)
                else expires_at
            )
            if expires_at_dt:
                if expires_at_dt.tzinfo is None:
                    expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
                if _utcnow() > expires_at_dt:
                    await _update_upload(upload_id, {"status": "expired"})
                    return
        except Exception:
            pass

    bucket = str(row.get("bucket") or "")
    key = str(row.get("object_key") or "")
    if not bucket or not key:
        await _update_upload(upload_id, {"status": "failed", "qc_status": "blurry", "qc_advice": "Missing object key"})
        return

    client = _s3_client()
    if not client:
        await _update_upload(upload_id, {"status": "failed", "qc_status": "blurry", "qc_advice": "Storage client unavailable"})
        return

    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        blob = obj["Body"].read()
    except Exception as e:
        await _update_upload(upload_id, {"status": "failed", "qc_status": "blurry", "qc_advice": f"Failed to read upload ({type(e).__name__})"})
        return

    qc_status, qc_details = _qc_classify_image(blob)
    advice = _qc_advice(qc_status)
    await _update_upload(
        upload_id,
        {
            "status": "qc_done",
            "qc_status": qc_status,
            "qc_advice": advice.get("summary"),
            "qc_details": json_dumps_safe(qc_details),
        },
    )


def json_dumps_safe(obj: Any) -> Optional[str]:
    try:
        import json

        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return None


class PhotoPresignRequest(BaseModel):
    content_type: str = Field(..., min_length=3, description="e.g. image/jpeg")
    file_name: Optional[str] = Field(None, description="Original filename (optional)")
    byte_size: Optional[int] = Field(None, ge=1, description="Client-reported size (optional)")
    consent: bool = Field(..., description="User explicitly consented to store the selfie for analysis")
    user_id: Optional[str] = Field(None, description="Optional user id for ownership/deletion")


class PhotoPresignResponse(BaseModel):
    upload_id: str
    upload: Dict[str, Any]
    expires_at: str
    max_bytes: int
    tips: Dict[str, Any]


@router.post("/presign", response_model=PhotoPresignResponse)
async def presign_photo_upload(
    body: PhotoPresignRequest,
    context: AgentContext = Depends(get_agent_context),
):
    if not _required_setup_ok():
        raise HTTPException(status_code=500, detail="PHOTO_UPLOAD_BUCKET_NOT_CONFIGURED")
    if not _photo_upload_credentials_configured():
        raise HTTPException(status_code=500, detail="STORAGE_CREDENTIALS_NOT_CONFIGURED")
    if not body.consent:
        raise HTTPException(status_code=400, detail="USER_CONSENT_REQUIRED")
    if not str(body.content_type or "").lower().startswith("image/"):
        raise HTTPException(status_code=400, detail="UNSUPPORTED_CONTENT_TYPE")
    if body.byte_size and int(body.byte_size) > PHOTO_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=400, detail="FILE_TOO_LARGE")

    await _ensure_photo_uploads_table()

    upload_id = f"upl_{uuid4().hex}"
    key = _object_key(upload_id, body.content_type)
    expires_at = _utcnow() + timedelta(hours=PHOTO_UPLOAD_TTL_HOURS)

    client = _s3_client()
    if not client:
        raise HTTPException(status_code=500, detail="STORAGE_CLIENT_UNAVAILABLE")

    try:
        # Cloudflare R2 does not support presigned POST (policy) uploads, so we use
        # presigned PUT URLs which work for both AWS S3 and R2.
        presigned_url = client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": PHOTO_UPLOAD_BUCKET,
                "Key": key,
                "ContentType": body.content_type,
            },
            ExpiresIn=PHOTO_PRESIGN_TTL_SECONDS,
        )
    except Exception as e:
        # Make upstream storage misconfig actionable without leaking secrets.
        err_type = type(e).__name__
        safe_msg = (str(e) or "").strip()
        if len(safe_msg) > 240:
            safe_msg = safe_msg[:240]

        detail: Dict[str, Any] = {
            "error": "PRESIGN_FAILED",
            "type": err_type,
            "message": safe_msg or None,
            "storage": {
                "bucket": PHOTO_UPLOAD_BUCKET,
                "region": PHOTO_UPLOAD_REGION or None,
                "endpoint_url_configured": bool(PHOTO_UPLOAD_ENDPOINT_URL),
            },
        }

        # Try to classify common botocore errors into clearer error strings.
        try:
            from botocore.exceptions import (  # type: ignore
                NoCredentialsError,
                PartialCredentialsError,
                ParamValidationError,
                NoRegionError,
                UnknownEndpointError,
            )

            if isinstance(e, NoCredentialsError):
                detail["error"] = "STORAGE_CREDENTIALS_NOT_CONFIGURED"
            elif isinstance(e, PartialCredentialsError):
                detail["error"] = "STORAGE_PARTIAL_CREDENTIALS"
            elif isinstance(e, NoRegionError):
                detail["error"] = "STORAGE_REGION_NOT_CONFIGURED"
            elif isinstance(e, UnknownEndpointError):
                detail["error"] = "STORAGE_UNKNOWN_ENDPOINT"
            elif isinstance(e, ParamValidationError):
                detail["error"] = "STORAGE_CONFIG_INVALID"
        except Exception:
            pass

        logger.error("photos.presign.failed", extra={"error": safe_msg, "type": err_type})
        raise HTTPException(status_code=500, detail=detail)

    await database.execute(
        """
        INSERT INTO photo_uploads (
          upload_id, agent_id, user_id, consented, consented_at,
          status, bucket, object_key, content_type, byte_size, expires_at
        ) VALUES (
          :upload_id, :agent_id, :user_id, true, CURRENT_TIMESTAMP,
          'created', :bucket, :object_key, :content_type, :byte_size, :expires_at
        )
        """,
        {
            "upload_id": upload_id,
            "agent_id": context.agent_id,
            "user_id": (body.user_id or "").strip() or None,
            "bucket": PHOTO_UPLOAD_BUCKET,
            "object_key": key,
            "content_type": body.content_type,
            "byte_size": int(body.byte_size) if body.byte_size else None,
            "expires_at": expires_at.replace(tzinfo=None),
        },
    )

    return PhotoPresignResponse(
        upload_id=upload_id,
        upload={
            "method": "PUT",
            "url": presigned_url,
            "headers": {"Content-Type": body.content_type},
            "fields": {},
        },
        expires_at=expires_at.isoformat(),
        max_bytes=PHOTO_UPLOAD_MAX_BYTES,
        tips=_lighting_tips(),
    )


class PhotoConfirmRequest(BaseModel):
    upload_id: str = Field(..., min_length=8)
    byte_size: Optional[int] = Field(None, ge=1)


class PhotoDownloadUrlRequest(BaseModel):
    upload_id: str = Field(..., min_length=8)


@router.post("/confirm")
async def confirm_photo_upload(
    body: PhotoConfirmRequest,
    background_tasks: BackgroundTasks,
    context: AgentContext = Depends(get_agent_context),
):
    row = await _load_upload(body.upload_id)
    if not row:
        raise HTTPException(status_code=404, detail="UPLOAD_NOT_FOUND")
    if row.get("agent_id") and str(row.get("agent_id")) != str(context.agent_id):
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    if row.get("deleted_at"):
        raise HTTPException(status_code=410, detail="UPLOAD_DELETED")

    # Best-effort HEAD to confirm object exists
    client = _s3_client()
    if not client:
        raise HTTPException(status_code=500, detail="STORAGE_CLIENT_UNAVAILABLE")
    try:
        client.head_object(Bucket=row.get("bucket"), Key=row.get("object_key"))
    except Exception:
        raise HTTPException(status_code=400, detail="OBJECT_NOT_FOUND")

    await _update_upload(
        body.upload_id,
        {
            "status": "qc_pending",
            **({"byte_size": int(body.byte_size)} if body.byte_size else {}),
        },
    )

    # Trigger QC (best-effort async). GET /photos/qc can also lazily compute if needed.
    try:
        asyncio.create_task(_run_qc(body.upload_id))
    except Exception:
        background_tasks.add_task(_run_qc, body.upload_id)

    return {
        "status": "success",
        "upload_id": body.upload_id,
        "qc_status": None,
        "qc": {"state": "pending", "qc_status": None, "advice": _qc_advice("pending")},
        "next_poll_ms": 1000,
    }


@router.get("/qc")
async def get_photo_qc(
    upload_id: str,
    context: AgentContext = Depends(get_agent_context),
):
    row = await _load_upload(upload_id)
    if not row:
        raise HTTPException(status_code=404, detail="UPLOAD_NOT_FOUND")
    if row.get("agent_id") and str(row.get("agent_id")) != str(context.agent_id):
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    if row.get("deleted_at"):
        raise HTTPException(status_code=410, detail="UPLOAD_DELETED")

    status = str(row.get("status") or "")
    qc_status = row.get("qc_status")
    if status in {"created"}:
        return {
            "status": "success",
            "upload_id": upload_id,
            "qc_status": None,
            "qc": {"state": "waiting_upload", "qc_status": None, "advice": _qc_advice("pending")},
            "next_poll_ms": 1000,
        }
    if status in {"qc_pending", "uploaded"} and not qc_status:
        # Lazy compute if still pending.
        try:
            await _update_upload(upload_id, {"status": "qc_pending"})
            await _run_qc(upload_id)
            row = await _load_upload(upload_id) or row
            status = str(row.get("status") or status)
            qc_status = row.get("qc_status") or qc_status
        except Exception:
            pass

    if status in {"expired"}:
        raise HTTPException(status_code=410, detail="UPLOAD_EXPIRED")

    if qc_status:
        advice = _qc_advice(str(qc_status))
        return {
            "status": "success",
            "upload_id": upload_id,
            "qc_status": qc_status,
            "qc": {"state": "done", "qc_status": qc_status, "advice": advice},
        }

    return {
        "status": "success",
        "upload_id": upload_id,
        "qc_status": None,
        "qc": {"state": "pending", "qc_status": None, "advice": _qc_advice("pending")},
        "next_poll_ms": 1000,
    }


async def _build_photo_download_url_response(upload_id: str, context: AgentContext) -> Dict[str, Any]:
    row = await _load_upload(upload_id)
    if not row:
        raise HTTPException(status_code=404, detail="UPLOAD_NOT_FOUND")
    if row.get("agent_id") and str(row.get("agent_id")) != str(context.agent_id):
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    if row.get("deleted_at"):
        raise HTTPException(status_code=410, detail="UPLOAD_DELETED")

    expires_at = row.get("expires_at")
    if expires_at:
        try:
            expires_at_dt = (
                datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if isinstance(expires_at, str)
                else expires_at
            )
            if expires_at_dt and expires_at_dt.tzinfo is None:
                expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
            if expires_at_dt and _utcnow() > expires_at_dt:
                await _update_upload(upload_id, {"status": "expired"})
                raise HTTPException(status_code=410, detail="UPLOAD_EXPIRED")
        except HTTPException:
            raise
        except Exception:
            pass

    bucket = str(row.get("bucket") or "")
    key = str(row.get("object_key") or "")
    if not bucket or not key:
        raise HTTPException(status_code=404, detail="OBJECT_NOT_FOUND")

    client = _s3_client()
    if not client:
        raise HTTPException(status_code=500, detail="STORAGE_CLIENT_UNAVAILABLE")
    try:
        download_url = client.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
            },
            ExpiresIn=PHOTO_DOWNLOAD_TTL_SECONDS,
        )
    except Exception as e:
        err_type = type(e).__name__
        safe_msg = (str(e) or "").strip()
        if len(safe_msg) > 240:
            safe_msg = safe_msg[:240]
        logger.error("photos.download_url.failed", extra={"error": safe_msg, "type": err_type})
        raise HTTPException(
            status_code=500,
            detail={
                "error": "DOWNLOAD_URL_FAILED",
                "type": err_type,
                "message": safe_msg or None,
                "storage": {
                    "bucket": bucket,
                    "endpoint_url_configured": bool(PHOTO_UPLOAD_ENDPOINT_URL),
                },
            },
        )

    return {
        "status": "success",
        "upload_id": upload_id,
        "download": {
            "method": "GET",
            "url": download_url,
            "headers": {},
            "expires_in_seconds": PHOTO_DOWNLOAD_TTL_SECONDS,
        },
        "content_type": row.get("content_type") or None,
    }


@router.get("/download-url")
async def get_photo_download_url(
    upload_id: str,
    context: AgentContext = Depends(get_agent_context),
):
    return await _build_photo_download_url_response(upload_id, context)


@router.post("/download-url")
async def post_photo_download_url(
    body: PhotoDownloadUrlRequest,
    context: AgentContext = Depends(get_agent_context),
):
    return await _build_photo_download_url_response(body.upload_id, context)


@router.delete("")
async def delete_photo_upload(
    upload_id: str,
    context: AgentContext = Depends(get_agent_context),
):
    row = await _load_upload(upload_id)
    if not row:
        raise HTTPException(status_code=404, detail="UPLOAD_NOT_FOUND")
    if row.get("agent_id") and str(row.get("agent_id")) != str(context.agent_id):
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    if row.get("deleted_at"):
        return {"status": "success", "upload_id": upload_id, "deleted": True}

    bucket = str(row.get("bucket") or "")
    key = str(row.get("object_key") or "")
    if bucket and key:
        await _delete_storage_object(bucket, key)

    await _update_upload(upload_id, {"deleted_at": _utcnow().replace(tzinfo=None), "status": "deleted"})
    return {"status": "success", "upload_id": upload_id, "deleted": True}


@router.post("/cleanup")
async def cleanup_photo_uploads(
    limit: int = Query(default=PHOTO_CLEANUP_BATCH_SIZE, ge=1, le=1000),
    dry_run: bool = Query(default=False),
    _: None = Depends(require_photos_admin),
):
    """
    Admin-only cleanup: delete expired photo uploads (DB row soft-delete + best-effort storage delete).

    Intended for a cron job (or to backstop the optional in-process cleanup loop).
    """
    return await _cleanup_expired_uploads(limit=limit, dry_run=dry_run)
