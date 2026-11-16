"""Launch and collect a pinned read-only official-source review Cloud Run job."""

import argparse
from base64 import b64decode, b64encode
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
import zlib

from . import model_worker as worker


REGION = "us-west1"
PROJECT_NUMBER = "184498324954"
IMAGE = ("us-west1-docker.pkg.dev/pivota-shared/pivota/backend@"
         "sha256:f969a6d733cd31767a1f93ef7709130f3f1af158264f3d17f869a51ff56d23d6")
RUN_TIMEOUT = "600"
LOG_NAME = f"projects/{worker.PROJECT}/logs/run.googleapis.com%2Fstdout"
INPUT_NAME = "review-input.json"
DIGEST_NAME = "review-input.sha256"
EXECUTION_NAME = "review-execution.json"
SUMMARY_NAME = "review-summary.json"
INTENT_NAME = ".review-launch-intent.json"


class ReviewPending(RuntimeError):
    """The exact review execution or its completion receipt is still pending."""


class UnsupportedReviewWorker(RuntimeError):
    """The preserved execution used a different source review worker."""


def _gcloud(*args, regional=True, missing_job=None):
    flags = [f"--project={worker.PROJECT}", "--format=json"]
    if regional:
        flags.append(f"--region={REGION}")
    run = subprocess.run(["gcloud", *args, *flags], capture_output=True, text=True, timeout=90, check=False)
    if run.returncode:
        detail = run.stderr.casefold()
        if (missing_job and missing_job in detail and
                re.search(r"not found|cannot find (?:the )?job|does not exist|could not find", detail)):
            return None
        raise RuntimeError(f"gcloud {' '.join(args[:3])} failed with exit code {run.returncode}")
    return json.loads(run.stdout)


def _packed(data):
    return b64encode(zlib.compress(data, level=9)).decode("ascii")


def _render_program(source, payload):
    return (
        "import base64,zlib;"
        f"SOURCE={source!r};"
        f"INPUT={payload!r};"
        "scope={'__name__':'review_model_worker'};"
        "exec(compile(zlib.decompress(base64.b64decode(SOURCE)),'review_model_worker.py','exec'),scope);"
        "scope['run_cloud'](zlib.decompress(base64.b64decode(INPUT)))"
    )


def _program(raw):
    program = _render_program(_packed(Path(worker.__file__).read_bytes()), _packed(raw))
    worker.require(len(program) < 30_000, "review input exceeds the inline job limit; split the input")
    return program


def _job_name(program):
    return worker.JOB_PREFIX + sha256(program.encode()).hexdigest()[:12]


def _check_names(job, execution):
    worker.require(worker._identifier(job, worker.JOB_PREFIX + r"[a-f0-9]{12}")
                   and worker._identifier(execution, re.escape(job) + r"-[a-z0-9]{1,20}"),
                   "invalid review job or execution")


def _check_template(spec, task):
    worker.require(spec.get("taskCount") == 1 and spec.get("parallelism") == 1
                   and task.get("serviceAccountName") == worker.ACCOUNT
                   and str(task.get("timeoutSeconds")) == RUN_TIMEOUT and task.get("maxRetries", 0) == 0
                   and not task.get("volumes"), "review execution settings differ from the staging pins")
    containers = task.get("containers")
    worker.require(isinstance(containers, list) and len(containers) == 1, "expected one review container")
    container = containers[0]
    worker.require(container.get("image") == IMAGE and container.get("command") == ["python"]
                   and not container.get("env") and not container.get("envFrom") and not container.get("volumeMounts"),
                   "review container differs from the read-only image or configuration")
    args = container.get("args")
    worker.require(isinstance(args, list) and len(args) == 2 and args[0] == "-c" and isinstance(args[1], str),
                   "unexpected review container command")
    return args[1]


def _check_metadata(metadata, name):
    worker.require(metadata.get("name") == name and str(metadata.get("namespace")) == PROJECT_NUMBER
                   and metadata.get("labels", {}).get("cloud.googleapis.com/location") == REGION,
                   "unexpected Cloud Run resource identity")


def _unpack(data, limit):
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(b64decode(data, validate=True), limit + 1)
    worker.require(decompressor.eof and not decompressor.unused_data and len(raw) <= limit,
                   "review executable contains an invalid bundle")
    return raw


def _embedded_input(program, input_sha256):
    match = re.match(r"import base64,zlib;SOURCE='([A-Za-z0-9+/=]+)';INPUT='([A-Za-z0-9+/=]+)';", program)
    if match is None:
        raise UnsupportedReviewWorker("unrecognized review executable")
    worker.require(len(program) < 30_000, "review executable exceeds its limit")
    source, payload = match.groups()
    expected_worker = Path(worker.__file__).read_bytes()
    raw = _unpack(payload, worker.MAX_INPUT_BYTES)
    worker.require(sha256(raw).hexdigest() == input_sha256, "review executable input differs from the pinned digest")
    if _unpack(source, 100_000) != expected_worker:
        raise UnsupportedReviewWorker("review executable used a different worker revision")
    worker.require(_render_program(source, payload) == program, "unexpected commands in the review executable")
    return raw


def _check_execution(detail, job, execution):
    worker.require(detail.get("kind") == "Execution", "expected a Cloud Run execution")
    metadata = detail["metadata"]
    _check_metadata(metadata, execution)
    worker.require(metadata["labels"].get("run.googleapis.com/job") == job, "execution belongs to another job")
    task = detail["spec"]["template"]["spec"]
    program = _check_template(detail["spec"], task)
    worker.require(_job_name(program) == job, "execution differs from its content-addressed job")
    return program


def _check_completed(detail, job, execution, input_sha256):
    program = _check_execution(detail, job, execution)
    status = detail.get("status", {})
    condition = [entry["status"] for entry in status.get("conditions", []) if entry.get("type") == "Completed"]
    if condition in ([], ["Unknown"]):
        raise ReviewPending(f"review execution {execution} is still running")
    worker.require(condition == ["True"] and status.get("succeededCount") == 1
                   and not status.get("failedCount") and not status.get("cancelledCount"),
                   "review Cloud Run execution did not succeed")
    return _embedded_input(program, input_sha256)


def _check_receipt(payload, raw, input_sha256):
    packets = worker.review_packets(raw)
    worker.require(
        (payload.get("scope"), payload.get("market"), payload.get("review_policy"), payload.get("review_input_sha256"))
        == (worker.SCOPE, "US", worker.REVIEW_POLICY, input_sha256)
        and type(payload.get("candidate_count")) is int and payload["candidate_count"] == len(packets)
        and all(type(payload.get(key)) is int and payload[key] == 0 for key in worker.ZERO_WRITES),
        "review receipt does not attest the exact read-only input and policy",
    )
    expected = {row["candidate_id"]: (row["source_url"], [v["variant_id"] for v in row["variants"]])
                for row in packets}
    observed = {}
    reviews = payload.get("reviews")
    worker.require(isinstance(reviews, list) and len(reviews) == len(expected), "review receipt has incomplete results")
    for row in reviews:
        item = row["candidate_id"]
        worker.require(item not in observed, "review receipt repeats a candidate")
        observed[item] = (row.get("url"), row.get("model_sample_variant_ids"))
        models = row.get("models")
        worker.require(isinstance(models, list) and len(models) <= len(worker.STAGES), "invalid model receipt count")
        for model in models:
            stage = model.get("stage")
            worker.require(stage in worker.STAGES and model.get("model") == worker.STAGES[stage][0]
                           and model.get("decision") in worker.DECISIONS
                           and worker._clean_text(model.get("response_id"), 256), "wrong review model or response")
        worker.require(row.get("disposition") == worker.disposition(models), "review disposition does not match decisions")
    worker.require(observed == expected, "review candidate, URL or variant sample differs from the exact input")


def read_completed_review(job: str, execution: str, input_sha256: str) -> dict | None:
    """Revalidate the exact successful Cloud execution without trusting a local receipt."""
    _check_names(job, execution)
    worker.require(worker._identifier(input_sha256, r"[a-f0-9]{64}"), "invalid review input digest")
    detail = _gcloud("run", "jobs", "executions", "describe", execution)
    try:
        raw = _check_completed(detail, job, execution, input_sha256)
    except (ReviewPending, UnsupportedReviewWorker):
        return None
    log_filter = (
        f'resource.type="cloud_run_job" AND resource.labels.project_id="{worker.PROJECT}" '
        f'AND resource.labels.location="{REGION}" AND resource.labels.job_name="{job}" '
        f'AND labels."run.googleapis.com/execution_name"="{execution}" AND logName="{LOG_NAME}" '
        f'AND jsonPayload.scope="{worker.SCOPE}"'
    )
    logs = _gcloud("logging", "read", log_filter, "--freshness=30d", "--limit=2", regional=False)
    if logs == []:
        return None
    worker.require(isinstance(logs, list) and len(logs) == 1, "expected a single authoritative review log")
    record = logs[0]
    worker.require(record.get("logName") == LOG_NAME and record.get("resource") == {
        "type": "cloud_run_job", "labels": {"project_id": worker.PROJECT, "location": REGION, "job_name": job},
    } and all(record.get("labels", {}).get(f"run.googleapis.com/{key}") == value for key, value in (
        ("execution_name", execution), ("task_index", "0"), ("task_attempt", "0"),
    )), "review log belongs to another execution or task")
    payload = record["jsonPayload"]
    _check_receipt(payload, raw, input_sha256)
    return payload


def _archive_once(path, content):
    try:
        with path.open("xb") as file:
            file.write(content)
    except FileExistsError:
        worker.require(path.read_bytes() == content, f"existing {path.name} is different")


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _read_input(run_dir):
    raw = (run_dir / INPUT_NAME).read_bytes()
    return raw, sha256(raw).hexdigest(), len(worker.review_packets(raw))


def _read_launch(run_dir, input_sha256):
    worker.require((run_dir / DIGEST_NAME).read_text().strip() == input_sha256, "review input was modified after launch")
    launch = json.loads((run_dir / EXECUTION_NAME).read_bytes())
    worker.require(launch.get("review_input_sha256") == input_sha256, "launch belongs to another review input")
    _check_names(launch["job"], launch["execution"])
    return launch


def _existing_execution(job, digest):
    records = _gcloud("run", "jobs", "executions", "list", f"--job={job}", "--limit=2")
    worker.require(isinstance(records, list) and len(records) < 2, "review job has multiple executions")
    if not records:
        return None
    execution = records[0]["metadata"]["name"]
    _check_names(job, execution)
    detail = _gcloud("run", "jobs", "executions", "describe", execution)
    _embedded_input(_check_execution(detail, job, execution), digest)
    return execution


def _write_intent(path, job, digest, state):
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(_json_bytes({"job": job, "review_input_sha256": digest, "state": state}))
    temp.replace(path)


def launch(run_dir):
    raw, digest, count = _read_input(run_dir)
    if not count:
        return {"candidate_count": 0, "review_input_sha256": digest}
    program = _program(raw)
    job = _job_name(program)
    if (run_dir / EXECUTION_NAME).exists():
        archive = _read_launch(run_dir, digest)
        worker.require(archive["job"] == job, "review worker changed after launch")
        return archive

    lock = run_dir / ".review-launch.lock"
    try:
        with lock.open("xb"):
            pass
    except FileExistsError:
        raise RuntimeError("review launch is already in progress") from None
    try:
        if (run_dir / EXECUTION_NAME).exists():
            return _read_launch(run_dir, digest)
        _archive_once(run_dir / DIGEST_NAME, (digest + "\n").encode())
        intent_path = run_dir / INTENT_NAME
        intent = json.loads(intent_path.read_bytes()) if intent_path.exists() else None
        if intent:
            worker.require(intent.get("job") == job and intent.get("review_input_sha256") == digest
                           and intent.get("state") in {"ready_to_submit", "submission_pending"},
                           "launch intent belongs to another immutable review")
        resource = _gcloud("run", "jobs", "describe", job, missing_job=job)
        if resource is None:
            worker.require(intent is None, "review job disappeared after launch intent")
            _gcloud("run", "jobs", "create", job, f"--image={IMAGE}",
                    f"--service-account={worker.ACCOUNT}", "--tasks=1", "--parallelism=1",
                    f"--task-timeout={RUN_TIMEOUT}s", "--max-retries=0", "--command=python",
                    f"--args=^|^-c|{program}", "--quiet")
            _write_intent(intent_path, job, digest, "ready_to_submit")
            intent = {"state": "ready_to_submit"}
            resource = _gcloud("run", "jobs", "describe", job)
        worker.require(resource.get("kind") == "Job", "expected a Cloud Run review job")
        _check_metadata(resource["metadata"], job)
        spec = resource["spec"]["template"]["spec"]
        worker.require(_check_template(spec, spec["template"]["spec"]) == program,
                       "existing review job does not match the immutable input")
        execution = _existing_execution(job, digest)
        if execution is None:
            if not intent or intent["state"] != "ready_to_submit":
                raise ReviewPending(f"review job {job} has no visible execution; submission remains unresolved")
            _write_intent(intent_path, job, digest, "submission_pending")
            try:
                result = _gcloud("run", "jobs", "execute", job, "--async", "--quiet")
                execution = result["metadata"]["name"]
            except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                raise ReviewPending(f"review job {job} submission is unresolved; rerun launch to reconcile") from None
        _check_names(job, execution)
        archive = {"job": job, "execution": execution, "review_input_sha256": digest}
        _archive_once(run_dir / EXECUTION_NAME, _json_bytes(archive))
        return archive
    finally:
        lock.unlink()


def collect(run_dir):
    _, digest, count = _read_input(run_dir)
    if not count:
        return {"candidate_count": 0, "review_input_sha256": digest}
    archive = _read_launch(run_dir, digest)
    summary = read_completed_review(archive["job"], archive["execution"], digest)
    if summary is None:
        raise ReviewPending(f"verified review receipt for {archive['execution']} is unavailable")
    _archive_once(run_dir / SUMMARY_NAME, _json_bytes(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("launch", "collect"))
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    try:
        result = (launch if args.mode == "launch" else collect)(args.run_dir)
    except ReviewPending as error:
        print(error, file=sys.stderr)
        return 3
    except (RuntimeError, ValueError, KeyError, TypeError, OSError, zlib.error, subprocess.TimeoutExpired) as error:
        message = "gcloud timed out" if isinstance(error, subprocess.TimeoutExpired) else str(error)
        print(message, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
