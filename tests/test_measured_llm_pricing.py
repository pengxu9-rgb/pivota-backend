from datetime import datetime, timezone
from decimal import Decimal

import pytest

from services.measured_llm_pricing import credits_from_actual_cost, price_deepseek_response


def response():
    return {"model": "deepseek-flash", "usage": {
        "prompt_tokens": 2000, "completion_tokens": 500,
        "prompt_cache_hit_tokens": 1000, "prompt_cache_miss_tokens": 1000,
        "total_tokens": 2500,
    }}


def test_fractional_cost_accumulates_without_one_credit_minimum():
    price = credits_from_actual_cost(Decimal("0.000420"))
    assert price == Decimal("0.06720000")
    assert sum(price for _ in range(100)) == Decimal("6.72000000")


@pytest.mark.parametrize("hour,peak", [(0, False), (1, True), (3, True), (4, False), (6, True), (9, True), (10, False)])
def test_weekday_price_boundaries_and_cache_usage(hour, peak):
    receipt = price_deepseek_response(response(), requested_at=datetime(2026, 9, 11, hour, tzinfo=timezone.utc))
    assert receipt["price_window"] == ("peak" if peak else "off_peak")
    assert Decimal(receipt["usd_cogs"]) == Decimal("0.000906" if peak else "0.000453")
    assert Decimal(receipt["credits"]) == Decimal("0.14496" if peak else "0.07248")


def test_weekend_is_off_peak():
    assert price_deepseek_response(response(), requested_at=datetime(2026, 9, 12, 2, tzinfo=timezone.utc))["price_window"] == "off_peak"


@pytest.mark.parametrize("field,value", [("prompt_tokens", None), ("completion_tokens", -1), ("prompt_cache_hit_tokens", True), ("prompt_cache_miss_tokens", 999), ("total_tokens", 2499)])
def test_missing_or_inconsistent_usage_cannot_be_billed(field, value):
    payload = response()
    payload["usage"][field] = value
    with pytest.raises(ValueError):
        price_deepseek_response(payload, requested_at=datetime.now(timezone.utc))


def test_unknown_model_cannot_silently_use_another_models_price():
    payload = response()
    payload["model"] = "deepseek-chat"
    with pytest.raises(ValueError, match="No verified"):
        price_deepseek_response(payload, requested_at=datetime.now(timezone.utc))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.01"])
def test_invalid_actual_cost_is_rejected(value):
    with pytest.raises(ValueError):
        credits_from_actual_cost(Decimal(value))


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['valid', 'empty', 'truncated', 'missing_usage'])
async def test_generation_uses_measured_provider_receipt(monkeypatch, case):
    from services import merchant_measured_generation as generation
    from services.llm_providers import deepseek_probe
    from config.settings import settings
    import json
    payload = response()
    payload['choices'] = [{'finish_reason': 'length' if case == 'truncated' else 'stop',
                           'message': {'content': json.dumps({'answer': '' if case == 'empty' else 'Verified answer'})}}]
    if case == 'missing_usage':
        payload.pop('usage')
    async def call(**kw):
        assert kw['model'] == 'deepseek-flash'
        assert kw['enable_web_search'] is False
        return payload
    monkeypatch.setattr(settings, 'deepseek_api_key', 'test-key')
    monkeypatch.setattr(deepseek_probe, '_call_deepseek_chat', call)
    if case == 'valid':
        result, receipt = await generation.generate_measured_text(system_prompt='report only', user_message='Question')
        assert result['answer'] == 'Verified answer'
        assert Decimal(receipt['credits']) < 1
        assert receipt['cache_hit_tokens'] == 1000
    else:
        with pytest.raises(ValueError):
            await generation.generate_measured_text(system_prompt='report only', user_message='Question')
