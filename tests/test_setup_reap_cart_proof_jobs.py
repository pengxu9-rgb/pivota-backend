"""`infra/gcp/setup_reap_cart_proof_jobs.sh` provisions the two cart-proof jobs where they must run.

Both writers fetch merchant storefronts, so both jobs must leave from the crawl subnet and never from
the payment-partner-allowlisted default NAT; both must be dark (dry run, trigger paused) unless
`--enable` was typed on THIS run; their `--args` must be in the equals form (#2367); and their
schedules and timeouts must keep the proofs fresh without landing on the crawl address's daily
neighbours or on each other.

The REAL script is driven through its `GCLOUD` injection point against a fake that records every
invocation ONE ARGUMENT PER FIELD. Nothing here reaches Google Cloud.
"""
from __future__ import annotations

import os
import re
import subprocess
import textwrap
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "gcp" / "setup_reap_cart_proof_jobs.sh"
TIERB_SCRIPT = REPO / "infra" / "gcp" / "setup_tierb_cart_link_eligibility_job.sh"
SCHEDULER_SCRIPT = REPO / "infra" / "gcp" / "setup_scheduler.sh"
TAG = "b" * 40
JOBS = ("reap-cart-proof-enrichment", "reap-cart-proof-mirror")

FAKE_GCLOUD = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf '%s\\x1f' "$@" >> "$GCLOUD_LOG"; printf '\\n' >> "$GCLOUD_LOG"
    case "$1 $2 $3" in
      "run jobs describe") [ "${JOB_EXISTS:-0}" = 1 ] && exit 0 || exit 1 ;;
      "scheduler jobs describe") [ "${TRIGGER_EXISTS:-0}" = 1 ] && exit 0 || exit 1 ;;
      "compute networks subnets") [ "${SUBNET_EXISTS:-1}" = 1 ] && exit 0 || exit 1 ;;
      "scheduler jobs resume") [ "${RESUME_FAILS:-0}" = 1 ] && exit 1 || exit 0 ;;
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
    for i, token in enumerate(argv):
        if token == name:
            return argv[i + 1]
        if token.startswith(name + "="):
            return token[len(name) + 1:]
    raise AssertionError(f"{name} not passed: {argv}")


def _env_vars(argv: List[str]) -> Dict[str, str]:
    return dict(pair.split("=", 1) for pair in _flag(argv, "--set-env-vars").split(","))


def _args(argv: List[str]) -> List[str]:
    tokens = [t for t in argv if t.startswith("--args=")]
    assert len(tokens) == 1 and "--args" not in argv, argv
    return tokens[0][len("--args="):].split(",")


def _pause_resume(calls: List[List[str]]) -> List[Tuple[str, str]]:
    return [(c[2], c[3]) for c in calls if c[:2] == ["scheduler", "jobs"] and c[2] in ("pause", "resume")]


def _seconds(value: str) -> int:
    return int(re.fullmatch(r"(\d+)s", value).group(1))


def test_the_script_parses_and_is_executable():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert os.access(SCRIPT, os.X_OK), "the operator runs it directly, like the Tier B script"


def test_dark_by_default_both_jobs_on_the_crawl_subnet_dry_run_and_paused(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG)
    assert proc.returncode == 0, proc.stderr
    for name in JOBS:
        job = _one(calls, "run", "jobs", "create", name)
        assert _flag(job, "--subnet") == "pivota-crawl", "never the default subnet: that is the payment NAT"
        assert _flag(job, "--network") == "default"
        assert _flag(job, "--vpc-egress") == "all-traffic", "private-ranges-only would leave via the default NAT"
        assert _flag(job, "--max-retries") == "0"
        assert _flag(job, "--image") == f"us-west1-docker.pkg.dev/pivota-shared/pivota/backend:{TAG}"
        assert _flag(job, "--service-account") == "sa-worker@pivota-prod.iam.gserviceaccount.com"
        assert _flag(job, "--command") == "python"
        assert _env_vars(job)["REAP_CART_PROOF_APPLY"] == "false"
        trigger = _one(calls, "scheduler", "jobs", "create", "http", f"{name}-cron")
        assert _flag(trigger, "--time-zone") == "Etc/UTC"
        assert _flag(trigger, "--uri").endswith(f"/namespaces/pivota-prod/jobs/{name}:run")
    assert sorted(_pause_resume(calls)) == sorted(("pause", f"{n}-cron") for n in JOBS)
    assert "DARK" in proc.stdout and "DRY RUN" in proc.stdout


def test_enable_arms_all_four_switches_together(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG, "--enable")
    assert proc.returncode == 0, proc.stderr
    for name in JOBS:
        assert _env_vars(_one(calls, "run", "jobs", "create", name))["REAP_CART_PROOF_APPLY"] == "true"
    assert sorted(_pause_resume(calls)) == sorted(("resume", f"{n}-cron") for n in JOBS)
    assert "ARMED" in proc.stdout


def test_a_rerun_without_enable_updates_in_place_and_disarms(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG, env={"JOB_EXISTS": "1", "TRIGGER_EXISTS": "1"})
    assert proc.returncode == 0, proc.stderr
    for name in JOBS:
        assert _env_vars(_one(calls, "run", "jobs", "update", name))["REAP_CART_PROOF_APPLY"] == "false"
        _one(calls, "scheduler", "jobs", "update", "http", f"{name}-cron")
    assert not [c for c in calls if c[2:3] == ["create"]]
    assert {state for state, _ in _pause_resume(calls)} == {"pause"}


def test_a_failed_resume_fails_the_script(tmp_path):
    proc, _calls = _run(tmp_path, "prod", TAG, "--enable", env={"RESUME_FAILS": "1"})
    assert proc.returncode == 1 and "FAILED to resume" in proc.stderr


def test_the_env_is_exactly_the_gate_the_db_guardrails_and_the_plumbing(tmp_path):
    """Nothing else: no REAP key (writing a proof needs none), no pacing override (the timeouts are
    sized from the writers' defaults), no second vantage."""
    _proc, calls = _run(tmp_path, "prod", TAG)
    for name in JOBS:
        job = _one(calls, "run", "jobs", "create", name)
        assert _flag(job, "--set-secrets") == "DATABASE_URL=DATABASE_URL:latest"
        assert _env_vars(job) == {
            "PIVOTA_ENV": "production", "PIVOTA_SERVICE_NAME": name, "PIVOTA_COMMIT_SHA": TAG,
            "DB_POOL_MIN_SIZE": "1", "DB_POOL_MAX_SIZE": "2",
            "DB_STATEMENT_TIMEOUT_SECONDS": "30", "DB_COMMAND_TIMEOUT_SECONDS": "600",
            "REAP_CART_PROOF_APPLY": "false",
        }


def test_args_name_the_wrapper_lane_the_egress_flag_and_the_budget_in_the_equals_form(tmp_path):
    _proc, calls = _run(tmp_path, "prod", TAG)
    for name, lane in (("reap-cart-proof-mirror", "mirror"), ("reap-cart-proof-enrichment", "enrichment")):
        args = _args(_one(calls, "run", "jobs", "create", name))
        assert args[:3] == ["-m", "jobs.reap_cart_proof_refresh", lane], args
        assert "--on-crawl-egress" in args, "the wrapper exits 2 without it"
        assert float(args[args.index("--budget-seconds") + 1]) > 0


def test_the_args_parse_as_the_wrapper_cli(tmp_path):
    """The job's argv must be one the wrapper's own parser accepts."""
    import jobs.reap_cart_proof_refresh as refresh

    _proc, calls = _run(tmp_path, "prod", TAG)
    for name in JOBS:
        args = _args(_one(calls, "run", "jobs", "create", name))
        parsed = refresh._parse(args[2:])
        assert parsed.on_crawl_egress and parsed.budget_seconds > 0 and parsed.lane in refresh.LANES
    source = Path(refresh.__file__).read_text()
    assert 'if __name__ == "__main__":\n    raise SystemExit(main())' in source


def _job_numbers(calls: List[List[str]], name: str) -> Tuple[int, float, str]:
    job = _one(calls, "run", "jobs", "create", name)
    args = _args(job)
    trigger = _one(calls, "scheduler", "jobs", "create", "http", f"{name}-cron")
    return _seconds(_flag(job, "--task-timeout")), float(args[args.index("--budget-seconds") + 1]), \
        _flag(trigger, "--schedule")


#: Handles per enrichment store for the worst case (one .js per handle), from #2464's prod census
#: (2026-09-29): products per host; MAC is 286 products + 1,583 folded shades, each its own handle.
ENRICHMENT_WORST_CASE_HANDLES = {
    "stilacosmetics.com": 124, "jsmbeauty.sg": 171, "bluemercury.com": 250,
    "tartecosmetics.com": 417, "maccosmetics.com": 1870,
}


def test_the_enrichment_timeout_covers_the_all_js_fallback_through_mac(tmp_path):
    """The budget stops the wrapper STARTING a domain; one in flight finishes. Walk the wrapper's
    own domain order at the writer's own default gap: every domain that starts inside the budget
    runs to its end, and the task timeout must outlast that, or Cloud Run — not the budget — ends
    the run and the domain in flight loses every write (the writer writes at the domain's end)."""
    import jobs.enrichment_cart_variant_proof as writer
    import jobs.reap_cart_proof_refresh as refresh

    _proc, calls = _run(tmp_path, "prod", TAG)
    timeout, budget, _ = _job_numbers(calls, "reap-cart-proof-enrichment")
    assert set(refresh.ENRICHMENT_DOMAINS) == set(ENRICHMENT_WORST_CASE_HANDLES)
    gap = writer.request_gap_s({})
    elapsed = 0.0
    for domain in refresh.ENRICHMENT_DOMAINS:
        if elapsed >= budget:
            break
        elapsed += ENRICHMENT_WORST_CASE_HANDLES[domain] * gap
    assert elapsed > budget, "precondition: the fallback does overrun the budget, so MAC is the tail"
    assert timeout >= elapsed + 1800, (timeout, elapsed)
    assert refresh.ENRICHMENT_DOMAINS[-1] == "maccosmetics.com", "the long store goes last"


def test_the_mirror_timeout_covers_the_budget_plus_one_page(tmp_path):
    import jobs.reap_cart_proof_refresh as refresh
    from scripts import backfill_shopify_variant_ids as backfill

    _proc, calls = _run(tmp_path, "prod", TAG)
    timeout, budget, _ = _job_numbers(calls, "reap-cart-proof-mirror")
    one_page = refresh.MIRROR_PAGE_SIZE * (backfill.PER_DOMAIN_MIN_GAP_S + backfill.REQUEST_TIMEOUT_S)
    assert timeout >= budget + one_page, (timeout, budget, one_page)


def test_a_daily_run_keeps_each_proof_inside_its_validity(tmp_path):
    """Daily, and the worst-case end of one run to the next run's worst-case end must be well
    inside the proof's life: 72 h for enrichment, 7 days for mirror."""
    from services.reap_enrichment_cart_proof import MAX_PROOF_AGE
    from services.shopify_variant_identity import CART_PROOF_MAX_AGE

    _proc, calls = _run(tmp_path, "prod", TAG)
    for name, ttl in (("reap-cart-proof-enrichment", MAX_PROOF_AGE),
                      ("reap-cart-proof-mirror", CART_PROOF_MAX_AGE)):
        timeout, _budget, cron = _job_numbers(calls, name)
        assert len(_cron_starts(cron)) == 1, "daily"
        assert timedelta(days=1) + timedelta(seconds=timeout) <= ttl / 2, (name, ttl)


def _cron_starts(expr: str) -> List[int]:
    minute, hour, *rest = expr.split()
    assert rest == ["*", "*", "*"] and minute.isdigit(), expr
    hours: List[int] = []
    for part in hour.split(","):
        lo, _, hi = part.partition("-")
        hours.extend(range(int(lo), int(hi or lo) + 1))
    return [h * 60 + int(minute) for h in hours]


def _windows(expr: str, timeout_s: int, max_retries: int = 0) -> List[Tuple[int, int]]:
    span_min = -(-timeout_s * (max_retries + 1) // 60)
    out: List[Tuple[int, int]] = []
    for start in _cron_starts(expr):
        # A window that crosses midnight is also checked against the next day's starts.
        out.extend([(start, start + span_min), (start - 1440, start - 1440 + span_min)])
    return out


def _last_int(pattern: str, text: str) -> int:
    found = re.findall(pattern, text)
    assert found, pattern
    return int(found[-1])


def _overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


#: Crawl-subnet jobs that exist in prod but are defined by NO script in this repo, read live on
#: 2026-09-30 (`gcloud scheduler jobs list` / `gcloud run jobs describe`, read-only).
LIVE_ONLY_NEIGHBOURS = {
    # external-seed-destination-sweep's LIVE trigger is 03:15 with 0 retries; the script says 02:20
    # with mkcrawljob's retry. Both are checked (the script's below, from the script).
    "external-seed-destination-sweep (live)": ("15 3 * * *", 3600, 0),
    "external-referral-refresh (live)": ("15 5 * * *", 3600, 0),
}


def test_the_schedules_never_overlap_the_daily_crawls_or_each_other(tmp_path):
    _proc, calls = _run(tmp_path, "prod", TAG)
    ours = {}
    for name in JOBS:
        timeout, _budget, cron = _job_numbers(calls, name)
        ours[name] = _windows(cron, timeout)

    tierb = TIERB_SCRIPT.read_text()
    tierb_cron = re.search(r'^SCHEDULE="([^"]+)"', tierb, re.M).group(1)
    sched = SCHEDULER_SCRIPT.read_text()
    seed_cron = re.search(r'sched external-seed-destination-sweep-cron "([^"]+)"', sched).group(1)
    helper = sched[sched.index("mkcrawljob(){"):]
    helper = helper[: helper.index("\n}\n")]
    seed_block = sched[sched.index("mkcrawljob external-seed-destination-sweep"):]
    seed_block = seed_block[: seed_block.index("\nelse")]
    neighbours = {
        "tierb-cart-link-eligibility": _windows(
            tierb_cron, _last_int(r"--task-timeout (\d+)s", tierb), _last_int(r"--max-retries (\d+)", tierb)),
        "external-seed-destination-sweep (script)": _windows(
            seed_cron, _last_int(r"--task-timeout (\d+)s", helper + seed_block),
            _last_int(r"--max-retries (\d+)", helper + seed_block)),
    }
    for label, (cron, timeout, retries) in LIVE_ONLY_NEIGHBOURS.items():
        neighbours[label] = _windows(cron, timeout, retries)

    for name, windows in ours.items():
        for label, theirs in neighbours.items():
            clashes = [(a, b) for a in windows for b in theirs if _overlaps(a, b)]
            assert clashes == [], f"{name} overlaps {label}: {clashes}"
    a, b = (ours[n] for n in JOBS)
    assert not [(x, y) for x in a for y in b if _overlaps(x, y)], "the two jobs share stores"

    for name in JOBS:
        (start,) = _cron_starts(_job_numbers(calls, name)[2])
        assert start % 5, f"{name} starts on a multiple of 5: the probe / drain bursts"
        assert start % 60 != 7, f"{name} starts with the hourly purchasability sweep"


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


def test_a_missing_crawl_subnet_stops_before_any_job_or_trigger(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG, env={"SUBNET_EXISTS": "0"})
    assert proc.returncode == 1 and "setup_crawl_egress.sh" in proc.stderr
    assert not [c for c in calls if c[:2] == ["run", "jobs"] and c[2] in ("create", "update")]
    assert not [c for c in calls if c[:2] == ["scheduler", "jobs"] and c[2] != "describe"]


def test_staging_targets_the_staging_project(tmp_path):
    proc, calls = _run(tmp_path, "staging", TAG)
    assert proc.returncode == 0, proc.stderr
    for name in JOBS:
        job = _one(calls, "run", "jobs", "create", name)
        assert _flag(job, "--service-account") == "sa-worker@pivota-staging.iam.gserviceaccount.com"
        assert _env_vars(job)["PIVOTA_ENV"] == "staging"
        trigger = _one(calls, "scheduler", "jobs", "create", "http", f"{name}-cron")
        assert "/namespaces/pivota-staging/" in _flag(trigger, "--uri")
