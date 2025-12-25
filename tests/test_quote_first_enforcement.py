import pytest


@pytest.mark.asyncio
async def test_quote_first_global_flag_requires_quote(monkeypatch):
    import services.quote_first_enforcement as qfe

    monkeypatch.setattr(qfe, "ENABLE_QUOTE_FIRST_ORDER_CREATE", True)
    monkeypatch.setattr(qfe, "ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT", False)

    require, ctx = await qfe.should_require_quote_for_order_create(merchant_id="m_1")
    assert require is True
    assert ctx["mode"] == "global"


@pytest.mark.asyncio
async def test_quote_first_tiered_allowlist_requires_quote(monkeypatch):
    import services.quote_first_enforcement as qfe

    async def fake_tier(*, merchant_id: str):
        return "L0"

    monkeypatch.setattr(qfe, "ENABLE_QUOTE_FIRST_ORDER_CREATE", False)
    monkeypatch.setattr(qfe, "ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT", True)
    monkeypatch.setattr(qfe, "get_merchant_pcs_tier", fake_tier)
    monkeypatch.setenv("FF_QUOTE_FIRST_REQUIRED_MERCHANT_IDS", "m_1,m_2")

    require, ctx = await qfe.should_require_quote_for_order_create(merchant_id="m_2")
    assert require is True
    assert ctx["mode"] == "allowlist"


@pytest.mark.asyncio
async def test_quote_first_tier_threshold_requires_quote(monkeypatch):
    import services.quote_first_enforcement as qfe

    async def fake_tier(*, merchant_id: str):
        return "L2"

    monkeypatch.setattr(qfe, "ENABLE_QUOTE_FIRST_ORDER_CREATE", False)
    monkeypatch.setattr(qfe, "ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT", True)
    monkeypatch.setattr(qfe, "get_merchant_pcs_tier", fake_tier)
    monkeypatch.setenv("FF_QUOTE_FIRST_REQUIRED_MERCHANT_IDS", "")
    monkeypatch.setenv("FF_QUOTE_FIRST_MIN_TIER", "L1C")

    require, ctx = await qfe.should_require_quote_for_order_create(merchant_id="m_9")
    assert require is True
    assert ctx["mode"] == "tiered"
    assert ctx["tier"] == "L2"
    assert ctx["min_tier"] == "L1C"

