"""Real worker lines and executable installer receipts; no cloud writes or email."""
import pytest
from test_reap_rail_alerts import (source, _filter, matches, _entry, worker_log, reap_policies,
                                  _threshold, _db, _env, _no_network, reap, attribution,
                                  _start, _park, _run, ledger, job, IS_POSTGRES)
pytestmark=pytest.mark.skipif(IS_POSTGRES, reason='real SQLite worker; filters dialect independent')
QUEUES=[('checkout_needs_human','REAP_POLL_HUMAN_FILTER','human','checkout needs human reconciliation'),
        ('contact_retention_blocked','REAP_POLL_CONTACT_FILTER','contact','buyer contact retention blocked')]

@pytest.mark.parametrize('field,var,metric,label',QUEUES)
@pytest.mark.parametrize('count,hit',[(0,False),(-1,False),(1,True),(12,True)])
def test_filter_exact_queue_and_worker(source,field,var,metric,label,count,hit):
    log_filter=_filter(source,var)
    line=f'reap_agentic_poll: PollReport({field}={count}, errors=0)'
    assert matches(log_filter,_entry(line)) is hit
    assert not matches(log_filter,_entry(line,service='web'))
    assert not matches(log_filter,_entry(line,resource_type='cloud_run_job'))
    assert not matches(log_filter,_entry(f'reap_agentic_poll: {field}=1'))
    other='contact_retention_blocked' if field=='checkout_needs_human' else 'checkout_needs_human'
    assert not matches(log_filter,_entry(f'reap_agentic_poll: PollReport({other}=1, {field}=0, errors=0)'))

@pytest.mark.parametrize('field,var,metric,label',QUEUES)
def test_policy_generator_owns_queue_and_verified_channel(source,field,var,metric,label):
    policy=reap_policies()['prod: Reap '+label]
    t=_threshold(policy)
    assert t['filter']==f'metric.type="logging.googleapis.com/user/reap_agentic_poll_{metric}" AND resource.type="cloud_run_revision"'
    assert t['duration']=='0s' and t['thresholdValue']==0
    assert t['aggregations'][0]['alignmentPeriod']=='900s'
    assert policy['notificationChannels']==['projects/p/notificationChannels/1']
    assert 'Do not' in policy['documentation']['content'] or 'do not' in policy['documentation']['content']
    assert f'upsert_log_metric reap_agentic_poll_{metric}' in source

async def test_real_held_worker_report_reaches_both_queues_only(source,reap,monkeypatch):
    monkeypatch.setenv('REAP_AGENTIC_RECONCILE_ENABLED','0')
    human=await _start(buyer_ref='queue_human')
    contact=await _start(buyer_ref='queue_contact')
    await _park(human,'processing',2000,reap_checkout_id='chk_queue',last_error_code='checkout_unresolvable:3:reap_status_404')
    await _park(contact,'quoting',2000,last_error_code='contact_retention_elapsed')
    with worker_log() as lines:
        report=await _run(worker_id='queue-alert-test')
    assert report.checkout_needs_human==1 and report.contact_retention_blocked==1
    assert report.stuck_over_age==0 and report.errors==0 and reap.calls==[]
    for _,var,_,_ in QUEUES:
        assert sum(matches(_filter(source,var),_entry(x)) for x in lines)==1
    assert not any(matches(_filter(source,'REAP_POLL_STUCK_FILTER'),_entry(x)) for x in lines)
    assert not any(matches(_filter(source,'REAP_POLL_FAILING_FILTER'),_entry(x)) for x in lines)

async def test_queue_diagnostic_unavailable_pages_error_never_fabricates_queue(source,reap,monkeypatch):
    monkeypatch.setenv('REAP_AGENTIC_RECONCILE_ENABLED','0')
    async def unavailable():raise RuntimeError('fixture unavailable')
    monkeypatch.setattr(ledger,'count_checkout_needs_human',unavailable)
    with worker_log() as lines:
        report=await _run(worker_id='queue-alert-unavailable')
    assert report.checkout_needs_human==-1 and report.errors==1
    assert not any(matches(_filter(source,'REAP_POLL_HUMAN_FILTER'),_entry(x)) for x in lines)
    assert any(matches(_filter(source,'REAP_POLL_FAILING_FILTER'),_entry(x)) for x in lines)


# ── the policy text an operator reads when the queue pages ─────────────────────────────────────
#
# The contact policy used to say flags cannot restore contact and a "separately reviewed
# procedure" was needed. That predates POST /purchases/{purchase_id}/resume: the owner restores
# contact within the re-entry window, and an unresumed row lapses on its own. Pinned so the
# operator text cannot drift back behind the code.

from pathlib import Path

RUNBOOK = Path(__file__).resolve().parents[1] / 'docs' / 'runbooks' / 'reap_agentic_purchase.md'


def _runbook_headings():
    import re
    return {m.strip() for m in re.findall(r'^#{2,4} (.+)$', RUNBOOK.read_text(encoding='utf-8'), re.M)}


@pytest.mark.parametrize('label,sections', [
    ('checkout needs human reconciliation', ['Checkout reads requiring human reconciliation',
                                             'Audited manual resolution of classified checkout uncertainty',
                                             'Parked checkout create']),
    ('buyer contact retention blocked', ['Contact-paused purchases']),
])
def test_queue_policy_names_runbook_sections_that_exist(label, sections):
    content = reap_policies()['prod: Reap ' + label]['documentation']['content']
    tail = content.split('Runbook: docs/runbooks/reap_agentic_purchase.md, ', 1)[1]
    headings = _runbook_headings()
    for section in sections:
        assert section in tail, section
        assert section in headings, section


def test_contact_policy_describes_owner_resume_and_the_lapse_not_an_operator_procedure():
    content = reap_policies()['prod: Reap buyer contact retention blocked']['documentation']['content']
    for phrase in ('/purchases/{purchase_id}/resume', 'contact_reentry_required=true',
                   'REAP_AGENTIC_CONTACT_REENTRY_WINDOW_SECONDS', 'contact_reentry_lapsed',
                   'needs_enrollment to expired', 'resolving or quoting to failed',
                   'create gate', 'checkout needs human'):
        assert phrase in content, phrase
    for stale in ('Resuming flags cannot restore contact', 'reauthorization', 'independently reviewed'):
        assert stale not in content, stale
    runbook = RUNBOOK.read_text(encoding='utf-8')
    assert 'separately reviewed recovery/contact-reauthorization procedure' not in runbook
    assert 'Restoring buyer contact or restarting a blocked attempt requires separate' not in runbook


def test_needs_human_policy_names_both_cohorts():
    content = reap_policies()['prod: Reap checkout needs human reconciliation']['documentation']['content']
    for phrase in ('Two cohorts', 'checkout_unresolvable', 'permanent checkout read failures',
                   'parked checkout create', 'MAY EXIST', 'legacy quoting rows',
                   'list_parked_dispatches', 'resolve_parked_dispatch', 'resolve_checkout_manually',
                   'python -m jobs.reap_operator', 'list-needs-human', 'resolve-parked', 'resolve-checkout',
                   '--apply needs --operator, --expect-env, --expect-database and --evidence-verified'):
        assert phrase in content, phrase


def test_contact_policy_and_runbook_state_the_window_the_code_runs():
    # Every value from the code: the poller's dial (name, default, bounds), the lapse sweep's
    # anchor column, terminal code and transitions, and the PollReport field.
    from test_reap_agentic_routes_doc import lapse_facts
    dial, code, anchor, transitions = lapse_facts()
    content = reap_policies()['prod: Reap buyer contact retention blocked']['documentation']['content']
    for phrase in (dial.env, f'default {dial.default}', f'measured from {anchor}', code,
                   'no dispatch evidence'):
        assert phrase in content, phrase
    for target in sorted(set(transitions.values())):
        sources = ' or '.join(s for s, t in transitions.items() if t == target)
        assert f'{sources} to {target}' in content, (sources, target)
    runbook = RUNBOOK.read_text(encoding='utf-8')
    row = next(line for line in runbook.splitlines() if line.startswith(f'| `{dial.env}` |'))
    cells = [c.strip() for c in row.strip('|').split('|')]
    assert cells[1] == str(dial.default) and cells[2] == f'{dial.minimum}–{dial.maximum}', cells[:3]
    assert f'measured from `{anchor}`' in cells[3] and f'`{code}`' in cells[3] and 'PollReport' in cells[3]
    section = runbook.split('### Contact-paused purchases', 1)[1].split('\n### ', 1)[0]
    flat = ' '.join(section.split())
    for phrase in (f'`{dial.env}`', f'default {dial.default}', f'{dial.minimum}–{dial.maximum}',
                   f'measured from `{anchor}`', f"'{code}'", 'PollReport', 'dispatch evidence'):
        assert phrase in flat, phrase
    for source, target in transitions.items():
        assert f'`{source}` → `{target}`' in flat, (source, target)
