"""Lease-guarded adapter for frozen supplemental consumer capture.

Launch/billing wiring remains separate: this adapter requires a matching plan
already persisted in the run's launch payload. Never calls debit itself.
"""
import json
from db.database import database
from db.merchant_audit_runs import ensure_merchant_audit_runs_table
from services import agent_center_llm_client
from services.consumer_capture_plan import execute_plan, validate_plan


class CaptureCheckpointRejected(RuntimeError):
    pass


async def save_checkpoint(*, run_id, merchant_id, worker_id, plan_sha256, previous, state):
    await ensure_merchant_audit_runs_table()
    row = await database.fetch_one("""
        UPDATE merchant_audit_runs
           SET partial_result_jsonb = COALESCE(partial_result_jsonb, '{}'::jsonb)
                                     || jsonb_build_object('consumer_capture', CAST(:state AS JSONB)),
               stage_updated_at = CURRENT_TIMESTAMP
         WHERE run_id = :run_id AND merchant_id = :merchant_id
           AND claimed_by_worker = :worker_id
           AND claimed_until > CURRENT_TIMESTAMP
           AND cancelled_at IS NULL AND stage = 'probing'
           AND partial_result_jsonb #>> '{launch,consumer_capture_plan,sha256}' = :plan_sha256
           AND (partial_result_jsonb -> 'consumer_capture') IS NOT DISTINCT FROM CAST(:previous AS JSONB)
        RETURNING partial_result_jsonb -> 'consumer_capture' AS checkpoint
    """, {'run_id':run_id, 'merchant_id':merchant_id, 'worker_id':worker_id,
            'plan_sha256':plan_sha256, 'previous':None if previous is None else json.dumps(previous),
            'state':json.dumps(state)})
    if row is None:
        raise CaptureCheckpointRejected('Consumer capture lease, plan or checkpoint changed')
    stored = row['checkpoint']
    if isinstance(stored, str):
        stored = json.loads(stored)
    if stored != state:
        raise CaptureCheckpointRejected('Consumer capture checkpoint readback mismatch')


async def capture_for_leased_run(*, run_id, merchant_id, worker_id, plan, retained=None):
    validate_plan(plan)
    previous = retained
    async def checkpoint(state):
        nonlocal previous
        await save_checkpoint(run_id=run_id, merchant_id=merchant_id, worker_id=worker_id,
                              plan_sha256=plan['sha256'], previous=previous, state=state)
        previous = state

    async def probe(job):
        # The target identity remains metadata and never enters the prompt.
        return await agent_center_llm_client.probe(
            scan_mode='consumer_answer_test', scan_target_id=f"{run_id}:consumer:{job['id']}",
            merchant_id=merchant_id, store_id=f'{merchant_id}_audit',
            context={'queries':[job['query']]}, provider=job['provider'], max_runs=1,
            allow_local_mock=False,
        )
    return await execute_plan(plan, retained=retained, checkpoint=checkpoint, probe=probe)
