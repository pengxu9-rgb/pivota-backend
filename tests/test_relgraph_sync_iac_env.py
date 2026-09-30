"""Offline contract checks: never execute the provisioning scripts."""
import json
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_relgraph_reconcile_preserves_live_env_and_phase_one_caps():
    source = (ROOT / 'infra/gcp/setup_scheduler.sh').read_text()
    block = source.split('mkjob relgraph-sync ', 1)[1].split('\necho ', 1)[0]
    env = dict(token.split('=', 1) for token in re.search(r'--set-env-vars "([^"]+)"', block)[1].split(','))
    assert env == {
        'PIVOTA_ENV': '$PIVOTA_ENV', 'PIVOTA_SERVICE_NAME': 'relgraph-sync',
        'PIVOTA_COMMIT_SHA': '$GATEWAY_TAG', 'DB_POOL_MAX': '3', 'PCI_KB_DB_POOL_MAX': '1',
        'INGREDIENT_REFERENCE_DB_POOL_MAX': '1', 'INGREDIENT_SIGNAL_DB_POOL_MAX': '1',
        'RELGRAPH_SYNC_APPLY_BUILD': 'true', 'RELGRAPH_SYNC_APPLY_REVIEW': 'true',
        'RELGRAPH_SYNC_ALLOW_WRITES': 'true', 'RELGRAPH_SYNC_CONFIRM': 'APPLY_RELGRAPH_SYNC_ROUTINE',
        'VERTEX_AI_ENABLED': 'true', 'GOOGLE_CLOUD_PROJECT': 'pivota-prod',
        'GOOGLE_CLOUD_LOCATION': 'global', 'GCE_METADATA_HOST': 'metadata.google.internal',
        'RELGRAPH_SYNC_REVIEW_LIMIT': '1000', 'RELGRAPH_SYNC_REVIEW_CONCURRENCY': '6',
        'RELGRAPH_SYNC_STEP_TIMEOUT_MINUTES': '90',
    }
    assert '--task-timeout 14400s' in block
    assert 'DATABASE_URL=DATABASE_URL_NOVERIFY:latest,PCI_KB_DATABASE_URL=PCI_KB_DATABASE_URL_NOVERIFY:latest' in block


def test_running_duration_policy_uses_existing_json_generator():
    source = (ROOT / 'infra/gcp/setup_monitoring.sh').read_text()
    generator = source.split('policy() {', 1)[1].split("python3 -c '\n", 1)[1].split("' \"$@\"", 1)[0]
    call = source.split('upsert "prod: relgraph-sync running over two hours" "$(policy ', 1)[1].split(')"', 1)[0]
    args = shlex.split(call.replace('\\\n', ''))
    result = subprocess.run(['python3', '-c', generator, *args, 'offline-channel'], capture_output=True, text=True, check=True)
    policy = json.loads(result.stdout)
    condition = policy['conditions'][0]['conditionThreshold']
    assert condition['filter'] == ('metric.type="run.googleapis.com/job/running_executions" '
                                  'AND resource.type="cloud_run_job" AND resource.label.job_name="relgraph-sync"')
    assert condition['duration'] == '7200s'
    assert condition['thresholdValue'] == 0
    assert condition['comparison'] == 'COMPARISON_GT'
    assert condition['aggregations'] == [{
        'alignmentPeriod': '60s', 'perSeriesAligner': 'ALIGN_MAX',
        'crossSeriesReducer': 'REDUCE_SUM', 'groupByFields': ['resource.label.job_name'],
    }]
    assert policy['notificationChannels'] == ['offline-channel']
    assert 'continuous' in policy['documentation']['content']


def test_shell_syntax_only():
    for name in ['setup_scheduler.sh', 'setup_monitoring.sh']:
        subprocess.run(['bash', '-n', str(ROOT / 'infra/gcp' / name)], check=True)
