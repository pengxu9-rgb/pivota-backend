-- ops_external_seed_root_cause_probe.sql
--
-- Scope: read-only diagnostics for external-seed / auth / pool pressure investigation.
-- Target DB: Postgres-xMr6 production.
--
-- Usage:
--   psql "$DATABASE_URL" -f scripts/ops_external_seed_root_cause_probe.sql
--
-- Notes:
-- - This script does not mutate data.
-- - If pg_stat_statements is unavailable, related sections will fail; run available sections only.

\echo '=== 0) clock ==='
SELECT now() AS captured_at_utc;

\echo '=== 1) connection pressure and waits ==='
SELECT
  coalesce(wait_event_type, 'none') AS wait_event_type,
  coalesce(wait_event, 'none') AS wait_event,
  state,
  count(*) AS sessions
FROM pg_stat_activity
WHERE datname = current_database()
GROUP BY 1,2,3
ORDER BY sessions DESC, 1, 2;

\echo '=== 1.1) longest active sessions ==='
SELECT
  pid,
  usename,
  application_name,
  client_addr,
  state,
  wait_event_type,
  wait_event,
  now() - query_start AS query_age,
  left(query, 300) AS query_snippet
FROM pg_stat_activity
WHERE datname = current_database()
  AND state <> 'idle'
ORDER BY query_start ASC
LIMIT 30;

\echo '=== 1.2) database-level connection utilization ==='
SELECT
  d.datname,
  d.numbackends,
  s.setting::int AS max_connections,
  round(100.0 * d.numbackends / greatest(1, s.setting::int), 2) AS connection_usage_pct
FROM pg_stat_database d
JOIN pg_settings s ON s.name = 'max_connections'
WHERE d.datname = current_database();

\echo '=== 2) top statements (requires pg_stat_statements) ==='
SELECT
  calls,
  round(total_exec_time::numeric, 2) AS total_exec_ms,
  round(mean_exec_time::numeric, 2) AS mean_exec_ms,
  round(max_exec_time::numeric, 2) AS max_exec_ms,
  rows,
  left(regexp_replace(query, '\\s+', ' ', 'g'), 240) AS query_snippet
FROM pg_stat_statements
WHERE query ILIKE '%agents%'
   OR query ILIKE '%api_keys%'
   OR query ILIKE '%agent_api_keys%'
   OR query ILIKE '%agent_usage_logs%'
   OR query ILIKE '%products_cache%'
   OR query ILIKE '%external_product_seeds%'
ORDER BY total_exec_time DESC
LIMIT 30;

\echo '=== 3) key index inventory ==='
SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE tablename IN ('api_keys','agent_api_keys','agents','agent_usage_logs','products_cache','external_product_seeds')
ORDER BY tablename, indexname;

\echo '=== 4) table health / dead tuples ==='
SELECT
  relname,
  n_live_tup,
  n_dead_tup,
  coalesce(last_autovacuum, last_vacuum) AS last_maintained_at,
  autovacuum_count,
  vacuum_count,
  analyze_count,
  autoanalyze_count
FROM pg_stat_user_tables
WHERE relname IN ('api_keys','agent_api_keys','agents','agent_usage_logs','products_cache','external_product_seeds')
ORDER BY n_dead_tup DESC, relname;

\echo '=== 5) auth lookup EXPLAIN (adapt literal if needed) ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT a.*
FROM agents a
JOIN api_keys k ON k.agent_id = a.agent_id
WHERE k.key_hash = '0000000000000000000000000000000000000000000000000000000000000000'
  AND k.status = 'active'
LIMIT 1;

\echo '=== 5.1) auth lookup EXPLAIN for legacy advanced schema (agent_api_keys) ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT a.*
FROM agents a
JOIN agent_api_keys k ON k.agent_id = a.agent_id
WHERE k.key_hash IN (
  '0000000000000000000000000000000000000000000000000000000000000000',
  '00000000000000000000000000000000'
)
  AND COALESCE(k.is_active, TRUE) = TRUE
LIMIT 1;

\echo '=== 6) external seed text search EXPLAIN (LOWER LIKE shape) ==='
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, title, domain, canonical_url, destination_url
FROM external_product_seeds
WHERE status = 'active'
  AND attached_product_key IS NULL
  AND (
    LOWER(title) LIKE '%copper peptide%'
    OR LOWER(domain) LIKE '%copper peptide%'
    OR LOWER(canonical_url) LIKE '%copper peptide%'
    OR LOWER(destination_url) LIKE '%copper peptide%'
  )
ORDER BY updated_at DESC
LIMIT 40;
