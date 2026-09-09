import copy
import pytest
from services.consumer_capture_plan import build_plan, quote_plan, execute_plan


def plan():
    return build_plan(product_keys=['sku'],queries=['best serum','Anua alternative'],providers=['gemini','chatgpt'])


def test_quote_counts_exact_frozen_jobs_not_diagnostic_sampling_fraction():
    result=plan(); quote=quote_plan(result)
    assert len(result['jobs']) == quote['probe_count'] == 4
    assert quote['plan_sha256'] == result['sha256']
    assert quote['credits'] > 0
    assert len({job['id'] for job in result['jobs']}) == 4


async def test_checkpoint_precedes_call_and_completed_work_is_not_repeated():
    saved=[]; called=[]
    async def checkpoint(state): saved.append(copy.deepcopy(state))
    async def probe(job):
        assert saved[-1]['jobs'][job['id']]['status'] == 'started'
        called.append(job['id'])
        return {'raw_runs':[]}
    result=await execute_plan(plan(),retained=None,checkpoint=checkpoint,probe=probe)
    await execute_plan(plan(),retained=result,checkpoint=checkpoint,probe=probe)
    assert len(called) == 4


async def test_ambiguous_started_call_is_not_replayed():
    frozen=plan(); called=[]
    retained={'plan_sha256':frozen['sha256'],'jobs':{job['id']:{'status':'started'} for job in frozen['jobs']}}
    async def checkpoint(state): pass
    async def probe(job): called.append(job)
    result=await execute_plan(frozen,retained=retained,checkpoint=checkpoint,probe=probe)
    assert called == []
    assert all(row['status']=='started' for row in result['jobs'].values())


async def test_checkpoint_failure_prevents_provider_call():
    called=[]
    async def checkpoint(state): raise RuntimeError('lease lost')
    async def probe(job): called.append(job)
    with pytest.raises(RuntimeError):
        await execute_plan(plan(),retained=None,checkpoint=checkpoint,probe=probe)
    assert called == []


async def test_changed_plan_is_rejected_before_any_work():
    async def unexpected(*args): raise AssertionError('must not run')
    with pytest.raises(ValueError):
        await execute_plan(plan(),retained={'plan_sha256':'other'},checkpoint=unexpected,probe=unexpected)


def test_mutated_plan_cannot_be_quoted():
    frozen=plan();frozen['jobs'][0]['query']='changed after quote'
    with pytest.raises(ValueError): quote_plan(frozen)


async def test_mutated_plan_cannot_execute():
    frozen=plan();frozen['jobs'].pop()
    async def unexpected(*args): raise AssertionError('must not run')
    with pytest.raises(ValueError):
        await execute_plan(frozen,retained=None,checkpoint=unexpected,probe=unexpected)


def test_unverified_provider_rejected_before_launch_quote(monkeypatch):
    from services.consumer_capture_plan import plan_for_launch
    monkeypatch.setenv('PIVOTA_CONSUMER_ANSWER_ENABLED','true')
    monkeypatch.delenv('PIVOTA_CONSUMER_ANSWER_PROVIDERS',raising=False)
    with pytest.raises(ValueError, match='not available'):
        plan_for_launch(product_keys=['sku'],queries=['best serum'],providers=['claude'])
    assert plan_for_launch(product_keys=['sku'],queries=['best serum'],providers=['gemini'])
