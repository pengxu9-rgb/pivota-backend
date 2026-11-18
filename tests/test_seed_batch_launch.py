"""Cloud Build launcher for the reviewed seed batch: every check fails closed, receipts survive every failure, and
the lock is released only once the execution is known to be over."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts.ops import reviewed_seed_batch as job
from scripts.ops import seed_batch_launch as launch

NOW = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)
RELEASE = "a" * 40
DIGEST = "sha256:" + "b" * 64
NAMED_SHA = "c" * 64
BUILD = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
CONFIG = Path(__file__).resolve().parents[1] / "infra" / "gcp" / "cloudbuild.seed_batch.yaml"
SUMMARY = "cpu_peak_5m=0.05 db_job_executions_running=0 utc=07:00Z"


@pytest.fixture(autouse=True)
def build_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BUILD_ID", BUILD)
    monkeypatch.setattr(launch, "WORK", tmp_path)


# ------------------------------------------------------------------ config provenance and the gate dict

def test_config_must_be_the_release_copy():
    raw = CONFIG.read_bytes()
    launch.config_matches(raw, hashlib.sha256(raw).hexdigest())
    for sha in ("0" * 64, "", "not-a-sha"):
        with pytest.raises(launch.LaunchRefused):
            launch.config_matches(raw, sha)


def test_gate_dict_is_built_from_a_real_gate_run_and_passes_the_jobs_rules():
    gate = launch.gate_json(f"{SUMMARY}\nGATE_OPEN\n", 0, NOW)
    assert gate == {"verdict": "GATE_OPEN", "observed_at": NOW.isoformat(), "summary": SUMMARY,
                    "source": "scripts/ops/oneoff_gate.py", "cpu_peak_5m": 0.05, "db_job_executions_running": 0}
    assert job.check_gate(json.dumps(gate), NOW) == gate


@pytest.mark.parametrize(("output", "code", "message"), [
    ("", 0, "gate closed"), ("GATE_CLOSED: CPU\n", 3, "gate closed"),
    (f"{SUMMARY}\nGATE_OPEN\n", 3, "gate closed"),            # exit code is read, not only the text
    (f"{SUMMARY}\nGATE_OPEN\nlater\n", 0, "gate closed"), ("GATE_OPEN\n", 0, "unreadable"),
    ("cpu=0.05\nGATE_OPEN\n", 0, "unreadable"),
])
def test_gate_refusals(output, code, message):
    with pytest.raises(launch.LaunchRefused, match=message):
        launch.gate_json(output, code, NOW)


# ------------------------------------------------------------------ web: the SERVING revision, not the template

def service(**changes):
    status = {"traffic": [{"revisionName": "web-1", "percent": 100}], "latestReadyRevisionName": "web-1",
              "latestCreatedRevisionName": "web-1", "observedGeneration": 7,
              "conditions": [{"type": "Ready", "status": "True"}]}
    status.update(changes)
    return {"metadata": {"generation": 7}, "status": status}


def revision(**changes):
    container = {"image": f"{launch.REPO}:{RELEASE}",
                 "env": [{"name": "NAMED_REFERRAL_VARIANTS_MANIFEST_PATH", "value": f"{launch.NAMED_MOUNT}/{launch.NAMED_FILE}"},
                         {"name": "NAMED_REFERRAL_VARIANTS_MANIFEST_SHA256", "value": NAMED_SHA}],
                 "volumeMounts": [{"name": "named", "mountPath": launch.NAMED_MOUNT}]}
    spec = {"containers": [container], "volumes": [{"name": "named", "secret": {
        "secretName": launch.NAMED_SECRET, "items": [{"key": "2", "path": launch.NAMED_FILE}]}}]}
    rev = {"status": {"imageDigest": f"{launch.REPO}@{DIGEST}"}, "spec": spec}
    if "digest" in changes:
        rev["status"]["imageDigest"] = changes["digest"]
    if "sha" in changes:
        container["env"][1]["value"] = changes["sha"]
    if "secret" in changes:
        spec["volumes"][0]["secret"]["secretName"] = changes["secret"]
    return rev


def test_web_serving_revision_and_its_digest():
    assert launch.serving_revision_name(service()) == "web-1"
    launch.check_revision(revision(), digest=DIGEST, named_sha256=NAMED_SHA)


@pytest.mark.parametrize(("changes", "message"), [
    ({"latestCreatedRevisionName": "web-2"}, "mid-rollout"),               # new revision rolling out or failed
    ({"latestReadyRevisionName": "web-2"}, "mid-rollout"),
    ({"traffic": [{"revisionName": "web-1", "percent": 90}, {"revisionName": "web-0", "percent": 10}]}, "100%"),
    ({"observedGeneration": 6}, "not observed"), ({"conditions": [{"type": "Ready", "status": "False"}]}, "not Ready"),
])
def test_web_service_refusals(changes, message):
    with pytest.raises(launch.LaunchRefused, match=message):
        launch.serving_revision_name(service(**changes))


@pytest.mark.parametrize(("changes", "message"), [
    ({"digest": f"{launch.REPO}@sha256:" + "e" * 64}, "release digest"), ({"sha": "d" * 64}, "named-referral manifest"),
    ({"secret": "other-secret"}, "secret mount"),
])
def test_web_revision_refusals(changes, message):
    with pytest.raises(launch.LaunchRefused, match=message):
        launch.check_revision(revision(**changes), digest=DIGEST, named_sha256=NAMED_SHA)


# ------------------------------------------------------------------ secrets and the job spec

def secret(name, version=1, sha="1" * 64):
    return json.dumps([launch.SECRET_IDS[name], version, sha])


def raw_secrets(mode):
    raw = {"MANIFEST": secret("MANIFEST", 3), "PLAN": "", "APPROVAL": "", "RECEIPTS": "", "NAMED": secret("NAMED", 2, NAMED_SHA)}
    if mode != "plan":
        raw.update(PLAN=secret("PLAN", 1, "2" * 64), APPROVAL=secret("APPROVAL", 1, "4" * 64))
    if mode == "rollback":
        raw["RECEIPTS"] = secret("RECEIPTS", 1, "3" * 64)
    return raw


@pytest.mark.parametrize(("name", "value", "message"), [
    ("NAMED", json.dumps([launch.NAMED_SECRET, "latest", NAMED_SHA]), "numbered version"),
    ("NAMED", json.dumps([launch.NAMED_SECRET, "2,PARTNER_API_KEY=partner-api-key:latest", NAMED_SHA]), "numbered version"),
    ("NAMED", json.dumps(["partner-api-key", 2, NAMED_SHA]), "numbered version"),
    ("MANIFEST", json.dumps(["seed-batch-manifest", True, "1" * 64]), "numbered version"),
    ("MANIFEST", "not json", "must be"),
    ("NAMED", secret("NAMED", 2, "d" * 64), "differs from web's"),
])
def test_secret_refusals(name, value, message):
    raw = {**raw_secrets("plan"), name: value}
    with pytest.raises(launch.LaunchRefused, match=message):
        launch.parse_secrets("plan", raw, NAMED_SHA)


def test_each_mode_takes_exactly_its_secrets():
    for mode in launch.MODES:
        assert set(launch.parse_secrets(mode, raw_secrets(mode), NAMED_SHA)) == launch.NEEDED[mode]
    with pytest.raises(launch.LaunchRefused, match="needs exactly"):
        launch.parse_secrets("plan", raw_secrets("apply"), NAMED_SHA)


def _env(args):
    raw = args[args.index("--set-env-vars") + 1]
    assert raw.startswith("^@^")
    return dict(item.split("=", 1) for item in raw[3:].split("@"))


def spec(mode, *, armed=True):
    secrets = launch.parse_secrets(mode, raw_secrets(mode), NAMED_SHA)
    gate = launch.gate_json(f"{SUMMARY}\nGATE_OPEN\n", 0, NOW) if armed else None
    return launch.job_spec_args(mode=mode, batch_id="batch-one", release=RELEASE, digest=DIGEST, secrets=secrets,
                                gate=gate, armed=armed)


def test_job_runs_as_sa_worker_once_on_the_release_digest():
    args = spec("apply")
    assert args[4] == "reviewed-seed-batch-batch-one" and launch.JOB_PREFIX == "reviewed-seed-batch-"
    assert args[args.index("--service-account") + 1] == "sa-worker@pivota-prod.iam.gserviceaccount.com"
    assert args[args.index("--image") + 1] == f"{launch.REPO}@{DIGEST}"
    assert (args[args.index("--tasks") + 1], args[args.index("--max-retries") + 1],
            args[args.index("--task-timeout") + 1]) == ("1", "0", "1800s")
    assert f"build-id={BUILD},batch-mode=apply" == args[args.index("--labels") + 1]


def test_armed_apply_spec_has_gate_write_flag_and_crawl_egress():
    args = spec("apply")
    env = _env(args)
    assert env["REVIEWED_SEED_BATCH_WRITE_ENABLED"] == "1" and json.loads(env["REVIEWED_SEED_BATCH_GATE"])["cpu_peak_5m"] == 0.05
    assert env["REVIEWED_SEED_BATCH_APPROVAL_SHA256"] == "4" * 64 and args[args.index("--subnet") + 1] == "pivota-crawl"
    mounts = args[args.index("--set-secrets") + 1].split(",")
    assert "/var/run/pivota-seed-batch-approval/approval.json=seed-batch-approval:1" in mounts
    assert f"{launch.NAMED_MOUNT}/{launch.NAMED_FILE}={launch.NAMED_SECRET}:2" in mounts
    assert [m for m in mounts if m.endswith(":latest")] == ["DATABASE_URL=DATABASE_URL:latest"]


def test_armed_plan_spec_has_the_gate_and_no_write_flag():
    env = _env(spec("plan"))
    assert "REVIEWED_SEED_BATCH_GATE" in env and "REVIEWED_SEED_BATCH_WRITE_ENABLED" not in env


@pytest.mark.parametrize("mode", launch.MODES)
def test_disarmed_spec_has_no_gate_write_flag_plan_approval_or_receipts(mode):
    args = spec(mode, armed=False)
    env = _env(args)
    assert not {"REVIEWED_SEED_BATCH_GATE", "REVIEWED_SEED_BATCH_WRITE_ENABLED", "REVIEWED_SEED_BATCH_PLAN_PATH",
                "REVIEWED_SEED_BATCH_APPROVAL_PATH", "REVIEWED_SEED_BATCH_RECEIPTS_PATH"} & env.keys()
    mounts = args[args.index("--set-secrets") + 1]
    assert "seed-batch-plan" not in mounts and "seed-batch-approval" not in mounts and "seed-batch-receipts" not in mounts


def test_spec_refusals():
    secrets = launch.parse_secrets("plan", raw_secrets("plan"), NAMED_SHA)
    with pytest.raises(launch.LaunchRefused, match="armed job takes the gate"):
        launch.job_spec_args(mode="plan", batch_id="batch-one", release=RELEASE, digest=DIGEST, secrets=secrets,
                             gate=None, armed=True)
    for batch_id in ("Batch One", "batch-", "-batch", "a"):
        with pytest.raises(launch.LaunchRefused, match="slug"):
            launch.job_name(batch_id)


def test_build_id_must_be_mapped(monkeypatch):
    monkeypatch.delenv("BUILD_ID")
    with pytest.raises(launch.LaunchRefused, match="BUILD_ID"):
        launch.build_id()


# ------------------------------------------------------------------ run: async execute, wait by name, disarm

class Gcloud:
    """A fake `subprocess.run` that answers the launcher's gcloud calls and records them."""

    def __init__(self, *, gate=(f"{SUMMARY}\nGATE_OPEN\n", 0), states=("EXECUTION_SUCCEEDED",), describe_errors=0,
                 lock_build=BUILD, log=None, cp_fail=None):
        self.calls, self.gate, self.states, self.describe_errors = [], gate, list(states), describe_errors
        self.lock_build, self.log, self.cp_fail = lock_build, log, cp_fail

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        ok = lambda out="": subprocess.CompletedProcess(args, 0, stdout=out, stderr="")  # noqa: E731
        if args[1:2] and args[1].endswith("oneoff_gate.py"):
            return subprocess.CompletedProcess(args, self.gate[1], stdout=self.gate[0], stderr="")
        if args[:4] == ["gcloud", "run", "jobs", "execute"]:
            return ok("reviewed-seed-batch-batch-one-x7k2p\n")
        if args[:5] == ["gcloud", "run", "jobs", "executions", "describe"]:
            if self.describe_errors:
                self.describe_errors -= 1
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="connection reset")
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            status = {} if state is None else {"completionTime": "t", "conditions": [
                {"type": "Completed", "status": "True" if state == "EXECUTION_SUCCEEDED" else "False"}]}
            return ok(json.dumps({"status": status}))
        if args[:4] == ["gcloud", "storage", "objects", "describe"]:
            return ok(json.dumps({"generation": "17"}))
        if args[:3] == ["gcloud", "storage", "cat"]:
            return ok(json.dumps({"build": self.lock_build}))
        if args[:3] == ["gcloud", "logging", "read"]:
            return ok(self.log or "")
        if args[:3] == ["gcloud", "storage", "cp"] and self.cp_fail and args[-1].endswith(self.cp_fail):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="precondition failed")
        return ok()


def run_args(mode="apply"):
    raw = raw_secrets(mode)
    return type("A", (), {"mode": mode, "batch_id": "batch-one", "release": RELEASE, "digest": DIGEST,
                          "named_sha256": NAMED_SHA, "manifest": raw["MANIFEST"], "plan": raw["PLAN"],
                          "approval": raw["APPROVAL"], "receipts": raw["RECEIPTS"], "named": raw["NAMED"],
                          "scripts": "/workspace/release/scripts"})()


def held(tmp_path):
    (tmp_path / "lock.held").write_text(BUILD)


def deploys(calls):
    return [c for c in calls if c[:4] == ["gcloud", "run", "jobs", "deploy"]]


def test_run_gates_then_deploys_armed_executes_async_waits_by_name_and_disarms(monkeypatch, tmp_path):
    held(tmp_path)
    fake = Gcloud(states=(None, None, "EXECUTION_SUCCEEDED"), describe_errors=1)
    monkeypatch.setattr(launch.subprocess, "run", fake)
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    launch.cmd_run(run_args())
    order = [("gate" if c[1].endswith("oneoff_gate.py") else " ".join(c[1:4])) for c in fake.calls]
    assert order[0] == "gate" and order[1] == "run jobs deploy" and order[2] == "run jobs execute"
    assert "--async" in fake.calls[2]
    assert "REVIEWED_SEED_BATCH_WRITE_ENABLED" in _env(deploys(fake.calls)[0])
    assert "REVIEWED_SEED_BATCH_WRITE_ENABLED" not in _env(deploys(fake.calls)[-1]) and len(deploys(fake.calls)) == 2
    state = json.loads((tmp_path / "run.json").read_text())
    # The name was kept at once (P1-1); a client error while polling was not taken as a terminal state.
    assert state["execution"] == "reviewed-seed-batch-batch-one-x7k2p" and state["state"] == "EXECUTION_SUCCEEDED"
    assert state["disarmed"] is True


def test_run_with_a_closed_gate_deploys_and_executes_nothing_armed(monkeypatch, tmp_path):
    held(tmp_path)
    fake = Gcloud(gate=("GATE_CLOSED: CPU\n", 3))
    monkeypatch.setattr(launch.subprocess, "run", fake)
    with pytest.raises(launch.LaunchRefused, match="gate closed"):
        launch.cmd_run(run_args())
    assert not [c for c in fake.calls if c[:4] == ["gcloud", "run", "jobs", "execute"]]
    assert all("REVIEWED_SEED_BATCH_WRITE_ENABLED" not in _env(c) for c in deploys(fake.calls))


def test_run_without_this_builds_lock_does_nothing(monkeypatch, tmp_path):
    fake = Gcloud()
    monkeypatch.setattr(launch.subprocess, "run", fake)
    with pytest.raises(launch.LaunchRefused, match="lock"):
        launch.cmd_run(run_args())
    assert not [c for c in fake.calls if c[:4] == ["gcloud", "run", "jobs", "execute"]]


def test_an_execution_still_running_at_the_deadline_is_not_terminal_and_is_not_disarmed(monkeypatch, tmp_path):
    held(tmp_path)
    fake = Gcloud(states=(None,))
    monkeypatch.setattr(launch.subprocess, "run", fake)
    ticks = iter(range(0, 10 ** 6, 1000))
    monkeypatch.setattr(launch.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    with pytest.raises(launch.LaunchRefused, match="terminal state"):
        launch.cmd_run(run_args())
    state = json.loads((tmp_path / "run.json").read_text())
    assert state["execution"] and state["state"] is None and "disarmed" not in state
    assert len(deploys(fake.calls)) == 1


# ------------------------------------------------------------------ publish: receipts always, lock only when over

def line(row):
    return job.PREFIX + base64.b64encode(json.dumps(row).encode()).decode()


def run_state(tmp_path, **state):
    (tmp_path / "run.json").write_text(json.dumps({"execution": "reviewed-seed-batch-batch-one-x7k2p",
                                                   "state": "EXECUTION_SUCCEEDED", "error": None, **state}))


def puts(calls):
    return [c[-1].rsplit("/", 1)[1] for c in calls if c[:3] == ["gcloud", "storage", "cp"] and "seed-batch.lock" not in c[-1]]


ENTRY = {"kind": "entry", "seed_id": "eps_1", "status": "committed", "receipt_sha256": "r"}


def publish(monkeypatch, fake, mode="apply"):
    monkeypatch.setattr(launch.subprocess, "run", fake)
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    # A fast clock: a log without its batch line is re-read until the deadline, which then passes at once.
    ticks = iter(range(0, 10 ** 9, 100))
    monkeypatch.setattr(launch.time, "monotonic", lambda: next(ticks))
    return launch.cmd_publish(type("A", (), {"mode": mode, "batch_id": "batch-one", "release": RELEASE})())


def result_of(tmp_path):
    return json.loads((tmp_path / "result.json").read_text())


def test_publish_writes_log_rows_then_result_create_only_and_releases_the_lock(monkeypatch, tmp_path):
    held(tmp_path)
    run_state(tmp_path)
    fake = Gcloud(log="\n".join([line(ENTRY), line({"kind": "batch", "status": "committed"})]))
    publish(monkeypatch, fake)
    assert puts(fake.calls) == ["log.txt", "rows.json", "result.json"]
    for c in fake.calls:
        if c[:3] == ["gcloud", "storage", "cp"]:
            assert "--if-generation-match=0" in c
            assert c[-1].startswith(f"gs://pivota-prod-ops-receipts/seed-batch/batch-one/apply-{BUILD}/")
    assert ["gcloud", "storage", "rm", "--if-generation-match=17", "gs://pivota-prod-ops-locks/seed-batch.lock"] in fake.calls
    result = result_of(tmp_path)
    assert result["ok"] and result["decision"] == "pending_keep" and result["lock"] == "lock 17 released"


def test_a_failed_execution_still_publishes_its_committed_receipts_and_releases(monkeypatch, tmp_path):
    held(tmp_path)
    run_state(tmp_path, state="EXECUTION_FAILED")
    fake = Gcloud(log="\n".join([line(ENTRY), line({**ENTRY, "seed_id": "eps_2", "status": "rolled_back"}),
                                 line({"kind": "batch", "status": "stopped"})]))
    with pytest.raises(launch.LaunchRefused, match="did not finish cleanly"):
        publish(monkeypatch, fake)
    result = result_of(tmp_path)
    assert not result["ok"] and result["entries"][0]["receipt_sha256"] == "r" and result["lock"].startswith("lock 17")


def test_a_non_terminal_execution_keeps_the_lock(monkeypatch, tmp_path):
    held(tmp_path)
    run_state(tmp_path, state=None, error="connection reset")
    fake = Gcloud(log=line(ENTRY))
    with pytest.raises(launch.LaunchRefused):
        publish(monkeypatch, fake)
    assert not [c for c in fake.calls if c[:3] == ["gcloud", "storage", "rm"]]
    assert result_of(tmp_path)["lock"].startswith("HELD") and puts(fake.calls)[-1] == "result.json"


def test_a_lock_that_is_not_this_builds_is_never_released(monkeypatch, tmp_path):
    held(tmp_path)
    run_state(tmp_path)
    fake = Gcloud(lock_build="another-build", log="\n".join([line(ENTRY), line({"kind": "batch", "status": "committed"})]))
    with pytest.raises(launch.LaunchRefused):
        publish(monkeypatch, fake)
    assert not [c for c in fake.calls if c[:3] == ["gcloud", "storage", "rm"]]
    assert ["gcloud", "storage", "cat", "gs://pivota-prod-ops-locks/seed-batch.lock#17"] in fake.calls


def test_a_bad_line_or_missing_log_still_publishes_everything(monkeypatch, tmp_path):
    held(tmp_path)
    run_state(tmp_path)
    fake = Gcloud(log="\n".join([line(ENTRY), job.PREFIX + "trunc@ted", line({"kind": "batch", "status": "committed"})]))
    with pytest.raises(launch.LaunchRefused):
        publish(monkeypatch, fake)
    assert puts(fake.calls) == ["log.txt", "rows.json", "result.json"] and result_of(tmp_path)["bad_lines"]
    (tmp_path / "run.json").unlink()
    fake = Gcloud()
    with pytest.raises(launch.LaunchRefused):
        publish(monkeypatch, fake)
    assert puts(fake.calls) == ["log.txt", "rows.json", "result.json"]
    assert result_of(tmp_path)["run_error"] == "run step did not start"


def test_publish_waits_for_the_batch_line_to_be_ingested(monkeypatch, tmp_path):
    reads = iter(["", line(ENTRY), "\n".join([line(ENTRY), line({"kind": "batch", "status": "committed"})])])

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=next(reads), stderr="")

    monkeypatch.setattr(launch.subprocess, "run", fake_run)
    sleeps = []
    text, error = launch.read_log("x", sleep=sleeps.append)
    assert error is None and len(sleeps) == 2 and '"batch"' in base64.b64decode(text.splitlines()[-1][len(job.PREFIX):]).decode()


def test_an_existing_receipt_object_is_an_error_not_a_skip(monkeypatch, tmp_path):
    held(tmp_path)
    run_state(tmp_path)
    fake = Gcloud(log=line({"kind": "batch", "status": "committed"}), cp_fail="log.txt")
    with pytest.raises(launch.LaunchRefused, match="precondition failed"):
        publish(monkeypatch, fake)


def test_summary_is_ok_only_for_a_clean_terminal_batch():
    good = [ENTRY, {"kind": "batch", "status": "committed"}]
    ok = {"state": "EXECUTION_SUCCEEDED"}
    assert launch.summarise(good, mode="apply", run_state=ok, bad_lines=[], lock="x")["ok"]
    for rows, state, bad in ((good, {"state": "EXECUTION_FAILED"}, []), (good, {"state": None}, []), (good, ok, ["x"]),
                             ([ENTRY], ok, []), ([{"kind": "batch", "status": "committed"}], ok, []),
                             (good + [{"kind": "batch", "status": "committed"}], ok, []),
                             ([{**ENTRY, "status": "outcome_unknown"}, {"kind": "batch", "status": "committed"}], ok, [])):
        assert not launch.summarise(rows, mode="apply", run_state=state, bad_lines=bad, lock="x")["ok"]


# ------------------------------------------------------------------ the build config itself

def steps():
    return yaml.safe_load(CONFIG.read_text())["steps"]


def test_step_order_and_failure_policy():
    ids = [s["id"] for s in steps()]
    assert ids == ["release-scripts", "config-is-the-release-config", "digest-is-the-release-tag", "web", "lock", "run",
                   "publish"]
    assert [s["id"] for s in steps() if s.get("allowFailure")] == ["run"]
    total = sum(int(s["timeout"].rstrip("s")) for s in steps())
    assert all("timeout" in s for s in steps()) and total < int(yaml.safe_load(CONFIG.read_text())["timeout"].rstrip("s"))


def test_builder_images_are_pinned_and_the_digest_check_escapes_its_shell_variables():
    for step in steps():
        assert "@sha256:" in step["name"] or step["name"].endswith("@${_DIGEST}"), step["id"]
    digest_step = next(s for s in steps() if s["id"] == "digest-is-the-release-tag")["args"][1]
    assert 'test "$$tag_digest" = "${_DIGEST}"' in digest_step and "$tag_digest\"" not in digest_step.replace("$$", "")
    assert "$$(cat /app/.image_commit_sha)" in next(s for s in steps() if s["id"] == "release-scripts")["args"][1]


def test_build_id_is_mapped_and_buckets_are_not_substitutions():
    config = yaml.safe_load(CONFIG.read_text())
    assert "BUILD_ID=$BUILD_ID" in config["options"]["env"]
    assert not {"_BUCKET", "_LOCK_BUCKET"} & config["substitutions"].keys()
    assert not any("oneoff_gate" in " ".join(map(str, s.get("args", []))) for s in steps())  # gated inside `run` only


def test_a_failed_job_is_a_terminal_state_and_keeps_its_name(monkeypatch, tmp_path):
    held(tmp_path)
    monkeypatch.setattr(launch.subprocess, "run", Gcloud(states=("EXECUTION_FAILED",)))
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    launch.cmd_run(run_args())
    state = json.loads((tmp_path / "run.json").read_text())
    assert (state["execution"], state["state"], state["disarmed"]) == ("reviewed-seed-batch-batch-one-x7k2p",
                                                                       "EXECUTION_FAILED", True)
