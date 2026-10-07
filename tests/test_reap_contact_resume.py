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


# -- the re-entry window: a contact-paused row whose owner never comes back ends -----------------
#
# `ledger.lapse_contact_reentry` (REAP_AGENTIC_CONTACT_REENTRY_WINDOW_SECONDS, default 86400).
# Before it, a paused row was exempt from every sweep and `contact_retention_blocked` never fell.

_DAY = 86400


async def _age_purge(pid, seconds):
    from db.database import IS_POSTGRES
    expr = (f"clock_timestamp()-INTERVAL '{int(seconds)} seconds'" if IS_POSTGRES
            else f"datetime('now','-{int(seconds)} seconds')")
    await database.execute(f'UPDATE reap_agentic_purchases SET contact_purged_at={expr} WHERE id=:id', {'id': pid})


async def _journal(pid, key, event, code=None, checkout=None):
    from db import reap_continuation as continuation
    await database.execute(continuation._APPEND, {'id': pid, 'key': key, 'event': event, 'quote': 'q_fixture',
                                                  'enrollment': 'e_fixture', 'checkout': checkout, 'code': code})


@pytest.mark.parametrize('state,terminal', [('resolving', 'failed'), ('needs_enrollment', 'expired'),
                                            ('quoting', 'failed')])
async def test_paused_row_past_the_reentry_window_ends_with_its_own_code(client, state, terminal):
    pid, body = await _paused(client)
    await database.execute('UPDATE reap_agentic_purchases SET state=:s WHERE id=:id', {'s': state, 'id': pid})
    assert await ledger.count_contact_retention_blocked() == 1
    await _age_purge(pid, _DAY + 60)
    assert await ledger.lapse_contact_reentry(window_seconds=_DAY) == [pid]
    row = await ledger.get_purchase_internal(pid)
    assert row['state'] == terminal and row['last_error_code'] == 'contact_reentry_lapsed'
    assert row['terminal_at'] is not None and row['claimed_by'] is None
    assert row['buyer_email'] is None and row['shipping_address'] is None
    assert terminal in ledger.ALLOWED_TRANSITIONS[state]
    assert await ledger.count_contact_retention_blocked() == 0
    assert await ledger.lapse_contact_reentry(window_seconds=_DAY) == []
    # A terminal row cannot be hydrated by a late resume.
    late = await client.post(BASE + f'/purchases/{pid}/resume', json=body)
    assert late.status_code == 409 and _error(late) == 'terminal_purchase_not_resumable'
    assert await database.fetch_val('SELECT count(*) FROM reap_agentic_purchases') == 1


async def test_paused_row_inside_the_reentry_window_is_untouched(client):
    pid, _ = await _paused(client)
    await _age_purge(pid, _DAY - 600)
    assert await ledger.lapse_contact_reentry(window_seconds=_DAY) == []
    row = await ledger.get_purchase_internal(pid)
    assert row['state'] == 'resolving' and row['last_error_code'] == 'contact_retention_elapsed'
    assert await ledger.count_contact_retention_blocked() == 1


@pytest.mark.parametrize('evidence', ['dispatch_key', 'checkout_id', 'order_id', 'observed',
                                      'started_unanswered', 'legacy_quoting', 'claimed'])
async def test_reentry_lapse_never_touches_a_row_with_dispatch_evidence_or_a_lease(client, evidence):
    pid, _ = await _paused(client)
    await database.execute("UPDATE reap_agentic_purchases SET state='quoting' WHERE id=:id", {'id': pid})
    if evidence == 'dispatch_key':
        await database.execute("UPDATE reap_agentic_purchases SET checkout_dispatch_key='k-held' WHERE id=:id", {'id': pid})
    if evidence == 'checkout_id':
        await database.execute("UPDATE reap_agentic_purchases SET reap_checkout_id='chk_fixture' WHERE id=:id", {'id': pid})
    if evidence == 'order_id':
        await database.execute("UPDATE reap_agentic_purchases SET reap_order_id='ord_fixture' WHERE id=:id", {'id': pid})
    if evidence == 'observed':
        await _journal(pid, 'k-observed', 'started')
        await _journal(pid, 'k-observed', 'not_created', code='CHECKOUT_TEMPORARILY_UNAVAILABLE')
        await _journal(pid, 'k-observed', 'observed', checkout='chk_late')
    if evidence == 'started_unanswered':
        await _journal(pid, 'k-unanswered', 'started')
    if evidence == 'legacy_quoting':
        await database.execute('UPDATE reap_agentic_purchases SET dispatch_tracking_version=NULL WHERE id=:id', {'id': pid})
    if evidence == 'claimed':
        await database.execute("UPDATE reap_agentic_purchases SET claimed_by='live-worker' WHERE id=:id", {'id': pid})
    before = await ledger.get_purchase_internal(pid)
    await _age_purge(pid, 30 * _DAY)
    assert await ledger.lapse_contact_reentry(window_seconds=_DAY) == []
    row = await ledger.get_purchase_internal(pid)
    assert row['state'] == 'quoting' and row['last_error_code'] == before['last_error_code']
    assert row['terminal_at'] is None


async def test_reentry_lapse_takes_a_quoting_row_whose_every_send_was_answered_not_created(client):
    pid, _ = await _paused(client)
    await database.execute("UPDATE reap_agentic_purchases SET state='quoting' WHERE id=:id", {'id': pid})
    await _journal(pid, 'k-answered', 'started')
    await _journal(pid, 'k-answered', 'not_created', code='CHECKOUT_TEMPORARILY_UNAVAILABLE')
    await _age_purge(pid, _DAY + 60)
    assert await ledger.lapse_contact_reentry(window_seconds=_DAY) == [pid]
    assert (await ledger.get_purchase_internal(pid))['state'] == 'failed'


async def test_resume_inside_the_window_still_works_and_is_never_lapsed(client):
    pid, body = await _paused(client)
    await _age_purge(pid, _DAY - 600)
    response = await client.post(BASE + f'/purchases/{pid}/resume', json=body)
    assert response.status_code == 200, response.text
    row = await ledger.get_purchase_internal(pid)
    assert row['contact_purged_at'] is None and row['contact_revision'] == 1
    assert await ledger.lapse_contact_reentry(window_seconds=ledger.CONTACT_REENTRY_WINDOW_SECONDS_MIN) == []
    assert (await ledger.get_purchase_internal(pid))['state'] == 'resolving'
    assert await ledger.count_contact_retention_blocked() == 0


@pytest.mark.parametrize('bad', [0, 3599, 604801, True, 3600.5, '86400'])
async def test_reentry_window_bound_is_strict(bad):
    with pytest.raises(ValueError):
        await ledger.lapse_contact_reentry(window_seconds=bad)


@pytest.mark.parametrize('dials', [{'REAP_AGENTIC_RECONCILE_ENABLED': '0'}, {'REAP_AGENTIC_ENABLED': '0'}])
async def test_poller_lapses_on_every_tick_even_with_the_rail_off(client, monkeypatch, dials):
    import jobs.reap_agentic_purchase_poll as job
    pid, _ = await _paused(client)
    young, _ = await _paused_again(client)
    await _age_purge(pid, _DAY + 60)
    for name, value in dials.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('REAP_AGENTIC_CONTACT_REENTRY_WINDOW_SECONDS', str(_DAY))
    report = await job.run_reap_agentic_purchase_poll(worker_id='reentry-lapse')
    assert report.contact_reentry_lapsed == 1 and report.contact_retention_blocked == 1
    assert report.errors == 0
    assert (await ledger.get_purchase_internal(pid))['last_error_code'] == 'contact_reentry_lapsed'
    assert (await ledger.get_purchase_internal(young))['state'] == 'resolving'


async def _paused_again(client):
    """A second paused purchase for the same owner, under another idempotency key."""
    body = _body(idempotency_key='same-attempt-contact-2')
    response = await client.post(BASE + '/purchases', json=body)
    assert response.status_code == 202, response.text
    pid = response.json()['purchase_id']
    from db.database import IS_POSTGRES
    old = "clock_timestamp()-INTERVAL '1000 seconds'" if IS_POSTGRES else "datetime('now','-1000 seconds')"
    await database.execute(f'UPDATE reap_agentic_purchases SET created_at={old} WHERE id=:id', {'id': pid})
    assert await ledger.scrub_reconciling_purchase_pii() == [pid]
    return pid, body


def test_reentry_dial_matches_the_ledger_bounds():
    import jobs.reap_agentic_purchase_poll as job
    dial = job.DIALS['contact_reentry_window_seconds']
    assert dial.env == 'REAP_AGENTIC_CONTACT_REENTRY_WINDOW_SECONDS'
    assert (dial.default, dial.minimum, dial.maximum) == (86400, 3600, 604800)


# -- legacy (pre-migration-256) paused rows: no contact_purged_at, no tracking version ---------


async def _legacy_paused(client, state, entered_ago):
    from db.database import IS_POSTGRES
    pid, _ = await _paused(client)
    expr = (f"clock_timestamp()-INTERVAL '{int(entered_ago)} seconds'" if IS_POSTGRES
            else f"datetime('now','-{int(entered_ago)} seconds')")
    await database.execute(
        f'UPDATE reap_agentic_purchases SET state=:s, contact_purged_at=NULL, dispatch_tracking_version=NULL, '
        f'state_entered_at={expr} WHERE id=:id', {'s': state, 'id': pid})
    row = await ledger.get_purchase_internal(pid)
    assert row['last_error_code'] == 'contact_retention_elapsed' and row['contact_purged_at'] is None
    return pid


@pytest.mark.parametrize('state,terminal', [('resolving', 'failed'), ('needs_enrollment', 'expired')])
async def test_a_legacy_paused_row_lapses_on_its_state_clock(client, state, terminal):
    pid = await _legacy_paused(client, state, _DAY + 60)
    assert await ledger.count_contact_retention_blocked() == 1
    # A claim and release (every poll) bumps updated_at and must not move the anchor.
    await database.execute("UPDATE reap_agentic_purchases SET next_poll_at=CURRENT_TIMESTAMP WHERE id=:id", {'id': pid})
    assert [r['id'] for r in await ledger.claim_due_purchases('legacy-poll')] == [pid]
    assert await ledger.release_claim(pid, 'legacy-poll') is not None
    assert await ledger.lapse_contact_reentry(window_seconds=_DAY) == [pid]
    row = await ledger.get_purchase_internal(pid)
    assert row['state'] == terminal and row['last_error_code'] == 'contact_reentry_lapsed'
    assert await ledger.count_contact_retention_blocked() == 0


async def test_a_legacy_paused_row_inside_the_window_is_untouched(client):
    pid = await _legacy_paused(client, 'resolving', _DAY - 600)
    assert await ledger.lapse_contact_reentry(window_seconds=_DAY) == []
    assert (await ledger.get_purchase_internal(pid))['state'] == 'resolving'


@pytest.mark.parametrize('variant', ['legacy_quoting', 'versioned_without_purge', 'other_code'])
async def test_the_legacy_branch_takes_nothing_else(client, variant):
    pid = await _legacy_paused(client, 'quoting' if variant == 'legacy_quoting' else 'resolving', 30 * _DAY)
    if variant == 'versioned_without_purge':
        await database.execute('UPDATE reap_agentic_purchases SET dispatch_tracking_version=1 WHERE id=:id', {'id': pid})
    if variant == 'other_code':
        await database.execute("UPDATE reap_agentic_purchases SET last_error_code='transport_error:readtimeout' WHERE id=:id", {'id': pid})
    assert await ledger.lapse_contact_reentry(window_seconds=_DAY) == []
    assert (await ledger.get_purchase_internal(pid))['terminal_at'] is None
