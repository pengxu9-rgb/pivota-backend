"""Optional detail identity recovery: real HTTP boundary, no external network or DB."""
import asyncio
import copy
import hashlib
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from services import curated_brand_feed as feed
from services import catalog_onboard_worker as worker
from scripts import onboard_curated_brands as cli

GTIN = '08809530070499'


def product(n=1, vendor="A'PIEU"):
    return {'id': 9000000+n, 'handle': f'honey-milk-lip-oil-{n}', 'vendor': vendor,
            'title': 'Honey & Milk Lip Oil', 'product_type': 'Lip Oil',
            'images': [{'src': 'https://cdn.example/oil.jpg'}],
            'variants': [{'id': 45000000000000+n, 'price': '10.00', 'available': True}]}


def detail(p):
    result = copy.deepcopy(p)
    result['title'] = 'WRONG DETAIL TITLE'
    result['variants'][0].update(barcode='8809530070499', price=999999)
    return result


def install_http(monkeypatch, replies):
    seen = []
    def handler(request):
        seen.append(str(request.url))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)
    real_client = httpx.AsyncClient
    def factory(**kwargs):
        assert kwargs['follow_redirects'] is False
        assert kwargs['timeout'].read == 10
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(feed.httpx, 'AsyncClient', factory)
    monkeypatch.setattr(feed.crawl_politeness, 'before_request', AsyncMock())
    monkeypatch.setattr(feed.crawl_politeness, 'note_response', lambda *a, **kw: None)
    return seen


@pytest.mark.asyncio
async def test_only_barcode_copied_and_native_identity_preserved(monkeypatch):
    p = product()
    p['variants'].append({'id': 45000000000002, 'barcode': 'keep-supplier-code', 'price': '22.00'})
    before = copy.deepcopy(p)
    d = detail(p)
    d['variants'][1]['barcode'] = '8809530070499'
    d['variants'].append({'id': 45000000000999, 'barcode': '8809530070499'})
    install_http(monkeypatch, [d])
    rows, report = await feed.recover_missing_variant_gtins([p], domain='eyurs.com')
    expected = copy.deepcopy(before)
    expected['variants'][0]['barcode'] = GTIN
    assert rows == [expected]
    assert p == before
    assert report == dict(attempted=1, recovered=1, failed=0, capped=0, recovered_gtins=1, http_requests=1)
    assert feed.crawl_politeness.before_request.await_args.kwargs['max_wait'] == 10.0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'id': 9999999}, {'handle': 'other-product'}, {'vendor': 'MISSHA'},
    {'variants': [{'id': 45000000000999, 'barcode': '8809530070499'}]},
    {'variants': [{'id': 45000000000001, 'barcode': '8809530070499'}]*2},
    {'variants': [None]}, {'variants': None},
])
async def test_wrong_product_vendor_handle_or_variant_never_earns_identity(monkeypatch, change):
    p = product()
    d = detail(p)
    d.update(change)
    install_http(monkeypatch, [d])
    rows, report = await feed.recover_missing_variant_gtins([p], domain='eyurs.com')
    assert rows == [p]
    assert report['failed'] == 1 and report['recovered'] == 0


@pytest.mark.parametrize('value', [None, True, 8809530070499, '7', '8809530070498',
    'code8809530070499', '８８０９５３００７０４９９', '0'*15, ''])
def test_invalid_source_gtin_is_not_normalized_into_match_key(value):
    assert feed.validated_source_gtin(value) is None


def test_source_gtin_uses_existing_canonical_normalization():
    assert feed.validated_source_gtin('8809530070499') == GTIN
    assert feed.validated_source_gtin('08809530070499') == GTIN


@pytest.mark.asyncio
@pytest.mark.parametrize('location', ['https://sg.eyurs.com/products/oil.js',
    'https://eyurs.com.evil.example/oil.js', 'https://other.com/oil.js',
    'http://eyurs.com/oil.js', 'https://user@eyurs.com/oil.js', 'https://eyurs.com:8443/oil.js'])
async def test_unsafe_redirect_is_never_visited(monkeypatch, location):
    seen = install_http(monkeypatch, [httpx.Response(302, headers={'location': location})])
    rows, report = await feed.recover_missing_variant_gtins([product()], domain='eyurs.com')
    assert len(seen) == 1 and report['failed'] == 1
    assert 'barcode' not in rows[0]['variants'][0]


@pytest.mark.asyncio
async def test_apex_www_redirect_allowed_and_paced(monkeypatch):
    p = product()
    url = 'https://www.eyurs.com/products/honey-milk-lip-oil-1.js'
    seen = install_http(monkeypatch, [httpx.Response(302, headers={'location': url}), detail(p)])
    rows, report = await feed.recover_missing_variant_gtins([p], domain='eyurs.com')
    assert seen[-1] == url and report['http_requests'] == 2 and report['recovered'] == 1
    assert rows[0]['variants'][0]['barcode'] == GTIN
    assert feed.crawl_politeness.before_request.await_count == 2


@pytest.mark.asyncio
async def test_final_response_host_is_pinned_even_if_client_misbehaves(monkeypatch):
    p = product()
    install_http(monkeypatch, [])
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, json=detail(p), request=httpx.Request('GET', 'https://other.com/oil.js'))
    recovered, requests = await feed._fetch_missing_variant_gtins(p, domain='eyurs.com', client=client)
    assert recovered == {} and requests == 1


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded(monkeypatch):
    replies = [httpx.Response(302, headers={'location': '/products/again.js'}) for _ in range(3)]
    seen = install_http(monkeypatch, replies)
    _, report = await feed.recover_missing_variant_gtins([product()], domain='eyurs.com')
    assert len(seen) == 3 and report['failed'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('budget', [0, 1])
async def test_attempt_budget_and_capped_diagnostics(monkeypatch, budget):
    seen = install_http(monkeypatch, [detail(product())])
    rows, report = await feed.recover_missing_variant_gtins([product(), product(2)], domain='eyurs.com', max_fetches=budget)
    assert len(seen) == budget and report['attempted'] == budget
    assert report['capped'] == 2-budget and report['recovered'] == budget
    assert 'barcode' not in rows[1]['variants'][0]


@pytest.mark.asyncio
async def test_present_barcode_never_fetches_even_if_invalid(monkeypatch):
    p = product()
    p['variants'][0]['barcode'] = 'existing-code'
    seen = install_http(monkeypatch, [])
    rows, report = await feed.recover_missing_variant_gtins([p], domain='eyurs.com')
    assert not seen and rows == [p] and report['attempted'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [httpx.Response(429), httpx.Response(200, text='not JSON'), httpx.ReadTimeout('timeout')])
async def test_observation_failure_is_reported_without_modifying_product(monkeypatch, reply):
    p = product()
    install_http(monkeypatch, [reply])
    rows, report = await feed.recover_missing_variant_gtins([p], domain='eyurs.com')
    assert rows == [p] and report['failed'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
async def test_selected_only_before_fold_and_batch_local_diagnostics(monkeypatch, enabled):
    selected, excluded = product(), product(2, 'MISSHA')
    raw = feed.ShopifyProductBatch([selected, excluded], scanned_products=2, pages=1)
    monkeypatch.setattr(feed, 'fetch_shopify_products', AsyncMock(return_value=raw))
    monkeypatch.setattr(feed, 'fetch_shopify_shop_locale', AsyncMock(return_value={'currency': 'USD'}))
    seen = install_http(monkeypatch, [detail(selected)])
    folded = []
    def fold(products):
        folded.extend(copy.deepcopy(products))
        return products, {}
    monkeypatch.setattr(feed, 'fold_shade_listings', fold)
    rows = await feed.records_for_brand(domain='eyurs.com', category_path='beauty',
        source_role='retailer', only_vendors=["A'PIEU"], emit_real_variants=True,
        base_listings_only=True, enrich_missing_gtin=enabled)
    assert len(folded) == 1 and len(rows) == 1 and len(seen) == int(enabled)
    assert folded[0]['variants'][0].get('barcode') == (GTIN if enabled else None)
    assert rows.crawl_report['status'] == 'complete'
    assert ('gtin_recovery' in rows.crawl_report) == enabled
    if enabled:
        assert rows.crawl_report['gtin_recovery']['recovered'] == 1
        assert rows[0]['pdp']['barcode'] == GTIN


@pytest.mark.parametrize('override', [{'enrich_missing_gtin': 'false'}, {'max_pdp_identity_fetches': -1},
    {'max_pdp_identity_fetches': True}, {'max_pdp_identity_fetches': 1.5}])
def test_invalid_recovery_controls_fail_before_fetch(monkeypatch, override):
    fetch = AsyncMock()
    monkeypatch.setattr(feed, 'fetch_shopify_products', fetch)
    with pytest.raises(ValueError):
        asyncio.run(feed.records_for_brand(domain='eyurs.com', category_path='beauty', **override))
    fetch.assert_not_awaited()
    with pytest.raises(ValueError):
        worker.normalize_curated_brand_payload({'domain': 'eyurs.com', **override})


def test_worker_keys_preserve_disabled_jobs_and_distinguish_enabled_budget():
    job = {'domain': 'eyurs.com'}
    normalized = worker.normalize_curated_brand_payload(job)
    normalized.pop('enrich_missing_gtin')
    normalized.pop('max_pdp_identity_fetches')
    for field in ('brand', 'retailer_name'):
        normalized[field] = (normalized[field] or '').casefold()
    normalized['source'] = 'curated_list'
    old_digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    assert worker.curated_brand_work_key(job) == f'curated:v2:eyurs.com:{old_digest}'
    assert worker.curated_brand_work_key(job) == worker.curated_brand_work_key(dict(job, enrich_missing_gtin=False, max_pdp_identity_fetches=0))
    enabled = dict(job, enrich_missing_gtin=True)
    assert worker.curated_brand_work_key(job) != worker.curated_brand_work_key(enabled)
    assert worker.curated_brand_work_key(enabled) != worker.curated_brand_work_key(dict(enabled, max_pdp_identity_fetches=7))


def test_cli_passes_opt_in_and_zero_budget(monkeypatch):
    fake = AsyncMock(return_value=[])
    fake.last_vendor_filter_report = None
    fake.last_brand_census = None
    monkeypatch.setattr(cli, 'records_for_brand', fake)
    assert cli.main(['--domain', 'eyurs.com', '--category', 'beauty', '--enrich-missing-gtin', '--max-pdp-identity-fetches', '0']) == 0
    assert fake.await_args.kwargs['enrich_missing_gtin'] is True
    assert fake.await_args.kwargs['max_pdp_identity_fetches'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'id': None}, {'handle': ''}, {'vendor': ''},
    {'variants': [{'id': 'synthetic:1'}]},
    {'variants': [{'id': 45000000000001}]*2}])
async def test_unproven_source_identity_does_not_trigger_detail_read(monkeypatch, change):
    p = product()
    p.update(change)
    seen = install_http(monkeypatch, [])
    rows, report = await feed.recover_missing_variant_gtins([p], domain='eyurs.com')
    assert not seen and rows == [p] and report['failed'] == 1


@pytest.mark.asyncio
async def test_shared_gate_refusal_is_counted_without_bypassing_it(monkeypatch):
    seen = install_http(monkeypatch, [])
    feed.crawl_politeness.before_request.side_effect = RuntimeError('merchant backoff exceeds bounded wait')
    rows, report = await feed.recover_missing_variant_gtins([product()], domain='eyurs.com')
    assert not seen and report['failed'] == 1 and report['http_requests'] == 0
    assert 'barcode' not in rows[0]['variants'][0]


def test_cli_row_can_disable_opt_in_and_preserves_zero_budget(monkeypatch, tmp_path):
    fake = AsyncMock(return_value=[])
    fake.last_vendor_filter_report = None
    fake.last_brand_census = None
    monkeypatch.setattr(cli, 'records_for_brand', fake)
    roster = tmp_path / 'roster.jsonl'
    roster.write_text(json.dumps({'domain': 'eyurs.com', 'category_path': 'beauty',
        'enrich_missing_gtin': False, 'max_pdp_identity_fetches': 0})+'\n')
    assert cli.main(['--file', str(roster), '--enrich-missing-gtin', '--max-pdp-identity-fetches', '99']) == 0
    assert fake.await_args.kwargs['enrich_missing_gtin'] is False
    assert fake.await_args.kwargs['max_pdp_identity_fetches'] == 0
