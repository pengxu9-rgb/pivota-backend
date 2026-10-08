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

# Fields separated by \x1f, one invocation per line. Reads are answered from env:
#   JOB_EXISTS / TRIGGER_EXISTS          create vs update, and whether describe answers at all
#   <MIRROR|ENRICHMENT>_JOB_EXISTS, _TRIGGER_EXISTS   the same, for one job
#   ENRICHMENT_GATE / MIRROR_GATE        the job's REAP_CART_PROOF_APPLY as `describe --format=json` shows it
#   ENRICHMENT_TRIGGER / MIRROR_TRIGGER  the trigger's `state`
#   SUBNET_EXISTS=0                      the crawl-subnet preflight fails
#   RESUME_FAILS=1                       every `scheduler jobs resume` fails
#   FAIL_UPDATE_JOB=<name>               `run jobs create|update <name>` fails
#   ENRICHMENT_SIGNED / MIRROR_SIGNED    the job's CRAWL_WEB_BOT_AUTH_ENABLED (unset = absent)
#   SECRET_EXISTS=0                      `secrets describe` fails
#   SECRET_GRANTED=0                     the secret's IAM policy has no accessor binding for sa-worker
#   MIRROR_ARGS / ENRICHMENT_ARGS        the job's current container args, as a JSON value (unset = absent)
#   JSON_FAIL_NTH=<name>:<n>             that job's n-th `run jobs describe --format=json` fails (1-based)
#   PLAIN_FAIL_NTH=<name>:<n>            that job's n-th plain (existence) `run jobs describe` fails
FAKE_GCLOUD = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf '%s\\x1f' "$@" >> "$GCLOUD_LOG"; printf '\\n' >> "$GCLOUD_LOG"
    case "$1 $2 $3" in
      "run jobs describe")
        if [ -n "${JSON_FAIL_NTH:-}" ] && [ "$4" = "${JSON_FAIL_NTH%%:*}" ] && [ "${*: -1}" = "--format=json" ]; then
          n=$(( $(cat "$GCLOUD_LOG.n" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$GCLOUD_LOG.n"
          [ "$n" = "${JSON_FAIL_NTH##*:}" ] && exit 1
        fi
        if [ -n "${PLAIN_FAIL_NTH:-}" ] && [ "$4" = "${PLAIN_FAIL_NTH%%:*}" ] && [ "${*: -1}" != "--format=json" ]; then
          m=$(( $(cat "$GCLOUD_LOG.m" 2>/dev/null || echo 0) + 1 )); echo "$m" > "$GCLOUD_LOG.m"
          [ "$m" = "${PLAIN_FAIL_NTH##*:}" ] && exit 1
        fi
        case "$4" in
          reap-cart-proof-mirror) e="${MIRROR_JOB_EXISTS:-${JOB_EXISTS:-0}}"; g="${MIRROR_GATE:-false}"; w="${MIRROR_SIGNED:-}"; a="${MIRROR_ARGS:-}" ;;
          *) e="${ENRICHMENT_JOB_EXISTS:-${JOB_EXISTS:-0}}"; g="${ENRICHMENT_GATE:-false}"; w="${ENRICHMENT_SIGNED:-}"; a="${ENRICHMENT_ARGS:-}" ;;
        esac
        [ "$e" = 1 ] || exit 1
        extra=""; [ -n "$w" ] && extra=",{\\"name\\":\\"CRAWL_WEB_BOT_AUTH_ENABLED\\",\\"value\\":\\"$w\\"}"
        argsjson=""; [ -n "$a" ] && argsjson=",\\"args\\":$a"
        printf '{"spec":{"template":{"spec":{"template":{"spec":{"containers":[{"env":[{"name":"PIVOTA_ENV","value":"production"},{"name":"REAP_CART_PROOF_APPLY","value":"%s"}%s]%s}]}}}}}}' "$g" "$extra" "$argsjson"
        exit 0 ;;
      "scheduler jobs describe")
        case "$4" in
          reap-cart-proof-mirror-cron) e="${MIRROR_TRIGGER_EXISTS:-${TRIGGER_EXISTS:-0}}"; t="${MIRROR_TRIGGER:-PAUSED}" ;;
          *) e="${ENRICHMENT_TRIGGER_EXISTS:-${TRIGGER_EXISTS:-0}}"; t="${ENRICHMENT_TRIGGER:-PAUSED}" ;;
        esac
        [ "$e" = 1 ] || exit 1
        echo "$t"; exit 0 ;;
      "compute networks subnets") [ "${SUBNET_EXISTS:-1}" = 1 ] && exit 0 || exit 1 ;;
      "secrets describe WEB_BOT_AUTH_PRIVATE_KEY") [ "${SECRET_EXISTS:-1}" = 1 ] && exit 0 || exit 1 ;;
      "secrets get-iam-policy WEB_BOT_AUTH_PRIVATE_KEY")
        if [ "${SECRET_GRANTED:-1}" = 1 ]; then
          printf '{"bindings":[{"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:sa-worker@pivota-prod.iam.gserviceaccount.com"]}]}'
        else
          printf '{"bindings":[{"role":"roles/secretmanager.secretAccessor","members":["serviceAccount:someone-else@pivota-prod.iam.gserviceaccount.com"]}]}'
        fi
        exit 0 ;;
      "scheduler jobs resume") [ "${RESUME_FAILS:-0}" = 1 ] && exit 1 || exit 0 ;;
      "run jobs create"|"run jobs update") [ "$4" = "${FAIL_UPDATE_JOB:-}" ] && exit 1 || exit 0 ;;
    esac
    exit 0
    """
)

ARMED = {"JOB_EXISTS": "1", "TRIGGER_EXISTS": "1", "ENRICHMENT_GATE": "true", "MIRROR_GATE": "true",
         "ENRICHMENT_TRIGGER": "ENABLED", "MIRROR_TRIGGER": "ENABLED"}
DARK = {"JOB_EXISTS": "1", "TRIGGER_EXISTS": "1"}


def _run(tmp_path: Path, *args: str, env: Optional[Dict[str, str]] = None, shell: str = "bash"
         ) -> Tuple[subprocess.CompletedProcess, List[List[str]]]:
    fake = tmp_path / "fake-gcloud"
    fake.write_text(FAKE_GCLOUD)
    fake.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    for counter in ("calls.log.n", "calls.log.m"):  # the fake's JSON_FAIL_NTH / PLAIN_FAIL_NTH counters
        (tmp_path / counter).unlink(missing_ok=True)
    proc = subprocess.run(
        [shell, str(SCRIPT), *args],
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


def _final_states(calls: List[List[str]]) -> Dict[str, str]:
    """Each trigger's LAST pause/resume: the state the script leaves it in."""
    return {trigger: verb for verb, trigger in _pause_resume(calls)}


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
    assert _final_states(calls) == {f"{n}-cron": "pause" for n in JOBS}
    assert not [c for c in calls if c[:3] == ["scheduler", "jobs", "resume"]]
    assert "DARK" in proc.stdout and "DRY RUN" in proc.stdout


def test_enable_arms_all_four_switches_together(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG, "--enable")
    assert proc.returncode == 0, proc.stderr
    for name in JOBS:
        assert _env_vars(_one(calls, "run", "jobs", "create", name))["REAP_CART_PROOF_APPLY"] == "true"
    assert _final_states(calls) == {f"{n}-cron": "resume" for n in JOBS}
    resumes = [i for i, c in enumerate(calls) if c[:3] == ["scheduler", "jobs", "resume"]]
    job_writes = [i for i, c in enumerate(calls) if c[:2] == ["run", "jobs"] and c[2] in ("create", "update")]
    assert min(resumes) > max(job_writes), "the triggers are resumed last"
    assert "ARMED" in proc.stdout


def _writes(calls: List[List[str]]) -> List[List[str]]:
    return [c for c in calls if not (c[1:3] == ["jobs", "describe"] or c[:2] in (["iam", "service-accounts"],
                                                                                ["artifacts", "docker"],
                                                                                ["compute", "networks"]))]


def test_a_plain_rerun_of_armed_jobs_keeps_them_armed(tmp_path):
    """F5: re-imaging onto a newer tag must not silently disarm."""
    proc, calls = _run(tmp_path, "prod", TAG, env=ARMED)
    assert proc.returncode == 0, proc.stderr
    for name in JOBS:
        assert _env_vars(_one(calls, "run", "jobs", "update", name))["REAP_CART_PROOF_APPLY"] == "true"
        _one(calls, "scheduler", "jobs", "update", "http", f"{name}-cron")
    assert {state for state, _ in _pause_resume(calls)} == {"resume"}
    assert "KEEPING" in proc.stdout and "DISARMING" not in proc.stdout


def test_a_plain_rerun_of_dark_jobs_keeps_them_dark(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG, env=DARK)
    assert proc.returncode == 0, proc.stderr
    for name in JOBS:
        assert _env_vars(_one(calls, "run", "jobs", "update", name))["REAP_CART_PROOF_APPLY"] == "false"
    assert not [c for c in calls if c[2:3] == ["create"]]
    assert {state for state, _ in _pause_resume(calls)} == {"pause"}
    assert "KEEPING" in proc.stdout


def test_disable_disarms_loudly_and_pauses_the_triggers_before_touching_a_job(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG, "--disable", env=ARMED)
    assert proc.returncode == 0, proc.stderr
    assert "DISARMING" in proc.stdout
    for name in JOBS:
        assert _env_vars(_one(calls, "run", "jobs", "update", name))["REAP_CART_PROOF_APPLY"] == "false"
    writes = _writes(calls)
    first_job_write = next(i for i, c in enumerate(writes) if c[:3] == ["run", "jobs", "update"])
    paused_first = [c[3] for c in writes[:first_job_write] if c[:3] == ["scheduler", "jobs", "pause"]]
    assert sorted(paused_first) == sorted(f"{n}-cron" for n in JOBS), writes
    assert not [c for c in calls if c[:3] == ["scheduler", "jobs", "resume"]]


@pytest.mark.parametrize("env, why", [
    ({**DARK, "MIRROR_GATE": "true", "MIRROR_TRIGGER": "ENABLED"}, "SPLIT"),
    ({**DARK, "ENRICHMENT_GATE": "true"}, "disagree"),
    ({**DARK, "MIRROR_TRIGGER": "ENABLED"}, "disagree"),
])
def test_a_plain_rerun_refuses_a_split_or_inconsistent_state_before_writing(tmp_path, env, why):
    proc, calls = _run(tmp_path, "prod", TAG, env=env)
    assert proc.returncode == 1 and why in proc.stderr and "Nothing was changed" in proc.stderr
    assert _writes(calls) == [], "refused after reading, before any write"


@pytest.mark.parametrize("flag, gate, verb", [("--enable", "true", "resume"), ("--disable", "false", "pause")])
def test_an_explicit_flag_settles_a_split_state(tmp_path, flag, gate, verb):
    env = {**DARK, "MIRROR_GATE": "true", "MIRROR_TRIGGER": "ENABLED"}
    proc, calls = _run(tmp_path, "prod", TAG, flag, env=env)
    assert proc.returncode == 0, proc.stderr
    for name in JOBS:
        assert _env_vars(_one(calls, "run", "jobs", "update", name))["REAP_CART_PROOF_APPLY"] == gate
    assert {state for state, _ in _pause_resume(calls)} == {verb}


def test_an_arm_that_fails_on_the_second_job_resumes_no_trigger(tmp_path):
    """The triggers are resumed LAST, so a failure part-way through an arm leaves nothing firing."""
    proc, calls = _run(tmp_path, "prod", TAG, "--enable", env={"FAIL_UPDATE_JOB": "reap-cart-proof-mirror"})
    assert proc.returncode != 0
    assert not [c for c in calls if c[:3] == ["scheduler", "jobs", "resume"]]
    assert not [c for c in calls if c[:3] == ["scheduler", "jobs", "create"]]


@pytest.mark.parametrize("flag", ["", "--enable", "--disable"])
def test_a_created_trigger_is_paused_before_any_other_write(tmp_path, flag):
    """N5: `scheduler jobs create` has no --paused and a new trigger starts ENABLED."""
    proc, calls = _run(tmp_path, "prod", TAG, *([flag] if flag else []))
    assert proc.returncode == 0, proc.stderr
    writes = _writes(calls)
    created = [i for i, c in enumerate(writes) if c[:4] == ["scheduler", "jobs", "create", "http"]]
    assert len(created) == 2
    for i in created:
        assert writes[i + 1][:4] == ["scheduler", "jobs", "pause", writes[i][4]], writes[i:i + 2]


@pytest.mark.parametrize("missing", ["MIRROR", "ENRICHMENT"])
def test_a_plain_rerun_never_creates_a_missing_job_next_to_an_armed_one(tmp_path, missing):
    """N5: one job armed, the other missing: that is a split. A plain re-run refuses without writing;
    only --enable creates the missing one (armed, like its twin)."""
    env = {**ARMED, f"{missing}_JOB_EXISTS": "0", f"{missing}_TRIGGER_EXISTS": "0"}
    proc, calls = _run(tmp_path, "prod", TAG, env=env)
    assert proc.returncode == 1 and "SPLIT" in proc.stderr and "missing" in proc.stderr
    assert _writes(calls) == []
    proc, calls = _run(tmp_path, "prod", TAG, "--enable", env=env)
    assert proc.returncode == 0, proc.stderr
    name = f"reap-cart-proof-{missing.lower()}"
    assert _env_vars(_one(calls, "run", "jobs", "create", name))["REAP_CART_PROOF_APPLY"] == "true"


def test_a_plain_rerun_with_one_job_missing_and_the_other_dark_creates_it_dark(tmp_path):
    env = {**DARK, "MIRROR_JOB_EXISTS": "0", "MIRROR_TRIGGER_EXISTS": "0"}
    proc, calls = _run(tmp_path, "prod", TAG, env=env)
    assert proc.returncode == 0, proc.stderr
    assert _env_vars(_one(calls, "run", "jobs", "create", "reap-cart-proof-mirror"))["REAP_CART_PROOF_APPLY"] == "false"
    assert _final_states(calls) == {f"{n}-cron": "pause" for n in JOBS}


def test_a_failed_resume_fails_the_script(tmp_path):
    proc, _calls = _run(tmp_path, "prod", TAG, "--enable", env={"RESUME_FAILS": "1"})
    assert proc.returncode == 1 and "FAILED to resume" in proc.stderr


@pytest.mark.parametrize("flag, gate", [((), "false"), (("--enable",), "true")])
def test_the_env_is_exactly_the_gate_the_db_guardrails_the_plumbing_and_the_shared_pacer(tmp_path, flag, gate):
    """Nothing else: no REAP key (writing a proof needs none), no writer pacing override (the timeouts
    are sized from the writers' defaults), no second vantage. The shared Shopify-edge pacer (#2474) is
    ON for both jobs whether dark or armed -- a dry run fetches exactly as hard -- with its lease CAPPED
    at 2 (leases are sized by demand; the cap bounds what one lane can hold of the shared schedule)."""
    _proc, calls = _run(tmp_path, "prod", TAG, *flag)
    for name in JOBS:
        job = _one(calls, "run", "jobs", "create", name)
        assert _flag(job, "--set-secrets") == "DATABASE_URL=DATABASE_URL:latest"
        assert _env_vars(job) == {
            "PIVOTA_ENV": "production", "PIVOTA_SERVICE_NAME": name, "PIVOTA_COMMIT_SHA": TAG,
            "DB_POOL_MIN_SIZE": "1", "DB_POOL_MAX_SIZE": "2",
            "DB_STATEMENT_TIMEOUT_SECONDS": "30", "DB_COMMAND_TIMEOUT_SECONDS": "600",
            "REAP_CART_PROOF_APPLY": gate,
            "CRAWL_SHOPIFY_EDGE_PACER_ENABLED": "true", "CRAWL_SHOPIFY_EDGE_LEASE": "2",
        }


def test_the_pacer_env_names_are_the_pacers_own():
    """The setup script's names must be the ones services/shopify_edge_pacer.py reads, and the lease cap
    one it accepts unclamped (#2474 sizes leases by demand, capped at CRAWL_SHOPIFY_EDGE_LEASE)."""
    import os

    from services import shopify_edge_pacer

    text = SCRIPT.read_text()
    assert f"{shopify_edge_pacer.ENABLED_ENV}=true" in text
    assert f"{shopify_edge_pacer.LEASE_ENV}=2" in text
    old = os.environ.get(shopify_edge_pacer.LEASE_ENV)
    os.environ[shopify_edge_pacer.LEASE_ENV] = "2"
    try:
        assert shopify_edge_pacer.lease_size() == 2, "the cap is taken as set, not clamped"
    finally:
        if old is None:
            del os.environ[shopify_edge_pacer.LEASE_ENV]
        else:
            os.environ[shopify_edge_pacer.LEASE_ENV] = old


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
    # With the politeness gate in front of every request, a request can wait up to
    # MIRROR_MAX_POLITE_WAIT_S before it is sent (or given up), then take the request timeout.
    # Every hop the client follows by hand (up to MIRROR_MAX_REDIRECTS) can take a request timeout; the
    # polite wait is ONE deadline for the whole request, hops and lease refills included.
    one_page = refresh.MIRROR_PAGE_SIZE * (backfill.PER_DOMAIN_MIN_GAP_S + refresh.MIRROR_MAX_POLITE_WAIT_S
                                           + (refresh.MIRROR_MAX_REDIRECTS + 1) * backfill.REQUEST_TIMEOUT_S)
    assert timeout >= budget + one_page, (timeout, budget, one_page)


#: Prod census, 2026-09-30, read-only (scripts/ops/run_oneoff_job.sh, the backfill's own eligibility
#: SQL, counts only): mirror candidates over the 42 Tier B domains. Re-measure before changing the budget.
MIRROR_CANDIDATES_CENSUS = 2092


def test_the_mirror_budget_covers_the_measured_candidates_with_headroom(tmp_path):
    """The lane walks stores one at a time at the backfill's per-store gap; the budget must cover every
    measured candidate at that pace, plus the inter-call gaps, with at least 1.5x headroom."""
    import jobs.reap_cart_proof_refresh as refresh
    from scripts import backfill_shopify_variant_ids as backfill

    _proc, calls = _run(tmp_path, "prod", TAG)
    _timeout, budget, _ = _job_numbers(calls, "reap-cart-proof-mirror")
    gap = refresh.inter_call_gap_s("mirror", backfill=backfill)
    calls_needed = MIRROR_CANDIDATES_CENSUS // refresh.MIRROR_PAGE_SIZE + 42
    need = MIRROR_CANDIDATES_CENSUS * backfill.PER_DOMAIN_MIN_GAP_S + calls_needed * gap
    assert budget >= 1.5 * need, (budget, need)


def test_the_shared_edge_budget_cannot_slow_either_lane_below_its_own_pacing():
    """Re-derived at 2 req/s shared: each lane sends one request per 3 s (0.33 req/s), under the pacer's
    fail-open LOCAL rate (a quarter of the shared rate), let alone the shared rate itself. So the
    timeouts, sized from each writer's own pacing, still hold with the pacer on."""
    import jobs.enrichment_cart_variant_proof as writer
    from scripts import backfill_shopify_variant_ids as backfill
    from services import shopify_edge_pacer

    assert shopify_edge_pacer.rate() == 2.0
    for lane_gap in (backfill.PER_DOMAIN_MIN_GAP_S, writer.request_gap_s({})):
        assert 1.0 / lane_gap <= shopify_edge_pacer.fallback_rate() <= shopify_edge_pacer.rate()


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
    (("prod", TAG, "--enable", "--disable"), "too many arguments"),
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


@pytest.mark.parametrize("flag", [(), ("--enable",), ("--disable",)])
def test_the_shared_pacer_rate_is_left_unset_on_both_jobs(tmp_path, flag):
    """#2474: every crawl job on the crawl IP MUST use the same CRAWL_SHOPIFY_EDGE_RPS, so these jobs
    never set their own; they inherit the global default the pacer reads."""
    from services import shopify_edge_pacer

    proc, calls = _run(tmp_path, "prod", TAG, *flag)
    assert proc.returncode == 0, proc.stderr
    for name in JOBS:
        job = _one(calls, "run", "jobs", "create", name)
        env = _env_vars(job)
        assert shopify_edge_pacer.RATE_ENV == "CRAWL_SHOPIFY_EDGE_RPS"
        assert shopify_edge_pacer.RATE_ENV not in env, env
        assert shopify_edge_pacer.FALLBACK_RATE_ENV not in env, env
    code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    assert "CRAWL_SHOPIFY_EDGE_RPS" not in code


# ── Web Bot Auth (services/crawl_identity.py) ───────────────────────────────────────────────────

def _signed_state(job: List[str]) -> Tuple[Optional[str], str]:
    return _env_vars(job).get("CRAWL_WEB_BOT_AUTH_ENABLED"), _flag(job, "--set-secrets")


@pytest.mark.parametrize("env,expect", [
    ({"WEB_BOT_AUTH": "on"}, ("true", "DATABASE_URL=DATABASE_URL:latest,WEB_BOT_AUTH_PRIVATE_KEY=WEB_BOT_AUTH_PRIVATE_KEY:latest")),
    ({"WEB_BOT_AUTH": "off", **DARK, "MIRROR_SIGNED": "true", "ENRICHMENT_SIGNED": "true"}, (None, "DATABASE_URL=DATABASE_URL:latest")),
    ({**DARK, "MIRROR_SIGNED": "true", "ENRICHMENT_SIGNED": "true"}, ("true", "DATABASE_URL=DATABASE_URL:latest,WEB_BOT_AUTH_PRIVATE_KEY=WEB_BOT_AUTH_PRIVATE_KEY:latest")),
    ({"MIRROR_JOB_EXISTS": "1", "MIRROR_SIGNED": "true"}, ("true", "DATABASE_URL=DATABASE_URL:latest,WEB_BOT_AUTH_PRIVATE_KEY=WEB_BOT_AUTH_PRIVATE_KEY:latest")),
    ({**DARK}, (None, "DATABASE_URL=DATABASE_URL:latest")),
])
def test_web_bot_auth_is_set_removed_or_kept_on_both_jobs_together(tmp_path, env, expect):
    proc, calls = _run(tmp_path, "prod", TAG, env=env)
    assert proc.returncode == 0, proc.stderr
    for name in JOBS:
        exists = env.get("JOB_EXISTS") == "1" or (name.endswith("mirror") and env.get("MIRROR_JOB_EXISTS") == "1")
        assert _signed_state(_one(calls, "run", "jobs", "update" if exists else "create", name)) == expect


@pytest.mark.parametrize("env,message", [
    ({"WEB_BOT_AUTH": "on", "SECRET_EXISTS": "0"}, "WEB_BOT_AUTH_PRIVATE_KEY does not exist"),
    ({"WEB_BOT_AUTH": "on", "SECRET_GRANTED": "0"}, "has no roles/secretmanager.secretAccessor"),
    ({**DARK, "MIRROR_SIGNED": "true", "ENRICHMENT_SIGNED": "false"}, "one job signs"),
    ({**DARK, "MIRROR_SIGNED": "true", "ENRICHMENT_SIGNED": "true", "SECRET_GRANTED": "0"}, "has no roles/secretmanager.secretAccessor"),
])
def test_web_bot_auth_refuses_before_any_write(tmp_path, env, message):
    proc, calls = _run(tmp_path, "prod", TAG, env=env)
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert not [c for c in calls if c[:3] in (["run", "jobs", "create"], ["run", "jobs", "update"])
                or c[:3] in (["scheduler", "jobs", "create"], ["scheduler", "jobs", "pause"])]


def test_a_bad_web_bot_auth_value_exits_2_before_touching_anything(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG, env={"WEB_BOT_AUTH": "yes"})
    assert proc.returncode == 2 and calls == []


def test_the_web_bot_auth_names_are_the_signers_own():
    from services import crawl_identity

    text = SCRIPT.read_text()
    assert f"{crawl_identity.FLAG_ENV}=true" in text
    assert f"{crawl_identity.KEY_ENV}={crawl_identity.KEY_ENV}:latest" in text


# ── scope: a job's `--only` list is kept unless changed on purpose ──────────────────────────────

JUDYDOLL_ARGS = '["-m","jobs.reap_cart_proof_refresh","mirror","--on-crawl-egress","--budget-seconds","10800","--only","judydoll.com"]'
FULL_ARGS = '["-m","jobs.reap_cart_proof_refresh","enrichment","--on-crawl-egress","--budget-seconds","3600"]'
SPLIT_JUDYDOLL = {"JOB_EXISTS": "1", "TRIGGER_EXISTS": "1", "MIRROR_GATE": "true", "MIRROR_TRIGGER": "ENABLED",
                  "MIRROR_ARGS": JUDYDOLL_ARGS, "ENRICHMENT_ARGS": FULL_ARGS}


def _only(argv: List[str]) -> List[str]:
    args = _args(argv)
    return [args[i + 1] for i, a in enumerate(args) if a == "--only"]


@pytest.mark.parametrize("flag", ["--enable", "--disable"])
def test_the_2026_10_08_case_enable_or_disable_keeps_a_judydoll_only_mirror(tmp_path, flag):
    """Peng's mirror ran `--only judydoll.com`, enrichment was dark; `--enable` widened it to 42 stores."""
    proc, calls = _run(tmp_path, "prod", TAG, flag, env=SPLIT_JUDYDOLL)
    assert proc.returncode == 0, proc.stderr
    assert _only(_one(calls, "run", "jobs", "update", "reap-cart-proof-mirror")) == ["judydoll.com"]
    assert _only(_one(calls, "run", "jobs", "update", "reap-cart-proof-enrichment")) == []
    assert "== scope: reap-cart-proof-mirror only=judydoll.com (MIRROR_ONLY=unset, kept)" in proc.stdout


def test_a_plain_rerun_keeps_each_jobs_own_scope(tmp_path):
    env = {**DARK, "MIRROR_ARGS": JUDYDOLL_ARGS,
           "ENRICHMENT_ARGS": FULL_ARGS.replace('"3600"]', '"3600","--only","stilacosmetics.com","--only=bluemercury.com"]')}
    proc, calls = _run(tmp_path, "prod", TAG, env=env)
    assert proc.returncode == 0, proc.stderr
    assert _only(_one(calls, "run", "jobs", "update", "reap-cart-proof-mirror")) == ["judydoll.com"]
    assert _only(_one(calls, "run", "jobs", "update", "reap-cart-proof-enrichment")) == [
        "stilacosmetics.com", "bluemercury.com"]


@pytest.mark.parametrize("value,expect", [("all", []), ("judydoll.com,fentybeauty.com", ["judydoll.com", "fentybeauty.com"])])
def test_mirror_only_sets_or_clears_the_scope_explicitly(tmp_path, value, expect):
    proc, calls = _run(tmp_path, "prod", TAG, "--enable", env={**SPLIT_JUDYDOLL, "MIRROR_ONLY": value})
    assert proc.returncode == 0, proc.stderr
    assert _only(_one(calls, "run", "jobs", "update", "reap-cart-proof-mirror")) == expect
    assert _only(_one(calls, "run", "jobs", "update", "reap-cart-proof-enrichment")) == []


@pytest.mark.parametrize("value,expect", [("all", []), ("stilacosmetics.com", ["stilacosmetics.com"])])
def test_enrichment_only_sets_or_clears_its_own_scope_only(tmp_path, value, expect):
    env = {**SPLIT_JUDYDOLL, "ENRICHMENT_ARGS": FULL_ARGS.replace('"3600"]', '"3600","--only","bluemercury.com"]'),
           "ENRICHMENT_ONLY": value}
    proc, calls = _run(tmp_path, "prod", TAG, "--enable", env=env)
    assert proc.returncode == 0, proc.stderr
    assert _only(_one(calls, "run", "jobs", "update", "reap-cart-proof-enrichment")) == expect
    assert _only(_one(calls, "run", "jobs", "update", "reap-cart-proof-mirror")) == ["judydoll.com"]


def test_new_jobs_walk_every_store(tmp_path):
    proc, calls = _run(tmp_path, "prod", TAG)
    for name in JOBS:
        assert _only(_one(calls, "run", "jobs", "create", name)) == []


@pytest.mark.parametrize("var", ["MIRROR_ONLY", "ENRICHMENT_ONLY"])
@pytest.mark.parametrize("value", ["JudyDoll.com", "judydoll", "a.com;rm", "a.com,,b.com", "https://a.com", " a.com",
                                   "\njudydoll.com", "judydoll.com\nfentybeauty.com", "judydoll.com\n", "a.com, b.com",
                                   "a.com,", ",a.com", "a.com,--budget-seconds,1"])
def test_a_bad_scope_value_exits_2_before_touching_anything(tmp_path, var, value):
    proc, calls = _run(tmp_path, "prod", TAG, "--enable", env={**SPLIT_JUDYDOLL, var: value})
    assert proc.returncode == 2 and f"{var} must be" in proc.stderr and calls == []


@pytest.mark.parametrize("args", ['"not-a-list"', '["-m","x","--only"]', '["--only",""]',
                                  '["--only","--budget-seconds"]', '["--only","a.com,b.com"]', '["--only","a b.com"]'])
def test_an_unreadable_scope_refuses_before_any_write(tmp_path, args):
    proc, calls = _run(tmp_path, "prod", TAG, env={**DARK, "MIRROR_ARGS": args})
    assert proc.returncode == 1 and "could not read reap-cart-proof-mirror's current --only args" in proc.stderr
    assert not [c for c in calls if c[:3] in (["run", "jobs", "create"], ["run", "jobs", "update"])]


def test_kept_scope_args_still_parse_as_the_wrapper_cli(tmp_path):
    from jobs import reap_cart_proof_refresh as wrapper

    _proc, calls = _run(tmp_path, "prod", TAG, "--enable", env=SPLIT_JUDYDOLL)
    args = _args(_one(calls, "run", "jobs", "update", "reap-cart-proof-mirror"))
    assert args[:2] == ["-m", "jobs.reap_cart_proof_refresh"]
    parsed = wrapper._parse(args[2:])
    assert parsed.lane == "mirror" and parsed.only == ["judydoll.com"] and parsed.on_crawl_egress


def _mirror_json_reads(tmp_path) -> int:
    """How many `run jobs describe reap-cart-proof-mirror --format=json` calls a clean --enable makes."""
    _proc, calls = _run(tmp_path, "prod", TAG, "--enable", env=SPLIT_JUDYDOLL)
    return sum(1 for c in calls if c[:4] == ["run", "jobs", "describe", "reap-cart-proof-mirror"] and c[-1] == "--format=json")


def test_a_failed_scope_read_on_an_existing_job_refuses_instead_of_widening(tmp_path):
    """The SCOPE read of the mirror fails (reads as "absent") while every other read works: writing would
    replace a judydoll-only job with all 42 stores. It must refuse before any write."""
    # The mirror's json reads are, in order, the gate, signing and SCOPE reads, and nothing else: a
    # second scope read would let one transient failure be papered over by the next answer.
    (tmp_path / "count").mkdir()
    assert _mirror_json_reads(tmp_path / "count") == 3
    proc, calls = _run(tmp_path, "prod", TAG, "--enable",
                       env={**SPLIT_JUDYDOLL, "JSON_FAIL_NTH": "reap-cart-proof-mirror:3"})
    assert proc.returncode == 1 and "read as missing, then exists" in proc.stderr, proc.stderr
    assert not [c for c in calls if c[:3] in (["run", "jobs", "create"], ["run", "jobs", "update"])]


@pytest.mark.parametrize("nth", [1, 2])
def test_a_failed_earlier_read_never_writes_the_mirror_without_its_scope(tmp_path, nth):
    proc, calls = _run(tmp_path, "prod", TAG, "--enable", env={**SPLIT_JUDYDOLL, "JSON_FAIL_NTH": f"reap-cart-proof-mirror:{nth}"})
    updates = [c for c in calls if c[:4] == ["run", "jobs", "update", "reap-cart-proof-mirror"]]
    assert all(_only(c) == ["judydoll.com"] for c in updates), (proc.returncode, proc.stderr)


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="no /bin/bash")
def test_the_2026_10_08_case_under_the_systems_own_bash(tmp_path):
    """The operator runs `bash` from a Mac (/bin/bash 3.2); the harness's PATH bash may be 5.x."""
    proc, calls = _run(tmp_path, "prod", TAG, "--enable", env=SPLIT_JUDYDOLL, shell="/bin/bash")
    assert proc.returncode == 0, proc.stderr
    assert _only(_one(calls, "run", "jobs", "update", "reap-cart-proof-mirror")) == ["judydoll.com"]
    bad, calls = _run(tmp_path, "prod", TAG, env={"MIRROR_ONLY": "\njudydoll.com"}, shell="/bin/bash")
    assert bad.returncode == 2 and calls == []


def test_a_scope_read_and_an_existence_check_both_failing_never_update_without_the_scope(tmp_path):
    """Two transient failures in a row: the scope read AND the one existence check. The job is then
    CREATED (which real gcloud refuses for an existing job, loudly), never updated with no --only."""
    proc, calls = _run(tmp_path, "prod", TAG, "--enable", env={
        **SPLIT_JUDYDOLL, "JSON_FAIL_NTH": "reap-cart-proof-mirror:3", "PLAIN_FAIL_NTH": "reap-cart-proof-mirror:1"})
    assert not [c for c in calls if c[:4] == ["run", "jobs", "update", "reap-cart-proof-mirror"]], proc.stdout
    plain = [c for c in calls if c[:4] == ["run", "jobs", "describe", "reap-cart-proof-mirror"] and c[-1] != "--format=json"]
    assert len(plain) == 1  # existence is checked once, and the write's verb comes from that check
