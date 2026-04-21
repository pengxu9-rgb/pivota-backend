from __future__ import annotations

import pytest
from fastapi import HTTPException

from models.quote import QuotePreviewRequest
from routes import quote_routes
from utils.database_readiness import DatabaseUnavailableError


class _Context:
    agent_id = "agent_test"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id == "m_test"


@pytest.mark.asyncio
async def test_preview_quote_returns_retryable_503_when_database_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ensure_database_ready() -> None:
        raise DatabaseUnavailableError(
            phase="connect",
            error_type="TimeoutError",
            message="database connect failed",
        )

    monkeypatch.setattr(quote_routes, "ensure_database_ready", fake_ensure_database_ready)

    request = QuotePreviewRequest(
        merchant_id="m_test",
        items=[{"product_id": "p1", "variant_id": "v1", "quantity": 1}],
    )

    with pytest.raises(HTTPException) as exc_info:
        await quote_routes.preview_quote(request, context=_Context())

    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "1"}
    assert exc_info.value.detail == {
        "error": "TEMPORARY_UNAVAILABLE",
        "message": "Temporary database unavailable. Please retry shortly.",
    }
