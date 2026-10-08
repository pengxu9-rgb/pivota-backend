"""Exercise the provisioning script with executable fakes: no cloud calls or email."""
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "infra/gcp/setup_monitoring.sh"
STUB = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p = Path(os.environ["MONITORING_TEST_STATE"])
s = json.loads(p.read_text())
args = sys.argv[1:]
if Path(sys.argv[0]).name == "gcloud":
    s["gcloud"].append(args)
    out = "fake-token" if args[:2] == ["auth", "print-access-token"] else ""
else:
    method = args[args.index("-X") + 1]
    url = args[-1]
    project = url.split("/projects/")[1].split("/")[0]
    path = url.split("/projects/" + project + "/")[1]
    body = json.loads(args[args.index("-d") + 1]) if "-d" in args else None
    s["calls"].append({"method": method, "project": project, "path": path, "body": body})
    if method == "GET":
        if path.startswith("notificationChannels/"):
            out = dict(next(c for c in s["notificationChannels"]
                            if c["name"] == "projects/" + project + "/" + path))
            mode = os.environ.get("MONITORING_CHANNEL_MODE", "verified")
            out["verificationStatus"] = "VERIFIED"
            if mode == "omitted": out.pop("verificationStatus")
            elif mode == "unspecified": out["verificationStatus"] = "VERIFICATION_STATUS_UNSPECIFIED"
            elif mode == "unverified": out["verificationStatus"] = "UNVERIFIED"
            elif mode == "null_status": out["verificationStatus"] = None
            elif mode == "unknown_status": out["verificationStatus"] = "NOT_A_STATUS"
            elif mode == "wrong_name": out["name"] = "projects/other/notificationChannels/wrong"
            elif mode == "wrong_type": out["type"] = "sms"
            elif mode == "disabled": out["enabled"] = False
            elif mode == "missing_enabled": out.pop("enabled")
            elif mode == "recipient_mismatch": out["labels"] = {"email_address": "other@example.com"}
            elif mode == "malformed_labels": out["labels"] = []
            elif mode == "empty_object": out = {}
            elif mode == "nonobject": out = []
            elif mode == "api_error": out = {"error": {"code": 404, "message": "not found"}}
            elif mode == "empty": out = ""
            elif mode == "malformed": out = "not JSON"
            elif mode == "transport_error":
                p.write_text(json.dumps(s))
                print("fake transport failure", file=sys.stderr)
                sys.exit(7)
        else:
            out = {path: s.get(path, [])}
    elif method == "DELETE":
        kind = path.split("/")[0]
        s[kind] = [x for x in s.get(kind, []) if x["name"] != "projects/" + project + "/" + path]
        out = {}
    else:
        out = {**body, "name": "projects/" + project + "/" + path + "/" + str(len(s["calls"]))}
        s.setdefault(path, []).append(out)
p.write_text(json.dumps(s))
print(out if isinstance(out, str) else json.dumps(out))
'''


def run_script(tmp_path, env, *, repeat=False, channel_mode="verified",
               channel_existing=False, return_process=False, worker_service="worker"):
    state = tmp_path / "state.json"
    if not repeat:
        initial = {"calls": [], "gcloud": []}
        if channel_existing:
            project = "pivota-staging" if env == "staging" else "pivota-prod"
            initial["notificationChannels"] = [{
                "name": "projects/" + project + "/notificationChannels/existing",
                "type": "email", "displayName": "existing alerts", "enabled": True,
                "labels": {"email_address": "monitored@example.com"},
            }]
        state.write_text(json.dumps(initial))
    for name in ["gcloud", "curl"]:
        stub = tmp_path / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(SCRIPT), env], capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "GCLOUD": str(tmp_path / "gcloud"), "ALERT_EMAIL": "monitored@example.com",
             "MONITORING_TEST_STATE": str(state), "NEW_METRIC_TRIES": "0",
             "MONITORING_CHANNEL_MODE": channel_mode,
             "REAP_WORKER_SERVICE_NAME": worker_service},
    )
    if return_process:
        return json.loads(state.read_text()), proc
    assert proc.returncode == 0, proc.stderr
    return json.loads(state.read_text())


@pytest.mark.parametrize("channel_existing", [False, True], ids=["create", "reuse"])
@pytest.mark.parametrize("mode", ["verified", "omitted", "unspecified"])
def test_usable_email_api_state_does_not_claim_actual_delivery(tmp_path, channel_existing, mode):
    state, proc = run_script(tmp_path, "staging", channel_mode=mode,
                             channel_existing=channel_existing, return_process=True)
    assert proc.returncode == 0, proc.stderr
    assert len(state["alertPolicies"]) == 13
    assert "Channel API state does not prove alert delivery" in proc.stdout
    assert "Confirm a controlled notification" in proc.stdout
    assert "cannot receive" not in proc.stderr
    assert "complete" not in proc.stderr.lower()
    if mode != "verified":
        assert "verification may not be required" in proc.stdout
    creates = [c for c in state["calls"] if c["method"] == "POST"
               and c["path"] == "notificationChannels"]
    assert len(creates) == (0 if channel_existing else 1)
    assert not any("VerificationCode" in c["path"] for c in state["calls"])


@pytest.mark.parametrize("channel_existing", [False, True], ids=["create", "reuse"])
def test_explicit_unverified_email_channel_fails_and_requires_receipt(tmp_path, channel_existing):
    state, proc = run_script(tmp_path, "staging", channel_mode="unverified",
                             channel_existing=channel_existing, return_process=True)
    assert proc.returncode != 0
    assert "UNVERIFIED and requires verification" in proc.stderr
    assert "confirm actual alert receipt" in proc.stderr
    assert "no policy reconciliation was performed" in proc.stderr
    assert len(state["notificationChannels"]) == 1
    assert not any("VerificationCode" in c["path"] for c in state["calls"])
    read_index = next(i for i, c in enumerate(state["calls"])
                      if c["method"] == "GET" and c["path"].startswith("notificationChannels/"))
    assert not any(c["method"] in {"POST", "PATCH", "DELETE"}
                   for c in state["calls"][read_index + 1:])
    assert not state.get("alertPolicies")
    assert not state.get("uptimeCheckConfigs")
    assert not any(args[:2] == ["logging", "metrics"] for args in state["gcloud"])


@pytest.mark.parametrize("channel_existing", [False, True], ids=["create", "reuse"])
@pytest.mark.parametrize("mode", [
    "empty", "malformed", "api_error", "transport_error", "empty_object", "nonobject",
    "wrong_name", "wrong_type", "disabled", "missing_enabled", "recipient_mismatch",
    "malformed_labels", "null_status", "unknown_status",
])
def test_failed_channel_reads_never_become_exempt_or_reconcile_policies(tmp_path, channel_existing, mode):
    state, proc = run_script(tmp_path, "staging", channel_mode=mode,
                             channel_existing=channel_existing, return_process=True)
    assert proc.returncode != 0
    assert "FAILED: notification channel" in proc.stderr
    assert "verification may not be required" not in proc.stdout
    assert not state.get("alertPolicies")
    assert not any(args[:2] == ["logging", "metrics"] for args in state["gcloud"])


def test_staging_never_probes_prod_and_scopes_regular_and_reap_policies(tmp_path):
    state = run_script(tmp_path, "staging")
    assert all(c["project"] == "pivota-staging" for c in state["calls"])
    assert not state.get("uptimeCheckConfigs")
    assert state["notificationChannels"][0]["displayName"] == "pivota staging alerts"
    policies = state["alertPolicies"]
    assert len(policies) == 13  # 8 project policies, 5 Reap policies; no host/TLS
    assert all(p["displayName"].startswith("staging:") for p in policies)
    assert not any("prod:" in json.dumps(p) for p in policies)
    assert sum("Reap" in p["displayName"] for p in policies) == 5
    assert all("--project" not in args or args[args.index("--project") + 1] == "pivota-staging"
               for args in state["gcloud"])


def test_production_names_hosts_and_policy_shapes_are_preserved(tmp_path):
    state = run_script(tmp_path, "prod")
    assert state["notificationChannels"][0]["displayName"] == "pivota prod alerts"
    assert {c["monitoredResource"]["labels"]["host"] for c in state["uptimeCheckConfigs"]} == {
        "api.pivota.cc", "gateway.pivota.cc", "mcp.pivota.cc", "commerce.mcp.pivota.cc",
        "ucp.pivota.cc", "acp.pivota.cc",
    }
    assert len(state["alertPolicies"]) == 15
    assert all(p["displayName"].startswith("prod:") for p in state["alertPolicies"])


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_rerun_reuses_channel_and_checks_and_replaces_policies_by_environment_name(tmp_path, env):
    initial = run_script(tmp_path, env)
    state = run_script(tmp_path, env, repeat=True)
    assert len(state["notificationChannels"]) == 1
    assert len(state.get("uptimeCheckConfigs", [])) == len(initial.get("uptimeCheckConfigs", []))
    assert len(state["alertPolicies"]) == len(initial["alertPolicies"])
    assert len({p["displayName"] for p in state["alertPolicies"]}) == len(state["alertPolicies"])


def test_dedicated_worker_filters_do_not_watch_shared_worker(tmp_path):
    state=run_script(tmp_path,"staging",worker_service="reap-isolated-worker")
    calls=[args for args in state["gcloud"] if args[:2]==["logging","metrics"] and len(args)>3 and args[2] in {"create","update"} and args[3].startswith("reap_agentic_poll_")]
    assert len(calls)==5
    for args in calls:
        log_filter=next(x for x in args if x.startswith("--log-filter="))
        assert 'resource.labels.service_name="reap-isolated-worker"' in log_filter
        assert 'resource.labels.service_name="worker"' not in log_filter

@pytest.mark.parametrize("worker",["*",'worker" OR true',"Bad","-worker","worker-","w"*64])
def test_invalid_worker_target_fails_before_cloud_reads(tmp_path,worker):
    state,proc=run_script(tmp_path,"staging",worker_service=worker,return_process=True)
    assert proc.returncode==2 and "one exact Cloud Run service name" in proc.stderr
    assert state["calls"]==[] and state["gcloud"]==[]
