"""The in-VPC health probe in deploy_backend.sh / deploy_gateway.sh must authenticate when the
service is private (PUBLIC=0), and must send no token when it is public (PUBLIC=1).

WHY THIS EXISTS. 2026-09-29 ~08:05Z: `CONFIG=preserve deploy_backend.sh staging 74e29e99f...`
deployed web-00025-tut. Cloud Run reported it Ready and it logged "Application startup complete",
but the gate printed "in-VPC probe job exited 1 ... candidate health check returned 000 - NOT
shifting traffic", and the traffic shift was done by hand. Staging `web` is private
(--no-allow-unauthenticated), and the probe job called /health with NO Authorization header:
Google's front end answered "The request was not authenticated ... Empty Authorization header"
before the app saw the request. The script built a token for the DIRECT curl only. Prod is
public, so prod never took this path.

The AUDIENCE is load-bearing, and it is not the URL being called. Measured 2026-09-29 from inside
the staging VPC as sa-worker (which holds project-level run.invoker), calling a TAG URL of `web`:
aud=<tag URL> -> 401, aud=<service URL> -> 200, no token -> 403. So the token must be scoped to the
service's own URL (`status.url`) while the request goes to the candidate's tag URL.

Both scripts are driven END TO END with gcloud/curl stubbed, and the assertions read the `--args`
the real `gcloud run jobs create` received - then EXECUTE that payload against a fake metadata
server and a fake private service, so "includes an auth header" means "a service that demands
one answers 200", not "the text contains the word Bearer".
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = {
    "web": REPO / "infra" / "gcp" / "deploy_backend.sh",
    "gateway": REPO / "infra" / "gcp" / "deploy_gateway.sh",
}
SHA = "74e29e99f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5"
CANDIDATE_TAG = "c-" + SHA[-12:]
METADATA = "http://metadata.google.internal"

GCLOUD_STUB = r"""#!/bin/sh
case "$1 $2" in
  "run services")
    [ "$3" = describe ] || exit 0
    case "$*" in
      *"--format=json"*)       printf '%s' '{"status":{"traffic":[]}}'; exit 0 ;;
      *"traffic.extract"*)     printf '%s\n' "$STUB_CAND_URL"; exit 0 ;;
      *"value(status.url)"*)   printf '%s\n' "$STUB_SERVICE_URL"; exit 0 ;;
      *maxScale*)              echo 4; exit 0 ;;
      *containers*env*)        echo "{'name': 'DB_POOL_MAX_SIZE', 'value': '8'}"; exit 0 ;;
      *)                       exit 0 ;;
    esac ;;
  "run jobs")
    case "$3" in
      create)
        # Keep the payload verbatim. It is multi-line Python, so it cannot share a
        # line-per-call log with the rest of argv.
        for a in "$@"; do
          case "$a" in --args=*) printf '%s' "${a#--args=}" > "$STUB_ARGS_FILE" ;; esac
        done
        exit 0 ;;
      execute) exit "$STUB_EXECUTE_RC" ;;
      delete)  echo deleted >> "$STUB_DELETES"; exit 0 ;;
    esac ;;
  "auth print-identity-token") echo operator-token; exit 0 ;;
esac
exit 0
"""


def _deploy(tmp_path: Path, service: str, public: str, *, service_url: str | None = None,
            execute_rc: int = 0):
    """Run the REAL script for `service` on staging, preserve, PROMOTE=0, direct probe 404."""
    here = tmp_path / "infra_gcp"
    here.mkdir()
    script = here / SCRIPTS[service].name
    shutil.copy2(SCRIPTS[service], script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(GCLOUD_STUB)
    # 404: what an operator's laptop gets from an `internal` service - forces the in-VPC path.
    (bin_dir / "curl").write_text("#!/bin/sh\nprintf 404\nexit 0\n")
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    args_file = tmp_path / "probe_args"
    deletes = tmp_path / "deletes"
    deletes.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GCLOUD": str(bin_dir / "gcloud"),
        "CONFIG": "preserve", "PROMOTE": "0", "PUBLIC": public,
        "STUB_CAND_URL": f"https://{CANDIDATE_TAG}---{service}-abc-uw.a.run.app",
        "STUB_SERVICE_URL": f"https://{service}-abc-uw.a.run.app" if service_url is None else service_url,
        "STUB_ARGS_FILE": str(args_file), "STUB_DELETES": str(deletes),
        "STUB_EXECUTE_RC": str(execute_rc),
    }
    for var in ("CONCURRENCY", "CONCURRENCY_LIMIT", "MIN_INSTANCES", "MAX_INSTANCES",
                "WORKERS", "MOUNT_DB", "SERVICE", "INGRESS"):
        env.pop(var, None)
    proc = subprocess.run(["bash", str(script), "staging", SHA], capture_output=True, text=True,
                          timeout=120, env=env, cwd=str(tmp_path))
    payload = args_file.read_text() if args_file.exists() else None
    return proc, payload, deletes.read_text().count("deleted")


def _python(payload: str) -> str:
    assert payload.startswith("^|^-c|"), payload[:40]
    return payload[len("^|^-c|"):]


class _Fake:
    """One local server playing BOTH the metadata server and a private Cloud Run service."""

    def __init__(self, *, health_status: int = 200, public: bool = False):
        self.token_requests: list[tuple[str, str | None]] = []
        self.health_auth: list[str | None] = []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/computeMetadata/"):
                    fake.token_requests.append((self.path, self.headers.get("Metadata-Flavor")))
                    if self.headers.get("Metadata-Flavor") != "Google":
                        return self._send(403, b"missing Metadata-Flavor")
                    return self._send(200, b"minted-id-token")
                fake.health_auth.append(self.headers.get("Authorization"))
                if not public and self.headers.get("Authorization") != "Bearer minted-id-token":
                    return self._send(401, b"The request was not authenticated")
                return self._send(health_status, b"ok")

            def _send(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()


def _execute(program: str, fake: _Fake, candidate_url: str) -> int:
    """Run the captured probe with its two hosts pointed at the fake."""
    program = program.replace(METADATA, fake.base).replace(candidate_url, fake.base + "/health")
    env = {**os.environ, "http_proxy": "", "https_proxy": "", "no_proxy": "*", "NO_PROXY": "*"}
    return subprocess.run([sys.executable, "-c", program], capture_output=True, timeout=60,
                          env=env).returncode


# ------------------------------------------------------------------------------ the regression

@pytest.mark.parametrize("service", sorted(SCRIPTS))
def test_a_private_services_probe_sends_a_token_scoped_to_the_service_url(tmp_path, service):
    proc, payload, _ = _deploy(tmp_path, service, "0")
    assert payload is not None, f"no in-VPC probe job was created:\n{proc.stderr}"
    program = _python(payload)
    assert "Authorization" in program and "Bearer" in program
    # The SERVICE url, never the tag url being called (measured: aud=<tag URL> -> 401).
    assert f"audience=https://{service}-abc-uw.a.run.app'" in program
    assert f"audience=https://{CANDIDATE_TAG}" not in program
    assert f"https://{CANDIDATE_TAG}---{service}-abc-uw.a.run.app/health" in program


@pytest.mark.parametrize("service", sorted(SCRIPTS))
def test_a_public_services_probe_sends_no_token(tmp_path, service):
    proc, payload, _ = _deploy(tmp_path, service, "1")
    assert payload is not None, f"no in-VPC probe job was created:\n{proc.stderr}"
    program = _python(payload)
    assert "Authorization" not in program
    assert "metadata.google.internal" not in program


@pytest.mark.parametrize("service", sorted(SCRIPTS))
def test_the_private_payload_gets_a_200_from_a_service_that_demands_a_token(tmp_path, service):
    """Execute what gcloud received. The fake service answers 401 to anything but the token the
    fake metadata server minted - exactly what staging `web` did on 2026-09-29."""
    _, payload, _ = _deploy(tmp_path, service, "0")
    cand = f"https://{CANDIDATE_TAG}---{service}-abc-uw.a.run.app/health"
    with _Fake() as fake:
        rc = _execute(_python(payload), fake, cand)
    assert rc == 0
    assert fake.health_auth == ["Bearer minted-id-token"]
    assert len(fake.token_requests) == 1
    path, flavor = fake.token_requests[0]
    assert flavor == "Google"
    assert path.endswith(f"audience=https://{service}-abc-uw.a.run.app")


@pytest.mark.parametrize("service", sorted(SCRIPTS))
@pytest.mark.parametrize("status", [201, 204, 401, 403, 500, 503])
def test_an_authenticated_probe_still_passes_only_on_an_exact_200(tmp_path, service, status):
    _, payload, _ = _deploy(tmp_path, service, "0")
    cand = f"https://{CANDIDATE_TAG}---{service}-abc-uw.a.run.app/health"
    with _Fake(health_status=status) as fake:
        assert _execute(_python(payload), fake, cand) != 0


@pytest.mark.parametrize("service", sorted(SCRIPTS))
def test_the_old_tokenless_payload_is_refused_by_a_private_service(tmp_path, service):
    """The PUBLIC=1 payload sent to a private service is the incident: it must NOT pass. Pins that
    the fake really enforces auth, so the 200 above is evidence rather than an open door."""
    _, payload, _ = _deploy(tmp_path, service, "1")
    cand = f"https://{CANDIDATE_TAG}---{service}-abc-uw.a.run.app/health"
    with _Fake() as fake:
        assert _execute(_python(payload), fake, cand) != 0
    assert fake.health_auth == [None]


@pytest.mark.parametrize("service", sorted(SCRIPTS))
def test_the_public_payload_passes_a_public_service(tmp_path, service):
    _, payload, _ = _deploy(tmp_path, service, "1")
    cand = f"https://{CANDIDATE_TAG}---{service}-abc-uw.a.run.app/health"
    with _Fake(public=True) as fake:
        assert _execute(_python(payload), fake, cand) == 0
    assert fake.token_requests == []


@pytest.mark.parametrize("service", sorted(SCRIPTS))
@pytest.mark.parametrize("public", ["0", "1"])
def test_the_probe_job_is_always_reaped(tmp_path, service, public):
    for rc in (0, 1):
        sub = tmp_path / f"rc{rc}"
        sub.mkdir()
        proc, payload, deletes = _deploy(sub, service, public, execute_rc=rc)
        assert payload is not None
        assert deletes == 1, f"execute rc={rc}: probe job deleted {deletes}x\n{proc.stderr}"
        assert (proc.returncode == 0) is (rc == 0), proc.stderr


@pytest.mark.parametrize("service", sorted(SCRIPTS))
def test_a_private_service_with_no_readable_url_refuses_instead_of_probing_unauthenticated(
    tmp_path, service
):
    """An empty audience would silently drop the token and reproduce the incident as a refusal
    nobody can read. Say why instead. (CAND_URL comes from the tag lookup, so only the audience
    read comes back empty here.)"""
    proc, payload, _ = _deploy(tmp_path, service, "0", service_url="")
    assert proc.returncode != 0
    assert "cannot authenticate" in proc.stderr
    assert payload is None
