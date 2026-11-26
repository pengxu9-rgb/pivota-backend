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

FORMAT_VERSION = 1
MANIFEST_NAME = "_MANIFEST.json"
_STAMP_RE = re.compile(r"^\d{8}T\d{4}Z$")
LEDGER_TABLES = ("startup_sql_migrations", "schema_migrations")

#: Statement starts that can hold a credential, a role, or rows. Checked at the start of each line,
#: case-insensitively (pg_dump writes keywords upper-case at column 0).
_FORBIDDEN = re.compile(
    r"^\s*(CREATE\s+(SERVER|USER\s+MAPPING|SUBSCRIPTION|PUBLICATION|ROLE|USER|GROUP|DATABASE)\b"
    r"|ALTER\s+(ROLE|USER|GROUP|SYSTEM|SERVER|USER\s+MAPPING|DATABASE)\b"
    r"|GRANT\b|REVOKE\b|SECURITY\s+LABEL\b|COPY\b|INSERT\b)",
    re.IGNORECASE | re.MULTILINE,
)
_META_OK = re.compile(r"^\\(restrict|unrestrict) [A-Za-z0-9]+$")
_LEDGER_LINE_OK = re.compile(
    r"^(--.*|"
    r"SET [a-z_]+ = .*;|"
    r"SELECT pg_catalog\.set_config\('search_path', '', false\);|"
    r"SELECT pg_catalog\.setval\('public\.(startup_sql_migrations|schema_migrations)_[a-z_]*seq', \d+, (true|false)\);|"
    # One statement per line: no ';' before the final one (ledger values are file ids, checksums, times).
    r"INSERT INTO public\.(startup_sql_migrations|schema_migrations) \([^;]*\) VALUES \([^;]*\);)$"
)


class SchemaRefused(RuntimeError):
    """The schema apply will not run. Messages name rules and stamps, never row values."""


def _strip_meta(text: str, what: str) -> str:
    out: List[str] = []
    for line in text.splitlines():
        if line.startswith("\\"):
            if not _META_OK.match(line):
                raise SchemaRefused("%s carries a psql meta-command other than \\restrict/\\unrestrict" % what)
            continue
        out.append(line)
    return "\n".join(out) + "\n"


# The tag group always participates (possibly empty): a backreference to an unmatched optional
# group never matches in Python, which would leave every plain $$ body unstripped.
_DOLLAR_BODY = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*|)\$.*?\$\1\$", re.DOTALL)
_QUOTED = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _top_level(text: str) -> str:
    """``text`` without dollar-quoted bodies, string literals and comments: what is left is statement
    structure, so a trigger function whose body says INSERT or GRANT is not read as one."""
    return _LINE_COMMENT.sub("", _QUOTED.sub("''", _DOLLAR_BODY.sub("$$$$", text)))


def validate_schema_sql(text: str) -> str:
    """The schema file as it may be executed, or SchemaRefused."""
    if "PostgreSQL database dump complete" not in text[-400:]:
        raise SchemaRefused("schema file has no completion trailer")
    match = _FORBIDDEN.search(_top_level(text))
    if match:
        raise SchemaRefused("schema file carries a forbidden statement (%s)" % " ".join(match.group(1).split()[:2]).upper())
    return _strip_meta(text, "schema file")


def validate_ledgers_sql(text: str) -> Tuple[str, int]:
    """(executable ledger SQL, INSERT count), or SchemaRefused."""
    cleaned = _strip_meta(text, "ledger file")
    rows = 0
    for line in cleaned.splitlines():
        if not line.strip():
            continue
        if not _LEDGER_LINE_OK.match(line):
            raise SchemaRefused("ledger file carries a statement other than a ledger INSERT")
        rows += line.startswith("INSERT INTO ")
    return cleaned, rows


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
