"""Opt-in local PostgreSQL with production credit migrations applied."""
import asyncio
from contextvars import Context
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4
import pytest

pytestmark = pytest.mark.skipif(os.getenv('RUN_RECOVERY_POSTGRES') != '1', reason='dedicated local PostgreSQL required')


@pytest.mark.parametrize('scenario', ['concurrent', 'rollback', 'cancelled', 'expired', 'wrong_owner'])
async def test_real_ledger_and_completion_are_atomic(monkeypatch, scenario):
    from db.database import database
    from db import merchant_audit_runs as runs
    from services.consumer_capture_plan import build_plan, quote_plan
    from services import consumer_capture_settlement as settlement
    url = urlparse(os.environ['DATABASE_URL'])
    assert url.hostname in {'localhost', '127.0.0.1'} and url.path == '/recovery_contract_test'
    merchant, run_id = 'settlement-' + uuid4().hex, str(uuid4())
    plan = build_plan(product_keys=['sku'], queries=['q'], providers=['chatgpt'])
    quote = quote_plan(plan)
    launch = {'consumer_capture_plan': plan, 'consumer_capture_quote': quote,
              'debited': [{'kind': 'audit', 'amount': 100 + quote['credits'], 'purchased_credits': 3}]}
    now = datetime.now(timezone.utc)
    await database.connect()
    try:
        await runs.ensure_merchant_audit_runs_table()
        await database.execute('INSERT INTO merchant_onboarding (merchant_id) VALUES (:m)', {'m': merchant})
        await database.execute('INSERT INTO merchant_credit_balance (merchant_id, credits, purchased_credits) VALUES (:m, 100, 0)', {'m': merchant})
        await database.execute(runs.merchant_audit_runs.insert().values(
            run_id=run_id, merchant_id=merchant, stage='verifying', status='running', product_keys=['sku'],
            claimed_by_worker='owner', claimed_until=now+timedelta(minutes=-1 if scenario=='expired' else 5),
            cancelled_at=now if scenario=='cancelled' else None, partial_result_jsonb=json.loads(json.dumps({'launch': launch}, default=str))))
        args = dict(run_id=run_id, merchant_id=merchant, worker_id='other' if scenario=='wrong_owner' else 'owner',
                    launch=launch, report={}, cost_summary={})
        if scenario == 'rollback':
            real_credit = settlement.credit
            async def fail_after_credit(*a, **kw):
                await real_credit(*a, **kw)
                raise RuntimeError('failure after ledger update')
            monkeypatch.setattr(settlement, 'credit', fail_after_credit)
            with pytest.raises(RuntimeError, match="failure after ledger update"):
                await settlement.complete_with_refund(**args)
        elif scenario == 'concurrent':
            results = await asyncio.wait_for(asyncio.gather(*(asyncio.create_task(settlement.complete_with_refund(**args), context=Context()) for _ in range(2)), return_exceptions=True), timeout=10)
            assert results.count(True) == results.count(False) == 1, results
            assert await settlement.complete_with_refund(**args) is False
        else:
            assert await settlement.complete_with_refund(**args) is False
        balance = await database.fetch_one('SELECT credits, purchased_credits FROM merchant_credit_balance WHERE merchant_id=:m', {'m': merchant})
        row = await runs.fetch_audit_run_by_id(run_id=run_id)
        events = await database.fetch_val('SELECT COUNT(*) FROM agent_center_usage_events WHERE merchant_id=:m', {'m': merchant})
        success = scenario == 'concurrent'
        assert balance['credits'] == 100 + (quote['credits'] if success else 0)
        assert balance['purchased_credits'] == (min(3, quote['credits']) if success else 0)
        assert row['stage'] == ('completed' if success else 'verifying')
        assert events == (1 if success else 0)
    finally:
        await database.execute(runs.merchant_audit_runs.delete().where(runs.merchant_audit_runs.c.run_id == run_id))
        await database.execute('DELETE FROM agent_center_usage_events WHERE merchant_id=:m', {'m': merchant})
        await database.execute('DELETE FROM merchant_onboarding WHERE merchant_id=:m', {'m': merchant})
        await database.disconnect()
