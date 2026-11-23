"""Cloud Build launcher steps for the reviewed seed batch job (infra/gcp/cloudbuild.seed_batch.yaml).

Runs in the Cloud Build workspace (cloud-sdk image pinned by digest; stdlib + gcloud), from scripts COPIED OUT OF THE
RELEASE IMAGE by the build's first step, so every step is the reviewed release's own code. _CONFIG_SHA256 must equal
the release copy of this config: that catches a stale operator checkout (a config other than the release's); it is
not a defence against a submitter who deliberately passes a mismatched config and sha.

Subcommands, in build order:
  web       web's ONE serving revision (100% traffic == latest ready == latest created, generation observed, Ready)
            runs the release digest and mounts the batch's named-referral manifest with its pin
  lock      create gs://pivota-prod-ops-locks/seed-batch.lock with ifGenerationMatch=0 (the gate is not a lock)
  run       gate (a real scripts/ops/oneoff_gate.py run, parsed into the job's gate dict) -> deploy the ARMED job ->
            execute --async (the execution name is saved at once) -> poll it BY NAME to a terminal state ->
            DISARM the job (redeploy with no write flag, gate, plan, approval or receipts) -> READ THE JOB BACK
            (anything beyond the disarmed set = not disarmed) -> write run.json.
            A failed execute call may still have started an execution: it is looked up as the ONE execution
            created since arming; none or several keeps the lock held
  publish   always: log.txt, rows.json, then result.json (each create-only), to
            gs://pivota-prod-ops-receipts/seed-batch/<batch>/<mode>-<build id>/; then releases the lock ONLY when
            nothing can still run and the job is disarmed, recording the outcome in lock.json

Before `lock`, any failure stops the build with nothing taken and nothing run. From `lock` on, `run` may fail
(allowFailure) so that `publish` always writes what exists and decides about the lock.
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
import time

PROJECT = "pivota-prod"
REGION = "us-west1"
REPO = "us-west1-docker.pkg.dev/pivota-shared/pivota/backend"
SERVICE_ACCOUNT = "sa-worker@pivota-prod.iam.gserviceaccount.com"
JOB_PREFIX = "reviewed-seed-batch-"
NAMED_ENV = "NAMED_REFERRAL_VARIANTS_MANIFEST"
NAMED_MOUNT = "/var/run/pivota-private"
NAMED_FILE = "named_referral_variants.json"
NAMED_SECRET = "named-referral-variants-manifest"
BATCH_MOUNT = "/var/run/pivota-seed-batch"
RECEIPTS_BUCKET = "pivota-prod-ops-receipts"
LOCK_BUCKET = "pivota-prod-ops-locks"
LOCK_OBJECT = "seed-batch.lock"
GATE_SOURCE = "scripts/ops/oneoff_gate.py"
WORK = Path(os.getenv("SEED_BATCH_WORKDIR", "/workspace/seed-batch"))
MODES = ("plan", "apply", "rollback")
SECRET_IDS = {"MANIFEST": "seed-batch-manifest", "PLAN": "seed-batch-plan", "APPROVAL": "seed-batch-approval",
              "RECEIPTS": "seed-batch-receipts", "NAMED": NAMED_SECRET}
NEEDED = {"plan": {"MANIFEST", "NAMED"}, "apply": {"MANIFEST", "PLAN", "APPROVAL", "NAMED"},
          "rollback": {"MANIFEST", "PLAN", "APPROVAL", "RECEIPTS", "NAMED"}}
EXECUTION_POLL_SECONDS = 15
READBACK_TIMEOUT_SECONDS = 30       # the disarm read-back (`gcloud run jobs describe`)
EXECUTION_DEADLINE_SECONDS = 1900      # task timeout 1800 s + cold start; under the run step's timeout
LOG_POLL_SECONDS = 20
LOG_DEADLINE_SECONDS = 300
TERMINAL = {"EXECUTION_SUCCEEDED", "EXECUTION_FAILED", "EXECUTION_CANCELLED"}


class LaunchRefused(RuntimeError):
    pass


def require(test, message):
    if not test:
        raise LaunchRefused(message)


def stderr_excerpt(stderr: str, *, head: int = 400, tail: int = 200) -> str:
    """gcloud's stderr, short enough for a message: its FIRST `ERROR:` line (the reason; a parse error is followed by
    the whole usage text, so a tail alone keeps only the flag list) and then the last `tail` characters."""
    text = (stderr or "").strip()
    if len(text) <= head + tail:
        return text
    errors = [line.strip() for line in text.splitlines() if line.lstrip().startswith("ERROR:")]
    first = (errors[0] if errors else text)[:head]
    return f"{first} ... {text[-tail:]}"


def run(args: list[str], *, timeout: int = 300) -> str:
    done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    require(done.returncode == 0, f"{' '.join(args[:5])} exited {done.returncode}: {stderr_excerpt(done.stderr)}")
    return done.stdout


def build_id() -> str:
    value = os.getenv("BUILD_ID", "")
    require(re.fullmatch(r"[0-9a-f-]{36}", value), "BUILD_ID is not mapped into this step")
    return value


def job_name(batch_id: str) -> str:
    require(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,28}[a-z0-9])", batch_id), "batch id is not a job-name slug")
    return JOB_PREFIX + batch_id


# ------------------------------------------------------------------ pure checks (unit tested)

def config_matches(release_config: bytes, expected_sha256: str) -> None:
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""), "_CONFIG_SHA256 must be a sha256")
    require(hashlib.sha256(release_config).hexdigest() == expected_sha256,
            "the submitted build config is not the release's own cloudbuild.seed_batch.yaml")


_SUMMARY = re.compile(r"cpu_peak_5m=([0-9.eE+-]+) db_job_executions_running=([0-9]+) utc=\d{2}:\d{2}Z\Z")


def gate_json(output: str, returncode: int, now: datetime) -> dict:
    """The job's gate dict, built ONLY from a real oneoff_gate.py run: exit 0, last line GATE_OPEN, and the line
    before it its summary, parsed into the readings the job enforces."""
    lines = [line for line in output.splitlines() if line.strip()]
    require(returncode == 0 and lines and lines[-1] == "GATE_OPEN",
            f"gate closed: {lines[-1] if lines else 'no output'} (exit {returncode})")
    match = _SUMMARY.fullmatch(lines[-2]) if len(lines) > 1 else None
    require(match, "gate summary line is unreadable")
    return {"verdict": "GATE_OPEN", "observed_at": now.isoformat(), "summary": lines[-2], "source": GATE_SOURCE,
            "cpu_peak_5m": float(match.group(1)), "db_job_executions_running": int(match.group(2))}


def serving_revision_name(service: dict) -> str:
    status, metadata = service.get("status", {}), service.get("metadata", {})
    traffic = [t for t in status.get("traffic", []) if t.get("percent")]
    require(len(traffic) == 1 and traffic[0]["percent"] == 100 and traffic[0].get("revisionName"),
            "web must serve one revision at 100%")
    revision = traffic[0]["revisionName"]
    require(status.get("latestReadyRevisionName") == revision == status.get("latestCreatedRevisionName"),
            "web is mid-rollout or has a failed newer revision")
    require(status.get("observedGeneration") == metadata.get("generation"), "web's latest generation is not observed")
    ready = [c for c in status.get("conditions", []) if c.get("type") == "Ready"]
    require(ready and ready[0].get("status") == "True", "web is not Ready")
    return revision


def check_revision(revision: dict, *, digest: str, named_sha256: str) -> None:
    status = revision.get("status", {})
    require(status.get("imageDigest") == f"{REPO}@{digest}", "web's serving revision does not run the release digest")
    spec = revision["spec"]
    container, = spec["containers"]
    env = {e["name"]: e.get("value") for e in container.get("env", [])}
    require(env.get(NAMED_ENV + "_PATH") == f"{NAMED_MOUNT}/{NAMED_FILE}" and env.get(NAMED_ENV + "_SHA256") == named_sha256,
            "web's serving revision does not pin the batch's named-referral manifest")
    mounts = {m["name"]: m["mountPath"] for m in container.get("volumeMounts", [])}
    volumes = [v for v in spec.get("volumes", []) if mounts.get(v["name"]) == NAMED_MOUNT]
    require(len(volumes) == 1 and volumes[0].get("secret", {}).get("secretName") == NAMED_SECRET
            and [i["path"] for i in volumes[0]["secret"].get("items", [])] == [NAMED_FILE],
            "web's serving revision lacks the named-referral secret mount")


def parse_secret(name: str, raw: str) -> tuple[str, int, str]:
    try:
        value = json.loads(raw)
    except ValueError:
        raise LaunchRefused(f"{name.lower()} secret must be [id, version, sha256]") from None
    require(isinstance(value, list) and len(value) == 3, f"{name.lower()} secret must be [id, version, sha256]")
    secret, version, sha = value
    require(secret == SECRET_IDS[name] and type(version) is int and version > 0
            and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha),
            f"{name.lower()} secret must be {SECRET_IDS[name]} at a numbered version with its sha256")
    return secret, version, sha


def parse_secrets(mode: str, raw: dict, named_sha256: str) -> dict:
    require(mode in MODES, "mode must be plan, apply or rollback")
    given = {name for name, value in raw.items() if value}
    require(given == NEEDED[mode], f"{mode} needs exactly the secrets {sorted(NEEDED[mode])}")
    secrets = {name: parse_secret(name, raw[name]) for name in sorted(given)}
    require(secrets["NAMED"][2] == named_sha256, "the job's named-referral pin differs from web's")
    return secrets


def job_spec_args(*, mode: str, batch_id: str, release: str, digest: str, secrets: dict, gate: dict | None,
                  armed: bool) -> list[str]:
    """`gcloud run jobs deploy` args. Armed: the mode's inputs, the gate and (apply/rollback) the write flag.
    Disarmed: the same job with only the manifest and named pin, no gate, no write flag (a re-execution refuses).

    EVERY deploy first clears the job's volume mounts and volumes (gcloud applies those before the secret changes).
    `--set-secrets` alone drops the old secret MOUNTS but leaves their secret VOLUME definitions on the template (seen
    on the prod test-transaction job, build fc63d22a, 2026-11-22), so a disarmed job would still list the plan,
    approval or receipts secret as an unmounted volume, and stale volumes would pile up across deploys."""
    require(mode in MODES, "mode must be plan, apply or rollback")
    require(re.fullmatch(r"[0-9a-f]{40}", release) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
            "release must be a commit and digest a sha256 digest")
    require(set(secrets) == NEEDED[mode], f"{mode} needs exactly the secrets {sorted(NEEDED[mode])}")
    require((gate is not None) == armed, "an armed job takes the gate; a disarmed one none")
    names = sorted(NEEDED[mode] - {"NAMED"}) if armed else ["MANIFEST"]
    named_secret, named_version, named_sha = secrets["NAMED"]
    mounts = [f"{NAMED_MOUNT}/{NAMED_FILE}={named_secret}:{named_version}"]
    env = {"PIVOTA_ENV": "production", "DB_AUTO_MIGRATE": "false", "DB_STATEMENT_TIMEOUT_SECONDS": "30",
           "DB_COMMAND_TIMEOUT_SECONDS": "120", "REVIEWED_SEED_BATCH_RELEASE_COMMIT": release,
           NAMED_ENV + "_PATH": f"{NAMED_MOUNT}/{NAMED_FILE}", NAMED_ENV + "_SHA256": named_sha}
    for name in names:
        secret, version, sha = secrets[name]
        path = f"{BATCH_MOUNT}-{name.lower()}/{name.lower()}.json"
        mounts.append(f"{path}={secret}:{version}")
        env[f"REVIEWED_SEED_BATCH_{name}_PATH"] = path
        env[f"REVIEWED_SEED_BATCH_{name}_SHA256"] = sha
    if armed:
        env["REVIEWED_SEED_BATCH_GATE"] = json.dumps(gate, separators=(",", ":"))
        if mode != "plan":
            env["REVIEWED_SEED_BATCH_WRITE_ENABLED"] = "1"
    # DATABASE_URL stays :latest (the shared prod secret); the job pins its host and name by sha256 itself.
    # The apply re-reads the storefront, so it egresses through the crawl subnet; plan/rollback make no outbound
    # fetch and use the default subnet.
    return ["gcloud", "run", "jobs", "deploy", job_name(batch_id), "--project", PROJECT, "--region", REGION,
            "--image", f"{REPO}@{digest}", "--service-account", SERVICE_ACCOUNT,
            "--network", "default", "--subnet", "pivota-crawl" if mode == "apply" else "default",
            "--vpc-egress", "all-traffic", "--tasks", "1", "--max-retries", "0", "--task-timeout", "1800s",
            "--labels", f"build-id={build_id()},batch-mode={mode}",
            # --args=VALUE in ONE token: the value starts with a dash, so `--args -m,...` is read as a second flag
            # (gcloud exits 2, "argument --args: expected one argument"; #2367).
            "--command", "python", f"--args=-m,scripts.ops.reviewed_seed_batch,{mode}",
            "--clear-volume-mounts", "--clear-volumes",
            "--set-secrets", ",".join(["DATABASE_URL=DATABASE_URL:latest", *mounts]),
            # ^@^ makes gcloud split on @ (JSON values contain commas). --set-env-vars REPLACES every env var.
            "--set-env-vars", "^@^" + "@".join(f"{k}={v}" for k, v in env.items())]


def execution_state(execution: dict) -> str | None:
    """EXECUTION_SUCCEEDED / FAILED / CANCELLED once the execution has a completion time; None while running."""
    status = execution.get("status", {})
    if not status.get("completionTime"):
        return None
    conditions = {c.get("type"): c for c in status.get("conditions", [])}
    completed = conditions.get("Completed", {})
    if completed.get("status") == "True":
        return "EXECUTION_SUCCEEDED"
    if status.get("cancelledCount"):
        return "EXECUTION_CANCELLED"
    return "EXECUTION_FAILED"


def parse_lines(text: str) -> tuple[list[dict], list[str]]:
    """Every SEED_BATCH_RESULT line decoded on its own; unreadable ones are kept, never fatal."""
    import base64

    prefix = "SEED_BATCH_RESULT "
    rows, bad = [], []
    for line in text.splitlines():
        if prefix not in line:
            continue
        payload = line.split(prefix, 1)[1].strip()
        try:
            rows.append(json.loads(base64.b64decode(payload, validate=True)))
        except ValueError:
            bad.append(payload[:200])
    return rows, bad


def summarise(rows: list[dict], *, mode: str, run_state: dict, bad_lines: list[str], lock: str) -> dict:
    """The terminal result.json. `ok` only for a terminal execution with exactly one clean batch line."""
    batch_rows = [r for r in rows if r.get("kind") == "batch"]
    entries = [r for r in rows if r.get("kind") == "entry"]
    line = batch_rows[0] if len(batch_rows) == 1 else None
    wanted = "planned" if mode == "plan" else "committed"
    ok = (bool(line) and run_state.get("state") == "EXECUTION_SUCCEEDED" and not bad_lines and line.get("status") == wanted
          and bool(entries) and all(e.get("status") == wanted for e in entries))
    return {"mode": mode, "ok": ok, "execution": run_state.get("execution"), "execution_state": run_state.get("state"),
            "run_error": run_state.get("error"), "batch": line, "batch_lines": len(batch_rows), "bad_lines": bad_lines,
            "entries": [{k: e.get(k) for k in ("seed_id", "status", "phase", "error", "receipt_sha256")} for e in entries],
            "decision": "pending_keep" if ok and mode == "apply" else None, "lock": lock,
            "written_at": datetime.now(timezone.utc).isoformat()}


# ------------------------------------------------------------------ subcommands (gcloud)

def cmd_config(args) -> None:
    config_matches(Path(args.scripts).parent.joinpath("infra/gcp/cloudbuild.seed_batch.yaml").read_bytes(), args.config_sha256)


def cmd_web(args) -> None:
    service = json.loads(run(["gcloud", "run", "services", "describe", "web", "--project", PROJECT, "--region", REGION,
                              "--format", "json"]))
    revision_name = serving_revision_name(service)
    revision = json.loads(run(["gcloud", "run", "revisions", "describe", revision_name, "--project", PROJECT,
                               "--region", REGION, "--format", "json"]))
    check_revision(revision, digest=args.digest, named_sha256=args.named_sha256)
    print("web serving revision:", revision_name)


def lock_url() -> str:
    return f"gs://{LOCK_BUCKET}/{LOCK_OBJECT}"


def cmd_lock(args) -> None:
    body = {"build": build_id(), "batch_id": args.batch_id, "mode": args.mode, "at": datetime.now(timezone.utc).isoformat()}
    (WORK / "lock.json").write_text(json.dumps(body))
    done = subprocess.run(["gcloud", "storage", "cp", "--if-generation-match=0", str(WORK / "lock.json"), lock_url()],
                          capture_output=True, text=True, timeout=120, check=False)
    require(done.returncode == 0, f"lock not taken: {stderr_excerpt(done.stderr)}")
    (WORK / "lock.held").write_text(build_id())
    print("lock held by build", build_id())


def release_lock() -> str:
    """Delete the lock only if its body names THIS build, by the generation read with it."""
    if not (WORK / "lock.held").exists():
        return "no lock held by this build"
    described = json.loads(run(["gcloud", "storage", "objects", "describe", lock_url(), "--format", "json"]))
    generation = str(described.get("generation") or "")
    require(re.fullmatch(r"[0-9]+", generation), "the lock has no generation")
    # The body of THAT generation, so a lock re-created in between is never released by this build.
    body = json.loads(run(["gcloud", "storage", "cat", f"{lock_url()}#{generation}"]))
    require(body.get("build") == build_id(), "the lock is not this build's")
    run(["gcloud", "storage", "rm", f"--if-generation-match={generation}", lock_url()])
    return f"lock {generation} released"


def wait_for_execution(name: str, *, sleep=None, monotonic=None) -> str | None:
    sleep, monotonic = sleep or time.sleep, monotonic or time.monotonic
    started = monotonic()
    while True:
        try:
            execution = json.loads(run(["gcloud", "run", "jobs", "executions", "describe", name, "--project", PROJECT,
                                        "--region", REGION, "--format", "json"], timeout=60))
            state = execution_state(execution)
            if state:
                return state
        except (LaunchRefused, subprocess.TimeoutExpired, ValueError):
            pass  # a client error is not a terminal state: keep polling until the deadline
        if monotonic() - started > EXECUTION_DEADLINE_SECONDS:
            return None
        sleep(EXECUTION_POLL_SECONDS)


def list_executions(job: str) -> list[dict]:
    return json.loads(run(["gcloud", "run", "jobs", "executions", "list", "--job", job, "--project", PROJECT,
                           "--region", REGION, "--format", "json"], timeout=120))


def unfinished(rows: list[dict]) -> list[str]:
    return sorted(r["metadata"]["name"] for r in rows if not r.get("status", {}).get("completionTime"))


def find_started_execution(job: str, before: list[str]) -> str | None:
    """After a failed execute call: the ONE execution of this job that did not exist just before the call, else None.
    Names, not timestamps, so no clock skew can matter."""
    new = sorted(r["metadata"]["name"] for r in list_executions(job) if r["metadata"]["name"] not in set(before))
    return new[0] if len(new) == 1 else None


def cmd_run(args) -> None:
    state = {"execution": None, "state": None, "error": None, "execute_attempted": False, "armed": False}
    path = WORK / "run.json"
    path.write_text(json.dumps(state))
    job = job_name(args.batch_id) if args.batch_id else ""
    try:
        require((WORK / "lock.held").exists(), "run needs this build's lock")
        secrets = parse_secrets(args.mode, {"MANIFEST": args.manifest, "PLAN": args.plan, "APPROVAL": args.approval,
                                            "RECEIPTS": args.receipts, "NAMED": args.named}, args.named_sha256)
        # The gate is observed HERE, right before execute, and only from a real gate run in this build.
        gate_run = subprocess.run([sys.executable, str(Path(args.scripts) / "ops" / "oneoff_gate.py")],
                                  capture_output=True, text=True, timeout=420, check=False)
        print(gate_run.stdout, end="")
        gate = gate_json(gate_run.stdout, gate_run.returncode, datetime.now(timezone.utc))
        common = dict(mode=args.mode, batch_id=args.batch_id, release=args.release, digest=args.digest, secrets=secrets)
        # No execution of this job may be running (a hand-started one would write alongside ours).
        pre_arm = list_executions(job) if job else []
        busy = unfinished(pre_arm)
        require(not busy, f"an execution of this job is already running: {', '.join(busy)}")
        # Every execution that existed before arming; any other name, except ours, is foreign (publish checks).
        state["executions_pre_arm"] = sorted(r["metadata"]["name"] for r in pre_arm)
        state["armed"] = True          # set before the deploy: a deploy that fails half-way is still disarmed
        path.write_text(json.dumps(state))
        run(job_spec_args(**common, gate=gate, armed=True))
        # ONE listing: what it shows is both the check and the list the lookup below compares against.
        rows = list_executions(job)
        before = sorted(r["metadata"]["name"] for r in rows)
        require(before == state["executions_pre_arm"] and not unfinished(rows),
                "an execution started while the job was being armed")
        # From here an execution may exist even if the call below fails: record that BEFORE calling.
        state.update(execute_attempted=True, executions_before=before)
        path.write_text(json.dumps(state))
        try:
            out = run(["gcloud", "run", "jobs", "execute", job, "--project", PROJECT, "--region", REGION,
                       "--async", "--format", "value(metadata.name)"], timeout=120).strip().splitlines()
            require(out and out[-1].startswith(job + "-"), "execute did not name an execution")
            state["execution"] = out[-1]
        except Exception as error:  # noqa: BLE001 - the server may have created it anyway
            state["execute_error"] = f"{type(error).__name__}: {error}"[:300]
            state["execution"] = find_started_execution(job, state["executions_before"])
            # A run started by hand in this window would look the same: publish keeps the lock for a human to confirm.
            state["execution_found_by_lookup"] = state["execution"] is not None
            path.write_text(json.dumps(state))
            require(state["execution"] is not None,
                    "execute failed and exactly one execution since arming could not be found: lock stays held")
        path.write_text(json.dumps(state))
        state["state"] = wait_for_execution(state["execution"])
        path.write_text(json.dumps(state))
        require(state["state"] is not None, "the execution did not reach a terminal state before the deadline")
    except Exception as error:  # noqa: BLE001 - publish reads run.json whatever happened
        state["error"] = f"{type(error).__name__}: {error}"[:500]
        path.write_text(json.dumps(state))
        raise
    finally:
        # Disarm once nothing can still be running: execute never attempted, or the execution is terminal.
        if state["armed"] and (not state["execute_attempted"] or state["state"] is not None):
            try:
                disarm_secrets = parse_secrets(args.mode, {"MANIFEST": args.manifest, "PLAN": args.plan,
                                                           "APPROVAL": args.approval, "RECEIPTS": args.receipts,
                                                           "NAMED": args.named}, args.named_sha256)
                run(job_spec_args(mode=args.mode, batch_id=args.batch_id, release=args.release, digest=args.digest,
                                  secrets=disarm_secrets, gate=None, armed=False))
                # Disarmed only when the job READ BACK carries nothing beyond the disarmed set: exit 0 is not proof.
                problems = disarmed_spec_problems(json.loads(run(
                    ["gcloud", "run", "jobs", "describe", job, "--project", PROJECT, "--region", REGION,
                     "--format", "json"], timeout=READBACK_TIMEOUT_SECONDS)), disarm_secrets)
                state["disarm_check"] = problems
                require(not problems, f"the disarmed job still carries: {'; '.join(problems)}")
                state["disarmed"] = True
            except Exception as error:  # noqa: BLE001
                state["disarmed"] = False
                state["error"] = (state.get("error") or "") + f"; disarm failed: {error}"[:300]
            path.write_text(json.dumps(state))


DISARMED_SECRET_ENV = frozenset({"DATABASE_URL"})
ARMING_ENV = frozenset({"REVIEWED_SEED_BATCH_GATE", "REVIEWED_SEED_BATCH_WRITE_ENABLED"} | {
    f"REVIEWED_SEED_BATCH_{name}_{part}" for name in ("PLAN", "APPROVAL", "RECEIPTS") for part in ("PATH", "SHA256")})


def disarmed_spec_problems(doc: dict, secrets: dict) -> list[str]:
    """What the job read back (`gcloud run jobs describe --format json`) carries BEYOND the disarmed set. Disarmed =
    secret volumes of exactly the manifest and the named pin, each at its given version; mounts only at their two
    directories; DATABASE_URL the only secret env; no gate, write flag or plan / approval / receipts env. Names and
    version numbers only, never values. An unreadable document is itself a problem."""
    allowed = {secrets["MANIFEST"][0]: str(secrets["MANIFEST"][1]), secrets["NAMED"][0]: str(secrets["NAMED"][1])}
    allowed_mounts = {NAMED_MOUNT, f"{BATCH_MOUNT}-manifest"}
    try:
        template = doc["spec"]["template"]["spec"]["template"]["spec"]
        containers = template["containers"]
    except (KeyError, TypeError):
        return ["an unreadable job document"]
    problems = []
    for volume in template.get("volumes") or []:
        source = volume.get("secret")
        if not source:
            continue
        name = source.get("secretName")
        versions = sorted(str(item.get("key")) for item in source.get("items") or [])
        if name not in allowed:
            problems.append(f"secret volume {volume.get('name')} ({name})")
        elif versions != [allowed[name]]:
            problems.append(f"secret volume {volume.get('name')} ({name}) at versions {versions}")
    for container in containers:
        problems += [f"volume mount {m.get('mountPath')}" for m in container.get("volumeMounts") or []
                     if m.get("mountPath") not in allowed_mounts]
        env = container.get("env") or []
        problems += [f"secret env {e.get('name')}" for e in env
                     if "valueFrom" in e and e.get("name") not in DISARMED_SECRET_ENV]
        problems += [f"env {e.get('name')}" for e in env if e.get("name") in ARMING_ENV]
    return problems


def lock_decision(state: dict) -> str | None:
    """None when the lock may be released; otherwise why it must stay held."""
    if state.get("execute_attempted") and not state.get("execution"):
        return "HELD: execute was attempted and no execution could be identified; check before removing the lock"
    if state.get("execution") and state.get("state") not in TERMINAL:
        return "HELD: the execution's state is not terminal; do not remove the lock"
    # Armed and not KNOWN to be disarmed (the disarm failed, was skipped while something may run, or the run step
    # was killed before it got there): the job can still be executed with the write flag.
    if state.get("armed") and state.get("disarmed") is not True:
        return "HELD: the job is still ARMED (disarm failed or never ran); disarm it before removing the lock"
    if state.get("execution_found_by_lookup"):
        return ("HELD: the execute call failed and the execution was found by lookup; confirm it is this build's "
                "before removing the lock")
    return None


def read_log(execution: str, *, sleep=None, monotonic=None) -> tuple[str, str | None]:
    """The execution's text lines, re-read until its batch line is ingested or the deadline passes."""
    sleep, monotonic = sleep or time.sleep, monotonic or time.monotonic
    started, text, error = monotonic(), "", None
    while True:
        try:
            text = run(["gcloud", "logging", "read",
                        f'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="{execution}"',
                        "--project", PROJECT, "--order", "asc", "--limit", "5000", "--format", "value(textPayload)"],
                       timeout=120)
            error = None
            rows, _ = parse_lines(text)
            if any(r.get("kind") == "batch" for r in rows):
                return text, None
        except (LaunchRefused, subprocess.TimeoutExpired) as caught:
            error = str(caught)[:300]
        if monotonic() - started > LOG_DEADLINE_SECONDS:
            return text, error or "no batch line ingested before the deadline"
        sleep(LOG_POLL_SECONDS)


def put(prefix: str, name: str, content: str) -> None:
    (WORK / name).write_text(content)
    # Create-only: an existing object is an error, never a silent skip.
    run(["gcloud", "storage", "cp", "--if-generation-match=0", str(WORK / name), f"{prefix}/{name}"])


def cmd_publish(args) -> None:
    state = json.loads((WORK / "run.json").read_text()) if (WORK / "run.json").exists() else {
        "execution": None, "state": None, "error": "run step did not start"}
    prefix = f"gs://{RECEIPTS_BUCKET}/seed-batch/{args.batch_id}/{args.mode}-{build_id()}"
    log, log_error = (read_log(state["execution"]) if state.get("execution") else ("", "no execution"))
    put(prefix, "log.txt", log if log else f"(no log: {log_error})\n")
    rows, bad = parse_lines(log)
    put(prefix, "rows.json", json.dumps(rows, indent=1))
    held = lock_decision(state)
    unsure = False
    if held is None and state.get("armed"):
        # Since arming, no execution but ours may have existed, finished or not.
        try:
            executions = list_executions(job_name(args.batch_id))
            known = set(state.get("executions_pre_arm") or []) | {state.get("execution")}
            others = sorted(r["metadata"]["name"] for r in executions if r["metadata"]["name"] not in known)
            running = [n for n in unfinished(executions) if n in others]
        except Exception as error:  # noqa: BLE001
            others, running, unsure = [], [], True
            held = f"HELD: could not list the job's executions: {error}"[:300]
        if others:
            held = (f"HELD: a FOREIGN execution of this job ran during the batch: {', '.join(others)}"
                    f" (still running: {', '.join(running) or 'none'})")
    lock = held or "to be released after result.json (outcome in lock.json)"
    result = summarise(rows, mode=args.mode, run_state=state, bad_lines=bad, lock=lock)
    if state.get("armed") and state.get("disarmed") is not True:
        result["ok"], result["decision"] = False, None
    if unsure or (held and held.startswith(("HELD: a FOREIGN", "HELD: the execute call failed"))):
        result["ok"], result["decision"] = False, None
    result.update(build=build_id(), batch_id=args.batch_id, release=args.release, log_error=log_error,
                  job_armed=bool(state.get("armed")) and state.get("disarmed") is not True)
    # result.json first, so a failure in the release below cannot lose it.
    put(prefix, "result.json", json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))
    if held is None:
        try:
            lock = release_lock()
        except Exception as error:  # noqa: BLE001
            lock = f"HELD: release failed: {error}"[:300]
        put(prefix, "lock.json", json.dumps({"lock": lock}))
    require(result["ok"], "batch did not finish cleanly; read result.json")
    require(not lock.startswith("HELD"), f"the batch finished but the lock is still held: {lock}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("config", "web", "lock", "run", "publish"))
    parser.add_argument("--scripts", default="/workspace/release/scripts")
    for name in ("mode", "batch-id", "release", "digest", "named-sha256", "config-sha256", "manifest", "plan",
                 "receipts", "named", "approval"):
        parser.add_argument("--" + name, default="")
    args = parser.parse_args(argv)
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        {"config": cmd_config, "web": cmd_web, "lock": cmd_lock, "run": cmd_run, "publish": cmd_publish}[args.command](args)
    except (LaunchRefused, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
        print(f"LAUNCH_REFUSED {args.command}: {type(error).__name__}: {error}", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
