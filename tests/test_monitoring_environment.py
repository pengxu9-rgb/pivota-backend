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
            out = {"verificationStatus": "VERIFIED"}
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


def run_script(tmp_path, env, *, repeat=False):
    state = tmp_path / "state.json"
    if not repeat:
        state.write_text(json.dumps({"calls": [], "gcloud": []}))
    for name in ["gcloud", "curl"]:
        stub = tmp_path / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(SCRIPT), env], capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
             "GCLOUD": str(tmp_path / "gcloud"), "ALERT_EMAIL": "monitored@example.com",
             "MONITORING_TEST_STATE": str(state), "NEW_METRIC_TRIES": "0"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(state.read_text())


def test_staging_never_probes_prod_and_scopes_regular_and_reap_policies(tmp_path):
    state = run_script(tmp_path, "staging")
    assert all(c["project"] == "pivota-staging" for c in state["calls"])
    assert not state.get("uptimeCheckConfigs")
    assert state["notificationChannels"][0]["displayName"] == "pivota staging alerts"
    policies = state["alertPolicies"]
    assert len(policies) == 10  # 7 project policies, 3 Reap policies; no host/TLS
    assert all(p["displayName"].startswith("staging:") for p in policies)
    assert not any("prod:" in json.dumps(p) for p in policies)
    assert sum("Reap" in p["displayName"] for p in policies) == 3
    assert all("--project" not in args or args[args.index("--project") + 1] == "pivota-staging"
               for args in state["gcloud"])


def test_production_names_hosts_and_policy_shapes_are_preserved(tmp_path):
    state = run_script(tmp_path, "prod")
    assert state["notificationChannels"][0]["displayName"] == "pivota prod alerts"
    assert {c["monitoredResource"]["labels"]["host"] for c in state["uptimeCheckConfigs"]} == {
        "api.pivota.cc", "gateway.pivota.cc", "mcp.pivota.cc", "commerce.mcp.pivota.cc",
        "ucp.pivota.cc", "acp.pivota.cc",
    }
    assert len(state["alertPolicies"]) == 12
    assert all(p["displayName"].startswith("prod:") for p in state["alertPolicies"])


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_rerun_reuses_channel_and_checks_and_replaces_policies_by_environment_name(tmp_path, env):
    initial = run_script(tmp_path, env)
    state = run_script(tmp_path, env, repeat=True)
    assert len(state["notificationChannels"]) == 1
    assert len(state.get("uptimeCheckConfigs", [])) == len(initial.get("uptimeCheckConfigs", []))
    assert len(state["alertPolicies"]) == len(initial["alertPolicies"])
    assert len({p["displayName"] for p in state["alertPolicies"]}) == len(state["alertPolicies"])
