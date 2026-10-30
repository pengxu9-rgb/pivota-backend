"""Retention and dispatch fault-injection regressions; only local DB and fake transport."""
import asyncio
import pytest
from db.database import database, IS_POSTGRES
from db import reap_agentic_ledger as ledger, reap_continuation as continuation
from services import reap_agentic_purchase as svc
from test_reap_agentic_purchase_postgres import (
    _db, _env, _no_network, reap, attribution, _start, _step, _get, _claim,
    _active_enrollment, _ok, _transport, ENROLLMENT_ACTIVE, ENROLLMENT_CREATED,
    CHECKOUT_CREATED,
)

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="requires disposable Postgres test database")

async def _age(pid):
    await ledger.release_claim(pid, 'w1')
    await database.execute("UPDATE reap_agentic_purchases SET created_at=clock_timestamp()-INTERVAL '1000 seconds', hosted_url_expires_at=clock_timestamp()-INTERVAL '600 seconds', state_entered_at=clock_timestamp()-INTERVAL '1000 seconds', next_poll_at=CURRENT_TIMESTAMP WHERE id=:id", {'id':pid})

@pytest.mark.parametrize('status', ['ACTIVE', 'REQUIRES_ACTION', 'UNRECOGNIZED'])
async def test_retention_and_hosted_deadline_cannot_stop_same_enrollment_reads(reap, status):
    pid = await _start()
    await _step(pid)
    await _age(pid)
    assert await ledger.scrub_reconciling_purchase_pii() == [pid]
    assert await ledger.expire_overdue_purchases(max_age_seconds=900) == []
    assert await ledger.fail_exhausted_purchases(1) == []
    reap.get_enrollment = _ok({**ENROLLMENT_ACTIVE, 'status':status})
    reap.calls.clear()
    await _claim(pid, 'late-reader')
    await svc.advance(pid, 'late-reader')
    row = await _get(pid)
    assert row['state'] == 'needs_enrollment'
    assert row['buyer_email'] is None and row['shipping_address'] is None
    assert continuation.contact_required(row)
    assert reap.sequence() == ['get_enrollment']
    enrollment = await ledger.get_enrollment_internal(row['enrollment_id'])
    assert enrollment['status'] == ('active' if status == 'ACTIVE' else 'pending')
    assert continuation.dispatch_state(row) == 'not_dispatched'

async def test_enrollment_reads_survive_create_off_and_changed_pilot_scope(reap, monkeypatch):
    pid = await _start(); await _step(pid); await _age(pid)
    await ledger.scrub_reconciling_purchase_pii()
    monkeypatch.setenv('REAP_AGENTIC_CREATE_ENABLED','0')
    monkeypatch.setenv('REAP_AGENTIC_PILOT_SCOPE','{"agent_ids":["other"],"merchant_domains":["other.test"],"markets":["US"],"product_keys":["other"],"quantities":[1]}')
    claimed = await ledger.claim_due_purchases('reader-only', reconciliation_only=True, pilot_scope={'agent_ids':['other'],'merchant_domains':['other.test'],'markets':['US'],'product_keys':['other'],'quantities':[1]})
    assert [r['id'] for r in claimed] == [pid]
    reap.calls.clear(); await svc.advance(pid,'reader-only')
    assert reap.sequence() == ['get_enrollment']
    assert (await _get(pid))['state'] == 'needs_enrollment'

async def test_terminal_purchase_never_reconciles_or_quotes(reap):
    pid=await _start(); await _step(pid)
    await ledger.transition_as_holder(pid,'w1',from_states=['needs_enrollment'],to_state='expired',last_error_code='contact_retention_elapsed')
    reap.calls.clear()
    result=await svc.advance(pid,'w1')
    assert result.outcome == 'terminal' and reap.calls == []
    assert (await _get(pid))['state'] == 'expired'

async def test_dispatch_intent_is_durable_before_call_and_timeout_blocks_all_replay(reap):
    await _active_enrollment(); pid=await _start(); await _step(pid)
    async def timeout(**kwargs):
        row=await _get(pid)
        assert row['checkout_dispatch_key'] and row['reap_quote_id'] == kwargs['quote_id']
        events=await database.fetch_all('SELECT event_type FROM reap_checkout_dispatch_events WHERE purchase_id=:id',{'id':pid})
        assert [x['event_type'] for x in events] == ['started']
        return _transport()
    reap.create_checkout=timeout
    await _step(pid)
    assert continuation.dispatch_state(await _get(pid)) == 'dispatch_started'
    reap.calls.clear()
    result=await _step(pid)
    assert result.last_error_code == 'checkout_dispatch_unresolved'
    assert reap.calls == []
    await _age(pid)
    assert await ledger.fail_exhausted_purchases(1) == []
    assert await ledger.expire_overdue_purchases(max_age_seconds=60) == []

async def test_missing_legacy_ids_are_unknown_and_cannot_dispatch(reap):
    await _active_enrollment();pid=await _start();await _step(pid)
    await database.execute('UPDATE reap_agentic_purchases SET dispatch_tracking_version=NULL WHERE id=:id',{'id':pid})
    reap.calls.clear();result=await _step(pid)
    assert result.last_error_code == 'checkout_dispatch_unresolved' and reap.calls == []
    assert continuation.dispatch_state(await _get(pid)) == 'unknown'

async def test_duplicate_concurrent_dispatch_intent_has_one_winner(reap):
    await _active_enrollment();pid=await _start();await _step(pid)
    row=await _get(pid)
    args=dict(quote_id='quote-concurrent',enrollment_id='provider-enrollment',enrollment_row_id=row['enrollment_id'],total=100,expires=None)
    first,second=await asyncio.gather(continuation.begin_dispatch(row,'w1',**args),continuation.begin_dispatch(row,'w1',**args))
    assert sum(x is not None for x in (first,second)) == 1
    events=await database.fetch_all('SELECT * FROM reap_checkout_dispatch_events WHERE purchase_id=:id',{'id':pid})
    assert len(events) == 1

async def test_provider_response_after_claim_loss_preserves_dispatch_evidence(reap):
    await _active_enrollment();pid=await _start();await _step(pid)
    async def lost(**kwargs):
        await database.execute("UPDATE reap_agentic_purchases SET claimed_by='different-worker' WHERE id=:id",{'id':pid})
        return _ok(CHECKOUT_CREATED)
    reap.create_checkout=lost
    result=await _step(pid)
    assert result.outcome == 'lost_claim'
    row=await _get(pid)
    assert row['state'] == 'quoting' and row['reap_checkout_id'] is None
    assert continuation.dispatch_state(row) == 'dispatch_started'
    events=await database.fetch_all('SELECT event_type FROM reap_checkout_dispatch_events WHERE purchase_id=:id',{'id':pid})
    assert {x['event_type'] for x in events} == {'started','observed'}

@pytest.mark.parametrize('operation', ['UPDATE reap_checkout_dispatch_events SET provider_code=\'fake\' WHERE purchase_id=:id', 'DELETE FROM reap_checkout_dispatch_events WHERE purchase_id=:id'])
async def test_dispatch_journal_cannot_be_changed_or_deleted(reap,operation):
    await _active_enrollment();pid=await _start();await _step(pid);await _step(pid)
    with pytest.raises(Exception,match='append-only'):
        await database.execute(operation,{'id':pid})
    assert await database.fetch_val('SELECT count(*) FROM reap_checkout_dispatch_events WHERE purchase_id=:id',{'id':pid}) == 2

async def test_pending_enrollment_after_grace_is_not_replaced(reap):
    from test_reap_agentic_purchase_postgres import _a_waiting_on_a_link as _purchase_a_waiting_on_a_link, _age_links, _echo_session
    original_pid, enrollment = await _purchase_a_waiting_on_a_link(reap)
    reap.get_enrollment = _echo_session()
    await _age_links(400)
    reap.calls.clear()
    # This is a separately explicit local fixture purchase, not an automatic replacement.
    pid=await _start()
    result=await _step(pid)
    assert result.last_error_code=='enrollment_settling'
    assert reap.named('create_enrollment')==[]
    assert (await ledger.get_enrollment_internal(enrollment))['status']=='pending'
    assert (await _get(original_pid))['enrollment_id']==enrollment
    assert (await _get(pid))['hosted_url'] is None

async def test_contact_cutoff_before_hosted_deadline_keeps_enrollment_reader(reap):
    from datetime import datetime, timedelta, timezone
    pid=await _start();await _step(pid);await ledger.release_claim(pid,'w1')
    now=datetime.now(timezone.utc)
    await database.execute('UPDATE reap_agentic_purchases SET created_at=:created,hosted_url_expires_at=:deadline,next_poll_at=CURRENT_TIMESTAMP WHERE id=:id',
        {'id':pid,'created':ledger._bind_dt(now-timedelta(seconds=901)),'deadline':ledger._bind_dt(now+timedelta(seconds=12))})
    assert await ledger.scrub_reconciling_purchase_pii(max_age_seconds=900)==[pid]
    assert await ledger.expire_overdue_purchases(enrollment_grace_seconds=0)==[]
    reap.calls.clear();await _step(pid)
    assert reap.sequence()==['get_enrollment']
    assert (await _get(pid))['state']=='needs_enrollment'

async def test_only_explicit_negative_dispatch_receipt_allows_fresh_quote(reap):
    from services import reap_agentic_client as rc
    from test_reap_agentic_purchase_postgres import QUOTE_200
    await _active_enrollment();pid=await _start();await _step(pid)
    reap.create_checkout=rc.ReapResponse(ok=False,status=409,error='reap_status_409',error_code='QUOTE_EXPIRED')
    await _step(pid)
    assert continuation.dispatch_state(await _get(pid))=='not_dispatched'
    events=await database.fetch_all('SELECT event_type FROM reap_checkout_dispatch_events WHERE purchase_id=:id',{'id':pid})
    assert {x['event_type'] for x in events}=={'started','not_created'}
    reap.request_quote=_ok(dict(QUOTE_200,id='new_quote'))
    reap.create_checkout=_ok(CHECKOUT_CREATED)
    assert (await _step(pid)).state=='awaiting_approval'
    assert len(reap.named('create_checkout'))==2
    assert continuation.dispatch_state(await _get(pid))=='dispatched'

async def test_untrusted_negative_shape_cannot_clear_dispatch_fence(reap):
    from services import reap_agentic_client as rc
    await _active_enrollment();pid=await _start();await _step(pid)
    reap.create_checkout=rc.ReapResponse(ok=False,status=500,error='reap_status_500',error_code='QUOTE_EXPIRED')
    await _step(pid)
    assert continuation.dispatch_state(await _get(pid))=='dispatch_started'
    await _step(pid)
    assert len(reap.named('create_checkout'))==1
    assert await database.fetch_val("SELECT count(*) FROM reap_checkout_dispatch_events WHERE purchase_id=:id AND event_type='not_created'",{'id':pid})==0
