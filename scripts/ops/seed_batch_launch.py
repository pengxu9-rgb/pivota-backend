"""Cloud Build launcher steps for the reviewed seed batch job (infra/gcp/cloudbuild.seed_batch.yaml).

Runs in the Cloud Build workspace (cloud-sdk image, stdlib + gcloud), from scripts COPIED OUT OF THE RELEASE IMAGE
by the build's first step, so the launcher is always the reviewed release's own code. Subcommands, in build order:

  gate      run scripts/ops/oneoff_gate.py; write gate.json only on a literal last line GATE_OPEN
  web       web's single 100% revision runs the release (tag or its digest) with the named-referral mount + pin
  deploy    create/replace the reviewed-seed-batch-<batch> job spec (no execution)
  execute   run it once and wait, then read its SEED_BATCH_RESULT lines from Cloud Logging
  publish   write log.txt, rows.json and LAST result.json to gs://<receipts bucket>/seed-batch/<batch>/<build>/

Every step fails closed: any error exits non-zero and later steps do not run. `publish` always runs (waitFor
"-" is not used; it is the last step and reads whatever earlier steps left) and writes result.json LAST, so an
async watcher verifies from that one object.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PROJECT = "pivota-prod"
REGION = "us-west1"
REPO = "us-west1-docker.pkg.dev/pivota-shared/pivota/backend"
SERVICE_ACCOUNT = "sa-worker@pivota-prod.iam.gserviceaccount.com"
JOB_PREFIX = "reviewed-seed-batch-"
NAMED_ENV = "NAMED_REFERRAL_VARIANTS_MANIFEST"
NAMED_MOUNT = "/var/run/pivota-private"
NAMED_FILE = "named_referral_variants.json"
BATCH_MOUNT = "/var/run/pivota-seed-batch"
WORK = Path(os.getenv("SEED_BATCH_WORKDIR", "/workspace/seed-batch"))
MODES = ("plan", "apply", "rollback")


class LaunchRefused(RuntimeError):
    pass


def require(test, message):
    if not test:
        raise LaunchRefused(message)


def run(args: list[str], *, timeout: int = 900) -> str:
    done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    require(done.returncode == 0, f"{' '.join(args[:4])} exited {done.returncode}: {done.stderr[-400:]}")
    return done.stdout


# ------------------------------------------------------------------ pure checks (unit tested)

def gate_json(output: str, now: datetime) -> dict:
    lines = [line for line in output.splitlines() if line.strip()]
    require(lines and lines[-1] == "GATE_OPEN", f"gate closed: {lines[-1] if lines else 'no output'}")
    return {"verdict": "GATE_OPEN", "observed_at": now.isoformat(), "summary": lines[-2] if len(lines) > 1 else ""}


def check_web(service: dict, *, release: str, digest: str, named_sha256: str) -> str:
    traffic = [t for t in service.get("status", {}).get("traffic", []) if t.get("percent")]
    require(len(traffic) == 1 and traffic[0]["percent"] == 100 and traffic[0].get("revisionName"),
            "web must serve one revision at 100%")
    spec = service["spec"]["template"]["spec"]
    require(service["status"].get("latestReadyRevisionName") == traffic[0]["revisionName"],
            "web's traffic revision is not its latest ready template")
    container, = spec["containers"]
    require(container["image"] in (f"{REPO}:{release}", f"{REPO}@{digest}"), "web does not run the release")
    env = {e["name"]: e.get("value") for e in container.get("env", [])}
    require(env.get(NAMED_ENV + "_PATH") == f"{NAMED_MOUNT}/{NAMED_FILE}" and env.get(NAMED_ENV + "_SHA256") == named_sha256,
            "web does not pin the batch's named-referral manifest")
    mounts = {m["name"]: m["mountPath"] for m in container.get("volumeMounts", [])}
    volumes = [v for v in spec.get("volumes", []) if mounts.get(v["name"]) == NAMED_MOUNT]
    require(len(volumes) == 1 and [i["path"] for i in volumes[0].get("secret", {}).get("items", [])] == [NAMED_FILE],
            "web lacks the named-referral secret mount")
    return traffic[0]["revisionName"]


def job_name(batch_id: str) -> str:
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{2,30}", batch_id), "batch id is not a job-name slug")
    return JOB_PREFIX + batch_id


def deploy_args(*, mode: str, batch_id: str, release: str, digest: str, gate: dict, approval: str | None,
                secrets: dict) -> list[str]:
    """`gcloud run jobs deploy` args. secrets: name -> (secret id, version, sha256) for manifest/plan/receipts/named."""
    require(mode in MODES, "mode must be plan, apply or rollback")
    require(re.fullmatch(r"[0-9a-f]{40}", release) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
            "release must be a commit and digest a sha256 digest")
    needed = {"plan": {"MANIFEST", "NAMED"}, "apply": {"MANIFEST", "PLAN", "NAMED"},
              "rollback": {"MANIFEST", "PLAN", "RECEIPTS", "NAMED"}}[mode]
    require(set(secrets) == needed, f"{mode} needs exactly the secrets {sorted(needed)}")
    require((approval is None) == (mode == "plan"), "apply/rollback need an approval; plan takes none")
    mounts = [f"{NAMED_MOUNT}/{NAMED_FILE}={secrets['NAMED'][0]}:{secrets['NAMED'][1]}"]
    env = {"PIVOTA_ENV": "production", "DB_AUTO_MIGRATE": "false", "DB_STATEMENT_TIMEOUT_SECONDS": "30",
           "DB_COMMAND_TIMEOUT_SECONDS": "120", "REVIEWED_SEED_BATCH_RELEASE_COMMIT": release,
           NAMED_ENV + "_PATH": f"{NAMED_MOUNT}/{NAMED_FILE}", NAMED_ENV + "_SHA256": secrets["NAMED"][2],
           "REVIEWED_SEED_BATCH_GATE": json.dumps(gate, separators=(",", ":"))}
    for name in sorted(needed - {"NAMED"}):
        secret, version, sha = secrets[name]
        require(re.fullmatch(r"[a-z0-9-]{3,80}", secret) and re.fullmatch(r"[1-9][0-9]*", str(version))
                and re.fullmatch(r"[0-9a-f]{64}", sha), f"{name.lower()} secret must be pinned to a numbered version")
        path = f"{BATCH_MOUNT}-{name.lower()}/{name.lower()}.json"
        mounts.append(f"{path}={secret}:{version}")
        env[f"REVIEWED_SEED_BATCH_{name}_PATH"] = path
        env[f"REVIEWED_SEED_BATCH_{name}_SHA256"] = sha
    if mode != "plan":
        env["REVIEWED_SEED_BATCH_WRITE_ENABLED"] = "1"
        env["REVIEWED_SEED_BATCH_APPROVAL"] = approval
    # ^@^ makes gcloud split on @ (JSON values contain commas).
    return ["gcloud", "run", "jobs", "deploy", job_name(batch_id), "--project", PROJECT, "--region", REGION,
            "--image", f"{REPO}@{digest}", "--service-account", SERVICE_ACCOUNT,
            "--network", "default", "--subnet", "pivota-crawl" if mode == "apply" else "default",
            "--vpc-egress", "all-traffic", "--tasks", "1", "--max-retries", "0", "--task-timeout", "1800s",
            "--command", "python", "--args", f"-m,scripts.ops.reviewed_seed_batch,{mode}",
            "--set-secrets", ",".join(["DATABASE_URL=DATABASE_URL:latest", *mounts]),
            "--set-env-vars", "^@^" + "@".join(f"{k}={v}" for k, v in env.items())]


def summarise(rows: list[dict], *, mode: str, execution: str | None) -> dict:
    """The terminal result.json. `ok` only for a clean batch line; a missing batch line is never ok."""
    batch_rows = [r for r in rows if r.get("kind") == "batch"]
    entries = [r for r in rows if r.get("kind") == "entry"]
    line = batch_rows[-1] if len(batch_rows) == 1 else None
    expected = "planned" if mode == "plan" else "committed"
    ok = bool(line) and line.get("status") == expected and all(
        e.get("status") == ("planned" if mode == "plan" else "committed") for e in entries)
    return {"mode": mode, "execution": execution, "ok": ok, "batch": line,
            "batch_lines": len(batch_rows), "entries": [{k: e.get(k) for k in (
                "seed_id", "status", "phase", "error", "receipt_sha256")} for e in entries],
            "decision": "pending_keep" if ok and mode == "apply" else None,
            "written_at": datetime.now(timezone.utc).isoformat()}


# ------------------------------------------------------------------ subcommands (gcloud)

def cmd_gate(args) -> None:
    done = subprocess.run([sys.executable, str(Path(args.scripts) / "ops" / "oneoff_gate.py")], capture_output=True,
                          text=True, timeout=600, check=False)
    print(done.stdout, end="")
    gate = gate_json(done.stdout, datetime.now(timezone.utc))
    require(done.returncode == 0, "gate exited non-zero")
    (WORK / "gate.json").write_text(json.dumps(gate))


def cmd_web(args) -> None:
    service = json.loads(run(["gcloud", "run", "services", "describe", "web", "--project", PROJECT, "--region", REGION,
                              "--format", "json"]))
    print("web revision:", check_web(service, release=args.release, digest=args.digest, named_sha256=args.named_sha256))


def cmd_deploy(args) -> None:
    secrets = {name: tuple(json.loads(value)) for name, value in (
        ("MANIFEST", args.manifest), ("PLAN", args.plan), ("RECEIPTS", args.receipts), ("NAMED", args.named)) if value}
    gate = json.loads((WORK / "gate.json").read_text())
    run(deploy_args(mode=args.mode, batch_id=args.batch_id, release=args.release, digest=args.digest, gate=gate,
                    approval=args.approval or None, secrets=secrets))
    (WORK / "job.txt").write_text(job_name(args.batch_id))


def cmd_execute(args) -> None:
    job = (WORK / "job.txt").read_text().strip()
    out = run(["gcloud", "run", "jobs", "execute", job, "--project", PROJECT, "--region", REGION, "--wait",
               "--format", "value(metadata.name)"], timeout=2400)
    execution = out.strip().splitlines()[-1] if out.strip() else ""
    (WORK / "execution.txt").write_text(execution)


def cmd_publish(args) -> None:
    execution = (WORK / "execution.txt").read_text().strip() if (WORK / "execution.txt").exists() else ""
    log = ""
    if execution:
        log = run(["gcloud", "logging", "read",
                   f'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="{execution}"',
                   "--project", PROJECT, "--order", "asc", "--limit", "5000", "--format", "value(textPayload)"],
                  timeout=300)
    sys.path[:0] = [str(Path(args.scripts).parent)]
    from scripts.ops.reviewed_seed_batch import parse_result_lines

    rows = parse_result_lines(log)
    result = summarise(rows, mode=args.mode, execution=execution or None)
    result.update(build=os.getenv("BUILD_ID"), batch_id=args.batch_id, release=args.release)
    prefix = f"gs://{args.bucket}/seed-batch/{args.batch_id}/{os.getenv('BUILD_ID', 'local')}"
    for name, content in (("log.txt", log), ("rows.json", json.dumps(rows, indent=1)), ("result.json", json.dumps(result, indent=1))):
        (WORK / name).write_text(content)
        run(["gcloud", "storage", "cp", "--no-clobber", str(WORK / name), f"{prefix}/{name}"])
    print(json.dumps(result, indent=1))
    require(result["ok"], "batch did not finish cleanly; read result.json")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("gate", "web", "deploy", "execute", "publish"))
    parser.add_argument("--scripts", default="/workspace/release/scripts")
    for name in ("mode", "batch-id", "release", "digest", "named-sha256", "manifest", "plan", "receipts", "named",
                 "approval", "bucket"):
        parser.add_argument("--" + name, default="")
    args = parser.parse_args(argv)
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        {"gate": cmd_gate, "web": cmd_web, "deploy": cmd_deploy, "execute": cmd_execute, "publish": cmd_publish}[
            args.command](args)
    except (LaunchRefused, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as error:
        print(f"LAUNCH_REFUSED {args.command}: {type(error).__name__}: {error}", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
