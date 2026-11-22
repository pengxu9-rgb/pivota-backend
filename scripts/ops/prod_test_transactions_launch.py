"""Cloud Build launcher for the prod test-transaction job (infra/gcp/cloudbuild.prod_test_transactions.yaml).

The ONE sanctioned exception to the no-prod-probe rule: this job, this key, on the owner's go per run. See
docs/runbooks/prod_test_transactions.md. Runs in the Cloud Build workspace from scripts COPIED OUT OF THE RELEASE IMAGE.

Subcommands, in build order:
  config    the submitted config is the release copy (sha256), as for the seed batch
  gateway   the gateway serves ONE revision (100% == latest ready == latest created, Ready); records the revision, its
            commit and the flag values each selected case depends on; refuses a case whose flags or reviewed gateway
            release do not match
  lock      gs://pivota-prod-ops-locks/prod-test-transactions.lock, create-only (the gate is not a lock)
  run       gate -> ARM the job (the key, the private targets and, only for checkout cases, the test issuer key, each
            mounted by numbered version; the gate, the cases, the run id) -> execute --async -> poll BY NAME to a
            terminal state or EXECUTION_DEADLINE_SECONDS -> DISARM (--clear-secrets, no gate, no cases) -> run.json
  publish   always: log.txt, rows.json, result.json (create-only) under
            gs://pivota-prod-ops-receipts/prod-test-transactions/<build id>/; releases the lock only when nothing can
            still run and the job is disarmed. Fails the build when anything was LEFT OPEN.

The launcher never reads a secret: it passes Secret Manager names and numbered versions to `--set-secrets`. It reads
logs only through two reviewed log views: LOG_VIEW (the job's own lines) and SHADOW_LOG_VIEW (the gateway's shadow
lookup lines).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

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
SHADOW_EVENT = "ucp_shopify_catalog_attribution"
LOCK_OBJECT = "prod-test-transactions.lock"
RECEIPTS_PREFIX = "prod-test-transactions"
CONFIG_PATH = "infra/gcp/cloudbuild.prod_test_transactions.yaml"
# The log views the launcher reads through (roles/logging.viewAccessor on these views only). A view filter allows
# nesting depth 1, so one OR-of-ANDs view was refused (2026-11-20); each source has its own flat view (runbook):
#   LOG_VIEW:        resource.type="cloud_run_job" AND resource.labels.job_name="prod-test-transactions"
#   SHADOW_LOG_VIEW: resource.type="cloud_run_revision" AND resource.labels.service_name="gateway"
#                    AND jsonPayload.event="ucp_shopify_catalog_attribution"
LOG_BUCKET = "_Default"
LOG_LOCATION = "global"
LOG_VIEW = "prod-test-transactions"
SHADOW_LOG_VIEW = "prod-test-transactions-gateway-shadow"
WORK = Path(os.getenv("PROD_TEST_TXN_WORKDIR", "/workspace/prod-test-transactions"))
TASK_TIMEOUT_SECONDS = 600
# The run step has RUN_STEP_SECONDS (the yaml's `run` timeout). The wait is budgeted FROM THE STEP'S START, leaving
# DISARM_RESERVE_SECONDS for the cancel call (60) + its terminal poll (CANCEL_POLL_SECONDS + one poll interval) + the
# disarm deploy (300), so a slow gate or deploy can never push the disarm past the step's end and leave keys mounted.
# A poll never starts unless its sleep AND its describe (DESCRIBE_TIMEOUT_SECONDS) still fit before its deadline.
RUN_STEP_SECONDS = 2100
EXECUTION_DEADLINE_SECONDS = 840          # the task itself times out at 600 s
EXECUTION_POLL_SECONDS = 15
DESCRIBE_TIMEOUT_SECONDS = 60
CANCEL_POLL_SECONDS = 120
DISARM_RESERVE_SECONDS = 60 + CANCEL_POLL_SECONDS + EXECUTION_POLL_SECONDS + 300

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


def parse_version(raw: str, name: str) -> int:
    """A Secret Manager version: a positive integer, never `latest` (a new version needs a new go)."""
    require(re.fullmatch(r"[1-9][0-9]{0,5}", raw or ""), f"{name} must be a numbered secret version")
    return int(raw)


def parse_go_reference(raw: str) -> str:
    """Where the owner's go for THIS run was given (a session and time), recorded in the receipts."""
    text = (raw or "").strip()
    require(8 <= len(text) <= 200 and re.fullmatch(r"[A-Za-z0-9 :._/#'\"()-]+", text),
            "_GO_REFERENCE must say where the owner's go for this run was given")
    return text


def serving_revision(service: dict) -> str:
    status, metadata = service.get("status", {}), service.get("metadata", {})
    traffic = [t for t in status.get("traffic", []) if t.get("percent")]
    require(len(traffic) == 1 and traffic[0]["percent"] == 100 and traffic[0].get("revisionName"),
            "the gateway must serve one revision at 100%")
    revision = traffic[0]["revisionName"]
    require(status.get("latestReadyRevisionName") == revision == status.get("latestCreatedRevisionName"),
            "the gateway is mid-rollout or has a failed newer revision")
    require(status.get("observedGeneration") == metadata.get("generation"), "the gateway's generation is not observed")
    ready = [c for c in status.get("conditions", []) if c.get("type") == "Ready"]
    require(ready and ready[0].get("status") == "True", "the gateway is not Ready")
    return revision


def gateway_reading(service: dict, revision_doc: dict) -> tuple[str, str | None, dict]:
    """The gateway's ONE serving revision, the image digest that revision RUNS (status.imageDigest; not the
    PIVOTA_COMMIT_SHA env, which a `deploy --image` can leave stale), and the plain values of the cases' flags."""
    revision = serving_revision(service)
    require(revision_doc.get("metadata", {}).get("name") == revision, "the revision read is not the serving one")
    digest = str(revision_doc.get("status", {}).get("imageDigest") or "")
    digest = digest.rsplit("@", 1)[-1] if "@" in digest else None
    require(digest is None or re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "the serving image digest is unreadable")
    container, = service["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in container.get("env", []) if "value" in e}
    return revision, digest, {name: env.get(name) for name in job.GATEWAY_FLAG_NAMES}


def needs_user_key(cases: list[str]) -> bool:
    return any(c.needs_user for c in job.select_cases(",".join(cases))) if cases else False


def job_spec_args(*, release: str, digest: str, cases: list[str], key_version: int, targets_version: int,
                  gate: dict | None, armed: bool, user_key_version: int | None = None) -> list[str]:
    """`gcloud run jobs deploy` args. Armed: the key, the private targets and (ONLY when a selected case needs an end
    user) the test issuer key, each by numbered version; the gate, the case ids and the run id. Disarmed: the same job
    with NO secret mount (--clear-secrets), no gate, no cases: a re-execution refuses at once."""
    require(re.fullmatch(r"[0-9a-f]{40}", release) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
            "release must be a commit and digest a sha256 digest")
    require((gate is not None) == armed, "an armed job takes the gate; a disarmed one none")
    env = {"PIVOTA_ENV": "production", job.ENV + "RELEASE_COMMIT": release}
    if armed:
        job.select_cases(",".join(cases))  # the same validation the job applies
        env.update({job.ENV + "ARMED": "1", job.ENV + "CASES": ",".join(cases), job.ENV + "RUN_ID": build_id(),
                    job.ENV + "GATE": json.dumps(gate, separators=(",", ":"))})
        mounts = [f"{job.KEY_MOUNT_DIR}/{job.KEY_FILE}={job.KEY_SECRET}:{key_version}",
                  f"{job.TARGETS_MOUNT_DIR}/{job.TARGETS_FILE}={job.TARGETS_SECRET}:{targets_version}"]
        if needs_user_key(cases):
            require(user_key_version is not None, "a selected case needs the test issuer key: pass _USER_KEY_VERSION")
            mounts.append(f"{job.USER_KEY_MOUNT_DIR}/{job.USER_KEY_FILE}={job.USER_KEY_SECRET}:{user_key_version}")
        else:
            require(user_key_version is None, "no selected case needs the test issuer key: do not mount it")
        secrets = ["--set-secrets", ",".join(mounts)]
    else:
        secrets = ["--clear-secrets"]
    # No VPC egress: the job does not leave through the default subnet's NAT (the payment-allowlisted IP). Its calls
    # are attributed by the test agent's own key; it reaches the gateway host only (job.ALLOWED_HOSTS, in code).
    return ["gcloud", "run", "jobs", "deploy", JOB_NAME, "--project", PROJECT, "--region", REGION,
            "--image", f"{REPO}@{digest}", "--service-account", SERVICE_ACCOUNT,
            "--tasks", "1", "--max-retries", "0", "--task-timeout", f"{TASK_TIMEOUT_SECONDS}s",
            "--labels", f"build-id={build_id()},armed={'yes' if armed else 'no'}",
            # --args=VALUE in ONE token: the value starts with a dash, so `--args -m,...` is read as a second flag
            # (gcloud exits 2, "argument --args: expected one argument"; build 798b8dd8, 2026-11-21; #2367).
            "--command", "python", "--args=-m,scripts.ops.prod_test_transactions",
            *secrets,
            # ^@^ makes gcloud split on @ (the gate JSON contains commas). --set-env-vars REPLACES every env var.
            "--set-env-vars", "^@^" + "@".join(f"{k}={v}" for k, v in env.items())]


def shadow_check(case_rows: list[dict], lines: list[dict] | None) -> dict | None:
    """Path 3, ADVISORY: every gateway shadow lookup line in the run's window. The line carries no request id or
    caller and the job publishes no seller host (not even a hash: an unsalted host hash is reversible), so other
    callers' lines are counted too: this never decides `ok`. None when no US storefront create passed. No line read
    is UNKNOWN (all_shadow_unsent None), never a vacuous True: a view that misses the lines reads as empty."""
    wanted = [r for r in case_rows if r.get("shadow_lookup") and r.get("pass") is True]
    if not wanted:
        return None
    if lines is None:
        return {"advisory": True, "wanted": len(wanted), "lines": None, "note": "the shadow lines could not be read"}
    if not lines:
        return {"advisory": True, "wanted": len(wanted), "lines": 0, "all_shadow_unsent": None,
                "note": "no shadow line was read in the window"}
    return {"advisory": True, "wanted": len(wanted), "lines": len(lines),
            "all_shadow_unsent": all(line.get("mode") == "shadow" and line.get("sent") is False for line in lines),
            "reasons": sorted({str(line.get("reason")) for line in lines})}


def summarise(rows: list[dict], *, run_state: dict, bad_lines: list[str], lock: str, cases: list[str],
              shadow: dict | None = None) -> dict:
    """The terminal result.json. `ok` only for a terminal execution whose ONE run line says ok, with one line per
    selected case, every case passed and nothing left open. A selected CHECKOUT case with no line may have created a
    checkout before the task died: it is `unknown:<case>` in left_open. Storefront carts that expire on the seller are
    listed, never a failure (owner decision 2026-11-19); the shadow check is advisory."""
    run_rows = [r for r in rows if r.get("kind") == "run"]
    case_rows = [r for r in rows if r.get("kind") == "case"]
    line = run_rows[0] if len(run_rows) == 1 else None
    seen = {r.get("case") for r in case_rows}
    checkout_ids = {c.case_id for c in job.CASES if c.kind == "checkout"}
    missing = [f"unknown:{c}" for c in cases if c not in seen and c in checkout_ids]
    left_open = sorted({ref for r in case_rows for ref in (r.get("left_open") or [])} | set(missing))
    ok = (bool(line) and line.get("ok") is True and run_state.get("state") == "EXECUTION_SUCCEEDED" and not bad_lines
          and sorted(r.get("case") for r in case_rows) == sorted(cases)
          and all(r.get("pass") is True for r in case_rows) and not left_open)
    return {"ok": ok, "execution": run_state.get("execution"), "execution_state": run_state.get("state"),
            "run_error": run_state.get("error"), "run": line, "run_lines": len(run_rows), "bad_lines": bad_lines,
            "cases": [{k: r.get(k) for k in ("case", "pass", "expected", "got", "created", "cancelled", "left_open",
                                             "expires_on_seller", "error")} for r in case_rows],
            "left_open": left_open,
            "expires_on_seller": sorted({ref for r in case_rows for ref in (r.get("expires_on_seller") or [])}),
            "shadow_lookup": shadow, "lock": lock, "written_at": datetime.now(timezone.utc).isoformat()}


# ------------------------------------------------------------------ subcommands (gcloud)

def cmd_config(args) -> None:
    config_matches(Path(args.scripts).parent.joinpath(CONFIG_PATH).read_bytes(), args.config_sha256)


def cmd_gateway(args) -> None:
    cases = job.select_cases(args.cases)
    service = json.loads(run(["gcloud", "run", "services", "describe", "gateway", "--project", PROJECT,
                              "--region", REGION, "--format", "json"]))
    revision_doc = json.loads(run(["gcloud", "run", "revisions", "describe", serving_revision(service), "--project",
                                   PROJECT, "--region", REGION, "--format", "json"]))
    revision, digest, flags = gateway_reading(service, revision_doc)
    job.require_flags(cases, flags)
    job.require_reviewed_gateway(cases, digest)
    commit = job.REVIEWED_GATEWAY_DIGESTS.get(digest)
    (WORK / "gateway.json").write_text(json.dumps({"revision": revision, "digest": digest, "reviewed_commit": commit,
                                                   "flags": flags}))
    print("gateway serving revision:", revision, "digest:", digest, "flags:", json.dumps(flags, sort_keys=True))


def lock_url() -> str:
    return f"gs://{sbl.LOCK_BUCKET}/{LOCK_OBJECT}"


def cmd_lock(args) -> None:
    body = {"build": build_id(), "cases": args.cases, "at": datetime.now(timezone.utc).isoformat()}
    (WORK / "lock.json").write_text(json.dumps(body))
    done = subprocess.run(["gcloud", "storage", "cp", "--if-generation-match=0", str(WORK / "lock.json"), lock_url()],
                          capture_output=True, text=True, timeout=120, check=False)
    require(done.returncode == 0, f"lock not taken: {sbl.stderr_excerpt(done.stderr)}")
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


def wait_budget(step_started: float, now: float) -> float:
    """Seconds the wait may take: at most EXECUTION_DEADLINE_SECONDS, and never past the point where cancel + disarm
    still fit inside the run step."""
    return max(0.0, min(EXECUTION_DEADLINE_SECONDS, RUN_STEP_SECONDS - DISARM_RESERVE_SECONDS - (now - step_started)))


def wait_for_execution(name: str, *, deadline: float = EXECUTION_DEADLINE_SECONDS, sleep=None,
                       monotonic=None) -> str | None:
    """Poll BY NAME to a terminal state for at most `deadline` seconds."""
    sleep, monotonic = sleep or time.sleep, monotonic or time.monotonic
    started = monotonic()
    while True:
        try:
            execution = json.loads(run(["gcloud", "run", "jobs", "executions", "describe", name, "--project", PROJECT,
                                        "--region", REGION, "--format", "json"], timeout=DESCRIBE_TIMEOUT_SECONDS))
            state = sbl.execution_state(execution)
            if state:
                return state
        except (LaunchRefused, subprocess.TimeoutExpired, ValueError):
            pass
        if monotonic() - started + EXECUTION_POLL_SECONDS + DESCRIBE_TIMEOUT_SECONDS > deadline:
            return None
        sleep(EXECUTION_POLL_SECONDS)


def cancel_execution(name: str, state: dict, *, wait=None) -> None:
    """The wait ran out: cancel the execution, so the disarm may follow, and poll briefly for its terminal state."""
    state["cancel_requested"] = True
    try:
        run(["gcloud", "run", "jobs", "executions", "cancel", name, "--project", PROJECT, "--region", REGION,
             "--quiet"], timeout=60)
    except Exception as error:  # noqa: BLE001 - recorded; the disarm still runs
        state["cancel_error"] = f"{type(error).__name__}: {error}"[:200]
    state["state"] = (wait or wait_for_execution)(name, deadline=CANCEL_POLL_SECONDS)


def spec_inputs(args) -> dict:
    cases = [c.case_id for c in job.select_cases(args.cases)]
    user = needs_user_key(cases)
    require(user or not args.user_key_version, "no selected case needs the test issuer key: leave _USER_KEY_VERSION empty")
    return {"cases": cases, "key_version": parse_version(args.key_version, "_KEY_VERSION"),
            "targets_version": parse_version(args.targets_version, "_TARGETS_VERSION"),
            "user_key_version": parse_version(args.user_key_version, "_USER_KEY_VERSION") if user else None}


def cmd_run(args, *, monotonic=None) -> None:
    monotonic = monotonic or time.monotonic
    step_started = monotonic()
    state = {"execution": None, "state": None, "error": None, "execute_attempted": False, "armed": False}
    path = WORK / "run.json"
    path.write_text(json.dumps(state))
    common = {"release": args.release, "digest": args.digest}
    try:
        require((WORK / "lock.held").exists(), "run needs this build's lock")
        require((WORK / "gateway.json").exists(), "run needs the gateway step's reading")
        inputs = spec_inputs(args)
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
        run(job_spec_args(**common, **inputs, gate=gate, armed=True))
        rows = sbl.list_executions(JOB_NAME)
        before = sorted(r["metadata"]["name"] for r in rows)
        require(before == state["executions_pre_arm"] and not sbl.unfinished(rows),
                "an execution started while the job was being armed")
        state.update(execute_attempted=True, executions_before=before, started_at=datetime.now(timezone.utc).isoformat())
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
        state["state"] = wait_for_execution(state["execution"], deadline=wait_budget(step_started, monotonic()))
        if state["state"] is None:
            cancel_execution(state["execution"], state)
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(state))
        require(not state.get("cancel_requested"), "the execution ran past its budget and was cancelled")
        require(state["state"] is not None, "the execution did not reach a terminal state before the deadline")
    except Exception as error:  # noqa: BLE001 - publish reads run.json whatever happened
        state["error"] = f"{type(error).__name__}: {error}"[:500]
        path.write_text(json.dumps(state))
        raise
    finally:
        # ALWAYS disarm an armed job, on every exit path. A running execution keeps what it already mounted, so the
        # disarm cannot disturb it; it only stops any NEW execution from mounting the keys. Whether the lock may be
        # released is decided separately (sbl.lock_decision): an unidentified or non-terminal execution keeps it held.
        if state["armed"]:
            try:
                run(job_spec_args(**common, cases=[], key_version=1, targets_version=1, gate=None, armed=False))
                state["disarmed"] = True
            except Exception as error:  # noqa: BLE001
                state["disarmed"] = False
                state["error"] = (state.get("error") or "") + f"; disarm failed: {error}"[:300]
            path.write_text(json.dumps(state))


def read_view(filter_text: str, *, fmt: str, limit: int, view: str = LOG_VIEW) -> str:
    if view not in (LOG_VIEW, SHADOW_LOG_VIEW):
        raise LaunchRefused(f"not a reviewed log view: {view!r}")
    return run(["gcloud", "logging", "read", filter_text, "--project", PROJECT, "--bucket", LOG_BUCKET,
                "--location", LOG_LOCATION, "--view", view, "--order", "asc", "--limit", str(limit),
                "--format", fmt], timeout=120)


def read_log(execution: str, *, sleep=None, monotonic=None) -> tuple[str, str | None]:
    """The execution's text lines (through the view), re-read until its run line is ingested or the deadline passes."""
    sleep, monotonic = sleep or time.sleep, monotonic or time.monotonic
    started, text, error = monotonic(), "", None
    while True:
        try:
            text = read_view(f'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="{execution}"',
                             fmt="value(textPayload)", limit=5000)
            error = None
            rows, _ = job.parse_result_lines(text)
            if any(r.get("kind") == "run" for r in rows):
                return text, None
        except (LaunchRefused, subprocess.TimeoutExpired) as caught:
            error = str(caught)[:300]
        if monotonic() - started > sbl.LOG_DEADLINE_SECONDS:
            return text, error or "no run line ingested before the deadline"
        sleep(sbl.LOG_POLL_SECONDS)


def read_shadow_lines(started_at: str, finished_at: str) -> list[dict] | None:
    """The gateway's shadow lookup lines in the run's window (plus 3 min: they are written after the lookup settles).
    Only mode / sent / reason are kept. Read through SHADOW_LOG_VIEW (LOG_VIEW holds the job's lines only).
    None when the view cannot be read."""
    try:
        end = (datetime.fromisoformat(finished_at) + timedelta(minutes=3)).isoformat()
        text = read_view(f'resource.type="cloud_run_revision" AND resource.labels.service_name="gateway" AND '
                         f'jsonPayload.event="{SHADOW_EVENT}" AND timestamp>="{started_at}" AND timestamp<="{end}"',
                         fmt="json", limit=200, view=SHADOW_LOG_VIEW)
        lines = []
        for entry in json.loads(text or "[]"):
            payload = entry.get("jsonPayload") or {}
            lines.append({"mode": payload.get("mode"), "sent": payload.get("sent"), "reason": payload.get("reason")})
        return lines
    except (LaunchRefused, subprocess.TimeoutExpired, ValueError, TypeError):
        return None


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
    shadow_ids = {c.case_id for c in job.CASES if c.shadow_lookup}
    case_rows = [dict(r, shadow_lookup=r.get("case") in shadow_ids) for r in rows if r.get("kind") == "case"]
    run_line = next((r for r in rows if r.get("kind") == "run"), {})
    shadow = None
    if any(r["shadow_lookup"] and r.get("pass") is True for r in case_rows):
        lines = (read_shadow_lines(state["started_at"], state["finished_at"])
                 if state.get("started_at") and state.get("finished_at") else None)
        shadow = shadow_check(case_rows, lines)
    result = summarise(rows, run_state=state, bad_lines=bad, lock=lock, cases=cases, shadow=shadow)
    if (state.get("armed") and state.get("disarmed") is not True) or unsure or (
            held and held.startswith(("HELD: a FOREIGN", "HELD: the execute call failed"))):
        result["ok"] = False
    gateway = json.loads((WORK / "gateway.json").read_text()) if (WORK / "gateway.json").exists() else None
    result.update(build=build_id(), release=args.release, go_reference=args.go_reference, gateway=gateway,
                  targets_sha256=run_line.get("targets_sha256"), log_error=log_error,
                  job_armed=bool(state.get("armed")) and state.get("disarmed") is not True)
    put(prefix, "result.json", json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))
    if held is None:
        try:
            lock = release_lock()
        except Exception as error:  # noqa: BLE001
            lock = f"HELD: release failed: {error}"[:300]
        put(prefix, "lock.json", json.dumps({"lock": lock}))
    require(not result["left_open"], f"checkouts may be LEFT OPEN: {', '.join(result['left_open'])}")
    require(result["ok"], "the run did not finish cleanly; read result.json")
    require(not lock.startswith("HELD"), f"the run finished but the lock is still held: {lock}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("config", "gateway", "lock", "run", "publish"))
    parser.add_argument("--scripts", default="/workspace/release/scripts")
    for name in ("release", "digest", "config-sha256", "cases", "key-version", "targets-version", "user-key-version",
                 "go-reference"):
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
