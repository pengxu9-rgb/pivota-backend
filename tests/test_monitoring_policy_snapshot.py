"""Execute only extracted Python generators; never execute the provisioning shell script."""
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = json.loads((ROOT / 'tests/fixtures/live_monitoring_policies_2026_09_30.json').read_text())
LB_NAME = 'prod: load balancer 5xx'
LB_GROUP = '(project_id, forwarding_rule_name, backend_target_name)'
LB_QUERY = (
    f'sum by {LB_GROUP} (rate(loadbalancing_googleapis_com:https_request_count'
    '{monitored_resource="https_lb_rule",response_code_class="500"}[10m])) / '
    f'sum by {LB_GROUP} (rate(loadbalancing_googleapis_com:https_request_count'
    '{monitored_resource="https_lb_rule"}[10m])) > 0.01'
)
LB_SUBJECT = ('LB 5xx >1%: ${metric_or_resource.label.project_id} / '
              '${metric_or_resource.label.forwarding_rule_name} / '
              '${metric_or_resource.label.backend_target_name}')
LB_DOC = (
    'The external load balancer/backend is returning more than 1% 5xx over a rolling 10-minute window, '
    'sustained for 10 minutes. Project: ${metric_or_resource.label.project_id}; forwarding rule: '
    '${metric_or_resource.label.forwarding_rule_name}; backend: ${metric_or_resource.label.backend_target_name}. '
    'Inspect load-balancer request logs and the corresponding service request/error logs. A revision '
    'that will not start, database saturation, or an unhandled exception can cause this user-visible '
    'failure. An unassigned backend can indicate an edge routing failure.'
)

JOB_FAILING_NAME = 'prod: Cloud Run job failing'
JOB_FAILING_FILTER_BASE = ('metric.type="run.googleapis.com/job/completed_task_attempt_count" AND '
                           'resource.type="cloud_run_job" AND metric.label.result="failed"')
# Ad hoc jobs, 2026-11-02: read from the 30-day failure backtest and the launchers' naming, not guessed.
JOB_FAILING_EXCLUSIONS = [
    ('regex', '.+-[0-9]+'),                           # ends in a number: run_oneoff_job.sh's $JOB_PREFIX-$$-$RANDOM,
                                                      # restore_to_cloudsql.sh's killconns-$$ / verify-import-$$
    ('regex', '.+-20[0-9]{6}([0-9]{6})?(-.+)?'),      # named for its launch date by hand
    ('prefix', 'oneoff-'),                            # run_oneoff_job.sh's default JOB_PREFIX
    ('prefix', 'verify-'),                            # ad hoc verification jobs
    ('prefix', 'eval-'),                              # eval-sql-runner
]
JOB_FAILING_FILTER = JOB_FAILING_FILTER_BASE + ''.join(
    f' AND NOT resource.label.job_name = monitoring.regex.full_match("{v}")' if kind == 'regex'
    else f' AND NOT resource.label.job_name = starts_with("{v}")'
    for kind, v in JOB_FAILING_EXCLUSIONS)
JOB_FAILING_DOC = (
    'A Cloud Run job task attempt FAILED (one failed attempt opens this; the job is named in the incident). '
    'Ad hoc jobs are not watched (names ending -<digits>, as scripts/ops/run_oneoff_job.sh makes '
    'them, names carrying a -20YYMMDD launch date, or names starting oneoff-, verify- or eval-): whoever '
    'launched one owns its failure. '
    'reviews-invitation-send runs every minute and relgraph-sync daily; a persistent failure in either is '
    'otherwise completely silent - relgraph-sync failed every night from 2026-10-22 to 2026-10-31 without a '
    'page, when this policy needed the failure to hold for 5 minutes. Only an execution\'s LAST attempt '
    'counts (a retried attempt is result=retried), so a failed execution opens one incident and a retry '
    'that succeeds opens none. The incident closes about 5 minutes after the failed '
    'attempt; while a job keeps failing it stays open and repeats hourly. Read the execution\'s logs: '
    '`gcloud run jobs executions list --job JOB --region us-west1`. For retailer-ingest-drain, a failure '
    'here is an UNEXPECTED exception (the run is recorded as outcome=error in retailer_ingest_runs); stages '
    'that end held or failed exit 0 and page through their own retailer-ingest policies instead.'
)
RELGRAPH_SUCCESS_NAME = 'prod: relgraph-sync has not succeeded in a day'
RELGRAPH_SUCCESS_QUERY = (
    '((sum(sum_over_time(run_googleapis_com:job_completed_execution_count'
    '{monitored_resource="cloud_run_job",job_name="relgraph-sync",result="succeeded"}[24h])) '
    'or vector(0)) == 0) and on() (hour() < 10)'
)


def reviewed_policy(live):
    """Keep the historical readback intact; permit only the reviewed changes: the LB identity/severity
    change (2026-10-27) and the job-failing duration 0s + hourly renotify + ad hoc exclusions (2026-11-02)."""
    expected = json.loads(json.dumps(live))
    if expected['displayName'] == LB_NAME:
        expected['severity'] = 'ERROR'
        expected['documentation'].update(content=LB_DOC, subject=LB_SUBJECT)
        expected['conditions'][0]['conditionPrometheusQueryLanguage']['query'] = LB_QUERY
    if expected['displayName'] == JOB_FAILING_NAME:
        expected['documentation']['content'] = JOB_FAILING_DOC
        expected['conditions'][0]['conditionThreshold']['duration'] = '0s'
        assert expected['conditions'][0]['conditionThreshold']['filter'] == JOB_FAILING_FILTER_BASE
        expected['conditions'][0]['conditionThreshold']['filter'] = JOB_FAILING_FILTER
        expected['alertStrategy']['notificationChannelStrategy'] = [{
            'notificationChannelNames': expected['notificationChannels'], 'renotifyInterval': '3600s'}]
    return expected


def generated_policies():
    source = (ROOT / 'infra/gcp/setup_monitoring.sh').read_text()
    generators = {}
    for name in ['policy', 'promql_policy']:
        generators[name] = source.split(name + '() {', 1)[1].split("python3 -c '\n", 1)[1].split("' \"$@\"", 1)[0]
    channel = LIVE[0]['notificationChannels'][0]
    result = {}
    # Both writers: `upsert` and `upsert_on_new_metric` (a policy over a log metric this script creates).
    for name, generator, body in re.findall(
            r'upsert(?:_on_new_metric)? "([^"]+)" "\$\((policy|promql_policy) (.*?)\)"', source, re.S):
        args = shlex.split(body.replace('\\\n', '').replace('\\`', '`'))
        proc = subprocess.run([sys.executable, '-c', generators[generator], *args, channel],
                              text=True, capture_output=True, check=True)
        result[name] = json.loads(proc.stdout)
    return result


def clean_condition(condition):
    condition = json.loads(json.dumps(condition))
    condition.pop('name', None)
    if 'conditionThreshold' in condition:
        condition['conditionThreshold'].setdefault('thresholdValue', 0)
    return condition


def test_lb_ratio_retains_backend_identity_and_preserves_threshold_and_wiring():
    live = reviewed_policy(next(p for p in LIVE if p['displayName'] == LB_NAME))
    generated = generated_policies()[live['displayName']]
    assert generated['conditions'] == [clean_condition(c) for c in live['conditions']]
    for key in ['documentation', 'notificationChannels', 'combiner', 'alertStrategy', 'displayName', 'severity']:
        assert generated[key] == live[key]
    # The matching grouping is required on numerator AND denominator. No volume gate or backend
    # filter may hide a low-traffic failure or a failure before a backend is assigned.
    condition = generated['conditions'][0]['conditionPrometheusQueryLanguage']
    assert condition == {'query': LB_QUERY, 'duration': '600s', 'evaluationInterval': '60s'}


def test_relgraph_occupancy_alert_shape_and_current_sizing():
    policy = generated_policies()['prod: relgraph-sync running over five hours']
    condition = policy['conditions'][0]['conditionThreshold']
    assert condition == {
        'filter': 'metric.type="run.googleapis.com/job/running_executions" AND resource.type="cloud_run_job" AND resource.label.job_name="relgraph-sync"',
        'aggregations': [{'alignmentPeriod': '300s', 'perSeriesAligner': 'ALIGN_MAX',
                          'crossSeriesReducer': 'REDUCE_SUM', 'groupByFields': ['resource.label.job_name']}],
        'comparison': 'COMPARISON_GT', 'thresholdValue': 0, 'duration': '18000s', 'trigger': {'count': 1},
    }
    # 2026-10-20: a 10,000-review nightly at concurrency 12 runs ~3.0-4.0 h; five hours warns an hour
    # before the 21600s timeout.
    for phrase in ['concurrency 12', '~3.0-4.0 hours', '300 minutes', '21600s', 'max-retries 1',
                   'continuous occupancy of five hours']:
        assert phrase in policy['documentation']['content']
    assert policy['notificationChannels'] == LIVE[0]['notificationChannels']



def test_one_failed_job_attempt_opens_an_incident():
    """2026-10-22..31: relgraph-sync failed both attempts every night and nobody was paged. A failed
    attempt is ONE sparse 5-minute point; a 300s duration asks it to hold the condition for 300s, which
    it does only on the boundary. Duration 0s: one point opens an incident. Ad hoc jobs are excluded
    (below). Everything else - the 300s ALIGN_SUM window, the job_name grouping, GT 0, autoclose - is
    unchanged."""
    live = next(p for p in LIVE if p['displayName'] == JOB_FAILING_NAME)
    policy = generated_policies()[JOB_FAILING_NAME]
    condition = policy['conditions'][0]['conditionThreshold']
    assert condition['duration'] == '0s'
    assert condition == {**live['conditions'][0]['conditionThreshold'], 'duration': '0s', 'thresholdValue': 0,
                         'filter': JOB_FAILING_FILTER}
    assert condition['aggregations'] == [{'alignmentPeriod': '300s', 'perSeriesAligner': 'ALIGN_SUM',
                                          'crossSeriesReducer': 'REDUCE_SUM',
                                          'groupByFields': ['resource.label.job_name']}]
    assert policy['displayName'] == live['displayName']  # same name: the upsert replaces it in place
    assert policy['notificationChannels'] == live['notificationChannels']
    assert policy['alertStrategy'] == {
        'autoClose': '3600s',
        'notificationChannelStrategy': [{'notificationChannelNames': live['notificationChannels'],
                                         'renotifyInterval': '3600s'}]}


def _excluded(job_name):
    return any(re.fullmatch(v, job_name) if kind == 'regex' else job_name.startswith(v)
               for kind, v in JOB_FAILING_EXCLUSIONS)


# Every Cloud Scheduler trigger live in pivota-prod (us-west1), read back 2026-11-02, by target job.
LIVE_SCHEDULED_JOBS_2026_11_02 = [
    'agent-pdp-orphan-reaper', 'audit-domainless-offer-currency', 'commerce-index-checkout-validation',
    'commerce-index-insight-refresh', 'commerce-index-relgraph', 'commerce-index-search-index',
    'content-canonical-election', 'derive-offer-market-currency', 'external-referral-refresh',
    'external-seed-destination-sweep', 'external-seed-sentinel-nongrowth', 'merchant-purchasability-sweep',
    'pdp-identity-graph-backfill', 'reap-cart-proof-enrichment', 'reap-cart-proof-mirror', 'relgraph-sync',
    'retailer-ingest-drain', 'reviews-invitation-send', 'store-audit-commerce-probe',
    'store-audit-commerce-reprobe-enqueue', 'store-audit-ucp-probe', 'store-audit-ucp-reprobe-enqueue',
    'tierb-cart-link-eligibility',
]


_JOB_SOURCES = [*sorted((ROOT / 'infra/gcp').glob('*.sh')), *sorted((ROOT / 'scripts').rglob('*.sh')),
                *sorted((ROOT / 'scripts').rglob('*.py'))]
_NAME = r'"?([a-z][a-zA-Z0-9$_{}:-]*[a-zA-Z0-9$}])"?'


def _job_names_in_scripts():
    """(literal, templated) job names that infra/gcp/ and scripts/ schedule, create, execute or assign
    to a *JOB* variable, quoted or not. A TEMPLATED name carries a shell expansion ($$, $RANDOM,
    $JOB_PREFIX): an ad hoc job made per run. A literal one is a standing job."""
    found = set()
    for script in _JOB_SOURCES:
        text = script.read_text(errors='replace')
        found |= set(re.findall(r'^\s*sched \S+ "[^"]+" ([a-z0-9-]+)', text, re.M))
        found |= set(re.findall(r'^\s*mk\w*job ([a-z0-9-]+) ', text, re.M))
        found |= set(re.findall(r'^\s*(?:export\s+|local\s+)?[A-Z_]*JOB[A-Z_]*=' + _NAME + r'\s*(?:#.*)?$', text, re.M))
        found |= set(re.findall(r'run jobs (?:create|deploy|update|execute|replace) ' + _NAME + r'(?=\s)', text))
    literal = {n for n in found if '$' not in n}
    templated = {n for n in found if '$' in n and not n.startswith('$')}
    return literal, templated


def _provisioned_job_names():
    return _job_names_in_scripts()[0]


def _instantiate(template):
    """A templated name as it runs: $$ -> a PID, $RANDOM -> a number, ${JOB_PREFIX...} -> its default."""
    name = template.replace('$JOB_PREFIX', 'oneoff').replace('$RANDOM', '4200').replace('$$', '43396')
    assert '$' not in name, template
    return name


def test_the_job_name_parser_finds_quoted_assignments_and_scripts():
    """The parser must see quoted X_JOB="..." assignments and scripts/, not only infra/gcp/ literals."""
    literal, templated = _job_names_in_scripts()
    # quoted, templated, in infra/gcp/ and scripts/ops/
    assert {'killconns-$$', 'verify-import-$$', 'verify-worker-$$-$RANDOM'} <= templated, sorted(templated)
    # literal jobs named only under scripts/ (a backfill job, an ops re-run of a scheduled one)
    assert {'backfill-stale-pdp-overlays', 'agent-pdp-orphan-reaper'} <= literal, sorted(literal)
    # unquoted X_JOB= in infra/gcp/
    assert {'reap-cart-proof-mirror', 'reap-cart-proof-enrichment', 'merchant-purchasability-sweep'} <= literal


def test_ad_hoc_jobs_are_excluded_from_job_failing_and_no_scheduled_job_is():
    """With duration 0s every failed ad hoc job would page, and some exit non-zero on purpose (a
    GATE_CLOSED, a refused migration, an exit-3/4/5 verdict). Their owner is whoever launched them. A
    scheduled job matching an exclusion would fail SILENTLY: the relgraph-sync outage again."""
    condition = generated_policies()[JOB_FAILING_NAME]['conditions'][0]['conditionThreshold']
    assert condition['filter'] == JOB_FAILING_FILTER
    provisioned = _provisioned_job_names()
    for required in ['relgraph-sync', 'reviews-invitation-send', 'retailer-ingest-drain',
                     'external-seed-sentinel-nongrowth', 'merchant-purchasability-sweep',
                     'tierb-cart-link-eligibility', 'reap-cart-proof-mirror', 'store-audit-ucp-probe']:
        assert required in provisioned, required  # the parser still finds the scripts' jobs
    for name in sorted(provisioned | set(LIVE_SCHEDULED_JOBS_2026_11_02)):
        assert not _excluded(name), f'{name} is scheduled or provisioned, and the job-failing policy ignores it'
    # The ad hoc names the 30-day backtest (2026-10-03..11-02) saw fail, one per rule, are excluded.
    for name in ['oneoff-43396-3521', 'census-seeds-43121-1210', 'relgraph-source-probe4-64372-153',
                 'catalog-priority-preview-20261003', 'krave-schema-256-ro-20261004-prod',
                 'verify-lb-ck-20261027-d4d2cc3f', 'eval-sql-runner', 'sgc-census8-20261101130459']:
        assert _excluded(name), name
    # The launcher's naming is what the first rule is derived from.
    launcher = (ROOT / 'scripts/ops/run_oneoff_job.sh').read_text()
    assert 'JOB="$JOB_PREFIX-$$-$RANDOM"' in launcher and 'JOB_PREFIX="${JOB_PREFIX:-oneoff}"' in launcher
    assert _excluded('oneoff-12345-6789') and _excluded('anything-1-2')
    # Every TEMPLATED (per-run, ad hoc) job name in the scripts is excluded once instantiated: killconns-$$
    # (restore_to_cloudsql.sh) matched no rule before 2026-11-02 and would have paged.
    _, templated = _job_names_in_scripts()
    assert 'killconns-$$' in templated
    for template in sorted(templated | {'$JOB_PREFIX-$$-$RANDOM'}):
        assert _excluded(_instantiate(template)), template
    doc = generated_policies()[JOB_FAILING_NAME]['documentation']['content']
    assert 'owns its failure' in doc


def test_the_renotify_argument_leaves_every_other_threshold_policy_as_it_was():
    """`policy` gained an optional RENOTIFY; without it the alert strategy is autoClose alone."""
    for name, policy in generated_policies().items():
        if name in (JOB_FAILING_NAME, RELGRAPH_SUCCESS_NAME) or 'conditionThreshold' not in policy['conditions'][0]:
            continue
        assert set(policy['alertStrategy']) == {'autoClose'}, name


def _relgraph_schedule():
    """(start minute of day UTC, task timeout s, attempts) of relgraph-sync, from setup_scheduler.sh."""
    scheduler = (ROOT / 'infra/gcp/setup_scheduler.sh').read_text()
    (minute, hour), = re.findall(r'^sched relgraph-sync-cron "(\d+) (\d+) \* \* \*" relgraph-sync$', scheduler, re.M)
    job = scheduler.split('\nmkjob relgraph-sync ', 1)[1].split('\n\n', 1)[0]
    (timeout,) = re.findall(r'--task-timeout (\d+)s', job)
    assert '--max-retries' not in job, 'relgraph-sync overrides max-retries; re-derive the gate'
    mkjob = scheduler.split('\nmkjob(){', 1)[1].split('\n}', 1)[0]
    (retries,) = re.findall(r'--max-retries (\d+)', mkjob)
    return int(hour) * 60 + int(minute), int(timeout), int(retries) + 1


def test_relgraph_has_not_succeeded_policy_reads_succeeded_executions_and_pages():
    policy = generated_policies()[RELGRAPH_SUCCESS_NAME]
    assert policy['displayName'] == RELGRAPH_SUCCESS_NAME
    assert policy['notificationChannels'] == LIVE[0]['notificationChannels']
    (condition,) = policy['conditions']
    promql = condition['conditionPrometheusQueryLanguage']
    # A system metric: its descriptor exists, so validation stays ON and catches a typo.
    assert promql == {'query': RELGRAPH_SUCCESS_QUERY, 'duration': '300s', 'evaluationInterval': '60s'}
    query = promql['query']
    assert 'run_googleapis_com:job_completed_execution_count{' in query
    assert 'job_name="relgraph-sync"' in query and 'result="succeeded"' in query
    # No completions in the window = NO SAMPLES (a DELTA), an empty vector rather than 0: without the
    # fallback the policy could never fire on a job that stopped running.
    assert 'or vector(0)) == 0' in query
    assert 'increase(' not in query and 'rate(' not in query
    (strategy,) = policy['alertStrategy']['notificationChannelStrategy']
    assert strategy == {'notificationChannelNames': policy['notificationChannels'], 'renotifyInterval': '14400s'}
    assert policy['alertStrategy']['autoClose'] == '3600s'
    assert policy['documentation']['subject'] == 'relgraph-sync: no successful run in the last 24 hours'
    doc = policy['documentation']['content']
    for phrase in ['closes at 10:00Z WHETHER OR NOT', 'A manual successful run counts', 'snooze']:
        assert phrase in doc


def test_relgraph_has_not_succeeded_gate_holds_exactly_one_night_within_the_look_back_limit():
    """For T in [00:00Z, gate), [T - range, T] must hold ALL of yesterday's run and NONE of the day
    before's, and range + duration must stay within the documented 25 hours."""
    query = RELGRAPH_SUCCESS_QUERY
    (range_h,) = map(int, re.findall(r'\[(\d+)h\]', query))
    (gate_h,) = map(int, re.findall(r'hour\(\) < (\d+)\)', query))
    assert 'offset' not in query
    duration_s = int(generated_policies()[RELGRAPH_SUCCESS_NAME]['conditions'][0]
                     ['conditionPrometheusQueryLanguage']['duration'].rstrip('s'))
    assert range_h * 3600 + duration_s <= 25 * 3600
    # REPO config, not prod's: this assumes what the 2026-11-02 read-only readback confirmed live
    # (`gcloud run jobs describe relgraph-sync`: timeoutSeconds 21600, maxRetries 1; relgraph-sync-cron
    # "37 10 * * *" Etc/UTC, ENABLED). A hand change in prod is not seen here; re-read it before relying on it.
    start_min, timeout_s, attempts = _relgraph_schedule()
    assert (start_min, timeout_s, attempts) == (10 * 60 + 37, 21600, 2)
    latest_success_min = start_min + attempts * timeout_s // 60 + 15  # + container start, retry, metric lag
    day = 24 * 60
    # The latest window start (gate - range, i.e. yesterday at the gate hour) is before yesterday's run.
    assert gate_h * 60 - range_h * 60 + day <= start_min
    # The earliest window start (midnight - range) is after the day-before's last possible success.
    assert latest_success_min - day < 0 - range_h * 60 + day
    assert latest_success_min < day


def test_relgraph_has_not_succeeded_is_prod_only_last_and_written_by_plain_upsert():
    """Staging triggers are created paused; there it would page every morning. A system metric is
    visible to Monitoring at once, so the plain writer is right (no NEW_METRIC wait). It is the LAST
    policy write and after every retirement: hour() in an alert condition is unproven by a create, and a
    refusal aborts the run at check(), which must not leave any other policy unreconciled."""
    source = (ROOT / 'infra/gcp/setup_monitoring.sh').read_text()
    at = source.index(f'upsert "{RELGRAPH_SUCCESS_NAME}"')
    assert source.rindex('\nif [ "$ENV" = prod ]; then\n', 0, at) > source.rindex('\nfi\n', 0, at)
    end = source.index('\nfi\n', at)
    tail = source[end:]
    for writer in ('\nupsert ', '\nupsert_on_new_metric ', '\nretire_policy ', '\nupsert_log_metric '):
        assert writer not in tail, writer
    writes = [m.group(1) for m in re.finditer(r'^(?:upsert|upsert_on_new_metric) "([^"]+)"', source, re.M)]
    assert writes[-1] == RELGRAPH_SUCCESS_NAME


# Fields the API assigns or manages; everything else the upsert's delete+create writes (or wipes).
SERVER_MANAGED = {'name', 'creationRecord', 'mutationRecord'}


def comparable_policy(policy):
    policy = {k: v for k, v in json.loads(json.dumps(policy)).items() if k not in SERVER_MANAGED}
    policy['conditions'] = [clean_condition(c) for c in policy.get('conditions', [])]
    policy.setdefault('enabled', True)  # the API default on create, echoed on read
    return policy


def test_every_field_the_upsert_writes_matches_live_for_every_policy():
    # Whole policy, not a key list: a live severity, userLabels or enabled=false that the generator
    # omits would be wiped by the delete+create, and must fail here first.
    generated = generated_policies()
    for live in LIVE:
        assert comparable_policy(generated[live['displayName']]) == comparable_policy(reviewed_policy(live)), live['displayName']


def test_every_live_policy_is_compared_and_only_reported_drift_remains():
    generated = generated_policies()
    drifts = {}
    for live in LIVE:
        live = reviewed_policy(live)
        name = live['displayName']
        conditions = [clean_condition(c) for c in live['conditions']]
        if conditions != generated[name]['conditions']:
            drifts[name] = (conditions, generated[name]['conditions'])
        for key in ['displayName', 'documentation', 'combiner', 'notificationChannels', 'alertStrategy']:
            assert generated[name][key] == live[key], (name, key)
    assert drifts == {}


def test_purchasability_sweep_ip_throttle_policy_pages_once_per_window():
    """The sweep is HOURLY and a trip exits 0. The policy counts the IP_THROTTLE line's
    `"ip_throttled":true` (compact JSON: no space after the colon), keyed on the job, and aligns over
    twice the cadence so a 14-17 h throttle window is ONE incident, not one page per hour."""
    source = (ROOT / 'infra/gcp/setup_monitoring.sh').read_text()
    assert ('MPS_THROTTLED_FILTER=\'resource.type="cloud_run_job" AND resource.labels.job_name='
            '"merchant-purchasability-sweep" AND textPayload:"IP_THROTTLE " AND '
            'textPayload:"\\"ip_throttled\\":true"\'') in source
    policy = generated_policies()['prod: purchasability sweep IP-throttled']
    condition = policy['conditions'][0]['conditionThreshold']
    assert condition['filter'] == ('metric.type="logging.googleapis.com/user/'
                                   'merchant_purchasability_sweep_ip_throttled" AND resource.type="cloud_run_job"')
    assert condition['aggregations'][0]['alignmentPeriod'] == '7200s'
    assert condition['comparison'] == 'COMPARISON_GT' and condition.get('thresholdValue', 0) == 0
    assert policy['alertStrategy'] == {'autoClose': '7200s'}
    # ITS METRIC IS CREATED BY THIS SCRIPT, so it must go through the waiting writer: Monitoring takes up to
    # 10 minutes to see a new log metric, and on 2026-10-08 the plain `upsert` aborted the first prod run
    # here ("Cannot find metric(s)"), before the policies after it. And the metric comes first.
    assert re.search(r'^upsert_on_new_metric "prod: purchasability sweep IP-throttled"', source, re.M)
    assert not re.search(r'^upsert "prod: purchasability sweep IP-throttled"', source, re.M)
    assert (source.index('upsert_log_metric merchant_purchasability_sweep_ip_throttled ')
            < source.index('\nupsert "'))


def test_the_ip_throttle_filter_matches_the_line_the_sweep_prints():
    """The filter's literal must be a substring of what `ip_throttle_line` + json.dumps really emit."""
    import json as _json
    sys.path.insert(0, str(ROOT))
    import jobs.merchant_purchasability_sweep as sweep

    breaker = sweep.SweepThrottleBreaker(trip_hosts=1, window_seconds=60)
    breaker.observe("a.example", 429, {"retry-after": "60"})
    line = sweep.IP_THROTTLE_PREFIX + _json.dumps(sweep.ip_throttle_line(breaker, {}), separators=(",", ":"), default=str)
    assert line.startswith("IP_THROTTLE ") and '"ip_throttled":true' in line
    quiet = sweep.ip_throttle_line(sweep.SweepThrottleBreaker(), {})
    assert '"ip_throttled":true' not in _json.dumps(quiet, separators=(",", ":"), default=str)
