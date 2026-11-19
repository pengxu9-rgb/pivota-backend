"""Cloud Build launcher for the prod test-transaction job: the config is the release copy, the key goes in by numbered
version only while armed, the disarmed job has no key, gate or cases, and result.json is ok only for a clean run."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from scripts.ops import prod_test_transactions as job
from scripts.ops import prod_test_transactions_launch as launch

RELEASE = "a" * 40
DIGEST = "sha256:" + "b" * 64
BUILD = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
CONFIG = Path(__file__).resolve().parents[1] / "infra" / "gcp" / "cloudbuild.prod_test_transactions.yaml"
GATE = {"verdict": "GATE_OPEN", "observed_at": "2026-01-02T07:00:00+00:00", "summary": "s",
        "source": "scripts/ops/oneoff_gate.py", "cpu_peak_5m": 0.05, "db_job_executions_running": 0}


@pytest.fixture(autouse=True)
def build_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BUILD_ID", BUILD)
    monkeypatch.setattr(launch, "WORK", tmp_path)
    monkeypatch.setattr(job, "OFFERS_PRODUCT_ID", "prod_test_reviewed_0001")


def flag(args, name):
    return args[args.index(name) + 1]


def env_of(args):
    raw = flag(args, "--set-env-vars")
    assert raw.startswith("^@^")
    return dict(item.split("=", 1) for item in raw[3:].split("@"))


def test_config_must_be_the_release_copy_and_parses():
    raw = CONFIG.read_bytes()
    launch.config_matches(raw, hashlib.sha256(raw).hexdigest())
    for sha in ("0" * 64, "", "x"):
        with pytest.raises(launch.LaunchRefused):
            launch.config_matches(raw, sha)
    config = yaml.safe_load(raw)
    assert [s["id"] for s in config["steps"]] == ["release-scripts", "config-is-the-release-config",
                                                   "digest-is-the-release-tag", "gateway", "lock", "run", "publish"]
    assert [s for s in config["steps"] if s.get("allowFailure")] == [config["steps"][5]]
    assert set(config["substitutions"]) == {"_RELEASE", "_DIGEST", "_CONFIG_SHA256", "_CASES", "_KEY_VERSION",
                                            "_GO_REFERENCE"}


@pytest.mark.parametrize("raw", ["", "latest", "0", "-1", "1.5", "01", "1234567"])
def test_key_version_must_be_a_numbered_version(raw):
    with pytest.raises(launch.LaunchRefused):
        launch.parse_key_version(raw)


def test_key_version_and_go_reference_accepted():
    assert launch.parse_key_version("3") == 3
    assert launch.parse_go_reference("Dark flag session 2026-11-19 02:10Z") .startswith("Dark")
    for raw in ("", "ok", "go; rm -rf /", "x" * 201):
        with pytest.raises(launch.LaunchRefused):
            launch.parse_go_reference(raw)


def test_armed_spec_mounts_the_key_by_version_with_the_gate_cases_and_run_id():
    args = launch.job_spec_args(release=RELEASE, digest=DIGEST, cases=["OR-US", "OR-DE"], key_version=4, gate=GATE,
                                armed=True)
    assert args[:5] == ["gcloud", "run", "jobs", "deploy", "prod-test-transactions"]
    assert flag(args, "--set-secrets") == "/var/run/pivota-test-agent/key=prod-test-agent-key:4"
    assert "--clear-secrets" not in args and ":latest" not in " ".join(args)
    assert flag(args, "--service-account") == "sa-prod-test-txn@pivota-prod.iam.gserviceaccount.com"
    assert flag(args, "--max-retries") == "0" and flag(args, "--tasks") == "1"
    assert flag(args, "--subnet") == "default" and flag(args, "--vpc-egress") == "all-traffic"
    assert flag(args, "--image") == f"{launch.REPO}@{DIGEST}"
    env = env_of(args)
    assert env["PROD_TEST_TXN_ARMED"] == "1" and env["PROD_TEST_TXN_CASES"] == "OR-US,OR-DE"
    assert env["PROD_TEST_TXN_RUN_ID"] == BUILD and env["PROD_TEST_TXN_RELEASE_COMMIT"] == RELEASE
    assert json.loads(env["PROD_TEST_TXN_GATE"]) == GATE


def test_disarmed_spec_has_no_key_no_gate_no_cases():
    args = launch.job_spec_args(release=RELEASE, digest=DIGEST, cases=[], key_version=1, gate=None, armed=False)
    assert "--clear-secrets" in args and "--set-secrets" not in args
    env = env_of(args)
    assert set(env) == {"PIVOTA_ENV", "PROD_TEST_TXN_RELEASE_COMMIT"}


def test_spec_refusals():
    with pytest.raises(launch.LaunchRefused):
        launch.job_spec_args(release="x", digest=DIGEST, cases=["OR-US"], key_version=1, gate=GATE, armed=True)
    with pytest.raises(launch.LaunchRefused):
        launch.job_spec_args(release=RELEASE, digest=DIGEST, cases=["OR-US"], key_version=1, gate=None, armed=True)
    with pytest.raises(job.CaseError):
        launch.job_spec_args(release=RELEASE, digest=DIGEST, cases=["NOPE"], key_version=1, gate=GATE, armed=True)


def service(flags=None, *, traffic=None):
    env = [{"name": k, "value": v} for k, v in (flags or {}).items()]
    env.append({"name": "SECRET_THING", "valueFrom": {"secretKeyRef": {"name": "s", "key": "latest"}}})
    return {"metadata": {"generation": 7},
            "spec": {"template": {"spec": {"containers": [{"env": env}]}}},
            "status": {"traffic": traffic or [{"revisionName": "gateway-1", "percent": 100}],
                       "latestReadyRevisionName": "gateway-1", "latestCreatedRevisionName": "gateway-1",
                       "observedGeneration": 7, "conditions": [{"type": "Ready", "status": "True"}]}}


def test_gateway_reading_takes_one_serving_revision_and_plain_flag_values(monkeypatch):
    monkeypatch.setattr(job, "GATEWAY_FLAG_NAMES", ("AGENT_CHECKOUT_UCP_CURRENCY_FOLLOWS_MARKET", "SECRET_THING"))
    revision, flags = launch.gateway_flags(service({"AGENT_CHECKOUT_UCP_CURRENCY_FOLLOWS_MARKET": "1"}))
    assert revision == "gateway-1"
    assert flags == {"AGENT_CHECKOUT_UCP_CURRENCY_FOLLOWS_MARKET": "1", "SECRET_THING": None}  # a secret is never read
    split = [{"revisionName": "gateway-1", "percent": 50}, {"revisionName": "gateway-0", "percent": 50}]
    with pytest.raises(launch.LaunchRefused):
        launch.gateway_flags(service(traffic=split))


def rows_for(cases, *, passes=True, left_open=()):
    rows = [{"kind": "case", "case": c, "pass": passes, "left_open": list(left_open)} for c in cases]
    rows.append({"kind": "run", "ok": passes and not left_open})
    return rows


DONE = {"state": "EXECUTION_SUCCEEDED", "execution": "prod-test-transactions-x"}


def test_result_is_ok_only_for_a_clean_terminal_run_with_every_selected_case():
    cases = ["OR-US", "OR-DE"]
    assert launch.summarise(rows_for(cases), run_state=DONE, bad_lines=[], lock="l", cases=cases)["ok"] is True
    bad = [
        dict(rows=rows_for(cases, passes=False)),
        dict(rows=rows_for(cases, left_open=["cs_1"])),
        dict(rows=rows_for(["OR-US"])),                                     # a selected case has no line
        dict(rows=rows_for(cases) + [{"kind": "run", "ok": True}]),         # two run lines
        dict(rows=rows_for(cases), run_state={"state": "EXECUTION_FAILED"}),
        dict(rows=rows_for(cases), bad_lines=["zz"]),
    ]
    for over in bad:
        args = {"rows": rows_for(cases), "run_state": DONE, "bad_lines": [], "lock": "l", "cases": cases, **over}
        assert launch.summarise(**args)["ok"] is False, over


def test_left_open_is_collected_from_every_case():
    rows = [{"kind": "case", "case": "A", "pass": False, "left_open": ["cs_2", "unknown:A"]},
            {"kind": "case", "case": "B", "pass": False, "left_open": ["cs_1"]}, {"kind": "run", "ok": False}]
    result = launch.summarise(rows, run_state=DONE, bad_lines=[], lock="l", cases=["A", "B"])
    assert result["left_open"] == ["cs_1", "cs_2", "unknown:A"] and result["ok"] is False


def test_publish_refuses_loudly_when_anything_was_left_open(monkeypatch, tmp_path):
    written = {}
    monkeypatch.setattr(launch, "put", lambda prefix, name, content: written.__setitem__(name, content))
    monkeypatch.setattr(launch, "read_log", lambda execution: ("", None))
    monkeypatch.setattr(launch, "release_lock", lambda: "lock 1 released")
    monkeypatch.setattr(launch.sbl, "list_executions", lambda job_name: [])
    rows = rows_for(["OR-US"], passes=False, left_open=["cs_9"])
    monkeypatch.setattr(launch.job, "parse_result_lines", lambda text: (rows, []))
    (tmp_path / "run.json").write_text(json.dumps({**DONE, "armed": True, "disarmed": True,
                                                   "execute_attempted": True, "executions_pre_arm": []}))
    args = type("A", (), {"cases": "OR-US", "release": RELEASE, "go_reference": "Dark flag session 02:10Z"})()
    with pytest.raises(launch.LaunchRefused, match="LEFT OPEN: cs_9"):
        launch.cmd_publish(args)
    result = json.loads(written["result.json"])
    assert result["left_open"] == ["cs_9"] and result["ok"] is False and "lock.json" in written


def test_publish_keeps_the_lock_while_the_job_may_still_be_armed(monkeypatch, tmp_path):
    written = {}
    monkeypatch.setattr(launch, "put", lambda prefix, name, content: written.__setitem__(name, content))
    monkeypatch.setattr(launch, "read_log", lambda execution: ("", None))
    monkeypatch.setattr(launch.job, "parse_result_lines", lambda text: (rows_for(["OR-US"]), []))
    (tmp_path / "run.json").write_text(json.dumps({**DONE, "armed": True, "disarmed": False,
                                                   "execute_attempted": True}))
    args = type("A", (), {"cases": "OR-US", "release": RELEASE, "go_reference": "Dark flag session 02:10Z"})()
    with pytest.raises(launch.LaunchRefused):
        launch.cmd_publish(args)
    result = json.loads(written["result.json"])
    assert result["ok"] is False and result["job_armed"] is True and result["lock"].startswith("HELD")
    assert "lock.json" not in written
