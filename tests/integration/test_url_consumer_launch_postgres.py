"""Real debit/enqueue transaction, isolated local PostgreSQL only."""
import asyncio
from contextvars import Context
import os
from urllib.parse import urlparse
from uuid import uuid4
import pytest
pytestmark = pytest.mark.skipif(os.getenv('RUN_RECOVERY_POSTGRES') != '1', reason='local PostgreSQL required')

@pytest.mark.parametrize('scenario', ['concurrent', 'enqueue_failure', 'enqueue_exception', 'insufficient', 'terminal_replay'])
async def test_url_debit_and_enqueue_share_one_commit(monkeypatch, scenario):
    from db.database import database
    from db import merchant_audit_runs as runs
    from services import url_consumer_launch as launch
    from services import merchant_credit_balance_service as balances
    url=urlparse(os.environ['DATABASE_URL'])
    assert url.hostname in {'localhost','127.0.0.1'} and url.path=='/recovery_contract_test'
    merchant='url-atomic-'+uuid4().hex
    await database.connect()
    try:
        await runs.ensure_merchant_audit_runs_table()
        await database.execute('INSERT INTO merchant_onboarding (merchant_id) VALUES (:m)',{'m':merchant})
        await database.execute('INSERT INTO merchant_credit_balance (merchant_id, credits, purchased_credits) VALUES (:m, :n, :n)',{'m':merchant,'n':1 if scenario=='insufficient' else 100})
        async def no_allowance(*args, **kwargs): return None
        monkeypatch.setattr(balances,'apply_subscription_allowance',no_allowance)
        kwargs=dict(merchant_id=merchant,product_keys=['urlwedge:sku'],idempotency_key='same',request_options_jsonb={'launch':{}},credits=10,usd_cogs=0)
        real_enqueue=launch.enqueue_audit_run_with_replay
        if scenario.startswith('enqueue_'):
            async def fail(**kw):
                await real_enqueue(**kw)
                if scenario=='enqueue_exception': raise RuntimeError('after insert')
                return None,False
            monkeypatch.setattr(launch,'enqueue_audit_run_with_replay',fail)
            with pytest.raises((RuntimeError,launch.UrlLaunchUnavailable)):
                await launch.launch_quoted_url_audit(**kwargs)
        elif scenario=='insufficient':
            with pytest.raises(launch.UrlLaunchInsufficientCredits): await launch.launch_quoted_url_audit(**kwargs)
        elif scenario=='concurrent':
            result=await asyncio.wait_for(asyncio.gather(*(asyncio.create_task(launch.launch_quoted_url_audit(**kwargs),context=Context()) for _ in range(2))),10)
            assert result[0][0]==result[1][0]
            assert sorted(r[1] for r in result)==[False,True]
        else:
            run_id,_=await launch.launch_quoted_url_audit(**kwargs)
            await database.execute("UPDATE merchant_audit_runs SET stage='completed',status='succeeded' WHERE run_id=:id",{'id':run_id})
            assert await launch.launch_quoted_url_audit(**kwargs)==(run_id,True)
        expected=scenario in {'concurrent','terminal_replay'}
        row=await database.fetch_one('SELECT credits,purchased_credits FROM merchant_credit_balance WHERE merchant_id=:m',{'m':merchant})
        assert row['credits']==row['purchased_credits']==(90 if expected else 1 if scenario=='insufficient' else 100)
        count=await database.fetch_val('SELECT COUNT(*) FROM merchant_audit_runs WHERE merchant_id=:m',{'m':merchant})
        events=await database.fetch_val('SELECT COUNT(*) FROM agent_center_usage_events WHERE merchant_id=:m',{'m':merchant})
        assert count==events==int(expected)
        if expected:
            saved=await database.fetch_val("SELECT partial_result_jsonb #>> '{launch,debited,0,purchased_credits}' FROM merchant_audit_runs WHERE merchant_id=:m",{'m':merchant})
            assert saved=='10'
    finally:
        await database.execute('DELETE FROM merchant_audit_runs WHERE merchant_id=:m',{'m':merchant})
        await database.execute('DELETE FROM agent_center_usage_events WHERE merchant_id=:m',{'m':merchant})
        await database.execute('DELETE FROM merchant_onboarding WHERE merchant_id=:m',{'m':merchant})
        await database.disconnect()
