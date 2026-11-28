"""`infra/gcp/setup_merchant_protocol_profile_census_job.sh` puts the census on a schedule.

The census writes merchant_agent_checkout_facts, which ADR-030 D11 reads; a positive fact counts for
30 days, so the lane must run every two weeks, leave from the crawl subnet, run one pinned backend
commit, be dark unless `--enable` was typed on THIS run, and never leave its two switches (the job's
gate and the trigger) disagreeing without saying so: a dark run exits 0, which the missed-run alert
counts as a success.

These tests drive the REAL script through its `GCLOUD` injection point against a STATEFUL fake (the
job's gate and the trigger's state live in a JSON file, and every mutating call can be made to fail),
recording every invocation one argument per field. Nothing here reaches Google Cloud.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "gcp" / "setup_merchant_protocol_profile_census_job.sh"
GCP = REPO / "infra" / "gcp"
JOB = "merchant-protocol-profile-census"
TRIGGER = "merchant-protocol-profile-census-cron"
GATE = "MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED"
TAG = "0123456789abcdef0123456789abcdef01234567"

# One invocation per line, fields separated by \x1f. State: {"job": null | {"gate": "true"|"false"},
# "trigger": null | "ENABLED" | "PAUSED"}. FAIL_AT="N[,M...]" fails those MUTATING calls (counted from
# 1); FAIL_KEYS="run jobs update;scheduler jobs resume" fails every call with one of those first three
# words; NOOP_KEYS (same form) answers 0 and changes NOTHING (a call that silently did not apply);
# READ_FAIL=1 fails the --format reads. A failed call changes nothing.
FAKE_GCLOUD = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    argv = sys.argv[1:]
    with open(os.environ["GCLOUD_LOG"], "a") as log:
        log.write("\\x1f".join(argv) + "\\x1f\\n")
    path = os.environ["GCLOUD_STATE"]
    state = json.load(open(path))
    key = " ".join(argv[:3])
    gate_name = os.environ["GATE_NAME"]
    def save():
        json.dump(state, open(path, "w"))
    def value(flag):
        for i, tok in enumerate(argv):
            if tok == flag:
                return argv[i + 1]
            if tok.startswith(flag + "="):
                return tok[len(flag) + 1:]
        return None
    def env_pairs(raw):
        sep = raw[1] if raw.startswith("^") and raw[2:3] == "^" else ","
        body = raw[3:] if sep != "," else raw
        return dict(p.split("=", 1) for p in body.split(sep))
    mutating = key in ("run jobs create", "run jobs update", "scheduler jobs create",
                       "scheduler jobs update", "scheduler jobs pause", "scheduler jobs resume")
    if mutating:
        n = state.get("_mutations", 0) + 1
        state["_mutations"] = n
        save()
        fail_keys = [k for k in os.environ.get("FAIL_KEYS", "").split(";") if k]
        if str(n) in os.environ.get("FAIL_AT", "").split(",") or key in fail_keys:
            sys.stderr.write("fake: %s failed\\n" % key)
            sys.exit(1)
        if key in [k for k in os.environ.get("NOOP_KEYS", "").split(";") if k]:
            sys.exit(0)
    if key == "iam service-accounts describe":
        sys.exit(0 if os.environ.get("SA_EXISTS", "1") == "1" else 1)
    if key == "artifacts docker images":
        sys.exit(0 if os.environ.get("IMAGE_EXISTS", "1") == "1" else 1)
    if key == "compute networks subnets":
        sys.exit(0 if os.environ.get("SUBNET_EXISTS", "1") == "1" else 1)
    if key == "run jobs describe":
        if state.get("job") is None:
            sys.exit(1)
        if value("--format") == "json":
            if os.environ.get("READ_FAIL") == "1":
                sys.exit(1)
            env = [{"name": gate_name, "value": state["job"]["gate"]}]
            print(json.dumps({"spec": {"template": {"spec": {"template": {"spec": {
                "containers": [{"env": env}]}}}}}}))
        sys.exit(0)
    if key in ("run jobs create", "run jobs update"):
        job = state.get("job") or {"gate": "false"}
        if value("--set-env-vars") is not None:
            job["gate"] = env_pairs(value("--set-env-vars")).get(gate_name, "false")
        if value("--update-env-vars") is not None:
            job["gate"] = env_pairs(value("--update-env-vars")).get(gate_name, job["gate"])
        state["job"] = job
        save()
        sys.exit(0)
    if key == "scheduler jobs describe":
        if state.get("trigger") is None:
            sys.exit(1)
        if value("--format") is not None:
            if os.environ.get("READ_FAIL") == "1":
                sys.exit(1)
            print(state["trigger"])
        sys.exit(0)
    if key == "scheduler jobs create":
        state["trigger"] = "ENABLED"  # as the real one: a new trigger starts enabled
        save()
        sys.exit(0)
    if key == "scheduler jobs update":
        sys.exit(0)
    if key in ("scheduler jobs pause", "scheduler jobs resume"):
        if state.get("trigger") is None:
            sys.exit(1)
        state["trigger"] = "PAUSED" if argv[2] == "pause" else "ENABLED"
        save()
        sys.exit(0)
    sys.exit(0)
    """
)

INITIAL = {
    "absent": {"job": None, "trigger": None},
    "dark": {"job": {"gate": "false"}, "trigger": "PAUSED"},
    "armed": {"job": {"gate": "true"}, "trigger": "ENABLED"},
}


def _run(tmp_path: Path, *args: str, state: Optional[dict] = None, env: Optional[Dict[str, str]] = None,
         bash: str = "bash") -> Tuple[subprocess.CompletedProcess, List[List[str]], dict]:
    fake = tmp_path / "fake-gcloud"
    fake.write_text(FAKE_GCLOUD.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
    fake.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state if state is not None else INITIAL["absent"]))
    proc = subprocess.run(
        [bash, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(REPO),
        env={**os.environ, "GCLOUD": str(fake), "GCLOUD_LOG": str(log), "GCLOUD_STATE": str(state_file),
             "GATE_NAME": GATE, "PYTHON": sys.executable, **(env or {})},
    )
    calls = [line.split("\x1f")[:-1] for line in log.read_text().splitlines() if line]
    return proc, calls, json.loads(state_file.read_text())


def _one(calls: List[List[str]], *prefix: str) -> List[str]:
    found = [c for c in calls if c[: len(prefix)] == list(prefix)]
    assert len(found) == 1, (prefix, calls)
    return found[0]


def _flag(argv: List[str], name: str) -> str:
    for i, token in enumerate(argv):
        if token == name:
            return argv[i + 1]
        if token.startswith(name + "="):
            return token[len(name) + 1:]
    raise AssertionError(f"{name} not passed: {argv}")


def _env_vars(argv: List[str]) -> Dict[str, str]:
    raw = _flag(argv, "--set-env-vars")
    sep = raw[1] if raw.startswith("^") and raw[2:3] == "^" else ","
    body = raw[3:] if sep != "," else raw
    return dict(pair.split("=", 1) for pair in body.split(sep))


def _pair(state: dict) -> Tuple[str, str]:
    gate = state["job"]["gate"] if state.get("job") else "absent"
    return gate, state.get("trigger") or "absent"


def _agrees(state: dict) -> bool:
    return _pair(state) in {("true", "ENABLED"), ("false", "PAUSED"), ("false", "absent"),
                            ("absent", "PAUSED"), ("absent", "absent")}


def test_the_script_parses_under_both_bashes():
    """macOS ships bash 3.2; the operator may run either. Nothing in the script needs bash 4."""
    for bash in ("/bin/bash", "bash"):
        subprocess.run([bash, "-n", str(SCRIPT)], check=True)
    assert os.access(SCRIPT, os.X_OK)


def test_dark_by_default_on_the_crawl_subnet_gated_off_and_paused(tmp_path):
    proc, calls, state = _run(tmp_path, "prod", TAG)
    assert proc.returncode == 0, proc.stderr
    job = _one(calls, "run", "jobs", "create", JOB)
    assert _flag(job, "--subnet") == "pivota-crawl", "never the default subnet: that is the payment NAT"
    assert _flag(job, "--network") == "default"
    assert _flag(job, "--vpc-egress") == "all-traffic", "private-ranges-only would leave via the default NAT"
    assert _flag(job, "--max-retries") == "0", "a retry would ask every merchant again"
    assert _flag(job, "--image") == f"us-west1-docker.pkg.dev/pivota-shared/pivota/backend:{TAG}"
    assert _flag(job, "--service-account") == "sa-worker@pivota-prod.iam.gserviceaccount.com"
    assert _flag(job, "--set-secrets") == "DATABASE_URL=DATABASE_URL:latest", "DATABASE_URL and nothing else"
    assert _flag(job, "--command") == "python"
    assert _env_vars(job)[GATE] == "false"
    assert state == {**state, "job": {"gate": "false"}, "trigger": "PAUSED"}
    assert "DARK" in proc.stdout and "MISMATCH" not in proc.stderr


def test_the_env_is_exactly_the_reviewed_first_runs(tmp_path):
    """The 2026-11-28 first run's env, plus the service name and commit the job carries: one pooled
    connection, the DB guardrails web has, no startup migration. No Shopify edge pacer variable: the
    census paces itself and takes no lease on the shared schedule."""
    _proc, calls, _state = _run(tmp_path, "prod", TAG)
    env = _env_vars(_one(calls, "run", "jobs", "create", JOB))
    assert env == {
        "PIVOTA_ENV": "production", "PIVOTA_SERVICE_NAME": JOB, "PIVOTA_COMMIT_SHA": TAG,
        "DB_AUTO_MIGRATE": "false", "DB_POOL_MIN_SIZE": "1", "DB_POOL_MAX_SIZE": "1",
        "DB_STATEMENT_TIMEOUT_SECONDS": "30", "DB_COMMAND_TIMEOUT_SECONDS": "600",
        GATE: "false",
    }
    assert not [k for k in env if k.startswith("CRAWL_SHOPIFY_EDGE_")]


def test_args_are_the_first_runs_in_one_equals_form_token(tmp_path):
    """#2367: `--args "-m,..."` is parsed by gcloud as a second flag; the equals form is one token."""
    _proc, calls, _state = _run(tmp_path, "prod", TAG)
    job = _one(calls, "run", "jobs", "create", JOB)
    assert "--args" not in job
    assert [t for t in job if t.startswith("--args=")] == [
        "--args=-m,jobs.merchant_protocol_profile_census,--from-directory,--store,--deadline-seconds,690"]


def test_the_args_and_the_gate_match_the_job_module():
    """The gate name the script sets is the one the module reads, and the flags exist on its CLI."""
    import jobs.merchant_protocol_profile_census as census

    assert census.FLAG == GATE
    source = Path(census.__file__).read_text()
    for flag in ('"--from-directory"', '"--store"', '"--deadline-seconds"'):
        assert flag in source, flag
    assert 'if __name__ == "__main__":\n    sys.exit(main())' in source


def test_the_task_timeout_is_the_deadline_plus_the_reviewed_headroom(tmp_path):
    _proc, calls, _state = _run(tmp_path, "prod", TAG)
    job = _one(calls, "run", "jobs", "create", JOB)
    timeout = int(re.fullmatch(r"(\d+)s", _flag(job, "--task-timeout")).group(1))
    deadline = int(_flag(job, "--args").split(",")[-1])
    assert (deadline, timeout) == (690, 780), "690 s of crawl + 90 s start-up and last writes, as reviewed"


def test_the_trigger_target_identity_and_schedule(tmp_path):
    _proc, calls, _state = _run(tmp_path, "prod", TAG)
    trigger = _one(calls, "scheduler", "jobs", "create", "http", TRIGGER)
    assert _flag(trigger, "--schedule") == "47 11 1,15 * *"
    assert _flag(trigger, "--time-zone") == "Etc/UTC"
    assert _flag(trigger, "--http-method") == "POST"
    assert _flag(trigger, "--oauth-service-account-email") == "sa-worker@pivota-prod.iam.gserviceaccount.com"
    assert _flag(trigger, "--uri") == (
        "https://us-west1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/pivota-prod/jobs/"
        f"{JOB}:run")
    assert "--oidc-service-account-email" not in " ".join(trigger), "the Run Admin API takes an OAuth token"


def test_a_new_trigger_is_paused_before_anything_else_is_written(tmp_path):
    """`scheduler jobs create` has no --paused: the very next mutating call must pause it."""
    _proc, calls, _state = _run(tmp_path, "prod", TAG, "--enable")
    mutating = [c[:3] for c in calls if c[:2] in (["run", "jobs"], ["scheduler", "jobs"])
                and c[2] in ("create", "update", "pause", "resume")]
    i = mutating.index(["scheduler", "jobs", "create"])
    assert mutating[i + 1] == ["scheduler", "jobs", "pause"], mutating


def test_enable_arms_both_switches_gate_first(tmp_path):
    proc, calls, state = _run(tmp_path, "prod", TAG, "--enable")
    assert proc.returncode == 0, proc.stderr
    assert _env_vars(_one(calls, "run", "jobs", "create", JOB))[GATE] == "true"
    assert _pair(state) == ("true", "ENABLED")
    order = [c[2] for c in calls if c[:2] in (["run", "jobs"], ["scheduler", "jobs"])
             and c[2] in ("create", "update", "resume")]
    assert order.index("resume") > order.index("create"), "the trigger resumes only after the job has gate=true"
    assert "ARMED" in proc.stdout


def test_a_rerun_without_enable_pauses_first_then_disarms(tmp_path):
    proc, calls, state = _run(tmp_path, "prod", TAG, state=INITIAL["armed"])
    assert proc.returncode == 0, proc.stderr
    first_mutation = next(c for c in calls if c[:2] in (["run", "jobs"], ["scheduler", "jobs"])
                          and c[2] in ("create", "update", "pause", "resume"))
    assert first_mutation[:4] == ["scheduler", "jobs", "pause", TRIGGER], "stop the firing before the job changes"
    assert _env_vars(_one(calls, "run", "jobs", "update", JOB))[GATE] == "false"
    _one(calls, "scheduler", "jobs", "update", "http", TRIGGER)
    assert not [c for c in calls if c[2:3] == ["create"]]
    assert _pair(state) == ("false", "PAUSED")


_MODES = [(), ("--enable",)]


@pytest.mark.parametrize("initial", sorted(INITIAL))
@pytest.mark.parametrize("mode", _MODES, ids=["dark", "enable"])
@pytest.mark.parametrize("fail", [{}] + [{"FAIL_AT": str(n)} for n in range(1, 8)] + [
    {"FAIL_KEYS": k} for k in ("run jobs create", "run jobs update", "scheduler jobs create",
                               "scheduler jobs update", "scheduler jobs pause", "scheduler jobs resume",
                               "run jobs update;scheduler jobs pause")] + [
    {"NOOP_KEYS": k} for k in ("run jobs update", "scheduler jobs pause", "scheduler jobs resume",
                               "run jobs update;scheduler jobs pause", "run jobs create;scheduler jobs resume")] + [
    {"READ_FAIL": "1"}],
    ids=lambda f: ",".join(f"{k}={v}" for k, v in f.items()) or "ok")
def test_the_two_switches_never_disagree_silently(tmp_path, initial, mode, fail):
    """Whatever step fails, the run ends with gate and trigger agreeing, or exits non-zero printing
    MISMATCH. A success always ends in exactly the pair asked for. FAIL_AT walks every mutating step;
    FAIL_KEYS also fails the recovery (the revert of a failed resume, the dark fallback)."""
    proc, _calls, state = _run(tmp_path, "prod", TAG, *mode, state=json.loads(json.dumps(INITIAL[initial])),
                               env=fail)
    if proc.returncode == 0:
        assert _pair(state) == (("true", "ENABLED") if mode else ("false", "PAUSED")), (proc.stderr, state)
        assert "MISMATCH" not in proc.stderr
    elif not _agrees(state):
        assert "MISMATCH" in proc.stderr, (fail, _pair(state), proc.stderr)
    if fail.get("READ_FAIL"):
        assert proc.returncode != 0, "a pair that cannot be read back is not a success"


def test_a_failed_resume_puts_the_gate_back(tmp_path):
    proc, _calls, state = _run(tmp_path, "prod", TAG, "--enable", env={"FAIL_KEYS": "scheduler jobs resume"})
    assert proc.returncode == 1
    assert _pair(state) == ("false", "PAUSED")
    assert "putting the gate back to false" in proc.stderr and "MISMATCH" not in proc.stderr


def _mutations(calls: List[List[str]]) -> List[List[str]]:
    return [c for c in calls if c[:2] in (["run", "jobs"], ["scheduler", "jobs"])
            and c[2] in ("create", "update", "pause", "resume")]


def test_the_revert_of_a_failed_resume_is_the_very_next_write(tmp_path):
    """The exit check would also go dark, but the revert must not lean on it: right after the failed
    resume, the job's gate goes back to false, before anything else is written."""
    _proc, calls, _state = _run(tmp_path, "prod", TAG, "--enable", env={"FAIL_KEYS": "scheduler jobs resume"})
    writes = _mutations(calls)
    i = max(n for n, c in enumerate(writes) if c[:3] == ["scheduler", "jobs", "resume"])
    assert writes[i + 1][:4] == ["run", "jobs", "update", JOB], writes
    assert _flag(writes[i + 1], "--update-env-vars") == f"{GATE}=false"


def test_an_arming_run_whose_revert_also_fails_goes_dark_at_exit(tmp_path):
    """Resume fails (write 4) and the revert fails too (write 5): the exit check pauses the trigger and
    puts the gate to false itself, so the run ends dark without a MISMATCH, still exiting 1."""
    proc, calls, state = _run(tmp_path, "prod", TAG, "--enable", env={"FAIL_AT": "4,5"})
    assert [c[:3] for c in _mutations(calls)][3:5] == [["scheduler", "jobs", "resume"], ["run", "jobs", "update"]]
    assert proc.returncode == 1
    assert _pair(state) == ("false", "PAUSED") and "MISMATCH" not in proc.stderr


def test_arming_an_existing_dark_lane_writes_the_gate_before_any_resume(tmp_path):
    proc, calls, state = _run(tmp_path, "prod", TAG, "--enable", state=INITIAL["dark"])
    assert proc.returncode == 0, proc.stderr
    writes = [c[:3] for c in _mutations(calls)]
    assert writes.index(["run", "jobs", "update"]) < writes.index(["scheduler", "jobs", "resume"]), writes
    assert _pair(state) == ("true", "ENABLED")


def test_a_failed_pause_while_disarming_changes_nothing_else(tmp_path):
    proc, calls, state = _run(tmp_path, "prod", TAG, state=INITIAL["armed"],
                              env={"FAIL_KEYS": "scheduler jobs pause"})
    assert proc.returncode == 1 and "nothing else was changed" in proc.stderr
    assert not [c for c in calls if c[:3] in (["run", "jobs", "create"], ["run", "jobs", "update"])]
    assert _pair(state) == ("true", "ENABLED"), "still armed, both switches agreeing"


@pytest.mark.parametrize("tag", ["latest", "0123456789ab", TAG.upper(), TAG + "0", "v1.2.3", TAG[:-1] + "g"])
def test_only_a_full_backend_commit_sha_is_accepted(tmp_path, tag):
    proc, calls, _state = _run(tmp_path, "prod", tag)
    assert proc.returncode == 2 and "40-hex" in proc.stderr, (tag, proc.stderr)
    assert calls == []


@pytest.mark.parametrize("args, message", [
    ((), "usage"),
    (("qa", TAG), "usage"),
    (("prod",), "backend-sha is required"),
    (("prod", TAG, "--enabled"), "unknown argument"),
    (("prod", TAG, "--enable", "extra"), "too many arguments"),
])
def test_bad_arguments_exit_2_before_touching_anything(tmp_path, args, message):
    proc, calls, _state = _run(tmp_path, *args)
    assert proc.returncode == 2 and message in proc.stderr, (proc.returncode, proc.stderr)
    assert calls == []


@pytest.mark.parametrize("missing, message", [
    ({"SUBNET_EXISTS": "0"}, "setup_crawl_egress.sh"),
    ({"IMAGE_EXISTS": "0"}, "build it first"),
    ({"SA_EXISTS": "0"}, "missing sa-worker@"),
])
def test_a_missing_prerequisite_stops_before_anything_is_written(tmp_path, missing, message):
    proc, calls, state = _run(tmp_path, "prod", TAG, env=missing)
    assert proc.returncode == 1 and message in proc.stderr
    assert not [c for c in calls if c[:2] in (["run", "jobs"], ["scheduler", "jobs"])
                and c[2] in ("create", "update", "pause", "resume")]
    assert _pair(state) == ("absent", "absent")


def test_staging_targets_the_staging_project(tmp_path):
    proc, calls, _state = _run(tmp_path, "staging", TAG)
    assert proc.returncode == 0, proc.stderr
    job = _one(calls, "run", "jobs", "create", JOB)
    assert _flag(job, "--service-account") == "sa-worker@pivota-staging.iam.gserviceaccount.com"
    assert _env_vars(job)["PIVOTA_ENV"] == "staging"
    assert "/namespaces/pivota-staging/" in _flag(_one(calls, "scheduler", "jobs", "create", "http", TRIGGER), "--uri")


# ---- the slot -------------------------------------------------------------------------------------

DAY = 24 * 60


def _hours(field: str) -> List[int]:
    if field == "*":
        return list(range(24))
    out: List[int] = []
    for part in field.split(","):
        lo, _, hi = part.partition("-")
        out.extend(range(int(lo), int(hi or lo) + 1))
    return out


def _starts(expr: str) -> List[int]:
    """Minutes of the day a cron fires on the days it fires. Day fields are ignored on purpose: the
    neighbours are daily or hourly, so a clash on any day is a clash on ours."""
    minute, hour, *_days = expr.split()
    if minute.startswith("*/"):
        step = int(minute[2:])
        return [h * 60 + m for h in _hours(hour) for m in range(0, 60, step)]
    return [h * 60 + int(minute) for h in _hours(hour)]


def _windows(expr: str, timeout_s: int, retries: int = 0) -> List[Tuple[int, int]]:
    span = -(-timeout_s * (retries + 1) // 60)
    return [(s, s + span) for s in _starts(expr)]


def _overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    # Either window may run past midnight: compare against the other shifted a day both ways.
    return any(a[0] < b[1] + d and b[0] + d < a[1] for d in (-DAY, 0, DAY))


def _last_int(pattern: str, text: str) -> int:
    found = re.findall(pattern, text)
    assert found, pattern
    return int(found[-1])


def _ours(tmp_path) -> List[Tuple[int, int]]:
    _proc, calls, _state = _run(tmp_path, "prod", TAG)
    job = _one(calls, "run", "jobs", "create", JOB)
    trigger = _one(calls, "scheduler", "jobs", "create", "http", TRIGGER)
    return _windows(_flag(trigger, "--schedule"), int(_flag(job, "--task-timeout").rstrip("s")),
                    int(_flag(job, "--max-retries")))


def _sweep() -> Tuple[str, List[Tuple[int, int]]]:
    text = (GCP / "setup_merchant_purchasability_sweep_job.sh").read_text()
    cron = re.search(r'^SCHEDULE="([^"]+)"', text, re.M).group(1)
    return "merchant-purchasability-sweep", _windows(
        cron, _last_int(r"--task-timeout (\d+)s", text), _last_int(r"--max-retries (\d+)", text))


def _reap() -> List[Tuple[str, List[Tuple[int, int]]]]:
    text = (GCP / "setup_reap_cart_proof_jobs.sh").read_text()
    retries = _last_int(r"--max-retries (\d+)", text)
    out = []
    for prefix, name in (("MIRROR", "reap-cart-proof-mirror"), ("ENRICHMENT", "reap-cart-proof-enrichment")):
        cron = re.search(rf'^{prefix}_SCHEDULE="([^"]+)"', text, re.M).group(1)
        timeout = int(re.search(rf"^{prefix}_TASK_TIMEOUT=(\d+)s", text, re.M).group(1))
        out.append((name, _windows(cron, timeout, retries)))
    return out


def _crawl_neighbours() -> List[Tuple[str, List[Tuple[int, int]]]]:
    """Every scheduled job on the crawl address with a window longer than a probe tick, read from its
    own script, retries included."""
    out = [_sweep(), *_reap()]
    tierb = (GCP / "setup_tierb_cart_link_eligibility_job.sh").read_text()
    out.append(("tierb-cart-link-eligibility", _windows(
        re.search(r'^SCHEDULE="([^"]+)"', tierb, re.M).group(1),
        _last_int(r"--task-timeout (\d+)s", tierb), _last_int(r"--max-retries (\d+)", tierb))))
    sched = (GCP / "setup_scheduler.sh").read_text()
    helper = sched[sched.index("mkcrawljob(){"):]
    helper = helper[: helper.index("\n}\n")]
    for name in ("external-seed-destination-sweep", "catalog-destination-sweep"):
        cron = re.search(rf'sched {name}-cron "([^"]+)"', sched).group(1)
        block = sched[sched.index(f"mkcrawljob {name}"):]
        block = block[: block.index("\n\n")]
        out.append((name, _windows(cron, _last_int(r"--task-timeout (\d+)s", helper + block),
                                   _last_int(r"--max-retries (\d+)", helper + block))))
    return out


# The prod one-off house windows (the controller's oneoff gate kits): hourly :08-:26 and :37-:46; the
# referral refresh HH:15-(HH+1):10 in its hours; daily 03:00-04:30, 05:15-06:15, 10:30-11:20.
HOUSE_WINDOWS = (
    [(h * 60 + 8, h * 60 + 26) for h in range(24)]
    + [(h * 60 + 37, h * 60 + 46) for h in range(24)]
    + [(h * 60 + 15, (h + 1) * 60 + 10) for h in (1, 5, 7, 9, 12, 14, 16, 18, 20, 22)]
    + [(180, 270), (315, 375), (630, 680)]
)


def test_the_schedule_is_every_two_weeks_and_the_run_ends_by_noon(tmp_path):
    _proc, calls, _state = _run(tmp_path, "prod", TAG)
    minute, hour, dom, month, dow = _flag(_one(calls, "scheduler", "jobs", "create", "http", TRIGGER),
                                          "--schedule").split()
    # The 1st and 15th: gaps of 14-17 days, against the 30-day fact age. A day-of-week field would OR
    # with the day of month, not narrow it.
    assert (dom, month, dow) == ("1,15", "*", "*")
    assert _ours(tmp_path) == [(11 * 60 + 47, 12 * 60)]


def test_the_slot_is_clear_of_every_crawl_neighbour_and_the_house_windows(tmp_path):
    ours = _ours(tmp_path)
    for name, theirs in _crawl_neighbours():
        clashes = [(a, b) for a in ours for b in theirs if _overlaps(a, b)]
        assert clashes == [], f"overlaps {name}: {clashes}"
    clashes = [(a, b) for a in ours for b in HOUSE_WINDOWS if _overlaps(a, b)]
    assert clashes == [], f"overlaps a house window: {clashes}"
    # The every-5 / every-10 minute crawl lanes cannot be avoided, only not coincided with.
    assert all(start % 5 for start, _ in ours), "a start on a multiple of 5 lands on the probe bursts"


def test_no_shopify_edge_pacer_holder_runs_in_the_slot(tmp_path):
    """The census takes no pacer lease, so no job that holds one may run beside it. The holders are
    found by what their scripts set, so a new pacer job fails this test until its window is checked."""
    holders = sorted(p.name for p in GCP.glob("*.sh")
                     if "CRAWL_SHOPIFY_EDGE_PACER_ENABLED=true" in p.read_text())
    assert holders == ["setup_merchant_purchasability_sweep_job.sh", "setup_reap_cart_proof_jobs.sh"], (
        "a new Shopify-edge pacer holder: add its window here")
    ours = _ours(tmp_path)
    for name, theirs in [_sweep(), *_reap()]:
        assert not [(a, b) for a in ours for b in theirs if _overlaps(a, b)], name
    assert "CRAWL_SHOPIFY_EDGE" not in SCRIPT.read_text().split("set -euo pipefail", 1)[1]


def test_15_47_would_have_overlapped_the_reap_mirror():
    """Why the slot is 11:47 and not the 15:47 first proposed: the mirror's longest window (13:13 +
    15000 s, to 17:23) covers it."""
    mirror = dict(_reap())["reap-cart-proof-mirror"]
    assert any(_overlaps((15 * 60 + 47, 16 * 60), w) for w in mirror)


# ---- the neighbours that must know about the job --------------------------------------------------

def test_reimage_jobs_registers_it_as_a_backend_job():
    text = (GCP / "reimage_jobs.sh").read_text()
    backend = re.search(r'^BACKEND_JOBS="([^"]+)"', text, re.M | re.S).group(1).split()
    assert JOB in backend


def test_the_job_failing_policy_watches_it():
    """A failed run must page through "prod: Cloud Run job failing": the name must not look ad hoc."""
    sys.path.insert(0, str(REPO / "tests"))
    try:
        import test_monitoring_policy_snapshot as snapshot
    finally:
        sys.path.pop(0)
    assert JOB in snapshot._provisioned_job_names()
    assert not snapshot._excluded(JOB)
