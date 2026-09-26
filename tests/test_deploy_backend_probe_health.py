"""`probe_health` in infra/gcp/deploy_backend.sh — the gate that decides whether a freshly
built Cloud Run revision may take production traffic.

WHY THIS EXISTS. On 2026-08-25 a deploy of a HEALTHY revision was refused. The in-VPC probe
job really did return 200 (`PROBE_STATUS=200`, logged at 02:25:10.788Z) and the revision was
Ready/ContainerHealthy — but the probe's verdict was read back by SCRAPING Cloud Logging, the
entry had not become queryable inside the poll window, the read came back empty, and the
function returned `000`. Prod stayed on the old revision and two merged fixes went unshipped.

The verdict now comes from the probe job's EXIT CODE, which `--wait` already hands back.
These tests pin that: a passing job must promote even when the log read yields NOTHING, and a
failing job must refuse even when it yields something.

The function is extracted rather than sourced — deploy_backend.sh performs a real deployment
at import time.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "gcp" / "deploy_backend.sh"


def _no_proxy_env() -> dict:
    """urlopen honours http_proxy even for 127.0.0.1 — measured, an inherited proxy turns the
    200 row from pass to fail. Neutralise it so these assertions describe the payload."""
    return {**os.environ, "http_proxy": "", "https_proxy": "", "no_proxy": "*", "NO_PROXY": "*"}


def _probe_health_source() -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index("probe_health(){")
    end = src.index("\n}\n", start) + len("\n}\n")
    body = src[start:end]
    # Guard both ENDS of the function: the execute call sits mid-body, so on its own it would
    # accept a block truncated before the verdict logic.
    for marker in ("run jobs execute", "probe_rc", 'echo "${out:-000}"'):
        assert marker in body, f"extracted the wrong block - missing {marker!r}"
    return body


def _run_probe(
    tmp_path: Path,
    *,
    execute_rc: int = 0,
    log_output: str = "",
    create_rc: int = 0,
    curl_out: str = "404",
) -> str:
    """Run the real probe_health with gcloud/curl/sleep stubbed. Returns its stdout.

    The gcloud stub is STRICT about the flags the gate's correctness depends on, because a
    permissive stub silently blesses mutations of them. Measured: with a stub that ignored
    argv, deleting `--wait` from `jobs execute` — which makes the call return 0 the moment the
    execution is CREATED, so every deploy reports healthy and the gate is fully open — passed
    every test in this file.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "job_name"

    (bin_dir / "gcloud").write_text(
        '#!/bin/sh\n'
        'case "$*" in\n'
        '  *"jobs create"*)\n'
        # The probe is only meaningful if it actually runs the python one-liner.
        '    case "$*" in *"--command python"*) ;; *) echo "stub: create without --command python" >&2; exit 9 ;; esac\n'
        "    case \"$*\" in *'--args=^|^-c|'*) ;; *) echo 'stub: create without the ^|^-c| payload' >&2; exit 9 ;; esac\n"
        '    prev=""; for a in "$@"; do [ "$prev" = "create" ] && { printf %s "$a" > "$STUB_STATE"; break; }; prev="$a"; done\n'
        '    exit "$STUB_CREATE_RC" ;;\n'
        '  *"jobs execute"*)\n'
        # Without --wait this returns 0 as soon as the execution is created, not when it passes.
        '    case "$*" in *"--wait"*) ;; *) echo "stub: execute without --wait" >&2; exit 9 ;; esac\n'
        '    job=$(cat "$STUB_STATE" 2>/dev/null)\n'
        '    case "$*" in *"$job"*) ;; *) echo "stub: executed a different job" >&2; exit 9 ;; esac\n'
        '    exit "$STUB_EXECUTE_RC" ;;\n'
        '  *"logging read"*)\n'
        '    job=$(cat "$STUB_STATE" 2>/dev/null)\n'
        '    case "$*" in *"$job"*) ;; *) echo "stub: read logs for a different job" >&2; exit 9 ;; esac\n'
        '    printf %s "$STUB_LOG_OUTPUT"; exit 0 ;;\n'
        '  *"jobs delete"*) exit 0 ;;\n'
        'esac\nexit 0\n'
    )
    (bin_dir / "curl").write_text('#!/bin/sh\nprintf %s "$STUB_CURL_OUT"\nexit 0\n')
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)

    harness = tmp_path / "harness.sh"
    harness.write_text(
        # The REAL script runs `set -euo pipefail` (deploy_backend.sh:11). Dropping `-e` here
        # would hide the very failure this file exists to prevent: with `-e`, an empty log read
        # makes the scrape pipeline exit 1 (pipefail), `out=$(...)` adopts it, and the function
        # dies mid-way. In production the single call site happens to be a command substitution,
        # which exempts the function body from `-e` — an accident of ONE caller, not a property
        # worth relying on, so the harness holds the function to the stricter contract.
        "set -euo pipefail\n"
        f'GCLOUD="{bin_dir}/gcloud"\n'
        'REGION=us-west1\nPROJECT=test-project\nAUTH_ARGS=()\n'
        f"{_probe_health_source()}\n"
        'probe_health "https://candidate.example.invalid/health"\n'
    )
    proc = subprocess.run(
        ["bash", str(harness)],
        capture_output=True, text=True, timeout=60,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
            "STUB_STATE": str(state), "STUB_CREATE_RC": str(create_rc),
            "STUB_EXECUTE_RC": str(execute_rc), "STUB_LOG_OUTPUT": log_output,
            "STUB_CURL_OUT": curl_out,
        },
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"
    # The caller does `CODE=$(probe_health ...)` and compares the WHOLE captured string to 200,
    # so ANY extra line on stdout breaks the gate. Taking splitlines()[-1] here would hide
    # exactly that: measured, an `echo noise` before `echo 200` left every test green, while the
    # real caller refused a healthy revision — the 2026-08-25 incident verbatim.
    lines = proc.stdout.splitlines()
    assert len(lines) == 1, f"probe_health must emit exactly one stdout line, got {proc.stdout!r}"
    return lines[0].strip()


def test_a_passing_probe_promotes_even_when_cloud_logging_has_not_caught_up(tmp_path):
    """THE REGRESSION. Job exited 0, log read empty — this is the exact 2026-08-25 shape.

    Before the fix this returned `000` and stranded a healthy revision.
    """
    assert _run_probe(tmp_path, execute_rc=0, log_output="") == "200"


def test_a_failed_job_create_never_promotes(tmp_path):
    """`set -e` does not fire inside this function, so an unchecked `jobs create` failure was
    entirely silent — "exit 0 means healthy" would have rested on a job that never existed.

    It was only safe by luck (the execute afterwards also failed). Pinned so it stays checked:
    a create failure must refuse even if the execute stub would have reported success.
    """
    assert _run_probe(tmp_path, execute_rc=0, log_output="PROBE_STATUS=200\n", create_rc=1) != "200"


def test_a_passing_probe_promotes_when_the_log_line_is_available(tmp_path):
    assert _run_probe(tmp_path, execute_rc=0, log_output="PROBE_STATUS=200\n") == "200"


def test_a_failing_probe_refuses_even_when_a_log_line_exists(tmp_path):
    """The log scrape must never be able to talk the gate INTO a promotion either."""
    assert _run_probe(tmp_path, execute_rc=1, log_output="PROBE_STATUS=200\n") == "000"


def test_a_failing_probe_reports_the_status_it_could_recover(tmp_path):
    assert _run_probe(tmp_path, execute_rc=1, log_output="PROBE_STATUS=503\n") == "503"


def test_a_failing_probe_with_no_log_line_reports_000(tmp_path):
    assert _run_probe(tmp_path, execute_rc=1, log_output="") == "000"


def test_a_disagreeing_log_line_cannot_downgrade_a_passing_probe(tmp_path):
    """The other direction of the same invariant, and the incident's actual direction.

    The suite already pins that the scrape cannot talk the gate INTO a promotion. This pins
    that it cannot talk it OUT of one — a stale or mismatched PROBE_STATUS must not refuse a
    revision whose probe exited 0.
    """
    assert _run_probe(tmp_path, execute_rc=0, log_output="PROBE_STATUS=503\n") == "200"


def test_a_probe_job_that_is_never_waited_on_cannot_promote(tmp_path):
    """`--wait` is the load-bearing word in this whole change.

    Without it `gcloud run jobs execute` returns 0 as soon as the execution is CREATED rather
    than when it PASSES, so the exit code stops meaning "healthy" and the gate opens for every
    deploy. The stub refuses an execute that lacks it; this asserts the consequence.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    execute = [ln for ln in src.splitlines() if "run jobs execute" in ln]
    assert execute, "could not find the probe's execute call"
    assert all("--wait" in ln for ln in execute), (
        "the in-VPC probe must be waited on; without --wait its exit code says only that the "
        "execution was created"
    )


@pytest.mark.parametrize(
    "curl_out,expected",
    [
        ("200", "200"),   # a real application 200 - short-circuits, no job needed
        ("500", "500"),   # a real application status - trusted, and refuses
        ("403", "000"),   # IAM/ingress, not the app - must fall through to the in-VPC probe
        ("404", "000"),   # ditto (this is what production actually returns)
        ("000", "000"),   # a FAILED transfer - must fall through, never be trusted as a status
        ("", "000"),      # empty capture - the `${code:-000}` guard
    ],
)
def test_the_direct_probe_only_short_circuits_on_a_real_application_status(
    tmp_path, curl_out, expected
):
    """Only one of these six arms was exercised before, so most of the `case` block was free to
    change unnoticed.

    Every row runs with a FAILING in-VPC probe on purpose. That is what makes short-circuit and
    fall-through distinguishable: with a passing probe both end at 200 and the assertion proves
    nothing — measured, that let `code="${code:-000}"` become `:-200` with every test green,
    which is the "000000" bug this file's own comment was written about.
    """
    assert _run_probe(tmp_path, execute_rc=1, log_output="", curl_out=curl_out) == expected


@pytest.mark.parametrize(
    "status,should_promote",
    [(200, True), (201, False), (204, False), (301, False), (302, False), (404, False), (500, False)],
)
def test_the_probe_payload_exits_zero_only_for_an_exact_200(tmp_path, status, should_promote):
    """The exit code IS the verdict, so pin what the payload does with each status.

    Extracted from the script itself — a retyped copy would let the two drift, and this
    assertion is the whole foundation of the gate.
    """
    import http.server
    import socket
    import sys
    import threading

    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'--args="\^\|\^-c\|(.*?)"\s*\\\n', src, re.S)
    assert m, "could not extract the in-VPC probe payload from deploy_backend.sh"
    payload = m.group(1)
    assert "urlopen" in payload and "sys.exit" in payload

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # A LIVE 200 behind the redirect, deliberately. Pointing it at a dead port would make
            # the row pass because the target was unreachable, proving nothing about 3xx — and
            # urlopen FOLLOWS redirects and reports the final status, so before the geturl()
            # check a /health redirecting to a 200 login page promoted the revision.
            if self.path == "/redirected":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            self.send_response(status)
            if status in (301, 302):
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/redirected")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/health"
        rc = subprocess.run(
            [sys.executable, "-c", payload.replace("$url", url)],
            capture_output=True, timeout=60, env=_no_proxy_env(),
        ).returncode
    finally:
        server.shutdown()
    assert (rc == 0) is should_promote, f"HTTP {status} -> exit {rc}"


def test_an_unreachable_candidate_never_promotes(tmp_path):
    import socket
    import sys

    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'--args="\^\|\^-c\|(.*?)"\s*\\\n', src, re.S)
    assert m, "could not extract the in-VPC probe payload from deploy_backend.sh"
    payload = m.group(1)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    rc = subprocess.run(
        [sys.executable, "-c", payload.replace("$url", f"http://127.0.0.1:{port}/health")],
        capture_output=True, timeout=60, env=_no_proxy_env(),
    ).returncode
    assert rc != 0
