"""Independent fault-injection probes against frozen 1b8953b; local SQLite, fake network."""
import asyncio
import json
import httpx
_REAL_ASYNC_CLIENT = httpx.AsyncClient
from datetime import datetime, timedelta, timezone

import pytest
from db.database import database, IS_POSTGRES
from db import reap_agentic_ledger as ledger, reap_continuation as continuation
from services import reap_agentic_purchase as svc, reap_agentic_client as rc
_REAL_CREATE_CHECKOUT = rc.create_checkout
from test_reap_agentic_purchase_postgres import (
    _db, _env, _no_network, reap, attribution, _start, _step, _get, _claim,
    _active_enrollment, _ok, _transport, ENROLLMENT_ACTIVE, ENROLLMENT_CREATED,
    CHECKOUT_CREATED, QUOTE_200,
)

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="requires disposable Postgres test database")

async def _quoting():
    await _active_enrollment()
    pid = await _start()
    await _step(pid)
    return pid

async def _events(pid):
    return [dict(r) for r in await database.fetch_all(
        'SELECT * FROM reap_checkout_dispatch_events WHERE purchase_id=:id', {'id':pid})]

async def _begin(pid, quote='external-quote'):
    row=await _get(pid)
    key=await continuation.begin_dispatch(row, 'w1', quote_id=quote,
        enrollment_id=ENROLLMENT_ACTIVE['id'], enrollment_row_id=row['enrollment_id'],
        total=4500, expires=None)
    return row, key

async def _record(row, key, response, quote='external-quote'):
    await continuation.record_dispatch_response(row, 'w1', key=key,
        quote_id=quote,enrollment_id=ENROLLMENT_ACTIVE['id'],checkout=response)

async def test_journal_insert_failure_rolls_back_predispatch_purchase_mutation(reap, monkeypatch):
    pid=await _quoting()
    before=await _get(pid)
    original=database.execute
    async def failing(query, *args, **kwargs):
        if 'INSERT INTO reap_checkout_dispatch_events' in str(query):
            raise RuntimeError('injected journal write failure')
        return await original(query,*args,**kwargs)
    monkeypatch.setattr(database,'execute',failing)
    with pytest.raises(RuntimeError,match='journal write failure'):
        await _step(pid)
    after=await _get(pid)
    for name in ('checkout_dispatch_key','reap_quote_id','quoted_total_minor','reap_quote_expires_at'):
        assert after[name] == before[name]
    assert await _events(pid) == []
    assert reap.named('create_checkout') == []

async def test_cancelled_checkout_keeps_fence_and_forbids_quote_replay(reap):
    pid=await _quoting()
    async def cancelled(**kw):
        assert (await _get(pid))['checkout_dispatch_key']
        raise asyncio.CancelledError()
    reap.create_checkout=cancelled
    with pytest.raises(asyncio.CancelledError):
        await _step(pid)
    assert [e['event_type'] for e in await _events(pid)] == ['started']
    reap.calls.clear()
    result=await _step(pid,'replacement')
    assert result.last_error_code == 'checkout_dispatch_unresolved'
    assert reap.calls == []
    assert await ledger.count_checkout_needs_human() == 1

async def test_response_journal_failure_keeps_prior_intent_and_blocks_replay(reap, monkeypatch):
    pid=await _quoting()
    original=database.execute
    async def failing(query,*args,**kwargs):
        values=args[0] if args else kwargs.get('values',{})
        if 'INSERT INTO reap_checkout_dispatch_events' in str(query) and values.get('event')=='observed':
            raise RuntimeError('injected receipt write failure')
        return await original(query,*args,**kwargs)
    monkeypatch.setattr(database,'execute',failing)
    with pytest.raises(RuntimeError,match='receipt write failure'):
        await _step(pid)
    assert [e['event_type'] for e in await _events(pid)] == ['started']
    reap.calls.clear()
    assert (await _step(pid,'replacement')).last_error_code=='checkout_dispatch_unresolved'
    assert reap.calls == []

async def test_same_quote_after_explicit_negative_never_dispatches_twice(reap):
    pid=await _quoting()
    reap.create_checkout=rc.ReapResponse(ok=False,status=409,error='reap_status_409',error_code='QUOTE_EXPIRED')
    await _step(pid)
    reap.create_checkout=_ok(CHECKOUT_CREATED)
    result=await _step(pid)
    assert result.last_error_code=='checkout_dispatch_unresolved'
    assert len(reap.named('create_checkout'))==1
    assert {e['event_type'] for e in await _events(pid)}=={'started','not_created'}
    assert continuation.dispatch_state(await _get(pid))=='dispatch_started'

async def test_positive_receipt_before_negative_cannot_clear_fence(reap):
    pid=await _quoting();row,key=await _begin(pid)
    await _record(row,key,_ok(CHECKOUT_CREATED))
    await _record(row,key,rc.ReapResponse(ok=False,status=409,error_code='QUOTE_EXPIRED'))
    assert continuation.dispatch_state(await _get(pid))=='dispatch_started'
    assert {e['event_type'] for e in await _events(pid)}=={'started','observed','not_created'}

async def test_negative_then_positive_receipt_must_not_advertise_no_dispatch(reap):
    pid=await _quoting();row,key=await _begin(pid)
    await _record(row,key,rc.ReapResponse(ok=False,status=409,error_code='QUOTE_EXPIRED'))
    await _record(row,key,_ok(CHECKOUT_CREATED))
    after=await _get(pid)
    state_before=continuation.dispatch_state(after)
    new=await continuation.begin_dispatch(after,'w1',quote_id='different-quote',
        enrollment_id=ENROLLMENT_ACTIVE['id'],enrollment_row_id=after['enrollment_id'],total=4500,expires=None)
    assert (state_before, new)==('dispatch_started', None), (state_before,new,[e['event_type'] for e in await _events(pid)])

@pytest.mark.parametrize('response',[
    rc.ReapResponse(ok=False,status=200,error='unparseable_response'),
    rc.ReapResponse(ok=False,status=200,error='response_too_large'),
    rc.ReapResponse(ok=False,status=200,error='hosted_url_not_allowed'),
    rc.ReapResponse(ok=False,status=500,error='reap_status_500'),
    rc.ReapResponse(ok=True,status=200,data={}),
])
async def test_unknown_checkout_response_remains_reconcilable_human_work(reap,response):
    pid=await _quoting();reap.create_checkout=response
    result=await _step(pid)
    after=await _get(pid)
    assert after['checkout_dispatch_key']
    human_count=await ledger.count_checkout_needs_human()
    assert after['state'] not in ledger.TERMINAL_STATES, (result,after['last_error_code'],human_count)
    assert human_count==1
    reap.calls.clear();await _step(pid)
    assert reap.calls==[]

@pytest.mark.parametrize('response',[_ok(CHECKOUT_CREATED),_transport(),
    rc.ReapResponse(ok=False,status=409,error='reap_status_409',error_code='QUOTE_EXPIRED')])
async def test_checkout_response_cannot_mutate_replacement_lease_with_same_worker_id(reap,response):
    pid=await _quoting()
    replacement_timestamp=ledger._bind_dt(datetime(2098,1,1,tzinfo=timezone.utc))
    async def replace_lease(**kwargs):
        await database.execute('UPDATE reap_agentic_purchases SET claimed_at=:stamp, attempts=attempts+1 WHERE id=:id',
            {'id':pid,'stamp':replacement_timestamp})
        return response
    reap.create_checkout=replace_lease
    result=await _step(pid)
    after=await _get(pid)
    assert result.outcome=='lost_claim', (result, after['state'],after['claimed_at'])
    assert after['state']=='quoting' and after['claimed_by']=='w1'
    assert after['claimed_at'].year==2098

@pytest.mark.parametrize('status',['ACTIVE','REQUIRES_ACTION','NEW_FUTURE_STATUS'])
async def test_enrollment_after_retention_flags_off_has_no_contact_side_effect(reap,monkeypatch,status):
    pid=await _start();await _step(pid);await ledger.release_claim(pid,'w1')
    await database.execute("UPDATE reap_agentic_purchases SET created_at=clock_timestamp()-INTERVAL '9999 seconds',hosted_url_expires_at=clock_timestamp()-INTERVAL '9000 seconds',next_poll_at=CURRENT_TIMESTAMP WHERE id=:id", {'id':pid})
    assert await ledger.scrub_reconciling_purchase_pii()==[pid]
    monkeypatch.setenv('REAP_AGENTIC_CREATE_ENABLED','0')
    reap.get_enrollment=_ok({**ENROLLMENT_ACTIVE,'status':status})
    reap.calls.clear()
    claims=await ledger.claim_due_purchases('reader',reconciliation_only=True)
    assert [r['id'] for r in claims]==[pid]
    await svc.advance(pid,'reader')
    after=await _get(pid)
    assert reap.sequence()==['get_enrollment']
    assert after['state']=='needs_enrollment'
    assert after['buyer_email'] is None and after['shipping_address'] is None
    assert continuation.contact_required(after)

async def test_stale_enrollment_response_cannot_change_same_worker_replacement(reap):
    from datetime import datetime, timezone
    pid=await _start();await _step(pid)
    replacement = {}
    async def change(**kwargs):
        replacement['attempts'] = (await _get(pid))['attempts'] + 1
        await database.execute("UPDATE reap_agentic_purchases SET claimed_at='2098-01-01 00:00:00+00', attempts=attempts+1 WHERE id=:id",{'id':pid})
        return _ok(ENROLLMENT_ACTIVE)
    reap.get_enrollment=change
    result=await _step(pid)
    after=await _get(pid)
    assert result.outcome=='lost_claim'
    assert (await ledger.get_enrollment_internal(after['enrollment_id']))['status']=='pending'
    assert after['claimed_by']=='w1' and after['claimed_at']==datetime(2098, 1, 1, tzinfo=timezone.utc)
    assert after['attempts']==replacement['attempts']

@pytest.mark.parametrize('terminal',sorted(ledger.TERMINAL_STATES))
async def test_every_terminal_state_has_no_provider_or_enrollment_effects(reap,attribution,terminal):
    pid=await _start();await _step(pid)
    await database.execute('UPDATE reap_agentic_purchases SET state=:state WHERE id=:id',{'id':pid,'state':terminal})
    before=await _get(pid);enrollment=await ledger.get_enrollment_internal(before['enrollment_id'])
    reap.calls.clear();result=await svc.advance(pid,'w1')
    assert result.outcome=='terminal' and reap.calls==[] and attribution.calls==[]
    assert await _get(pid)==before
    assert await ledger.get_enrollment_internal(before['enrollment_id'])==enrollment

@pytest.mark.parametrize('kind', ['invalid_json','oversize','unsafe_action','http_500','missing_id'])
async def test_real_checkout_client_ambiguous_response_stays_nonterminal(reap, monkeypatch, kind):
    pid=await _quoting()
    seen=[]
    def transport(request):
        assert request.method=='POST' and request.url.path=='/agentic/checkouts'
        seen.append(request)
        if kind=='invalid_json':
            return httpx.Response(200,content=b'{not json')
        if kind=='oversize':
            return httpx.Response(200,content=b'x'*(rc.MAX_RESPONSE_BYTES+1))
        if kind=='unsafe_action':
            return httpx.Response(200,json={**CHECKOUT_CREATED,'nextAction':{'type':'REDIRECT','url':'https://attacker.invalid/checkout'}})
        if kind=='http_500':
            return httpx.Response(500,json={'error':{'code':'UNKNOWN_PROVIDER_ERROR'}})
        return httpx.Response(200,json={})
    mock=httpx.MockTransport(transport)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:_REAL_ASYNC_CLIENT(transport=mock,**kw))
    monkeypatch.setattr(rc,'create_checkout',_REAL_CREATE_CHECKOUT)
    result=await _step(pid)
    after=await _get(pid)
    assert len(seen)==1 and after['checkout_dispatch_key']
    events=await _events(pid)
    assert {e['event_type'] for e in events}=={'started'}
    assert after['state'] not in ledger.TERMINAL_STATES, (kind,result,await ledger.count_checkout_needs_human())

async def test_ambiguous_dispatch_owner_projection_is_nonterminal_and_never_no_dispatch(reap):
    from routes.agent_commerce_reap import _owner_public_body
    pid=await _quoting()
    reap.create_checkout=rc.ReapResponse(ok=False,status=200,error='unparseable_response')
    await _step(pid)
    view=await _owner_public_body(await _get(pid))
    assert view['state']=='quoting'
    assert view['last_error_code']=='checkout_dispatch_unresolved'
    assert view['checkout_dispatch_state']=='dispatch_started'
    assert view['contact_reentry_required'] is False
    assert not view.get('hosted_url')
    assert 'buyer_email' not in view and 'shipping_address' not in view

@pytest.mark.parametrize('outcome,expected',[('COMPLETED','completed'),('FAILED','failed'),('EXPIRED','expired')])
async def test_valid_checkout_without_hosted_action_stays_visible_and_reconciles(reap,outcome,expected):
    from routes.agent_commerce_reap import _owner_public_body
    from test_reap_agentic_purchase_postgres import CHECKOUT_COMPLETED
    pid=await _quoting()
    reap.create_checkout=_ok({**CHECKOUT_CREATED,'nextAction':None})
    assert (await _step(pid)).state=='awaiting_approval'
    row=await _get(pid)
    assert row['reap_checkout_id']==CHECKOUT_CREATED['id']
    assert row['hosted_url'] is None
    assert await ledger.count_checkout_needs_human()==1
    view=await _owner_public_body(row)
    assert view['checkout_dispatch_state']=='dispatched' and view['state']=='awaiting_approval'
    assert view['last_error_code']=='checkout_unresolvable:3:checkout_no_hosted_action'
    reap.get_checkout=_ok({'status':'REQUIRES_ACTION'})
    await _step(pid)
    assert await ledger.count_checkout_needs_human()==1
    reap.get_checkout=_ok({**CHECKOUT_COMPLETED,'status':outcome})
    reap.calls.clear()
    assert (await _step(pid)).state==expected
    assert reap.sequence()==['get_checkout']
    assert await ledger.count_checkout_needs_human()==0

async def test_contact_scrub_cannot_hide_existing_dispatch_review(reap):
    from routes.agent_commerce_reap import _owner_public_body
    pid=await _quoting()
    reap.create_checkout=rc.ReapResponse(ok=False,status=500,error='reap_status_500')
    await _step(pid)
    await database.execute('UPDATE reap_agentic_purchases SET created_at=:old WHERE id=:id',
        {'id':pid,'old':ledger._bind_dt(datetime.now(timezone.utc)-timedelta(seconds=1000))})
    assert await ledger.scrub_reconciling_purchase_pii()==[pid]
    row=await _get(pid)
    assert row['buyer_email'] is None and row['shipping_address'] is None
    assert row['last_error_code']=='checkout_dispatch_unresolved'
    assert await ledger.count_checkout_needs_human()==1
    view=await _owner_public_body(row)
    assert view['last_error_code']=='checkout_dispatch_unresolved' and view['checkout_dispatch_state']=='dispatch_started'
    reap.calls.clear();await _step(pid)
    assert reap.calls==[]
    assert (await _get(pid))['last_error_code']=='checkout_dispatch_unresolved'
