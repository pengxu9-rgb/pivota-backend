"""Cloud Build launcher for the prod test-transaction job (infra/gcp/cloudbuild.prod_test_transactions.yaml).

The ONE sanctioned exception to the no-prod-probe rule: this job, this key, on the owner's go per run. See
docs/runbooks/prod_test_transactions.md. Runs in the Cloud Build workspace from scripts COPIED OUT OF THE RELEASE IMAGE.

Subcommands, in build order:
  config    the submitted config is the release copy (sha256), as for the seed batch
  gateway   the gateway serves ONE revision (100% == latest ready == latest created, Ready); records the revision
            and the flag values each selected case depends on, and refuses a case whose flags are not as it expects
  lock      gs://pivota-prod-ops-locks/prod-test-transactions.lock, create-only (the gate is not a lock)
  run       gate -> ARM the job (the key mounted by version, the gate, the cases, the run id) -> execute --async ->
            poll BY NAME to a terminal state -> DISARM (redeploy with NO key, NO gate, NO cases) -> run.json
  publish   always: log.txt, rows.json, result.json (create-only) under
            gs://pivota-prod-ops-receipts/prod-test-transactions/<build id>/; releases the lock only when nothing can
            still run and the job is disarmed

The launcher never reads the key: it passes the Secret Manager secret NAME and a numbered VERSION to `--set-secrets`.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

try:  # tests import the package; Cloud Build runs this file from /workspace/release/scripts/ops
    from scripts.ops import prod_test_transactions as job
    from scripts.ops import seed_batch_launch as sbl
except ImportError:  # pragma: no cover - the Cloud Build path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.ops import prod_test_transactions as job  # type: ignore[no-redef]
    from scripts.ops import seed_batch_launch as sbl  # type: ignore[no-redef]

PROJECT = sbl.PROJECT
REGION = sbl.REGION
REPO = sbl.REPO
JOB_NAME = job.JOB_NAME
SERVICE_ACCOUNT = "sa-prod-test-txn@pivota-prod.iam.gserviceaccount.com"
KEY_SECRET = job.KEY_SECRET
KEY_MOUNT_DIR = job.KEY_MOUNT_DIR
KEY_FILE = job.KEY_FILE
LOCK_OBJECT = "prod-test-transactions.lock"
RECEIPTS_PREFIX = "prod-test-transactions"
CONFIG_PATH = "infra/gcp/cloudbuild.prod_test_transactions.yaml"
WORK = Path(os.getenv("PROD_TEST_TXN_WORKDIR", "/workspace/prod-test-transactions"))
TASK_TIMEOUT_SECONDS = 600

LaunchRefused = sbl.LaunchRefused
require = sbl.require
run = sbl.run


def build_id() -> str:
    return sbl.build_id()


# ------------------------------------------------------------------ pure checks (unit tested)

def config_matches(release_config: bytes, expected_sha256: str) -> None:
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""), "_CONFIG_SHA256 must be a sha256")
    require(hashlib.sha256(release_config).hexdigest() == expected_sha256,
            "the submitted build config is not the release's own cloudbuild.prod_test_transactions.yaml")


def parse_key_version(raw: str) -> int:
    """The key's Secret Manager version: a positive integer, never `latest` (a new version needs a new go)."""
    require(re.fullmatch(r"[1-9][0-9]{0,5}", raw or ""), "_KEY_VERSION must be a numbered secret version")
    return int(raw)


def parse_go_reference(raw: str) -> str:
    """Where the owner's go for THIS run was given (a session and time), recorded in the receipts."""
    text = (raw or "").strip()
    require(8 <= len(text) <= 200 and re.fullmatch(r"[A-Za-z0-9 :._/#'\"()-]+", text),
            "_GO_REFERENCE must say where the owner's go for this run was given")
    return text


def gateway_flags(service: dict) -> tuple[str, dict]:
    """The gateway's ONE serving revision and the plain values of the flags the cases depend on."""
    revision = sbl.serving_revision_name(service)  # same rules as web: 100% == latest ready == latest created, Ready
    container, = service["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in container.get("env", []) if "value" in e}
    return revision, {name: env.get(name) for name in job.GATEWAY_FLAG_NAMES}


def job_spec_args(*, release: str, digest: str, cases: list[str], key_version: int, gate: dict | None,
                  armed: bool) -> list[str]:
    """`gcloud run jobs deploy` args. Armed: the key (by version), the gate, the case ids and the run id.
    Disarmed: the same job with NO key mount (--clear-secrets), no gate, no cases: a re-execution refuses at once."""
    require(re.fullmatch(r"[0-9a-f]{40}", release) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
            "release must be a commit and digest a sha256 digest")
    require((gate is not None) == armed, "an armed job takes the gate; a disarmed one none")
    env = {"PIVOTA_ENV": "production", job.ENV + "RELEASE_COMMIT": release}
    secrets: list[str] = []
    if armed:
        job.select_cases(",".join(cases))  # the same validation the job applies
        env.update({job.ENV + "ARMED": "1", job.ENV + "CASES": ",".join(cases), job.ENV + "RUN_ID": build_id(),
                    job.ENV + "GATE": json.dumps(gate, separators=(",", ":"))})
        secrets = ["--set-secrets", f"{KEY_MOUNT_DIR}/{KEY_FILE}={KEY_SECRET}:{key_version}"]
    else:
        secrets = ["--clear-secrets"]
    # Egress through the default subnet and pivota-nat: every call leaves from the one reserved prod IP, so the
    # gateway logs attribute it. The job reaches the gateway hosts only (job.ALLOWED_HOSTS, enforced in code).
    return ["gcloud", "run", "jobs", "deploy", JOB_NAME, "--project", PROJECT, "--region", REGION,
            "--image", f"{REPO}@{digest}", "--service-account", SERVICE_ACCOUNT,
            "--network", "default", "--subnet", "default", "--vpc-egress", "all-traffic",
            "--tasks", "1", "--max-retries", "0", "--task-timeout", f"{TASK_TIMEOUT_SECONDS}s",
            "--labels", f"build-id={build_id()},armed={'yes' if armed else 'no'}",
            "--command", "python", "--args", "-m,scripts.ops.prod_test_transactions",
            *secrets,
            # ^@^ makes gcloud split on @ (the gate JSON contains commas). --set-env-vars REPLACES every env var.
            "--set-env-vars", "^@^" + "@".join(f"{k}={v}" for k, v in env.items())]


def summarise(rows: list[dict], *, run_state: dict, bad_lines: list[str], lock: str, cases: list[str]) -> dict:
    """The terminal result.json. `ok` only for a terminal execution whose ONE run line says ok, with one line per
    selected case, every case passed and nothing left open."""
    run_rows = [r for r in rows if r.get("kind") == "run"]
    case_rows = [r for r in rows if r.get("kind") == "case"]
    line = run_rows[0] if len(run_rows) == 1 else None
    left_open = sorted({ref for r in case_rows for ref in (r.get("left_open") or [])})
    ok = (bool(line) and line.get("ok") is True and run_state.get("state") == "EXECUTION_SUCCEEDED" and not bad_lines
          and sorted(r.get("case") for r in case_rows) == sorted(cases)
          and all(r.get("pass") is True for r in case_rows) and not left_open)
    return {"ok": ok, "execution": run_state.get("execution"), "execution_state": run_state.get("state"),
            "run_error": run_state.get("error"), "run": line, "run_lines": len(run_rows), "bad_lines": bad_lines,
            "cases": [{k: r.get(k) for k in ("case", "pass", "expected", "got", "created", "cancelled", "left_open",
                                             "error")} for r in case_rows],
            "left_open": left_open, "lock": lock, "written_at": datetime.now(timezone.utc).isoformat()}


# ------------------------------------------------------------------ subcommands (gcloud)

def cmd_config(args) -> None:
    config_matches(Path(args.scripts).parent.joinpath(CONFIG_PATH).read_bytes(), args.config_sha256)


def cmd_gateway(args) -> None:
    cases = job.select_cases(args.cases)
    service = json.loads(run(["gcloud", "run", "services", "describe", "gateway", "--project", PROJECT,
                              "--region", REGION, "--format", "json"]))
    revision, flags = gateway_flags(service)
    job.require_flags(cases, flags)
    (WORK / "gateway.json").write_text(json.dumps({"revision": revision, "flags": flags}))
    print("gateway serving revision:", revision, "flags:", json.dumps(flags, sort_keys=True))


def lock_url() -> str:
    return f"gs://{sbl.LOCK_BUCKET}/{LOCK_OBJECT}"


def cmd_lock(args) -> None:
    body = {"build": build_id(), "cases": args.cases, "at": datetime.now(timezone.utc).isoformat()}
    (WORK / "lock.json").write_text(json.dumps(body))
    done = subprocess.run(["gcloud", "storage", "cp", "--if-generation-match=0", str(WORK / "lock.json"), lock_url()],
                          capture_output=True, text=True, timeout=120, check=False)
    require(done.returncode == 0, f"lock not taken: {done.stderr.strip()[-300:]}")
    (WORK / "lock.held").write_text(build_id())
    print("lock held by build", build_id())


def release_lock() -> str:
    """Delete the lock only if its body names THIS build, by the generation read with it."""
    if not (WORK / "lock.held").exists():
        return "no lock held by this build"
    described = json.loads(run(["gcloud", "storage", "objects", "describe", lock_url(), "--format", "json"]))
    generation = str(described.get("generation") or "")
    require(re.fullmatch(r"[0-9]+", generation), "the lock has no generation")
    body = json.loads(run(["gcloud", "storage", "cat", f"{lock_url()}#{generation}"]))
    require(body.get("build") == build_id(), "the lock is not this build's")
    run(["gcloud", "storage", "rm", f"--if-generation-match={generation}", lock_url()])
    return f"lock {generation} released"


def cmd_run(args) -> None:
    state = {"execution": None, "state": None, "error": None, "execute_attempted": False, "armed": False}
    path = WORK / "run.json"
    path.write_text(json.dumps(state))
    common = {"release": args.release, "digest": args.digest}
    try:
        require((WORK / "lock.held").exists(), "run needs this build's lock")
        require((WORK / "gateway.json").exists(), "run needs the gateway step's reading")
        cases = [c.case_id for c in job.select_cases(args.cases)]
        key_version = parse_key_version(args.key_version)
        parse_go_reference(args.go_reference)
        gate_run = subprocess.run([sys.executable, str(Path(args.scripts) / "ops" / "oneoff_gate.py")],
                                  capture_output=True, text=True, timeout=420, check=False)
        print(gate_run.stdout, end="")
        gate = sbl.gate_json(gate_run.stdout, gate_run.returncode, datetime.now(timezone.utc))
        pre_arm = sbl.list_executions(JOB_NAME)
        busy = sbl.unfinished(pre_arm)
        require(not busy, f"an execution of this job is already running: {', '.join(busy)}")
        state["executions_pre_arm"] = sorted(r["metadata"]["name"] for r in pre_arm)
        state["armed"] = True          # before the deploy: a deploy that fails half-way still gets disarmed
        path.write_text(json.dumps(state))
        run(job_spec_args(**common, cases=cases, key_version=key_version, gate=gate, armed=True))
        rows = sbl.list_executions(JOB_NAME)
        before = sorted(r["metadata"]["name"] for r in rows)
        require(before == state["executions_pre_arm"] and not sbl.unfinished(rows),
                "an execution started while the job was being armed")
        state.update(execute_attempted=True, executions_before=before)
        path.write_text(json.dumps(state))
        try:
            out = run(["gcloud", "run", "jobs", "execute", JOB_NAME, "--project", PROJECT, "--region", REGION,
                       "--async", "--format", "value(metadata.name)"], timeout=120).strip().splitlines()
            require(out and out[-1].startswith(JOB_NAME + "-"), "execute did not name an execution")
            state["execution"] = out[-1]
        except Exception as error:  # noqa: BLE001 - the server may have created it anyway
            state["execute_error"] = f"{type(error).__name__}: {error}"[:300]
            state["execution"] = sbl.find_started_execution(JOB_NAME, state["executions_before"])
            state["execution_found_by_lookup"] = state["execution"] is not None
            path.write_text(json.dumps(state))
            require(state["execution"] is not None,
                    "execute failed and exactly one execution since arming could not be found: lock stays held")
        path.write_text(json.dumps(state))
        state["state"] = sbl.wait_for_execution(state["execution"])
        path.write_text(json.dumps(state))
        require(state["state"] is not None, "the execution did not reach a terminal state before the deadline")
    except Exception as error:  # noqa: BLE001 - publish reads run.json whatever happened
        state["error"] = f"{type(error).__name__}: {error}"[:500]
        path.write_text(json.dumps(state))
        raise
    finally:
        if state["armed"] and (not state["execute_attempted"] or state["state"] is not None):
            try:
                run(job_spec_args(**common, cases=[], key_version=1, gate=None, armed=False))
                state["disarmed"] = True
            except Exception as error:  # noqa: BLE001
                state["disarmed"] = False
                state["error"] = (state.get("error") or "") + f"; disarm failed: {error}"[:300]
            path.write_text(json.dumps(state))


def read_log(execution: str, *, sleep=None, monotonic=None) -> tuple[str, str | None]:
    """The execution's text lines, re-read until its run line is ingested or the deadline passes."""
    import time

    sleep, monotonic = sleep or time.sleep, monotonic or time.monotonic
    started, text, error = monotonic(), "", None
    while True:
        try:
            text = run(["gcloud", "logging", "read",
                        f'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="{execution}"',
                        "--project", PROJECT, "--order", "asc", "--limit", "5000", "--format", "value(textPayload)"],
                       timeout=120)
            error = None
            rows, _ = job.parse_result_lines(text)
            if any(r.get("kind") == "run" for r in rows):
                return text, None
        except (LaunchRefused, subprocess.TimeoutExpired) as caught:
            error = str(caught)[:300]
        if monotonic() - started > sbl.LOG_DEADLINE_SECONDS:
            return text, error or "no run line ingested before the deadline"
        sleep(sbl.LOG_POLL_SECONDS)


def put(prefix: str, name: str, content: str) -> None:
    (WORK / name).write_text(content)
    run(["gcloud", "storage", "cp", "--if-generation-match=0", str(WORK / name), f"{prefix}/{name}"])


def cmd_publish(args) -> None:
    state = json.loads((WORK / "run.json").read_text()) if (WORK / "run.json").exists() else {
        "execution": None, "state": None, "error": "run step did not start"}
    cases = []
    try:
        cases = [c.case_id for c in job.select_cases(args.cases)]
    except Exception:  # noqa: BLE001 - an unreadable selection still publishes; the result is not ok
        pass
    prefix = f"gs://{sbl.RECEIPTS_BUCKET}/{RECEIPTS_PREFIX}/{build_id()}"
    log, log_error = (read_log(state["execution"]) if state.get("execution") else ("", "no execution"))
    put(prefix, "log.txt", log if log else f"(no log: {log_error})\n")
    rows, bad = job.parse_result_lines(log)
    put(prefix, "rows.json", json.dumps(rows, indent=1))
    held = sbl.lock_decision(state)
    unsure = False
    if held is None and state.get("armed"):
        try:
            executions = sbl.list_executions(JOB_NAME)
            known = set(state.get("executions_pre_arm") or []) | {state.get("execution")}
            others = sorted(r["metadata"]["name"] for r in executions if r["metadata"]["name"] not in known)
        except Exception as error:  # noqa: BLE001
            others, unsure = [], True
            held = f"HELD: could not list the job's executions: {error}"[:300]
        if others:
            held = f"HELD: a FOREIGN execution of this job ran during the run: {', '.join(others)}"
    lock = held or "to be released after result.json (outcome in lock.json)"
    result = summarise(rows, run_state=state, bad_lines=bad, lock=lock, cases=cases)
    if (state.get("armed") and state.get("disarmed") is not True) or unsure or (
            held and held.startswith(("HELD: a FOREIGN", "HELD: the execute call failed"))):
        result["ok"] = False
    gateway = json.loads((WORK / "gateway.json").read_text()) if (WORK / "gateway.json").exists() else None
    result.update(build=build_id(), release=args.release, go_reference=args.go_reference, gateway=gateway,
                  log_error=log_error, job_armed=bool(state.get("armed")) and state.get("disarmed") is not True)
    put(prefix, "result.json", json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))
    if held is None:
        try:
            lock = release_lock()
        except Exception as error:  # noqa: BLE001
            lock = f"HELD: release failed: {error}"[:300]
        put(prefix, "lock.json", json.dumps({"lock": lock}))
    require(not result["left_open"], f"checkouts or carts were LEFT OPEN: {', '.join(result['left_open'])}")
    require(result["ok"], "the run did not finish cleanly; read result.json")
    require(not lock.startswith("HELD"), f"the run finished but the lock is still held: {lock}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("config", "gateway", "lock", "run", "publish"))
    parser.add_argument("--scripts", default="/workspace/release/scripts")
    for name in ("release", "digest", "config-sha256", "cases", "key-version", "go-reference"):
        parser.add_argument("--" + name, default="")
    args = parser.parse_args(argv)
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        {"config": cmd_config, "gateway": cmd_gateway, "lock": cmd_lock, "run": cmd_run,
         "publish": cmd_publish}[args.command](args)
    except (LaunchRefused, job.CaseError, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
        print(f"LAUNCH_REFUSED {args.command}: {type(error).__name__}: {error}", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
