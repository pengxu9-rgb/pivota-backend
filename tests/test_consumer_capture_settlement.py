import pytest
from decimal import Decimal
from services.consumer_capture_plan import build_plan, quote_plan
from services.consumer_capture_settlement import refund_due


def case():
    plan = build_plan(product_keys=['sku'], queries=['a', 'b'], providers=['gemini', 'chatgpt'])
    quote = quote_plan(plan)
    launch = {'consumer_capture_plan': plan, 'consumer_capture_quote': quote,
              'debited': [{'kind': 'audit', 'amount': 100 + quote['credits'], 'purchased_credits': 5}]}
    rows = [{'product_key': j['product_key'], 'provider': j['provider'], 'query': j['query'],
             'status': 'answered', 'brand_mentioned': False} for j in plan['jobs']]
    return launch, {'consumer_selection_observations': rows}


def test_only_failed_jobs_refunded_at_frozen_price():
    launch, report = case()
    report['consumer_selection_observations'][0]['brand_mentioned'] = None
    quote = launch['consumer_capture_quote']
    expected = quote['job_credits'][launch['consumer_capture_plan']['jobs'][0]['id']]
    result = refund_due(launch, report)
    assert result['refunded_credits'] == expected
    assert result['charged_credits'] + expected == quote['credits']
    assert result['purchased_credits'] == min(expected, 5)


def test_all_missing_refunds_supplement_only_and_old_quote_not_repriced():
    launch, report = case()
    total = launch['consumer_capture_quote']['credits']
    assert refund_due(launch, {})['refunded_credits'] == total
    del launch['consumer_capture_quote']['job_credits']
    assert refund_due(launch, report)['refunded_credits'] == 0
    report['consumer_selection_observations'].pop()
    assert refund_due(launch, report)['refunded_credits'] == total


def test_invalid_quote_cannot_credit_more_than_debited():
    launch, report = case()
    launch['debited'][0]['amount'] = 0
    with pytest.raises(ValueError):
        refund_due(launch, report)


def test_refund_preserves_fractional_carryover_source():
    launch, _ = case()
    launch['debited'][0]['purchased_credits'] = '0.7'
    assert refund_due(launch, {})['purchased_credits'] == Decimal('0.7')
