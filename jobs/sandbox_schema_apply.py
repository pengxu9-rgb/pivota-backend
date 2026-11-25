"""Apply prod's schema (no rows) to the EMPTY sandbox database. Q15, runbook 10a (sandbox half).

    python -m jobs.sandbox_schema_apply            # runs in the SANDBOX, once, before anything else

The prod half is scripts/sandbox/dump_prod_schema.sh, which writes
``<prefix>/<stamp>/schema.sql.gz``, ``ledgers.sql.gz`` and ``_MANIFEST.json`` (last) into the drop.

Env: PIVOTA_ENV=sandbox, SANDBOX_SCHEMA_GCS_URI (gs://<bucket>/<prefix>), DATABASE_URL (the sandbox's).

REFUSES TO RUN unless:
  * PIVOTA_ENV=sandbox and config.sandbox_guard passes on this environment;
  * DATABASE_URL is positively the sandbox's (config.sandbox_identity.sandbox_database_refusal);
  * the database is EMPTY: no table in any non-system schema. This job creates a schema once; it is
    never a migration tool, and it never touches a populated database.

THE GATE IS THE ROLE. The apply runs as a dedicated role that is not a superuser, cannot create roles or
databases, is a member of no role, has no CREATE on the database and owns nothing yet (role_refusal(); the
bootstrap SQL is in docs/sandbox/schema_bootstrap.md). Whatever the dump makes the server execute runs as
that role. A statement-kind allowlist cannot be the gate: a view WITH DATA or a trigger calls a function.

IT DOES NOT TRUST THE DUMP EITHER. Before anything runs it checks the manifest (kind, version, stamp), every
file's sha256, the dump's pg_dump major version against this server's, and every statement with
PostgreSQL's own parser (pglast): an allowlist of parse-tree node types with their fields checked, pg_dump's
exact preamble SETs only, plpgsql/sql functions only, no ownership change, no CREATE TABLE AS or
materialized view WITH DATA, no rule, no trigger on a ledger table, no extension that is not already
installed, no literal that looks like a secret or a person. The ledger file must be exactly the two
explicit-column INSERT shapes. Each statement is executed alone (extended protocol).

Then, in ONE transaction with the apply's own lock/statement/idle timeouts: the schema; a check that no
ledger table can run code on INSERT; standard_conforming_strings re-asserted; the ledger rows. After it, the sandbox's base-table count
must equal the source's live count and the ledger row count the manifest's, or everything rolls
back. The ledgers come along so the gateway's `migrate` (runbook 10b) applies only what prod lacks
instead of replaying 71 migrations over tables that already exist. No startup_sql_migrations row is
invented: the sandbox's ledger is prod's, as prod has it.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from jobs.sandbox_catalog_copy import CopyRefused, GcsStore, LocalDirStore, newest_complete_stamp  # noqa: F401

FORMAT_VERSION = 2
MANIFEST_NAME = "_MANIFEST.json"
_STAMP_RE = re.compile(r"^\d{8}T\d{4}Z$")
LEDGER_TABLES = ("startup_sql_migrations", "schema_migrations")
LEDGER_TRAILER = "-- sandbox ledger dump complete"


class SchemaRefused(RuntimeError):
    """The schema apply will not run. Messages name rules and stamps, never values."""


# --- the parser ----------------------------------------------------------------------------------
#
# Every statement is read by PostgreSQL's OWN parser and scanner (pglast, libpg_query at the server's
# major version), never by a text tokenizer: \r ending a -- comment, `$` and bytes >= 0x80 inside
# identifiers, non-ASCII dollar tags and U&"..." are read exactly as the server reads them. The allowlist
# is over parse-tree NODE TYPES, with each node's own fields checked. Each kept statement is then executed
# ALONE through the extended protocol, which refuses a string holding more than one command, so the
# server cannot run more statements than were checked.
#
# This is the second layer. A statement kind cannot limit what runs at apply time (a view WITH DATA or a
# trigger can call a function), so the FIRST layer is the role the apply runs as: see role_refusal().
#
# pglast is imported lazily, here only: main, the web and the worker never load it (tested).

_META_LINE = re.compile(r"^\\(restrict|unrestrict) [A-Za-z0-9]+$", re.M)
#: pg_dump's preamble and per-object SETs, exactly (as the parser deparses them). The timeouts and the
#: two settings this job forces itself are CHECKED but not executed: the apply sets its own (D-1).
_SET_EXECUTED = (
    "SET check_function_bodies TO false", "SET xmloption TO content", "SET client_min_messages TO warning",
    "SET row_security TO off", "SET default_tablespace TO ''", "SET default_table_access_method TO heap",
    "SELECT pg_catalog.set_config('search_path', '', FALSE)",
)
_SET_SKIPPED = (
    "SET statement_timeout TO 0", "SET lock_timeout TO 0", "SET idle_in_transaction_session_timeout TO 0",
    "SET transaction_timeout TO 0", "SET client_encoding TO 'UTF8'", "SET standard_conforming_strings TO on",
)
#: Languages a function may be written in. Anything else (c, internal, plpython3u, a quoted spelling of
#: either) is refused; the apply role could not create one anyway.
_TRUSTED_LANGUAGES = frozenset({"plpgsql", "sql"})
#: DefineStmt kinds pg_dump emits that need no C code.
_DEFINE_KINDS = ("OBJECT_AGGREGATE", "OBJECT_OPERATOR", "OBJECT_COLLATION", "OBJECT_TSCONFIGURATION",
                 "OBJECT_TSDICTIONARY")
#: ALTER TABLE / INDEX / SEQUENCE / VIEW subcommands pg_dump --schema-only emits. OWNER TO is not one.
_ALTER_TABLE_CMDS = frozenset({
    "AT_AddConstraint", "AT_ColumnDefault", "AT_AddIdentity", "AT_SetIdentity", "AT_SetNotNull",
    "AT_ClusterOn", "AT_AttachPartition", "AT_EnableRowSecurity", "AT_ForceRowSecurity",
    "AT_ReplicaIdentity", "AT_SetRelOptions", "AT_SetOptions", "AT_SetStatistics", "AT_SetStorage",
    "AT_SetCompression", "AT_DisableTrig", "AT_EnableAlwaysTrig", "AT_EnableReplicaTrig", "AT_AddInherit",
})
_ALTER_TABLE_OBJECTS = ("OBJECT_TABLE", "OBJECT_INDEX", "OBJECT_SEQUENCE", "OBJECT_VIEW", "OBJECT_MATVIEW")
#: Kinds with no field that can carry a forbidden action.
_PLAIN_KINDS = frozenset({
    "CreateStmt", "CreateSeqStmt", "AlterSeqStmt", "CreateDomainStmt", "AlterDomainStmt", "CreateEnumStmt",
    "AlterEnumStmt", "CompositeTypeStmt", "CreateRangeStmt", "ViewStmt", "IndexStmt", "CreatePolicyStmt",
    "CreateStatsStmt", "CreateCastStmt",
})
#: The two ledger INSERT shapes: table -> its explicit columns.
_LEDGER_COLUMNS = {"schema_migrations": ("id", "applied_at"),
                   "startup_sql_migrations": ("filename", "checksum", "applied_at")}
#: Text that looks like a secret or a person, searched in each statement as the PARSER deparses it (so no
#: comment, and every literal and function body included). Same rule as the dump's (D-5 widened).
_SECRET_LITERAL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}"      # an email
    r"|\b(sk|rk|ak|pk)_(live|test)_[A-Za-z0-9]|\bsk-[A-Za-z0-9_-]{16,}"  # Stripe, Pivota, OpenAI/Anthropic
    r"|shpat_|ghp_|github_pat_|xox[abposr]-|whsec_"                    # Shopify, GitHub, Slack, webhooks
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}"                          # AWS, Google API keys
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."                       # a JWT
    r"|://[^/\s:@']+:[^@\s/']+@|\bpassword\s*=\s*[^\s,;')]"              # a DSN or conninfo password
    r"|\+[1-9][0-9]{7,14}\b|\(?\b[0-9]{3}\)?[ .-][0-9]{3}[ .-][0-9]{4}\b"  # a phone number
    r"|[0-9a-fA-F]{32,}|[A-Za-z0-9+/]{40,}={0,2}"                       # long hex / base64
)


def _pglast():
    import pglast  # noqa: F401 - lazy: only this job needs the parser
    from pglast import enums, parse_sql
    from pglast.parser import ParseError, scan
    from pglast.stream import RawStream

    return parse_sql, scan, RawStream, ParseError, enums


_PREAMBLE: Optional[Tuple[FrozenSet[str], FrozenSet[str]]] = None


def _preamble() -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """(SETs executed, SETs checked but skipped), each as the parser prints it."""
    global _PREAMBLE
    if _PREAMBLE is None:
        parse_sql = _pglast()[0]
        printed = lambda texts: frozenset(_deparse(parse_sql(t)[0].stmt) for t in texts)  # noqa: E731
        _PREAMBLE = (printed(_SET_EXECUTED), printed(_SET_SKIPPED))
    return _PREAMBLE


def _deparse(node) -> str:
    _, _, RawStream, _, _ = _pglast()
    try:
        return RawStream()(node)
    except Exception:  # noqa: BLE001 - a node the deparser cannot print cannot be checked
        raise SchemaRefused("a %s statement cannot be printed back for checking" % type(node).__name__) from None


def _parse(text: str, what: str):
    """[(statement node, its exact text)] for ``text``. pg_dump's top-level \\restrict/\\unrestrict lines
    are blanked first; the SCANNER decides what is top level, so the same text inside a body is left alone."""
    parse_sql, scan, _, ParseError, _ = _pglast()
    if "\x00" in text or "\ufeff" in text:
        raise SchemaRefused("%s carries a NUL byte or a byte-order mark" % what)
    if re.search(r"\r(?!\n)", text):
        raise SchemaRefused("%s carries a bare carriage return" % what)
    # pg_dump's \\restrict line carries a RANDOM key, and a key like `9abc...` is "trailing junk after a
    # numeric literal" to the scanner. So each meta-shaped line becomes a same-length SQL comment first,
    # and the scanner must then see that comment exactly there: a meta line inside a literal or a body
    # would instead be swallowed by that token, and is refused.
    metas = [(m.start(), m.end()) for m in _META_LINE.finditer(text)]
    if len(metas) > 2:
        raise SchemaRefused("%s carries more than one \\restrict/\\unrestrict pair" % what)
    blanked = text
    for a, b in metas:  # same length: every offset the parser reports still points into ``text``
        blanked = blanked[:a] + "--" + " " * (b - a - 2) + blanked[b:]
    try:
        tokens = scan(blanked)
    except ParseError as exc:
        raise SchemaRefused("%s does not scan as PostgreSQL (at offset %s)" % (what, getattr(exc, "location", "?"))) from None
    comment_starts = {t.start for t in tokens if t.name == "SQL_COMMENT"}
    if any(a not in comment_starts for a, _ in metas):
        raise SchemaRefused("%s has a psql meta-command inside a statement" % what)
    if any(t.name == "ASCII_92" for t in tokens):  # a backslash outside every literal, body and comment
        raise SchemaRefused("%s carries a psql meta-command other than \\restrict/\\unrestrict" % what)
    try:
        statements = parse_sql(blanked)
    except ParseError as exc:
        raise SchemaRefused("%s does not parse as PostgreSQL (at offset %s)" % (what, getattr(exc, "location", "?"))) from None
    real = [t for t in tokens if t.name not in ("SQL_COMMENT", "C_COMMENT")]
    if real and real[-1].name != "ASCII_59":
        raise SchemaRefused("%s ends with a statement that has no ';'" % what)
    out = []
    for raw in statements:
        loc = raw.stmt_location or 0
        sql = blanked[loc:loc + raw.stmt_len] if raw.stmt_len else blanked[loc:]
        out.append((raw.stmt, sql.strip().rstrip(";").strip()))
    return out


def _refusal(node, enums) -> Optional[str]:
    """Why ``node`` may not be in a schema-only dump, or None."""
    kind = type(node).__name__
    if kind in _PLAIN_KINDS:
        return None
    if kind == "CreateSchemaStmt":
        if node.schemaElts or node.authrole is not None:
            return "a CREATE SCHEMA that carries elements or AUTHORIZATION"
        return None
    if kind == "CreateExtensionStmt":
        if any(o.defname != "schema" for o in (node.options or ())):
            return "a CREATE EXTENSION option other than SCHEMA"
        return None
    if kind == "CreateFunctionStmt":
        language = next((o.arg.sval for o in (node.options or ()) if o.defname == "language"), None)
        if language is None and node.sql_body is not None:
            language = "sql"
        if language not in _TRUSTED_LANGUAGES:
            return "a function in a language other than plpgsql or sql"
        return None
    if kind == "DefineStmt":
        return None if enums.ObjectType(node.kind).name in _DEFINE_KINDS else "a CREATE TYPE / %s needing C code" % (
            enums.ObjectType(node.kind).name)
    if kind == "CreateTableAsStmt":
        if enums.ObjectType(node.objtype).name == "OBJECT_MATVIEW" and node.into.skipData:
            return None
        return "a CREATE TABLE AS or a materialized view WITH DATA (it runs its query at apply time)"
    if kind == "CreateTrigStmt":
        if (node.relation.schemaname in (None, "public")) and node.relation.relname in _LEDGER_COLUMNS:
            return "a trigger on a ledger table"
        return None
    if kind == "AlterTableStmt":
        if enums.ObjectType(node.objtype).name not in _ALTER_TABLE_OBJECTS:
            return "an ALTER of a %s" % enums.ObjectType(node.objtype).name
        for cmd in node.cmds:
            sub = enums.AlterTableType(cmd.subtype).name
            if sub == "AT_ChangeOwner":
                return "an ownership change"
            if sub not in _ALTER_TABLE_CMDS:
                return "an ALTER TABLE %s" % sub
        return None
    if kind == "AlterOwnerStmt":
        return "an ownership change"
    return "a statement pg_dump --schema-only does not emit (%s)" % kind


@dataclass(frozen=True)
class SchemaPlan:
    statements: Tuple[str, ...]  # executed in order, each alone
    extensions: FrozenSet[str]   # must already be installed by the admin: the dump never creates one
    checked: int                 # statements read, executed or not


def validate_schema_sql(text: str) -> SchemaPlan:
    """What may be executed from the schema file, or SchemaRefused."""
    if "PostgreSQL database dump complete" not in text[-400:]:
        raise SchemaRefused("schema file has no completion trailer")
    enums = _pglast()[4]
    executed, skipped = _preamble()
    keep: List[str] = []
    extensions = set()
    parsed = _parse(text, "schema file")
    for node, sql in parsed:
        kind = type(node).__name__
        if kind not in ("VariableSetStmt", "SelectStmt"):
            why = _refusal(node, enums)  # by type first: a refused kind need not be printable
            if why:
                raise SchemaRefused("schema file carries %s" % why)
        printed = _deparse(node)
        if kind in ("VariableSetStmt", "SelectStmt"):
            if printed in skipped:
                continue
            if printed not in executed:
                raise SchemaRefused("schema file carries a SET or SELECT outside pg_dump's preamble (%s)"
                                    % " ".join(printed.split()[:2]))
        if _SECRET_LITERAL.search(printed):
            raise SchemaRefused("schema file carries a literal that looks like a secret or a person")
        if kind == "CreateExtensionStmt":
            extensions.add(node.extname)
        keep.append(sql)
    return SchemaPlan(tuple(keep), frozenset(extensions), len(parsed))


def validate_ledgers_sql(text: str) -> Tuple[Tuple[str, ...], int]:
    """(ledger INSERTs, each executed alone; their count), or SchemaRefused."""
    if text.rstrip("\n").splitlines()[-1:] != [LEDGER_TRAILER]:
        raise SchemaRefused("ledger file has no completion trailer")
    keep: List[str] = []
    for node, sql in (_parse(text, "ledger file") if text.strip() != LEDGER_TRAILER else []):
        if not _is_ledger_insert(node):
            raise SchemaRefused("ledger file carries a statement other than an explicit-column ledger INSERT")
        keep.append(sql)
    return tuple(keep), len(keep)


def _is_ledger_insert(node) -> bool:
    if type(node).__name__ != "InsertStmt" or node.withClause or node.onConflictClause or node.returningList:
        return False
    if node.override or node.relation.schemaname != "public":
        return False
    columns = _LEDGER_COLUMNS.get(node.relation.relname)
    if columns is None or tuple(c.name for c in (node.cols or ())) != columns:
        return False
    if any(c.indirection for c in node.cols):
        return False
    select = node.selectStmt
    if type(select).__name__ != "SelectStmt" or not select.valuesLists or len(select.valuesLists) != 1:
        return False
    row = select.valuesLists[0]
    # The dump's own line rule: no quote, semicolon or backslash inside a ledger value.
    return len(row) == len(columns) and all(
        type(v).__name__ == "A_Const" and (v.isnull or (type(v.val).__name__ == "String"
                                                        and not any(ch in v.val.sval for ch in "';\\")))
        for v in row)


async def _user_table_count(conn) -> int:
    """Tables and partitioned tables outside the system schemas. From pg_class, which every role can read:
    information_schema shows only what the CURRENT role may access, so the apply role would not see a
    table someone else created."""
    return int(await conn.fetchval(
        """
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
        """
    ))


def _load(store, stamp: Optional[str]) -> Tuple[str, Dict[str, Any], str, str]:
    stamp = stamp or newest_complete_stamp(store)
    if not stamp or not _STAMP_RE.match(stamp):
        raise SchemaRefused("no complete schema dump (no stamp with a manifest) in the store")
    manifest = json.loads(store.get("%s/%s" % (stamp, MANIFEST_NAME)).decode("utf-8"))
    if manifest.get("kind") != "schema" or manifest.get("format_version") != FORMAT_VERSION:
        raise SchemaRefused("manifest is not a format-%d schema manifest" % FORMAT_VERSION)
    if manifest.get("stamp") != stamp:
        raise SchemaRefused("manifest stamp does not match its folder")
    texts = {}
    for part in ("schema", "ledgers"):
        entry = (manifest.get("files") or {}).get(part) or {}
        if entry.get("file") != "%s/%s.sql.gz" % (stamp, part):
            raise SchemaRefused("manifest %s file points outside its stamp" % part)
        data = store.get(entry["file"])
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise SchemaRefused("%s file sha256 does not match the manifest" % part)
        texts[part] = gzip.decompress(data).decode("utf-8")
    return stamp, manifest, texts["schema"], texts["ledgers"]


#: The apply's own caps (D-1). The dump's zero settings are checked and skipped, so nothing resets these.
APPLY_LOCK_TIMEOUT = "10s"
APPLY_STATEMENT_TIMEOUT = "120s"
APPLY_IDLE_TIMEOUT = "60s"
_CAPS_SQL = ("SELECT current_setting('lock_timeout') AS lock_timeout, current_setting('statement_timeout') AS st, "
             "current_setting('idle_in_transaction_session_timeout') AS idle, "
             "current_setting('standard_conforming_strings') AS scs")

_ROLE_SQL = """
SELECT r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolreplication, r.rolbypassrls,
       current_user = session_user AS same_user,
       EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) AS has_membership,
       has_database_privilege(current_database(), 'CREATE') AS can_create_schema,
       (EXISTS (SELECT 1 FROM pg_class WHERE relowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_proc WHERE proowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_type WHERE typowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_extension WHERE extowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_database WHERE datdba = r.oid)) AS owns_something
  FROM pg_roles r WHERE r.rolname = current_user
"""
_ROLE_RULES = (
    ("rolsuper", "is a superuser"), ("rolcreaterole", "can create roles"), ("rolcreatedb", "can create databases"),
    ("rolreplication", "has REPLICATION"), ("rolbypassrls", "bypasses row security"),
    ("has_membership", "is a member of another role (cloudsqlsuperuser, the app role, pg_*)"),
    ("can_create_schema", "has CREATE on the database"),
    ("owns_something", "already owns objects (a rerun is refused, never merged into)"),
)


async def role_refusal(conn) -> Optional[str]:
    """Why the connected role may not run the apply, or None. THE gate: whatever the dump makes the server
    execute runs as this role, which can create objects in public and nothing else."""
    row = await conn.fetchrow(_ROLE_SQL)
    if row is None:
        return "the apply role is not in pg_roles"
    if not row["same_user"]:
        return "the apply role is not the session user (SET ROLE / SESSION AUTHORIZATION in effect)"
    for column, why in _ROLE_RULES:
        if row[column]:
            return "the apply role %s" % why
    return None


def _major(version: Any) -> Optional[int]:
    m = re.match(r"\s*([0-9]+)", str(version or ""))
    return int(m.group(1)) if m else None


async def apply_schema(conn, store, *, stamp: Optional[str] = None) -> Dict[str, Any]:
    stamp, manifest, schema_text, ledger_text = _load(store, stamp)
    plan = validate_schema_sql(schema_text)
    ledger_sql, ledger_rows = validate_ledgers_sql(ledger_text)
    if ledger_rows != manifest.get("ledger_rows"):
        raise SchemaRefused("ledger file row count does not match the manifest")
    refusal = await role_refusal(conn)
    if refusal:
        raise SchemaRefused(refusal)
    server = int(await conn.fetchval("SHOW server_version_num")) // 10000
    if _major(manifest.get("pg_dump_version")) != server:
        raise SchemaRefused("the dump's pg_dump major version is not this server's (%s)" % server)
    if await _user_table_count(conn) != 0:
        raise SchemaRefused("the sandbox database is not empty; this job only creates a schema once")
    installed = {r["extname"] for r in await conn.fetch("SELECT extname FROM pg_extension")}
    missing = sorted(plan.extensions - installed)
    if missing:
        raise SchemaRefused("extensions are pre-created by the admin, never by the dump; missing: %s"
                            % ", ".join(missing))
    # pg_dump's preamble empties search_path for the SESSION (set_config(..., false)); put it back,
    # so the connection is handed back as it was found, whatever happens.
    search_path = await conn.fetchval("SELECT current_setting('search_path')")
    try:
        return await _apply_in_one_transaction(conn, stamp, manifest, plan, ledger_sql, ledger_rows)
    finally:
        await conn.execute("SELECT pg_catalog.set_config('search_path', $1, false)", search_path)


_LEDGER_TABLE_SQL = """
SELECT c.relname, c.relkind::text AS relkind, c.relrowsecurity,
       EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgrelid = c.oid) AS has_trigger,
       EXISTS (SELECT 1 FROM pg_rewrite w WHERE w.ev_class = c.oid) AS has_rule,
       EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid
                 AND (i.indexprs IS NOT NULL OR i.indpred IS NOT NULL)) AS has_expression_index,
       EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype NOT IN ('p', 'u')) AS has_check,
       EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_type ty ON ty.oid = a.atttypid
                WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                  AND (a.attgenerated <> '' OR ty.typtype <> 'b'
                       OR ty.typnamespace <> 'pg_catalog'::regnamespace)) AS has_custom_column,
       ARRAY(SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d WHERE d.adrelid = c.oid) AS defaults
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])
"""
_PLAIN_DEFAULT = re.compile(r"^(now\(\)|CURRENT_TIMESTAMP|nextval\('[A-Za-z0-9_.\"]+'::regclass\))$")


async def _ledger_tables_refusal(conn) -> Optional[str]:
    """Why a ledger INSERT could run code (a trigger, rule, expression index, CHECK, domain or default
    that calls a function), or None. Checked after the schema, before the ledger, in the same transaction."""
    for row in await conn.fetch(_LEDGER_TABLE_SQL, list(_LEDGER_COLUMNS)):
        name = row["relname"]
        if row["relkind"] != "r" or row["relrowsecurity"]:
            return "ledger table %s is not a plain table without row security" % name
        for column, what in (("has_trigger", "a trigger"), ("has_rule", "a rule"),
                             ("has_expression_index", "an expression or partial index"),
                             ("has_check", "a CHECK, foreign key or exclusion constraint"),
                             ("has_custom_column", "a generated, domain or non-catalog-type column")):
            if row[column]:
                return "ledger table %s has %s" % (name, what)
        if any(not _PLAIN_DEFAULT.match(d or "") for d in row["defaults"]):
            return "ledger table %s has a column default that is not now() or nextval()" % name
    return None


async def _apply_in_one_transaction(conn, stamp, manifest, plan: SchemaPlan, ledger_sql, ledger_rows) -> Dict[str, Any]:
    async with conn.transaction():
        await conn.execute("SET LOCAL standard_conforming_strings = on")
        await conn.execute("SET LOCAL lock_timeout = '%s'" % APPLY_LOCK_TIMEOUT)
        await conn.execute("SET LOCAL statement_timeout = '%s'" % APPLY_STATEMENT_TIMEOUT)
        await conn.execute("SET LOCAL idle_in_transaction_session_timeout = '%s'" % APPLY_IDLE_TIMEOUT)
        caps = await conn.fetchrow(_CAPS_SQL)
        for statement in plan.statements:
            # fetch(): the extended protocol, ONE command per call, or the server refuses the string.
            await conn.fetch(statement)
        if await conn.fetchrow(_CAPS_SQL) != caps:
            raise SchemaRefused("the schema changed the apply's own timeouts or standard_conforming_strings")
        refusal = await _ledger_tables_refusal(conn)
        if refusal:
            raise SchemaRefused(refusal)
        # Re-asserted right before the ledger: nothing in the schema may have turned it off.
        await conn.execute("SET LOCAL standard_conforming_strings = on")
        for statement in ledger_sql:
            await conn.fetch(statement)
        tables = await _user_table_count(conn)
        if tables != manifest.get("tables_live"):
            raise SchemaRefused("%d tables after apply, the source had %s" % (tables, manifest.get("tables_live")))
        counted = 0
        for name in LEDGER_TABLES:
            if await conn.fetchval("SELECT to_regclass($1)", "public.%s" % name):
                counted += int(await conn.fetchval('SELECT count(*) FROM public."%s"' % name))
        if counted != ledger_rows:
            raise SchemaRefused("ledger rows after apply differ from the manifest")
    return {"stamp": stamp, "tables": tables, "ledger_rows": ledger_rows, "statements": len(plan.statements),
            "pg_dump_version": manifest.get("pg_dump_version")}


def check_environment(env: Mapping[str, str]) -> None:
    from config.platform import is_sandbox
    from config.sandbox_guard import SandboxGuardRefused, enforce_sandbox_guard
    from config.sandbox_identity import sandbox_database_refusal

    if not is_sandbox(env):
        raise SchemaRefused("runs only in the sandbox (PIVOTA_ENV=sandbox)")
    try:
        enforce_sandbox_guard(env)
    except SandboxGuardRefused as exc:
        raise SchemaRefused(str(exc)) from None
    refusal = sandbox_database_refusal(env)
    if refusal:
        raise SchemaRefused(refusal)


async def _run(env: Mapping[str, str]) -> Dict[str, Any]:  # pragma: no cover - Cloud Run only
    import asyncpg

    check_environment(env)
    uri = (env.get("SANDBOX_SCHEMA_GCS_URI") or "").strip()
    if not uri:
        raise SchemaRefused("SANDBOX_SCHEMA_GCS_URI is not set")
    store = GcsStore(uri)
    conn = await asyncpg.connect(env["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"),
                                 statement_cache_size=0)
    try:
        return await apply_schema(conn, store)
    finally:
        await conn.close()


def check_files(schema_path: str, ledgers_path: str) -> Dict[str, Any]:
    """The authoritative offline check of a dump (the human read of the first dump runs it): the same
    parser and allowlist the apply uses, no database and no environment needed."""
    with open(schema_path, encoding="utf-8") as fh:
        plan = validate_schema_sql(fh.read())
    with open(ledgers_path, encoding="utf-8") as fh:
        _, rows = validate_ledgers_sql(fh.read())
    return {"statements": len(plan.statements), "checked": plan.checked, "ledger_rows": rows,
            "extensions": sorted(plan.extensions)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jobs.sandbox_schema_apply")
    parser.add_argument("command", nargs="?", choices=("apply", "check"), default="apply")
    parser.add_argument("files", nargs="*", help="check: <schema.sql> <ledgers.sql>")
    args = parser.parse_args(list(argv or []))
    try:
        if args.command == "check":
            if len(args.files) != 2:
                raise SchemaRefused("usage: check <schema.sql> <ledgers.sql>")
            result = check_files(*args.files)
        else:
            result = asyncio.run(_run(os.environ))
    except (SchemaRefused, CopyRefused) as exc:
        print("sandbox_schema_apply REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print("sandbox_schema_apply %s %s" % ("OK" if args.command == "check" else "DONE", json.dumps(result, sort_keys=True)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
