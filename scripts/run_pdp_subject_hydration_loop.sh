#!/bin/bash
set -euo pipefail

# Periodically hydrate pdp_subject_index for the employee PDP dashboard.
#
# This runs in a loop because the original platform had no cron service.
#
# Production is Cloud Run (pivota-prod/us-west1) now, where Cloud Scheduler does
# exist - see infra/gcp/setup_scheduler.sh. No scheduler entry covers PDP subject
# hydration today (checked 2026-08-25: the enabled crons are relgraph-sync-cron
# and reviews-invitation-send-cron), so this loop is still the way it runs.
#
# Env:
# - DATABASE_URL (required)
# - SLEEP_SECONDS (optional, default: 1800)
# - PDP_HYDRATION_LIMIT (optional, default: 10000; set 0 for all rows)
# - PDP_HYDRATION_ACTOR_ID (optional, default: pdp_subject_hydration_loop)
# - PORT (optional, default: 8080) healthcheck port

SLEEP_SECONDS="${SLEEP_SECONDS:-1800}"
PDP_HYDRATION_LIMIT="${PDP_HYDRATION_LIMIT:-10000}"
PDP_HYDRATION_ACTOR_ID="${PDP_HYDRATION_ACTOR_ID:-pdp_subject_hydration_loop}"
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
  echo "pdp_subject_hydration_start_utc=$(date -u +%FT%TZ)"
  "$PY" scripts/hydrate_pdp_subject_index.py \
    --apply \
    --limit "$PDP_HYDRATION_LIMIT" \
    --actor-id "$PDP_HYDRATION_ACTOR_ID"
  echo "pdp_subject_hydration_sleep_seconds=$SLEEP_SECONDS"
  sleep "$SLEEP_SECONDS"
done
