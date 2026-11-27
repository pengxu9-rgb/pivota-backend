"""The catalog export's prod role, per run (role design r3, R3 and R6). Run as the catalog tables'
OWNER (the app's DATABASE_URL), as a gated prod one-off through the PR controller:

    python -m jobs.sandbox_catalog_export_role grant     # R3: before a run
    python -m jobs.sandbox_catalog_export_role revoke    # R6 step 2: after a run, always

The role itself (R1), its password and LOGIN (R4), and NOLOGIN plus ending its sessions (R6 step 1) are
the owner's own psql steps; this module never sees a credential.

grant: ONE transaction. Pre-reads B (the role reads nothing yet: the last run's R6 finished) and C (no
PUBLIC SELECT on a catalog table), then check 0 (this connection owns every present catalog table, so
its GRANT is real and the later REVOKE removes it), then SELECT on each present catalog table, then
checks 1 to 10. ANY failed check rolls the whole transaction back, so the role is left with nothing,
and the exit code is 3. Checks 4 and 5b are printed for a person to read; they do not fail.

revoke: REVOKE SELECT ON ALL TABLES IN SCHEMA <schema>, committed, then the hard re-read: nothing
readable (table- or column-level, any schema), no session, LOGIN off. Any failure is exit 3 (the
REVOKE stays committed): the next run is blocked until it is explained.

Prints names and counts only, never a value. Exit 0 = done, 3 = STOP (read the lines), 2 = bad usage.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import List, Optional, Sequence, Tuple

ROLE = "sandbox_catalog_export"
SCHEMA = "public"

_RELKINDS = "('r', 'p', 'v', 'm', 'S', 'f')"
_USER_SCHEMAS = "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'"


class Stop(Exception):
    """A check failed: the transaction is rolled back (grant) and the run is blocked."""


def _say(line: str) -> None:
    print(line, flush=True)


async def _rows(conn, label: str, sql: str, *args) -> List[Tuple]:
    rows = [tuple(r) for r in await conn.fetch(sql, *args)]
    _say("%s rows=%d%s" % (label, len(rows), "" if not rows else " " + "; ".join(" ".join(map(str, r)) for r in rows[:20])))
    return rows


async def _must_be_empty(conn, label: str, sql: str, *args) -> None:
    if await _rows(conn, label, sql, *args):
        raise Stop(label)


def _readable_sql() -> str:
    """Every relation the role can read, table- or column-level (PUBLIC grants included)."""
    return ("SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE %s AND c.relkind IN %s AND (has_table_privilege($1, c.oid, 'SELECT') "
            "OR has_any_column_privilege($1, c.oid, 'SELECT'))" % (_USER_SCHEMAS, _RELKINDS))


async def grant(conn, tables: Sequence[str], *, role: str = ROLE, schema: str = SCHEMA,
                require_pg17: bool = True) -> None:
    pg17 = int(await conn.fetchval("SHOW server_version_num")) >= 170000
    if require_pg17 and not pg17:
        raise Stop("server is older than PostgreSQL 17 (checks 2, 7 and 8b need it)")
    names = list(tables)
    if not await conn.fetchval("SELECT count(*) FROM pg_roles WHERE rolname = $1", role):
        raise Stop("role %s does not exist (R1 first)" % role)
    async with conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = '30s'")
        await conn.execute("SET LOCAL lock_timeout = '2s'")
        present = [r[0] for r in await conn.fetch(
            "SELECT t FROM unnest($1::text[]) AS t WHERE to_regclass(format('%I.%I', $2::text, t)) IS NOT NULL",
            names, schema)]
        _say("PRESENT catalog_tables=%d of=%d" % (len(present), len(names)))
        # B. the previous run's R6 finished: the role reads nothing before this run grants
        await _must_be_empty(conn, "PRE_B readable_before_grant", _readable_sql(), role)
        # C. no PUBLIC SELECT on a catalog table (check 8 skips them; R6 would fail every run)
        await _must_be_empty(
            conn, "PRE_C public_select_on_catalog",
            "SELECT t FROM unnest($1::text[]) AS t WHERE to_regclass(format('%I.%I', $2::text, t)) IS NOT NULL "
            "AND (has_table_privilege('public', format('%I.%I', $2::text, t), 'SELECT') "
            "OR has_any_column_privilege('public', format('%I.%I', $2::text, t), 'SELECT'))", present, schema)
        # 0. this connection owns every present catalog table: its GRANT is real, its REVOKE will be
        await _must_be_empty(
            conn, "CHECK_0 not_owned_by_current_user",
            "SELECT t, pg_get_userbyid(c.relowner) FROM unnest($1::text[]) AS t "
            "JOIN pg_class c ON c.oid = to_regclass(format('%I.%I', $2::text, t)) "
            "WHERE c.relowner <> current_user::regrole", present, schema)
        for name in present:
            await conn.execute('GRANT SELECT ON "%s"."%s" TO "%s"' % (
                schema.replace('"', '""'), name.replace('"', '""'), role.replace('"', '""')))
        _say("GRANTED select_on=%d" % len(present))
        await _checks(conn, role, schema, names, present, pg17)
    _say("GRANT_DONE role=%s tables=%d" % (role, len(present)))


async def _checks(conn, role: str, schema: str, names: Sequence[str], present: Sequence[str], pg17: bool) -> None:
    await _must_be_empty(
        conn, "CHECK_1 membership_or_attribute",
        "SELECT 'member of ' || b.rolname FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid "
        "WHERE m.member = $1::text::regrole "
        "UNION ALL SELECT 'attribute' FROM pg_roles WHERE rolname = $1 "
        "AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)", role)
    write = "INSERT, UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES" + (", MAINTAIN" if pg17 else "")
    await _must_be_empty(
        conn, "CHECK_2 owns_or_may_write",
        "SELECT c.oid::regclass::text, c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE %s AND c.relkind IN %s AND (c.relowner = $1::text::regrole "
        "OR has_table_privilege($1, c.oid, '%s') "
        "OR has_any_column_privilege($1, c.oid, 'INSERT, UPDATE, REFERENCES') "
        "OR (c.relkind = 'S' AND has_sequence_privilege($1, c.oid, 'USAGE, UPDATE')))" % (_USER_SCHEMAS, _RELKINDS, write),
        role)
    await _must_be_empty(
        conn, "CHECK_3 may_create",
        "SELECT 'CREATE on schema ' || nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
        "AND nspname <> 'information_schema' AND has_schema_privilege($1, oid, 'CREATE') "
        "UNION ALL SELECT 'CREATE on database' WHERE has_database_privilege($1, current_database(), 'CREATE')", role)
    await _rows(  # a human read
        conn, "READ_4 security_definer_executable",
        "SELECT p.oid::regprocedure::text, pg_get_userbyid(p.proowner) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.prosecdef "
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND has_function_privilege($1, p.oid, 'EXECUTE')", role)
    await _must_be_empty(
        conn, "CHECK_5 role_settings",
        "SELECT setdatabase::text, setconfig::text FROM pg_db_role_setting WHERE setrole = $1::text::regrole "
        "AND setconfig <> ARRAY['default_transaction_read_only=on']", role)
    await _rows(  # a human read
        conn, "READ_5B database_settings",
        "SELECT setconfig::text FROM pg_db_role_setting WHERE setrole = 0 "
        "AND setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())")
    await _must_be_empty(
        conn, "CHECK_6 owns_anything",
        "SELECT classid::regclass::text, objid::text FROM pg_shdepend WHERE refobjid = $1::text::regrole AND deptype = 'o' "
        "UNION ALL SELECT 'pg_namespace', oid::text FROM pg_namespace WHERE nspowner = $1::text::regrole "
        "UNION ALL SELECT 'pg_proc', oid::text FROM pg_proc WHERE proowner = $1::text::regrole "
        "UNION ALL SELECT 'pg_type', oid::text FROM pg_type WHERE typowner = $1::text::regrole "
        "UNION ALL SELECT 'pg_largeobject_metadata', oid::text FROM pg_largeobject_metadata WHERE lomowner = $1::text::regrole",
        role)
    if pg17:
        await _must_be_empty(
            conn, "CHECK_7 large_object_write",
            "SELECT 'large object ' || oid FROM pg_largeobject_metadata WHERE has_largeobject_privilege($1, oid, 'UPDATE') "
            "UNION ALL SELECT 'lo_compat_privileges=on' WHERE current_setting('lo_compat_privileges')::boolean", role)
        await _must_be_empty(
            conn, "CHECK_8B large_object_read",
            "SELECT oid::text FROM pg_largeobject_metadata WHERE has_largeobject_privilege($1, oid, 'SELECT')", role)
    else:
        await _must_be_empty(conn, "CHECK_7 lo_compat_privileges (large-object ACLs need PG17: SKIPPED)",
                             "SELECT 'lo_compat_privileges=on' WHERE current_setting('lo_compat_privileges')::boolean")
        _say("CHECK_8B SKIPPED (needs PG17)")
    await _must_be_empty(
        conn, "CHECK_8 readable_outside_the_list",
        _readable_sql() + " AND NOT (n.nspname = $2 AND c.relname = ANY ($3::text[]))", role, schema, list(names))
    await _must_be_empty(
        conn, "CHECK_8C catalog_entry_not_a_table",
        "SELECT t, c.relkind::text FROM unnest($1::text[]) AS t "
        "JOIN pg_class c ON c.oid = to_regclass(format('%I.%I', $2::text, t)) WHERE c.relkind NOT IN ('r', 'p')",
        list(names), schema)
    await _must_be_empty(
        conn, "CHECK_9 catalog_table_not_readable",
        "SELECT t FROM unnest($1::text[]) AS t WHERE NOT has_table_privilege($3, format('%I.%I', $2::text, t), 'SELECT')",
        list(present), schema, role)
    await _must_be_empty(
        conn, "CHECK_10 row_level_security",
        "SELECT t FROM unnest($1::text[]) AS t JOIN pg_class c ON c.oid = to_regclass(format('%I.%I', $2::text, t)) "
        "WHERE c.relrowsecurity OR c.relforcerowsecurity", list(present), schema)


async def revoke(conn, *, role: str = ROLE, schema: str = SCHEMA) -> None:
    if not await conn.fetchval("SELECT count(*) FROM pg_roles WHERE rolname = $1", role):
        raise Stop("role %s does not exist" % role)
    async with conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = '30s'")
        await conn.execute("SET LOCAL lock_timeout = '2s'")
        await conn.execute('REVOKE SELECT ON ALL TABLES IN SCHEMA "%s" FROM "%s"' % (
            schema.replace('"', '""'), role.replace('"', '""')))
    _say("REVOKED schema=%s" % schema)
    failed: List[str] = []
    if await _rows(conn, "REREAD readable", _readable_sql(), role):
        failed.append("readable")  # a grant from another grantor survives this REVOKE
    if await _rows(conn, "REREAD sessions", "SELECT pid::text FROM pg_stat_activity WHERE usename = $1", role):
        failed.append("sessions")
    if await conn.fetchval("SELECT rolcanlogin FROM pg_roles WHERE rolname = $1", role):
        _say("REREAD rolcanlogin=true")
        failed.append("login")
    else:
        _say("REREAD rolcanlogin=false")
    if failed:
        raise Stop("after the REVOKE: " + ", ".join(failed))
    _say("REVOKE_DONE role=%s" % role)


async def _run(mode: str, dsn: str) -> None:
    import asyncpg

    from jobs.sandbox_catalog_copy import CATALOG_TABLES

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        if mode == "grant":
            await grant(conn, [s.name for s in CATALOG_TABLES])
        else:
            await revoke(conn)
    finally:
        await conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.sandbox_catalog_export_role")
    parser.add_argument("mode", choices=("grant", "revoke"))
    args = parser.parse_args(argv)
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        _say("STOP DATABASE_URL is not set")
        return 3
    try:
        asyncio.run(_run(args.mode, dsn))
    except Stop as exc:
        _say("STOP %s (%s)" % (exc, "rolled back: the role holds nothing new" if args.mode == "grant"
                               else "the REVOKE is committed; the next run is blocked"))
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
