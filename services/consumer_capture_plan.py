"""Frozen supplemental capture jobs shared by quoting and resumable execution.

Not connected to merchant launch yet. A checkpoint must durably store the entire
state before returning; callers must enforce the existing tenant worker lease.
"""
import hashlib
import json
from collections import Counter
from services.credit_consumption_service import estimate_probe_credits

PROVIDERS = frozenset({'gemini', 'chatgpt', 'claude'})


def build_plan(*, product_keys, queries, providers):
    def strings(values, limit):
        if not isinstance(values, list) or not values or len(values) > limit:
            raise ValueError('Invalid consumer capture scope')
        if any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in values):
            raise ValueError('Invalid consumer capture entry')
        return list(dict.fromkeys(v.strip() for v in values))
    products, questions, engines = strings(product_keys, 50), strings(queries, 8), strings(providers, 3)
    if any(p not in PROVIDERS for p in engines):
        raise ValueError('Consumer capture requires supported real providers')
    jobs = []
    for product in products:
        for provider in engines:
            for query in questions:
                identity = json.dumps([product, provider, query], ensure_ascii=False, separators=(',', ':'))
                jobs.append({'id': hashlib.sha256(identity.encode()).hexdigest(),
                             'product_key': product, 'provider': provider, 'query': query})
    digest = hashlib.sha256(json.dumps(jobs, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'version': 'consumer_capture_v1', 'sha256': digest, 'jobs': jobs}


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get('version') != 'consumer_capture_v1':
        raise ValueError('Unsupported consumer capture plan')
    jobs = plan.get('jobs')
    if not isinstance(jobs, list) or not jobs or len(jobs) > 1200:
        raise ValueError('Invalid consumer capture jobs')
    seen = set()
    for job in jobs:
        if not isinstance(job, dict) or any(not isinstance(job.get(k), str) or not job[k].strip() or len(job[k]) > 1000 for k in ('product_key', 'provider', 'query')):
            raise ValueError('Invalid consumer capture job')
        identity = json.dumps([job['product_key'], job['provider'], job['query']], ensure_ascii=False, separators=(',', ':'))
        expected = hashlib.sha256(identity.encode()).hexdigest()
        if job.get('id') != expected or expected in seen or job['provider'] not in PROVIDERS:
            raise ValueError('Invalid or duplicate consumer capture job')
        seen.add(expected)
    digest = hashlib.sha256(json.dumps(jobs, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if plan.get('sha256') != digest:
        raise ValueError('Consumer capture plan hash mismatch')


def quote_plan(plan):
    validate_plan(plan)
    counts = Counter(job['provider'] for job in plan['jobs'])
    credits, usd = estimate_probe_credits([(provider, count, True) for provider, count in sorted(counts.items())])
    return {'plan_sha256': plan['sha256'], 'probe_count': len(plan['jobs']),
            'credits': credits, 'estimated_usd_cogs': usd}


async def execute_plan(plan, *, retained, checkpoint, probe):
    """One invocation per frozen job; never replay an ambiguously started call.

    Checkpoint or lease failure propagates. Provider failures are recorded, not
    silently retried. This prevents recovery from multiplying paid API calls;
    it does not claim exactly-once delivery across an external provider.
    """
    validate_plan(plan)
    if retained and retained.get('plan_sha256') != plan['sha256']:
        raise ValueError('Consumer capture plan changed during recovery')
    state = {'plan_sha256': plan['sha256'], 'jobs': dict((retained or {}).get('jobs') or {})}
    for job in plan['jobs']:
        if job['id'] in state['jobs']:
            continue
        state['jobs'][job['id']] = {'status': 'started'}
        await checkpoint(json.loads(json.dumps(state)))
        try:
            result = await probe(job)
        except Exception as exc:
            state['jobs'][job['id']] = {'status': 'failed', 'error_type': type(exc).__name__}
        else:
            state['jobs'][job['id']] = {'status': 'completed', 'result': result}
        await checkpoint(json.loads(json.dumps(state)))
    return state
