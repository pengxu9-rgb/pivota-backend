#!/usr/bin/env bash
set -euo pipefail

# Compare "fresh connection per request" vs "single-process connection reuse"
# for the same URL using curl timing fields.
#
# Usage:
#   scripts/probe_http_tls_reuse.sh <URL> [COUNT]
#
# Examples:
#   scripts/probe_http_tls_reuse.sh https://web-production-fedb.up.railway.app/health 5
#   METHOD=POST BODY_JSON='{"alias":"test-product"}' \
#     scripts/probe_http_tls_reuse.sh https://web-production-fedb.up.railway.app/v1/subject/resolve 5
#
# Optional env:
#   METHOD=GET|POST|PUT|PATCH|DELETE   (default: GET)
#   BODY_JSON='{"k":"v"}'              (optional; adds Content-Type JSON)
#   PROTO=auto|h1|h2                   (default: auto)
#   IPV4_ONLY=1                        (default: 0)
#   CONNECT_TIMEOUT=10                 (seconds)
#   MAX_TIME=30                        (seconds)
#   HEADER_1='Authorization: Bearer ...'
#   HEADER_2='X-Foo: bar'

URL="${1:-}"
COUNT="${2:-5}"

if [[ -z "${URL}" ]]; then
  echo "usage: $0 <URL> [COUNT]" >&2
  exit 1
fi

if ! [[ "${COUNT}" =~ ^[0-9]+$ ]] || [[ "${COUNT}" -lt 1 ]]; then
  echo "COUNT must be a positive integer" >&2
  exit 1
fi

METHOD="${METHOD:-GET}"
BODY_JSON="${BODY_JSON:-}"
PROTO="${PROTO:-auto}"
IPV4_ONLY="${IPV4_ONLY:-0}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-10}"
MAX_TIME="${MAX_TIME:-30}"

SEGMENT_ARGS=(
  -sS
  -o /dev/null
  --connect-timeout "${CONNECT_TIMEOUT}"
  --max-time "${MAX_TIME}"
)

case "${PROTO}" in
  auto) ;;
  h1) SEGMENT_ARGS+=(--http1.1) ;;
  h2) SEGMENT_ARGS+=(--http2) ;;
  *)
    echo "PROTO must be one of: auto|h1|h2" >&2
    exit 1
    ;;
esac

if [[ "${IPV4_ONLY}" == "1" ]]; then
  SEGMENT_ARGS+=(--ipv4)
fi

if [[ "${METHOD}" != "GET" ]]; then
  SEGMENT_ARGS+=(-X "${METHOD}")
fi

if [[ -n "${BODY_JSON}" ]]; then
  SEGMENT_ARGS+=(-H "Content-Type: application/json" --data "${BODY_JSON}")
fi

for key in "${!HEADER_@}"; do
  val="${!key}"
  if [[ -n "${val}" ]]; then
    SEGMENT_ARGS+=(-H "${val}")
  fi
done

build_time_fmt() {
  local mode="$1"
  local run="$2"
  printf 'mode=%s run=%s dns=%%{time_namelookup} conn=%%{time_connect} tls=%%{time_appconnect} ttfb=%%{time_starttransfer} total=%%{time_total}\n' "${mode}" "${run}"
}

RAW_FILE="$(mktemp)"
trap 'rm -f "${RAW_FILE}"' EXIT

echo "[probe] url=${URL}"
echo "[probe] count=${COUNT} method=${METHOD} proto=${PROTO} ipv4_only=${IPV4_ONLY}"
echo "[probe] connect_timeout=${CONNECT_TIMEOUT}s max_time=${MAX_TIME}s"

echo
echo "[fresh] one request per curl process"
for ((i = 1; i <= COUNT; i++)); do
  line="$(curl "${SEGMENT_ARGS[@]}" -w "$(build_time_fmt fresh "${i}")" "${URL}")"
  echo "${line}"
  echo "${line}" >>"${RAW_FILE}"
done

echo
echo "[reuse] single curl process with --next (connection reuse candidate)"
cmd=(curl)
for ((i = 1; i <= COUNT; i++)); do
  cmd+=("${SEGMENT_ARGS[@]}" -w "$(build_time_fmt reuse "${i}")" "${URL}")
  if [[ "${i}" -lt "${COUNT}" ]]; then
    cmd+=(--next)
  fi
done
reuse_raw="$("${cmd[@]}")"
reuse_output="$(printf '%s' "${reuse_raw}" | sed 's/mode=/\nmode=/g' | sed '/^$/d')"
printf '%s\n' "${reuse_output}"
printf '%s\n' "${reuse_output}" >>"${RAW_FILE}"

echo
echo "[summary] averages (seconds)"
awk '
  function kv(line, key,    i, n, pair, parts) {
    n = split(line, pair, " ");
    for (i = 1; i <= n; i++) {
      split(pair[i], parts, "=");
      if (parts[1] == key) return parts[2] + 0.0;
    }
    return 0.0;
  }
  {
    mode = kv($0, "mode");
    # kv() returns 0 for strings, so parse mode separately:
    split($0, arr, " ");
    split(arr[1], p, "=");
    mode = p[2];
    run = kv($0, "run");
    dns = kv($0, "dns");
    conn = kv($0, "conn");
    tls = kv($0, "tls");
    ttfb = kv($0, "ttfb");
    total = kv($0, "total");
    count[mode] += 1;
    sum_dns[mode] += dns;
    sum_conn[mode] += conn;
    sum_tls[mode] += tls;
    sum_ttfb[mode] += ttfb;
    sum_total[mode] += total;
    if (run > 1) {
      warm_count[mode] += 1;
      warm_sum_dns[mode] += dns;
      warm_sum_conn[mode] += conn;
      warm_sum_tls[mode] += tls;
      warm_sum_ttfb[mode] += ttfb;
      warm_sum_total[mode] += total;
    }
  }
  END {
    printf "mode   n   dns_avg   conn_avg  tls_avg   ttfb_avg  total_avg\n";
    for (m in count) {
      n = count[m];
      printf "%-6s %2d  %.6f  %.6f  %.6f  %.6f  %.6f\n",
        m, n,
        sum_dns[m] / n,
        sum_conn[m] / n,
        sum_tls[m] / n,
        sum_ttfb[m] / n,
        sum_total[m] / n;
    }
    printf "\nmode(warm, run>1) n   dns_avg   conn_avg  tls_avg   ttfb_avg  total_avg\n";
    for (m in warm_count) {
      n = warm_count[m];
      printf "%-17s %2d  %.6f  %.6f  %.6f  %.6f  %.6f\n",
        m, n,
        warm_sum_dns[m] / n,
        warm_sum_conn[m] / n,
        warm_sum_tls[m] / n,
        warm_sum_ttfb[m] / n,
        warm_sum_total[m] / n;
    }
  }
' "${RAW_FILE}"

echo
echo "[hint] If reuse.tls_avg is near 0 and fresh.tls_avg is high, bottleneck is handshake/connection."
