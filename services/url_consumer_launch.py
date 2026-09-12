"""One commit for a quoted URL audit's credit debit and durable run."""
from copy import deepcopy
from db.database import database
from db.merchant_audit_runs import enqueue_audit_run_with_replay
from services.credit_consumption_service import consume


class UrlLaunchUnavailable(RuntimeError):
    pass


class UrlLaunchInsufficientCredits(ValueError):
    pass


async def launch_quoted_url_audit(*, merchant_id, product_keys, idempotency_key,
                                request_options_jsonb, credits, usd_cogs):
    async with database.transaction():
        # Serializes identical submissions, including completed-run replays.
        await database.execute('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))',
                               {'key': 'url-consumer:' + merchant_id + ':' + idempotency_key})
        existing = await database.fetch_one('''SELECT run_id FROM merchant_audit_runs
            WHERE merchant_id=:merchant AND idempotency_key=:key
            ORDER BY requested_at DESC LIMIT 1''', {'merchant': merchant_id, 'key': idempotency_key})
        if existing:
            return str(existing['run_id']), True
        balance = await database.fetch_one('''SELECT credits FROM merchant_credit_balance
            WHERE merchant_id=:merchant FOR UPDATE''', {'merchant': merchant_id})
        if not balance or int(balance['credits']) < credits:
            # Do not create a Stripe overage charge inside a database transaction.
            raise UrlLaunchInsufficientCredits('Not enough credits for the quoted audit')
        result = await consume(merchant_id, 'audit', idempotency_key='url_wedge:' + idempotency_key,
                               credits=credits, usd_cogs=usd_cogs, conn=database)
        if result.get('replay'):
            # A legacy orphan debit must never fund another run after a refund.
            raise UrlLaunchUnavailable('Debit exists without its matching audit; use a new quote later')
        options = deepcopy(request_options_jsonb)
        options['launch']['debited'] = [{'kind': 'audit', 'amount': credits, 'replay': False,
            'purchased_credits': int((result.get('debit') or {}).get('purchased_credits_debited') or 0)}]
        run_id, replay = await enqueue_audit_run_with_replay(
            merchant_id=merchant_id, product_keys=product_keys, subject_type='merchant_url',
            idempotency_key=idempotency_key, request_options_jsonb=options)
        if not run_id or replay:
            raise UrlLaunchUnavailable('Audit enqueue failed; debit rolled back')
        return run_id, False
