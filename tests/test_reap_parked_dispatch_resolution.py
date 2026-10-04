"""Operator resolution of a parked checkout create (#2508 follow-up); SQLite, fake network.

The Postgres twin is tests/test_reap_parked_dispatch_resolution_postgres.py (same body, the
#2508 per-engine convention). Before this module the only resolution tool refused every parked
'quoting' row, so a parked create and its needs-human alert stayed for ever.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from db.database import database, IS_POSTGRES
from db import reap_agentic_ledger as ledger, reap_continuation as continuation
from services import reap_agentic_purchase as svc, reap_checkout_recovery as recovery
from services.reap_checkout_recovery import (
    ManualResolutionRefused, dispatch_settle_seconds, list_parked_dispatches, resolve_checkout_manually,
    resolve_parked_dispatch,
)
from test_reap_agentic_purchase import (
    _db, _env, _no_network, reap, attribution, _start, _step, _get, _claim,
    _active_enrollment, _ok, _transport, ENROLLMENT_ACTIVE, CHECKOUT_CREATED, CHECKOUT_COMPLETED,
    QUOTE_200, EMAIL,
)

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="SQLite arm; see the _postgres twin")

PII = (EMAIL, "900 Brannan St", "Lovelace", "+15550100", "San Francisco")


def _evidence(**over):
    evidence = {'source': 'verified_reap_support_statement', 'authoritative_verified': True,
                'reference': 'reap_support_case_1', 'observed_at': datetime.now(timezone.utc),
                'provider_base_url': 'https://sandbox.api.reap.global'}
    evidence.update(over)
    return evidence


async def _events(pid):
    return [dict(r) for r in await database.fetch_all(
        'SELECT * FROM reap_checkout_dispatch_events WHERE purchase_id=:id ORDER BY event_type', {'id': pid})]


async def _audits(pid):
    return [dict(r) for r in await database.fetch_all(
        'SELECT * FROM reap_checkout_dispatch_resolution_audit WHERE purchase_id=:id', {'id': pid})]


async def _quoting(buyer='bref_alice'):
    if buyer == 'bref_alice':
        await _active_enrollment()
    else:
        await _active_enrollment(buyer_ref=buyer, reap_id='4fa85f64-5717-4562-b3fc-2c963f66afa7')
    pid = await _start(buyer_ref=buyer)
    await _step(pid)
    assert (await _get(pid))['state'] == 'quoting'
    return pid


async def _age_started(pid, seconds):
    """Backdate this purchase's `started` receipt. Test-only: the journal is append-only."""
    if IS_POSTGRES:
        await database.execute('ALTER TABLE reap_checkout_dispatch_events DISABLE TRIGGER reap_dispatch_events_immutable')
        try:
            await database.execute("UPDATE reap_checkout_dispatch_events SET recorded_at = recorded_at - (:s * INTERVAL '1 second')"
                                   " WHERE purchase_id=:id AND event_type='started'", {'s': seconds, 'id': pid})
        finally:
            await database.execute('ALTER TABLE reap_checkout_dispatch_events ENABLE TRIGGER reap_dispatch_events_immutable')
    else:
        await database.execute('DROP TRIGGER reap_dispatch_events_no_update')
        try:
            await database.execute("UPDATE reap_checkout_dispatch_events SET recorded_at = datetime(recorded_at, :w)"
                                   " WHERE purchase_id=:id AND event_type='started'", {'w': f'-{seconds} seconds', 'id': pid})
        finally:
            await database.execute(continuation._IMMUTABLE_UPDATE_SQLITE)


async def _parked_unknown(reap, settled=True):
    """A create whose response never arrived: started journaled, no receipt, row parked."""
    pid = await _quoting()
    reap.create_checkout = _transport()
    result = await _step(pid)
    assert result.last_error_code == 'checkout_dispatch_unresolved'
    if settled:
        await _age_started(pid, dispatch_settle_seconds() + 60)
    row = await _get(pid)
    assert row['state'] == 'quoting' and row['checkout_dispatch_key'] and row['claimed_by'] is None
    return pid, row


async def _parked_observed():
    """Reap named a checkout but the worker lost its lease before recording it on the row."""
    pid = await _quoting()
    row = await _get(pid)
    key = await continuation.begin_dispatch(row, 'w1', quote_id=QUOTE_200['id'],
        enrollment_id=ENROLLMENT_ACTIVE['id'], enrollment_row_id=row['enrollment_id'], total=4500, expires=None)
    assert key
    await continuation.record_dispatch_response(row, 'w1', key=key, quote_id=QUOTE_200['id'],
        enrollment_id=ENROLLMENT_ACTIVE['id'], checkout=_ok(CHECKOUT_CREATED))
    await database.execute("UPDATE reap_agentic_purchases SET claimed_by=NULL, claimed_at=NULL WHERE id=:id", {'id': pid})
    await _age_started(pid, dispatch_settle_seconds() + 60)
    row = await _get(pid)
    assert row['state'] == 'quoting' and row['checkout_dispatch_key'] == key and row['reap_checkout_id'] is None
    return pid, row


async def _resolve(row, outcome, dry_run=False, **over):
    args = dict(dispatch_key=row['checkout_dispatch_key'], outcome=outcome, evidence=_evidence(),
                operator_ref='operator_test', expected_updated_at=row['updated_at'], dry_run=dry_run)
    args.update(over)
    return await resolve_parked_dispatch(row['id'], **args)


async def test_the_existing_tool_refuses_a_parked_row(reap):
    pid, row = await _parked_unknown(reap)
    evidence = _evidence(source='authenticated_reap_checkout_read', payload=dict(CHECKOUT_COMPLETED, status='FAILED'))
    # It needs a stored checkout id to match, and a parked create has none.
    with pytest.raises(ManualResolutionRefused, match='evidence_checkout_mismatch'):
        await resolve_checkout_manually(pid, evidence=evidence, operator_ref='operator_test',
                                        expected_updated_at=row['updated_at'], dry_run=False)


async def test_checkout_found_from_the_journal_hands_the_row_to_the_poller(reap):
    pid, row = await _parked_observed()
    assert await ledger.count_checkout_needs_human() == 1
    reap.calls.clear()
    preview = await _resolve(row, 'checkout_found', dry_run=True)
    assert preview == {'status': 'eligible', 'outcome': 'checkout_found', 'state': 'quoting',
                       'proposed_state': 'awaiting_approval', 'dry_run': True}
    assert await _get(pid) == row and await _audits(pid) == []
    result = await _resolve(row, 'checkout_found')
    assert result == {'status': 'resolved', 'outcome': 'checkout_found', 'state': 'awaiting_approval', 'dry_run': False}
    after = await _get(pid)
    assert after['reap_checkout_id'] == CHECKOUT_CREATED['id'] and after['claimed_by'] is None
    assert after['hosted_url'] is None and after['hosted_url_expires_at'] is None
    assert after['last_error_code'] == 'checkout_unresolvable:3:checkout_no_hosted_action'
    assert after['next_poll_at'] <= datetime.now(timezone.utc) + timedelta(seconds=1)
    resolved = [e for e in await _events(pid) if e['event_type'] == 'resolved']
    assert [(e['checkout_id'], e['provider_code']) for e in resolved] == [(CHECKOUT_CREATED['id'], 'OPERATOR_CHECKOUT_FOUND')]
    [audit] = await _audits(pid)
    assert (audit['outcome'], audit['checkout_id_source'], audit['resolved_state'], audit['reap_checkout_id']) == \
        ('checkout_found', 'journal_observed', 'awaiting_approval', CHECKOUT_CREATED['id'])
    assert audit['dispatch_key'] == row['checkout_dispatch_key'] and len(audit['evidence_sha256']) == 64
    assert reap.calls == [] and await list_parked_dispatches() == []
    # The worker's ordinary read decides the outcome; an unapproved checkout is FAILED at Reap.
    reap.get_checkout = _ok(dict(CHECKOUT_COMPLETED, status='FAILED', orderId=None))
    await _step(pid)
    assert (await _get(pid))['state'] == 'failed'
    assert reap.named('get_checkout') == [{'id': CHECKOUT_CREATED['id']}]
    assert reap.named('create_checkout') == []
    assert await ledger.count_checkout_needs_human() == 0


async def test_checkout_found_with_an_operator_supplied_id(reap):
    pid, row = await _parked_unknown(reap)
    with pytest.raises(ManualResolutionRefused, match='checkout_id_required'):
        await _resolve(row, 'checkout_found')
    for bad in ('chk/../x', 'https://pay.prava.space/checkout/chk_1', 7, ''):
        with pytest.raises(ManualResolutionRefused, match='checkout_id_invalid'):
            await _resolve(row, 'checkout_found', checkout_id=bad)
    result = await _resolve(row, 'checkout_found', checkout_id='chk_found_1')
    assert result['state'] == 'awaiting_approval'
    after = await _get(pid)
    assert after['reap_checkout_id'] == 'chk_found_1' and after['claimed_by'] is None
    [audit] = await _audits(pid)
    assert audit['checkout_id_source'] == 'operator_supplied'
    assert [e['checkout_id'] for e in await _events(pid) if e['event_type'] == 'resolved'] == ['chk_found_1']
    assert not [e for e in await _events(pid) if e['event_type'] == 'observed']


async def test_a_supplied_id_that_contradicts_the_journal_is_refused(reap):
    pid, row = await _parked_observed()
    with pytest.raises(ManualResolutionRefused, match='checkout_id_conflicts_with_observed'):
        await _resolve(row, 'checkout_found', checkout_id='chk_other')
    with pytest.raises(ManualResolutionRefused, match='observed_checkout_exists'):
        await _resolve(row, 'confirmed_not_created')
    assert await _get(pid) == row and await _audits(pid) == []
    assert {e['event_type'] for e in await _events(pid)} == {'started', 'observed'}
    # The same id the journal holds is accepted, and recorded as journal evidence.
    await _resolve(row, 'checkout_found', checkout_id=CHECKOUT_CREATED['id'])
    assert (await _audits(pid))[0]['checkout_id_source'] == 'journal_observed'


async def test_confirmed_not_created_releases_through_the_existing_fence_and_flow_resumes(reap):
    pid, row = await _parked_unknown(reap)
    assert await ledger.count_checkout_needs_human() == 1
    with pytest.raises(ManualResolutionRefused, match='checkout_id_not_allowed'):
        await _resolve(row, 'confirmed_not_created', checkout_id='chk_x')
    reap.calls.clear()
    result = await _resolve(row, 'confirmed_not_created')
    assert result == {'status': 'resolved', 'outcome': 'confirmed_not_created', 'state': 'quoting', 'dry_run': False}
    after = await _get(pid)
    assert after['state'] == 'quoting' and after['checkout_dispatch_key'] is None and after['claimed_by'] is None
    assert after['last_error_code'] == 'checkout_dispatch_not_created'
    assert continuation.dispatch_state(after) == 'not_dispatched'
    events = {e['event_type']: e for e in await _events(pid)}
    assert set(events) == {'started', 'not_created', 'resolved'}
    assert events['not_created']['provider_code'] == events['resolved']['provider_code'] == 'OPERATOR_CONFIRMED_NOT_CREATED'
    [audit] = await _audits(pid)
    assert (audit['outcome'], audit['reap_checkout_id'], audit['resolved_state']) == ('confirmed_not_created', None, 'quoting')
    assert reap.calls == [] and await ledger.count_checkout_needs_human() == 0
    # Normal flow resumes: a fresh quote is a new dispatch key, created exactly once.
    reap.request_quote = _ok(dict(QUOTE_200, id='q_fresh_2'))
    reap.create_checkout = _ok(CHECKOUT_CREATED)
    await _step(pid)
    resumed = await _get(pid)
    assert resumed['state'] == 'awaiting_approval' and resumed['reap_checkout_id'] == CHECKOUT_CREATED['id']
    assert resumed['checkout_dispatch_key'] not in (None, row['checkout_dispatch_key'])
    assert len(reap.named('create_checkout')) == 1


async def test_a_live_claim_or_stale_timestamp_refuses_and_writes_nothing(reap):
    pid, row = await _parked_unknown(reap)
    await _claim(pid, 'w9')
    claimed = await _get(pid)
    for outcome, extra in (('confirmed_not_created', {}), ('checkout_found', {'checkout_id': 'chk_1'})):
        with pytest.raises(ManualResolutionRefused, match='stale_or_claimed_purchase'):
            await _resolve(claimed, outcome, **extra)
    await database.execute("UPDATE reap_agentic_purchases SET claimed_by=NULL, claimed_at=NULL WHERE id=:id", {'id': pid})
    with pytest.raises(ManualResolutionRefused, match='stale_or_claimed_purchase'):
        await _resolve(row, 'confirmed_not_created', expected_updated_at=row['updated_at'] - timedelta(seconds=5))
    assert (await _get(pid))['checkout_dispatch_key'] == row['checkout_dispatch_key']
    assert await _audits(pid) == [] and {e['event_type'] for e in await _events(pid)} == {'started'}


async def test_a_worker_claim_after_the_read_loses_the_compare_and_swap(reap, monkeypatch):
    pid, row = await _parked_unknown(reap)
    await _claim(pid, 'w_race')  # claims without moving updated_at: only the claim conjunct refuses
    assert (await _get(pid))['updated_at'] == row['updated_at']
    real = ledger.get_purchase_internal
    reads = []

    async def stale_first_read(purchase_id):
        reads.append(purchase_id)
        return row if len(reads) == 1 else await real(purchase_id)
    monkeypatch.setattr(ledger, 'get_purchase_internal', stale_first_read)
    with pytest.raises(ManualResolutionRefused, match='compare_and_swap_lost'):
        await _resolve(row, 'confirmed_not_created')
    monkeypatch.setattr(ledger, 'get_purchase_internal', real)
    after = await _get(pid)
    assert after['claimed_by'] == 'w_race' and after['checkout_dispatch_key'] == row['checkout_dispatch_key']
    assert await _audits(pid) == [] and {e['event_type'] for e in await _events(pid)} == {'started'}


async def test_only_parked_dispatch_rows_are_eligible(reap):
    pid = await _quoting('bref_bob')
    fresh = await _get(pid)
    with pytest.raises(ManualResolutionRefused, match='parked_dispatch_required'):
        await _resolve(dict(fresh, checkout_dispatch_key='a' * 64), 'confirmed_not_created')
    with pytest.raises(ManualResolutionRefused, match='dispatch_key_invalid'):
        await _resolve(dict(fresh, checkout_dispatch_key='not-a-key'), 'confirmed_not_created')
    with pytest.raises(ManualResolutionRefused, match='outcome_invalid'):
        await _resolve(dict(fresh, checkout_dispatch_key='a' * 64), 'failed')
    parked_pid, parked = await _parked_unknown(reap)
    with pytest.raises(ManualResolutionRefused, match='parked_dispatch_required'):
        await _resolve(dict(parked, checkout_dispatch_key='b' * 64), 'confirmed_not_created')
    # A legacy row (no tracking version) is listed for an operator but never resolved here.
    await database.execute("UPDATE reap_agentic_purchases SET dispatch_tracking_version=NULL WHERE id=:id", {'id': parked_pid})
    legacy = await _get(parked_pid)
    with pytest.raises(ManualResolutionRefused, match='parked_dispatch_required'):
        await _resolve(legacy, 'confirmed_not_created')
    assert [p['classification'] for p in await list_parked_dispatches()] == ['legacy_unknown']
    # A checkout-backed row belongs to the existing tool, not this one.
    await database.execute("UPDATE reap_agentic_purchases SET dispatch_tracking_version=1, state='awaiting_approval',"
                           " reap_checkout_id='chk_1' WHERE id=:id", {'id': parked_pid})
    with pytest.raises(ManualResolutionRefused, match='parked_dispatch_required'):
        await _resolve(await _get(parked_pid), 'checkout_found', checkout_id='chk_1')
    assert await _audits(parked_pid) == [] and await _audits(pid) == []


async def test_unverified_evidence_refuses_before_any_write(reap):
    pid, row = await _parked_unknown(reap)
    for evidence, code in ((_evidence(authoritative_verified=False), 'authoritative_evidence_required'),
                           (_evidence(source='agent_said_so'), 'authoritative_evidence_required'),
                           (_evidence(observed_at=datetime.now(timezone.utc) - timedelta(days=2)), 'evidence_time_invalid'),
                           (_evidence(provider_base_url='https://api.reap.global'), 'evidence_provider_origin_mismatch')):
        with pytest.raises(ManualResolutionRefused, match=code):
            await _resolve(row, 'confirmed_not_created', evidence=evidence)
    with pytest.raises(ManualResolutionRefused, match='operator_reference_invalid'):
        await _resolve(row, 'confirmed_not_created', operator_ref='ada@example.test')
    assert await _get(pid) == row and await _audits(pid) == []


@pytest.mark.parametrize('outcome,extra', [('confirmed_not_created', {}), ('checkout_found', {'checkout_id': 'chk_found_1'})])
async def test_an_exact_replay_is_read_only_and_changed_evidence_refuses(reap, outcome, extra):
    pid, row = await _parked_unknown(reap)
    evidence = _evidence()
    first = await _resolve(row, outcome, evidence=evidence, **extra)
    after, events, audits = await _get(pid), await _events(pid), await _audits(pid)
    replay = await _resolve(row, outcome, evidence=evidence, **extra)
    assert replay['status'] == 'already_resolved' and replay['state'] == first['state']
    assert await _get(pid) == after and await _events(pid) == events and await _audits(pid) == audits
    assert (await _resolve(row, outcome, evidence=evidence))['status'] == 'already_resolved'
    for change in ({'operator_ref': 'operator_other'}, {'evidence': _evidence(reference='other_case')}):
        with pytest.raises(ManualResolutionRefused, match='dispatch_already_resolved'):
            await _resolve(row, outcome, **{'evidence': evidence, **extra, **change})
    other = 'checkout_found' if outcome == 'confirmed_not_created' else 'confirmed_not_created'
    with pytest.raises(ManualResolutionRefused, match='dispatch_already_resolved'):
        await _resolve(row, other, evidence=evidence, **({'checkout_id': 'chk_found_1'} if other == 'checkout_found' else {}))
    assert len(await _audits(pid)) == 1


async def test_an_audit_failure_rolls_back_the_whole_decision(reap, monkeypatch):
    pid, row = await _parked_unknown(reap)
    original = database.execute

    async def failing(query, *args, **kwargs):
        if 'INSERT INTO reap_checkout_dispatch_resolution_audit' in str(query):
            raise RuntimeError('synthetic_audit_failure')
        return await original(query, *args, **kwargs)
    monkeypatch.setattr(database, 'execute', failing)
    with pytest.raises(RuntimeError, match='synthetic_audit_failure'):
        await _resolve(row, 'confirmed_not_created')
    monkeypatch.setattr(database, 'execute', original)
    assert await _get(pid) == row and await _audits(pid) == []
    assert {e['event_type'] for e in await _events(pid)} == {'started'}


async def test_the_listing_shows_identifiers_and_age_without_pii_or_urls(reap):
    unknown_pid, unknown = await _parked_unknown(reap)
    listing = await list_parked_dispatches()
    [entry] = listing
    assert entry['purchase_id'] == unknown_pid and entry['classification'] == 'dispatch_started'
    assert entry['dispatch_key'] == unknown['checkout_dispatch_key'] and entry['quote_id'] == QUOTE_200['id']
    assert entry['reap_enrollment_id'] == ENROLLMENT_ACTIVE['id'] and entry['observed_checkout_ids'] == []
    assert [e['event_type'] for e in entry['events']] == ['started'] and entry['claimed'] is False
    assert isinstance(entry['age_seconds'], int) and 0 <= entry['age_seconds'] < 600
    assert entry['updated_at'] == unknown['updated_at']
    assert entry['earliest_resolution_at'].tzinfo is not None
    assert entry['earliest_resolution_at'] <= datetime.now(timezone.utc) < entry['earliest_resolution_at'] + timedelta(seconds=600)
    result = await _resolve(dict(unknown, updated_at=entry['updated_at']), 'confirmed_not_created', dry_run=True)
    text = json.dumps([listing, result], default=str)
    assert all(value not in text for value in PII) and 'http' not in text and 'bref_' not in text


async def test_an_existing_journal_widens_to_accept_operator_events():
    if IS_POSTGRES:
        await database.execute("ALTER TABLE reap_checkout_dispatch_events DROP CONSTRAINT reap_checkout_dispatch_events_event_type_check,"
                               " ADD CONSTRAINT reap_checkout_dispatch_events_event_type_check CHECK (event_type IN ('started','not_created','observed')) NOT VALID")
    else:
        await database.execute('DROP TABLE reap_checkout_dispatch_events')
        await database.execute(continuation._SCHEMA.replace(",'resolved','superseded'", ''))
        await database.execute(continuation._IMMUTABLE_UPDATE_SQLITE)
        await database.execute(continuation._IMMUTABLE_DELETE_SQLITE)
    values = {'id': 'pre257_row', 'key': 'c' * 64, 'quote': 'q', 'enrollment': 'e', 'checkout': None, 'code': None}
    await database.execute(continuation._APPEND, {**values, 'event': 'started'})
    with pytest.raises(Exception):
        await database.execute(continuation._APPEND, {**values, 'event': 'resolved'})
    await continuation.ensure_continuation_schema()
    await continuation.ensure_continuation_schema()  # idempotent on the second boot
    await database.execute(continuation._APPEND, {**values, 'event': 'resolved'})
    await database.execute(continuation._APPEND, {**values, 'event': 'superseded'})
    kept = await database.fetch_all("SELECT event_type FROM reap_checkout_dispatch_events WHERE purchase_id='pre257_row' ORDER BY event_type")
    assert [r['event_type'] for r in kept] == ['resolved', 'started', 'superseded']
    with pytest.raises(Exception):
        await database.execute("DELETE FROM reap_checkout_dispatch_events WHERE purchase_id='pre257_row'")
    with pytest.raises(Exception):
        await database.execute("INSERT INTO reap_checkout_dispatch_events (purchase_id,dispatch_key,event_type,quote_id,enrollment_id)"
                               " VALUES ('pre257_row','d','invented','q','e')")


# ── review of #2511 ──────────────────────────────────────────────────────────────────────────


async def test_the_settle_window_spans_the_longest_lease_and_the_client_timeout_ceiling():
    from jobs.reap_agentic_purchase_poll import DIALS
    assert dispatch_settle_seconds() == DIALS['lease_seconds'].maximum + int(svc.rc._MAX_ENV_TIMEOUT_S) + 600


@pytest.mark.parametrize('outcome,extra', [('confirmed_not_created', {}), ('checkout_found', {'checkout_id': 'chk_found_1'})])
async def test_no_outcome_while_the_original_create_may_still_be_in_flight(reap, outcome, extra):
    pid, row = await _parked_unknown(reap, settled=False)
    for age in (0, dispatch_settle_seconds() - 60):
        if age:
            await _age_started(pid, age)
        with pytest.raises(ManualResolutionRefused, match='dispatch_may_still_be_in_flight'):
            await _resolve(row, outcome, dry_run=True, **extra)
        with pytest.raises(ManualResolutionRefused, match='dispatch_may_still_be_in_flight'):
            await _resolve(row, outcome, **extra)
    assert (await list_parked_dispatches())[0]['earliest_resolution_at'] > datetime.now(timezone.utc)
    assert await _get(pid) == row and await _audits(pid) == []
    await _age_started(pid, 120)  # now just past the bound
    assert (await _resolve(row, outcome, **extra))['status'] == 'resolved'


async def test_the_compare_and_swap_enforces_the_settle_window_itself(reap, monkeypatch):
    pid, row = await _parked_unknown(reap, settled=False)

    async def lying_precheck(purchase_id, dispatch_key):
        return datetime.now(timezone.utc) - timedelta(hours=1), True
    monkeypatch.setattr(recovery, '_earliest_allowed', lying_precheck)
    with pytest.raises(ManualResolutionRefused, match='compare_and_swap_lost'):
        await _resolve(row, 'confirmed_not_created')
    assert await _get(pid) == row and await _audits(pid) == []


async def test_evidence_gathered_before_the_window_closed_is_refused(reap):
    pid, row = await _parked_unknown(reap, settled=False)
    await _age_started(pid, dispatch_settle_seconds() + 600)
    await database.execute("UPDATE reap_agentic_purchases SET created_at = :t WHERE id=:id",
                           {'t': ledger._bind_dt(datetime.now(timezone.utc) - timedelta(seconds=2 * dispatch_settle_seconds())), 'id': pid})
    row = await _get(pid)
    early = _evidence(observed_at=datetime.now(timezone.utc) - timedelta(seconds=700))
    with pytest.raises(ManualResolutionRefused, match='evidence_predates_dispatch_settlement'):
        await _resolve(row, 'confirmed_not_created', evidence=early)
    assert (await _resolve(row, 'confirmed_not_created'))['status'] == 'resolved'


async def test_reviewer_two_checkout_scenario_a_late_create_cannot_be_raced_by_not_created(reap):
    """The late row's POST lands after the requeue; the operator must not release it at age 0."""
    pid, row = await _parked_unknown(reap, settled=False)
    original = row
    assert len(reap.named('create_checkout')) == 1
    with pytest.raises(ManualResolutionRefused, match='dispatch_may_still_be_in_flight'):
        await _resolve(row, 'confirmed_not_created')
    # The original POST now answers: Reap created chk_A for the old key. The receipt is unfenced.
    await continuation.record_dispatch_response(original, 'w1', key=row['checkout_dispatch_key'], quote_id=QUOTE_200['id'],
        enrollment_id=ENROLLMENT_ACTIVE['id'], checkout=_ok(CHECKOUT_CREATED))
    reap.request_quote = _ok(dict(QUOTE_200, id='q_fresh_2'))
    reap.create_checkout = _ok(dict(CHECKOUT_CREATED, id='chk_B'))
    await _step(pid)
    assert len(reap.named('create_checkout')) == 1, 'a second checkout was created'
    assert (await _get(pid))['state'] == 'quoting' and await ledger.count_checkout_needs_human() == 1
    await _age_started(pid, dispatch_settle_seconds() + 60)
    row = await _get(pid)
    with pytest.raises(ManualResolutionRefused, match='observed_checkout_exists'):
        await _resolve(row, 'confirmed_not_created')
    await _resolve(row, 'checkout_found')
    assert (await _get(pid))['reap_checkout_id'] == CHECKOUT_CREATED['id']


async def test_a_late_observed_receipt_lets_checkout_found_supersede_not_created(reap):
    pid, row = await _parked_unknown(reap)
    await _resolve(row, 'confirmed_not_created')
    assert await list_parked_dispatches() == []
    await continuation.record_dispatch_response(row, 'w1', key=row['checkout_dispatch_key'], quote_id=QUOTE_200['id'],
        enrollment_id=ENROLLMENT_ACTIVE['id'], checkout=_ok(CHECKOUT_CREATED))
    reparked = await _get(pid)
    assert reparked['checkout_dispatch_key'] == row['checkout_dispatch_key'] and reparked['state'] == 'quoting'
    assert [p['purchase_id'] for p in await list_parked_dispatches()] == [pid]
    with pytest.raises(ManualResolutionRefused, match='observed_checkout_exists'):
        await _resolve(reparked, 'confirmed_not_created', evidence=_evidence(reference='other_case'))
    result = await _resolve(reparked, 'checkout_found')
    assert result['state'] == 'awaiting_approval' and result['supersedes'] == 'confirmed_not_created'
    after = await _get(pid)
    assert after['reap_checkout_id'] == CHECKOUT_CREATED['id'] and await list_parked_dispatches() == []
    audits = {a['outcome']: a for a in await _audits(pid)}
    assert set(audits) == {'confirmed_not_created', 'checkout_found'}
    assert (audits['checkout_found']['supersedes_outcome'], audits['checkout_found']['decision_seq']) == ('confirmed_not_created', 2)
    assert (audits['confirmed_not_created']['supersedes_outcome'], audits['confirmed_not_created']['decision_seq']) == (None, 1)
    events = [e['event_type'] for e in await _events(pid)]
    assert events.count('resolved') == 1 and events.count('superseded') == 1


async def test_checkout_found_without_a_late_receipt_cannot_overturn_not_created(reap):
    pid, row = await _parked_unknown(reap)
    await _resolve(row, 'confirmed_not_created')
    await database.execute("UPDATE reap_agentic_purchases SET checkout_dispatch_key=:k WHERE id=:id",
                           {'k': row['checkout_dispatch_key'], 'id': pid})
    with pytest.raises(ManualResolutionRefused, match='dispatch_already_resolved'):
        await _resolve(await _get(pid), 'checkout_found', checkout_id='chk_found_1')


async def test_an_authenticated_read_must_name_this_checkout_and_this_quote(reap):
    pid, row = await _parked_unknown(reap)
    read = dict(source='authenticated_reap_checkout_read')
    for payload in (None, {'id': 'chk_found_1'}, {'id': 'chk_found_1', 'quoteId': 'other_quote'},
                    {'id': 'chk_other', 'quoteId': QUOTE_200['id']}):
        with pytest.raises(ManualResolutionRefused, match='evidence_checkout_not_bound_to_dispatch'):
            await _resolve(row, 'checkout_found', checkout_id='chk_found_1', evidence=_evidence(**read, payload=payload))
    assert await _audits(pid) == []
    bound = _evidence(**read, payload={'id': 'chk_found_1', 'quoteId': QUOTE_200['id'], 'status': 'FAILED'})
    assert (await _resolve(row, 'checkout_found', checkout_id='chk_found_1', evidence=bound))['status'] == 'resolved'
    assert (await _resolve(row, 'checkout_found', checkout_id='chk_found_1', evidence=bound))['status'] == 'already_resolved'


async def test_completed_on_an_operator_found_checkout_goes_to_a_human(reap, attribution):
    pid, row = await _parked_observed()
    await _resolve(row, 'checkout_found')
    reap.get_checkout = _ok(CHECKOUT_COMPLETED)
    await _step(pid)
    after = await _get(pid)
    assert after['state'] == 'awaiting_approval' and after['claimed_by'] is None
    assert after['last_error_code'] == svc.OPERATOR_FOUND_COMPLETED
    assert attribution.calls == [] and await ledger.count_checkout_needs_human() == 1


async def test_a_checkout_id_held_by_another_purchase_is_a_named_refusal(reap, monkeypatch):
    other = await _quoting('bref_bob')
    await database.execute("UPDATE reap_agentic_purchases SET state='awaiting_approval', reap_checkout_id='chk_dup' WHERE id=:id", {'id': other})
    pid, row = await _parked_unknown(reap)
    with pytest.raises(ManualResolutionRefused, match='checkout_id_attached_to_another_purchase'):
        await _resolve(row, 'checkout_found', checkout_id='chk_dup')
    # The unique index is the backstop when the pre-check races.
    original = database.fetch_val

    async def blind(query, *args, **kwargs):
        if 'WHERE reap_checkout_id=:checkout AND id<>:id' in str(query):
            return None
        return await original(query, *args, **kwargs)
    monkeypatch.setattr(database, 'fetch_val', blind)
    with pytest.raises(ManualResolutionRefused, match='checkout_id_attached_to_another_purchase'):
        await _resolve(row, 'checkout_found', checkout_id='chk_dup')
    monkeypatch.setattr(database, 'fetch_val', original)
    assert await _get(pid) == row and await _audits(pid) == []
    assert {e['event_type'] for e in await _events(pid)} == {'started'}


async def test_a_same_quote_re_park_after_not_created_can_be_released_once_more(reap):
    pid, row = await _parked_unknown(reap)
    first = _evidence()
    await _resolve(row, 'confirmed_not_created', evidence=first)
    creates = len(reap.named('create_checkout'))
    await _step(pid)  # Reap replays the same quote id: same key, re-fenced, nothing sent
    reparked = await _get(pid)
    assert reparked['checkout_dispatch_key'] == row['checkout_dispatch_key'] and reparked['state'] == 'quoting'
    assert len(reap.named('create_checkout')) == creates and await ledger.count_checkout_needs_human() == 1
    assert (await _resolve(reparked, 'confirmed_not_created', evidence=first))['status'] == 'already_resolved'
    with pytest.raises(ManualResolutionRefused, match='dispatch_already_resolved'):
        await _resolve(reparked, 'checkout_found', checkout_id='chk_found_1', evidence=_evidence(reference='case_2'))
    second = _evidence(reference='case_2')
    result = await _resolve(reparked, 'confirmed_not_created', evidence=second)
    assert result['status'] == 'resolved' and result['supersedes'] == 'confirmed_not_created'
    after = await _get(pid)
    assert after['checkout_dispatch_key'] is None and await ledger.count_checkout_needs_human() == 0
    audits = sorted(await _audits(pid), key=lambda a: a['decision_seq'])
    assert [(a['outcome'], a['decision_seq'], a['supersedes_outcome']) for a in audits] == \
        [('confirmed_not_created', 1, None), ('confirmed_not_created', 2, 'confirmed_not_created')]
    assert [e['event_type'] for e in await _events(pid)].count('superseded') == 1
    assert (await _resolve(reparked, 'confirmed_not_created', evidence=second))['status'] == 'already_resolved'
    await _step(pid)  # replayed again: one supersession per key, then engineering
    with pytest.raises(ManualResolutionRefused, match='dispatch_resolution_limit_reached'):
        await _resolve(await _get(pid), 'confirmed_not_created', evidence=_evidence(reference='case_3'))


async def test_the_age_bound_runs_from_the_purchases_latest_started(reap):
    pid, row = await _parked_unknown(reap)
    await database.execute(continuation._APPEND, {'id': pid, 'key': 'e' * 64, 'event': 'started', 'quote': 'q_later',
                                                  'enrollment': ENROLLMENT_ACTIVE['id'], 'checkout': None, 'code': None})
    with pytest.raises(ManualResolutionRefused, match='dispatch_may_still_be_in_flight'):
        await _resolve(row, 'confirmed_not_created')
    assert (await list_parked_dispatches())[0]['earliest_resolution_at'] > datetime.now(timezone.utc)
