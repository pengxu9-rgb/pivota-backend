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
  "logging read") printf '%s' "$STUB_LOG_OUTPUT"; exit 0 ;;
esac
exit 0
"""

# The helper sleeps 5s between log-read attempts; six of those would make the
# empty-log cases take 30s of pure wall clock.
SLEEP_STUB = "#!/bin/sh\nexit 0\n"


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

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "STUB_CALLS": str(calls_file),
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
    return Run(proc, calls)


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
    read = r.call("logging", "read")
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
    freshness = r.flag(r.call("logging", "read"), "--freshness")
    assert int(freshness.rstrip("s")) > 3600


def test_retries_run_lost_to_ingestion_lag_are_retried(tmp_path: Path) -> None:
    """The header promises the read happens 'behind a retry'. Prove it does."""
    r = run(tmp_path, ["scripts/x.py"], STUB_LOG_OUTPUT="", STUB_JOB_EXECUTE_RC="0")

    assert r.rc == 0
    reads = [c for c in r.calls if c[:2] == ["logging", "read"]]
    assert len(reads) == 6, f"want 6 attempts, got {len(reads)}"


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
