"""Guard before app imports or DDL. No dotenv, main, db modules or network SDK imports."""

from __future__ import annotations
import hashlib
import ipaddress
import os
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, unquote

BASE_COMMIT = "ffae02260a8520eaa7e9cbf044a90b62d376d6ab"
FIXTURE_ID = "20261002_01a0f"
MARKER = "reap_rehearsal_guard_manifest"
ORM_TABLES = (
    "agents",
    "agent_usage_logs",
    "catalog_merchants",
    "catalog_products",
    "catalog_skus",
    "catalog_offers",
    "surface_click_events",
    "commerce_attribution_edges",
    "commerce_interactions",
    "commerce_interaction_events",
    "buyer_addresses",
    "buyer_agent_links",
)
EXTRA_TABLES = (
    "buyer_identity_links",
    "reap_agentic_enrollments",
    "reap_agentic_purchases",
    "reap_agentic_eligibility",
    "reap_agentic_buyer_refs",
    "reap_agentic_purchase_keys",
    "tierb_cart_link_eligibility",
    "conversion_click_claims",
    "merchant_purchasability",
    "external_product_seeds",
    "api_keys",
)
CATALOG_TABLES = (
    "catalog_merchants",
    "catalog_products",
    "catalog_skus",
    "catalog_offers",
    "external_product_seeds",
    "index_pipeline_state",
    "catalog_row_trust",
    "pdp_identity_listing",
    "merchant_stores",
    "content_canonical_election",
)
TABLES = ORM_TABLES + EXTRA_TABLES + tuple(n for n in CATALOG_TABLES if n not in ORM_TABLES + EXTRA_TABLES)
READ_ONLY_TABLES = set(CATALOG_TABLES) | {
    "agents",
    "api_keys",
    "tierb_cart_link_eligibility",
    "merchant_purchasability",
    "reap_agentic_eligibility",
}
WRITE_TABLES = set(TABLES) - READ_ONLY_TABLES
EMPTY_TABLES = (
    "reap_agentic_purchases",
    "reap_agentic_enrollments",
    "buyer_identity_links",
    "reap_agentic_buyer_refs",
    "reap_agentic_purchase_keys",
    "buyer_addresses",
    "buyer_agent_links",
    "surface_click_events",
    "commerce_attribution_edges",
    "commerce_interactions",
    "commerce_interaction_events",
    "conversion_click_claims",
)
REQUIRED_FLAGS = {
    "PIVOTA_ENV": "staging",
    "REAP_REHEARSAL_PREPARATION_ONLY": "1",
    "REAP_AGENTIC_ENABLED": "0",
    "REAP_AGENTIC_CREATE_ENABLED": "0",
    "REAP_AGENTIC_CART_LINK_ENABLED": "0",
    "AUDIT_SCHEDULER_ENABLED": "false",
    "REVIEWS_SCHEDULER_ENABLED": "false",
    "SCHEDULER_ALLOWLIST": ",",
    "SKIP_HEAVY_STARTUP_INIT": "true",
    "AGENT_AUTH_KEY_TABLE": "api_keys",
    "AGENT_AUTH_ENABLE_LEGACY_API_KEY_FALLBACK": "false",
}
FORBIDDEN_SECRETS = (
    "REAP_API_KEY",
    "REAP_API_BASE_URL",
    "REAP_AGENTIC_SIMULATE_CHECKOUT",
    "AGENT_USER_JWKS_URL",
    "GCP_SERVICE_ACCOUNT_JSON",
)


@dataclass(frozen=True)
class Target:
    database: str = "reap_rehearsal_20261002_01a0f"
    host: str = "10.122.0.3"
    port: int = 5432
    migrator: str = "reap_rehearsal_01a0f_migrator"
    runtime: str = "reap_rehearsal_01a0f_runtime"


TARGET = Target()  # CLI never accepts a target/host override.


class GuardRejected(RuntimeError):
    """Messages are fixed codes: never include URL, credentials, row IDs or values."""


_receipt = None


def reject(code):
    raise GuardRejected(code)


def validate_environment(env):
    if any(env.get(k) != v for k, v in REQUIRED_FLAGS.items()):
        reject("unsafe_startup_flags")
    if any(env.get(k, "").strip() for k in FORBIDDEN_SECRETS):
        reject("external_credentials_or_provider_config")
    # Do not allow libpq/SDK environment or alternate config to redirect a connection.
    if any(
        env.get(k)
        for k in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGOPTIONS", "PGSERVICE", "PGSERVICEFILE")
    ):
        reject("ambient_pg_configuration")
    if (
        env.get("GOOGLE_CLOUD_PROJECT") != "pivota-staging"
        or env.get("REAP_REHEARSAL_SQL_INSTANCE") != "pivota-staging:us-west1:pivota-pg"
    ):
        reject("deployment_identity")


def validate_dsn(raw, mode, target=TARGET):
    if mode not in {"migrate", "runtime"}:
        reject("mode")
    if not isinstance(raw, str) or not raw or raw != raw.strip() or any(ord(c) < 33 or ord(c) == 127 for c in raw):
        reject("dsn_format")
    try:
        p = urlsplit(raw)
        port = p.port
    except ValueError:
        reject("dsn_format")
    wanted = target.migrator if mode == "migrate" else target.runtime
    if "?" in raw or "#" in raw:
        reject("dsn_options")
    if (
        p.scheme != "postgresql"
        or p.hostname != target.host
        or port != target.port
        or p.path != "/" + target.database
        or p.query
        or p.fragment
        or unquote(p.username or "") != wanted
        or not p.password
    ):
        reject("dsn_target")
    if p.netloc.count("@") != 1 or unquote(p.username or "") != p.username:
        reject("dsn_format")
    return raw


def fingerprint(env):
    keys = sorted(
        set(REQUIRED_FLAGS)
        | set(FORBIDDEN_SECRETS)
        | {
            "DATABASE_URL",
            "GOOGLE_CLOUD_PROJECT",
            "REAP_REHEARSAL_SQL_INSTANCE",
            "REAP_REHEARSAL_DATABASE_SECRET_REFERENCE",
        }
    )
    return hashlib.sha256("\0".join(k + "=" + env.get(k, "") for k in keys).encode()).hexdigest()


async def validate_connection(conn, mode, target=TARGET):
    row = await conn.fetchrow(
        """SELECT current_database() AS db,host(inet_server_addr()) AS host,inet_server_port() AS port,current_user AS actor,session_user AS session_actor,pg_get_userbyid(d.datdba) AS db_owner,pg_get_userbyid((SELECT nspowner FROM pg_namespace WHERE nspname='public')) AS schema_owner,has_database_privilege(current_user,current_database(),'CREATE') AS db_create,has_database_privilege(current_user,current_database(),'TEMP') AS db_temp,has_schema_privilege(current_user,'public','CREATE') AS schema_create,has_schema_privilege(current_user,'public','USAGE') AS schema_usage FROM pg_database d WHERE d.datname=current_database()"""
    )
    wanted = target.migrator if mode == "migrate" else target.runtime
    if not row or any(
        row[k] != v
        for k, v in {
            "db": target.database,
            "host": target.host,
            "port": target.port,
            "actor": wanted,
            "session_actor": wanted,
            "db_owner": target.migrator,
            "schema_owner": target.migrator,
        }.items()
    ):
        reject("server_identity")
    role = await conn.fetchrow(
        "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin FROM pg_roles WHERE rolname=current_user"
    )
    if (
        not role
        or not role["rolcanlogin"]
        or any(role[k] for k in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"))
    ):
        reject("role_privileges")
    if await conn.fetchval(
        "SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user)"
    ):
        reject("role_memberships")
    if mode == "migrate":
        if not row["db_create"] or not row["schema_create"]:
            reject("migration_ddl_privileges")
    elif row["db_create"] or row["db_temp"] or row["schema_create"] or not row["schema_usage"]:
        reject("runtime_ddl_privileges")
    if await conn.fetchval(
        "SELECT count(*) FROM pg_database WHERE datallowconn AND datname<>current_database() AND has_database_privilege(current_user,oid,'CONNECT')"
    ):
        reject("other_database_access")
    schemas = await conn.fetch(
        "SELECT nspname FROM pg_namespace WHERE left(nspname,3) <> 'pg_' AND nspname NOT IN ('public','information_schema')"
    )
    if schemas:
        reject("unexpected_schema")
    tables = await conn.fetch(
        "SELECT c.relname,c.relkind::text AS relkind,c.relrowsecurity,c.relforcerowsecurity,pg_get_userbyid(c.relowner) AS owner FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f')"
    )
    sequences = await conn.fetch("""SELECT c.relname,pg_get_userbyid(c.relowner) AS owner,t.relname AS owned_table
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=c.oid AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
        LEFT JOIN pg_class t ON t.oid=d.refobjid
        WHERE n.nspname='public' AND c.relkind='S'""")
    if mode == "runtime" and any(
        row["owner"] != target.migrator or row["owned_table"] not in TABLES for row in sequences
    ):
        reject("runtime_sequence_inventory")
    if mode == "migrate":
        if tables or sequences:
            reject("migration_requires_empty_database")
    else:
        if {r["relname"] for r in tables} != set(TABLES) | {MARKER} or any(
            r["owner"] != target.migrator or r["relkind"] != "r" or r["relrowsecurity"] or r["relforcerowsecurity"]
            for r in tables
        ):
            reject("runtime_schema_inventory")
    if await conn.fetchval(
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND left(n.nspname,3) <> 'pg_'"
    ):
        reject("unexpected_user_functions")
    if await conn.fetchval("SELECT count(*) FROM pg_event_trigger"):
        reject("unexpected_event_triggers")
    if mode == "runtime":
        marker = await conn.fetchrow(
            f"SELECT fixture_id,source_base_commit,stage,fixture_agent_id,fixture_owner_hash,fixture_buyer_id,fixture_buyer_ref FROM public.{MARKER} WHERE singleton=1"
        )
        if (
            not marker
            or marker["fixture_id"] != FIXTURE_ID
            or marker["source_base_commit"] != BASE_COMMIT
            or marker["stage"] not in {"prepared", "owned"}
            or await conn.fetchval(f"SELECT count(*) FROM public.{MARKER}") != 1
        ):
            reject("fixture_marker")
        if await conn.fetchval(
            f"SELECT has_table_privilege(current_user,'public.{MARKER}','INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
        ):
            reject("runtime_marker_write")
        for name in TABLES:
            if not await conn.fetchval("SELECT has_table_privilege(current_user,$1,'SELECT')", "public." + name):
                reject("runtime_table_read")
            write = WRITE_TABLES
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                granted = await conn.fetchval(
                    "SELECT has_table_privilege(current_user,$1,$2)", "public." + name, privilege
                )
                if granted != (name in write):
                    reject("runtime_table_write_scope")
            # No ownership/TRUNCATE/TRIGGER/REFERENCES escalation through application tables.
            if await conn.fetchval(
                "SELECT has_table_privilege(current_user,$1,'TRUNCATE,TRIGGER,REFERENCES')", "public." + name
            ):
                reject("runtime_table_admin")
        await validate_runtime_acls(conn)
        await validate_catalog_columns(conn)
        await validate_fixture_rows(conn, marker)
    return {
        "database": target.database,
        "host": target.host,
        "port": target.port,
        "actor": wanted,
        "mode": mode,
        "preparation_only": True,
        "source_base_commit": BASE_COMMIT,
        "fixture_stage": marker["stage"] if mode == "runtime" else None,
    }


def validate_acl_entries(entries):
    # PostgreSQL's aclitem expansion exposes future privilege names too (e.g. PG17 MAINTAIN).
    for entry in entries:
        allowed = (
            {"USAGE", "SELECT"}
            if entry["relkind"] == "S"
            else {"SELECT"} | ({"INSERT", "UPDATE", "DELETE"} if entry["relname"] in WRITE_TABLES else set())
        )
        if entry["privilege_type"] not in allowed or entry["is_grantable"]:
            reject("runtime_acl_admin")


async def validate_runtime_acls(conn):
    entries = await conn.fetch("""SELECT c.relname,c.relkind::text AS relkind,a.privilege_type,a.is_grantable
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault(CASE WHEN c.relkind='S' THEN 's'::"char" ELSE 'r'::"char" END,c.relowner))) a
        WHERE n.nspname='public' AND c.relkind IN ('r','S')
        AND (a.grantee=0 OR a.grantee=(SELECT oid FROM pg_roles WHERE rolname=current_user))""")
    validate_acl_entries(entries)
    column_entries = await conn.fetch("""SELECT c.relname,c.relkind::text AS relkind,a.privilege_type,a.is_grantable
        FROM pg_attribute attribute JOIN pg_class c ON c.oid=attribute.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(attribute.attacl) a
        WHERE n.nspname='public' AND (a.grantee=0 OR a.grantee=(SELECT oid FROM pg_roles WHERE rolname=current_user))""")
    # Per-column grants can bypass an otherwise read-only table ACL.
    validate_acl_entries(column_entries)


async def validate_catalog_columns(conn):
    import json
    from pathlib import Path

    expected = json.loads((Path(__file__).with_name("catalog_schema.json")).read_text())["columns"]
    actual = await conn.fetch(
        """SELECT c.relname AS table_name,a.attname AS column_name,
        format_type(a.atttypid,a.atttypmod) AS formatted_type,
        CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=ANY($1::text[]) AND a.attnum>0 AND NOT a.attisdropped""",
        list(CATALOG_TABLES),
    )
    key = lambda row: tuple(row[k] for k in ("table_name", "column_name", "formatted_type", "is_nullable"))
    if {key(row) for row in actual} != {key(row) for row in expected}:
        reject("catalog_column_contract")


async def validate_fixture_rows(conn, marker):
    """Baseline once; root-owned immutable marker binds an optional single synthetic cohort."""
    if marker["stage"] == "prepared":
        if any(
            marker[k] is not None
            for k in ("fixture_agent_id", "fixture_owner_hash", "fixture_buyer_id", "fixture_buyer_ref")
        ):
            reject("prepared_marker_scope")
        for name in (*EMPTY_TABLES, "agents", "api_keys", "agent_usage_logs"):
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM public." + name + " LIMIT 1)"):
                reject("nonfixture_state")
        return
    agent = marker["fixture_agent_id"]
    owner = marker["fixture_owner_hash"]
    buyer = marker["fixture_buyer_id"]
    ref = marker["fixture_buyer_ref"]
    if (
        not isinstance(agent, str)
        or not re.fullmatch(r"agent_reap_rehearsal_[a-z0-9_]{8,35}", agent)
        or not isinstance(owner, str)
        or not re.fullmatch(r"[0-9a-f]{64}", owner)
        or not isinstance(buyer, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", buyer)
        or not isinstance(ref, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ref)
    ):
        reject("owned_marker_scope")
    predicates = {
        "agents": ("agent_id IS DISTINCT FROM $1", (agent,)),
        "api_keys": ("agent_id IS DISTINCT FROM $1", (agent,)),
        "buyer_identity_links": (
            "agent_id IS DISTINCT FROM $1 OR agent_user_ref_hash IS DISTINCT FROM $2 OR buyer_id IS DISTINCT FROM $3",
            (agent, owner, buyer),
        ),
        "reap_agentic_buyer_refs": ("buyer_id IS DISTINCT FROM $1 OR reap_buyer_ref IS DISTINCT FROM $2", (buyer, ref)),
        "buyer_agent_links": ("buyer_id IS DISTINCT FROM $1 OR agent_id IS DISTINCT FROM $2", (buyer, agent)),
        "buyer_addresses": ("buyer_id IS DISTINCT FROM $1", (buyer,)),
        "reap_agentic_purchases": (
            "agent_id IS DISTINCT FROM $1 OR agent_user_ref_hash IS DISTINCT FROM $2 OR buyer_ref IS DISTINCT FROM $3",
            (agent, owner, ref),
        ),
        "reap_agentic_enrollments": ("agent_id IS DISTINCT FROM $1 OR buyer_ref IS DISTINCT FROM $2", (agent, ref)),
        "reap_agentic_purchase_keys": (
            "agent_id IS DISTINCT FROM $1 OR agent_user_ref_hash IS DISTINCT FROM $2 OR (purchase_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM reap_agentic_purchases p WHERE p.id=reap_agentic_purchase_keys.purchase_id))",
            (agent, owner),
        ),
        "surface_click_events": (
            "agent_id IS DISTINCT FROM $1 OR NOT EXISTS(SELECT 1 FROM reap_agentic_purchases p WHERE p.click_id=surface_click_events.click_id)",
            (agent,),
        ),
        "commerce_attribution_edges": (
            "agent_id IS DISTINCT FROM $1 OR NOT EXISTS(SELECT 1 FROM reap_agentic_purchases p WHERE p.click_id=commerce_attribution_edges.click_id AND p.reap_order_id=commerce_attribution_edges.external_order_id)",
            (agent,),
        ),
        "commerce_interactions": (
            "agent_id IS DISTINCT FROM $1 OR NOT EXISTS(SELECT 1 FROM reap_agentic_purchases p WHERE p.click_id=commerce_interactions.click_id)",
            (agent,),
        ),
        "commerce_interaction_events": (
            "NOT EXISTS(SELECT 1 FROM commerce_interactions i WHERE i.interaction_id=commerce_interaction_events.interaction_id) OR payload->>'agent_id' IS DISTINCT FROM $1",
            (agent,),
        ),
        "conversion_click_claims": (
            "claimed_by IS DISTINCT FROM 'reap_agentic' OR NOT EXISTS(SELECT 1 FROM reap_agentic_purchases p WHERE p.click_id=conversion_click_claims.click_id AND p.reap_order_id=conversion_click_claims.external_order_id)",
            (),
        ),
    }
    for name, (where, args) in predicates.items():
        if await conn.fetchval("SELECT count(*) FROM " + name) > 1 or await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM " + name + " WHERE " + where + " LIMIT 1)", *args
        ):
            reject("nonfixture_state")
    if await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM agent_usage_logs WHERE agent_id IS DISTINCT FROM $1 LIMIT 1)", agent
    ):
        reject("nonfixture_state")


async def run_guard(mode, env=None, target=TARGET, connect=None):
    global _receipt
    env = os.environ if env is None else env
    expected_secret = "reap-rehearsal-20261002-01a0f-" + ("migrator" if mode == "migrate" else "runtime") + "-dsn"
    reference = env.get("REAP_REHEARSAL_DATABASE_SECRET_REFERENCE", "")
    if not re.fullmatch("projects/pivota-staging/secrets/" + expected_secret + r"/versions/[1-9][0-9]*", reference):
        reject("numeric_secret_binding")
    validate_environment(env)
    dsn = validate_dsn(env.get("DATABASE_URL"), mode, target)
    if connect is None:
        import asyncpg

        connect = asyncpg.connect
    conn = None
    try:
        conn = await connect(
            dsn,
            timeout=5,
            command_timeout=5,
            server_settings={
                "statement_timeout": "3000",
                "lock_timeout": "1000",
                "idle_in_transaction_session_timeout": "5000",
            },
        )
        async with conn.transaction(readonly=True):
            result = await validate_connection(conn, mode, target)
    except GuardRejected:
        raise
    except Exception:
        reject("database_probe_failed")
    finally:
        if conn is not None:
            await conn.close()
    if mode == "runtime":
        _receipt = (time.monotonic(), fingerprint(env), result)
    return result


def require_runtime_receipt():
    if _receipt is None or time.monotonic() - _receipt[0] > 30 or _receipt[1] != fingerprint(os.environ):
        reject("missing_or_stale_runtime_guard")
    return _receipt[2]


def install_database_only_egress(target=TARGET):
    """Process-local TCP/DNS/UDP deny before route imports, not a replacement for VPC policy."""
    original_connect = socket.socket.connect
    original_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_dns = socket.getaddrinfo

    def check(address, family):
        if family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple) or len(address) < 2:
            reject("egress_denied")
        try:
            ip = ipaddress.ip_address(address[0])
        except ValueError:
            reject("egress_denied")
        if str(ip) != target.host or address[1] != target.port:
            reject("egress_denied")

    def connect(s, address):
        check(address, s.family)
        return original_connect(s, address)

    def connect_ex(s, address):
        check(address, s.family)
        return original_ex(s, address)

    def sendto(s, data, *args):
        check(args[-1], s.family)
        return original_sendto(s, data, *args)

    def dns(host, port, *args, **kwargs):
        if host is None:
            return original_dns(host, port, *args, **kwargs)
        check((host, int(port)), socket.AF_INET)
        return original_dns(host, port, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = dns
