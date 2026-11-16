#!/bin/bash
set -euo pipefail

here="$(cd -- "$(dirname -- "$0")" && pwd)"
project=pivota-staging
region=us-west1
account="sa-meitu-evidence-v1@$project.iam.gserviceaccount.com"
image=us-west1-docker.pkg.dev/pivota-shared/pivota/backend@sha256:f969a6d733cd31767a1f93ef7709130f3f1af158264f3d17f869a51ff56d23d6

mode="${1:-}"
case "$mode" in
    launch) manifest="${2:?input manifest JSON required}"; run_dir="${3:?new run directory required}" ;;
    collect) run_dir="${2:?run directory required}" ;;
    *) printf 'Usage: %s launch MANIFEST RUN_DIRECTORY | collect RUN_DIRECTORY\n' "$0" >&2; exit 2 ;;
esac

if [[ "$mode" == launch ]]; then
    mkdir -p -- "$run_dir"
    code="$(python3 - "$here/scan.py" "$here/capture.py" "$manifest" "$run_dir" <<'PY'
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import zlib

source_path, capture_path, manifest_path, run_dir = map(Path, sys.argv[1:])
source = source_path.read_bytes()
capture = capture_path.read_bytes()
compile(source, source_path.name, 'exec')
compile(capture, capture_path.name, 'exec')
data = json.loads(manifest_path.read_bytes())
if data.get('schema_version') != 1 or data.get('market') != 'US':
    raise SystemExit('expected schema v1 US official discovery manifest')
batch = data.get('batch_id') or ''
queries = data.get('queries') or []
if not re.fullmatch(r'[a-z0-9-]{8,75}', batch) or not isinstance(queries, list) or not 1 <= len(queries) <= 100:
    raise SystemExit('expected a stable batch ID and nonempty query list')
lines = sorted(query['line'] for query in queries)
if len(lines) != len(set(lines)) or not all(type(line) is int and line > 0 for line in lines):
    raise SystemExit('expected unique positive Meitu source lines')
payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def compress(raw):
    return base64.b64encode(zlib.compress(raw, level=9)).decode()

job = 'meitu-us-official-rediscovery-' + digest(source + b'\0' + capture + b'\0' + payload)[:12]
program = (
    "import base64,hashlib,io,os,sys,types,zlib;"
    f"assert os.environ.get('CLOUD_RUN_JOB')=={job!r} and os.environ.get('GOOGLE_CLOUD_PROJECT')=='pivota-staging' "
    "and not any(os.getenv(key) for key in ('DATABASE_URL','DIRECT_DATABASE_URL','SURREALDB_URL'));"
    f"source=zlib.decompress(base64.b64decode({compress(source)!r}));"
    f"capture=zlib.decompress(base64.b64decode({compress(capture)!r}));"
    f"manifest=zlib.decompress(base64.b64decode({compress(payload)!r}));"
    f"assert hashlib.sha256(source).hexdigest()=={digest(source)!r} "
    f"and hashlib.sha256(capture).hexdigest()=={digest(capture)!r} "
    f"and hashlib.sha256(manifest).hexdigest()=={digest(payload)!r};"
    "package=types.ModuleType('meitu_discovery_operator');package.__path__=[];sys.modules['meitu_discovery_operator']=package;"
    "module=types.ModuleType('meitu_discovery_operator.capture');module.__file__='/app/capture.py';"
    "sys.modules[module.__name__]=module;__package__=package.__name__;"
    "exec(compile(capture,'capture.py','exec'),module.__dict__);"
    "sys.stdin=io.StringIO(manifest.decode());"
    "__file__='/app/official_rediscover.py';exec(compile(source,'official_rediscover.py','exec'))"
)
if len(program) >= 30000 or '|' in program:
    raise SystemExit('inline job payload exceeds the launcher envelope')
launch = {'job': job, 'batch_id': batch, 'source_sha256': digest(source),
          'capture_sha256': digest(capture),
          'manifest_sha256': digest(payload), 'query_lines': lines}
path = run_dir / 'launch.json'
if path.exists() and json.loads(path.read_bytes()) != launch:
    raise SystemExit('run directory already describes a different discovery batch')
path.write_text(json.dumps(launch, indent=2, sort_keys=True) + '\n')
(run_dir / 'manifest.json').write_bytes(payload)
print(program)
PY
)"
fi

job="$(jq -er .job "$run_dir/launch.json")"
[[ "$job" =~ ^meitu-us-official-rediscovery-[0-9a-f]{12}$ ]]
if [[ "$mode" == launch ]]; then
    if [[ -f "$run_dir/execution.json" ]]; then
        jq -e --arg job "$job" '.metadata.name | startswith($job + "-")' "$run_dir/execution.json" >/dev/null
        cat "$run_dir/execution.json"
        exit 0
    fi
    if ! gcloud run jobs describe "$job" --project="$project" --region="$region" --format='json(spec.template)' \
        > "$run_dir/job.json" 2> "$run_dir/job-describe.log"; then
        gcloud run jobs create "$job" --project="$project" --region="$region" --image="$image" \
            --service-account="$account" --tasks=1 --parallelism=1 --task-timeout=1800s --max-retries=0 \
            --set-env-vars="GOOGLE_CLOUD_PROJECT=$project" --command=python --args="^|^-c|$code" --quiet
        gcloud run jobs describe "$job" --project="$project" --region="$region" --format='json(spec.template)' \
            > "$run_dir/job.json"
    fi
    jq -e --arg image "$image" --arg account "$account" --arg code "$code" --arg project "$project" '
        .spec.template.spec as $execution | $execution.template.spec as $task |
        ($execution.taskCount == 1 and $execution.parallelism == 1) and
        ($task.serviceAccountName == $account and ($task.maxRetries // 0) == 0 and (($task.volumes // []) | length) == 0) and
        ($task.containers | length) == 1 and ($task.containers[0] |
            .image == $image and .command == ["python"] and .args == ["-c", $code] and
            .env == [{"name":"GOOGLE_CLOUD_PROJECT", "value":$project}])
    ' "$run_dir/job.json" >/dev/null
    gcloud run jobs execute "$job" --project="$project" --region="$region" \
        --async --quiet --format='json(metadata.name)' > "$run_dir/execution.tmp.json"
    jq -e --arg job "$job" '.metadata.name | startswith($job + "-")' "$run_dir/execution.tmp.json" >/dev/null
    mv -- "$run_dir/execution.tmp.json" "$run_dir/execution.json"
    cat "$run_dir/execution.json"
    exit 0
fi

execution="$(jq -er '.metadata.name' "$run_dir/execution.json")"
[[ "$execution" == "$job-"* && "$execution" =~ ^[a-z0-9-]+$ ]]
gcloud run jobs executions describe "$execution" --project="$project" --region="$region" --format=json \
    > "$run_dir/execution-status.json"
status="$(jq -r '[.status.conditions[]? | select(.type == "Completed") | .status] | last // "Unknown"' \
    "$run_dir/execution-status.json")"
if [[ "$status" != True ]]; then
    jq -c '[.status.conditions[]? | select(.type == "Completed")]' "$run_dir/execution-status.json" >&2
    if [[ "$status" == False ]]; then exit 1; fi
    exit 3
fi

filter="resource.type=\"cloud_run_job\" AND resource.labels.location=\"$region\" AND resource.labels.job_name=\"$job\" AND labels.\"run.googleapis.com/execution_name\"=\"$execution\""
gcloud logging read "$filter" --project="$project" --freshness=7d --limit=2000 --format=json > "$run_dir/execution-logs.json"
python3 - "$run_dir" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
launch = json.loads((path / 'launch.json').read_bytes())
if hashlib.sha256((path / 'manifest.json').read_bytes()).hexdigest() != launch['manifest_sha256']:
    raise SystemExit('local input manifest does not match the launch receipt')
manifest = json.loads((path / 'manifest.json').read_bytes())
expected = {row['line']: row for row in manifest['queries']}
logs = json.loads((path / 'execution-logs.json').read_bytes())
batch = launch['batch_id']
query_scope = 'meitu_official_rediscovery_query'
summary_scope = 'meitu_official_rediscovery_summary'
queries, summaries = [], []
for entry in logs:
    row = entry.get('jsonPayload')
    if not isinstance(row, dict) or 'scope' not in row:
        raw = entry.get('textPayload') or (row.get('message') if isinstance(row, dict) else None)
        try:
            row = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            continue
    if not isinstance(row, dict) or row.get('scope') not in {query_scope, summary_scope}:
        continue
    if row.get('batch_id') != batch:
        raise SystemExit('execution published evidence for a different batch')
    (queries if row['scope'] == query_scope else summaries).append(row)
lines = sorted(row['meitu_line'] for row in queries)
if len(lines) < len(launch['query_lines']) or not summaries:
    print('Cloud Logging is missing expected evidence. Run collect again when the logs have propagated.', file=sys.stderr)
    raise SystemExit(3)
if lines != launch['query_lines'] or len(summaries) != 1:
    raise SystemExit('execution did not publish exactly one receipt per query and one summary')
summary_row = summaries[0]
if summary_row.get('query_count') != len(lines) or any(summary_row.get(field) != 0 for field in (
    'commerce_index_writes', 'merchant_db_writes', 'offer_db_writes',
)):
    raise SystemExit('summary does not confirm all launched queries and zero database writes')
for row in queries:
    source = expected[row['meitu_line']]
    if (row.get('market') != 'US' or any(row.get(field) != source[field] for field in ('meitu_id', 'brand', 'name', 'host'))
            or not isinstance(row.get('candidates'), list) or row.get('candidate_count') != len(row['candidates'])):
        raise SystemExit(f"invalid candidate count for Meitu line {row['meitu_line']}")
ordered = sorted(queries, key=lambda row: row['meitu_line'])
(path / 'queries.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in ordered))
summary = json.dumps(summary_row, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
(path / 'summary.json').write_text(summary)
print(summary, end='')
PY
