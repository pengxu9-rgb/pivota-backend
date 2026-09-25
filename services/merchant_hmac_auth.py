from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Dict, Optional


INACTIVE_MERCHANT_STATUSES = {"deleted", "rejected"}


@dataclass(frozen=True)
class MerchantHMACAuthError(Exception):
    status_code: int
    detail: str


async def authenticate_hmac_merchant(
    *,
    raw_body: bytes,
    merchant_id: Optional[str],
    signature: Optional[str],
) -> Dict[str, Any]:
    """Authenticate a merchant-signed raw request body.

    Unknown merchants, missing credentials, missing API keys, and invalid signatures
    deliberately share the same 401 response so this endpoint cannot be used as a
    merchant-id oracle. Merchant lifecycle is checked only after key possession is
    proven.
    """
    from db.merchant_onboarding import get_merchant_onboarding

    if not merchant_id or not signature:
        raise MerchantHMACAuthError(
            status_code=401,
            detail="Missing X-Pivota-Merchant-Id or X-Pivota-Signature",
        )

    merchant = await get_merchant_onboarding(merchant_id)
    api_key = (merchant or {}).get("api_key")
    expected = (
        hmac.new(str(api_key).encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if api_key
        else None
    )
    if not expected or not hmac.compare_digest(str(signature), expected):
        raise MerchantHMACAuthError(status_code=401, detail="Invalid signature")

    if str((merchant or {}).get("status") or "").strip() in INACTIVE_MERCHANT_STATUSES:
        raise MerchantHMACAuthError(status_code=403, detail="Merchant is not active")

    return dict(merchant)
