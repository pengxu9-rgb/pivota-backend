"""Postgres twin of tests/test_reap_dispatch_evidence.py: the same probes against real Postgres
(the journal's triggers, `substr` and ON CONFLICT run on the engine production uses)."""
import json
import logging

import httpx
_REAL_ASYNC_CLIENT = httpx.AsyncClient

import pytest
from db.database import database, IS_POSTGRES
from db import reap_agentic_ledger as ledger, reap_continuation as continuation
from services import reap_agentic_purchase as svc, reap_agentic_client as rc
_REAL_CREATE_CHECKOUT = rc.create_checkout
from test_reap_agentic_purchase_postgres import (
    _db, _env, _no_network, reap, attribution, _start, _step, _get,
    _active_enrollment, _ok, CHECKOUT_CREATED, QUOTE_200, RETURN_URL, ENROLLMENT_UUID,
)

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="requires disposable Postgres test database")

HOSTILE = 'https://attacker.invalid/card-capture-7Q2X'


async def _quoting():
    await _active_enrollment()
    pid = await _start()
    await _step(pid)
    return pid


async def _events(pid):
    return [dict(r) for r in await database.fetch_all(
        'SELECT * FROM reap_checkout_dispatch_events WHERE purchase_id=:id', {'id':pid})]


async def _every_stored_value():
    if IS_POSTGRES:
        tables = [r['tablename'] for r in await database.fetch_all(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")]
    else:
        tables = [r['name'] for r in await database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    values = []
    for table in tables:
        for record in await database.fetch_all(f'SELECT * FROM "{table}"'):
            values.extend(str(v) for v in dict(record).values() if v is not None)
    return values


def _real_client(monkeypatch, handler):
    seen = []
    def transport(request):
        seen.append(request)
        return handler(request)
    mock = httpx.MockTransport(transport)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: _REAL_ASYNC_CLIENT(transport=mock, **kw))
    monkeypatch.setattr(rc, 'create_checkout', _REAL_CREATE_CHECKOUT)
    return seen


# -- 1. a refused hosted action still names the checkout it created -------------------------


async def test_client_carries_validated_checkout_id_out_of_a_hosted_url_refusal(monkeypatch):
    def hostile(request):
        return httpx.Response(200, json={**CHECKOUT_CREATED, 'nextAction': {'type':'REDIRECT','url':HOSTILE}})
    _real_client(monkeypatch, hostile)
    result = await rc.create_checkout(quote_id='f1e2d3c4', enrollment_id=ENROLLMENT_UUID,
                                      return_url=RETURN_URL)
    assert (result.ok, result.error, result.data) == (False, 'hosted_url_not_allowed', {})
    assert result.refused_checkout_id == CHECKOUT_CREATED['id']
    assert HOSTILE not in repr(result)


@pytest.mark.parametrize('bad_id', ['../x', 'a/b', '', None, 7, 'x' * 65])
async def test_client_refused_checkout_id_uses_the_path_id_rule(monkeypatch, bad_id):
    def hostile(request):
        return httpx.Response(200, json={**CHECKOUT_CREATED, 'id': bad_id, 'nextAction': {'type':'REDIRECT','url':HOSTILE}})
    _real_client(monkeypatch, hostile)
    result = await rc.create_checkout(quote_id='f1e2d3c4', enrollment_id=ENROLLMENT_UUID,
                                      return_url=RETURN_URL)
    assert result.error == 'hosted_url_not_allowed' and result.refused_checkout_id is None


async def test_hostile_hosted_url_parks_with_checkout_id_in_journal_and_url_nowhere(reap, monkeypatch, caplog):
    from routes.agent_commerce_reap import _owner_public_body
    caplog.set_level(logging.DEBUG)
    pid = await _quoting()
    seen = _real_client(monkeypatch, lambda request: httpx.Response(
        200, json={**CHECKOUT_CREATED, 'nextAction': {'type':'REDIRECT','url':HOSTILE}}))
    result = await _step(pid)
    row = await _get(pid)
    assert len(seen) == 1
    assert result.last_error_code == svc.CHECKOUT_CREATED_HOSTED_URL_REFUSED
    assert row['state'] == 'quoting' and row['checkout_dispatch_key']
    assert row['reap_checkout_id'] is None and row['hosted_url'] is None
    observed = [e for e in await _events(pid) if e['event_type'] == 'observed']
    assert [e['checkout_id'] for e in observed] == [CHECKOUT_CREATED['id']]
    assert {e['event_type'] for e in await _events(pid)} == {'started', 'observed'}
    assert await ledger.count_checkout_needs_human() == 1
    view = await _owner_public_body(row)
    assert view['last_error_code'] == svc.CHECKOUT_CREATED_HOSTED_URL_REFUSED
    assert view['checkout_dispatch_state'] == 'dispatch_started' and not view.get('hosted_url')
    # Never re-sent, and the operator's name for it survives every later poll.
    reap.calls.clear()
    again = await _step(pid)
    assert len(seen) == 1 and reap.calls == []
    assert again.last_error_code == svc.CHECKOUT_CREATED_HOSTED_URL_REFUSED
    assert (await _get(pid))['last_error_code'] == svc.CHECKOUT_CREATED_HOSTED_URL_REFUSED
    for needle in (HOSTILE, 'attacker.invalid', 'card-capture-7Q2X'):
        assert needle not in caplog.text
        assert not [v for v in await _every_stored_value() if needle in v]


async def test_refused_hosted_url_with_malformed_id_stays_generic_unresolved(reap, monkeypatch):
    pid = await _quoting()
    _real_client(monkeypatch, lambda request: httpx.Response(
        200, json={**CHECKOUT_CREATED, 'id': '../x', 'nextAction': {'type':'REDIRECT','url':HOSTILE}}))
    result = await _step(pid)
    assert result.last_error_code == svc.CHECKOUT_DISPATCH_UNRESOLVED
    assert {e['event_type'] for e in await _events(pid)} == {'started'}
    assert (await _get(pid))['checkout_dispatch_key']


# -- 2. a create that never left the process releases the fence --------------------------------


class _StopAtFinalCheck:
    """A real AsyncClient whose entry flips a switch, so the client's re-check stops the send."""
    def __init__(self, monkeypatch, env):
        self.monkeypatch, self.env, self.streams = monkeypatch, env, []
    def __call__(self, **kwargs):
        outer = self
        class Client:
            async def __aenter__(self):
                outer.monkeypatch.setenv(outer.env, '0')
                return self
            async def __aexit__(self, *args):
                return False
            def stream(self, *args, **kwargs):
                outer.streams.append(args)
                raise AssertionError('checkout dispatched after a final-check stop')
        return Client()


async def _assert_released_not_dispatched(pid, reason):
    row = await _get(pid)
    assert row['checkout_dispatch_key'] is None and row['state'] == 'quoting'
    assert continuation.dispatch_state(row) == 'not_dispatched'
    events = await _events(pid)
    assert {e['event_type'] for e in events} == {'started', 'not_created'}
    assert [e['provider_code'] for e in events if e['event_type'] == 'not_created'] == [
        continuation.NOT_DISPATCHED_PREFIX + reason]
    assert await ledger.count_checkout_needs_human() == 0


@pytest.mark.parametrize('env', ['REAP_AGENTIC_CREATE_ENABLED', 'REAP_AGENTIC_RECONCILE_ENABLED'])
async def test_final_permission_stop_inside_client_releases_and_then_dispatches_once(reap, monkeypatch, env):
    pid = await _quoting()
    stop = _StopAtFinalCheck(monkeypatch, env)
    monkeypatch.setattr(httpx, 'AsyncClient', stop)
    monkeypatch.setattr(rc, 'create_checkout', _REAL_CREATE_CHECKOUT)
    result = await _step(pid)
    assert stop.streams == [] and result.outcome == 'released' and result.state == 'quoting'
    await _assert_released_not_dispatched(pid, 'provider_operation_stopped')
    assert (await _get(pid))['claimed_by'] is None
    # Switch back on: a NEW quote id is a new key, dispatched exactly once.
    monkeypatch.setenv(env, '1')
    reap.request_quote = _ok({**QUOTE_200, 'id': 'q_second'})
    sent = _real_client(monkeypatch, lambda request: httpx.Response(200, json=CHECKOUT_CREATED))
    assert (await _step(pid)).state == 'awaiting_approval'
    assert len(sent) == 1 and json.loads(sent[0].content)['quoteId'] == 'q_second'
    assert (await _get(pid))['reap_checkout_id'] == CHECKOUT_CREATED['id']


async def test_replayed_quote_of_an_unsent_key_waits_without_parking(reap, monkeypatch):
    pid = await _quoting()
    monkeypatch.setattr(httpx, 'AsyncClient', _StopAtFinalCheck(monkeypatch, 'REAP_AGENTIC_CREATE_ENABLED'))
    monkeypatch.setattr(rc, 'create_checkout', _REAL_CREATE_CHECKOUT)
    await _step(pid)
    monkeypatch.setenv('REAP_AGENTIC_CREATE_ENABLED', '1')
    sent = _real_client(monkeypatch, lambda request: httpx.Response(200, json=CHECKOUT_CREATED))
    # Same quote id inside Reap's quote idempotency bucket: same key, never dispatched twice.
    held = await _step(pid)
    assert sent == [] and held.last_error_code == 'checkout_quote_replayed'
    assert held.next_poll_in_seconds > rc.QUOTE_IDEMPOTENCY_BUCKET_S
    row = await _get(pid)
    assert row['checkout_dispatch_key'] is None and await ledger.count_checkout_needs_human() == 0
    reap.request_quote = _ok({**QUOTE_200, 'id': 'q_after_bucket'})
    assert (await _step(pid)).state == 'awaiting_approval' and len(sent) == 1


async def test_unconfigured_client_releases_without_parking(reap, monkeypatch):
    pid = await _quoting()
    monkeypatch.setattr(rc, 'create_checkout', _REAL_CREATE_CHECKOUT)
    monkeypatch.delenv('REAP_API_KEY')
    result = await _step(pid)
    assert result.last_error_code == 'reap_client_not_configured' and result.state == 'quoting'
    await _assert_released_not_dispatched(pid, 'reap_client_not_configured')


async def test_client_config_refusal_releases_fence_then_raises(reap, monkeypatch):
    pid = await _quoting()
    monkeypatch.setattr(rc, 'create_checkout', _REAL_CREATE_CHECKOUT)
    monkeypatch.setattr(rc, 'validate_base_url', lambda raw=None: (_ for _ in ()).throw(rc.ReapConfigError('bad host')))
    with pytest.raises(rc.ReapConfigError):
        await _step(pid)
    await _assert_released_not_dispatched(pid, 'reap_config_error')


async def test_local_failure_before_the_send_line_releases_with_transport_backoff(reap, monkeypatch):
    pid = await _quoting()
    seen = _real_client(monkeypatch, lambda request: httpx.Response(200, json=CHECKOUT_CREATED))
    def broken_headers(*args, **kwargs):
        raise RuntimeError('header build failed')
    monkeypatch.setattr(rc, '_headers', broken_headers)
    result = await _step(pid)
    assert seen == [] and result.last_error_code == 'transport_error:runtimeerror'
    await _assert_released_not_dispatched(pid, 'local_error')


# -- 3. once the send line is crossed, nothing can claim "not dispatched" -----------------------


@pytest.mark.parametrize('failure', ['connect', 'read_timeout', 'local_bug', 'stop_flips_mid_send'])
async def test_failure_after_send_starts_stays_parked(reap, monkeypatch, failure):
    pid = await _quoting()
    def handler(request):
        if failure == 'connect':
            raise httpx.ConnectError('injected', request=request)
        if failure == 'read_timeout':
            raise httpx.ReadTimeout('injected', request=request)
        if failure == 'stop_flips_mid_send':
            monkeypatch.setenv('REAP_AGENTIC_CREATE_ENABLED', '0')
            raise httpx.ReadError('injected', request=request)
        raise RuntimeError('injected after the send line')
    seen = _real_client(monkeypatch, handler)
    result = await _step(pid)
    row = await _get(pid)
    assert len(seen) == 1
    assert result.last_error_code == svc.CHECKOUT_DISPATCH_UNRESOLVED
    assert row['checkout_dispatch_key'] and row['state'] == 'quoting'
    assert {e['event_type'] for e in await _events(pid)} == {'started'}
    assert await ledger.count_checkout_needs_human() == 1
    monkeypatch.setenv('REAP_AGENTIC_CREATE_ENABLED', '1')
    reap.request_quote = _ok({**QUOTE_200, 'id': 'q_second'})
    reap.calls.clear()
    assert (await _step(pid)).last_error_code == svc.CHECKOUT_DISPATCH_UNRESOLVED
    assert len(seen) == 1 and reap.calls == []


async def test_a_fake_client_never_settles_the_probe_so_a_stop_stays_parked(reap):
    pid = await _quoting()
    def stopped(**kwargs):
        raise rc.ProviderOperationStopped('reconciliation_disabled')
    reap.create_checkout = stopped
    await _step(pid)
    assert (await _get(pid))['checkout_dispatch_key']
    assert {e['event_type'] for e in await _events(pid)} == {'started'}


def test_probe_is_write_once_and_the_send_line_wins():
    probe = rc.DispatchProbe()
    probe.mark_sending()
    probe.settle(rc.ProviderOperationStopped('x'))
    assert (probe.not_dispatched, probe.reason) == (False, None)
    settled = rc.DispatchProbe()
    settled.settle(rc.ReapResponse(ok=False, error='reap_client_not_configured'))
    assert (settled.not_dispatched, settled.reason) == (True, 'reap_client_not_configured')
    settled.settle(rc.ReapConfigError('later'))
    assert settled.reason == 'reap_client_not_configured'
    settled.mark_sending()
    assert settled.not_dispatched is False
    assert rc.DispatchProbe().not_dispatched is False


# -- 3. a definitive provider "not created" re-quotes past the quote bucket --------------------
#
# Reap's 503 CHECKOUT_TEMPORARILY_UNAVAILABLE (and QUOTE_EXPIRED on 400/409) say no checkout was
# created; the receipt clears the fence. The re-quote must land in a LATER quote idempotency
# bucket, or Reap replays the same quote id, the same dispatch key is refused (it has a
# `started`), and the row parked as needs-human work for nothing.


class _BucketedQuotes:
    """Reap's quote idempotency as the client keys it: one quote id per 240 s wall-clock bucket."""

    def __init__(self, t):
        self.t = t

    def __call__(self, **kwargs):
        return _ok({**QUOTE_200, 'id': f'qb{int(self.t // rc.QUOTE_IDEMPOTENCY_BUCKET_S)}'})


def _not_created(kind, retry_after=None):
    if kind == '503':
        return rc.ReapResponse(ok=False, status=503, error='reap_status_503',
                               error_code='CHECKOUT_TEMPORARILY_UNAVAILABLE',
                               retry_after_seconds=retry_after)
    return rc.ReapResponse(ok=False, status=409, error='reap_status_409', error_code='QUOTE_EXPIRED')


@pytest.mark.parametrize('kind,retry_after,code', [
    ('503', None, 'checkout_temporarily_unavailable'),
    ('503', 9, 'checkout_temporarily_unavailable'),
    ('409', None, 'quote_expired'),
])
@pytest.mark.parametrize('t0', [0, 239])
async def test_provider_not_created_requotes_under_a_new_quote_id_instead_of_parking(reap, kind, retry_after, code, t0):
    pid = await _quoting()
    quotes = _BucketedQuotes(t0)
    reap.request_quote = quotes
    reap.create_checkout = [_not_created(kind, retry_after), _ok(CHECKOUT_CREATED)]
    held = await _step(pid)
    assert (held.outcome, held.state, held.last_error_code) == ('released', 'quoting', code)
    assert held.next_poll_in_seconds >= svc.PROVIDER_NOT_CREATED_HOLD_S == rc.QUOTE_IDEMPOTENCY_BUCKET_S + 30
    row = await _get(pid)
    assert row['checkout_dispatch_key'] is None and continuation.dispatch_state(row) == 'not_dispatched'
    assert {e['event_type'] for e in await _events(pid)} == {'started', 'not_created'}
    assert await ledger.count_checkout_needs_human() == 0
    # The next look is no sooner than the hold, which is always a later quote bucket.
    quotes.t = t0 + held.next_poll_in_seconds
    moved = await _step(pid)
    assert moved.state == 'awaiting_approval', moved
    sent = [c['quote_id'] for c in reap.named('create_checkout')]
    assert len(sent) == 2 and sent[0] != sent[1]
    assert (await _get(pid))['reap_checkout_id'] == CHECKOUT_CREATED['id']
    assert await ledger.count_checkout_needs_human() == 0


async def test_provider_retry_after_longer_than_the_bucket_is_kept(reap):
    pid = await _quoting()
    reap.create_checkout = _not_created('503', retry_after=900)
    assert (await _step(pid)).next_poll_in_seconds == 900


async def test_a_quote_replayed_after_a_not_created_answer_still_never_sends_its_key_twice(reap):
    """The guarantee the hold leans on: if Reap ever returned the old quote id anyway, the fence
    refuses the key that already has a `started` event and parks; no second create is sent."""
    pid = await _quoting()
    reap.create_checkout = [_not_created('503'), _ok(CHECKOUT_CREATED)]
    await _step(pid)
    again = await _step(pid)
    assert again.last_error_code == svc.CHECKOUT_DISPATCH_UNRESOLVED
    assert len(reap.named('create_checkout')) == 1
    assert (await _get(pid))['checkout_dispatch_key'] and await ledger.count_checkout_needs_human() == 1
