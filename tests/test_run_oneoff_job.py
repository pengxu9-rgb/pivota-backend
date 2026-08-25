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
      create) exit "$STUB_JOB_CREATE_RC" ;;
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
    stub_dir.mkdir()
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
    assert "exit 0" in r.err


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
