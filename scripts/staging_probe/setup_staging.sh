#!/usr/bin/env bash
set -euo pipefail

# Build out Pivota Infra / staging / web for the controlled PSP probe.
# This script never echoes raw secrets. It expects Railway CLI auth and project
# linkage to Pivota Infra / staging / web.

MERCHANT_ID="merch_efbc46b4619cfbdf"
STAGING_DOMAIN="web-staging-3f9e.up.railway.app"

railway status

echo "Adding Postgres to the currently linked staging environment if needed..."
railway add -d postgres

if [[ -z "${STAGING_AGENT_API_KEY:-}" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    STAGING_AGENT_API_KEY="ak_live_$(openssl rand -hex 32)"
  else
    echo "openssl is required to generate STAGING_AGENT_API_KEY automatically" >&2
    exit 2
  fi
fi

if [[ ! "${STAGING_AGENT_API_KEY}" =~ ^ak_(live_)?[0-9a-f]{64}$ ]]; then
  echo "STAGING_AGENT_API_KEY must match ak_<64hex> or ak_live_<64hex>" >&2
  exit 2
fi

echo "Setting staging web variables..."
railway variables \
  -e staging \
  -s web \
  --skip-deploys \
  --set 'DATABASE_URL=${{Postgres.DATABASE_URL}}' \
  --set "ALLOW_TEST_PSP_PROBE=1" \
  --set "TEST_PSP_PROBE_MERCHANTS=${MERCHANT_ID}" \
  --set "APP_ENV=staging" \
  --set "ENVIRONMENT=staging" \
  --set "FRESH_QUOTE_VALIDATE_SKIP_SECONDS=86400" >/dev/null

railway variables \
  -e staging \
  -s web \
  --skip-deploys \
  --set "SHOP_GATEWAY_AGENT_API_KEY=${STAGING_AGENT_API_KEY}" >/dev/null

echo "Deploying main to staging web..."
railway up -e staging -s web --detach

echo "Polling staging health endpoints..."
for _ in $(seq 1 90); do
  if curl -fsS "https://${STAGING_DOMAIN}/version" >/tmp/pivota-staging-version.json 2>/dev/null \
    && curl -fsS "https://${STAGING_DOMAIN}/health" >/tmp/pivota-staging-health.json 2>/dev/null; then
    break
  fi
  sleep 10
done

python3 - <<'PY'
import json
from pathlib import Path

version = json.loads(Path("/tmp/pivota-staging-version.json").read_text())
health = json.loads(Path("/tmp/pivota-staging-health.json").read_text())

if version.get("branch") != "main":
    raise SystemExit(f"unexpected branch: {version.get('branch')}")
full_sha = str(version.get("full_sha") or version.get("commit") or "")
if not full_sha.startswith("370a2756"):
    raise SystemExit(f"unexpected full_sha: {full_sha[:12]}")
if health.get("status") not in {"ok", "healthy"}:
    raise SystemExit(f"unexpected health status: {health.get('status')}")
missing = health.get("missing_columns")
if missing not in ([], {}, None):
    raise SystemExit(f"missing_columns not empty: {missing}")
print(json.dumps({"version": version, "health": health}, indent=2, sort_keys=True))
PY

cat <<'MSG'
Staging web is deployed and healthy.

Seed requires real Stripe TEST secrets in shell variables:
  STRIPE_TEST_SECRET_KEY=sk_test_...
  STRIPE_TEST_PUBLISHABLE_KEY=pk_test_...
  STRIPE_TEST_WEBHOOK_SECRET=whsec_...

Then run:
  railway run -e staging -s web -- python scripts/staging_probe/seed.py
MSG
