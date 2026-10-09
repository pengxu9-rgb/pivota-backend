"""Execute only extracted Python generators; never execute the provisioning shell script."""
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = json.loads((ROOT / 'tests/fixtures/live_monitoring_policies_2026_09_30.json').read_text())


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


def test_lb_ratio_matches_live_condition_and_policy_wiring_exactly():
    live = next(p for p in LIVE if p['displayName'] == 'prod: load balancer 5xx')
    generated = generated_policies()[live['displayName']]
    assert generated['conditions'] == [clean_condition(c) for c in live['conditions']]
    for key in ['documentation', 'notificationChannels', 'combiner', 'alertStrategy', 'displayName']:
        assert generated[key] == live[key]


def test_relgraph_occupancy_alert_shape_and_current_sizing():
    policy = generated_policies()['prod: relgraph-sync running over two hours']
    condition = policy['conditions'][0]['conditionThreshold']
    assert condition == {
        'filter': 'metric.type="run.googleapis.com/job/running_executions" AND resource.type="cloud_run_job" AND resource.label.job_name="relgraph-sync"',
        'aggregations': [{'alignmentPeriod': '300s', 'perSeriesAligner': 'ALIGN_MAX',
                          'crossSeriesReducer': 'REDUCE_SUM', 'groupByFields': ['resource.label.job_name']}],
        'comparison': 'COMPARISON_GT', 'thresholdValue': 0, 'duration': '7200s', 'trigger': {'count': 1},
    }
    for phrase in ['~37 minutes', '45 minutes', '14400s', 'max-retries 1', 'continuous occupancy of two hours']:
        assert phrase in policy['documentation']['content']
    assert policy['notificationChannels'] == LIVE[0]['notificationChannels']


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
        assert comparable_policy(generated[live['displayName']]) == comparable_policy(live), live['displayName']


def test_every_live_policy_is_compared_and_only_reported_drift_remains():
    generated = generated_policies()
    drifts = {}
    for live in LIVE:
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
