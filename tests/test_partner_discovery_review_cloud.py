from copy import deepcopy
from hashlib import sha256
import json
from subprocess import TimeoutExpired
from types import SimpleNamespace

import pytest

from scripts.ops.partner_discovery import model_worker as worker
from scripts.ops.partner_discovery import review_cloud as cloud
from scripts.ops.partner_discovery import review_input


# A placeholder project number stands in for the pinned one; the pin is its sha256.
NAMESPACE = "000000000000"


@pytest.fixture(autouse=True)
def placeholder_project_number(monkeypatch):
    monkeypatch.setattr(cloud, "PROJECT_NUMBER_SHA256", sha256(NAMESPACE.encode()).hexdigest())


def planned_input():
    url = "https://example.com/products/fixture-lip"
    evidence = {
        "status": "shopify_us_quote_observed", "currency": "USD",
        "currency_proof": "product_cart_currency_cookie", "identity_status": "verified",
        "source_url": f"{url}.js?country=US", "product_id": "1000", "title": "Fixture Lip", "vendor": "Fixture",
        "variants": [{"variant_id": str(2000 + index), "gtin": "00094800364455", "sku": f"SKU-{index}",
                      "title": f"Shade {index}", "price_minor": 2500, "in_stock": True} for index in range(5)],
    }
    rows, reused, held = review_input.plan([{
        "batch_id": "fixture-review-batch", "scope": "meitu_official_rediscovery_query", "market": "US",
        "meitu_line": 40, "meitu_id": "123", "brand": "Fixture", "name": "Fixture Lip", "host": "example.com",
        "candidates": [{"status": "original_title_candidate", "url": url, "source_kind": "shopify_ajax",
                        "evidence": evidence}],
    }])
    assert reused == held == 0
    return review_input.canonical(rows) + b"\n"


def verdicts(token, stage, rows):
    assert token == "fixture-token"
    return {
        row["candidate_id"]: {"stage": stage, "model": worker.STAGES[stage][0], "response_id": f"fixture-{stage}",
                              "decision": "hold" if stage == "secondary" and len(row["variants"]) == 1 else
                              "catalog_review_candidate"}
        for row in rows
    }


def resources(raw, program=None):
    program = cloud._program(raw) if program is None else program
    job = cloud._job_name(program)
    execution = job + "-abc01"
    metadata = {"name": job, "namespace": NAMESPACE,
                "labels": {"cloud.googleapis.com/location": cloud.REGION}}
    run_metadata = {**metadata, "name": execution,
                    "labels": {**metadata["labels"], "run.googleapis.com/job": job}}
    task = {"serviceAccountName": worker.ACCOUNT, "timeoutSeconds": "600", "maxRetries": 0,
            "containers": [{"image": cloud.IMAGE, "command": ["python"], "args": ["-c", program]}]}
    spec = {"parallelism": 1, "taskCount": 1, "template": {"spec": task}}
    job_resource = {"kind": "Job", "metadata": metadata, "spec": {"template": {"spec": spec}}}
    run_resource = {"kind": "Execution", "metadata": run_metadata, "spec": spec,
                    "status": {"succeededCount": 1, "conditions": [{"type": "Completed", "status": "True"}]}}
    log = {"logName": cloud.LOG_NAME,
           "resource": {"type": "cloud_run_job", "labels": {
               "job_name": job, "project_id": worker.PROJECT, "location": cloud.REGION}},
           "labels": {"run.googleapis.com/execution_name": execution,
                      "run.googleapis.com/task_attempt": "0", "run.googleapis.com/task_index": "0"},
           "jsonPayload": worker.review(raw, "fixture-token", verdicts)}
    return job, execution, job_resource, run_resource, log


def test_worker_uses_every_planned_variant_and_tie_breaks_only_the_dispute():
    raw = planned_input()
    result = worker.review(raw, "fixture-token", verdicts)
    by_size = {len(row["model_sample_variant_ids"]): row for row in result["reviews"]}
    assert by_size[4]["model_sample_variant_ids"] == ["2000", "2001", "2002", "2003"]
    assert by_size[1]["model_sample_variant_ids"] == ["2004"]
    assert [v["stage"] for v in by_size[4]["models"]] == ["primary", "secondary"]
    assert [v["stage"] for v in by_size[1]["models"]] == ["primary", "secondary", "tie_break"]
    assert len(review_input.reuse_from_completion(raw, result)) == 2

    def failed_arbiter(token, stage, rows):
        return {} if stage == "tie_break" else verdicts(token, stage, rows)

    held = worker.review(raw, "fixture-token", failed_arbiter)
    assert len(review_input.reuse_from_completion(raw, held)) == 1
    assert worker.INCOMPLETE in {row["disposition"] for row in held["reviews"]}


def test_noop_plan_needs_no_google_calls(tmp_path, monkeypatch):
    (tmp_path / cloud.INPUT_NAME).write_text("[]\n")
    monkeypatch.setattr(cloud, "_gcloud", lambda *args, **kwargs: pytest.fail("unexpected gcloud"))
    assert cloud.launch(tmp_path)["candidate_count"] == cloud.collect(tmp_path)["candidate_count"] == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == [cloud.INPUT_NAME]


def test_remote_authority_rejects_wrong_model_sample_or_cloud_execution(monkeypatch):
    raw = planned_input()
    job, execution, _, detail, log = resources(raw)
    digest = sha256(raw).hexdigest()
    logs = [log]

    def gcloud(*args, **kwargs):
        if args[:4] == ("run", "jobs", "executions", "describe"):
            assert args[4] == execution
            return detail
        assert args[:2] == ("logging", "read") and all(value in args[2] for value in
                                                        (cloud.REGION, worker.PROJECT, job, execution))
        return logs

    monkeypatch.setattr(cloud, "_gcloud", gcloud)
    assert cloud.read_completed_review(job, execution, digest) == log["jsonPayload"]
    for mutate in (
        lambda item: item["jsonPayload"]["reviews"][0]["model_sample_variant_ids"].append("9999"),
        lambda item: item["jsonPayload"]["reviews"][0]["models"][0].update(model="other"),
        lambda item: item["jsonPayload"]["reviews"][0].update(models=[None]),
        lambda item: item["jsonPayload"].update(offer_db_writes=1),
        lambda item: item["labels"].update({"run.googleapis.com/execution_name": execution + "-other"}),
    ):
        logs[:] = [deepcopy(log)]
        mutate(logs[0])
        with pytest.raises(ValueError):
            cloud.read_completed_review(job, execution, digest)
    logs.clear()
    assert cloud.read_completed_review(job, execution, digest) is None
    old_program = cloud._render_program(cloud._packed(b"older review source"), cloud._packed(raw))
    job, execution, _, detail, _ = resources(raw, old_program)
    assert cloud.read_completed_review(job, execution, digest) is None


def test_deleted_execution_is_unusable_and_permission_failures_remain_failures(monkeypatch):
    raw = planned_input()
    job, execution, *_ = resources(raw)
    digest = sha256(raw).hexdigest()
    missing = SimpleNamespace(returncode=1, stderr=f"NOT_FOUND: execution {execution} does not exist")
    monkeypatch.setattr(cloud.subprocess, "run", lambda *args, **kwargs: missing)
    assert cloud.read_completed_review(job, execution, digest) is None
    missing.stderr += " or your account does not have permission"
    with pytest.raises(RuntimeError, match="gcloud run jobs executions"):
        cloud.read_completed_review(job, execution, digest)


def test_ambiguous_submit_reconciles_before_retry_and_reuses_from_another_dir(tmp_path, monkeypatch):
    raw = planned_input()
    first, second = (tmp_path / name for name in ("first", "second"))
    for path in (first, second):
        path.mkdir()
        (path / cloud.INPUT_NAME).write_bytes(raw)
    job, execution, resource, detail, log = resources(raw)
    calls = []
    state = {"created": False, "visible": False}

    def gcloud(*args, **kwargs):
        calls.append(args)
        action = args[:3]
        if action == ("run", "jobs", "describe"):
            return resource if state["created"] else None
        if action == ("run", "jobs", "create"):
            state["created"] = True
            owner = next(value.split("=")[-1] for value in args if value.startswith("--labels="))
            resource["metadata"]["labels"][cloud.OWNER_LABEL] = owner
            raise TimeoutExpired("gcloud", timeout=90)
        if action == ("run", "jobs", "execute"):
            raise TimeoutExpired("gcloud", timeout=90)
        if args[:4] == ("run", "jobs", "executions", "list"):
            return [detail] if state["visible"] else []
        if args[:4] == ("run", "jobs", "executions", "describe"):
            return detail
        assert args[:2] == ("logging", "read")
        return [log]

    monkeypatch.setattr(cloud, "_gcloud", gcloud)
    with pytest.raises(cloud.ReviewPending, match="creation"):
        cloud.launch(first)
    assert json.loads((first / cloud.INTENT_NAME).read_bytes())["state"] == "creating_job"
    for path in (second, first, first, second):
        with pytest.raises(cloud.ReviewPending):
            cloud.launch(path)
    assert json.loads((first / cloud.INTENT_NAME).read_bytes())["state"] == "submission_pending"
    assert sum(args[:3] == ("run", "jobs", "execute") for args in calls) == 1
    state["visible"] = True
    assert cloud.launch(first) == cloud.launch(second) == {
        "job": job, "execution": execution, "review_input_sha256": sha256(raw).hexdigest(),
    }
    assert cloud.collect(first) == log["jsonPayload"]
    assert json.loads((first / cloud.SUMMARY_NAME).read_bytes()) == log["jsonPayload"]
    assert sum(args[:3] == ("run", "jobs", "execute") for args in calls) == 1
    (first / cloud.INPUT_NAME).write_bytes(b"[]\n")
    for command in (cloud.launch, cloud.collect):
        with pytest.raises(ValueError, match="modified"):
            command(first)


def test_explicit_retry_requires_terminal_failure_or_all_models_incomplete(tmp_path, monkeypatch):
    raw = planned_input()
    digest = sha256(raw).hexdigest()
    prior = tmp_path / "prior"
    retry = tmp_path / "retry"
    prior.mkdir()
    retry.mkdir()
    job, execution, _, detail, log = resources(raw)
    retry_job, retry_execution, retry_resource, retry_detail, retry_log = resources(raw, cloud._program(raw, execution))
    (prior / cloud.DIGEST_NAME).write_text(digest + "\n")
    (prior / cloud.EXECUTION_NAME).write_text(json.dumps({
        "job": job, "execution": execution, "review_input_sha256": digest,
    }))
    (retry / cloud.INPUT_NAME).write_bytes(raw)
    original_status = detail["status"]
    original_payload = log["jsonPayload"]
    calls = []
    created = False

    def gcloud(*args, **kwargs):
        nonlocal created
        calls.append(args)
        if args[:4] == ("run", "jobs", "executions", "describe"):
            return detail if args[4] == execution else retry_detail
        if args[:2] == ("logging", "read"):
            return [retry_log] if retry_execution in args[2] else [log]
        if args[:3] == ("run", "jobs", "describe"):
            assert args[3] == retry_job
            return retry_resource if created else None
        if args[:3] == ("run", "jobs", "create"):
            created = True
            owner = next(value.split("=")[-1] for value in args if value.startswith("--labels="))
            retry_resource["metadata"]["labels"][cloud.OWNER_LABEL] = owner
            return None
        if args[:4] == ("run", "jobs", "executions", "list"):
            return []
        assert args[:3] == ("run", "jobs", "execute") and args[3] == retry_job
        return {"metadata": {"name": retry_execution}}

    monkeypatch.setattr(cloud, "_gcloud", gcloud)
    detail["status"] = {"runningCount": 1, "conditions": [{"type": "Completed", "status": "Unknown"}]}
    with pytest.raises(cloud.ReviewPending):
        cloud.launch(retry, retry_from=prior)
    detail["status"] = original_status
    for payload in (original_payload, worker.review(raw, "fixture-token", lambda token, stage, rows:
                    {} if stage == "tie_break" else verdicts(token, stage, rows))):
        log["jsonPayload"] = payload
        with pytest.raises(ValueError, match="every model decision"):
            cloud.launch(retry, retry_from=prior)
    assert cloud._job_name(cloud._program(raw)) != retry_job
    assert not any(args[:3] in (("run", "jobs", "create"), ("run", "jobs", "execute")) for args in calls)

    detail["status"] = {"failedCount": 1, "conditions": [{"type": "Completed", "status": "False"}]}
    assert cloud._retry_predecessor(retry, prior, digest) == execution
    detail["status"] = original_status
    log["jsonPayload"] = worker.review(raw, "fixture-token", lambda *args: {})
    assert cloud.launch(retry, retry_from=prior)["execution"] == retry_execution
    assert cloud.collect(retry)["review_input_sha256"] == digest
    assert sum(args[:3] == ("run", "jobs", "execute") for args in calls) == 1
