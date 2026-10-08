"""`infra/gcp/setup_merchant_purchasability_sweep_job.sh` provisions the sweep where it must run.

The sweep renders merchant checkouts, so it must leave from the crawl subnet and never from the
payment-partner-allowlisted default NAT; it must be dark unless `--enable` was typed on THIS run;
and its `--args` must be in the equals form, because the value starts with a dash — the Tier B
script's first prod run failed on the two-word form (#2367).

These tests drive the REAL script through its `GCLOUD` injection point against a fake that
records every invocation ONE ARGUMENT PER FIELD, so an assertion about `--args=-m,...` is about
the argv gcloud would receive and not about a space-joined string that cannot tell the two forms
apart. Nothing here reaches Google Cloud.
"""
from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "gcp" / "setup_merchant_purchasability_sweep_job.sh"
TIERB_SCRIPT = REPO / "infra" / "gcp" / "setup_tierb_cart_link_eligibility_job.sh"
SCHEDULER_SCRIPT = REPO / "infra" / "gcp" / "setup_scheduler.sh"
TAG = "a" * 40

# Fields separated by \x1f, one invocation per line. The reads the script makes are answered from
# env: JOB_EXISTS / TRIGGER_EXISTS pick create-vs-update, SUBNET_EXISTS=0 makes the preflight fail.
FAKE_GCLOUD = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf '%s\\x1f' "$@" >> "$GCLOUD_LOG"; printf '\\n' >> "$GCLOUD_LOG"
    case "$1 $2 $3" in
      "run jobs describe") [ "${JOB_EXISTS:-0}" = 1 ] && exit 0 || exit 1 ;;
      "scheduler jobs describe") [ "${TRIGGER_EXISTS:-0}" = 1 ] && exit 0 || exit 1 ;;
      "compute networks subnets") [ "${SUBNET_EXISTS:-1}" = 1 ] && exit 0 || exit 1 ;;
    esac
    exit 0
    """
)


def _run(tmp_path: Path, *args: str, env: Optional[Dict[str, str]] = None
         ) -> Tuple[subprocess.CompletedProcess, List[List[str]]]:
    fake = tmp_path / "fake-gcloud"
    fake.write_text(FAKE_GCLOUD)
    fake.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(REPO),
        env={**os.environ, "GCLOUD": str(fake), "GCLOUD_LOG": str(log), **(env or {})},
    )
    calls = [line.split("\x1f")[:-1] for line in log.read_text().splitlines() if line]
    return proc, calls


def _one(calls: List[List[str]], *prefix: str) -> List[str]:
    found = [c for c in calls if c[: len(prefix)] == list(prefix)]
    assert len(found) == 1, (prefix, calls)
    return found[0]


def _flag(argv: List[str], name: str) -> str:
    """The value of `--name VALUE` or `--name=VALUE` in one recorded argv."""
    for i, token in enumerate(argv):
        if token == name:
            return argv[i + 1]
        if token.startswith(name + "="):
            return token[len(name) + 1:]
    raise AssertionError(f"{name} not passed: {argv}")


def _env_vars(argv: List[str]) -> Dict[str, str]:
    return dict(pair.split("=", 1) for pair in _flag(argv, "--set-env-vars").split(","))


def test_the_script_parses():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert os.access(SCRIPT, os.X_OK), "the operator runs it directly, like the Tier B script"


def test_dark_by_default_the_job_is_created_on_the_crawl_subnet_gated_off_and_paused(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG)
    assert proc.returncode == 0, proc.stderr
    job = _one(calls, "run", "jobs", "create", "merchant-purchasability-sweep")

    assert _flag(job, "--subnet") == "pivota-crawl", "never the default subnet: that is the payment NAT"
    assert _flag(job, "--network") == "default"
    assert _flag(job, "--vpc-egress") == "all-traffic", "private-ranges-only would leave via the default NAT"
    assert _flag(job, "--max-retries") == "0"
    assert _flag(job, "--image") == f"us-west1-docker.pkg.dev/pivota-shared/pivota/backend:{TAG}"
    assert _flag(job, "--command") == "python"
    assert _flag(job, "--set-secrets") == "DATABASE_URL=DATABASE_URL:latest"

    env = _env_vars(job)
    assert env["MERCHANT_PURCHASABILITY_SWEEP_ENABLED"] == "false"
    assert env["PIVOTA_ENV"] == "production" and env["PIVOTA_COMMIT_SHA"] == TAG
    assert env["DB_STATEMENT_TIMEOUT_SECONDS"] and env["DB_COMMAND_TIMEOUT_SECONDS"]
    assert "VANTAGE_PROXY_URL" not in env, "a second vantage doubles every check; the timeout is for one"

    trigger = _one(calls, "scheduler", "jobs", "create", "http", "merchant-purchasability-sweep-cron")
    assert _flag(trigger, "--schedule") == "7 0-1,5-23 * * *"
    assert _flag(trigger, "--time-zone") == "Etc/UTC"
    assert _flag(trigger, "--uri").endswith(
        "/namespaces/pivota-prod/jobs/merchant-purchasability-sweep:run")

    states = [c[2] for c in calls if c[:2] == ["scheduler", "jobs"] and c[2] in ("pause", "resume")]
    assert states == ["pause"], states
    assert "DARK" in proc.stdout


def test_enable_arms_both_switches_together(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG, "--enable")
    assert proc.returncode == 0, proc.stderr
    job = _one(calls, "run", "jobs", "create", "merchant-purchasability-sweep")
    assert _env_vars(job)["MERCHANT_PURCHASABILITY_SWEEP_ENABLED"] == "true"
    states = [c[2] for c in calls if c[:2] == ["scheduler", "jobs"] and c[2] in ("pause", "resume")]
    assert states == ["resume"], states
    assert "ARMED" in proc.stdout


def test_a_rerun_without_enable_updates_in_place_and_disarms(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG, env={"JOB_EXISTS": "1", "TRIGGER_EXISTS": "1"})
    assert proc.returncode == 0, proc.stderr
    job = _one(calls, "run", "jobs", "update", "merchant-purchasability-sweep")
    assert _env_vars(job)["MERCHANT_PURCHASABILITY_SWEEP_ENABLED"] == "false"
    _one(calls, "scheduler", "jobs", "update", "http", "merchant-purchasability-sweep-cron")
    assert not [c for c in calls if c[2:3] == ["create"]]
    assert [c[2] for c in calls if c[2:3] in (["pause"], ["resume"])] == ["pause"]


def test_args_is_one_equals_form_token_with_the_dash_leading_value(tmp_path):
    """#2367. gcloud reads `--args "-m,..."` as `--args` followed by a SECOND FLAG and fails with
    "argument --args: expected one argument". The equals form is one token."""
    _proc, calls = _run(tmp_path, "prod", TAG)
    job = _one(calls, "run", "jobs", "create", "merchant-purchasability-sweep")
    assert "--args" not in job, "the two-word form: gcloud would parse the value as a flag"
    tokens = [t for t in job if t.startswith("--args=")]
    assert tokens == ["--args=-m,jobs.merchant_purchasability_sweep"], job


def test_the_job_module_the_args_name_is_a_real_cli():
    """The args name a module; that module must have the entry point the job runs."""
    import jobs.merchant_purchasability_sweep as sweep

    source = Path(sweep.__file__).read_text()
    assert 'if __name__ == "__main__":\n    raise SystemExit(main())' in source
    assert callable(sweep.main)


def test_the_task_timeout_covers_the_budget_plus_one_merchant(tmp_path):
    """The budget stops the job STARTING a merchant; one in flight finishes. So Cloud Run's task
    timeout must exceed the budget by at least one merchant's realistic worst case (300 s, what
    the old scheduler deadline allowed: 600 + 300), or Cloud Run — not the budget — ends runs.
    And the budget must be PINNED on the job, or a code default change would silently break the
    relation."""
    _proc, calls = _run(tmp_path, "prod", TAG)
    job = _one(calls, "run", "jobs", "create", "merchant-purchasability-sweep")
    timeout = int(re.fullmatch(r"(\d+)s", _flag(job, "--task-timeout")).group(1))
    env = _env_vars(job)
    budget = int(env["MERCHANT_PURCHASABILITY_BUDGET_SECONDS"])
    assert int(env["MERCHANT_PURCHASABILITY_BATCH"]) >= 1
    assert timeout >= budget + 300, (timeout, budget)
    # ...and below the trigger's spacing, so two executions of this job can never overlap.
    assert timeout < 3600


def _cron_starts(expr: str) -> List[int]:
    """Minutes-of-day a `M H * * *` cron fires at. Only the shapes these scripts use: one
    minute, and an hour field of numbers, ranges and lists."""
    minute, hour, *rest = expr.split()
    assert rest == ["*", "*", "*"] and minute.isdigit(), expr
    hours: List[int] = []
    for part in hour.split(","):
        lo, _, hi = part.partition("-")
        hours.extend(range(int(lo), int(hi or lo) + 1))
    return [h * 60 + int(minute) for h in hours]


def _windows(expr: str, timeout_s: int, max_retries: int = 0) -> List[Tuple[int, int]]:
    """start .. the latest a run can still be going: every attempt may use its whole timeout,
    and Cloud Run starts a retry as soon as an attempt fails."""
    span_min = -(-timeout_s * (max_retries + 1) // 60)
    return [(start, start + span_min) for start in _cron_starts(expr)]


def _last_int(pattern: str, text: str) -> int:
    """The LAST occurrence wins, as it does on a gcloud line where an override follows a default."""
    found = re.findall(pattern, text)
    assert found, pattern
    return int(found[-1])


def _overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def test_the_schedule_never_overlaps_the_daily_crawls_on_the_same_address(tmp_path):
    """Read the neighbours from THEIR scripts, so moving one of them re-runs this check: the Tier B
    job (same merchants, same address) and the external-seed destination sweep. Each window is
    start .. start + task timeout x (max-retries + 1), the longest a run can last — the
    external-seed sweep inherits mkcrawljob's `--max-retries 1`, which the first version of this
    schedule missed (its retry can run to 04:20)."""
    _proc, calls = _run(tmp_path, "prod", TAG)
    job = _one(calls, "run", "jobs", "create", "merchant-purchasability-sweep")
    trigger = _one(calls, "scheduler", "jobs", "create", "http", "merchant-purchasability-sweep-cron")
    ours = _windows(_flag(trigger, "--schedule"),
                    int(_flag(job, "--task-timeout").rstrip("s")))
    assert len(ours) >= 20, "hourly, less the skipped hours"

    tierb = TIERB_SCRIPT.read_text()
    tierb_cron = re.search(r'^SCHEDULE="([^"]+)"', tierb, re.M).group(1)
    tierb_timeout = _last_int(r"--task-timeout (\d+)s", tierb)
    tierb_retries = _last_int(r"--max-retries (\d+)", tierb)

    sched = SCHEDULER_SCRIPT.read_text()
    seed_cron = re.search(
        r'sched external-seed-destination-sweep-cron "([^"]+)"', sched).group(1)
    # mkcrawljob's defaults, then the job's own overrides (passed after them, so they win).
    helper = sched[sched.index("mkcrawljob(){"):]
    helper = helper[: helper.index("\n}\n")]
    seed_block = sched[sched.index("mkcrawljob external-seed-destination-sweep"):]
    seed_block = seed_block[: seed_block.index("\nelse")]
    seed_timeout = _last_int(r"--task-timeout (\d+)s", helper + seed_block)
    seed_retries = _last_int(r"--max-retries (\d+)", helper + seed_block)
    assert seed_retries >= 1, "precondition: this is the retry the first schedule missed"

    for name, theirs in (
        ("tierb-cart-link-eligibility", _windows(tierb_cron, tierb_timeout, tierb_retries)),
        ("external-seed-destination-sweep", _windows(seed_cron, seed_timeout, seed_retries)),
    ):
        clashes = [(a, b) for a in ours for b in theirs if _overlaps(a, b)]
        assert clashes == [], f"overlaps {name}: {clashes}"

    # The every-5 / every-10 minute crawl lanes cannot be avoided, only not coincided with.
    assert all(start % 5 for start, _ in ours), "a start on a multiple of 5 lands on the probe bursts"


@pytest.mark.parametrize("args, message", [
    ((), "usage"),
    (("qa", TAG), "usage"),
    (("prod",), "backend-tag is required"),
    (("prod", TAG, "--enabled"), "unknown argument"),
    (("prod", TAG, "--enable", "extra"), "too many arguments"),
])
def test_bad_arguments_exit_2_before_touching_anything(tmp_path, args, message):
    proc, calls = _run(tmp_path, *args)
    assert proc.returncode == 2 and message in proc.stderr, (proc.returncode, proc.stderr)
    assert calls == []


def test_a_missing_crawl_subnet_stops_before_the_job_is_created(tmp_path):
    """Without the subnet, --subnet would fail late — or, worse, an operator would 'fix' it by
    dropping the flag, which puts the sweep back on the payment NAT."""
    proc, calls = _run(tmp_path, "prod", TAG, env={"SUBNET_EXISTS": "0"})
    assert proc.returncode == 1 and "setup_crawl_egress.sh" in proc.stderr
    assert not [c for c in calls if c[:2] == ["run", "jobs"] and c[2] in ("create", "update")]
    assert not [c for c in calls if c[:2] == ["scheduler", "jobs"] and c[2] != "describe"]


def test_staging_targets_the_staging_project(tmp_path):
    proc, calls = _run(tmp_path, "staging", TAG)
    assert proc.returncode == 0, proc.stderr
    job = _one(calls, "run", "jobs", "create", "merchant-purchasability-sweep")
    assert _flag(job, "--service-account") == "sa-worker@pivota-staging.iam.gserviceaccount.com"
    assert _env_vars(job)["PIVOTA_ENV"] == "staging"


def test_the_job_shares_the_shopify_edge_budget_like_the_reap_proof_jobs(tmp_path):
    """#2474: the sweep's every request already goes through `PacedTransport(shopify_edge=True)`, which is
    a no-op unless the flag is on in the process. Set exactly as setup_reap_cart_proof_jobs.sh sets it,
    and the shared RATE is never set here (every job on the address must use one rate). The IP breaker's
    dials are not pinned: the module's defaults are the documented ones."""
    proc, calls = _run(tmp_path, "prod", TAG)
    assert proc.returncode == 0, proc.stderr
    env = _env_vars(_one(calls, "run", "jobs", "create", "merchant-purchasability-sweep"))
    assert env["CRAWL_SHOPIFY_EDGE_PACER_ENABLED"] == "true" and env["CRAWL_SHOPIFY_EDGE_LEASE"] == "2"
    assert "CRAWL_SHOPIFY_EDGE_RPS" not in env
    assert not [k for k in env if k.startswith("MERCHANT_PURCHASABILITY_IP_THROTTLE_")]
