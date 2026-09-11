"""Explicit opt-in dedicated local PostgreSQL: no production data accepted."""
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4
import pytest

pytestmark=pytest.mark.skipif(os.getenv('RUN_RECOVERY_POSTGRES')!='1',reason='dedicated local PostgreSQL required')


async def test_checkpoint_lease_plan_and_compare_swap_on_real_postgres():
    from db import merchant_audit_runs as runs
    from db.database import database
    from services.consumer_capture_worker import save_checkpoint, CaptureCheckpointRejected
    url=urlparse(os.environ['DATABASE_URL'])
    assert url.hostname in {'localhost','127.0.0.1'} and url.path=='/recovery_contract_test'
    run_id=str(uuid4()); now=datetime.now(timezone.utc)
    await database.connect()
    try:
        await runs.ensure_merchant_audit_runs_table()
        await database.execute(runs.merchant_audit_runs.insert().values(
            run_id=run_id,merchant_id='checkpoint-test',status='running',stage='probing',
            product_keys=['sku'],claimed_by_worker='owner',claimed_until=now+timedelta(minutes=5),
            partial_result_jsonb={'launch':{'consumer_capture_plan':{'sha256':'plan'}},'unrelated':{'keep':True}},
        ))
        base=dict(run_id=run_id,merchant_id='checkpoint-test',worker_id='owner',plan_sha256='plan')
        state={'plan_sha256':'plan','jobs':{'job':{'status':'started'}}}
        for override in ({'merchant_id':'other'},{'worker_id':'other'},{'plan_sha256':'other'}):
            with pytest.raises(CaptureCheckpointRejected):
                await save_checkpoint(**{**base,**override},previous=None,state=state)
        await save_checkpoint(**base,previous=None,state=state)
        row=await runs.fetch_audit_run_by_id(run_id=run_id)
        assert row['partial_result_jsonb']['unrelated']=={'keep':True}
        with pytest.raises(CaptureCheckpointRejected):
            await save_checkpoint(**base,previous=None,state=state)
        done={'plan_sha256':'plan','jobs':{'job':{'status':'completed'}}}
        await save_checkpoint(**base,previous=state,state=done)
        await database.execute(runs.merchant_audit_runs.update().where(runs.merchant_audit_runs.c.run_id==run_id).values(claimed_until=now-timedelta(seconds=1)))
        with pytest.raises(CaptureCheckpointRejected):
            await save_checkpoint(**base,previous=done,state=state)
    finally:
        await database.execute(runs.merchant_audit_runs.delete().where(runs.merchant_audit_runs.c.run_id==run_id))
        await database.disconnect()


@pytest.mark.parametrize('interrupt', [False, True])
async def test_concurrent_capture_and_interrupted_resume_do_not_repeat_provider(monkeypatch, interrupt):
    import asyncio
    from db import merchant_audit_runs as runs
    from db.database import database
    from services import consumer_capture_worker as worker
    from services.consumer_capture_plan import build_plan

    url = urlparse(os.environ['DATABASE_URL'])
    assert url.hostname in {'localhost', '127.0.0.1'} and url.path == '/recovery_contract_test'
    plan = build_plan(product_keys=['sku'], queries=['best serum'], providers=['chatgpt'])
    run_id = str(uuid4())
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs)
        entered.set()
        await release.wait()
        return {'raw_runs': []}

    monkeypatch.setattr(worker.agent_center_llm_client, 'probe', provider)
    await database.connect()
    task = None
    try:
        await runs.ensure_merchant_audit_runs_table()
        await database.execute(runs.merchant_audit_runs.insert().values(
            run_id=run_id, merchant_id='capture-race-test', status='running', stage='probing',
            product_keys=['sku'], claimed_by_worker='owner',
            claimed_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            partial_result_jsonb={'launch': {'consumer_capture_plan': plan}},
        ))
        args = dict(run_id=run_id, merchant_id='capture-race-test', worker_id='owner', plan=plan)
        task = asyncio.create_task(worker.capture_for_leased_run(**args))
        await asyncio.wait_for(entered.wait(), timeout=5)
        row = await runs.fetch_audit_run_by_id(run_id=run_id)
        retained = row['partial_result_jsonb']['consumer_capture']
        job_id = plan['jobs'][0]['id']
        assert retained['jobs'][job_id]['status'] == 'started'
        # A concurrent attempt with the stale checkpoint loses the SQL CAS.
        with pytest.raises(worker.CaptureCheckpointRejected):
            await worker.capture_for_leased_run(**args)
        if interrupt:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await asyncio.wait_for(task, timeout=5)
        row = await runs.fetch_audit_run_by_id(run_id=run_id)
        retained = row['partial_result_jsonb']['consumer_capture']
        assert retained['jobs'][job_id]['status'] == ('started' if interrupt else 'completed')
        resumed = await worker.capture_for_leased_run(**args, retained=retained)
        assert resumed == retained
        assert len(calls) == 1
        assert calls[0]['context']['consumer_execution_profile'] == 'openai_web_required_v2'
        assert calls[0]['allow_local_mock'] is False
        # Missing/interrupted evidence must not become a successful mention.
        observations = worker.observations_for_capture(plan, resumed, merchant_brand='Brand', merchant_host='brand.example')
        assert len(observations) == 1
        assert observations[0]["brand_mentioned"] is None
        assert observations[0]["status"] == "provider_failed"
    finally:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await database.execute(runs.merchant_audit_runs.delete().where(runs.merchant_audit_runs.c.run_id == run_id))
        await database.disconnect()
