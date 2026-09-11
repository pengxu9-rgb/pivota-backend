"""Frozen supplemental capture jobs shared by quoting and resumable execution.

Connected to merchant preview and launch. A checkpoint must durably store the entire
state before returning; callers must enforce the existing tenant worker lease.
"""
import hashlib
import json
import os
from collections import Counter
from services.credit_consumption_service import estimate_probe_credits

PROVIDERS = frozenset({'gemini', 'chatgpt', 'claude'})


def build_plan(*, product_keys, queries, providers, version='consumer_capture_v2'):
    def strings(values, limit):
        if not isinstance(values, list) or not values or len(values) > limit:
            raise ValueError('Invalid consumer capture scope')
        if any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in values):
            raise ValueError('Invalid consumer capture entry')
        return sorted(set(v.strip() for v in values))
    products, questions, engines = strings(product_keys, 50), strings(queries, 8), strings(providers, 3)
    if any(p not in PROVIDERS for p in engines):
        raise ValueError('Consumer capture requires supported real providers')
    jobs = []
    for product in products:
        for provider in engines:
            for query in questions:
                profile = 'openai_web_required_v2' if version == 'consumer_capture_v2' and provider == 'chatgpt' else None
                identity_parts = [product, provider, query] + ([profile] if profile else [])
                identity = json.dumps(identity_parts, ensure_ascii=False, separators=(',', ':'))
                jobs.append({'id': hashlib.sha256(identity.encode()).hexdigest(),
                             'product_key': product, 'provider': provider, 'query': query,
                             **({'execution_profile':profile} if profile else {})})
    digest = hashlib.sha256(json.dumps(jobs, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'version': version, 'sha256': digest, 'jobs': jobs}


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get('version') not in {'consumer_capture_v1','consumer_capture_v2'}:
        raise ValueError('Unsupported consumer capture plan')
    jobs = plan.get('jobs')
    if not isinstance(jobs, list) or not jobs or len(jobs) > 1200:
        raise ValueError('Invalid consumer capture jobs')
    seen = set()
    for job in jobs:
        if not isinstance(job, dict) or any(not isinstance(job.get(k), str) or not job[k].strip() or len(job[k]) > 1000 for k in ('product_key', 'provider', 'query')):
            raise ValueError('Invalid consumer capture job')
        profile = 'openai_web_required_v2' if plan['version']=='consumer_capture_v2' and job['provider']=='chatgpt' else None
        if job.get('execution_profile') != profile:
            raise ValueError('Consumer execution profile changed')
        identity = json.dumps([job['product_key'], job['provider'], job['query']] + ([profile] if profile else []), ensure_ascii=False, separators=(',', ':'))
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
    # Freeze deterministic per-job allocations; their sum equals the quote.
    allocations = {}
    previous = 0
    specs = []
    for job in plan['jobs']:
        specs.append((job['provider'], 1, True))
        cumulative, _ = estimate_probe_credits(specs)
        allocations[job['id']] = cumulative - previous
        previous = cumulative
    return {'job_credits': allocations, 'plan_sha256': plan['sha256'], 'probe_count': len(plan['jobs']),
            'credits': credits, 'estimated_usd_cogs': usd,
            'execution_profiles': sorted({job.get('execution_profile','consumer_query_v1') for job in plan['jobs']}),
            'pricing_basis': 'fixed_probe_credits_not_token_settlement'}


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


def plan_for_launch(*, product_keys, queries, providers):
    if not queries:
        return None
    if os.getenv('PIVOTA_CONSUMER_ANSWER_ENABLED') != 'true':
        raise ValueError('Consumer answer capture is not enabled')
    # Admission is explicit operational configuration, not a quota guarantee.
    # Claude has not passed the real Vertex availability check yet.
    admitted = {p.strip() for p in os.getenv(
        'PIVOTA_CONSUMER_ANSWER_PROVIDERS', 'gemini,chatgpt').split(',') if p.strip()}
    if not admitted.issubset(PROVIDERS):
        raise ValueError('Invalid consumer provider admission configuration')
    if any(p not in admitted for p in providers):
        raise ValueError('Consumer answer provider is not available; remove it before requesting a quote')
    return build_plan(product_keys=product_keys, queries=queries, providers=providers)
