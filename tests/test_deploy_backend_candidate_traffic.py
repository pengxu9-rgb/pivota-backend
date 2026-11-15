"""Run the candidate deploy path with simulated Cloud Run routing and health."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "infra/gcp/deploy_backend.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"
TAG = "c-" + SHA[-12:]
TAG_URL = f"https://{TAG}---web-fixture.a.run.app"
DIGEST = "sha256:" + "a" * 64


def _run(tmp_path, scenario, *, public=True):
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(f"#!{sys.executable}\n" + """
import json
from io import BytesIO
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import urllib.request

args = sys.argv[1:]
root = Path(os.environ["FIXTURE_PATH"])
with (root / "gcloud-calls").open("a") as calls:
    calls.write(json.dumps(args) + "\\n")

scenario = os.environ["FIXTURE_SCENARIO"]
public = os.environ["FIXTURE_PUBLIC"] == "1"
if args[:4] == ["artifacts", "docker", "images", "describe"]:
    digest = "sha256:" + "b" * 64 if scenario == "registry-digest-mismatch" else os.environ["FIXTURE_DIGEST"]
    print(digest)
elif args[:3] == ["run", "services", "get-iam-policy"]:
    if scenario == "iam-lookup-failure":
        sys.exit(1)
    members = ["allUsers"] if public else ["serviceAccount:private-fixture@example.com"]
    print(json.dumps({"bindings": [{"role": "roles/run.invoker", "members": members}]}))
elif args[:3] == ["run", "services", "describe"]:
    if any("status.latestCreatedRevisionName" in arg for arg in args):
        before = not (root / "deployed").exists()
        traffic = [
            {"latestRevision": True, "percent": 70} if before else {"revisionName": "web-live", "percent": 70},
            {"revisionName": "web-split", "percent": 30},
        ]
        status = {"latestReadyRevisionName": "web-live", "traffic": traffic}
        if before:
            if scenario == "describe-failure":
                sys.exit(1)
            if scenario == "unresolved-baseline":
                traffic[0] = {"percent": 70}
        else:
            status.update(latestCreatedRevisionName="web-new", latestReadyRevisionName="web-new")
            if scenario == "traffic-changed" or (scenario == "traffic-changed-after-probe" and (root / "probe").exists()):
                traffic[0]["percent"], traffic[1]["percent"] = 80, 20
            if scenario == "candidate-is-serving":
                traffic[1]["revisionName"] = "web-new"
            if scenario != "missing-tag":
                traffic.append({
                    "tag": os.environ["FIXTURE_TAG"], "url": os.environ["FIXTURE_URL"],
                    "revisionName": "web-stale" if scenario == "stale-tag" else "web-new",
                })
            traffic.reverse()
        print(json.dumps({"status": status}))
    elif any("maxScale" in arg for arg in args):
        print("10")
    elif any("containers" in arg for arg in args):
        print("{'name': 'DB_POOL_MAX_SIZE', 'value': '12'}")
    elif any("status.url" in arg for arg in args):
        print("https://live-fixture.a.run.app")
elif args[:2] == ["run", "deploy"]:
    (root / "deployed").touch()
elif args[:3] == ["run", "revisions", "describe"]:
    assert args[3] == "web-new"
    sha = "different-commit" if scenario == "different-commit" else os.environ["FIXTURE_SHA"]
    container = {"env": [{"name": "PIVOTA_COMMIT_SHA", "value": sha}]}
    status = {}
    if scenario != "missing-digest":
        digest = os.environ["FIXTURE_DIGEST"]
        container["image"] = "registry.example/backend@" + digest
        status["imageDigest"] = ("sha256:" + "b" * 64) if scenario == "different-digest" else digest
    print(json.dumps({"spec": {"containers": [container]}, "status": status}))
elif args[:3] == ["run", "jobs", "create"]:
    program = next(arg for arg in args if arg.startswith("--args=^|^-c|"))
    (root / "job-payload").write_text(program.removeprefix("--args=^|^-c|"))
elif args[:3] == ["run", "jobs", "execute"]:
    def urlopen(request, timeout):
        url = request.full_url
        if url.startswith("http://metadata.google.internal/"):
            assert not public and url.endswith("audience=https://live-fixture.a.run.app")
            return BytesIO(b"fixture-token")
        assert request.get_header("Authorization") == (None if public else "Bearer fixture-token")
        (root / "probe").write_text(url)
        sha = "different-commit" if scenario == "different-health-commit" else os.environ["FIXTURE_SHA"]
        response = BytesIO(json.dumps({"version": {"full_sha": sha}}).encode())
        response.status = 200
        response.geturl = lambda: url
        return response

    urllib.request.urlopen = urlopen
    urllib.request.build_opener = lambda *_: SimpleNamespace(open=urlopen)
    exec((root / "job-payload").read_text())
elif args[:3] == ["run", "jobs", "delete"]:
    pass
elif args[:2] == ["logging", "read"]:
    print("PROBE_STATUS=200")
else:
    sys.exit(f"Unexpected gcloud call: {args!r}")
""")
    gcloud.chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text(f"#!{sys.executable}\n" + """
import json
from pathlib import Path
import os
import sys

(Path(os.environ["FIXTURE_PATH"]) / "probe").write_text(sys.argv[-1])
scenario = os.environ["FIXTURE_SCENARIO"]
sha = "different-commit" if scenario == "different-health-commit" else os.environ["FIXTURE_SHA"]
Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps({"version": {"full_sha": sha}}))
print("503" if scenario == "unhealthy" else "200", end="")
if scenario == "incomplete-direct":
    sys.exit(18)
""")
    curl.chmod(0o755)
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GCLOUD": str(gcloud),
        "CONFIG": "preserve",
        "PROMOTE": "0",
        "FIXTURE_PATH": str(tmp_path),
        "FIXTURE_SCENARIO": scenario,
        "FIXTURE_SHA": SHA,
        "FIXTURE_TAG": TAG,
        "FIXTURE_URL": TAG_URL,
        "FIXTURE_DIGEST": DIGEST,
        "FIXTURE_PUBLIC": "1" if public else "0",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    run = subprocess.run(["bash", str(SCRIPT), "prod", SHA], env=env, capture_output=True, text=True, timeout=20)
    calls = [json.loads(row) for row in (tmp_path / "gcloud-calls").read_text().splitlines()]
    return run, calls


@pytest.mark.parametrize("public", [True, False])
def test_zero_traffic_candidate_verifies_the_exact_tag_and_preserves_access_and_default_split(tmp_path, public):
    run, calls = _run(tmp_path, "healthy", public=public)
    assert run.returncode == 0, run.stderr
    assert '"web-live":70,"web-split":30' in run.stdout
    deploy = next(args for args in calls if args[:2] == ["run", "deploy"])
    assert "--no-traffic" in deploy and deploy[deploy.index("--tag") + 1] == TAG
    assert deploy[deploy.index("--image") + 1].endswith(f"@{DIGEST}")
    assert not {"--ingress", "--allow-unauthenticated", "--no-allow-unauthenticated", "--labels"}.intersection(deploy)
    assert any(args[:3] == ["run", "services", "get-iam-policy"] for args in calls)
    assert not any(args[:2] == ["auth", "print-identity-token"] for args in calls)
    assert (tmp_path / "job-payload").exists() == (not public)
    assert (tmp_path / "probe").read_text() == f"{TAG_URL}/health"
    assert SHA in run.stdout and DIGEST in run.stdout
    summary = (tmp_path / "summary").read_text()
    assert all(item in summary for item in ("web-new", SHA, DIGEST))
    assert not any(args[:3] == ["run", "services", "update-traffic"] for args in calls)


@pytest.mark.parametrize("scenario", ["describe-failure", "unresolved-baseline"])
def test_unknown_existing_default_traffic_prevents_candidate_deployment(tmp_path, scenario):
    run, calls = _run(tmp_path, scenario)
    assert run.returncode != 0 and "refusing a candidate deploy" in run.stderr
    assert not any(args[:2] == ["run", "deploy"] for args in calls)


def test_unknown_invoker_policy_prevents_candidate_deployment(tmp_path):
    run, calls = _run(tmp_path, "iam-lookup-failure")
    assert run.returncode != 0 and "IAM policy" in run.stderr
    assert not any(args[:2] == ["run", "deploy"] for args in calls)


def test_incomplete_direct_response_requires_an_in_vpc_check(tmp_path):
    run, calls = _run(tmp_path, "incomplete-direct")
    assert run.returncode == 0, run.stderr
    assert any(args[:3] == ["run", "jobs", "execute"] for args in calls)


@pytest.mark.parametrize(
    ("scenario", "probed"),
    [
        ("missing-tag", False),
        ("stale-tag", False),
        ("candidate-is-serving", False),
        ("traffic-changed", False),
        ("different-commit", False),
        ("missing-digest", False),
        ("different-digest", False),
        ("registry-digest-mismatch", False),
        ("unhealthy", True),
        ("traffic-changed-after-probe", True),
    ],
)
def test_candidate_fails_closed_on_wrong_revision_health_or_default_routing(tmp_path, scenario, probed):
    run, calls = _run(tmp_path, scenario)
    assert run.returncode != 0, (run.stdout, run.stderr)
    assert (tmp_path / "probe").exists() == probed
    assert not any(args[:3] == ["run", "services", "update-traffic"] for args in calls)


@pytest.mark.parametrize("public", [True, False])
def test_candidate_rejects_a_healthy_image_reporting_another_commit(tmp_path, public):
    run, calls = _run(tmp_path, "different-health-commit", public=public)
    assert run.returncode != 0 and "candidate health check returned" in run.stderr
    assert (tmp_path / "probe").read_text() == f"{TAG_URL}/health"
    assert not (tmp_path / "summary").exists()
    assert not any(args[:3] == ["run", "services", "update-traffic"] for args in calls)
