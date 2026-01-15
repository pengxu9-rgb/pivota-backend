#!/bin/bash
set -euo pipefail

# Periodically cleanup buyer submission replay/idempotency tables.
#
# Env:
# - DATABASE_URL (required)
# - SLEEP_SECONDS (optional, default: 21600 = 6 hours)
# - PORT (optional, default: 8080) healthcheck port
#
# This script intentionally does not print DATABASE_URL or any tokens.

SLEEP_SECONDS="${SLEEP_SECONDS:-21600}"
PORT="${PORT:-8080}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: missing DATABASE_URL" >&2
  exit 2
fi

PY=""
if command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "ERROR: python runtime not found (python3/python)" >&2
  exit 2
fi

echo "health_server_port=$PORT"
"$PY" - <<'PY' &
import http.server
import os
import socketserver

PORT = int(os.environ.get("PORT", "8080"))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/health", "/healthz", "/live", "/ready"):
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = b"not_found\n"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


class _TCPServer(socketserver.TCPServer):
    allow_reuse_address = True


with _TCPServer(("0.0.0.0", PORT), _Handler) as httpd:
    httpd.serve_forever()
PY

HEALTH_PID="$!"
trap 'kill "$HEALTH_PID" >/dev/null 2>&1 || true' EXIT

while true; do
  echo "cleanup_start_utc=$(date -u +%FT%TZ)"
  "$PY" scripts/cleanup_buyer_submission_tables.py --apply
  echo "cleanup_sleep_seconds=$SLEEP_SECONDS"
  sleep "$SLEEP_SECONDS"
done
