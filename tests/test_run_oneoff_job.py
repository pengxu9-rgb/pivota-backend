"""`scripts/ops/run_oneoff_job.sh` — the GCP replacement for `railway run`.

WHY THIS EXISTS. Since the 2026-08-22 cutover production is Cloud Run in
pivota-prod/us-west1 and Railway is the ROLLBACK, so every operator script whose
usage text said `railway run` was handing back an instruction that acts on the
platform nobody is served from. Those docstrings now point here, which makes this
script the single place all of them can be wrong at once.

The properties under test are the three that have already caused incidents:

  1. THE VERDICT IS THE EXIT CODE, NOT THE LOG. Cloud Logging ingestion lag is
     unbounded, so a read taken right after a run can come back empty. Believing
     a log read over an exit code stranded a healthy revision at 0% traffic on
     2026-08-25. A run that FAILS must exit non-zero even when the log looks fine,
     and a run that SUCCEEDS must exit 0 even when no log is readable at all.
  2. THE JOB IS ALWAYS DELETED. A left-behind job is a standing execution surface
     with a service account attached — including on the failure path, which is
     exactly when a naive script returns early.
  3. SECRETS ARE MOUNTED AND ARGS SURVIVE. A job inherits NO env and NO secrets,
     and `--args` splits on COMMAS, so an argument containing one is silently
     shredded into separate argv entries.

The script is driven END TO END with `gcloud` stubbed and the assertions read the
recorded argv of the real calls. Asserting on the script's TEXT instead would
pass for a flag that is constructed and then never passed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ops" / "run_oneoff_job.sh"

GCLOUD_STUB = r"""#!/bin/sh
{
  first=1
  for a in "$@"; do
    if [ "$first" = 1 ]; then printf '%s' "$a"; first=0; else printf '\t%s' "$a"; fi
  done
  printf '\n'
} >> "$STUB_CALLS"

case "$1 $2" in
  "run jobs")
    case "$3" in
      create)
        [ "$STUB_JOB_CREATE_RC" = 0 ] || echo "stub: PERMISSION_DENIED on secret" >&2
        exit "$STUB_JOB_CREATE_RC" ;;
      # Without --wait this returns 0 as soon as the execution is CREATED rather
      # than when it FINISHES, so the exit code stops meaning "the script ran".
      execute)
        case "$*" in *"--wait"*) ;; *) echo "stub: execute without --wait" >&2; exit 9 ;; esac
        exit "$STUB_JOB_EXECUTE_RC" ;;
      delete) exit 0 ;;
    esac ;;
  # STUB_LOG_SEQ_DIR holds reads 1, 2, 3, ... served in turn, the last one repeating: Cloud
  # Logging filling in as ingestion catches up. Without it every read returns STUB_LOG_OUTPUT.
  "logging read")
    if [ -n "${STUB_LOG_SEQ_DIR-}" ]; then
      k=$(cat "$STUB_LOG_SEQ_DIR/counter" 2>/dev/null || echo 0); k=$((k + 1))
      echo "$k" > "$STUB_LOG_SEQ_DIR/counter"
      while [ "$k" -gt 1 ] && [ ! -e "$STUB_LOG_SEQ_DIR/$k" ]; do k=$((k - 1)); done
      [ "$(cat "$STUB_LOG_SEQ_DIR/$k")" = "STUB_READ_FAILS" ] && exit 1
      cat "$STUB_LOG_SEQ_DIR/$k"
    else
      printf '%s' "$STUB_LOG_OUTPUT"
    fi
    exit 0 ;;
esac
exit 0
"""

# The helper sleeps between log-read attempts; real sleeps would make the empty-log
# cases take 30s of pure wall clock. Each requested nap is recorded instead, so the
# tests can read the schedule the script would have kept.
SLEEP_STUB = '#!/bin/sh\necho "$1" >> "$STUB_SLEEPS"\nexit 0\n'


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, calls: list[list[str]]):
        self.proc, self.calls = proc, calls

    @property
    def rc(self) -> int:
        return self.proc.returncode

    @property
    def out(self) -> str:
        return self.proc.stdout

    @property
    def err(self) -> str:
        return self.proc.stderr

    def call(self, *prefix: str) -> list[str]:
        """The one recorded gcloud invocation starting with these argv items."""
        hits = [c for c in self.calls if c[: len(prefix)] == list(prefix)]
        assert len(hits) == 1, f"want exactly one {prefix!r} call, got {len(hits)}: {self.calls}"
        return hits[0]

    def has(self, *prefix: str) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)

    def flag(self, call: list[str], name: str) -> str:
        """Value of `--name=v` or `--name v`, whichever form was used."""
        for i, a in enumerate(call):
            if a.startswith(name + "="):
                return a.split("=", 1)[1]
            if a == name:
                return call[i + 1]
        raise AssertionError(f"{name} not passed: {call}")


def run(tmp_path: Path, args: list[str], **env_overrides: str) -> Run:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "gcloud").write_text(GCLOUD_STUB)
    (stub_dir / "gcloud").chmod(0o755)
    (stub_dir / "sleep").write_text(SLEEP_STUB)
    (stub_dir / "sleep").chmod(0o755)

    calls_file = tmp_path / "calls.tsv"
    calls_file.touch()
    sleeps_file = tmp_path / "sleeps.txt"
    sleeps_file.touch()

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "STUB_CALLS": str(calls_file),
        "STUB_SLEEPS": str(sleeps_file),
        "STUB_JOB_CREATE_RC": "0",
        "STUB_JOB_EXECUTE_RC": "0",
        "STUB_LOG_OUTPUT": "",
        **env_overrides,
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120
    )
    calls = [
        line.split("\t")
        for line in calls_file.read_text().splitlines()
        if line.strip()
    ]
    r = Run(proc, calls)
    r.sleeps = [int(x) for x in sleeps_file.read_text().split()]
    return r


def test_success_exits_zero_and_mounts_the_database_secret(tmp_path: Path) -> None:
    r = run(tmp_path, ["scripts/partner_settlement_dry_run.py"], STUB_LOG_OUTPUT="all good\n")

    assert r.rc == 0, r.err
    create = r.call("run", "jobs", "create")
    # A job inherits nothing; without this the script reads no DATABASE_URL and
    # fails looking like a database outage rather than a missing mount.
    assert r.flag(create, "--set-secrets") == "DATABASE_URL=DATABASE_URL:latest"
    assert r.flag(create, "--project") == "pivota-prod"
    assert r.flag(create, "--region") == "us-west1"
    assert r.flag(create, "--command") == "python"
    # In-VPC, or the private Cloud SQL IP is unreachable.
    assert "--vpc-egress" in create and "all-traffic" in create


def test_failure_propagates_the_exit_code_even_when_the_log_reads_clean(tmp_path: Path) -> None:
    """The log is DETAIL. The exit code is the VERDICT."""
    r = run(
        tmp_path,
        ["scripts/partner_settlement_dry_run.py"],
        STUB_JOB_EXECUTE_RC="1",
        STUB_LOG_OUTPUT="GRAND TOTAL net comp across partners: $0.00\n",
    )

    assert r.rc == 1
    assert "FAILED" in r.err
    # The reassuring log line must not be dressed up as a pass.
    assert "succeeded" not in r.err


def test_success_with_no_readable_log_is_still_a_pass(tmp_path: Path) -> None:
    """Ingestion lag is unbounded; an empty read is not a result."""
    r = run(tmp_path, ["scripts/partner_settlement_dry_run.py"], STUB_LOG_OUTPUT="")

    assert r.rc == 0
    assert "no log entries readable" in r.err
    assert "succeeded" in r.err


def test_the_job_is_deleted_on_the_failure_path(tmp_path: Path) -> None:
    """A left-behind job is a standing execution surface with a service account."""
    r = run(tmp_path, ["scripts/partner_settlement_dry_run.py"], STUB_JOB_EXECUTE_RC="1")

    assert r.rc == 1
    assert r.has("run", "jobs", "delete")
    created = r.flag(r.call("run", "jobs", "create"), "--project")  # forces exactly one create
    assert created == "pivota-prod"
    # Same job name created and deleted — not a delete of some other job.
    assert r.call("run", "jobs", "create")[3] == r.call("run", "jobs", "delete")[3]


def test_the_job_is_deleted_when_create_itself_fails(tmp_path: Path) -> None:
    r = run(tmp_path, ["scripts/x.py"], STUB_JOB_CREATE_RC="1")

    assert r.rc != 0
    assert r.has("run", "jobs", "delete")
    # A failed create must not be reported as a job that ran and passed.
    assert not r.has("run", "jobs", "execute")


def test_args_are_passed_with_a_delimiter_absent_from_the_payload(tmp_path: Path) -> None:
    """`--args` splits on COMMAS; a comma-bearing argument would be shredded."""
    r = run(tmp_path, ["-c", "import json,sys; print(sys.argv)"])

    assert r.rc == 0
    raw = r.flag(r.call("run", "jobs", "create"), "--args")
    delim = raw[1]
    assert raw.startswith(f"^{delim}^"), raw
    payload = raw[3:]
    assert payload.split(delim) == ["-c", "import json,sys; print(sys.argv)"]
    # The chosen delimiter must not occur inside the values, or the split is
    # silently wrong rather than an error.
    assert delim != ","
    assert all(delim not in part for part in payload.split(delim))


def test_delimiter_moves_off_a_character_the_payload_contains(tmp_path: Path) -> None:
    """The default `|` is a poor choice for Python using regex alternation."""
    r = run(tmp_path, ["-c", "import re; re.match('a|b', 'a')"])

    assert r.rc == 0
    raw = r.flag(r.call("run", "jobs", "create"), "--args")
    assert raw[1] != "|", raw
    assert raw.split("^", 2)[2].split(raw[1]) == ["-c", "import re; re.match('a|b', 'a')"]


def test_refuses_rather_than_splitting_wrongly_when_no_delimiter_is_safe(tmp_path: Path) -> None:
    every_candidate = "|@#%~+!;:?"
    r = run(tmp_path, ["-c", every_candidate])

    assert r.rc == 2
    assert "no safe --args delimiter" in r.err
    assert not r.has("run", "jobs", "create")


def test_log_lines_are_replayed_in_the_order_the_script_printed_them(tmp_path: Path) -> None:
    """Cloud Logging returns newest-first, which reverses a report."""
    r = run(
        tmp_path,
        ["scripts/x.py"],
        STUB_LOG_OUTPUT="GRAND TOTAL: $0.00\npartners : 0\nPARTNER SETTLEMENT DRY RUN\n",
    )

    assert r.rc == 0
    lines = [ln for ln in r.out.splitlines() if ln.strip()]
    assert lines == ["PARTNER SETTLEMENT DRY RUN", "partners : 0", "GRAND TOTAL: $0.00"]


def test_secrets_are_overridable_for_scripts_that_need_more_than_the_database(tmp_path: Path) -> None:
    r = run(
        tmp_path,
        ["scripts/x.py"],
        SECRETS="DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest",
    )

    assert r.rc == 0
    assert r.flag(r.call("run", "jobs", "create"), "--set-secrets") == (
        "DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest"
    )


def test_no_arguments_is_refused_rather_than_running_a_bare_interpreter(tmp_path: Path) -> None:
    r = run(tmp_path, [])

    assert r.rc == 2
    assert not r.has("run", "jobs", "create")


# ---------------------------------------------------------------------------
# Coverage for the `gcloud logging read` invocation and the create-time flags.
#
# The first version of this file asserted five flags on `create` and NOTHING on
# `logging read`, so ten separate one-line mutations of the script survived the
# whole suite — including dropping `--max-retries 0` (Cloud Run then defaults to
# THREE retries, so a failing --apply re-runs up to four times) and pointing the
# log filter at a different job name (you get someone else's output as yours).
# ---------------------------------------------------------------------------


def test_the_log_read_is_scoped_to_this_job_and_to_stdout(tmp_path: Path) -> None:
    r = run(tmp_path, ["scripts/x.py"], STUB_LOG_OUTPUT="line\n")

    assert r.rc == 0
    # The read repeats until the log is complete; EVERY read must carry the scoping.
    reads = [c for c in r.calls if c[:2] == ["logging", "read"]]
    assert reads and all(c == reads[0] for c in reads), reads
    read = reads[0]
    filt = read[2]
    # Someone else's job output presented as yours is worse than no output.
    job = r.call("run", "jobs", "create")[3]
    assert f'job_name="{job}"' in filt
    # cloudaudit system_event entries carry no textPayload and render as blank
    # lines interleaved through the report.
    assert "logName" in filt and "stdout" in filt
    # A structured logger writes to jsonPayload, not textPayload.
    fmt = r.flag(read, "--format")
    assert "textPayload" in fmt and "jsonPayload" in fmt
    # Newest-first means a low --limit silently returns the LAST N lines and
    # presents them as the whole run. Keep the cap far above a real script's
    # output rather than near it.
    assert int(r.flag(read, "--limit")) >= 5000


def test_the_log_window_is_wider_than_the_task_can_run(tmp_path: Path) -> None:
    """--freshness=10m against a 600s task timeout drops the start of a long run."""
    r = run(tmp_path, ["scripts/x.py"], STUB_LOG_OUTPUT="line\n", TASK_TIMEOUT="3600s")

    assert r.rc == 0
    reads = [c for c in r.calls if c[:2] == ["logging", "read"]]
    freshness = r.flag(reads[0], "--freshness")
    assert int(freshness.rstrip("s")) > 3600


def test_retries_run_lost_to_ingestion_lag_are_retried(tmp_path: Path) -> None:
    """The header promises the read happens 'behind a retry'. Prove it does."""
    r = run(tmp_path, ["scripts/x.py"], STUB_LOG_OUTPUT="", STUB_JOB_EXECUTE_RC="0")

    assert r.rc == 0
    reads = [c for c in r.calls if c[:2] == ["logging", "read"]]
    # Every 5s inside the 30s budget, the last read landing ON the deadline (the old
    # loop slept a final 5s after its sixth read and never looked again).
    assert len(reads) == 7, f"want 7 attempts, got {len(reads)}"
    assert r.sleeps == [5] * 6


def test_the_job_does_not_silently_rerun_a_failing_apply(tmp_path: Path) -> None:
    """Cloud Run defaults --max-retries to 3; an --apply script would run 4x."""
    r = run(tmp_path, ["scripts/x.py", "--apply"])

    assert r.flag(r.call("run", "jobs", "create"), "--max-retries") == "0"


def test_create_carries_the_identity_image_and_vpc_route(tmp_path: Path) -> None:
    r = run(tmp_path, ["scripts/x.py"])
    create = r.call("run", "jobs", "create")

    # The default compute SA has no secretAccessor on DATABASE_URL.
    assert r.flag(create, "--service-account") == "sa-worker@pivota-prod.iam.gserviceaccount.com"
    assert r.flag(create, "--image").endswith("/pivota/backend:latest")
    # Cloud SQL is on a private IP; without the VPC route there is no path to it.
    # Adjacency, not mere membership — `--vpc-egress` must carry THIS value.
    assert r.flag(create, "--vpc-egress") == "all-traffic"
    assert r.flag(create, "--network") == "default"
    assert r.flag(create, "--subnet") == "default"
    assert r.flag(create, "--task-timeout") == "600s"


def test_the_egress_subnet_is_overridable_and_defaults_to_the_payment_one(tmp_path: Path) -> None:
    """MUTANT: hardcode `--subnet default` again.

    prod has TWO NAT addresses. The `default` subnet egresses from 8.231.167.230, the address
    payment partners allowlist; `pivota-crawl` egresses from 34.82.199.35. NAT port exhaustion
    is per-IP, so a burst crawl on the payment address can starve payment egress even with
    clean reputation, which is why anything fetching from a merchant must move.

    Asserting on the recorded argv, not on the script's text: a flag that is constructed and
    then never passed would satisfy a grep and still leave every crawl on the payment IP.
    """
    # One directory per run: `run()` records argv into <tmp_path>/calls.tsv, and `call()`
    # demands exactly one matching invocation, so two runs sharing a directory collide.
    def at(name: str, **env: str):
        d = tmp_path / name
        d.mkdir()
        r = run(d, ["scripts/x.py"], **env)
        return r, r.call("run", "jobs", "create")

    plain, create = at("plain")
    assert plain.flag(create, "--subnet") == "default", (
        "the default must not move — every existing caller depends on it")
    assert plain.flag(create, "--network") == "default"

    crawl, create_crawl = at("crawl", SUBNET="pivota-crawl")
    assert crawl.flag(create_crawl, "--subnet") == "pivota-crawl"
    assert crawl.flag(create_crawl, "--network") == "default", (
        "SUBNET alone must not move the network")
    # The route to Cloud SQL must survive the move, or the job cannot reach the database.
    assert crawl.flag(create_crawl, "--vpc-egress") == "all-traffic"

    both, create_both = at("both", NETWORK="vpc-prod", SUBNET="pivota-crawl")
    assert both.flag(create_both, "--network") == "vpc-prod"
    assert both.flag(create_both, "--subnet") == "pivota-crawl"


def test_the_database_guardrails_are_resupplied(tmp_path: Path) -> None:
    """A job inherits nothing — including the timeouts that bound a bad query.

    db/database.py defaults BOTH of these to 0.0, which means OFF, so a job that
    did not re-supply them would run against production with statement timeouts
    disabled.
    """
    r = run(tmp_path, ["scripts/x.py"])
    env = r.flag(r.call("run", "jobs", "create"), "--set-env-vars")

    assert "DB_STATEMENT_TIMEOUT_SECONDS=30" in env
    assert "DB_COMMAND_TIMEOUT_SECONDS=600" in env


def test_env_vars_are_overridable(tmp_path: Path) -> None:
    r = run(tmp_path, ["scripts/x.py"], ENV_VARS="FOO=bar")

    assert r.flag(r.call("run", "jobs", "create"), "--set-env-vars") == "FOO=bar"


def test_help_prints_usage_without_touching_production(tmp_path: Path) -> None:
    """`--help` used to become the PAYLOAD: it mounted the production database
    secret to print Python's help, then reported success."""
    for flag in ("--help", "-h"):
        r = run(tmp_path, [flag])
        assert r.rc == 2, flag
        assert not r.has("run", "jobs", "create"), flag
        assert "Usage:" in r.err, flag


def test_an_empty_argument_is_refused_not_silently_dropped(tmp_path: Path) -> None:
    """gcloud drops a leading/trailing empty value and SHIFTS argv after it.

    `--merchant-id "$MID"` with MID unset would otherwise ship
    `['scripts/x.py', '--merchant-id']` — a different command than the one
    written down, run against production.
    """
    for args in (["", "scripts/x.py"], ["scripts/x.py", "--merchant-id", ""]):
        r = run(tmp_path, args)
        assert r.rc == 2, args
        assert "empty" in r.err, args
        assert not r.has("run", "jobs", "create"), args


def test_a_failed_create_says_why(tmp_path: Path) -> None:
    """The exit code says THAT it failed; only gcloud's stderr says WHY."""
    r = run(tmp_path, ["scripts/x.py"], STUB_JOB_CREATE_RC="1")

    assert r.rc != 0
    assert "PERMISSION_DENIED" in r.err
    assert not r.has("run", "jobs", "execute")


# ---------------------------------------------------------------------------
# The first non-empty read can be PARTIAL.
#
# 2026-11-24 ~17:51Z, the migration 296 verification read (job oneoff-10927-24436):
# the fetched job.log lacked the DEFS and PROBES lines, which were in Cloud Logging a
# minute later. The loop stopped at the first non-empty read. Exit codes were right;
# the risk is an operator or a waiter reading a truncated verdict line as the whole.
# Reads below are NEWEST-FIRST, as Cloud Logging returns them.
# ---------------------------------------------------------------------------


def seq(tmp_path: Path, *reads: str) -> str:
    d = tmp_path / "logseq"
    d.mkdir()
    for i, body in enumerate(reads, 1):
        (d / str(i)).write_text(body)
    return str(d)


def body_lines(r: Run) -> list[str]:
    return [ln for ln in r.out.splitlines() if ln.strip()]


def test_a_partial_first_read_is_not_taken_as_the_whole_log(tmp_path: Path) -> None:
    """MUTANT: break on the first non-empty read again. The DEFS and PROBES lines vanish."""
    partial = "HEADER mig 296\n"
    full = "VERIFY_DONE\nPROBES ok\nDEFS ok\nHEADER mig 296\n"
    r = run(tmp_path, ["scripts/x.py"], STUB_LOG_SEQ_DIR=seq(tmp_path, partial, full))

    assert r.rc == 0
    assert body_lines(r) == ["HEADER mig 296", "DEFS ok", "PROBES ok", "VERIFY_DONE"]
    assert "log complete" in r.err and "WARNING" not in r.err
    # read 1 partial -> 15s -> read 2 full (4 lines) -> 15s -> read 3 still 4 lines: stable.
    assert r.sleeps == [15, 15]
    assert len([c for c in r.calls if c[:2] == ["logging", "read"]]) == 3


def test_stability_needs_reads_at_least_15s_apart(tmp_path: Path) -> None:
    """Two reads 5s apart can agree while the tail is still in flight."""
    r = run(tmp_path, ["scripts/x.py"], STUB_LOG_OUTPUT="a\nb\n")

    assert r.rc == 0
    assert r.sleeps == [15]
    assert all(n >= 15 for n in r.sleeps)
    assert "two reads 15s apart both held 2 lines" in r.err


def test_a_log_still_growing_at_the_deadline_is_printed_with_a_warning(tmp_path: Path) -> None:
    r = run(
        tmp_path,
        ["scripts/x.py"],
        STUB_LOG_SEQ_DIR=seq(tmp_path, "a\n", "b\na\n", "c\nb\na\n", "d\nc\nb\na\n"),
    )

    assert r.rc == 0, "an incomplete log must not change the verdict"
    # Reads at 0s, 15s, 30s; nothing after the deadline.
    assert r.sleeps == [15, 15] and sum(r.sleeps) == 30
    assert body_lines(r) == ["a", "b", "c"], "the newest read is the one printed"
    assert "WARNING" in r.err and "INCOMPLETE" in r.err
    assert "succeeded" in r.err


def test_the_last_sleep_is_clamped_so_a_read_lands_on_the_deadline(tmp_path: Path) -> None:
    """First lines at 20s: a 15s nap would overshoot, so it is cut to 10s and the
    final read happens at 30s. 10s apart is too close to call it complete."""
    r = run(
        tmp_path,
        ["scripts/x.py"],
        STUB_LOG_SEQ_DIR=seq(tmp_path, "", "", "", "", "a\n", "a\n"),
    )

    assert r.sleeps == [5, 5, 5, 5, 10]
    assert body_lines(r) == ["a"]
    assert "WARNING" in r.err


def test_done_marker_ends_the_wait_as_soon_as_a_line_matches(tmp_path: Path) -> None:
    partial = "DEFS ok\nHEADER\n"
    full = "APPLY_DONE\nPROBES ok\nDEFS ok\nHEADER\n"
    r = run(
        tmp_path,
        ["scripts/x.py"],
        DONE_MARKER="_DONE$|STOP",
        STUB_LOG_SEQ_DIR=seq(tmp_path, partial, partial, full),
    )

    assert r.rc == 0
    assert body_lines(r) == ["HEADER", "DEFS ok", "PROBES ok", "APPLY_DONE"]
    # With a marker there is no stability gap to wait out: poll every 5s, stop on the match.
    assert r.sleeps == [5, 5]
    assert "matched DONE_MARKER" in r.err and "WARNING" not in r.err


def test_done_marker_anchors_at_end_of_line_despite_the_value_tab(tmp_path: Path) -> None:
    """value(textPayload,jsonPayload.message) emits a trailing tab for the empty half,
    so `_DONE$` against the raw read could never match."""
    r = run(tmp_path, ["scripts/x.py"], DONE_MARKER="_DONE$", STUB_LOG_OUTPUT="VERIFY_DONE\t\nHEADER\t\n")

    assert r.sleeps == []
    assert "matched DONE_MARKER" in r.err


def test_done_marker_overrides_stability(tmp_path: Path) -> None:
    """A stable count is not completion when the caller named the line that is.
    MUTANT: accept stability in marker mode -> stops at 5s on two equal partial reads."""
    r = run(tmp_path, ["scripts/x.py"], DONE_MARKER="_DONE$", STUB_LOG_OUTPUT="HEADER\nDEFS ok\n")

    assert r.rc == 0
    assert sum(r.sleeps) == 30 and r.sleeps == [5] * 6
    assert "WARNING" in r.err and "DONE_MARKER" in r.err
    assert body_lines(r) == ["DEFS ok", "HEADER"]


def test_done_marker_missing_on_a_failed_job_keeps_the_failure_verdict(tmp_path: Path) -> None:
    r = run(
        tmp_path,
        ["scripts/x.py"],
        DONE_MARKER="_DONE$",
        STUB_JOB_EXECUTE_RC="1",
        STUB_LOG_OUTPUT="Traceback\n",
    )

    assert r.rc == 1
    assert "WARNING" in r.err and "FAILED" in r.err and "succeeded" not in r.err


def test_a_failed_read_after_a_good_one_keeps_the_good_one_and_restarts_stability(tmp_path: Path) -> None:
    """A blip must neither blank the log nor count as agreement with the read before it."""
    r = run(
        tmp_path,
        ["scripts/x.py"],
        STUB_LOG_SEQ_DIR=seq(tmp_path, "b\na\n", "STUB_READ_FAILS", "b\na\n", "b\na\n"),
    )

    assert r.rc == 0
    assert body_lines(r) == ["a", "b"]
    # 0s good (2) -> 15s FAILED -> 30s good (2): the 0s read is not compared across the blip.
    assert r.sleeps == [15, 15]
    assert "WARNING" in r.err


def test_log_wait_seconds_extends_the_deadline(tmp_path: Path) -> None:
    r = run(
        tmp_path,
        ["scripts/x.py"],
        LOG_WAIT_SECONDS="90",
        STUB_LOG_SEQ_DIR=seq(tmp_path, "a\n", "b\na\n", "c\nb\na\n", "c\nb\na\n"),
    )

    assert r.sleeps == [15, 15, 15]
    assert body_lines(r) == ["a", "b", "c"]
    assert "log complete" in r.err


@pytest.mark.parametrize(
    "env, needle",
    [
        ({"DONE_MARKER": "(unclosed"}, "not a valid extended regex"),
        ({"LOG_WAIT_SECONDS": "30s"}, "not a whole number"),
        ({"LOG_WAIT_SECONDS": "-5"}, "not a whole number"),
    ],
)
def test_bad_wait_settings_are_refused_before_the_job_is_created(tmp_path: Path, env, needle) -> None:
    """Found out only after the read, a bad setting would mean a production job ran
    and its log could never be judged complete."""
    r = run(tmp_path, ["scripts/x.py"], **env)

    assert r.rc == 2
    assert needle in r.err
    assert not r.has("run", "jobs", "create")
