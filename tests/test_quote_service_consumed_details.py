import pytest
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_load_consumed_quote_includes_order_id_in_details(monkeypatch):
    import services.quote_service as qs

    now = datetime.now(timezone.utc)

    async def fake_get_quote(quote_id: str):
        return {
            "quote_id": quote_id,
            "merchant_id": "m_1",
            "agent_id": None,
            "expires_at": now,
            "status": "consumed",
            "engine": "shopify_rest_checkout",
            "engine_ref": "ref",
            "request_fingerprint": "fp",
            "snapshot_json": {},
            "quote_hash_sha256": "h" * 64,
            "debug_id": "dbg",
            "consumed_order_id": "ORD_123",
        }

    async def fake_expire_quote_if_needed(quote_id: str) -> None:
        return None

    monkeypatch.setattr(qs, "get_quote", fake_get_quote)
    monkeypatch.setattr(qs, "expire_quote_if_needed", fake_expire_quote_if_needed)

    svc = qs.QuoteService()
    with pytest.raises(qs.QuoteError) as exc:
        await svc.load_active_quote_or_raise(quote_id="q_1")

    assert exc.value.code == "QUOTE_CONSUMED"
    assert exc.value.details.get("order_id") == "ORD_123"

