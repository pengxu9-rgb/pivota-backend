#!/usr/bin/env bash
set -euo pipefail

# Reviews Center smoke (Postgres)
#
# Prereq:
# - Postgres is running and DATABASE_URL is set to a Postgres URL
# - API server is running in another terminal using the SAME DATABASE_URL and the same REVIEWS_IMPORT_DIR
# - `psql` is installed (or use docker fallback)
#
# Optional env overrides:
#   BASE_URL="http://localhost:8000"
#   APPLY_MIGRATIONS=1  (runs 040/041 via psql before seeding)
#   REVIEWS_MEDIA_SIGNING_SECRET="test-secret"
#   REVIEWS_IMPORT_DIR="/abs/path/to/tmp/reviews-imports"

BASE_URL="${BASE_URL:-http://localhost:8000}"
APPLY_MIGRATIONS="${APPLY_MIGRATIONS:-0}"
REVIEWS_MEDIA_SIGNING_SECRET="${REVIEWS_MEDIA_SIGNING_SECRET:-test-secret}"
REVIEWS_IMPORT_DIR="${REVIEWS_IMPORT_DIR:-$(pwd)/tmp/reviews-imports}"
PG_CONTAINER_NAME="${PG_CONTAINER_NAME:-pivota-pg}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "❌ DATABASE_URL is required (postgresql://...)"
  exit 2
fi
if ! [[ "$DATABASE_URL" =~ ^postgres ]]; then
  echo "❌ DATABASE_URL does not look like Postgres: $DATABASE_URL"
  exit 2
fi

PSQL_MODE="local"
if command -v psql >/dev/null 2>&1; then
  PSQL=(psql)
else
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -qx "${PG_CONTAINER_NAME}"; then
    PSQL_MODE="docker"
    PSQL=(docker exec -i "${PG_CONTAINER_NAME}" psql)
  else
    echo "❌ psql not found and docker container '${PG_CONTAINER_NAME}' not running."
    exit 2
  fi
fi

mkdir -p "$REVIEWS_IMPORT_DIR"

export BASE_URL
export DATABASE_URL
export REVIEWS_IMPORT_DIR
export REVIEWS_MEDIA_SIGNING_SECRET

PSQL_BASE_ARGS=(-X -v ON_ERROR_STOP=1)
PSQL_SCALAR_ARGS=("${PSQL_BASE_ARGS[@]}" -tAq -P footer=off)

psql_scalar_int() {
  local sql="$1"
  local val
  val="$("${PSQL[@]}" "$DATABASE_URL" "${PSQL_SCALAR_ARGS[@]}" <<<"$sql" | awk '$1 ~ /^[0-9]+$/ {print $1; exit}')"
  if [[ -z "$val" ]]; then
    echo "❌ Failed to capture integer from psql output for SQL:" >&2
    echo "$sql" >&2
    exit 1
  fi
  printf '%s' "$val"
}

echo "== Reviews Center Postgres smoke =="
echo "BASE_URL=$BASE_URL"
echo "DATABASE_URL=$DATABASE_URL"
echo "REVIEWS_IMPORT_DIR=$REVIEWS_IMPORT_DIR"
echo "PSQL_MODE=$PSQL_MODE"

if [[ "$APPLY_MIGRATIONS" == "1" ]]; then
  echo
  echo "== Apply migrations (040/041) =="
  if [[ "$PSQL_MODE" == "local" ]]; then
    "${PSQL[@]}" "$DATABASE_URL" "${PSQL_BASE_ARGS[@]}" -f db/migrations/040_reviews_center.sql
    "${PSQL[@]}" "$DATABASE_URL" "${PSQL_BASE_ARGS[@]}" -f db/migrations/041_reviews_center_hardening.sql
  else
    cat db/migrations/040_reviews_center.sql | "${PSQL[@]}" "$DATABASE_URL" "${PSQL_BASE_ARGS[@]}"
    cat db/migrations/041_reviews_center_hardening.sql | "${PSQL[@]}" "$DATABASE_URL" "${PSQL_BASE_ARGS[@]}"
  fi
fi

echo
echo "== Seed (psql) =="
eval "$(python3 - <<'PY'
import hashlib, os, pathlib, time, uuid, shlex

import_dir = pathlib.Path(os.environ["REVIEWS_IMPORT_DIR"])
public_id = str(uuid.uuid4())
fname = f"smoke-{public_id}.txt"
file_path = import_dir / fname
file_path.write_bytes(b"demo image bytes\n")
file_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()

merchant_id, platform, platform_product_id = "m_demo", "demo", "p_demo"
product_key = f"{merchant_id}|{platform}|{platform_product_id}"
sku_key = f"{product_key}|∅"
external_review_id = f"r_demo_{int(time.time())}"

print("PUBLIC_ID=" + shlex.quote(public_id))
print("FILE_PATH=" + shlex.quote(str(file_path)))
print("FILE_HASH=" + shlex.quote(file_hash))
print("MERCHANT_ID=" + shlex.quote(merchant_id))
print("PLATFORM=" + shlex.quote(platform))
print("PLATFORM_PRODUCT_ID=" + shlex.quote(platform_product_id))
print("PRODUCT_KEY=" + shlex.quote(product_key))
print("SKU_KEY=" + shlex.quote(sku_key))
print("EXTERNAL_REVIEW_ID=" + shlex.quote(external_review_id))
PY
)"

AUTHOR_ID="$(psql_scalar_int "
WITH ins AS (
  INSERT INTO external_identities (merchant_id, source_system, external_user_id, display_name, status)
  SELECT '$MERCHANT_ID', 'demo_import', 'u_demo', 'DemoUser', 'unclaimed'
  WHERE NOT EXISTS (
    SELECT 1 FROM external_identities
    WHERE merchant_id='$MERCHANT_ID' AND source_system='demo_import' AND external_user_id='u_demo'
  )
  RETURNING id
)
SELECT id FROM ins
UNION ALL
SELECT id FROM external_identities
WHERE merchant_id='$MERCHANT_ID' AND source_system='demo_import' AND external_user_id='u_demo'
LIMIT 1;
")"

REVIEW_ID="$(psql_scalar_int "
INSERT INTO product_reviews (
  product_key, sku_key,
  merchant_id, platform, platform_product_id, variant_id,
  group_id, author_user_id,
  source_type, source_system, external_review_id,
  verification, rating, title, body,
  media_count, status
) VALUES (
  '$PRODUCT_KEY', '$SKU_KEY',
  '$MERCHANT_ID', '$PLATFORM', '$PLATFORM_PRODUCT_ID', NULL,
  NULL, $AUTHOR_ID,
  'imported', 'demo_import', '$EXTERNAL_REVIEW_ID',
  'unverified', 5, 'Great', 'Nice product',
  1, 'active'
)
RETURNING id;
")"

MEDIA_ID="$(psql_scalar_int "
INSERT INTO media_assets (review_id, type, url, public_id, file_path, file_hash, status)
VALUES ($REVIEW_ID, 'image', 'file://$FILE_PATH', '$PUBLIC_ID', '$FILE_PATH', '$FILE_HASH', 'active')
RETURNING id;
")"

echo "Seed OK: review_id=$REVIEW_ID media_id=$MEDIA_ID public_id=$PUBLIC_ID"

echo
echo "== list_sku_reviews (expect 200 + items + media url) =="
LIST_OUT="$(curl -sS -H "Content-Type: application/json" \
  -X POST "$BASE_URL/agent/shop/v1/invoke" \
  -d '{"operation":"list_sku_reviews","payload":{"sku":{"merchant_id":"m_demo","platform":"demo","platform_product_id":"p_demo","variant_id":null},"filters":{"limit":2}}}')"
echo "$LIST_OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("count=", len(d.get("items",[]))); print("next_cursor=", d.get("next_cursor")); print("first_media_path=", (d["items"][0]["media"][0]["url"] if d.get("items") and d["items"][0].get("media") else None))'

MEDIA_PATH="$(printf '%s' "$LIST_OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["items"][0]["media"][0]["url"])')"
MEDIA_URL="$BASE_URL$MEDIA_PATH"

echo
echo "== review-media 200 =="
curl -sS -D - -o /dev/null -H "X-Forwarded-For: 1.1.1.1" "$MEDIA_URL"

echo
echo "== review-media 304 =="
ETAG="$(curl -sS -D - -o /dev/null -H "X-Forwarded-For: 2.2.2.2" "$MEDIA_URL" | awk -F': ' 'tolower($1)=="etag"{print $2}' | tr -d '\r')"
curl -sS -D - -o /dev/null -H "X-Forwarded-For: 3.3.3.3" -H "If-None-Match: $ETAG" "$MEDIA_URL"

echo
echo "✅ Postgres smoke OK"
