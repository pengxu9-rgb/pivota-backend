#!/usr/bin/env bash
set -euo pipefail

# Reviews Center smoke (SQLite)
#
# Prereq: run the API server in another terminal using the same SQLite file, e.g.
#   export DATABASE_URL="sqlite+aiosqlite:////.../pivota_reviews_local.db"
#   export REVIEWS_IMPORT_DIR="$PWD/tmp/reviews-imports"
#   export REVIEWS_MEDIA_SIGNING_SECRET="test-secret"
#   python3 main.py
#
# Then run:
#   ./scripts/smoke_reviews_center_sqlite.sh
#
# Optional env overrides:
#   BASE_URL="http://localhost:8000"
#   DATABASE_URL="sqlite+aiosqlite:////abs/path/to/pivota_reviews_local.db"  (used to derive DB_PATH when set)
#   DB_PATH="/abs/path/to/pivota_reviews_local.db"
#   REVIEWS_IMPORT_DIR="/abs/path/to/tmp/reviews-imports"
#   REVIEWS_MEDIA_SIGNING_SECRET="test-secret"
#   METRICS_BEARER_TOKEN="devtoken"
#   TEST_429=1   (expects second request to return 429 for same IP)

BASE_URL="${BASE_URL:-http://localhost:8000}"
REVIEWS_MEDIA_SIGNING_SECRET="${REVIEWS_MEDIA_SIGNING_SECRET:-test-secret}"
REVIEWS_IMPORT_DIR="${REVIEWS_IMPORT_DIR:-$(pwd)/tmp/reviews-imports}"
TEST_429="${TEST_429:-0}"

if [[ -z "${DB_PATH:-}" ]]; then
  if [[ -n "${DATABASE_URL:-}" ]]; then
    # Parse sqlite URLs without regex (paths may contain spaces).
    # Supported:
    # - sqlite+aiosqlite:////abs/path.db
    # - sqlite+aiosqlite:///./rel/path.db
    case "${DATABASE_URL}" in
      sqlite+aiosqlite:////*)
        DB_PATH="/${DATABASE_URL#sqlite+aiosqlite:////}"
        ;;
      sqlite+aiosqlite:///*)
        remainder="${DATABASE_URL#sqlite+aiosqlite:///}"
        if [[ "${remainder}" == /* ]]; then
          DB_PATH="${remainder}"
        else
          DB_PATH="$(pwd)/${remainder}"
        fi
        ;;
      *)
        echo "❌ Unsupported DATABASE_URL for this script: ${DATABASE_URL}"
        echo "   Set DB_PATH explicitly, e.g. DB_PATH=/abs/path/to/pivota_reviews_local.db"
        exit 2
        ;;
    esac
  else
    DB_PATH="$(pwd)/pivota_reviews_local.db"
  fi
fi

export BASE_URL
export DB_PATH
export REVIEWS_IMPORT_DIR
export REVIEWS_MEDIA_SIGNING_SECRET

echo "== Reviews Center SQLite smoke =="
echo "BASE_URL=$BASE_URL"
echo "DB_PATH=$DB_PATH"
echo "REVIEWS_IMPORT_DIR=$REVIEWS_IMPORT_DIR"

mkdir -p "$REVIEWS_IMPORT_DIR"

echo
echo "== Seed (sqlite3) =="
eval "$(python3 - <<'PY'
import base64, hashlib, hmac, os, pathlib, sqlite3, time, uuid, shlex, sys

db_path = os.environ["DB_PATH"]
import_dir = pathlib.Path(os.environ["REVIEWS_IMPORT_DIR"])
secret = os.environ["REVIEWS_MEDIA_SIGNING_SECRET"].encode("utf-8")
base_url = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")

import_dir.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(db_path)
cur = conn.cursor()
tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
missing = sorted({"external_identities", "product_reviews", "media_assets"} - tables)
if missing:
    print("echo " + shlex.quote(f"❌ Missing tables in {db_path}: {missing}. Start server once (SQLite create_all) then retry."))
    sys.exit(1)

merchant_id, platform, platform_product_id = "m_demo", "demo", "p_demo"
product_key = f"{merchant_id}|{platform}|{platform_product_id}"
sku_key = f"{product_key}|∅"

source_system = "demo_import"
external_user_id = "u_demo"

cur.execute(
    "INSERT OR IGNORE INTO external_identities (merchant_id, source_system, external_user_id, display_name, status) "
    "VALUES (?, ?, ?, ?, ?)",
    (merchant_id, source_system, external_user_id, "DemoUser", "unclaimed"),
)
author_id = cur.execute(
    "SELECT id FROM external_identities WHERE merchant_id=? AND source_system=? AND external_user_id=?",
    (merchant_id, source_system, external_user_id),
).fetchone()[0]

public_id = str(uuid.uuid4())
fname = f"smoke-{public_id}.txt"
file_path = import_dir / fname
file_path.write_bytes(b"demo image bytes\n")
file_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()

external_review_id = f"r_demo_{int(time.time())}"
cur.execute(
    """
    INSERT INTO product_reviews (
      product_key, sku_key,
      merchant_id, platform, platform_product_id, variant_id,
      group_id, author_user_id,
      source_type, source_system, external_review_id,
      verification, rating, title, body,
      media_count, status
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        product_key, sku_key,
        merchant_id, platform, platform_product_id, None,
        None, author_id,
        "imported", source_system, external_review_id,
        "unverified", 5, "Great", "Nice product",
        1, "active",
    ),
)
review_id = cur.lastrowid

cur.execute(
    """
    INSERT INTO media_assets (review_id, type, url, public_id, file_path, file_hash, status)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
    (review_id, "image", f"file://{file_path}", public_id, str(file_path), file_hash, "active"),
)
media_id = cur.lastrowid

conn.commit()
conn.close()

exp_ok = int(time.time()) + 300
msg = f"{public_id}|{exp_ok}".encode("utf-8")
sig_ok = base64.urlsafe_b64encode(hmac.new(secret, msg, hashlib.sha256).digest()).decode("utf-8").rstrip("=")

media_url_ok = f"{base_url}/agent/shop/v1/review-media/{public_id}?exp={exp_ok}&sig={sig_ok}"

print("SKU_KEY=" + shlex.quote(sku_key))
print("PUBLIC_ID=" + shlex.quote(public_id))
print("REVIEW_ID=" + shlex.quote(str(review_id)))
print("MEDIA_ID=" + shlex.quote(str(media_id)))
print("MEDIA_URL_OK=" + shlex.quote(media_url_ok))
PY
)"

echo "Seed OK: sku_key=$SKU_KEY review_id=$REVIEW_ID media_id=$MEDIA_ID public_id=$PUBLIC_ID"

echo
echo "== list_sku_reviews (expect 200 + items + media url) =="
LIST_OUT="$(curl -sS -H "Content-Type: application/json" \
  -X POST "$BASE_URL/agent/shop/v1/invoke" \
  -d '{"operation":"list_sku_reviews","payload":{"sku":{"merchant_id":"m_demo","platform":"demo","platform_product_id":"p_demo","variant_id":null},"filters":{"limit":2}}}')"
echo "$LIST_OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("count=", len(d.get("items",[]))); print("next_cursor=", d.get("next_cursor")); print("first_review_id=", (d["items"][0]["review_id"] if d.get("items") else None)); print("first_media_path=", (d["items"][0]["media"][0]["url"] if d.get("items") and d["items"][0].get("media") else None))'

CURSOR="$(printf '%s' "$LIST_OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("next_cursor") or "")')"
if [[ -z "$CURSOR" ]]; then
  echo "❌ Missing next_cursor from list_sku_reviews; cannot test pagination"
  exit 1
fi

echo
echo "== list_sku_reviews pagination (cursor; expect different/older items, or empty if DB has <3 rows) =="
PAGE2="$(curl -sS -H "Content-Type: application/json" \
  -X POST "$BASE_URL/agent/shop/v1/invoke" \
  -d "{\"operation\":\"list_sku_reviews\",\"payload\":{\"sku\":{\"merchant_id\":\"m_demo\",\"platform\":\"demo\",\"platform_product_id\":\"p_demo\",\"variant_id\":null},\"filters\":{\"limit\":2,\"cursor\":\"$CURSOR\"}}}")"
echo "$PAGE2" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("page2_count=", len(d.get("items",[]))); print("page2_first_review_id=", (d["items"][0]["review_id"] if d.get("items") else None)); print("page2_next_cursor=", d.get("next_cursor"))'

echo
echo "== review-media 200 (expect 200 + Cache-Control + ETag) =="
curl -sS -D - -o /dev/null -H "X-Forwarded-For: 1.1.1.1" "$MEDIA_URL_OK"

echo
echo "== review-media 304 (If-None-Match; expect 304) =="
ETAG="$(curl -sS -D - -o /dev/null -H "X-Forwarded-For: 2.2.2.2" "$MEDIA_URL_OK" | awk -F': ' 'tolower($1)=="etag"{print $2}' | tr -d '\r')"
curl -sS -D - -o /dev/null -H "X-Forwarded-For: 3.3.3.3" -H "If-None-Match: $ETAG" "$MEDIA_URL_OK"

echo
echo "== review-media 403 (missing signature; expect 403) =="
curl -sS -D - -o /dev/null -H "X-Forwarded-For: 4.4.4.4" "${MEDIA_URL_OK%%\?*}" || true

if [[ "$TEST_429" == "1" ]]; then
  echo
  echo "== review-media 429 (requires REVIEWS_MEDIA_RPM=1 on server; expect 200 then 429) =="
  curl -sS -o /dev/null -w "status=%{http_code}\n" -H "X-Forwarded-For: 5.5.5.5" "$MEDIA_URL_OK"
  curl -sS -o /dev/null -w "status=%{http_code}\n" -H "X-Forwarded-For: 5.5.5.5" "$MEDIA_URL_OK"
fi

if [[ -n "${METRICS_BEARER_TOKEN:-}" ]]; then
  echo
  echo "== metrics sample =="
  curl -sS --max-time 5 -H "Authorization: Bearer $METRICS_BEARER_TOKEN" "$BASE_URL/metrics" \
    | egrep "^reviews_(invoke|media)_" | head -n 120
fi

echo
echo "✅ SQLite smoke OK"
