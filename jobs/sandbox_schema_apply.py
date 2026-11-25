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

IT DOES NOT TRUST THE DUMP. Before anything runs it checks the manifest (kind, version, stamp), every
file's sha256, and re-applies the dump's own refusals: no CREATE SERVER / USER MAPPING / SUBSCRIPTION /
PUBLICATION / ROLE / DATABASE, no GRANT / REVOKE / ALTER ROLE / ALTER SYSTEM, no row data in the schema
file, no psql meta-command but pg_dump's own \\restrict / \\unrestrict (stripped here: they mean
something to psql only), and in the ledger file nothing but INSERTs into startup_sql_migrations /
schema_migrations (and their sequences' setval).

Then, in ONE transaction: the schema, then the ledger rows. After it, the sandbox's base-table count
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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from jobs.sandbox_catalog_copy import CopyRefused, GcsStore, LocalDirStore, newest_complete_stamp  # noqa: F401

FORMAT_VERSION = 2
MANIFEST_NAME = "_MANIFEST.json"
_STAMP_RE = re.compile(r"^\d{8}T\d{4}Z$")
LEDGER_TABLES = ("startup_sql_migrations", "schema_migrations")
LEDGER_TRAILER = "-- sandbox ledger dump complete"


class SchemaRefused(RuntimeError):
    """The schema apply will not run. Messages name rules and stamps, never values."""


# --- a real tokenizer -------------------------------------------------------------------------------
#
# One pass over the text, tracking line comments, nested block comments, '...' literals ('' escapes),
# "..." identifiers, $tag$...$tag$ bodies and psql meta lines (top level, line start). Statements split
# at top-level ';' only. Each statement also gets a SKELETON: comments removed, every literal replaced
# by '?', every body by $$, whitespace collapsed, upper-cased; the allowlist below reads the skeleton.
# Refused while scanning: E'...' and U&'...' literals (backslash escapes can desync a naive reader),
# an unterminated literal/identifier/body/comment, trailing text with no ';',
# and a psql meta-command other than \restrict / \unrestrict.

_META_OK = re.compile(r"\\(restrict|unrestrict) [A-Za-z0-9]+\Z")
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def split_statements(text: str, what: str) -> List[Tuple[str, str]]:
    """``[(statement, skeleton)]`` for every top-level statement in ``text``."""
    out: List[Tuple[str, str]] = []
    i, n, start = 0, len(text), 0
    skel: List[str] = []

    def finish(end: int) -> None:
        nonlocal start, skel
        sk = " ".join("".join(skel).split()).upper()
        if sk:
            out.append((text[start:end], sk))
        start, skel = end + 1, []

    while i < n:
        c = text[i]
        if c == "\\" and (i == 0 or text[i - 1] == "\n"):
            j = text.find("\n", i)
            j = n if j < 0 else j
            if not _META_OK.match(text[i:j]):
                raise SchemaRefused("%s carries a psql meta-command other than \\restrict/\\unrestrict" % what)
            if "".join(skel).strip():
                raise SchemaRefused("%s has a psql meta-command inside a statement" % what)
            i, start, skel = j + 1, j + 1, []
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            skel.append(" ")
            continue
        if text.startswith("/*", i):
            depth, j = 1, i + 2
            while depth:
                a, b = text.find("/*", j), text.find("*/", j)
                if b < 0:
                    raise SchemaRefused("%s has an unterminated block comment" % what)
                if 0 <= a < b:
                    depth, j = depth + 1, a + 2
                else:
                    depth, j = depth - 1, b + 2
            i = j
            skel.append(" ")
            continue
        if c == "'":
            before = text[i - 2:i] if i >= 2 else text[:i]
            if before[-1:] in ("e", "E") and not (before[:-1].isalnum() or before[:-1] == "_"):
                raise SchemaRefused("%s has an E'...' literal" % what)
            if before.upper().endswith("U&"):
                raise SchemaRefused("%s has a U&'...' literal" % what)
            j = i + 1
            while True:
                k = text.find("'", j)
                if k < 0:
                    raise SchemaRefused("%s has an unterminated literal" % what)
                if text.startswith("''", k):
                    j = k + 2
                    continue
                break
            # A '$' inside a standard literal is ordinary text to Postgres and to this scanner (literals
            # are consumed before $-tags are looked for), so it is not refused: regex CHECKs such as
            # '^[A-Z]{3}$' are common. What would desync both is standard_conforming_strings=off, which
            # the allowlist refuses and the apply forces on.
            skel.append("'?'")
            i = k + 1
            continue
        if c == '"':
            j = i + 1
            while True:
                k = text.find('"', j)
                if k < 0:
                    raise SchemaRefused("%s has an unterminated identifier" % what)
                if text.startswith('""', k):
                    j = k + 2
                    continue
                break
            skel.append(text[i:k + 1])
            i = k + 1
            continue
        if c == "$":
            m = _DOLLAR_TAG.match(text, i)
            prev = text[i - 1] if i else " "
            if m and not (prev.isalnum() or prev == "_"):
                close = text.find(m.group(0), m.end())
                if close < 0:
                    raise SchemaRefused("%s has an unterminated $-quoted body" % what)
                skel.append(" $$ ")
                i = close + len(m.group(0))
                continue
        if c == ";":
            finish(i)
            i += 1
            continue
        skel.append(c)
        i += 1
    if "".join(skel).strip():
        raise SchemaRefused("%s ends with a statement that has no ';'" % what)
    return out


# --- what pg_dump --schema-only emits, and nothing else ----------------------------------------------
_ALLOWED_SCHEMA = re.compile(
    r"^(SET [A-Z_][A-Z0-9_.]* = .*"
    r"|SELECT PG_CATALOG\.SET_CONFIG\('\?', '\?', FALSE\)"
    r"|CREATE SCHEMA .*|CREATE EXTENSION .*|CREATE TYPE .*|CREATE DOMAIN .*"
    r"|CREATE (OR REPLACE )?(FUNCTION|PROCEDURE|AGGREGATE) .*"
    r"|CREATE SEQUENCE .*|CREATE (UNLOGGED )?TABLE .*|CREATE (MATERIALIZED )?VIEW .*"
    r"|CREATE (UNIQUE )?INDEX .*|CREATE (CONSTRAINT )?TRIGGER .*|CREATE RULE .*|CREATE POLICY .*"
    r"|CREATE TEXT SEARCH .*|CREATE COLLATION .*|CREATE STATISTICS .*|CREATE OPERATOR .*|CREATE CAST .*"
    r"|ALTER (TABLE|SEQUENCE|INDEX|TYPE|DOMAIN) .*)$"
)
#: Never, even inside an allowed kind.
_REFUSED_IN_SCHEMA = (
    (re.compile(r"^SET (ROLE|SESSION)\b"), "SET ROLE / SESSION AUTHORIZATION"),
    # The scanner reads '...' as standard-conforming; turning it off would let '\'' desync it.
    (re.compile(r"^SET STANDARD_CONFORMING_STRINGS = (?!ON$)"), "standard_conforming_strings other than on"),
    (re.compile(r"^ALTER TABLE .*\bOWNER TO\b"), "an ownership change"),
    (re.compile(r"\bLANGUAGE '?\??'? ?(C|INTERNAL|PLPYTHONU|PLPYTHON3U|PLPERLU|PLTCLU)\b"), "an untrusted language"),
    (re.compile(r"^CREATE EXTENSION .*\b(DBLINK|POSTGRES_FDW|[A-Z_]*_FDW|PG_CRON|PGLOGICAL|PLPYTHON3U|PLPERLU|PLTCLU)\b"),
     "an extension that connects out or runs jobs"),
)
_ALLOWED_LEDGER = (
    "INSERT INTO PUBLIC.SCHEMA_MIGRATIONS (ID, APPLIED_AT) VALUES ('?', '?')",
    "INSERT INTO PUBLIC.STARTUP_SQL_MIGRATIONS (FILENAME, CHECKSUM, APPLIED_AT) VALUES ('?', '?', '?')",
)
#: Literals that look like a secret or a person (the same rule as the dump's; comments excluded).
_SECRET_LITERAL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[A-Za-z]{2,}|\b(sk|rk|ak|pk)_(live|test)_[A-Za-z0-9]"
    r"|shpat_|ghp_|xox[abposr]-|[0-9a-fA-F]{32,}|[A-Za-z0-9+/]{40,}={0,2}"
)


def validate_schema_sql(text: str) -> str:
    """The schema file as it may be executed (meta lines removed), or SchemaRefused."""
    if "PostgreSQL database dump complete" not in text[-400:]:
        raise SchemaRefused("schema file has no completion trailer")
    kept: List[str] = []
    for statement, skeleton in split_statements(text, "schema file"):
        for pattern, label in _REFUSED_IN_SCHEMA:
            if pattern.search(skeleton):
                raise SchemaRefused("schema file carries %s" % label)
        if not _ALLOWED_SCHEMA.match(skeleton):
            raise SchemaRefused("schema file carries a statement pg_dump --schema-only does not emit (%s)"
                                % " ".join(skeleton.split()[:3]))
        body = "\n".join(line for line in statement.splitlines() if not line.lstrip().startswith("--"))
        if _SECRET_LITERAL.search(body):
            raise SchemaRefused("schema file carries a literal that looks like a secret or a person")
        kept.append(statement.strip())
    return ";\n".join(kept) + ";\n"


def validate_ledgers_sql(text: str) -> Tuple[str, int]:
    """(executable ledger SQL, INSERT count), or SchemaRefused."""
    if text.rstrip("\n").splitlines()[-1:] != [LEDGER_TRAILER]:
        raise SchemaRefused("ledger file has no completion trailer")
    kept: List[str] = []
    for statement, skeleton in split_statements(text, "ledger file"):
        if skeleton not in _ALLOWED_LEDGER:
            raise SchemaRefused("ledger file carries a statement other than an explicit-column ledger INSERT")
        kept.append(statement.strip())
    return (";\n".join(kept) + ";\n") if kept else "", len(kept)


async def _user_table_count(conn) -> int:
    return int(await conn.fetchval(
        """
        SELECT count(*) FROM information_schema.tables
         WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
           AND table_schema NOT LIKE 'pg_toast%' AND table_type = 'BASE TABLE'
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


async def apply_schema(conn, store, *, stamp: Optional[str] = None) -> Dict[str, Any]:
    stamp, manifest, schema_text, ledger_text = _load(store, stamp)
    schema_sql = validate_schema_sql(schema_text)
    ledger_sql, ledger_rows = validate_ledgers_sql(ledger_text)
    if ledger_rows != manifest.get("ledger_rows"):
        raise SchemaRefused("ledger file row count does not match the manifest")
    if await _user_table_count(conn) != 0:
        raise SchemaRefused("the sandbox database is not empty; this job only creates a schema once")
    # pg_dump's preamble empties search_path for the SESSION (set_config(..., false)); put it back,
    # so the connection is handed back as it was found, whatever happens.
    search_path = await conn.fetchval("SELECT current_setting('search_path')")
    try:
        return await _apply_in_one_transaction(conn, stamp, manifest, schema_sql, ledger_sql, ledger_rows)
    finally:
        await conn.execute("SELECT pg_catalog.set_config('search_path', $1, false)", search_path)


async def _apply_in_one_transaction(conn, stamp, manifest, schema_sql, ledger_sql, ledger_rows) -> Dict[str, Any]:
    async with conn.transaction():
        # The tokenizer's reading of '...' literals is the standard-conforming one; make the server's agree.
        await conn.execute("SET LOCAL standard_conforming_strings = on")
        await conn.execute(schema_sql)
        if ledger_rows:
            await conn.execute(ledger_sql)
        tables = await _user_table_count(conn)
        if tables != manifest.get("tables_live"):
            raise SchemaRefused("%d tables after apply, the source had %s" % (tables, manifest.get("tables_live")))
        counted = 0
        for name in LEDGER_TABLES:
            if await conn.fetchval("SELECT to_regclass($1)", "public.%s" % name):
                counted += int(await conn.fetchval('SELECT count(*) FROM public."%s"' % name))
        if counted != ledger_rows:
            raise SchemaRefused("ledger rows after apply differ from the manifest")
    return {"stamp": stamp, "tables": tables, "ledger_rows": ledger_rows,
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
    conn = await asyncpg.connect(env["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    try:
        return await apply_schema(conn, store)
    finally:
        await conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    argparse.ArgumentParser(prog="python -m jobs.sandbox_schema_apply").parse_args(list(argv or []))
    try:
        result = asyncio.run(_run(os.environ))
    except (SchemaRefused, CopyRefused) as exc:
        print("sandbox_schema_apply REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print("sandbox_schema_apply DONE %s" % json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
