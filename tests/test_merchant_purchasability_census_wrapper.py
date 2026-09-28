"""`scripts/ops/merchant_purchasability_census.sh`: the census's CPU gate and its job.

The wrapper is the only thing standing between an operator and a full seed scan on the 2-vCPU
prod primary, so its refusals are driven here against the REAL script, with `curl` and `gcloud`
replaced on PATH by fakes that record every call. Nothing here reaches Google Cloud.
"""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ops" / "merchant_purchasability_census.sh"
CENSUS_SCRIPT = REPO / "scripts" / "merchant_purchasability_census.py"

FAKE_CURL = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf 'curl %s\\n' "$*" >> "$CALLS"
    printf '%s' "$CPU_JSON"
    """
)
FAKE_GCLOUD = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf 'gcloud %s\\n' "$*" >> "$CALLS"
    case "$1 $2" in
      "auth print-access-token") echo token ;;
      "logging read") printf '%s' "$JOB_LOG" ;;
    esac
    exit 0
    """
)


def _cpu(*points):
    return json.dumps({"timeSeries": [{"points": [{"value": {"doubleValue": p}} for p in points]}]}
                      if points else {})


def _job_log():
    import importlib.util

    spec = importlib.util.spec_from_file_location("mp_census", CENSUS_SCRIPT)
    census = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(census)
    row = {"kind": "row", "i": 0, "n": 1, "host": "a.example", "market": "US",
           "state": "never_checked", "lanes": ["cart-mint"], "cart_mint_seeds": 2,
           "verdict": None, "checked_at": None}
    summary = {"kind": "summary", "n": 1, "vantage": "worker", "population_counts": {},
               "cart_mint_lane": {}, "keys": {}, "cart_mint_keys": {}, "cart_mint_seeds": {},
               "hosts_best_state": {}}
    return census._fence(summary) + "\n" + census._fence(row) + "\n"


def _run(tmp_path: Path, cpu_json: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("gcloud", FAKE_GCLOUD)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.write_text("")
    proc = subprocess.run(
        ["bash", str(SCRIPT), "b" * 40, str(tmp_path / "out")],
        capture_output=True, text=True, cwd=str(REPO),
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls),
             "CPU_JSON": cpu_json, "JOB_LOG": _job_log(), "CENSUS_LOG_WAIT_S": "0"},
    )
    return proc, calls.read_text().splitlines()


@pytest.mark.parametrize("cpu_json", [_cpu(0.2, 0.36, 0.1), _cpu(0.35), _cpu()])
def test_a_busy_or_unreadable_primary_refuses_before_any_job_exists(tmp_path, cpu_json):
    proc, calls = _run(tmp_path, cpu_json)
    assert proc.returncode == 2, proc.stderr
    assert not any(c.startswith("gcloud run jobs") for c in calls), calls


def test_a_quiet_primary_runs_one_read_only_census_job_and_decodes_it(tmp_path):
    proc, calls = _run(tmp_path, _cpu(0.12, 0.2))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    create = [c for c in calls if c.startswith("gcloud run jobs create")]
    assert len(create) == 1, calls
    assert "backend:" + "b" * 40 in create[0]
    assert "scripts/merchant_purchasability_census.py" in create[0]
    assert "DB_POOL_MAX_SIZE=1" in create[0] and "DB_STATEMENT_TIMEOUT_SECONDS=30" in create[0]
    assert "--subnet default" in create[0], "it reads the database only; it contacts no merchant"
    assert "a.example" in proc.stdout and "never_checked" in proc.stdout
    assert any(c.startswith("gcloud run jobs delete") for c in calls), "no job is left behind"
