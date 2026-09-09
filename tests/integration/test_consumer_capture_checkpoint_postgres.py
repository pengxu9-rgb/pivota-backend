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
