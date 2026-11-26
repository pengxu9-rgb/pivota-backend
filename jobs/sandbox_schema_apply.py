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
bootstrap SQL is in docs/sandbox/schema_bootstrap.md), may create no temporary object, has no per-role
setting, and owns nothing yet. Whatever the dump makes the server execute runs as that role. A statement-kind allowlist cannot be the gate: a view WITH DATA or a trigger calls a function.

IT DOES NOT TRUST THE DUMP EITHER. Before anything runs it checks the manifest (kind, version, stamp), every
file's sha256, the dump's pg_dump major version against this server's, and every statement with
PostgreSQL's own parser (pglast): an allowlist of parse-tree node types with their fields checked, pg_dump's
exact preamble SETs only, plpgsql/sql functions only, no ownership change, no CREATE TABLE AS or
materialized view WITH DATA, no rule, no trigger on a ledger table, no extension that is not already
installed, no literal that looks like a secret or a person. The ledger file must be exactly the two
explicit-column INSERT shapes. Each statement is executed alone (extended protocol). And NO DUMP-DEFINED
CODE IS REACHABLE while the schema is created: every expression the server evaluates (or constant-folds)
at DDL time may call only pg_catalog's functions and cast only to pg_catalog types or the dump's enums, a
domain's constraints included, so every type's input runs built-in code only (_ddl_time_refusal); no
TEMP object or pg_temp name anywhere.

Then, in ONE transaction with the apply's own lock/statement/idle timeouts: the schema; a check that no
ledger table can run code on INSERT, under the job's own search_path (pg_catalog, pg_temp) and row security
off; the ledger rows; a post-apply audit that reads only pg_catalog.* (no rows, grants, settings, temp
objects, large objects or moved sequences; exactly the dump's triggers, policies and row security). After
it, the sandbox's base-table count
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
#: DefineStmt kinds pg_dump emits that need no C code and can call no dump-defined function. An OPERATOR
#: or a CAST would let an expression reach one without naming it; an AGGREGATE's INITCOND runs its STYPE's
#: input (controller A2): an aggregate only by a reviewed name (AGGREGATES_ALLOWED).
_DEFINE_KINDS = ("OBJECT_COLLATION", "OBJECT_TSCONFIGURATION", "OBJECT_TSDICTIONARY")
#: CREATE TYPE ... AS RANGE parameters that name no function.
_RANGE_PARAMS = frozenset({"subtype", "subtype_opclass", "collation", "multirange_type_name"})
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
    "CreateStmt", "CreateSeqStmt", "AlterSeqStmt", "CreateDomainStmt", "CreateEnumStmt",
    "AlterEnumStmt", "CompositeTypeStmt", "ViewStmt", "IndexStmt", "CreatePolicyStmt", "CreateStatsStmt",
})
#: ALTER DOMAIN (ADD CONSTRAINT runs the CHECK, controller A3) and CREATE CAST are refused by name: with
#: neither, a domain's constraints are only the ones its CREATE DOMAIN shows.
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


def _walk(node):
    """``node`` and every parse-tree node under it."""
    from pglast import ast

    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Node):
            yield current
            stack.extend(getattr(current, name, None) for name in type(current).__slots__)
        elif isinstance(current, (tuple, list)):
            stack.extend(current)


def _names(names) -> Tuple[str, ...]:
    return tuple(getattr(n, "sval", None) or "" for n in (names or ()))


def _qualified_outside_catalog(names) -> bool:
    parts = _names(names)
    return len(parts) > 1 and parts[0] != "pg_catalog"


#: Non-pg_catalog functions and types an expression may name, by "schema.name", each one REVIEWED (the
#: 8155964d4 review, direction (a)). EMPTY: neither repo's migrations need one. The first real dump's
#: `check` names any that prod's schema has; each is added here only after a review that it is an
#: extension's (admin-installed C, never dump-defined code). The dump may not define one of these names.
EXPRESSION_NAMES_ALLOWED: FrozenSet[str] = frozenset()
#: CREATE AGGREGATE, by "schema.name", same review (direction (b)). EMPTY: neither repo has one.
AGGREGATES_ALLOWED: FrozenSet[str] = frozenset()
#: pg_catalog functions that run SQL text or another type's input by OID: built in, but they could reach
#: dump-defined code, so no expression the server evaluates at DDL time may call them.
_RUNS_OTHER_CODE = re.compile(
    r"^((query|cursor|table|schema|database)_to_xml(schema|_and_xmlschema)?|ts_stat|ts_rewrite|set_config"
    r"|(array|record|domain|range|multirange|anyarray|anyelement|anyrange|anymultirange|enum)_(in|recv))$")
#: Fields whose subtree is not evaluated while the statement runs: a function body (a string, or a
#: SQL-standard body that is analysed but not executed), a view's or a WITH NO DATA matview's query, a
#: trigger's function reference. Everything else in a statement is an expression position.
_NOT_EVALUATED = frozenset({("CreateFunctionStmt", "sql_body"), ("CreateFunctionStmt", "options"),
                            ("ViewStmt", "query"), ("CreateTableAsStmt", "query"), ("CreateTrigStmt", "funcname")})
#: Where a TypeName DECLARES a type (a column, a parameter, a return type, a domain's base type, a
#: sequence's AS) instead of converting a value to it.
_DECLARED_TYPES = frozenset({("ColumnDef", "typeName"), ("FunctionParameter", "argType"),
                             ("CreateFunctionStmt", "returnType"), ("CreateDomainStmt", "typeName"),
                             ("DefElem", "arg"), ("CreateStmt", "ofTypename"), ("ObjectWithArgs", "objargs")})


def _with_fields(node, skip=frozenset()):
    """(node, its parent's type name, the parent's field) for ``node`` and every node under it, leaving
    out the subtrees of the (parent type, field) pairs in ``skip``."""
    from pglast import ast

    stack = [(node, None, None)]
    while stack:
        current, parent, field = stack.pop()
        if isinstance(current, (tuple, list)):
            stack.extend((item, parent, field) for item in current)
        elif isinstance(current, ast.Node):
            yield current, parent, field
            kind = type(current).__name__
            stack.extend((getattr(current, name, None), kind, name) for name in type(current).__slots__
                         if (kind, name) not in skip)


def _safe_type(names, enums: FrozenSet[str]) -> bool:
    """A type whose input runs no dump-defined code: pg_catalog's (an unqualified name: search_path is '',
    and TEMP is refused), an enum the dump creates (enum_in reads pg_enum), or a reviewed name."""
    parts = _names(names)
    return len(parts) == 1 or parts[0] == "pg_catalog" or ".".join(parts) in enums | EXPRESSION_NAMES_ALLOWED


def _temp_refusal(stmt) -> Optional[str]:
    """TEMP / TEMPORARY / pg_temp anywhere (8155964d4 review, direction (c)): pg_temp is searched BEFORE
    pg_catalog for relations and types, so a temp view could shadow every catalog the audit reads."""
    for node, _, _ in _with_fields(stmt):
        kind = type(node).__name__
        if kind == "RangeVar" and (node.relpersistence == "t" or (node.schemaname or "").startswith("pg_temp")):
            return "a TEMPORARY object or a pg_temp name"
        if kind == "DefElem" and node.defname == "as":
            continue  # a function body: a string, not a name
        for name in type(node).__slots__:
            value = getattr(node, name, None)
            if isinstance(value, tuple) and value and all(type(v).__name__ == "String" for v in value) \
                    and value[0].sval.startswith("pg_temp"):
                return "a TEMPORARY object or a pg_temp name"
    return None


def _ddl_time_refusal(stmt, enums: FrozenSet[str]) -> Optional[str]:
    """Why ``stmt`` could make the server run dump-defined code WHILE the schema is being created, or None.

    The invariant (8155964d4 review, direction (a)): in every expression position (a CHECK, a domain's
    constraint, a DEFAULT, a generated column, an index, partition-key or statistics expression, a bound, a
    predicate, a policy, a trigger's WHEN, a parameter default) a function or operator may only be
    pg_catalog's, minus the few that run SQL or another type's input, and a cast only to a pg_catalog type,
    an enum the dump creates or a reviewed name. Because a DOMAIN's constraints obey the same rule, the input
    function of every type the dump can create runs built-in code only, so even a bare literal coerced to a
    domain or composite column (`c public.comp DEFAULT '(1)'`, a bound coerced to a domain key) reaches no
    dump-defined function. Function bodies, view queries and trigger function references are not
    evaluated when created and are left alone. With search_path pinned to '' an unqualified function or
    operator is pg_catalog's."""
    kind = type(stmt).__name__
    for node, parent, field in _with_fields(stmt, _NOT_EVALUATED):
        sub = type(node).__name__
        names = {"FuncCall": "funcname", "A_Expr": "name", "SubLink": "operName"}.get(sub)
        if names:
            parts = _names(getattr(node, names))
            if _qualified_outside_catalog(getattr(node, names)) and ".".join(parts) not in EXPRESSION_NAMES_ALLOWED:
                return "a function or operator that is not pg_catalog's (%s) in an expression" % ".".join(parts)
            if sub == "FuncCall" and _RUNS_OTHER_CODE.match(parts[-1] or ""):
                return "a call of pg_catalog.%s (it runs SQL or another type's input) in an expression" % parts[-1]
        if sub == "TypeName" and (parent, field) not in _DECLARED_TYPES and not _safe_type(node.names, enums):
            return "a cast to a type that is not pg_catalog's or an enum (%s) in an expression" % ".".join(_names(node.names))
        if sub == "PartitionBoundSpec":
            for datum in (node.listdatums or ()) + (node.lowerdatums or ()) + (node.upperdatums or ()):
                if not _constant_datum(datum):
                    return "a partition bound that is not a constant (it is evaluated at create time)"
    if kind == "CreateStmt" and stmt.partspec is not None:
        # Defence in depth: a bound is coerced to the key column's type at CREATE/ATTACH (controller A5).
        columns = {c.colname: c.typeName for c in (stmt.tableElts or ()) if type(c).__name__ == "ColumnDef"}
        for elem in stmt.partspec.partParams or ():
            if elem.name is not None and (elem.name not in columns or not _safe_type(columns[elem.name].names, enums)):
                return "a partition key column whose type is not pg_catalog's or an enum"
    if kind == "CreateFunctionStmt" and ".".join(_names(stmt.funcname)) in EXPRESSION_NAMES_ALLOWED:
        return "a function with a reviewed extension function's name"
    return None


def _constant_datum(datum) -> bool:
    kind = type(datum).__name__
    if kind == "A_Const":
        return True
    if kind == "TypeCast":
        return type(datum.arg).__name__ == "A_Const" and not _qualified_outside_catalog(datum.typeName.names)
    if kind == "ColumnRef":  # MINVALUE / MAXVALUE
        fields = [getattr(f, "sval", None) for f in datum.fields]
        return fields in (["minvalue"], ["maxvalue"])
    return False


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
        if any(o.defname == "security" and getattr(o.arg, "boolval", False) for o in (node.options or ())):
            # After REASSIGN OWNED it would run as the app owner, EXECUTE-able by PUBLIC (--no-privileges
            # dropped prod's REVOKEs). Neither repo has one (controller B3).
            return "a SECURITY DEFINER function"
        return None
    if kind == "DefineStmt":
        define = enums.ObjectType(node.kind).name
        if define == "OBJECT_AGGREGATE" and ".".join(_names(node.defnames)) in AGGREGATES_ALLOWED:
            return None
        if define == "OBJECT_AGGREGATE":
            return "a CREATE AGGREGATE that is not reviewed by name (%s)" % ".".join(_names(node.defnames))
        if define not in _DEFINE_KINDS:
            return "a CREATE %s (it needs C code, or lets an expression reach a dump function unnamed)" % define
        return None
    if kind == "CreateRangeStmt":
        if any(p.defname not in _RANGE_PARAMS for p in (node.params or ())):
            return "a range type with a canonical or subtype_diff function"
        return None
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
            constraint = cmd.def_ if sub == "AT_AddConstraint" else None
            if constraint is not None and enums.ConstrType(constraint.contype).name == "CONSTR_CHECK" \
                    and not (constraint.conname and constraint.skip_validation):
                # pg_dump writes a CHECK on its own only when it is NOT VALID, which the server does not
                # evaluate; a validated one is planned (constant-folded) on the spot (controller A4).
                return "an ALTER TABLE ADD CONSTRAINT CHECK that is not pg_dump's named NOT VALID form"
        return None
    if kind == "AlterOwnerStmt":
        return "an ownership change"
    if kind in ("AlterDomainStmt", "CreateCastStmt"):
        return "an %s (a domain's constraints and a type's casts come only from its CREATE)" % (
            "ALTER DOMAIN" if kind == "AlterDomainStmt" else "CREATE CAST")
    return "a statement pg_dump --schema-only does not emit (%s)" % kind


@dataclass(frozen=True)
class SchemaPlan:
    statements: Tuple[str, ...]  # executed in order, each alone
    extensions: FrozenSet[str]   # must already be installed by the admin: the dump never creates one
    checked: int                 # statements read, executed or not
    kinds: Tuple[Tuple[str, int], ...] = ()  # executed statements per node type (the post-apply audit's expectations)
    row_security: FrozenSet[str] = frozenset()         # "schema.table" with ENABLE ROW LEVEL SECURITY
    forced_row_security: FrozenSet[str] = frozenset()  # "schema.table" with FORCE ROW LEVEL SECURITY

    def count(self, kind: str) -> int:
        return dict(self.kinds).get(kind, 0)


def validate_schema_sql(text: str) -> SchemaPlan:
    """What may be executed from the schema file, or SchemaRefused."""
    if "PostgreSQL database dump complete" not in text[-400:]:
        raise SchemaRefused("schema file has no completion trailer")
    enums = _pglast()[4]
    executed, skipped = _preamble()
    keep: List[str] = []
    extensions = set()
    kinds: Dict[str, int] = {}
    row_security: Dict[str, set] = {"AT_EnableRowSecurity": set(), "AT_ForceRowSecurity": set()}
    parsed = _parse(text, "schema file")
    dump_enums = frozenset(".".join(_names(n.typeName)) for n, _ in parsed if type(n).__name__ == "CreateEnumStmt")
    for node, sql in parsed:
        kind = type(node).__name__
        # Every statement, the preamble too: a TEMP object would shadow the catalogs the audit reads.
        why = _temp_refusal(node)
        if not why and kind not in ("VariableSetStmt", "SelectStmt"):
            # by type first: a refused kind need not be printable
            why = _refusal(node, enums) or _ddl_time_refusal(node, dump_enums)
        if why:
            raise SchemaRefused("schema file carries %s" % why)
        if kind == "AlterTableStmt":
            for cmd in node.cmds:
                sub = enums.AlterTableType(cmd.subtype).name
                if sub in row_security:
                    row_security[sub].add("%s.%s" % (node.relation.schemaname, node.relation.relname))
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
        kinds[kind] = kinds.get(kind, 0) + 1
        keep.append(sql)
    return SchemaPlan(tuple(keep), frozenset(extensions), len(parsed), tuple(sorted(kinds.items())),
                      frozenset(row_security["AT_EnableRowSecurity"]), frozenset(row_security["AT_ForceRowSecurity"]))


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
        SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
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
#: CLIENT-side caps too (controller B2): the server-side ones live in GUCs a statement could change and
#: change back between two snapshots. Each statement, and the whole apply, has a deadline of its own.
APPLY_STATEMENT_SECONDS = 120.0
APPLY_DEADLINE_SECONDS = 900.0
_CAPS_SQL = ("SELECT pg_catalog.current_setting('lock_timeout') AS lock_timeout, "
             "pg_catalog.current_setting('statement_timeout') AS st, "
             "pg_catalog.current_setting('idle_in_transaction_session_timeout') AS idle, "
             "pg_catalog.current_setting('standard_conforming_strings') AS scs, "
             "pg_catalog.current_setting('search_path') AS search_path, "
             "pg_catalog.current_setting('row_security') AS row_security, "
             "pg_catalog.current_setting('check_function_bodies') AS check_function_bodies")
#: What the job's own SQL runs under, after the schema (8155964d4 review, direction (d)): pg_temp listed
#: LAST, so a temp relation could never shadow a catalog even if one existed; row security off, so a
#: policy can neither hide a row from a probe nor run (a query it would filter fails instead).
JOB_SEARCH_PATH = "pg_catalog, pg_temp"

_ROLE_SQL = """
SELECT r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolreplication, r.rolbypassrls,
       current_user = session_user AS same_user,
       EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid) AS has_membership,
       pg_catalog.has_database_privilege(pg_catalog.current_database(), 'CREATE') AS can_create_schema,
       pg_catalog.has_database_privilege(pg_catalog.current_database(), 'TEMP') AS can_create_temp,
       EXISTS (SELECT 1 FROM pg_catalog.pg_db_role_setting s WHERE s.setrole = r.oid) AS has_settings,
       (EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE relowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc WHERE proowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_type WHERE typowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_extension WHERE extowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_largeobject_metadata WHERE lomowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datdba = r.oid)) AS owns_something
  FROM pg_catalog.pg_roles r WHERE r.rolname = CURRENT_USER
"""
_ROLE_RULES = (
    ("rolsuper", "is a superuser"), ("rolcreaterole", "can create roles"), ("rolcreatedb", "can create databases"),
    ("rolreplication", "has REPLICATION"), ("rolbypassrls", "bypasses row security"),
    ("has_membership", "is a member of another role (cloudsqlsuperuser, the app role, pg_*)"),
    ("can_create_schema", "has CREATE on the database"),
    # The admin's bootstrap revokes TEMP from PUBLIC in the sandbox DB: a temp relation would be searched
    # before pg_catalog (8155964d4 review, direction (c)).
    ("can_create_temp", "may create temporary objects (REVOKE TEMP ON DATABASE ... FROM PUBLIC)"),
    ("has_settings", "has per-role settings (ALTER ROLE ... SET)"),
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


def _parser_major() -> int:
    from pglast.parser import get_postgresql_version

    return int(get_postgresql_version()[0])


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
    if server > _parser_major():
        raise SchemaRefused("this server (%d) is newer than the parser the dump is checked with (%d)"
                            % (server, _parser_major()))
    if await _user_table_count(conn) != 0:
        raise SchemaRefused("the sandbox database is not empty; this job only creates a schema once")
    installed = {r["extname"] for r in await conn.fetch("SELECT extname FROM pg_catalog.pg_extension")}
    missing = sorted(plan.extensions - installed)
    if missing:
        raise SchemaRefused("extensions are pre-created by the admin, never by the dump; missing: %s"
                            % ", ".join(missing))
    # pg_dump's preamble empties search_path for the SESSION (set_config(..., false)); put it back,
    # so the connection is handed back as it was found, whatever happens.
    search_path = await conn.fetchval("SELECT current_setting('search_path')")
    try:
        return await asyncio.wait_for(_apply_in_one_transaction(conn, stamp, manifest, plan, ledger_sql, ledger_rows),
                                      timeout=APPLY_DEADLINE_SECONDS)
    except asyncio.TimeoutError:
        raise SchemaRefused("the apply passed a client-side deadline; nothing was committed") from None
    finally:
        try:  # best effort: after a cancelled statement the connection may be unusable; never mask the refusal
            await conn.execute("SELECT pg_catalog.set_config('search_path', $1, false)", search_path, timeout=10)
        except Exception:  # noqa: BLE001
            pass


_LEDGER_TABLE_SQL = """
SELECT c.relname, c.relkind::pg_catalog.text AS relkind, c.relrowsecurity,
       EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid = c.oid) AS has_trigger,
       EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite w WHERE w.ev_class = c.oid) AS has_rule,
       EXISTS (SELECT 1 FROM pg_catalog.pg_index i WHERE i.indrelid = c.oid
                 AND (i.indexprs IS NOT NULL OR i.indpred IS NOT NULL)) AS has_expression_index,
       EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conrelid = c.oid
                 AND k.contype NOT IN ('p', 'u')) AS has_check,
       EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_type ty ON ty.oid = a.atttypid
                WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                  AND (a.attgenerated <> '' OR ty.typtype <> 'b'
                       OR ty.typnamespace <> 'pg_catalog'::pg_catalog.regnamespace)) AS has_custom_column,
       ARRAY(SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid) FROM pg_catalog.pg_attrdef d
              WHERE d.adrelid = c.oid) AS defaults
  FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relname = ANY($1::pg_catalog.text[])
"""
_PLAIN_DEFAULT = re.compile(r"^(now\(\)|CURRENT_TIMESTAMP|nextval\('[A-Za-z0-9_.\"]+'::regclass\))$")


async def _ledger_tables_refusal(conn) -> Optional[str]:
    """Why a ledger INSERT could run code (a trigger, rule, expression index, CHECK, domain or default
    that calls a function), or None. Checked after the schema, before the ledger, in the same transaction."""
    for row in await conn.fetch(_LEDGER_TABLE_SQL, list(_LEDGER_COLUMNS), timeout=APPLY_STATEMENT_SECONDS):
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


# Every catalog is named pg_catalog.x and the audit runs under JOB_SEARCH_PATH with row security off, so
# nothing the schema created can stand in for a catalog or hide a row (8155964d4 review, (d) and (e)).
_AUDIT_SQL = """
WITH me AS (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = CURRENT_USER)
SELECT
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c, me WHERE c.relowner = me.oid AND c.relacl IS NOT NULL)
    AS table_grants,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid, me
    WHERE c.relowner = me.oid AND a.attacl IS NOT NULL) AS column_grants,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p, me WHERE p.proowner = me.oid AND p.proacl IS NOT NULL)
    AS function_grants,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type t, me WHERE t.typowner = me.oid AND t.typacl IS NOT NULL)
    AS type_grants,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_namespace n, me WHERE n.nspowner = me.oid AND n.nspacl IS NOT NULL)
    AS schema_grants,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_default_acl d, me WHERE d.defaclrole = me.oid) AS default_privileges,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p, me WHERE p.proowner = me.oid AND p.prosecdef)
    AS security_definers,
  -- any owner: a new sandbox database holds no large object at all
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_largeobject_metadata) AS large_objects,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_db_role_setting s, me WHERE s.setrole = me.oid) AS role_settings,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c, me WHERE c.relowner = me.oid AND c.relpersistence = 't')
    + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_proc p WHERE p.pronamespace = pg_catalog.pg_my_temp_schema())
    + (SELECT pg_catalog.count(*) FROM pg_catalog.pg_type t WHERE t.typnamespace = pg_catalog.pg_my_temp_schema())
    AS temp_objects,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_rewrite r JOIN pg_catalog.pg_class c ON c.oid = r.ev_class, me
    WHERE c.relowner = me.oid AND r.rulename <> '_RETURN') AS rules,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid, me
    WHERE c.relowner = me.oid AND NOT t.tgisinternal AND t.tgparentid = 0) AS triggers,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_policy p JOIN pg_catalog.pg_class c ON c.oid = p.polrelid, me
    WHERE c.relowner = me.oid) AS policies,
  (SELECT pg_catalog.count(*) FROM pg_catalog.pg_class c, me
    WHERE c.relowner = me.oid AND c.relkind = 'm' AND c.relispopulated) AS populated_matviews
"""
_AUDIT_ZERO = (
    ("table_grants", "a GRANT on a table"), ("column_grants", "a column-level GRANT"),
    ("function_grants", "a GRANT on a function"), ("type_grants", "a GRANT on a type"),
    ("schema_grants", "a GRANT on a schema"), ("default_privileges", "ALTER DEFAULT PRIVILEGES"),
    ("security_definers", "a SECURITY DEFINER function"), ("large_objects", "a large object"),
    ("role_settings", "a per-role setting (ALTER ROLE ... SET)"), ("temp_objects", "a temporary object"),
    ("rules", "a rule"), ("populated_matviews", "a populated materialized view"),
)
_OWNED_TABLES_SQL = """
SELECT pg_catalog.format('%I.%I', n.nspname, c.relname) AS name, n.nspname || '.' || c.relname AS plain,
       c.relrowsecurity, c.relforcerowsecurity,
       (n.nspname = 'public' AND c.relname = ANY($1::pg_catalog.text[])) AS is_ledger
  FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = CURRENT_USER) AND c.relkind IN ('r', 'p')
"""
#: The role's sequences but those a ledger column owns (a ledger INSERT may draw from one).
_OWNED_SEQUENCES_SQL = """
SELECT pg_catalog.format('%I.%I', n.nspname, c.relname) AS name, s.seqstart
  FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_catalog.pg_sequence s ON s.seqrelid = c.oid
 WHERE c.relowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = CURRENT_USER) AND c.relkind = 'S'
   AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d JOIN pg_catalog.pg_class t ON t.oid = d.refobjid
                     JOIN pg_catalog.pg_namespace tn ON tn.oid = t.relnamespace
                    WHERE d.classid = 'pg_catalog.pg_class'::pg_catalog.regclass AND d.objid = c.oid
                      AND d.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass AND d.deptype IN ('a', 'i')
                      AND tn.nspname = 'public' AND t.relname = ANY($1::pg_catalog.text[]))
"""


class _Probed(Exception):
    """Carries a probe's answer out of a savepoint, which is then rolled back."""


async def _has_rows(conn, name: str, forced: bool) -> bool:
    """Whether the table holds a row, whatever its policies say. With row_security off a policy never
    runs; for a table that FORCEs row security the probe would fail instead, so for those it lifts FORCE
    inside a savepoint that is rolled back (the owner is then exempt from its own policies)."""
    import asyncpg

    sql = "SELECT EXISTS (SELECT 1 FROM ONLY %s)" % name
    try:
        if not forced:
            return bool(await conn.fetchval(sql, timeout=APPLY_STATEMENT_SECONDS))
        try:
            async with conn.transaction():  # a savepoint
                await conn.execute("ALTER TABLE ONLY %s NO FORCE ROW LEVEL SECURITY" % name, timeout=APPLY_STATEMENT_SECONDS)
                raise _Probed(bool(await conn.fetchval(sql, timeout=APPLY_STATEMENT_SECONDS)))
        except _Probed as answer:
            return bool(answer.args[0])
    except asyncpg.PostgresError:
        raise SchemaRefused("a table could not be read past its row security; rolled back") from None


async def _audit_refusal(conn, plan: SchemaPlan) -> Optional[str]:
    """What the apply left that the dump did not declare, or None (controller B1). The catch-all behind
    the parser: whatever ran, the transaction commits only a schema with no rows outside the ledgers,
    no privileges, no rules, no settings, no temp objects, sequences as created, and exactly the dump's
    triggers, policies and row-security switches."""
    row = await conn.fetchrow(_AUDIT_SQL, timeout=APPLY_STATEMENT_SECONDS)
    for column, what in _AUDIT_ZERO:
        if row[column]:
            return "the apply left %s the dump does not declare; rolled back" % what
    if row["triggers"] != plan.count("CreateTrigStmt"):
        return "the apply left %d triggers, the dump declares %d; rolled back" % (row["triggers"], plan.count("CreateTrigStmt"))
    if row["policies"] != plan.count("CreatePolicyStmt"):
        return "the apply left %d policies, the dump declares %d; rolled back" % (row["policies"], plan.count("CreatePolicyStmt"))
    tables = await conn.fetch(_OWNED_TABLES_SQL, list(_LEDGER_COLUMNS), timeout=APPLY_STATEMENT_SECONDS)
    if {t["plain"] for t in tables if t["relrowsecurity"]} != plan.row_security \
            or {t["plain"] for t in tables if t["relforcerowsecurity"]} != plan.forced_row_security:
        return "the apply left row security on tables the dump does not declare it for; rolled back"
    for table in tables:
        if not table["is_ledger"] and await _has_rows(conn, table["name"], table["relforcerowsecurity"]):
            return "the apply left rows in a table that is not a ledger; rolled back"
    for sequence in await conn.fetch(_OWNED_SEQUENCES_SQL, list(_LEDGER_COLUMNS), timeout=APPLY_STATEMENT_SECONDS):
        state = await conn.fetchrow("SELECT last_value, is_called FROM %s" % sequence["name"], timeout=APPLY_STATEMENT_SECONDS)
        if state["is_called"] or state["last_value"] != sequence["seqstart"]:  # setval(s, n, false) included
            return "the apply left a sequence that was advanced or set; rolled back"
    return None


def _expected_job_caps(job_caps, caps) -> bool:
    """The job's own settings: the apply's (as the server prints them back) but search_path."""
    return (dict(job_caps, search_path=None) == dict(caps, search_path=None)
            and job_caps["search_path"] == JOB_SEARCH_PATH and job_caps["row_security"] == "off"
            and job_caps["scs"] == "on" and job_caps["check_function_bodies"] == "off")


async def _apply_in_one_transaction(conn, stamp, manifest, plan: SchemaPlan, ledger_sql, ledger_rows) -> Dict[str, Any]:
    async with conn.transaction():
        await conn.execute("SET LOCAL standard_conforming_strings = on")
        await conn.execute("SET LOCAL lock_timeout = '%s'" % APPLY_LOCK_TIMEOUT)
        await conn.execute("SET LOCAL statement_timeout = '%s'" % APPLY_STATEMENT_TIMEOUT)
        await conn.execute("SET LOCAL idle_in_transaction_session_timeout = '%s'" % APPLY_IDLE_TIMEOUT)
        # A body is never parsed at CREATE (pg_dump sets the same), and no policy runs.
        await conn.execute("SET LOCAL check_function_bodies = off")
        await conn.execute("SET LOCAL row_security = off")
        # Only pg_catalog resolves an unqualified name: the DDL-time checks rely on it.
        await conn.execute("SELECT pg_catalog.set_config('search_path', '', true)")  # as pg_dump spells it
        caps = await conn.fetchrow(_CAPS_SQL)
        for statement in plan.statements:
            # fetch(): the extended protocol, ONE command per call, or the server refuses the string.
            await conn.fetch(statement, timeout=APPLY_STATEMENT_SECONDS)
        if await conn.fetchrow(_CAPS_SQL) != caps:
            raise SchemaRefused("the schema changed the apply's own timeouts, settings or search_path")
        # From here on only the job's own SQL and the ledger INSERTs run.
        await conn.execute("SELECT pg_catalog.set_config('search_path', $1, true)", JOB_SEARCH_PATH)
        await conn.execute("SET LOCAL row_security = off")
        await conn.execute("SET LOCAL standard_conforming_strings = on")
        job_caps = await conn.fetchrow(_CAPS_SQL)
        if not _expected_job_caps(job_caps, caps):
            raise SchemaRefused("the job's own search_path, row security or timeouts did not take effect")
        refusal = await _ledger_tables_refusal(conn)
        if refusal:
            raise SchemaRefused(refusal)
        for statement in ledger_sql:
            await conn.fetch(statement, timeout=APPLY_STATEMENT_SECONDS)
        if await conn.fetchrow(_CAPS_SQL) != job_caps:  # right before the audit
            raise SchemaRefused("the ledger changed the job's own settings")
        refusal = await _audit_refusal(conn, plan)
        if refusal:
            raise SchemaRefused(refusal)
        tables = await _user_table_count(conn)
        if tables != manifest.get("tables_live"):
            raise SchemaRefused("%d tables after apply, the source had %s" % (tables, manifest.get("tables_live")))
        counted = 0
        for name in LEDGER_TABLES:
            if await conn.fetchval("SELECT pg_catalog.to_regclass($1)", "public.%s" % name):
                counted += int(await conn.fetchval('SELECT pg_catalog.count(*) FROM public."%s"' % name))
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
