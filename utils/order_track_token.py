from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional


TOKEN_PURPOSE = "otrk1"
DEFAULT_EXPIRES_IN_SECONDS = 90 * 24 * 60 * 60


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _base64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def _signing_secret() -> str:
    return (os.getenv("ORDER_TRACK_TOKEN_SECRET") or "").strip()


def _constant_time_equals(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(str(a or ""), str(b or ""))
    except Exception:
        return False


def mint_order_track_token(
    order_id: str,
    *,
    expires_in_seconds: int = DEFAULT_EXPIRES_IN_SECONDS,
) -> str:
    raw_order_id = str(order_id or "").strip()
    if not raw_order_id:
        raise ValueError("ORDER_ID_REQUIRED")

    secret = _signing_secret()
    if not secret:
        raise ValueError("ORDER_TRACK_TOKEN_SECRET_REQUIRED")

    now = int(time.time())
    payload: Dict[str, Any] = {
        "v": TOKEN_PURPOSE,
        "order_id": raw_order_id,
        "iat": now,
        "exp": now + int(expires_in_seconds),
    }
    payload_b64 = _base64url_encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    sig = _base64url_encode(
        hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    )
    return f"v1.{payload_b64}.{sig}"


def verify_order_track_token(token: str) -> Optional[str]:
    raw = str(token or "").strip()
    if not raw:
        return None

    secret = _signing_secret()
    if not secret:
        return None

    parts = raw.split(".")
    if len(parts) == 2:
        payload_b64, sig = parts
    elif len(parts) == 3 and parts[0] == "v1":
        payload_b64, sig = parts[1], parts[2]
    else:
        return None

    expected = _base64url_encode(
        hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    )
    if not _constant_time_equals(sig, expected):
        return None

    try:
        payload = json.loads(_base64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict) or payload.get("v") != TOKEN_PURPOSE:
        return None

    try:
        exp = int(payload.get("exp") or 0)
    except Exception:
        return None
    if not exp or int(time.time()) > exp:
        return None

    order_id = str(payload.get("order_id") or "").strip()
    return order_id or None
