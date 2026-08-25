#!/bin/bash
set -euo pipefail

# Periodically send buyer review invitation emails for eligible orders.
#
# This runs in a loop because the original platform had no cron service.
#
# THAT IS NO LONGER TRUE, so check before you start it. Production is Cloud Run
# (pivota-prod/us-west1) and Cloud Scheduler exists: `reviews-invitation-send-cron`
# is ENABLED on a `* * * * *` schedule and triggers the `reviews-invitation-send`
# Cloud Run job (verified 2026-08-25). That job runs a DIFFERENT entry point -
# scripts/process_due_reviews_invitation_send_jobs.py, the due-jobs queue - rather
# than the direct sender this loop calls, so the two are not simply duplicates.
# But they touch the same invitation population, and buyer emails do not unsend.
# Confirm the overlap before running this alongside the cron:
#   gcloud scheduler jobs list --project pivota-prod --location us-west1
#
# Env:
# - DATABASE_URL (required)
# - REVIEWS_BASE_URL (required)
# - REVIEWS_INVITATION_ISSUER_INTERNAL_KEY (required)
# - SLEEP_SECONDS (optional, default: 3600)
# - PORT (optional, default: 8080) healthcheck port
#
# Optional overrides (passed through to python script):
# - DELIVERED_AFTER_DAYS (default: 3)
# - SHIPPED_AFTER_DAYS (default: 10)
# - PAID_AFTER_DAYS (default: 14)
# - TTL_SECONDS (default: 604800)
# - MAX_LINKS (default: 3)
# - LIMIT (default: 50)

SLEEP_SECONDS="${SLEEP_SECONDS:-3600}"
PORT="${PORT:-8080}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: missing DATABASE_URL" >&2
  exit 2
fi
if [[ -z "${REVIEWS_BASE_URL:-}" ]]; then
  echo "ERROR: missing REVIEWS_BASE_URL" >&2
  exit 2
fi
if [[ -z "${REVIEWS_INVITATION_ISSUER_INTERNAL_KEY:-}" ]]; then
  echo "ERROR: missing REVIEWS_INVITATION_ISSUER_INTERNAL_KEY" >&2
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

DELIVERED_AFTER_DAYS="${DELIVERED_AFTER_DAYS:-3}"
SHIPPED_AFTER_DAYS="${SHIPPED_AFTER_DAYS:-10}"
PAID_AFTER_DAYS="${PAID_AFTER_DAYS:-14}"
TTL_SECONDS="${TTL_SECONDS:-604800}"
MAX_LINKS="${MAX_LINKS:-3}"
LIMIT="${LIMIT:-50}"

while true; do
  echo "invitation_email_start_utc=$(date -u +%FT%TZ)"
  "$PY" scripts/send_reviews_invitation_emails.py \
    --apply \
    --delivered-after-days "$DELIVERED_AFTER_DAYS" \
    --shipped-after-days "$SHIPPED_AFTER_DAYS" \
    --paid-after-days "$PAID_AFTER_DAYS" \
    --ttl-seconds "$TTL_SECONDS" \
    --max-links "$MAX_LINKS" \
    --limit "$LIMIT"
  echo "invitation_email_sleep_seconds=$SLEEP_SECONDS"
  sleep "$SLEEP_SECONDS"
done

