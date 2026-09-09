import copy
import pytest
from services import consumer_capture_worker as worker
from services.consumer_capture_plan import build_plan


@pytest.fixture
def plan():
    return build_plan(product_keys=['sku'],queries=['best serum'],providers=['gemini'])


async def test_lease_rejection_prevents_real_client_invocation(monkeypatch,plan):
    async def reject(**kwargs): raise worker.CaptureCheckpointRejected('lost')
    async def unexpected(**kwargs): raise AssertionError('must not invoke')
    monkeypatch.setattr(worker,'save_checkpoint',reject)
    monkeypatch.setattr(worker.agent_center_llm_client,'probe',unexpected)
    with pytest.raises(worker.CaptureCheckpointRejected):
        await worker.capture_for_leased_run(run_id='r',merchant_id='m',worker_id='w',plan=plan)


async def test_adapter_persists_before_probe_and_passes_only_question(monkeypatch,plan):
    writes=[]; calls=[]
    async def save(**kw):
        if writes: assert kw['previous'] == writes[-1]['state']
        writes.append(copy.deepcopy(kw))
    async def probe(**kw):
        assert len(writes)==1
        calls.append(kw)
        return {'raw_runs':[]}
    monkeypatch.setattr(worker,'save_checkpoint',save)
    monkeypatch.setattr(worker.agent_center_llm_client,'probe',probe)
    state=await worker.capture_for_leased_run(run_id='r',merchant_id='m',worker_id='w',plan=plan)
    assert calls[0]['context']=={'queries':['best serum']}
    assert calls[0]['allow_local_mock'] is False
    assert len(writes)==2
    await worker.capture_for_leased_run(run_id='r',merchant_id='m',worker_id='w',plan=plan,retained=state)
    assert len(calls)==1


async def test_zero_row_checkpoint_is_rejected(monkeypatch,plan):
    async def ensure(): pass
    async def fetch(*args): return None
    monkeypatch.setattr(worker,'ensure_merchant_audit_runs_table',ensure)
    monkeypatch.setattr(worker.database,'fetch_one',fetch)
    with pytest.raises(worker.CaptureCheckpointRejected):
        await worker.save_checkpoint(run_id='r',merchant_id='m',worker_id='w',plan_sha256=plan['sha256'],previous=None,state={})
