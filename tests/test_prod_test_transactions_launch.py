"""Cloud Build launcher for the prod test-transaction job: the config is the release copy, secrets go in by numbered
version only while armed, the disarmed job has none, the wait fits inside the run step, and result.json is ok only for
a clean run with nothing possibly left open."""

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
GW_DIGEST = "sha256:908df3e4b47324610fd535f0969c923f03aa63e145875a93c94e385885c5846d"
CONFIG = Path(__file__).resolve().parents[1] / "infra" / "gcp" / "cloudbuild.prod_test_transactions.yaml"
GATE = {"verdict": "GATE_OPEN", "observed_at": "2026-01-02T07:00:00+00:00", "summary": "s",
        "source": "scripts/ops/oneoff_gate.py", "cpu_peak_5m": 0.05, "db_job_executions_running": 0}
HOST_SHA = hashlib.sha256(b"shop.reviewed.example").hexdigest()


@pytest.fixture(autouse=True)
def build_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BUILD_ID", BUILD)
    monkeypatch.setattr(launch, "WORK", tmp_path)


def flag(args, name):
    return args[args.index(name) + 1]


def env_of(args):
    raw = flag(args, "--set-env-vars")
    assert raw.startswith("^@^")
    return dict(item.split("=", 1) for item in raw[3:].split("@"))


def spec(cases, **over):
    kw = dict(release=RELEASE, digest=DIGEST, cases=cases, key_version=4, targets_version=7, gate=GATE, armed=True)
    kw.update(over)
    return launch.job_spec_args(**kw)


def test_config_must_be_the_release_copy_and_its_run_step_outlasts_every_wait():
    raw = CONFIG.read_bytes()
    launch.config_matches(raw, hashlib.sha256(raw).hexdigest())
    for sha in ("0" * 64, "", "x"):
        with pytest.raises(launch.LaunchRefused):
            launch.config_matches(raw, sha)
    config = yaml.safe_load(raw)
    steps = {s["id"]: s for s in config["steps"]}
    assert list(steps) == ["workspace", "release-scripts", "config-is-the-release-config", "digest-is-the-release-tag",
                           "gateway", "lock", "run", "publish"]
    assert [s for s in config["steps"] if s.get("allowFailure")] == [steps["run"]]
    assert set(config["substitutions"]) == {"_RELEASE", "_DIGEST", "_CONFIG_SHA256", "_CASES", "_KEY_VERSION",
                                            "_TARGETS_VERSION", "_USER_KEY_VERSION", "_GO_REFERENCE"}
    run_step = int(steps["run"]["timeout"].rstrip("s"))
    assert run_step == launch.RUN_STEP_SECONDS
    # gate (420) + arm deploy and execute (<= 300) + the wait + cancel/disarm reserve must fit inside the run step.
    assert 420 + 300 + launch.EXECUTION_DEADLINE_SECONDS + launch.DISARM_RESERVE_SECONDS <= run_step
    # The reserve covers the cancel call (60) + its poll with one interval of slack + the disarm deploy (300).
    assert launch.DISARM_RESERVE_SECONDS >= 60 + launch.CANCEL_POLL_SECONDS + launch.EXECUTION_POLL_SECONDS + 300
    assert sum(int(s.get("timeout", "0s").rstrip("s")) for s in config["steps"]) <= int(config["timeout"].rstrip("s"))


def test_the_workspace_step_hands_the_directories_to_the_backend_images_runtime_user():
    # Build 99b4e02b (2026-11-20): with --no-source /workspace is root's, and the backend image runs as `USER app`, so
    # release-scripts could not mkdir. A root step (the pinned cloud-sdk image, like every other non-backend step)
    # makes the directories first and chowns them to the uid the Dockerfile creates.
    import re

    config = yaml.safe_load(CONFIG.read_bytes())
    steps = config["steps"]
    sdk = steps[1 + 1]["name"]  # config-is-the-release-config: the pinned cloud-sdk image
    assert sdk.startswith("gcr.io/google.com/cloudsdktool/cloud-sdk@sha256:")
    first, release = steps[0], steps[1]
    assert first["id"] == "workspace" and first["name"] == sdk and first["entrypoint"] == "bash"
    assert [s["id"] for s in steps if "pivota/backend@" in s["name"]] == ["release-scripts"]
    dockerfile = (CONFIG.parents[0] / "Dockerfile").read_text()
    uid = re.search(r"useradd [^\n]*--uid (\d+) app\b", dockerfile).group(1)
    assert re.search(r"^USER app$", dockerfile, re.M)
    script = first["args"][1]
    made = re.search(r"^mkdir -p (.+)$", script, re.M).group(1).split()
    assert re.search(rf"^chown -R {uid} /workspace/release /workspace/prod-test-transactions$", script, re.M)
    assert made == ["/workspace/release/infra/gcp", "/workspace/prod-test-transactions"]
    # release-scripts may only write under the directories the root step handed over.
    wanted = re.search(r"^mkdir -p (.+)$", release["args"][1], re.M).group(1).split()
    assert all(any(w == m or w.startswith(m + "/") or m.startswith(w + "/") for m in made) for w in wanted)
    for target in re.findall(r"^cp (?:-r )?\S+ (\S+)$", release["args"][1], re.M):
        assert target.startswith(("/workspace/release/", "/workspace/prod-test-transactions/")), target


def test_every_cloud_sdk_step_starts_an_executable_the_pinned_image_is_recorded_to_have():
    # Build c2e19f14 (2026-11-20) failed at step 2: the pinned cloud-sdk image (594.0.0, slim) has no python3 on PATH,
    # and these tests never run an image. tests/fixtures/cloud_sdk_image.json records what that image's own build
    # proves present / absent; a new pin must re-record it, and every step must start something recorded present.
    import re

    recorded = json.loads((Path(__file__).parent / "fixtures" / "cloud_sdk_image.json").read_text())
    steps = yaml.safe_load(CONFIG.read_bytes())["steps"]
    sdk_steps = [s for s in steps if s["name"].startswith("gcr.io/google.com/cloudsdktool/cloud-sdk")]
    assert len(sdk_steps) == len(steps) - 1          # all but release-scripts (the backend image)
    for step in sdk_steps:
        assert step["name"] == recorded["image"], step["id"]
        assert step["entrypoint"] in recorded["present"], step["id"]
        assert step["entrypoint"] not in recorded["absent_on_path"], step["id"]
        if step["entrypoint"] == "bash":             # a bash step must not reach for a Python the image lacks either
            for word in recorded["absent_on_path"]:
                assert not re.search(rf"(?<![\w/.-]){word}(?![\w.-])", step["args"][-1]), (step["id"], word)
    python_steps = [s["id"] for s in sdk_steps if s["entrypoint"].endswith("/python3")]
    assert python_steps == ["config-is-the-release-config", "gateway", "lock", "run", "publish"]
    # The gate is started with the step's own interpreter, never a bare name the image may not have.
    source = Path(launch.__file__).read_text()
    assert "sys.executable, str(Path(args.scripts) / \"ops\" / \"oneoff_gate.py\")" in source
    assert not re.search(r"""["']python3?["']\s*,\s*str\(""", source)


@pytest.mark.parametrize("raw", ["", "latest", "0", "-1", "1.5", "01", "1234567"])
def test_versions_must_be_numbered(raw):
    with pytest.raises(launch.LaunchRefused):
        launch.parse_version(raw, "_KEY_VERSION")


def test_go_reference():
    assert launch.parse_go_reference("Dark flag session 2026-11-19 02:10Z").startswith("Dark")
    for raw in ("", "ok", "go; rm -rf /", "x" * 201):
        with pytest.raises(launch.LaunchRefused):
            launch.parse_go_reference(raw)


def test_armed_spec_mounts_the_key_and_targets_by_version_without_the_payment_nat():
    args = spec(["OR-US", "OR-DE"])
    assert args[:5] == ["gcloud", "run", "jobs", "deploy", "prod-test-transactions"]
    assert flag(args, "--set-secrets") == ("/var/run/pivota-test-agent/key=test-shopping-agent-api-key:4,"
                                           "/var/run/pivota-test-targets/targets.json=prod-test-transactions-targets:7")
    assert "--clear-secrets" not in args and ":latest" not in " ".join(args)
    for vpc in ("--network", "--subnet", "--vpc-egress", "--vpc-connector"):
        assert vpc not in args
    assert flag(args, "--service-account") == "sa-prod-test-txn@pivota-prod.iam.gserviceaccount.com"
    assert flag(args, "--max-retries") == "0" and flag(args, "--tasks") == "1"
    env = env_of(args)
    assert env["PROD_TEST_TXN_ARMED"] == "1" and env["PROD_TEST_TXN_CASES"] == "OR-US,OR-DE"
    assert env["PROD_TEST_TXN_RUN_ID"] == BUILD and json.loads(env["PROD_TEST_TXN_GATE"]) == GATE


def test_the_issuer_key_is_mounted_only_when_a_selected_case_needs_an_end_user():
    args = spec(["MCP-K-US", "OR-DE"], user_key_version=2)
    assert flag(args, "--set-secrets").endswith(",/var/run/pivota-test-agent-user/key.pem=prod-test-agent-user-key:2")
    with pytest.raises(launch.LaunchRefused, match="_USER_KEY_VERSION"):
        spec(["MCP-K-US"])
    with pytest.raises(launch.LaunchRefused, match="do not mount"):
        spec(["OR-DE"], user_key_version=2)


def test_disarmed_spec_has_no_secret_no_gate_no_cases():
    args = spec([], gate=None, armed=False)
    assert "--clear-secrets" in args and "--set-secrets" not in args
    assert set(env_of(args)) == {"PIVOTA_ENV", "PROD_TEST_TXN_RELEASE_COMMIT"}


def test_spec_refusals():
    with pytest.raises(launch.LaunchRefused):
        spec(["OR-US"], release="x")
    with pytest.raises(launch.LaunchRefused):
        spec(["OR-US"], gate=None)
    with pytest.raises(job.CaseError):
        spec(["NOPE"])


def service(flags=None, *, traffic=None):
    env = [{"name": k, "value": v} for k, v in (flags or {}).items()]
    env.append({"name": "PIVOTA_COMMIT_SHA", "value": "7d650dce841828059c060a7593992478ddf4f11d"})   # never trusted
    env.append({"name": "SECRET_THING", "valueFrom": {"secretKeyRef": {"name": "s", "key": "latest"}}})
    return {"metadata": {"generation": 7},
            "spec": {"template": {"spec": {"containers": [{"env": env}]}}},
            "status": {"traffic": traffic or [{"revisionName": "gateway-1", "percent": 100}],
                       "latestReadyRevisionName": "gateway-1", "latestCreatedRevisionName": "gateway-1",
                       "observedGeneration": 7, "conditions": [{"type": "Ready", "status": "True"}]}}


def revision_doc(digest=GW_DIGEST, name="gateway-1"):
    return {"metadata": {"name": name},
            "status": {"imageDigest": f"us-west1-docker.pkg.dev/pivota-shared/pivota/gateway@{digest}"}}


def test_gateway_reading_takes_the_serving_images_digest_not_the_commit_env():
    revision, digest, flags = launch.gateway_reading(service({"AGENT_CHECKOUT_UCP_CURRENCY_FOLLOWS_MARKET": "1"}),
                                                     revision_doc())
    assert revision == "gateway-1" and digest == GW_DIGEST
    assert flags["AGENT_CHECKOUT_UCP_CURRENCY_FOLLOWS_MARKET"] == "1" and "SECRET_THING" not in flags
    with pytest.raises(launch.LaunchRefused, match="not the serving one"):
        launch.gateway_reading(service(), revision_doc(name="gateway-0"))
    split = [{"revisionName": "gateway-1", "percent": 50}, {"revisionName": "gateway-0", "percent": 50}]
    with pytest.raises(launch.LaunchRefused, match="the gateway must serve one revision"):
        launch.gateway_reading(service(traffic=split), revision_doc())


def test_the_gateway_step_refuses_path_four_on_an_unreviewed_image(monkeypatch, tmp_path):
    answers = {"services": json.dumps(service()), "revisions": json.dumps(revision_doc("sha256:" + "c" * 64))}
    monkeypatch.setattr(launch, "run", lambda args, **kw: answers[args[2]])
    args = type("A", (), {"cases": "OR-DE"})()
    with pytest.raises(job.CaseError, match="reviewed against"):
        launch.cmd_gateway(args)            # even though PIVOTA_COMMIT_SHA names a reviewed commit
    answers["revisions"] = json.dumps(revision_doc())
    launch.cmd_gateway(args)
    reading = json.loads((tmp_path / "gateway.json").read_text())
    assert reading["digest"] == GW_DIGEST and reading["reviewed_commit"].startswith("7d650dce8")


def test_the_wait_budget_counts_from_the_step_start():
    assert launch.wait_budget(0, 0) == launch.EXECUTION_DEADLINE_SECONDS
    late = launch.RUN_STEP_SECONDS - launch.DISARM_RESERVE_SECONDS - 100
    assert launch.wait_budget(0, late) == 100                      # a slow gate + deploy shortens the wait
    assert launch.wait_budget(0, launch.RUN_STEP_SECONDS) == 0


def test_a_wait_that_runs_out_cancels_the_execution_then_disarms(monkeypatch, tmp_path):
    (tmp_path / "lock.held").write_text(BUILD)
    (tmp_path / "gateway.json").write_text("{}")
    calls = []

    def fake_run(args, **kw):
        calls.append(args[:5])
        if args[:4] == ["gcloud", "run", "jobs", "execute"]:
            return "prod-test-transactions-abc12\n"
        return ""

    monkeypatch.setattr(launch, "run", fake_run)
    monkeypatch.setattr(launch.sbl, "gate_json", lambda out, code, now: GATE)
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "", "returncode": 0})())
    monkeypatch.setattr(launch.sbl, "list_executions", lambda job_name: [])
    waits = []

    def fake_wait(name, *, deadline, **kw):
        waits.append(deadline)
        return None if len(waits) == 1 else "EXECUTION_CANCELLED"

    monkeypatch.setattr(launch, "wait_for_execution", fake_wait)
    args = type("A", (), {"cases": "OR-DE", "release": RELEASE, "digest": DIGEST, "key_version": "4",
                          "targets_version": "7", "user_key_version": "", "go_reference": "Dark flag session 02:10Z",
                          "scripts": "/x"})()
    with pytest.raises(launch.LaunchRefused, match="cancelled"):
        launch.cmd_run(args, monotonic=lambda: 0.0)
    state = json.loads((tmp_path / "run.json").read_text())
    assert state["cancel_requested"] is True and state["state"] == "EXECUTION_CANCELLED" and state["disarmed"] is True
    assert ["gcloud", "run", "jobs", "executions", "cancel"] in calls
    assert waits == [launch.EXECUTION_DEADLINE_SECONDS, launch.CANCEL_POLL_SECONDS]
    assert calls[-1][:4] == ["gcloud", "run", "jobs", "deploy"]               # the disarm came after the cancel


def run_args():
    return type("A", (), {"cases": "OR-DE", "release": RELEASE, "digest": DIGEST, "key_version": "4",
                          "targets_version": "7", "user_key_version": "", "go_reference": "Dark flag session 02:10Z",
                          "scripts": "/x"})()


def armed_run(monkeypatch, tmp_path, *, execute=None, wait=None, find=None):
    """cmd_run with every gcloud call faked; returns (run.json state, the gcloud calls made)."""
    (tmp_path / "lock.held").write_text(BUILD)
    (tmp_path / "gateway.json").write_text("{}")
    calls = []

    def fake_run(args, **kw):
        calls.append(args[:5])
        if args[:4] == ["gcloud", "run", "jobs", "execute"]:
            if execute:
                raise execute
            return "prod-test-transactions-abc12\n"
        return ""

    monkeypatch.setattr(launch, "run", fake_run)
    monkeypatch.setattr(launch.sbl, "gate_json", lambda out, code, now: GATE)
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "", "returncode": 0})())
    monkeypatch.setattr(launch.sbl, "list_executions", lambda job_name: [])
    if find is not None:
        monkeypatch.setattr(launch.sbl, "find_started_execution", find)
    monkeypatch.setattr(launch, "wait_for_execution", wait or (lambda name, *, deadline, **kw: "EXECUTION_SUCCEEDED"))
    try:
        launch.cmd_run(run_args(), monotonic=lambda: 0.0)
    except Exception:  # noqa: BLE001 - the state file is what is asserted
        pass
    return json.loads((tmp_path / "run.json").read_text()), calls


def disarmed(calls):
    deploys = [c for c in calls if c[:4] == ["gcloud", "run", "jobs", "deploy"]]
    return len(deploys) == 2                                         # armed, then the disarm


def test_the_disarm_runs_even_when_the_cancelled_execution_never_reaches_a_terminal_state(monkeypatch, tmp_path):
    state, calls = armed_run(monkeypatch, tmp_path, wait=lambda name, *, deadline, **kw: None)
    assert state["cancel_requested"] is True and state["state"] is None and state["disarmed"] is True
    assert disarmed(calls) and launch.sbl.lock_decision(state).startswith("HELD")   # the lock stays held


def test_the_disarm_runs_when_execute_failed_and_no_execution_was_found(monkeypatch, tmp_path):
    state, calls = armed_run(monkeypatch, tmp_path, execute=launch.LaunchRefused("boom"),
                             find=lambda job_name, before: None)
    assert state["execution"] is None and state["disarmed"] is True and disarmed(calls)
    assert launch.sbl.lock_decision(state).startswith("HELD")


def test_the_disarm_runs_when_the_lookup_or_the_wait_raises(monkeypatch, tmp_path):
    def raising_find(job_name, before):
        raise RuntimeError("list failed")

    state, calls = armed_run(monkeypatch, tmp_path, execute=launch.LaunchRefused("boom"), find=raising_find)
    assert state["disarmed"] is True and disarmed(calls)

    def raising_wait(name, *, deadline, **kw):
        raise RuntimeError("describe exploded")

    state, calls = armed_run(monkeypatch, tmp_path, wait=raising_wait)
    assert state["disarmed"] is True and disarmed(calls) and "describe exploded" in state["error"]


def test_a_poll_never_starts_when_its_describe_would_overrun_the_deadline():
    # W12/W13: every describe may take its full timeout; the wait must still end by its deadline.
    clock = {"t": 0.0}

    def slow_describe(args, **kw):
        clock["t"] += launch.DESCRIBE_TIMEOUT_SECONDS
        return json.dumps({"status": {}})

    launch_run = launch.run
    try:
        launch.run = slow_describe
        for deadline in (launch.EXECUTION_DEADLINE_SECONDS, launch.CANCEL_POLL_SECONDS, 100, 0):
            clock["t"] = 0.0
            assert launch.wait_for_execution("x", deadline=deadline, monotonic=lambda: clock["t"],
                                             sleep=lambda s: clock.__setitem__("t", clock["t"] + s)) is None
            assert clock["t"] <= max(deadline, launch.DESCRIBE_TIMEOUT_SECONDS)
    finally:
        launch.run = launch_run


def test_the_wait_gives_up_inside_its_deadline():
    clock = {"t": 0.0}
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return json.dumps({"status": {}})                 # never terminal

    launch_run = launch.run
    try:
        launch.run = fake_run
        state = launch.wait_for_execution("prod-test-transactions-x", sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                                          monotonic=lambda: clock["t"])
    finally:
        launch.run = launch_run
    assert state is None and clock["t"] <= launch.EXECUTION_DEADLINE_SECONDS + launch.EXECUTION_POLL_SECONDS


def case_row(case, *, passes=True, left_open=(), expires=()):
    return {"kind": "case", "case": case, "pass": passes, "left_open": list(left_open),
            "expires_on_seller": list(expires)}


DONE = {"state": "EXECUTION_SUCCEEDED", "execution": "prod-test-transactions-x"}


def summary(rows, cases, **over):
    kw = dict(run_state=DONE, bad_lines=[], lock="l", cases=cases)
    kw.update(over)
    return launch.summarise(rows, **kw)


def test_result_is_ok_only_for_a_clean_terminal_run_with_every_selected_case():
    cases = ["OR-US", "OR-DE"]
    clean = [case_row(c) for c in cases] + [{"kind": "run", "ok": True}]
    assert summary(clean, cases)["ok"] is True
    for rows, over in (
        ([case_row("OR-US", passes=False), case_row("OR-DE"), {"kind": "run", "ok": True}], {}),
        ([case_row("OR-US", left_open=["cs_1"]), case_row("OR-DE"), {"kind": "run", "ok": True}], {}),
        ([case_row("OR-US"), {"kind": "run", "ok": True}], {}),
        (clean + [{"kind": "run", "ok": True}], {}),
        (clean, {"run_state": {"state": "EXECUTION_FAILED"}}),
        (clean, {"bad_lines": ["zz"]}),
    ):
        assert summary(rows, cases, **over)["ok"] is False


def test_a_selected_checkout_case_with_no_row_is_an_unknown_left_open():
    rows = [case_row("OR-DE"), {"kind": "run", "ok": True}]
    result = summary(rows, ["OR-DE", "MCP-K-US", "OR-US"], run_state={"state": "EXECUTION_FAILED"})
    assert result["left_open"] == ["unknown:MCP-K-US"] and result["ok"] is False   # OR-US is a read: nothing to open


def test_expiring_storefront_carts_are_listed_and_the_shadow_check_is_advisory():
    rows = [case_row("MCP-SF-US", expires=["esc_#1"]), case_row("MCP-K-US"), {"kind": "run", "ok": True}]
    result = summary(rows, ["MCP-SF-US", "MCP-K-US"], shadow={"advisory": True, "lines": 0})
    assert result["ok"] is True and result["expires_on_seller"] == ["esc_#1"] and result["shadow_lookup"]["lines"] == 0


def test_shadow_check_is_advisory_and_counts_every_line_in_the_window():
    rows = [{**case_row("MCP-SF-US"), "shadow_lookup": True}, {**case_row("UCP-SF-US"), "shadow_lookup": True}]
    line = {"mode": "shadow", "sent": False, "reason": "skipped:no_variant"}
    assert launch.shadow_check(rows, [line, line, line]) == {"advisory": True, "wanted": 2, "lines": 3,
                                                             "all_shadow_unsent": True, "reasons": ["skipped:no_variant"]}
    assert launch.shadow_check(rows, [line, {**line, "sent": True}])["all_shadow_unsent"] is False
    assert launch.shadow_check(rows, [line, {**line, "mode": "enforce"}])["all_shadow_unsent"] is False
    assert launch.shadow_check(rows, None)["lines"] is None
    assert launch.shadow_check([{**case_row("MCP-SF-US", passes=False), "shadow_lookup": True}], []) is None


def test_shadow_lines_keep_no_host(monkeypatch):
    entries = [{"jsonPayload": {"mode": "shadow", "sent": False, "reason": "r", "seller_host": "shop.reviewed.example",
                                "lines": [{"variant": "gid://x"}]}}]
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        return json.dumps(entries)

    monkeypatch.setattr(launch, "run", fake_run)
    lines = launch.read_shadow_lines("2026-01-02T07:00:00+00:00", "2026-01-02T07:05:00+00:00")
    assert lines == [{"mode": "shadow", "sent": False, "reason": "r"}]
    assert flag(seen["args"], "--view") == "prod-test-transactions" and flag(seen["args"], "--bucket") == "_Default"


def publish(monkeypatch, tmp_path, rows, state):
    written = {}
    monkeypatch.setattr(launch, "put", lambda prefix, name, content: written.__setitem__(name, content))
    monkeypatch.setattr(launch, "read_log", lambda execution: ("", None))
    monkeypatch.setattr(launch, "release_lock", lambda: "lock 1 released")
    monkeypatch.setattr(launch.sbl, "list_executions", lambda job_name: [])
    monkeypatch.setattr(launch, "read_shadow_lines", lambda a, b: [])
    monkeypatch.setattr(launch.job, "parse_result_lines", lambda text: (rows, []))
    (tmp_path / "run.json").write_text(json.dumps(state))
    args = type("A", (), {"cases": ",".join(r["case"] for r in rows if r["kind"] == "case"), "release": RELEASE,
                          "go_reference": "Dark flag session 02:10Z"})()
    return written, args


def test_publish_refuses_loudly_when_anything_may_be_left_open(monkeypatch, tmp_path):
    rows = [case_row("MCP-K-US", passes=False, left_open=["cs_9"]), {"kind": "run", "ok": False}]
    written, args = publish(monkeypatch, tmp_path, rows, {**DONE, "armed": True, "disarmed": True,
                                                         "execute_attempted": True, "executions_pre_arm": []})
    with pytest.raises(launch.LaunchRefused, match="LEFT OPEN: cs_9"):
        launch.cmd_publish(args)
    result = json.loads(written["result.json"])
    assert result["left_open"] == ["cs_9"] and result["ok"] is False and "lock.json" in written


def test_publish_keeps_the_lock_while_the_job_may_still_be_armed(monkeypatch, tmp_path):
    rows = [case_row("OR-US"), {"kind": "run", "ok": True}]
    written, args = publish(monkeypatch, tmp_path, rows, {**DONE, "armed": True, "disarmed": False,
                                                         "execute_attempted": True})
    with pytest.raises(launch.LaunchRefused):
        launch.cmd_publish(args)
    result = json.loads(written["result.json"])
    assert result["ok"] is False and result["job_armed"] is True and result["lock"].startswith("HELD")
    assert "lock.json" not in written


def test_publish_records_the_targets_sha_and_an_advisory_shadow_result(monkeypatch, tmp_path):
    rows = [{**case_row("MCP-SF-US", expires=["esc_#1"])},
            {"kind": "run", "ok": True, "targets_sha256": "d" * 64}]
    state = {**DONE, "armed": True, "disarmed": True, "execute_attempted": True, "executions_pre_arm": [],
             "started_at": "2026-01-02T07:00:00+00:00", "finished_at": "2026-01-02T07:05:00+00:00"}
    written, args = publish(monkeypatch, tmp_path, rows, state)
    launch.cmd_publish(args)
    result = json.loads(written["result.json"])
    assert result["ok"] is True and result["targets_sha256"] == "d" * 64
    assert result["shadow_lookup"] == {"advisory": True, "wanted": 1, "lines": 0, "all_shadow_unsent": True,
                                       "reasons": []}
