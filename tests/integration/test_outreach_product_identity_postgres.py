"""Product-scoped outreach persistence; isolated localhost database only."""
import os
from urllib.parse import urlparse
from uuid import uuid4
import pytest
from fastapi import HTTPException
pytestmark = pytest.mark.skipif(os.getenv('RUN_RECOVERY_POSTGRES') != '1', reason='local PostgreSQL required')

async def test_outreach_products_persist_replay_and_dismiss_independently(monkeypatch):
    from db.database import database
    from db import merchant_tasks as tasks
    from routes.merchant_audit_routes import _OutreachPitchBody, mark_outreach_pitch_sent, _TaskDismissBody, dismiss_merchant_task
    url = urlparse(os.environ['DATABASE_URL'])
    assert url.hostname in {'localhost', '127.0.0.1'} and url.path == '/recovery_contract_test'
    merchant = 'outreach-acceptance-' + uuid4().hex
    await database.connect()
    try:
        await tasks.ensure_merchant_tasks_table()
        await database.execute('ALTER TABLE merchant_tasks ADD COLUMN IF NOT EXISTS recovery_key TEXT NULL')
        async def mark(sku):
            return await mark_outreach_pitch_sent(_OutreachPitchBody(host='review.example',query='best mascara',sku_key=sku, audit_run_id=str(uuid4())), merchant_id=merchant)
        a, b = await mark('sku-a'), await mark('sku-b')
        assert a['task_id'] != b['task_id']
        assert (await mark('sku-b'))['task_id'] == b['task_id']
        rows = await database.fetch_all('SELECT * FROM merchant_tasks WHERE merchant_id=:m', {'m': merchant})
        assert len(rows) == 2
        assert len({r['recovery_key'] for r in rows}) == 2
        from services.task_queue_service import reverify_outreach_records
        report = {'authority_map': {'host_attribution_summary': {'endorsement_hosts': ['review.example']}},
                  'per_sku_reports': [{'sku_key': 'sku-b', 'run_facts': {'prompts': [{'query': 'best mascara', 'endorsed_by': ['review.example']}]}}]}
        from services.task_queue_service import _reconcile_dropped_pending_tasks
        assert await _reconcile_dropped_pending_tasks(merchant_id=merchant, audit_run_id=str(uuid4()), covered_product_keys={'sku-a','sku-b'}) == 0
        from services.audit_run_worker import _materialize_tasks_and_executors
        from services.executor_agents import dispatcher
        async def no_external_execution(context, *, agent_names=None):
            assert agent_names == dispatcher.URL_AUDIT_EXECUTORS
            return {'dispatched_count': 0}
        monkeypatch.setattr(dispatcher, 'dispatch_agents', no_external_execution)
        result = await _materialize_tasks_and_executors(merchant_id=merchant, run_id=str(uuid4()), brand_report=report,
            integration_state=None, dispatch_only=True, agent_names=dispatcher.URL_AUDIT_EXECUTORS)
        assert result['outreach_cited'] == 1 and result['tasks_materialized'] == 0
        assert (await tasks.fetch_task(task_id=a['task_id']))['status'] == 'pending'
        assert (await tasks.fetch_task(task_id=b['task_id']))['status'] == 'done'
        with pytest.raises(HTTPException) as exc:
            await dismiss_merchant_task(a['task_id'], _TaskDismissBody(reason='test cleanup'), merchant_id='another-merchant')
        assert exc.value.status_code == 404
        await dismiss_merchant_task(a['task_id'], _TaskDismissBody(reason='isolated acceptance'), merchant_id=merchant)
        assert (await tasks.fetch_task(task_id=a['task_id']))['status'] == 'dismissed'
        assert (await tasks.fetch_task(task_id=b['task_id']))['status'] == 'done'
    finally:
        await database.execute('DELETE FROM merchant_tasks WHERE merchant_id=:m', {'m': merchant})
        await database.disconnect()
