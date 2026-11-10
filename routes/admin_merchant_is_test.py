"""Staff lever for the test-merchant fact (`merchant_onboarding.is_test`, migration 282).

    GET  /admin/merchants/{merchant_id}/is-test
         The stored value and the newest audit rows for it.

    POST /admin/merchants/{merchant_id}/is-test
         body: {is_test: bool, reason: string}
         Set or clear it. The change and its employee_audit_logs row are one transaction
         (services/merchant_is_test_flag). Setting the value it already has changes nothing.

Both are gated by `Depends(require_admin)`: an admin or super_admin JWT, so every audit row names
a person. No X-ADMIN-KEY path: a shared key would leave the audit row without an actor.

What the fact does is decided by its readers: services/test_merchant_policy excludes is_test
merchants from the agent merchant list, the protocol directory and the shop-gateway search lanes
only while MERCHANT_IS_TEST_FILTER_ENABLED is on (default off).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, field_validator

from services import merchant_is_test_flag as flag
from utils.auth import require_admin

# The guard is on the ROUTER (tests/test_unauthenticated_admin_ops_routes.py says why): a route
# added here later inherits it. Each handler also takes `require_admin` for the actor it audits;
# FastAPI resolves the dependency once per request.
router = APIRouter(
    prefix="/admin/merchants", tags=["admin-merchants"], dependencies=[Depends(require_admin)]
)
logger = logging.getLogger(__name__)

_MERCHANT_ID = Path(..., min_length=1, max_length=128)


class SetIsTestRequest(BaseModel):
    is_test: bool
    reason: str = Field(..., min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_has_text(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("reason must say why (at least 3 characters)")
        return value


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="merchant is_test is unavailable; retry shortly")


@router.get("/{merchant_id}/is-test")
async def get_merchant_is_test(
    merchant_id: str = _MERCHANT_ID,
    current_user: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    try:
        value = await flag.get_merchant_is_test(merchant_id)
        audit = await flag.list_is_test_audit(merchant_id)
    except flag.MerchantNotFound:
        raise HTTPException(status_code=404, detail="merchant not found")
    except Exception:
        logger.error("merchant is_test read failed", exc_info=True)
        raise _unavailable()
    return {"merchant_id": merchant_id, "is_test": value, "audit": audit}


@router.post("/{merchant_id}/is-test")
async def set_merchant_is_test(
    body: SetIsTestRequest,
    merchant_id: str = _MERCHANT_ID,
    current_user: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    try:
        return await flag.set_merchant_is_test(
            merchant_id=merchant_id, is_test=body.is_test, reason=body.reason, actor=current_user
        )
    except flag.MerchantNotFound:
        raise HTTPException(status_code=404, detail="merchant not found")
    except Exception:
        logger.error("merchant is_test write failed", exc_info=True)
        raise _unavailable()
