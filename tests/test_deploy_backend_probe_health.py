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

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "gcp" / "deploy_backend.sh"


def _probe_health_source() -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index("probe_health(){")
    end = src.index("\n}\n", start) + len("\n}\n")
    body = src[start:end]
    assert "run jobs execute" in body, "extracted the wrong block"
    return body


def _run_probe(tmp_path: Path, *, execute_rc: int, log_output: str) -> str:
    """Run the real probe_health with gcloud/curl/sleep stubbed. Returns what it echoed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    (bin_dir / "gcloud").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"jobs create"*)  exit 0 ;;\n'
        f'  *"jobs execute"*) exit {execute_rc} ;;\n'
        '  *"jobs delete"*)  exit 0 ;;\n'
        f'  *"logging read"*) printf %s "{log_output}"; exit 0 ;;\n'
        "esac\nexit 0\n"
    )
    # The direct probe must fall through to the in-VPC path: 404 is the ingress-blocked arm.
    (bin_dir / "curl").write_text('#!/bin/sh\nprintf 404\nexit 0\n')
    # Keep the poll loop from actually sleeping.
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)

    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -uo pipefail\n"
        f'GCLOUD="{bin_dir}/gcloud"\n'
        'REGION=us-west1\nPROJECT=test-project\nAUTH_ARGS=()\n'
        f"{_probe_health_source()}\n"
        'probe_health "https://candidate.example.invalid/health"\n'
    )
    proc = subprocess.run(
        ["bash", str(harness)],
        capture_output=True, text=True, timeout=60,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"
    return proc.stdout.strip().splitlines()[-1].strip()


def test_a_passing_probe_promotes_even_when_cloud_logging_has_not_caught_up(tmp_path):
    """THE REGRESSION. Job exited 0, log read empty — this is the exact 2026-08-25 shape.

    Before the fix this returned `000` and stranded a healthy revision.
    """
    assert _run_probe(tmp_path, execute_rc=0, log_output="") == "200"


def test_a_passing_probe_promotes_when_the_log_line_is_available(tmp_path):
    assert _run_probe(tmp_path, execute_rc=0, log_output="PROBE_STATUS=200\n") == "200"


def test_a_failing_probe_refuses_even_when_a_log_line_exists(tmp_path):
    """The log scrape must never be able to talk the gate INTO a promotion either."""
    assert _run_probe(tmp_path, execute_rc=1, log_output="PROBE_STATUS=200\n") != "200"


def test_a_failing_probe_reports_the_status_it_could_recover(tmp_path):
    assert _run_probe(tmp_path, execute_rc=1, log_output="PROBE_STATUS=503\n") == "503"


def test_a_failing_probe_with_no_log_line_reports_000(tmp_path):
    assert _run_probe(tmp_path, execute_rc=1, log_output="") == "000"


@pytest.mark.parametrize(
    "status,should_promote",
    [(200, True), (201, False), (204, False), (301, False), (404, False), (500, False)],
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
            self.send_response(status)
            if status in (301, 302):
                self.send_header("Location", "http://127.0.0.1:1/gone")
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
            capture_output=True, timeout=60,
        ).returncode
    finally:
        server.shutdown()
    assert (rc == 0) is should_promote, f"HTTP {status} -> exit {rc}"


def test_an_unreachable_candidate_never_promotes(tmp_path):
    import socket
    import sys

    src = SCRIPT.read_text(encoding="utf-8")
    payload = re.search(r'--args="\^\|\^-c\|(.*?)"\s*\\\n', src, re.S).group(1)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    rc = subprocess.run(
        [sys.executable, "-c", payload.replace("$url", f"http://127.0.0.1:{port}/health")],
        capture_output=True, timeout=60,
    ).returncode
    assert rc != 0
