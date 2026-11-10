"""The catalog-jobs-on-GCP scripts: three job setups and the one re-image path.

  infra/gcp/setup_relgraph_health_job.sh               relgraph health + nightly readout (scheduled)
  infra/gcp/setup_brand_official_canonical_promote_job.sh   brand-official promote (scheduled)
  infra/gcp/setup_pdp_live_read_promote_job.sh         live-read promote (named job, NO trigger)
  infra/gcp/reimage_jobs.sh                            roll pinned jobs onto a merged sha

Each test drives the REAL script through its `GCLOUD` / `CURL` injection points against fakes that
record every invocation one argument per field, and keep job state so a re-image can be verified.
Nothing here reaches Google Cloud or GitHub.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]
GCP = REPO / "infra" / "gcp"
HEALTH = GCP / "setup_relgraph_health_job.sh"
PROMOTE = GCP / "setup_brand_official_canonical_promote_job.sh"
LIVE_READ = GCP / "setup_pdp_live_read_promote_job.sh"
REIMAGE = GCP / "reimage_jobs.sh"
SCRIPTS = [HEALTH, PROMOTE, LIVE_READ, REIMAGE]
TAG = "a" * 40
NEW = "b" * 40
AR = "us-west1-docker.pkg.dev/pivota-shared/pivota"

WORKER_ENV = {
    "AGENT_PDP_VIEW_MARKET_PRICES": "on",
    "ENABLE_KBEAUTY_AGENT_DECISION_GATES": "true",
    "ENABLE_STORELESS_BRAND_CATALOG": "1",
    "INDEX_ELIGIBLE_READ": "1",
    "INDEX_ELIGIBLE_RECALL": "1",
    "INDEX_ELIGIBLE_SITEMAP": "1",
    "INDEXNOW_ENABLED": "true",
    "PDP_QUALITY_SCORE_SOURCE_BACKED_OPTIONAL_COMPONENTS": "1",
    "PIVOTA_SERVING_PRICING_REGIONS": "US,SG",
    "SERVE_PDP_ENRICHMENT_ON_WRITE": "1",
    "SERVING_PRICE_REQUIRES_SEED_CURRENCY": "1",
    "STRICT_BEAUTY_CATEGORY_TEXT_RECALL": "true",
    "UNRELATED": "x",
}

def _pins(script: Path) -> Dict[str, str]:
    """The script's PINNED_FLAGS block, so the tests never restate a live config value."""
    block = re.search(r'^PINNED_FLAGS="(.*?)"$', script.read_text(), re.M | re.S).group(1)
    return dict(line.split("=", 1) for line in block.splitlines())


# The gateway as the live-read script expects it: every pinned value set, every `<unset>` absent.
GATEWAY_ENV = {**{k: v for k, v in _pins(REPO / "infra" / "gcp" / "setup_pdp_live_read_promote_job.sh").items()
                  if v != "<unset>"}, "UNRELATED": "x"}

# A Python fake: it records argv (\x1f-separated, one call per line), answers the reads, and keeps
# each job as a JSON file under $FAKE_STATE/jobs so `run jobs update` really changes what the next
# `describe` returns.
FAKE_GCLOUD = textwrap.dedent(
    r'''
    #!/usr/bin/env python3
    import json, os, sys
    a = sys.argv[1:]
    with open(os.environ["GCLOUD_LOG"], "a") as f:
        f.write("\x1f".join(a) + "\x1f\n")
    state = os.environ["FAKE_STATE"]
    def opt(name):
        for i, t in enumerate(a):
            if t == name: return a[i + 1]
            if t.startswith(name + "="): return t[len(name) + 1:]
        return None
    head = " ".join(a[:3])
    if head == "run jobs describe":
        p = os.path.join(state, "jobs", a[3] + ".json")
        if not os.path.exists(p): sys.exit(1)
        if opt("--format") == "json": print(open(p).read())
        sys.exit(0)
    if head == "run jobs update" and os.path.exists(os.path.join(state, "jobs", a[3] + ".json")) and opt("--update-env-vars"):
        p = os.path.join(state, "jobs", a[3] + ".json"); d = json.load(open(p))
        c = d["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
        c["image"] = opt("--image")
        k, v = opt("--update-env-vars").split("=", 1)
        c["env"] = [e for e in c.get("env", []) if e["name"] != k] + [{"name": k, "value": v}]
        if os.environ.get("FAKE_UPDATE_SIDE_EFFECT") == "1":
            c["env"].append({"name": "SURPRISE", "value": "1"})
        json.dump(d, open(p, "w")); sys.exit(0)
    if head == "run jobs executions":
        if os.environ.get("EXECUTIONS_READ_FAILS") == "1": sys.exit(1)
        sys.stdout.write(os.environ.get("FAKE_EXECUTIONS", "")); sys.exit(0)
    if head == "scheduler jobs describe":
        sys.exit(0 if os.environ.get("TRIGGER_EXISTS") == "1" else 1)
    if head == "artifacts docker images":
        if os.environ.get("IMAGE_EXISTS", "1") != "1": sys.exit(1)
        missing = os.environ.get("MISSING_REPOS", "").split(",")
        repo = a[4].split("/")[2] if len(a) > 4 and a[4].count("/") >= 3 else ""
        sys.exit(1 if repo and repo in missing else 0)
    if head == "run services describe":
        if os.environ.get("SERVICE_READ_FAILS") == "1": sys.exit(1)
        env = json.loads(os.environ.get("SERVICE_ENV", "{}"))
        print(json.dumps({"spec": {"template": {"spec": {"containers": [
            {"env": [{"name": k, "value": v} for k, v in env.items()]}]}}}}))
        sys.exit(0)
    sys.exit(0)
    ''').lstrip()

FAKE_CURL = textwrap.dedent(
    r'''
    #!/usr/bin/env bash
    printf 'ARGV %s\n' "$*" >> "$CURL_LOG"
    case " $* " in *" --config - "*) printf 'STDIN %s\n' "$(cat)" >> "$CURL_LOG" ;; esac
    [ "${CURL_FAILS:-0}" = 1 ] && exit 22
    # A real compare body carries every changed file: megabytes for an old image. PAD_KB makes the
    # fake that big, so a parse that cannot take a large body fails here and not in prod.
    pad=""; [ -n "${PAD_KB:-}" ] && pad=$(head -c $((PAD_KB * 1024)) /dev/zero | tr '\0' 'x')
    printf '{"status":"%s","ahead_by":%s,"behind_by":0,"pad":"%s"}' "${COMPARE_STATUS:-ahead}" "${AHEAD_BY:-5}" "$pad"
    ''').lstrip()


def job_json(image: str, sha: str, extra_env: Optional[Dict[str, str]] = None) -> dict:
    env = [{"name": "PIVOTA_COMMIT_SHA", "value": sha}, {"name": "PIVOTA_ENV", "value": "production"}]
    env += [{"name": k, "value": v} for k, v in (extra_env or {}).items()]
    return {"spec": {"template": {
        "metadata": {"annotations": {"run.googleapis.com/vpc-access-egress": "all-traffic"}},
        "spec": {"taskCount": 1, "template": {"spec": {"containers": [
            {"image": image, "env": env, "args": ["-m", "jobs.x"]}]}}}}}}


def run(tmp_path: Path, script: Path, *args: str, env: Optional[Dict[str, str]] = None,
        jobs: Optional[Dict[str, dict]] = None) -> Tuple[subprocess.CompletedProcess, List[List[str]], List[str]]:
    gcloud = tmp_path / "fake-gcloud"; gcloud.write_text(FAKE_GCLOUD); gcloud.chmod(0o755)
    curl = tmp_path / "fake-curl"; curl.write_text(FAKE_CURL); curl.chmod(0o755)
    log = tmp_path / "calls.log"; log.write_text("")
    curl_log = tmp_path / "curl.log"; curl_log.write_text("")
    state = tmp_path / "state"; (state / "jobs").mkdir(parents=True, exist_ok=True)
    for name, doc in (jobs or {}).items():
        (state / "jobs" / f"{name}.json").write_text(json.dumps(doc))
    proc = subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, cwd=str(REPO),
        env={**os.environ, "GCLOUD": str(gcloud), "CURL": str(curl), "GCLOUD_LOG": str(log),
             "CURL_LOG": str(curl_log), "FAKE_STATE": str(state), **(env or {})},
    )
    calls = [line.split("\x1f")[:-1] for line in log.read_text().splitlines() if line]
    return proc, calls, curl_log.read_text().splitlines()


def calls_with(calls: List[List[str]], *prefix: str) -> List[List[str]]:
    return [c for c in calls if c[: len(prefix)] == list(prefix)]


def one(calls: List[List[str]], *prefix: str) -> List[str]:
    found = calls_with(calls, *prefix)
    assert len(found) == 1, (prefix, calls)
    return found[0]


def flag(argv: List[str], name: str) -> str:
    for i, token in enumerate(argv):
        if token == name:
            return argv[i + 1]
        if token.startswith(name + "="):
            return token[len(name) + 1:]
    raise AssertionError(f"{name} not passed: {argv}")


def env_vars(argv: List[str]) -> Dict[str, str]:
    raw = flag(argv, "--set-env-vars")
    sep = raw[1] if raw.startswith("^") and raw[2:3] == "^" else ","
    body = raw[3:] if sep != "," else raw
    return dict(pair.split("=", 1) for pair in body.split(sep) if pair)


def writes(calls: List[List[str]]) -> List[List[str]]:
    verbs = {"create", "update", "pause", "resume", "execute", "delete"}
    return [c for c in calls if len(c) > 2 and c[2] in verbs or (len(c) > 3 and c[3] in verbs and c[0] == "scheduler")]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_script_parses_and_is_executable(script):
    subprocess.run(["bash", "-n", str(script)], check=True)
    assert os.access(script, os.X_OK)


# ------------------------------------------------------------------ relgraph-health

def test_health_is_dark_by_default_and_read_only_on_the_default_subnet(tmp_path):
    proc, calls, _ = run(tmp_path, HEALTH, "prod", TAG)
    assert proc.returncode == 0, proc.stderr
    job = one(calls, "run", "jobs", "create")
    assert job[3] == "relgraph-health"
    assert flag(job, "--image") == f"{AR}-gateway/gateway:{TAG}"
    assert flag(job, "--subnet") == "default"
    assert flag(job, "--set-secrets") == "DATABASE_URL=DATABASE_URL_NOVERIFY:latest"
    assert flag(job, "--command") == "npm" and flag(job, "--args") == "run,relgraph:health-job"
    e = env_vars(job)
    assert e["PIVOTA_COMMIT_SHA"] == TAG and e["PIVOTA_ENV"] == "production"
    # Owner's threshold (2026-11-10): <= 500 rows AND <= 1%; critical reasons stay at zero tolerance
    # through the job's built-in list, which is why RELGRAPH_CRITICAL_REASONS is asserted unset below.
    assert (e["RELGRAPH_MAX_SUPPRESSED_ROWS"], e["RELGRAPH_MAX_SUPPRESSED_PCT"]) == ("500", "1")
    assert (e["RELGRAPH_MAX_EXPIRING_14D_PCT"], e["RELGRAPH_MIN_TOTAL_ROWS"]) == ("30", "500")
    # The parser falls back to its built-in critical reasons only when the list is EMPTY; passing one
    # through comma-split gcloud env would risk a single bogus reason. It must stay unset.
    assert "RELGRAPH_CRITICAL_REASONS" not in e and "RELGRAPH_FAIL_ON_NOOP" not in e
    trig = one(calls, "scheduler", "jobs", "create", "http")
    assert flag(trig, "--schedule") == "5 17 * * *"
    assert flag(trig, "--uri").endswith("/namespaces/pivota-prod/jobs/relgraph-health:run")
    assert calls_with(calls, "scheduler", "jobs", "pause") and not calls_with(calls, "scheduler", "jobs", "resume")


def test_health_enable_resumes_the_trigger(tmp_path):
    proc, calls, _ = run(tmp_path, HEALTH, "prod", TAG, "--enable", env={"TRIGGER_EXISTS": "1"})
    assert proc.returncode == 0, proc.stderr
    assert calls_with(calls, "scheduler", "jobs", "update", "http")
    assert calls_with(calls, "scheduler", "jobs", "resume") and not calls_with(calls, "scheduler", "jobs", "pause")


def test_health_runs_after_relgraph_sync_and_outside_its_window():
    m = re.search(r'^SCHEDULE="(\d+) (\d+) \* \* \*"', HEALTH.read_text(), re.M)
    minute, hour = int(m.group(1)), int(m.group(2))
    hhmm = hour * 100 + minute
    # relgraph-sync starts 10:37 with a 21600s task timeout: it may still be writing until 16:37.
    assert hhmm >= 1637 and not (1015 <= hhmm < 1145)


@pytest.mark.parametrize("args", [("prod", "latest"), ("prod", TAG, "--arm"), ("prod", TAG, "--enable", "x"),
                                  ("dev", TAG), ("prod",)])
def test_health_refuses_bad_invocations_before_touching_anything(tmp_path, args):
    proc, calls, _ = run(tmp_path, HEALTH, *args)
    assert proc.returncode == 2
    assert not writes(calls)


def test_health_refuses_a_missing_image(tmp_path):
    proc, calls, _ = run(tmp_path, HEALTH, "prod", TAG, env={"IMAGE_EXISTS": "0"})
    assert proc.returncode == 1 and not writes(calls)


# ------------------------------------------------------------------ brand-official promote

def promote(tmp_path, *flags, worker=None, extra_env=None):
    env = {"SERVICE_ENV": json.dumps(WORKER_ENV if worker is None else worker), **(extra_env or {})}
    return run(tmp_path, PROMOTE, "prod", TAG, *flags, env=env)


def test_promote_is_dark_and_dry_by_default(tmp_path):
    proc, calls, _ = promote(tmp_path)
    assert proc.returncode == 0, proc.stderr
    job = one(calls, "run", "jobs", "create")
    assert job[3] == "brand-official-canonical-promote"
    assert flag(job, "--args") == "scripts/promote_brand_official_canonicals.py"
    assert flag(job, "--subnet") == "default" and flag(job, "--max-retries") == "0"
    assert flag(job, "--set-secrets") == "DATABASE_URL=DATABASE_URL:latest"
    assert calls_with(calls, "scheduler", "jobs", "pause") and not calls_with(calls, "scheduler", "jobs", "resume")


def test_promote_enable_alone_arms_a_dry_run(tmp_path):
    proc, calls, _ = promote(tmp_path, "--enable")
    assert proc.returncode == 0, proc.stderr
    assert "--apply" not in flag(one(calls, "run", "jobs", "create"), "--args")
    assert calls_with(calls, "scheduler", "jobs", "resume")


def test_promote_writes_only_with_enable_and_apply(tmp_path):
    proc, calls, _ = promote(tmp_path, "--enable", "--apply")
    assert proc.returncode == 0, proc.stderr
    assert flag(one(calls, "run", "jobs", "create"), "--args") == "scripts/promote_brand_official_canonicals.py,--apply"
    assert calls_with(calls, "scheduler", "jobs", "resume")


def test_promote_refuses_apply_without_enable(tmp_path):
    proc, calls, _ = promote(tmp_path, "--apply")
    assert proc.returncode == 2 and not writes(calls)


def test_promote_pins_every_catalog_flag_the_worker_carries(tmp_path):
    proc, calls, _ = promote(tmp_path)
    e = env_vars(one(calls, "run", "jobs", "create"))
    for name, value in WORKER_ENV.items():
        if name != "UNRELATED":
            assert e[name] == value, name
    # The decision gates are the flag whose absence promoted 410 ungated rows (2026-09-08).
    assert e["ENABLE_KBEAUTY_AGENT_DECISION_GATES"] == "true"
    assert e["PIVOTA_SERVING_PRICING_REGIONS"] == "US,SG"
    assert e["PIVOTA_COMMIT_SHA"] == TAG and "UNRELATED" not in e


@pytest.mark.parametrize("drift", [
    {"ENABLE_KBEAUTY_AGENT_DECISION_GATES": "false"},
    {"PIVOTA_SERVING_PRICING_REGIONS": "US"},
    {"AGENT_PDP_VIEW_MARKET_PRICES": None},
])
def test_promote_refuses_when_the_worker_disagrees(tmp_path, drift):
    worker = dict(WORKER_ENV)
    for k, v in drift.items():
        if v is None:
            worker.pop(k)
        else:
            worker[k] = v
    proc, calls, _ = promote(tmp_path, worker=worker)
    assert proc.returncode == 1, proc.stdout
    assert "REFUSING" in proc.stderr
    assert not calls_with(calls, "run", "jobs", "create") and not calls_with(calls, "scheduler", "jobs", "create")


def test_promote_refuses_when_the_worker_cannot_be_read(tmp_path):
    proc, calls, _ = promote(tmp_path, extra_env={"SERVICE_READ_FAILS": "1"})
    assert proc.returncode == 1 and not writes(calls)


def test_promote_schedule_is_clear_of_the_relgraph_window():
    m = re.search(r'^SCHEDULE="(\d+) (\d+) \* \* \*"', PROMOTE.read_text(), re.M)
    hhmm = int(m.group(2)) * 100 + int(m.group(1))
    assert not (1015 <= hhmm < 1145)


# ------------------------------------------------------------------ live-read promote

def live_read(tmp_path, *args, gateway=None):
    return run(tmp_path, LIVE_READ, "prod", TAG, *args,
               env={"SERVICE_ENV": json.dumps(GATEWAY_ENV if gateway is None else gateway)})


def test_live_read_is_a_named_dry_run_job_with_no_trigger(tmp_path):
    proc, calls, _ = live_read(tmp_path)
    assert proc.returncode == 0, proc.stderr
    job = one(calls, "run", "jobs", "create")
    assert job[3] == "pdp-live-read-promote"
    assert flag(job, "--image") == f"{AR}-gateway/gateway:{TAG}"
    assert flag(job, "--command") == "node"
    args = flag(job, "--args").split(",")
    assert args[0] == "scripts/promote-pdp-identity-live-read.js" and "--write" not in args
    assert not [c for c in calls if c[:1] == ["scheduler"]], "this job must never get a trigger"


def test_live_read_pins_the_gateway_flags_and_leaves_unset_ones_unset(tmp_path):
    proc, calls, _ = live_read(tmp_path)
    e = env_vars(one(calls, "run", "jobs", "create"))
    assert e["PDP_IDENTITY_GRAPH_ENABLED"] == "true"
    for name, value in GATEWAY_ENV.items():
        if name != "UNRELATED":
            assert e[name] == value, name
    for name in ("CATALOG_TRUST_CRAWL_FRESH_ENABLED", "SHADE_LINE_CARD_COLLAPSE_ENABLED",
                 "PDP_IDENTITY_GRAPH_AUTO_ENABLE_LIVE"):
        assert name not in e, name
    assert not any(v == "<unset>" for v in e.values())


@pytest.mark.parametrize("drift", [{"CATALOG_TRUST_CRAWL_FRESH_ENABLED": "true"},
                                   {"PDP_IDENTITY_GRAPH_AUTO_ENABLE_LIVE": "true"},
                                   {"PDP_IDENTITY_GRAPH_ENABLED": "false"}])
def test_live_read_refuses_when_the_gateway_disagrees(tmp_path, drift):
    proc, calls, _ = live_read(tmp_path, gateway={**GATEWAY_ENV, **drift})
    assert proc.returncode == 1 and "REFUSING" in proc.stderr
    assert not calls_with(calls, "run", "jobs", "create")


def test_live_read_has_no_enable(tmp_path):
    proc, calls, _ = live_read(tmp_path, "--enable")
    assert proc.returncode == 2 and not writes(calls)


# ------------------------------------------------------------------ reimage_jobs.sh

def backend_job(sha=TAG, **extra):
    return job_json(f"{AR}/backend:{sha}", sha, extra)


def gateway_job(sha=TAG):
    return job_json(f"{AR}/gateway:{sha}", sha)


def updates(calls):
    return calls_with(calls, "run", "jobs", "update")


def test_reimage_is_a_dry_run_by_default(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW,
                         jobs={"external-referral-refresh": backend_job()})
    assert proc.returncode == 0, proc.stderr
    assert "DRY RUN" in proc.stdout and "external-referral-refresh" in proc.stdout
    assert not updates(calls)


def test_reimage_apply_changes_only_image_and_commit_stamp(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "external-referral-refresh",
                         jobs={"external-referral-refresh": backend_job(EXTRA="kept")})
    assert proc.returncode == 0, proc.stderr
    up = one(calls, "run", "jobs", "update")
    assert up[3] == "external-referral-refresh"
    assert flag(up, "--image") == f"{AR}/backend:{NEW}"
    assert flag(up, "--update-env-vars") == f"PIVOTA_COMMIT_SHA={NEW}"
    forbidden = {"--set-env-vars", "--args", "--command", "--set-secrets", "--subnet", "--clear-env-vars",
                 "--remove-env-vars", "--service-account", "--task-timeout", "--max-retries"}
    assert not [t for t in up if t.split("=")[0] in forbidden], up
    assert "verified" in proc.stdout


def test_reimage_fails_verification_when_anything_else_changed(tmp_path):
    proc, _, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "external-referral-refresh",
                     jobs={"external-referral-refresh": backend_job()}, env={"FAKE_UPDATE_SIDE_EFFECT": "1"})
    assert proc.returncode == 1 and "FAILED VERIFY" in proc.stderr


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_reimage_refuses_a_sha_that_is_not_on_main(tmp_path, status):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "external-referral-refresh",
                         jobs={"external-referral-refresh": backend_job()}, env={"COMPARE_STATUS": status})
    assert proc.returncode == 1 and not updates(calls)


def test_reimage_refuses_when_github_cannot_be_reached(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "external-referral-refresh",
                         jobs={"external-referral-refresh": backend_job()}, env={"CURL_FAILS": "1"})
    assert proc.returncode == 1 and not updates(calls)


def test_reimage_checks_main_of_the_right_repository(tmp_path):
    _, _, curl = run(tmp_path, REIMAGE, "prod", "gateway", NEW, jobs={"relgraph-sync": gateway_job()},
                     env={"NOW_HHMM": "1300"})
    assert any(f"/repos/pengxu9-rgb/PIVOTA-Agent/compare/{NEW}...main" in c for c in curl), curl


def test_reimage_refuses_a_missing_image(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "external-referral-refresh",
                         jobs={"external-referral-refresh": backend_job()}, env={"IMAGE_EXISTS": "0"})
    assert proc.returncode == 1 and not updates(calls)


@pytest.mark.parametrize("hhmm,refused", [("1014", False), ("1015", True), ("1037", True), ("1144", True), ("1145", False)])
def test_reimage_refuses_relgraph_in_its_window(tmp_path, hhmm, refused):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "gateway", NEW, "--apply", "relgraph-sync",
                         jobs={"relgraph-sync": gateway_job()}, env={"NOW_HHMM": hhmm})
    assert (proc.returncode == 1) is refused, proc.stderr
    assert bool(updates(calls)) is (not refused)


def test_reimage_never_plans_the_drain_by_default(tmp_path):
    jobs = {"external-referral-refresh": backend_job(), "retailer-ingest-drain": backend_job()}
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, jobs=jobs)
    assert proc.returncode == 0, proc.stderr
    assert "plan  external-referral-refresh" in proc.stdout
    assert "plan  retailer-ingest-drain" not in proc.stdout and "skip  retailer-ingest-drain" in proc.stdout
    assert not updates(calls)


def test_reimage_apply_needs_job_names(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply",
                         jobs={"external-referral-refresh": backend_job()})
    assert proc.returncode == 2 and "needs job names" in proc.stderr and not updates(calls)


def test_reimage_refuses_the_drain_without_its_writer(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "retailer-ingest-drain",
                         jobs={"retailer-ingest-drain": backend_job()})
    assert proc.returncode == 1 and not updates(calls)


def test_reimage_rolls_the_drain_for_its_writer(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "retailer-ingest-drain",
                         jobs={"retailer-ingest-drain": backend_job()}, env={"RETAILER_INGEST_DRAIN_WRITER": "1"})
    assert proc.returncode == 0, proc.stderr
    assert [c[3] for c in updates(calls)] == ["retailer-ingest-drain"]


def test_reimage_refuses_a_job_with_a_running_execution(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "external-referral-refresh",
                         jobs={"external-referral-refresh": backend_job()},
                         env={"FAKE_EXECUTIONS": "external-referral-refresh-abc\t\n"})
    assert proc.returncode == 1 and "still running" in proc.stderr and not updates(calls)


def test_reimage_one_refusal_changes_nothing_at_all(tmp_path):
    jobs = {"external-referral-refresh": backend_job(), "content-canonical-election": backend_job()}
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply",
                         "external-referral-refresh", "retailer-ingest-drain", jobs=jobs)
    assert proc.returncode == 1 and not updates(calls)


@pytest.mark.parametrize("args", [("prod", "backend", "abc123"), ("prod", "backend", "latest"),
                                  ("prod", "backend", NEW, "relgraph-sync"), ("prod", "frontend", NEW),
                                  ("prod", "backend", NEW, "--force")])
def test_reimage_refuses_bad_invocations(tmp_path, args):
    proc, calls, _ = run(tmp_path, REIMAGE, *args, jobs={"external-referral-refresh": backend_job()})
    assert proc.returncode == 2 and not updates(calls)


def test_reimage_skips_a_job_already_on_the_sha(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "external-referral-refresh",
                         jobs={"external-referral-refresh": backend_job(NEW)})
    assert proc.returncode == 0 and "nothing to do" in proc.stdout and not updates(calls)


def test_status_lists_existing_jobs_with_commits_behind(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "status", env={"AHEAD_BY": "228"},
                         jobs={"merchant-purchasability-sweep": backend_job(), "relgraph-sync": gateway_job()})
    assert proc.returncode == 0, proc.stderr
    rows = {line.split()[0]: line.split() for line in proc.stdout.splitlines()[1:]}
    assert set(rows) == {"merchant-purchasability-sweep", "relgraph-sync"}
    assert rows["merchant-purchasability-sweep"][1:] == ["pivota-backend", TAG[:12], "228"]
    assert rows["relgraph-sync"][1] == "PIVOTA-Agent"
    assert not updates(calls)


def test_every_new_setup_job_is_in_the_reimage_registry():
    text = REIMAGE.read_text()
    for name in ("relgraph-health", "brand-official-canonical-promote", "pdp-live-read-promote"):
        assert re.search(rf"\b{name}\b", text), name


def test_status_calls_github_once_per_distinct_sha(tmp_path):
    jobs = {"content-canonical-election": backend_job(), "agent-pdp-orphan-reaper": backend_job(),
            "audit-domainless-offer-currency": backend_job(), "relgraph-sync": gateway_job()}
    proc, _, curl = run(tmp_path, REIMAGE, "prod", "status", jobs=jobs)
    assert proc.returncode == 0, proc.stderr
    calls = [c for c in curl if c.startswith("ARGV")]
    assert len(calls) == 2, curl  # one backend sha, one gateway sha
    behind = [line.split()[-1] for line in proc.stdout.splitlines()[1:]]
    assert behind == ["5"] * 4


def test_github_token_is_sent_only_when_set(tmp_path):
    jobs = {"content-canonical-election": backend_job()}
    _, _, without = run(tmp_path, REIMAGE, "prod", "status", jobs=jobs, env={"GITHUB_TOKEN": ""})
    assert not any("Authorization" in c for c in without)
    _, _, with_token = run(tmp_path, REIMAGE, "prod", "status", jobs=jobs, env={"GITHUB_TOKEN": "t0k"})
    assert any(c.startswith("STDIN") and "Authorization: Bearer t0k" in c for c in with_token), with_token
    # never on argv, where `ps` shows it to every user of the machine
    assert not any(c.startswith("ARGV") and "t0k" in c for c in with_token), with_token


def test_status_reports_unknown_when_github_fails(tmp_path):
    proc, _, _ = run(tmp_path, REIMAGE, "prod", "status", jobs={"content-canonical-election": backend_job()},
                     env={"CURL_FAILS": "1"})
    assert proc.returncode == 0 and proc.stdout.splitlines()[1].split()[-1] == "?"


def test_a_megabyte_compare_body_still_parses(tmp_path):
    proc, _, _ = run(tmp_path, REIMAGE, "prod", "status", jobs={"content-canonical-election": backend_job()},
                     env={"PAD_KB": "3000", "AHEAD_BY": "759"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[1].split()[-1] == "759"
    proc, _, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, jobs={"content-canonical-election": backend_job()},
                     env={"PAD_KB": "3000"})
    assert proc.returncode == 0 and "DRY RUN" in proc.stdout, proc.stderr


# ------------------------------------------------------------------ review of be#2779

def test_reimage_refuses_when_executions_cannot_be_read(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "external-referral-refresh",
                         jobs={"external-referral-refresh": backend_job()}, env={"EXECUTIONS_READ_FAILS": "1"})
    assert proc.returncode == 1 and "cannot list its executions" in proc.stderr and not updates(calls)


def test_reimage_names_what_was_rolled_when_a_later_verify_fails(tmp_path):
    # Both jobs are rolled in order; the side effect makes EVERY update fail verify, so the first
    # fails and nothing is reported as rolled.
    jobs = {"external-referral-refresh": backend_job(), "content-canonical-election": backend_job()}
    proc, _, _ = run(tmp_path, REIMAGE, "prod", "backend", NEW, "--apply", "external-referral-refresh",
                     "content-canonical-election", jobs=jobs, env={"FAKE_UPDATE_SIDE_EFFECT": "1"})
    assert proc.returncode == 1 and "already rolled and verified before it: none" in proc.stderr


@pytest.mark.parametrize("missing,expected", [("", "pivota-gateway"), ("pivota-gateway", "pivota")])
def test_gateway_images_prefer_the_dedicated_repository(tmp_path, missing, expected):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "gateway", NEW, "--apply", "relgraph-sync",
                         jobs={"relgraph-sync": gateway_job()}, env={"NOW_HHMM": "1300", "MISSING_REPOS": missing})
    assert proc.returncode == 0, proc.stderr
    assert flag(one(calls, "run", "jobs", "update"), "--image") == f"us-west1-docker.pkg.dev/pivota-shared/{expected}/gateway:{NEW}"


def test_gateway_image_missing_from_both_repositories_is_refused(tmp_path):
    proc, calls, _ = run(tmp_path, REIMAGE, "prod", "gateway", NEW, "--apply", "relgraph-sync",
                         jobs={"relgraph-sync": gateway_job()},
                         env={"NOW_HHMM": "1300", "MISSING_REPOS": "pivota-gateway,pivota"})
    assert proc.returncode == 1 and not updates(calls)


@pytest.mark.parametrize("script,args", [(HEALTH, ()), (PROMOTE, ())])
def test_a_plain_rerun_pauses_an_existing_trigger_even_when_the_preflight_refuses(tmp_path, script, args):
    proc, calls, _ = run(tmp_path, script, "prod", TAG, *args,
                         env={"TRIGGER_EXISTS": "1", "IMAGE_EXISTS": "0", "SERVICE_ENV": json.dumps(WORKER_ENV)})
    assert proc.returncode == 1  # the preflight refused (no image) ...
    assert calls_with(calls, "scheduler", "jobs", "pause"), calls  # ... but the disarm already happened
    assert not calls_with(calls, "scheduler", "jobs", "resume")


def test_an_enable_rerun_does_not_pause_first(tmp_path):
    proc, calls, _ = run(tmp_path, HEALTH, "prod", TAG, "--enable", env={"TRIGGER_EXISTS": "1"})
    assert proc.returncode == 0 and not calls_with(calls, "scheduler", "jobs", "pause")


def test_promote_pins_the_flags_its_write_path_reads_including_unset_ones(tmp_path):
    pins = _pins(PROMOTE)
    assert pins["SERVING_PRICE_REQUIRES_SEED_CURRENCY"] == "1"
    for name in ("CATEGORY_LEAF_REQUIRED_FOR_SERVING", "CATALOG_TRUST_CRAWL_FRESH_ENABLED",
                 "CATALOG_TRUST_CRAWL_FRESH_DAYS", "CATALOG_TRUST_CRAWL_FRESH_RECHECK_LIMIT",
                 "AGENT_PDP_VIEW_UNBUILDABLE_REAP"):
        assert pins[name] == "<unset>", name
    proc, calls, _ = promote(tmp_path)
    e = env_vars(one(calls, "run", "jobs", "create"))
    assert e["SERVING_PRICE_REQUIRES_SEED_CURRENCY"] == "1"
    assert "CATEGORY_LEAF_REQUIRED_FOR_SERVING" not in e and not any(v == "<unset>" for v in e.values())


@pytest.mark.parametrize("drift", [{"CATEGORY_LEAF_REQUIRED_FOR_SERVING": "1"},
                                   {"CATALOG_TRUST_CRAWL_FRESH_ENABLED": "true"},
                                   {"SERVING_PRICE_REQUIRES_SEED_CURRENCY": None}])
def test_promote_refuses_drift_in_a_write_path_flag(tmp_path, drift):
    worker = dict(WORKER_ENV)
    for k, v in drift.items():
        if v is None:
            worker.pop(k, None)
        else:
            worker[k] = v
    proc, calls, _ = promote(tmp_path, worker=worker)
    assert proc.returncode == 1 and "REFUSING" in proc.stderr
    assert not calls_with(calls, "run", "jobs", "create")


# ------------------------------------------------------------------ gateway image build identity (P2-1)

IDENTITY = GCP / "setup_gateway_image_build_identity.sh"

FAKE_IDENTITY_GCLOUD = textwrap.dedent(
    r'''
    #!/usr/bin/env python3
    import json, os, sys
    a = sys.argv[1:]
    with open(os.environ["GCLOUD_LOG"], "a") as f:
        f.write("\x1f".join(a) + "\x1f\n")
    head = " ".join(a[:3])
    if head == "projects describe pivota-shared": print("371394967380"); sys.exit(0)
    if a[:3] == ["artifacts", "repositories", "describe"]:
        if any(t.startswith("--format") for t in a):
            print(os.environ.get("REPO_IMMUTABLE", "True")); sys.exit(0)
        sys.exit(0 if os.environ.get("REPO_EXISTS") == "1" else 1)
    if a[:3] == ["artifacts", "repositories", "get-iam-policy"]:
        print(json.dumps({"bindings": [
            {"role": "roles/artifactregistry.reader", "members": ["serviceAccount:runtime@p.iam", "serviceAccount:robot@p.iam"]},
            {"role": "roles/artifactregistry.writer", "members": ["serviceAccount:hand-build@p.iam"]}]}))
        sys.exit(0)
    if a[:3] == ["iam", "workload-identity-pools", "providers"] and a[3] == "describe":
        sys.exit(1)
    sys.exit(0)
    ''').lstrip()


def run_identity(tmp_path, env=None):
    gcloud = tmp_path / "fake-gcloud"; gcloud.write_text(FAKE_IDENTITY_GCLOUD); gcloud.chmod(0o755)
    log = tmp_path / "calls.log"; log.write_text("")
    proc = subprocess.run(["bash", str(IDENTITY)], capture_output=True, text=True, cwd=str(REPO),
                          env={**os.environ, "GCLOUD": str(gcloud), "GCLOUD_LOG": str(log), **(env or {})})
    return proc, [line.split("\x1f")[:-1] for line in log.read_text().splitlines() if line]


def test_identity_writes_only_to_the_dedicated_immutable_repository(tmp_path):
    proc, calls = run_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    create = one(calls, "artifacts", "repositories", "create")
    assert create[3] == "pivota-gateway" and "--immutable-tags" in create
    writer = [c for c in calls if "--role=roles/artifactregistry.writer" in c]
    assert len(writer) == 1 and writer[0][:4] == ["artifacts", "repositories", "add-iam-policy-binding", "pivota-gateway"]
    # never on `pivota`, where backend:latest runs in prod as sa-worker
    assert not [c for c in calls if "add-iam-policy-binding" in c and "pivota" in c[3:4]]
    # no project-level grant at all
    assert not [c for c in calls if c[:2] == ["projects", "add-iam-policy-binding"]]


def test_identity_copies_only_the_readers_of_pivota(tmp_path):
    proc, calls = run_identity(tmp_path)
    readers = [flag(c, "--member") for c in calls if "--role=roles/artifactregistry.reader" in c]
    assert sorted(readers) == ["serviceAccount:robot@p.iam", "serviceAccount:runtime@p.iam"]
    assert all(c[3] == "pivota-gateway" for c in calls if "--role=roles/artifactregistry.reader" in c)


def test_identity_refuses_an_existing_repository_with_mutable_tags(tmp_path):
    proc, calls = run_identity(tmp_path, env={"REPO_EXISTS": "1", "REPO_IMMUTABLE": "False"})
    assert proc.returncode == 1 and "immutable" in proc.stderr
    assert not [c for c in calls if "--role=roles/artifactregistry.writer" in c]


def test_identity_provider_is_pinned_to_the_repo_branch_workflow_and_events(tmp_path):
    proc, calls = run_identity(tmp_path)
    prov = one(calls, "iam", "workload-identity-pools", "providers", "create-oidc")
    cond = flag(prov, "--attribute-condition")
    assert "assertion.repository_id == '1100586824'" in cond
    assert "assertion.ref == 'refs/heads/main'" in cond
    assert ("assertion.workflow_ref == 'pengxu9-rgb/PIVOTA-Agent/.github/workflows/build-gateway-image.yml"
            "@refs/heads/main'") in cond
    assert "assertion.event_name in ['push', 'workflow_dispatch']" in cond
