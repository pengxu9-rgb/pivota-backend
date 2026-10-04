"""Owner/request/attempt-bound contact re-entry, via real HTTP routes and local storage."""
import asyncio
import copy
import pytest
from db.database import database
from db import reap_agentic_ledger as ledger
from test_agent_commerce_reap_routes import (
    _db, _env, _no_network, client, _body, _seed_all, _error,
    BASE, CALLER, OTHER_USER_REF, USER_REF, DOMAIN,
)

async def _paused(client):
    await _seed_all()
    body=_body(idempotency_key='same-attempt-contact')
    response=await client.post(BASE+'/purchases',json=body)
    assert response.status_code == 202,response.text
    pid=response.json()['purchase_id']
    await database.execute("UPDATE reap_agentic_purchases SET created_at=datetime('now','-1000 seconds') WHERE id=:id",{'id':pid})
    assert await ledger.scrub_reconciling_purchase_pii() == [pid]
    return pid,body

async def test_reentry_restores_same_attempt_once_and_never_extends_duplicate_retention(client):
    pid,body=await _paused(client)
    before=await ledger.get_purchase_internal(pid)
    response=await client.post(BASE+f'/purchases/{pid}/resume',json=body)
    assert response.status_code == 200,response.text
    assert response.json()['contact_reentry_required'] is False
    first=await ledger.get_purchase_internal(pid)
    assert first['id']==pid and first['contact_revision']==1
    assert first['buyer_ref']==before['buyer_ref'] and first['click_id']==before['click_id']
    again=await client.post(BASE+f'/purchases/{pid}/resume',json=body)
    assert again.status_code == 200
    second=await ledger.get_purchase_internal(pid)
    assert second['contact_revision']==1 and second['contact_received_at']==first['contact_received_at']
    assert await ledger.scrub_reconciling_purchase_pii() == []
    assert await database.fetch_val('SELECT count(*) FROM reap_agentic_purchases') == 1

@pytest.mark.parametrize('mutation,code',[('owner','purchase_not_found'),('attempt','purchase_not_found'),('body','idempotency_conflict'),('key','purchase_not_found'),('terminal','terminal_purchase_not_resumable'),('legacy','checkout_dispatch_unresolved'),('dispatch','checkout_dispatch_unresolved'),('price','price_changed'),('claimed','resume_raced')])
async def test_reentry_refuses_wrong_binding_terminal_unknown_dispatch_or_changed_price(client,mutation,code):
    pid,body=await _paused(client)
    target=pid
    if mutation=='owner': CALLER.agent_user_ref=OTHER_USER_REF
    if mutation=='attempt': target='rp_wrong_attempt'
    if mutation=='body': body['buyer']['email']='changed@example.test'
    if mutation=='key': body['idempotency_key']='another-key'
    if mutation=='terminal': await database.execute("UPDATE reap_agentic_purchases SET state='expired' WHERE id=:id",{'id':pid})
    if mutation=='legacy': await database.execute('UPDATE reap_agentic_purchases SET dispatch_tracking_version=NULL WHERE id=:id',{'id':pid})
    if mutation=='dispatch': await database.execute("UPDATE reap_agentic_purchases SET checkout_dispatch_key='pending-dispatch' WHERE id=:id",{'id':pid})
    if mutation=='price': await database.execute('UPDATE catalog_offers SET merchant_effective_price=99')
    if mutation=='claimed': await database.execute("UPDATE reap_agentic_purchases SET claimed_by='poller' WHERE id=:id",{'id':pid})
    response=await client.post(BASE+f'/purchases/{target}/resume',json=body)
    assert response.status_code in (404,409),response.text
    assert _error(response)==code,response.text
    row=await ledger.get_purchase_internal(pid)
    assert row['contact_revision']==0 and row['buyer_email'] is None and row['shipping_address'] is None
    assert await database.fetch_val('SELECT count(*) FROM reap_agentic_purchases') == 1

async def test_concurrent_reentries_have_one_contact_revision(client):
    pid,body=await _paused(client)
    a,b=await asyncio.gather(client.post(BASE+f'/purchases/{pid}/resume',json=body),client.post(BASE+f'/purchases/{pid}/resume',json=body))
    assert a.status_code==200 and b.status_code==200,(a.text,b.text)
    row=await ledger.get_purchase_internal(pid)
    assert row['contact_revision']==1

async def test_owner_view_reports_contact_without_exposing_data_or_inventing_dispatch(client):
    pid,body=await _paused(client)
    response=await client.get(BASE+f'/purchases/{pid}')
    view=response.json()
    assert view['contact_reentry_required'] is True and view['checkout_dispatch_state']=='not_dispatched'
    assert 'buyer_email' not in view and 'shipping_address' not in view
    await database.execute('UPDATE reap_agentic_purchases SET dispatch_tracking_version=NULL WHERE id=:id',{'id':pid})
    assert (await client.get(BASE+f'/purchases/{pid}')).json()['checkout_dispatch_state']=='unknown'


async def test_fresh_acceptance_carries_committed_owner_dispatch_facts(client):
    await _seed_all()
    body = _body(idempotency_key='accepted-dispatch-facts')
    response = await client.post(BASE + '/purchases', json=body)
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted['checkout_dispatch_state'] == 'not_dispatched'
    assert accepted['contact_reentry_required'] is False
    assert set(accepted) == {'purchase_id', 'status', 'poll_after_seconds',
                             'checkout_dispatch_state', 'contact_reentry_required'}
    view = (await client.get(BASE + '/purchases/' + accepted['purchase_id'])).json()
    assert accepted['status'] == view['state']
    assert accepted['checkout_dispatch_state'] == view['checkout_dispatch_state']


@pytest.mark.parametrize('legacy', [True, False])
async def test_acceptance_replay_preserves_unknown_or_started_dispatch(client, legacy):
    pid, body = await _paused(client)
    if legacy:
        await database.execute('UPDATE reap_agentic_purchases SET dispatch_tracking_version=NULL WHERE id=:id', {'id':pid})
    else:
        await database.execute("UPDATE reap_agentic_purchases SET checkout_dispatch_key='already-started' WHERE id=:id", {'id':pid})
    response = await client.post(BASE + '/purchases', json=body)
    assert response.status_code == 202, response.text
    assert response.json()['purchase_id'] == pid
    assert response.json()['checkout_dispatch_state'] == ('unknown' if legacy else 'dispatch_started')
    assert response.json()['contact_reentry_required'] is True
    assert await database.fetch_val('SELECT count(*) FROM reap_agentic_purchases') == 1


async def test_fresh_acceptance_observes_dispatch_racing_after_commit(client, monkeypatch):
    import routes.agent_commerce_reap as routes
    await _seed_all()
    original = routes._owner_view
    async def racing_view(**kwargs):
        await database.execute("UPDATE reap_agentic_purchases SET state='quoting',checkout_dispatch_key='concurrent-dispatch' WHERE id=:id", {'id':kwargs['purchase_id']})
        return await original(**kwargs)
    monkeypatch.setattr(routes, '_owner_view', racing_view)
    response = await client.post(BASE + '/purchases', json=_body(idempotency_key='racing-dispatch-facts'))
    assert response.status_code == 202, response.text
    assert response.json()['status'] == 'quoting'
    assert response.json()['checkout_dispatch_state'] == 'dispatch_started'


async def test_acceptance_owner_read_failure_is_unknown_without_replacement(client, monkeypatch):
    import routes.agent_commerce_reap as routes
    await _seed_all()
    original = routes._owner_view
    async def unavailable_view(**kwargs):
        raise RuntimeError('synthetic owner read unavailable')
    monkeypatch.setattr(routes, '_owner_view', unavailable_view)
    body = _body(idempotency_key='lost-accepted-owner-read')
    response = await client.post(BASE + '/purchases', json=body)
    assert response.status_code == 503 and _error(response) == 'checkout_outcome_unknown'
    row = await database.fetch_one('SELECT id FROM reap_agentic_purchases')
    monkeypatch.setattr(routes, '_owner_view', original)
    replay = await client.post(BASE + '/purchases', json=body)
    assert replay.status_code == 202 and replay.json()['purchase_id'] == row['id']
    assert replay.json()['checkout_dispatch_state'] == 'not_dispatched'
    assert await database.fetch_val('SELECT count(*) FROM reap_agentic_purchases') == 1
