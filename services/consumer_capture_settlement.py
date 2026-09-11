"""Complete a consumer audit and refund undelivered supplemental work atomically."""
import json
from db.database import database
from services.consumer_capture_plan import validate_plan
from services.merchant_credit_balance_service import credit


def refund_due(launch, report):
    plan = launch.get('consumer_capture_plan')
    validate_plan(plan)
    quote = launch.get('consumer_capture_quote') or {}
    total = quote.get('credits')
    if quote.get('plan_sha256') != plan['sha256'] or type(total) is not int or total < 0:
        raise ValueError('Missing frozen consumer quote')
    observations = report.get('consumer_selection_observations') or []
    by_id = {}
    for row in observations:
        by_id.setdefault((row.get('product_key'), row.get('provider'), row.get('query')), []).append(row)
    failed = []
    for job in plan['jobs']:
        rows = by_id.get((job['product_key'], job['provider'], job['query']), [])
        if len(rows) != 1 or rows[0].get('status') != 'answered' or type(rows[0].get('brand_mentioned')) is not bool:
            failed.append(job['id'])
    allocations = quote.get('job_credits')
    if allocations is None:
        # Old queued quotes cannot safely be repriced at current rates.
        amount = total if failed else 0
    else:
        if set(allocations) != {j['id'] for j in plan['jobs']} or any(type(v) is not int or v < 0 for v in allocations.values()) or sum(allocations.values()) != total:
            raise ValueError('Invalid frozen consumer credit allocation')
        amount = sum(allocations[j] for j in failed)
    debit = next((d for d in launch.get('debited', []) if d.get('kind') == 'audit'), None)
    if not debit or int(debit['amount']) < total:
        raise ValueError('Consumer refund exceeds launch debit')
    return {'quoted_credits': total, 'refunded_credits': amount,
            'charged_credits': total - amount, 'failed_jobs': failed,
            'purchased_credits': min(amount, int(debit.get('purchased_credits') or 0))}


async def complete_with_refund(*, run_id, merchant_id, worker_id, launch, report, cost_summary):
    settlement = refund_due(launch, report)
    summary = {**(cost_summary or {}), 'consumer_settlement': settlement}
    # A cancellation, reaper or second worker must win/lose the same run lock.
    # Any credit failure rolls back completion; the existing whole-run failure
    # path can then refund the original debit without a partial double refund.
    async with database.transaction():
        row = await database.fetch_one('''
            UPDATE merchant_audit_runs
               SET stage='completed', status='succeeded', completed_at=CURRENT_TIMESTAMP,
                   stage_updated_at=CURRENT_TIMESTAMP, cost_summary_jsonb=CAST(:summary AS JSONB)
             WHERE run_id=:run_id AND merchant_id=:merchant_id
               AND stage='verifying' AND claimed_by_worker=:worker_id
               AND claimed_until > CURRENT_TIMESTAMP AND cancelled_at IS NULL
            RETURNING run_id
        ''', {'run_id': run_id, 'merchant_id': merchant_id, 'worker_id': worker_id,
              'summary': json.dumps(summary, default=str)})
        if row is None:
            return False
        if settlement['refunded_credits']:
            await credit(merchant_id, 'audit', settlement['refunded_credits'],
                         source_event_id=f'consumer_refund:{run_id}',
                         purchased_credits=settlement['purchased_credits'], conn=database)
    return True
