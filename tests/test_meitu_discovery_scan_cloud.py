import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from scripts.ops.meitu_discovery import sources


OPERATOR = Path(sources.__file__).parent
PROJECT = "pivota-staging"
NAMESPACE = "184498324954"
REGION = "us-west1"
ACCOUNT = "sa-meitu-evidence-v1@pivota-staging.iam.gserviceaccount.com"
IMAGE = "us-west1-docker.pkg.dev/pivota-shared/pivota/backend@sha256:" + "a" * 64


def embedded_python(command):
    launcher = (OPERATOR / "scan_cloud.sh").read_text()
    match = re.search(r"^" + command + r".*? <<'PY'\n(.*?)\nPY$", launcher, re.M | re.S)
    assert match is not None
    return match.group(1)


def run_python(code, *args):
    return subprocess.run([sys.executable, "-", *map(str, args)], input=code,
                          capture_output=True, text=True, check=False)


@pytest.fixture
def scan_execution(tmp_path):
    fixture = tmp_path / "input.json"
    fixture.write_text(json.dumps({"schema_version": 1, "market": "US", "batch_id": "example-us", "queries": [
        {"line": 1, "meitu_id": "example", "brand": "Example", "name": "Lip Pencil", "host": None,
         "platform": "unknown", "search_names": []},
    ]}))
    packer = embedded_python(r'    code="\$\(python3')
    packed = run_python(packer, OPERATOR / "scan.py", OPERATOR / "capture.py", fixture, tmp_path)
    assert packed.returncode == 0, packed.stderr
    job = json.loads((tmp_path / "launch.json").read_bytes())["job"]
    execution = job + "-demo"
    spec = {
        "taskCount": 1, "parallelism": 1,
        "template": {"spec": {
            "serviceAccountName": ACCOUNT, "maxRetries": 0, "timeoutSeconds": "1800",
            "containers": [{
                "image": IMAGE, "command": ["python"], "args": ["-c", packed.stdout.rstrip("\n")],
                "env": [{"name": "GOOGLE_CLOUD_PROJECT", "value": PROJECT}],
            }],
        }},
    }
    detail = {
        "kind": "Execution",
        "metadata": {"name": execution, "namespace": NAMESPACE,
                     "labels": {"run.googleapis.com/job": job, "cloud.googleapis.com/location": REGION}},
        "spec": spec,
    }
    return tmp_path, job, execution, detail


def verify_execution(case):
    run_dir, job, execution, detail = case
    (run_dir / "execution-status.json").write_text(json.dumps(detail))
    verifier = embedded_python(r'python3 - "\$run_dir" "\$project"')
    return run_python(verifier, run_dir, PROJECT, NAMESPACE, REGION, ACCOUNT, IMAGE, job, execution)


def test_scan_collect_binds_the_packed_manifest_and_accepts_the_archived_job(scan_execution):
    result = verify_execution(scan_execution)
    assert result.returncode == 0, result.stderr
    run_dir, _, _, detail = scan_execution
    launch_path = run_dir / "launch.json"
    launch = json.loads(launch_path.read_text())
    launch.pop("program_sha256")
    launch_path.write_text(json.dumps(launch))
    (run_dir / "job.json").write_text(json.dumps({"spec": {"template": {"spec": detail["spec"]}}}))
    result = verify_execution(scan_execution)
    assert result.returncode == 0, result.stderr

    (run_dir / "manifest.json").write_text("{}")
    assert "executed source, capture, or manifest differs" in verify_execution(scan_execution).stderr


@pytest.mark.parametrize("mismatch,reason", [
    ("namespace", "another Cloud Run job or namespace"),
    ("account", "settings differ from the staging pins"),
    ("image", "differs from the read-only image or configuration"),
    ("env", "differs from the read-only image or configuration"),
    ("args", "executed program differs from the launch digest"),
])
def test_scan_collect_refuses_an_execution_override(scan_execution, mismatch, reason):
    detail = scan_execution[3]
    task = detail["spec"]["template"]["spec"]
    container = task["containers"][0]
    if mismatch == "namespace":
        detail["metadata"]["namespace"] = "123456789123"
    elif mismatch == "account":
        task["serviceAccountName"] = "another@example.com"
    elif mismatch == "image":
        container["image"] = IMAGE[:-1] + "b"
    elif mismatch == "env":
        container["env"].append({"name": "UNEXPECTED", "value": "example"})
    else:
        container["args"][1] += ";print('unexpected')"
    result = verify_execution(scan_execution)
    assert result.returncode != 0
    assert reason in result.stderr


def test_source_url_holds_explicit_foreign_locales_and_malformed_inputs():
    assert sources.eligible_url("https://[invalid") is None
    assert sources.eligible_url("https://shop.example/fr-fr/products/lip-pencil") is None
    assert sources.eligible_url("https://shop.example/ca/products/lip-pencil") is None
    assert sources.eligible_url("https://shop.example/en-us/products/lip-pencil?variant=10#shade") == (
        "https://shop.example/en-us/products/lip-pencil?variant=10"
    )
