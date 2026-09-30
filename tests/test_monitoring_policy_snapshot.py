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
    for name, generator, body in re.findall(r'upsert "([^"]+)" "\$\((policy|promql_policy) (.*?)\)"', source, re.S):
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
