"""Cloud Build launcher steps for the reviewed seed batch: every check fails closed."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from scripts.ops import reviewed_seed_batch as job
from scripts.ops import seed_batch_launch as launch

NOW = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)
RELEASE = "a" * 40
DIGEST = "sha256:" + "b" * 64
NAMED_SHA = "c" * 64


@pytest.mark.parametrize("output", ["", "GATE_CLOSED: CPU", "summary\nGATE_OPEN\nlater", "GATE_OPEN \n"])
def test_gate_needs_a_literal_last_gate_open_line(output):
    with pytest.raises(launch.LaunchRefused, match="gate closed"):
        launch.gate_json(output, NOW)


def test_gate_json_carries_the_observed_time_and_summary():
    gate = launch.gate_json("cpu_peak_5m=0.05 db_job_executions_running=0 utc=07:00Z\nGATE_OPEN\n", NOW)
    assert gate == {"verdict": "GATE_OPEN", "observed_at": NOW.isoformat(),
                    "summary": "cpu_peak_5m=0.05 db_job_executions_running=0 utc=07:00Z"}
    assert job.check_gate(json.dumps(gate), NOW)["verdict"] == "GATE_OPEN"


def web(**changes):
    service = {
        "status": {"traffic": [{"revisionName": "web-1", "percent": 100}], "latestReadyRevisionName": "web-1"},
        "spec": {"template": {"spec": {
            "containers": [{"image": f"{launch.REPO}:{RELEASE}",
                            "env": [{"name": "NAMED_REFERRAL_VARIANTS_MANIFEST_PATH",
                                     "value": f"{launch.NAMED_MOUNT}/{launch.NAMED_FILE}"},
                                    {"name": "NAMED_REFERRAL_VARIANTS_MANIFEST_SHA256", "value": NAMED_SHA}],
                            "volumeMounts": [{"name": "named", "mountPath": launch.NAMED_MOUNT}]}],
            "volumes": [{"name": "named", "secret": {"items": [{"key": "2", "path": launch.NAMED_FILE}]}}]}}},
    }
    container = service["spec"]["template"]["spec"]["containers"][0]
    if "image" in changes:
        container["image"] = changes["image"]
    if "split" in changes:
        service["status"]["traffic"] = [{"revisionName": "web-1", "percent": 90}, {"revisionName": "web-0", "percent": 10}]
    if "stale_template" in changes:
        service["status"]["latestReadyRevisionName"] = "web-2"
    if "sha" in changes:
        container["env"][1]["value"] = "d" * 64
    if "no_mount" in changes:
        container["volumeMounts"] = []
    return service


@pytest.mark.parametrize("image", [f"{launch.REPO}:{RELEASE}", f"{launch.REPO}@{DIGEST}"])
def test_web_accepts_the_release_by_tag_or_digest(image):
    assert launch.check_web(web(image=image), release=RELEASE, digest=DIGEST, named_sha256=NAMED_SHA) == "web-1"


@pytest.mark.parametrize(("change", "message"), [
    ({"image": f"{launch.REPO}:{'e' * 40}"}, "does not run the release"),
    ({"image": f"{launch.REPO}:{RELEASE}@{DIGEST}"}, "does not run the release"),
    ({"split": True}, "one revision at 100%"), ({"stale_template": True}, "latest ready"),
    ({"sha": True}, "named-referral manifest"), ({"no_mount": True}, "secret mount"),
])
def test_web_refusals(change, message):
    with pytest.raises(launch.LaunchRefused, match=message):
        launch.check_web(web(**change), release=RELEASE, digest=DIGEST, named_sha256=NAMED_SHA)


SECRETS = {"MANIFEST": ("seed-batch-manifest", 3, "1" * 64), "PLAN": ("seed-batch-plan", 1, "2" * 64),
           "APPROVAL": ("seed-batch-approval", 1, "4" * 64),
           "RECEIPTS": ("seed-batch-receipts", 1, "3" * 64), "NAMED": ("named-referral-variants-manifest", 2, NAMED_SHA)}


def _args(mode, **kwargs):
    needed = {"plan": ("MANIFEST", "NAMED"), "apply": ("MANIFEST", "PLAN", "APPROVAL", "NAMED"),
              "rollback": ("MANIFEST", "PLAN", "APPROVAL", "RECEIPTS", "NAMED")}[mode]
    params = dict(mode=mode, batch_id="batch-one", release=RELEASE, digest=DIGEST,
                  gate={"verdict": "GATE_OPEN", "observed_at": NOW.isoformat()},
                  secrets={k: SECRETS[k] for k in needed})
    params.update(kwargs)
    return launch.deploy_args(**params)


def _env(args):
    raw = args[args.index("--set-env-vars") + 1]
    assert raw.startswith("^@^")
    return dict(item.split("=", 1) for item in raw[3:].split("@"))


def test_plan_spec_has_no_write_flag_and_runs_the_digest():
    args = _args("plan")
    env = _env(args)
    assert "REVIEWED_SEED_BATCH_WRITE_ENABLED" not in env and "REVIEWED_SEED_BATCH_APPROVAL_PATH" not in env
    assert args[args.index("--image") + 1] == f"{launch.REPO}@{DIGEST}"
    assert args[args.index("--max-retries") + 1] == "0" and args[args.index("--subnet") + 1] == "default"
    assert env["REVIEWED_SEED_BATCH_RELEASE_COMMIT"] == RELEASE
    assert env["REVIEWED_SEED_BATCH_MANIFEST_SHA256"] == "1" * 64
    secrets = args[args.index("--set-secrets") + 1].split(",")
    assert "/var/run/pivota-seed-batch-manifest/manifest.json=seed-batch-manifest:3" in secrets
    assert not any(s.endswith(":latest") for s in secrets if not s.startswith("DATABASE_URL="))


def test_apply_spec_sets_the_write_flag_and_crawl_egress_and_mounts_the_pinned_approval():
    args = _args("apply")
    env = _env(args)
    assert env["REVIEWED_SEED_BATCH_WRITE_ENABLED"] == "1"
    assert "REVIEWED_SEED_BATCH_APPROVAL" not in env and env["REVIEWED_SEED_BATCH_APPROVAL_SHA256"] == "4" * 64
    assert "/var/run/pivota-seed-batch-approval/approval.json=seed-batch-approval:1" in \
        args[args.index("--set-secrets") + 1].split(",")
    assert json.loads(env["REVIEWED_SEED_BATCH_GATE"])["verdict"] == "GATE_OPEN"
    assert args[args.index("--subnet") + 1] == "pivota-crawl"


@pytest.mark.parametrize(("mode", "change", "message"), [
    ("plan", {"secrets": {**{k: SECRETS[k] for k in ("MANIFEST", "NAMED")}, "APPROVAL": SECRETS["APPROVAL"]}},
     "needs exactly"),
    ("apply", {"secrets": {k: SECRETS[k] for k in ("MANIFEST", "PLAN", "NAMED")}}, "needs exactly"),
    ("apply", {"secrets": {"MANIFEST": SECRETS["MANIFEST"], "NAMED": SECRETS["NAMED"]}}, "needs exactly"),
    ("plan", {"secrets": {"MANIFEST": ("seed-batch-manifest", "latest", "1" * 64), "NAMED": SECRETS["NAMED"]}},
     "numbered version"),
    ("plan", {"digest": "sha256:short"}, "digest"),
    ("plan", {"batch_id": "Batch One"}, "slug"),
    ("delete", {}, "mode must be"),
])
def test_deploy_args_refusals(mode, change, message):
    with pytest.raises((launch.LaunchRefused, KeyError), match=message):
        _args(mode, **change) if mode != "delete" else launch.deploy_args(
            mode="delete", batch_id="batch-one", release=RELEASE, digest=DIGEST, gate={}, secrets={})


def _lines(*rows):
    import base64
    return "".join(job.PREFIX + base64.b64encode(json.dumps(r).encode()).decode() + "\n" for r in rows)


def test_summary_is_ok_only_for_one_clean_batch_line():
    entry = {"kind": "entry", "seed_id": "eps_1", "status": "committed", "receipt_sha256": "r"}
    clean = job.parse_result_lines(_lines(entry, {"kind": "batch", "status": "committed"}))
    result = launch.summarise(clean, mode="apply", execution="x")
    assert result["ok"] and result["decision"] == "pending_keep" and result["entries"][0]["receipt_sha256"] == "r"
    for rows in ([entry], [entry, {"kind": "batch", "status": "stopped"}],
                 [{**entry, "status": "outcome_unknown"}, {"kind": "batch", "status": "committed"}],
                 [entry, {"kind": "batch", "status": "committed"}, {"kind": "batch", "status": "committed"}]):
        result = launch.summarise(job.parse_result_lines(_lines(*rows)), mode="apply", execution="x")
        assert not result["ok"] and result["decision"] is None
    assert not launch.summarise([], mode="plan", execution=None)["ok"]


def _fake_gcloud(monkeypatch, *, cp_rc=0, generation="17"):
    calls = []

    def fake(args, **kwargs):
        calls.append(args)
        if args[:3] == ["gcloud", "storage", "cp"] and "--if-generation-match=0" in args:
            return launch.subprocess.CompletedProcess(args, cp_rc, stdout="", stderr="precondition failed" if cp_rc else "")
        if args[:4] == ["gcloud", "storage", "objects", "describe"]:
            return launch.subprocess.CompletedProcess(args, 0, stdout=json.dumps({"generation": generation}), stderr="")
        return launch.subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(launch.subprocess, "run", fake)
    return calls


def test_lock_is_created_only_if_absent_and_released_by_its_generation(monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "WORK", tmp_path)
    calls = _fake_gcloud(monkeypatch)
    launch.cmd_lock(type("A", (), {"batch_id": "batch-one", "mode": "apply"})())
    create = calls[0]
    assert "--if-generation-match=0" in create and create[-1] == "gs://pivota-prod-ops-locks/seed-batch.lock"
    assert launch.release_lock() == "lock 17 released"
    assert calls[-1] == ["gcloud", "storage", "rm", "--if-generation-match=17", "gs://pivota-prod-ops-locks/seed-batch.lock"]


def test_a_held_lock_refuses_the_batch(monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "WORK", tmp_path)
    _fake_gcloud(monkeypatch, cp_rc=1)
    with pytest.raises(launch.LaunchRefused, match="another seed batch holds the lock"):
        launch.cmd_lock(type("A", (), {"batch_id": "batch-one", "mode": "apply"})())
    assert launch.release_lock() == "no lock held by this build"


def test_lock_step_runs_after_gate_and_web_and_before_deploy():
    import yaml

    steps = [s["id"] for s in yaml.safe_load(open("infra/gcp/cloudbuild.seed_batch.yaml"))["steps"]]
    assert steps == ["release-scripts", "digest-is-the-release-tag", "gate", "web", "lock", "deploy", "execute", "publish"]
