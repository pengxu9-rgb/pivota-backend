"""URL worker must settle frozen supplemental quotes before completion."""
from unittest.mock import AsyncMock

import pytest
from tests.test_audit_worker_post_processing_failure import _make_worker_env
from services.consumer_capture_plan import build_plan, quote_plan
from services.consumer_capture_settlement import refund_due


@pytest.mark.parametrize('failed', [False, True])
async def test_url_worker_uses_atomic_settlement(monkeypatch, failed):
    worker, mar, transitions, _ = _make_worker_env(monkeypatch=monkeypatch)
    from services import consumer_capture_worker as capture
    from services import consumer_capture_settlement as settlement
    plan = build_plan(product_keys=['sku'], queries=['mascara'], providers=['gemini'])
    quote = quote_plan(plan)
    launch = {'synthetic_products': [{'sku_key': 'sku', 'pdp_url': 'https://example.com/p'}],
              'consumer_capture_plan': plan, 'consumer_capture_quote': quote,
              'debited': [{'kind': 'audit', 'amount': quote['credits'], 'purchased_credits': 0}]}
    monkeypatch.setattr(worker, '_resolve_synthetic_url_products', AsyncMock(return_value=(
        'Brand', 'example.com', [{'product_key': 'sku'}], [], None)))
    monkeypatch.setattr(worker, '_seed_url_audit_index', AsyncMock())
    monkeypatch.setattr(worker, '_submit_url_audit_seed_canonical_urls', AsyncMock())
    monkeypatch.setattr(worker, '_persist_url_recovery', AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(mar, 'recent_runs_for_merchant', AsyncMock(return_value=[]))
    monkeypatch.setattr(capture, 'capture_for_leased_run', AsyncMock(return_value={'jobs': {}}))
    rows = [{'product_key': 'sku', 'provider': 'gemini', 'query': 'mascara',
             'status': 'failed' if failed else 'answered', 'brand_mentioned': None if failed else False}]
    monkeypatch.setattr(capture, 'observations_for_capture', lambda *a, **kw: rows)
    complete = AsyncMock(return_value=True)
    monkeypatch.setattr(settlement, 'complete_with_refund', complete)
    result = await worker._process_one_audit_run_inner(
        run_id='url-run', merchant_id='m1', product_keys=['sku'], current_stage=mar.STAGE_QUEUED,
        brand_report=None, products=[], pivota_url_used=[], merchant_name='Brand',
        merchant_domain='example.com', integration_state=None, launch_options=launch)
    assert result is True
    complete.assert_awaited_once()
    args = complete.call_args.kwargs
    assert args['run_id'] == 'url-run' and args['launch'] == launch
    assert args['report']['catalog_dimensions_available'] is False
    assert refund_due(args['launch'], args['report'])['refunded_credits'] == (quote['credits'] if failed else 0)
    assert not any(t.get('to_stage') in {mar.STAGE_COMPLETED, mar.STAGE_FAILED} for t in transitions)
