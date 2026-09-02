"""Every SQL literal this repo hands to `database.*` must be PLANNABLE by Postgres.

THE WIDENING. `tests/test_ops_script_sql_prepare_postgres.py` proves the idea on
four ops scripts by DRIVING them. This file takes the other half: a static sweep
over every `database.<method>("...")` call site in the repo, asking Postgres to
PREPARE each one. The two are complementary and neither subsumes the other —

    driven   sees `.format()`/f-string SQL, but only where a test drives it;
    static   sees every literal call site it can FOLLOW.

Be precise about that second one, because "static sweep" oversells it. Of ~3,300
`database.*` call sites in the swept directories this collector resolves ~1,960:
literals, module-level constants, loop-over-literals, and lambda bodies. It does
NOT follow a literal assigned to a function-local first (~300 sites) or passed by
keyword (~5), and it cannot see f-strings or `.format()` at all (~825). Those are
not "dynamic SQL" — most are perfectly static strings this collector simply
cannot trace. Stated here rather than left to be inferred from a green run.

WHY IT PAYS. The first sweep found NINE statements that cannot be planned, all
in code shipped since May 2026 and all fixed in the commit that adds this file:
three in the `catalog_sync_service` tombstone prune (#1703's own defect, in a
live sync path), one in `pdp_matcher`, two `products_cache` backfills, one in
`subject_resolve`, one in `commerce_attribution_service`, and a `jsonb`-into-
`json` COALESCE in `routes/buyer_api.py` that sits inside `except Exception:
pass`. That is the #1588 story again: the repo's tests run on SQLite, and SQLite
cannot see any of it.

AND THE LIMIT THIS GATE CANNOT CROSS, learned by tripping over it. PREPARE is
Parse+Describe: it validates TYPES, never VALUES. Three of those nine statements
were still dead after being made plannable — they had moved from failing at Parse
to failing at Bind (a `Decimal` bound to a param the CASE typed `integer`; a raw
`dict` bound to a `json` column through raw SQL, where no SQLAlchemy bind
processor runs). A green run here means "Postgres would plan this", not "this
works". The driven gate can see bind-time failures; this one structurally cannot.

THE FIXTURE IS THE WHOLE BALLGAME. PREPARE resolves names against a real schema,
so what this gate can see is bounded by what the schema has. Each layer bought
real coverage, of 1,954 collected statements:

    metadata.create_all only .................  751 planned
    + db/migrations/*.sql .................... 1,518 planned
    + main.py's startup DDL .................. 1,790 planned
    + isolated from `public` ................. 1,826 planned
    + private database, UTF8 client ......... 1,835 planned,  122 unchecked

Note what "planned" does and does not prove: ~380 of these are utility statements
(CREATE/ALTER/DROP), for which Postgres does no name or type analysis at Parse.
They always plan. The number that carries weight is the ~1,450 DML statements.

The 3 `stale_catalog_*` stubs matter for the same reason — they are TEMP tables
the sync prune creates at runtime, and without them three genuinely-broken
statements in `services/catalog_sync_service.py` fail with `UndefinedTable` and
get written off as a fixture gap. A fixture hole does not just lose coverage, it
MASKS defects, which is why the unchecked count is pinned below rather than
merely reported.

🚨 THIS GATE DOES NOT SHARE A DATABASE AT ALL. It creates one, builds the schema
in it, and drops it. Applying 220 migrations — 118 of which ALTER or UPDATE — to
the database every other `test_*_postgres.py` file shares is exactly the blast
radius the warning in tests/test_canonical_feed_tombstoned_flag_postgres.py
describes, an order of magnitude up. A private SCHEMA was tried first and is not
sufficient in either direction; see the comment on `_GATE_DB`.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

# A separate DATABASE, not a schema in the shared one. A private schema cannot
# satisfy both halves of what this gate needs:
#   * keep `public` OFF the search path, or migrations that ALTER/UPDATE a table
#     absent from the private schema silently hit the SIBLING GATES' copy;
#   * keep `public` ON it, or the 120 migrations that name `public.x` explicitly
#     (`to_regclass('public.api_keys')` guards, `'public.x402_transactions'::regclass`)
#     turn into no-ops and coverage collapses — measured, 1826 planned -> 1429.
# Both were tried. A separate database gives the migrations the `public` they
# expect while sharing nothing with the other gate files, and teardown is one
# DROP DATABASE instead of a schema drop that cannot undo a cross-schema UPDATE.
_GATE_DB = f"sql_prepare_gate_{os.getpid()}"


def _gate_db_url() -> str:
    """DATABASE_URL with the database name swapped for the private one."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(DATABASE_URL)
    return urlunsplit(parts._replace(path=f"/{_GATE_DB}"))

# Directories whose SQL reaches production. `tests/` is deliberately absent:
# test fixtures build their own throwaway schemas and are not production SQL.
_SWEPT_DIRS = (
    "routes", "services", "db", "jobs", "scripts", "adapters", "orchestrator", "core",
)

# main.py is on the dialect workflow's path filter but is not under any swept
# directory, so without this entry its SQL — including the startup DDL every
# deploy runs — is the one production file nothing plans.
_SWEPT_FILES = ("main.py",)

_DB_METHODS = {"execute", "execute_many", "fetch_all", "fetch_one", "fetch_val", "iterate"}

# TEMP tables the code creates at runtime (`CREATE TEMP TABLE ... ON COMMIT DROP`).
# Declared as plain tables so statements that reference them can be planned. Without
# these, services/catalog_sync_service.py's three prune statements fail with
# UndefinedTable and their real defect is invisible.
_TEMP_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS stale_catalog_products (product_key text)",
    "CREATE TABLE IF NOT EXISTS stale_catalog_skus (sku_key text)",
    "CREATE TABLE IF NOT EXISTS stale_catalog_offers (offer_id text)",
)


def _startup_ddl() -> List[str]:
    """`CREATE TABLE IF NOT EXISTS` literals main.py runs at startup.

    `merchant_stores` and `merchant_psps` are created by application bootstrap
    rather than by a migration or by `metadata`, and between them they account
    for 157 of the statements this gate could not otherwise resolve. Lifted from
    main.py's own AST rather than copied here as a stub: a hand-copied schema
    drifts from the real one silently, and a fixture that is subtly wrong about
    column types is exactly the false confidence this file exists to prevent.
    """
    tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"), filename="main.py")
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "CREATE TABLE IF NOT EXISTS" in node.value
    ]

# A missing table/column/function is a hole in the FIXTURE, not a defect in the
# statement — Postgres never reaches type analysis, so the statement is unproven
# either way. Counted and pinned below, never silently dropped.
_FIXTURE_GAP = {
    "UndefinedTableError",
    "UndefinedColumnError",
    "UndefinedFunctionError",
    "UndefinedObjectError",
    "InvalidSchemaNameError",
}

# Measured on this fixture at the commit that added this file: 1954 collected,
# 1826 planned, 128 unchecked. Three separate things are pinned because they
# fail in three different ways.
#
#   _MIN_COLLECTED  the AST sweep still finds the call sites. A collector that
#                   silently stops matching leaves this gate green having
#                   planned nothing — the failure the workflow header names.
#   _MIN_PLANNED    the fixture still resolves what it used to resolve.
#   _MAX_UNCHECKED  the subtle one. A fixture hole does not merely lose
#                   coverage, it MASKS defects behind UndefinedTable: the three
#                   broken statements in catalog_sync_service.py hid there until
#                   the stale_catalog_* stubs were added. Holes may shrink
#                   freely; growing one needs a reason.
#
# Small slack on each so unrelated PRs that add or remove a query do not have to
# touch this file; a real regression moves these by far more.
#
# _MIN_COLLECTED last raised 2050 -> 2150 when the sweep was collecting 2227: the
# floor had drifted 177 below actual, wider than the ~100 of headroom the prior
# bump left, and a slack floor that keeps widening stops being a tripwire. Raise
# it in coarse steps like this one, never by the delta of a single PR.
_MIN_COLLECTED = 2150
_MIN_PLANNED = 1950
_MAX_UNCHECKED = 140

# Statements that cannot be planned today and are NOT fixed here, because each
# belongs to an unrelated subsystem and this commit is the agent_pdp_view
# enrichment bridge. Pinning is a deferral, not a verdict: entry 3 below is not a
# defect at all, merely unplannable.
#
# 🚨 KEYED BY STATEMENT FINGERPRINT, NOT BY FILE. The first version keyed on
# (path, error substring), which granted amnesty to an entire file x error-class:
# a brand-new broken `SELECT merchant_id, payout_iban, tax_id FROM merchants`
# appended to routes/billing_routes.py, and a brand-new unsubstituted `.format()`
# appended to manage_source_quarantine.py, both left the gate GREEN — the two
# broadest pins swallowed them. Demonstrated, then fixed. The fingerprint hashes
# the whitespace-normalized SQL, so reformatting or moving a pinned statement
# keeps its pin while any DIFFERENT statement, in the same file with the same
# error, fails.
#
# THIS LIST MAY ONLY SHRINK. The staleness guard fails if an entry stops
# matching, and the count assertion fails if anything unpinned gets excused. Do
# not add to it to turn a red gate green — that is the failure mode this file
# exists to prevent. Regenerate a fingerprint with DUMP_UNPLANNABLE=1.
_KNOWN_UNPLANNABLE = {
    # (file, error fragment, fingerprint of the exact statement)
    #
    # 1-2. Dead lookups inside a bare `except Exception:`. db/merchants.py declares
    #      `merchants` with PK `id`; there is no `merchant_id`, and schema_guard
    #      never adds one. Genuinely broken.
    ("routes/admin_partner_settlements.py", 'column "merchant_id" does not exist', "f848996c6f8c"),
    ("routes/admin_partner_subsidies.py", 'column "merchant_id" does not exist', "f848996c6f8c"),
    # 3.   NOT A DEFECT — pinned because it cannot be PLANNED, a different claim.
    #      routes/billing_routes.py:1907 is guarded at runtime by
    #      `columns = await _table_columns(db, "merchants")` and returns early when
    #      `merchant_id` is absent: deliberate schema-variance handling. The gate
    #      cannot see a runtime guard, so it must be excused rather than "fixed".
    #      An earlier revision of this list called it a defect. It is not.
    ("routes/billing_routes.py", 'column "merchant_id" does not exist', "d3136c82750d"),
    # 4.   One bind reaching both a text and a varchar column.
    ("routes/billing_routes.py", "inconsistent types deduced for parameter", "6d0ff58c230e"),
    # 5.   `WHERE {seed_domain_match}` is never substituted — the sibling fetch_one
    #      eight lines above calls .format() on the identical template.
    ("scripts/manage_source_quarantine.py", 'syntax error at or near "{"', "6eb33de0dec0"),
    # 6.   `mcp_connected_at` is declared by no migration, model or schema_guard
    #      ALTER anywhere in the repo.
    ("services/merchant_store_service.py", 'column "mcp_connected_at"', "ec6d28aa1ef9"),
    # 7-8. Surfaced only once the column parser above stopped emitting junk tokens
    #      (they hid behind unfaithful tables). Both real: `external_product_seeds`
    #      has no `category` column; `brand_claims` has `verification_status`, not
    #      `status`, so that VERIFIED-claim lookup can never match and `claimed` is
    #      always None — under a comment advertising itself as the fix for an
    #      earlier silent-miss bug.
    ("services/attached_seed_runtime_evidence.py", 'column "category" does not exist', "7a110251d5f0"),
    ("services/catalog_enrichment_agent/apply.py", 'column "status" does not exist', "a34e0f691cd4"),
}


def _sql_fingerprint(sql: str) -> str:
    """Stable short hash of whitespace-normalized SQL. Identifies the STATEMENT,
    not its location, so reformatting or moving it keeps its pin while a
    genuinely different statement gets a different fingerprint."""
    return hashlib.sha1(" ".join(sql.split()).encode("utf-8")).hexdigest()[:12]


def _is_known_unplannable(label: str, message: str, sql: str) -> bool:
    path = label.split(":", 1)[0]
    fingerprint = _sql_fingerprint(sql)
    return any(
        path == known_path and fragment in message and fingerprint == known_fp
        for known_path, fragment, known_fp in _KNOWN_UNPLANNABLE
    )


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------
# Identifiers a statement reads or writes.
#
# BE HONEST ABOUT THE DIRECTION OF ERROR. Over-inclusion (catching a CTE name, a
# LATERAL keyword, an alias) does not invent a false failure — but it does add an
# unknown name to `named`, which then cannot be a subset of `faithful`, which
# demotes a genuine column defect back to "unchecked". So over-inclusion HIDES
# defects. It is the safe direction for CI noise and the unsafe one for coverage;
# both halves are stated here because the first version of this comment claimed
# only the first and was used to justify not caring.
#
# Known escapes, measured: a CTE (`WITH c AS (...)`) contributes `c`; a statement
# with no FROM at all yields an empty `named` and is excluded by the `named and`
# guard in the classifier. Each is a statement this gate does not column-check.
_NOT_A_TABLE = frozenset({"lateral", "only", "unnest", "select"})

_TABLE_REF_RE = re.compile(
    # The leading quote is load-bearing: `FROM "catalog_products"` matched ZERO
    # names without it (the class started at [A-Za-z_]), producing an empty
    # `named` and silently exempting the statement.
    r"\b(?:FROM|JOIN|UPDATE|INTO|USING)\s+(?:ONLY\s+)?(\"?[A-Za-z_][A-Za-z0-9_.\"]*)",
    re.IGNORECASE,
)

# Comments and string literals must go before ANY column-list parsing. A `--`
# comment contributed a literal `--` "column"; a comma inside a comment or inside
# a DEFAULT 'x,y' literal split one column definition into two. Both produce
# tokens that can never appear in information_schema, which pins the table as
# permanently unfaithful — measured, 90 of 99 unfaithful tables were unfaithful
# for exactly this reason, silently disabling the wrong-column check on them.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _strip_sql_noise(sql: str) -> str:
    """Drop comments and blank out string literals, so neither can contribute a
    token or a spurious comma to a column list. Only ever used for PARSING —
    _apply_migrations executes the untouched original."""
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub(" ", sql)
    return _STRING_LITERAL_RE.sub("''", sql)


# The name at the head of a column definition: a quoted identifier (which may
# contain spaces) or a bare one. Anchored so `UNIQUE(a, b)` yields `unique` —
# splitting on whitespace yielded `unique(a,`, which matched nothing in
# _NOT_A_COLUMN and was therefore recorded as a column.
_COLUMN_NAME_RE = re.compile(r'\s*(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_$]*))')


def _bare_table(name: str) -> str:
    """`public."Foo"` -> `foo`. Schema-qualified and quoted forms collapse."""
    return name.replace('"', "").rsplit(".", 1)[-1].lower()


def _tables_named_in(sql: str) -> set:
    named = {_bare_table(m) for m in _TABLE_REF_RE.findall(_strip_sql_noise(sql))}
    return {t for t in named if t and t not in _NOT_A_TABLE}


_ADD_COLUMN_RE = re.compile(
    r"\bALTER\s+TABLE(?:\s+IF\s+EXISTS)?\s+(?:ONLY\s+)?([A-Za-z0-9_.\"]+)"
    r"(.*?)(?=;|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_ADD_COLUMN_NAME_RE = re.compile(
    r"\bADD\s+COLUMN(?:\s+IF\s+NOT\s+EXISTS)?\s+([A-Za-z0-9_\"]+)", re.IGNORECASE
)
_CREATE_TABLE_RE = re.compile(
    r"\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(?:ONLY\s+)?([A-Za-z0-9_.\"]+)\s*\(",
    re.IGNORECASE,
)
# Words that start a table CONSTRAINT clause rather than a column definition.
_NOT_A_COLUMN = {
    "primary", "foreign", "unique", "check", "constraint", "exclude", "like", "partition",
}


def _columns_declared_in_create(body: str, open_paren: int) -> set:
    """Column names from a CREATE TABLE parenthesised body, by depth-0 commas."""
    depth, item, items = 0, [], []
    for ch in body[open_paren:]:
        if ch == "(":
            depth += 1
            if depth == 1:
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                items.append("".join(item))
                break
        if depth == 1 and ch == ",":
            items.append("".join(item))
            item = []
            continue
        if depth >= 1:
            item.append(ch)
    out = set()
    for raw_item in items:
        match = _COLUMN_NAME_RE.match(raw_item)
        if not match:
            continue
        name = (match.group(1) or match.group(2)).lower()
        if name in _NOT_A_COLUMN:
            continue
        out.add(name)
    return out


def _columns_declared_by_migrations() -> Dict[str, set]:
    """table -> every column name db/migrations/*.sql declares for it.

    The yardstick for "did the fixture build this table faithfully". Compared
    against information_schema at fixture time; a table missing any declared
    column is one whose column errors prove nothing. Reading the migrations
    rather than hand-listing tables keeps this correct as schema changes land.
    """
    declared: Dict[str, set] = defaultdict(set)
    for path in (REPO_ROOT / "db" / "migrations").glob("*.sql"):
        # Noise stripped BEFORE the regexes run, so a comma inside a comment or a
        # string literal cannot split a column definition and a `--` cannot be
        # read as a column name.
        body = _strip_sql_noise(path.read_text(encoding="utf-8"))
        for match in _CREATE_TABLE_RE.finditer(body):
            table = _bare_table(match.group(1))
            declared[table] |= _columns_declared_in_create(body, match.end() - 1)
        for table_name, tail in _ADD_COLUMN_RE.findall(body):
            table = _bare_table(table_name)
            for col in _ADD_COLUMN_NAME_RE.findall(tail):
                declared[table].add(col.replace('"', "").lower())
    return declared


def _apply_migrations(raw) -> Tuple[int, int]:
    """Apply db/migrations/*.sql in version order. Returns (applied, failed).

    Failures are tolerated: a dozen migrations target tables this repo creates
    from application code rather than DDL, and a migration that cannot apply
    costs coverage (statements go UNCHECKED) but cannot produce a false PASS.
    Executed whole-file rather than split on `;` so `$$ ... $$` function bodies
    survive, and in AUTOCOMMIT so `CREATE INDEX CONCURRENTLY` is legal.

    Driven through a RAW psycopg2 connection with `autocommit = True`, and with
    no parameter argument. Both details are load-bearing:

    * psycopg2 only treats `%` as a placeholder when `vars` is passed, and these
      files are full of literal percents in comments ("0.1 = 10%").
    * autocommit alone is NOT enough, and this is the subtle one: 30 of these
      files open their own `BEGIN;`. A failure inside one leaves a live aborted
      transaction that autocommit never cleans up, so every later statement —
      including the ones that build the rest of the fixture — dies with
      InFailedSqlTransaction. The fixture collapses instead of the gate
      reporting. Hence the explicit reset after every file.
    """
    from psycopg2 import extensions

    applied = failed = 0
    paths = sorted(
        (REPO_ROOT / "db" / "migrations").glob("*.sql"),
        key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)],
    )
    for path in paths:
        cursor = raw.cursor()
        body = path.read_text(encoding="utf-8")
        try:
            cursor.execute(body)
            applied += 1
        except Exception:  # noqa: BLE001 — see docstring: a failed migration costs coverage only
            failed += 1
        finally:
            cursor.close()
            if raw.info.transaction_status != extensions.TRANSACTION_STATUS_IDLE:
                reset = raw.cursor()
                try:
                    reset.execute("ROLLBACK")
                except Exception:  # noqa: BLE001 — best effort; the next file re-checks
                    pass
                finally:
                    reset.close()
    return applied, failed


@pytest.fixture(scope="module")
def prepare():
    """Yield `prepare(sql)` against a private DATABASE holding the real schema.

    Build order is load-bearing twice over:
      1. `metadata.create_all` FIRST, then main.py's startup DDL, then the
         migrations. Many migrations ALTER tables that `create_all` or the
         startup DDL owns; run them earlier and 52 of them fail, costing a third
         of the coverage. Startup DDL before migrations for the same reason —
         migrations 009/057 ALTER `merchant_psps`, which main.py creates.
      2. Everything happens in a database this fixture created and drops, so the
         `public` the migrations expect is OURS. Nothing is shared with the
         other `test_*_postgres.py` files.
    """
    import asyncpg
    import importlib
    import pkgutil
    import psycopg2

    from sqlalchemy import create_engine

    import db  # noqa: F401  (parent package for the loop below)
    for mod in pkgutil.iter_modules([str(REPO_ROOT / "db")]):
        if mod.name != "migrations":
            try:
                importlib.import_module(f"db.{mod.name}")
            except Exception:  # noqa: BLE001 — a module that will not import owns no tables
                pass
    from db.database import metadata

    # CREATE DATABASE cannot run inside a transaction, hence raw + autocommit.
    admin = psycopg2.connect(DATABASE_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{_GATE_DB}"')
            cur.execute(f'CREATE DATABASE "{_GATE_DB}"')
    except psycopg2.Error as exc:
        admin.close()
        pytest.fail(
            f"could not create the private gate database {_GATE_DB!r}: {exc}. "
            "This gate needs CREATEDB (the CI postgres service runs as superuser). "
            "Failing rather than falling back to the shared database, which the "
            "migrations would mutate under every other test_*_postgres.py file."
        )

    url = _gate_db_url()
    try:
        engine = create_engine(url, future=True)
        metadata.create_all(engine, checkfirst=True)
        engine.dispose()

        raw = psycopg2.connect(url)
        raw.autocommit = True
        # Defensive, not load-bearing on a UTF8 database — measured identical
        # counts with and without it, under both LANG=C and a UTF-8 locale. It
        # matters when the server database is SQL_ASCII, where libpq derives
        # client_encoding from the locale and every migration carrying an
        # em-dash or emoji in a comment dies with "'ascii' codec can't encode
        # character". One line to not depend on how the cluster was initdb'd.
        raw.set_client_encoding("UTF8")
        try:
            for ddl in list(_startup_ddl()) + list(_TEMP_TABLE_DDL):
                cursor = raw.cursor()
                try:
                    cursor.execute(ddl)
                except Exception:  # noqa: BLE001 — costs coverage, cannot cause a false pass
                    pass
                finally:
                    cursor.close()
            applied, failed = _apply_migrations(raw)
            # Which tables did the fixture actually build faithfully? A table is
            # faithful when it carries every column the migrations declare for
            # it. Computed AFTER the migrations, from information_schema, so it
            # reflects what was really built rather than what was attempted.
            actual: Dict[str, set] = defaultdict(set)
            cursor = raw.cursor()
            try:
                cursor.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
                for table_name, column_name in cursor.fetchall():
                    actual[table_name.lower()].add(column_name.lower())
            finally:
                cursor.close()
            declared = _columns_declared_by_migrations()
            faithful = {
                table for table, cols in declared.items()
                # `cols` must be NON-EMPTY. A table the migrations never declare a
                # column for is one whose shape they do not own — it comes from a
                # model or from application bootstrap — and `set() <= anything` is
                # vacuously true, so without this it would count as faithful on the
                # strength of no evidence at all. `product_enrichment` is the live
                # example: 0 migration-declared columns (db/product_enrichment.py
                # owns it), so it is correctly NOT faithful and column errors
                # against it stay unchecked — including, ironically, one of the two
                # tables this commit's own fix is about.
                #
                # This is a WEAKER guarantee than "the fixture matches production".
                # `merchants` has 7 declared columns from migration 103 and so
                # counts as faithful, even though its CREATE lives in
                # db/merchants.py — seven applied ALTERs is the whole evidence base.
                # That is why entry 3 of _KNOWN_UNPLANNABLE is a false positive
                # rather than a defect.
                if cols and table in actual and cols <= actual[table]
            }
        finally:
            raw.close()
        print(f"\n[sql-prepare-gate] db={_GATE_DB} migrations applied={applied} "
              f"failed={failed} | faithful tables={len(faithful)}/{len(declared)}")

        loop = asyncio.new_event_loop()
        pg = loop.run_until_complete(asyncpg.connect(url.replace("+asyncpg", "")))

        def _prepare(sql: str) -> None:
            loop.run_until_complete(pg.prepare(_to_positional(sql)))

        # Tables the fixture built with every column the migrations declare. The
        # classifier reads this to tell "this column is genuinely wrong" from "we
        # never built the table that would have had it".
        _prepare.faithful_tables = faithful

        try:
            yield _prepare
        finally:
            try:
                loop.run_until_complete(pg.close())
            finally:
                loop.close()
    finally:
        # Always, even if setup raised above — a pid-keyed database is never
        # reclaimed by a later run, and DATABASE_URL may point at a shared dev
        # server, not only at CI's disposable container.
        try:
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()", (_GATE_DB,))
                cur.execute(f'DROP DATABASE IF EXISTS "{_GATE_DB}"')
        finally:
            admin.close()


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------
_PYFORMAT = re.compile(r"%\((\w+)\)s")


def _to_positional(sql: str) -> str:
    """Render `:name` binds as `$n`. See the twin helper in the ops-script gate."""
    from sqlalchemy import text as sa_text
    from sqlalchemy.dialects import postgresql

    rendered = sa_text(sql).compile(dialect=postgresql.dialect(paramstyle="pyformat")).string
    order: List[str] = []
    for match in _PYFORMAT.finditer(rendered):
        if match.group(1) not in order:
            order.append(match.group(1))
    numbered = {name: f"${i}" for i, name in enumerate(order, start=1)}
    return _PYFORMAT.sub(lambda m: numbered[m.group(1)], rendered).replace("%%", "%")


# Receivers that ARE a `databases.Database` handle, so their SQL uses the same
# `:name` bind syntax this gate compiles. `database` is the module-level handle;
# the rest are the injectable-handle idiom that exists for testability —
#
#     async def f(..., db: Any = None):
#         read_db = db or database
#         await read_db.fetch_one("SELECT ...")
#
# Sweeping only `database` made every such call site INVISIBLE: 361 call sites,
# 212 of them with directly-collectable literal SQL. That blind spot is not
# incidental — it tracks precisely the modules written to be unit-testable, and
# it is how a `SELECT ... WHERE platform_product_id = :x` against a table whose
# column is `source_product_id` reached production in
# services/agent_pdp_view_assembler.py, inside a best-effort `except` that
# swallowed the UndefinedColumn on every call.
#
# DELIBERATELY EXCLUDED: `conn`, `cur`, `cursor`, `tx`, `connection`. Those are
# raw asyncpg/DBAPI handles whose SQL is written with `$1` or `%s` positional
# binds, not `:name`. Feeding them to the `:name` -> `$n` compiler below would
# report the bind DIALECT as a planning failure. Covering them needs a second
# compile path, not an entry here.
_DB_RECEIVERS = frozenset({"database", "db", "read_db", "write_db", "_db"})


def _receiver_name(func: ast.Attribute) -> str | None:
    """Trailing name of the call receiver: `database`, `db.database` -> 'database'."""
    value = func.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _literal_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_postgres_test(node: ast.AST) -> str | None:
    """Classify an `if` test as an IS_POSTGRES guard: 'positive', 'negative', None."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _is_postgres_test(node.operand)
        return {"positive": "negative", "negative": "positive"}.get(inner or "")
    name = None
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    return "positive" if name == "IS_POSTGRES" else None


def _non_postgres_nodes(tree: ast.AST) -> set:
    """ids of AST nodes that only execute when the backend is NOT Postgres.

    `db/briefs.py` writes `INSERT OR IGNORE` on purpose — it is the SQLite/dev
    branch, sitting behind `if IS_POSTGRES: ...; return`. Flagging it would be a
    false positive, and a gate that cries wolf on deliberate code is a gate
    people learn to silence. Three guard shapes appear in this repo:

        if IS_POSTGRES: ...        else: <sqlite>          -> skip the else
        if not IS_POSTGRES: <sqlite>                       -> skip the body
        if IS_POSTGRES: ...; return   <sqlite falls through> -> skip the tail

    Anything subtler stays IN scope: better a false positive someone must think
    about than a silent hole.
    """
    excluded: set = set()

    def mark(node: ast.AST) -> None:
        for child in ast.walk(node):
            excluded.add(id(child))

    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for index, statement in enumerate(block):
                if not isinstance(statement, ast.If):
                    continue
                verdict = _is_postgres_test(statement.test)
                if verdict == "negative":
                    for child in statement.body:
                        mark(child)
                elif verdict == "positive":
                    for child in statement.orelse:
                        mark(child)
                    # `if IS_POSTGRES: ...; return` — the rest of this block is
                    # the non-Postgres path even though it is not an `else`.
                    if statement.body and isinstance(
                        statement.body[-1], (ast.Return, ast.Raise)
                    ) and not statement.orelse:
                        for sibling in block[index + 1:]:
                            mark(sibling)
    return excluded


def _scopes(tree: ast.AST):
    """The module, then every function body, each as its own name scope.

    Lambdas count. `_own_nodes` stops at one (it is a nested scope), so without
    this the repo's `_with_asyncpg_busy_retry("...", lambda: database.execute(...))`
    idiom is descended into by NOTHING — three production statements in
    routes/agent_payment_sdk.py and services/acp_offsession_payment.py, including
    an INSERT into `payments`, were invisible and uncounted. Indistinguishable
    from clean, which is the failure mode this whole file is against.
    """
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield node


def _own_nodes(scope: ast.AST):
    """Descendants of `scope` that are NOT inside a nested function or class.

    Scope discipline is load-bearing here: a loop variable called `sql` is one
    of the most common names in this repo, and a module-wide map cheerfully
    attributes one function's statements to another function's call site. That
    is not merely untidy — it reports defects against innocent line numbers.
    """
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _loop_bound_sql(scope: ast.AST) -> Dict[str, List[str]]:
    """Resolve `for _, sql in [( 'a', "SELECT.."), ...]: database.execute(sql)`.

    Worth the AST work rather than being written off as "dynamic": this exact
    idiom is how `services/catalog_sync_service.py` issues its three tombstone
    statements, and all three are defective. A call-site-only sweep sees a loop
    variable, shrugs, and reports nothing — which is indistinguishable from a
    clean result. Returns {loop_variable_name: [sql, ...]}.
    """
    collections: Dict[str, ast.AST] = {}
    for node in _own_nodes(scope):
        target, value = None, None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and isinstance(value, (ast.List, ast.Tuple)):
            collections[target.id] = value

    bound: Dict[str, List[str]] = defaultdict(list)
    for node in _own_nodes(scope):
        if not isinstance(node, ast.For):
            continue
        source = node.iter
        if isinstance(source, ast.Name):
            source = collections.get(source.id)
        if not isinstance(source, (ast.List, ast.Tuple)):
            continue

        # `for sql in [ "SELECT ...", ... ]`
        if isinstance(node.target, ast.Name):
            for element in source.elts:
                text = _literal_str(element)
                if text:
                    bound[node.target.id].append(text)
            continue

        # `for name, sql in [ ("x", "SELECT ..."), ... ]`
        if isinstance(node.target, ast.Tuple):
            for position, name in enumerate(node.target.elts):
                if not isinstance(name, ast.Name):
                    continue
                for element in source.elts:
                    if isinstance(element, ast.Tuple) and position < len(element.elts):
                        text = _literal_str(element.elts[position])
                        if text:
                            bound[name.id].append(text)
    return bound


def collect_statements() -> List[Tuple[str, str]]:
    """Every `database.<method>(<str literal>, ...)` reachable statically.

    Also resolves a module-level constant passed by name (how the ops scripts
    and several services spell their longer queries), and the loop-over-literals
    idiom above.
    """
    found: List[Tuple[str, str]] = []
    paths = [REPO_ROOT / name for name in _SWEPT_FILES]
    for directory in _SWEPT_DIRS:
        paths.extend(sorted((REPO_ROOT / directory).rglob("*.py")))

    for path in paths:
        if "__pycache__" in path.parts or "migrations" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue

        constants: Dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
        rel = path.relative_to(REPO_ROOT)
        sqlite_only = _non_postgres_nodes(tree)

        for scope in _scopes(tree):
            loop_bound = _loop_bound_sql(scope)
            for node in _own_nodes(scope):
                if not isinstance(node, ast.Call) or id(node) in sqlite_only:
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr not in _DB_METHODS:
                    continue
                if _receiver_name(func) not in _DB_RECEIVERS or not node.args:
                    continue
                first = node.args[0]
                resolved: List[str] = []
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    resolved = [first.value]
                elif isinstance(first, ast.Name):
                    if first.id in loop_bound:
                        resolved = loop_bound[first.id]
                    elif first.id in constants:
                        resolved = [constants[first.id]]
                for index, sql in enumerate(resolved):
                    if not sql.strip():
                        continue
                    suffix = f" [{index + 1}/{len(resolved)}]" if len(resolved) > 1 else ""
                    found.append((f"{rel}:{node.lineno}{suffix}", sql))
    return found


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
def test_every_sql_literal_in_the_repo_can_be_planned(prepare, capsys) -> None:
    statements = collect_statements()
    assert len(statements) >= _MIN_COLLECTED, (
        f"collected only {len(statements)} statements, floor is {_MIN_COLLECTED}; "
        "the AST sweep stopped matching call sites it used to match. A collector "
        "that finds nothing makes this gate green having planned nothing."
    )

    classes: Counter = Counter()
    defects: Dict[str, List[str]] = defaultdict(list)
    known_hits: set = set()

    for label, sql in statements:
        try:
            # Rendered separately from the PREPARE so a bind SQLAlchemy cannot
            # even compile is reported as its own class rather than as a
            # Postgres verdict it never reached.
            _to_positional(sql)
        except Exception as exc:  # noqa: BLE001 — an unrenderable bind is itself a defect
            classes[f"render:{type(exc).__name__}"] += 1
            defects["render"].append(f"  {label}\n    !! {type(exc).__name__}: {exc}")
            continue
        try:
            prepare(sql)
            classes["planned"] += 1
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            named = _tables_named_in(sql)
            if (
                name == "UndefinedColumnError"
                and named
                and named <= getattr(prepare, "faithful_tables", set())
            ):
                # Every table this statement names carries all the columns the
                # migrations declare for it, so the fixture's copy IS the real
                # schema and the column genuinely does not exist. Postgres
                # resolves relations before columns, so reaching a column error
                # already proves the tables resolved; the faithfulness check
                # above removes the one remaining fixture excuse, a table left
                # half-built. Blanket-exempting UndefinedColumnError instead let
                # a `WHERE platform_product_id = :x` against catalog_products —
                # whose column is `source_product_id` — sit in production inside
                # a best-effort `except`, dead on every call.
                name = "UndefinedColumnError:real"
            classes[name] += 1
            if name not in _FIXTURE_GAP:
                if _is_known_unplannable(label, str(exc), sql):
                    known_hits.add(
                        (label.split(":", 1)[0], str(exc), _sql_fingerprint(sql))
                    )
                    classes["known_unplannable"] += 1
                    continue
                if os.getenv("DUMP_UNPLANNABLE"):
                    print(f'    ("{label.split(":", 1)[0]}", '
                          f'"{str(exc).splitlines()[0][:60]}", "{_sql_fingerprint(sql)}"),')
                body = " ".join(sql.split())[:200]
                defects[name].append(f"  {label}\n    !! {name}: {exc}\n    {body}")

    planned = classes["planned"]
    unchecked = sum(count for name, count in classes.items() if name in _FIXTURE_GAP)

    with capsys.disabled():
        print(f"\n[sql-prepare-gate] {len(statements)} collected | {planned} planned | "
              f"{unchecked} unchecked (fixture gaps) | "
              f"{classes['known_unplannable']} known-unplannable (pinned)")

    # A pin that no longer matches anything is debt that was paid without the
    # ledger being updated. Left alone it turns into permanent cover for a future
    # defect at the same path — the exemption stays, the statement it excused is
    # gone, and the next broken statement in that file inherits the pass.
    stale = sorted(
        f"{path} :: {fragment} :: {fingerprint}"
        for path, fragment, fingerprint in _KNOWN_UNPLANNABLE
        if not any(
            p == path and fragment in message and fp == fingerprint
            for p, message, fp in known_hits
        )
    )
    assert not stale, (
        f"{len(stale)} entr(y/ies) in _KNOWN_UNPLANNABLE no longer match a failing "
        "statement. If you fixed one, delete its line — an exemption that outlives "
        "its statement is dead weight. If you only REFORMATTED the statement, "
        "regenerate its fingerprint with DUMP_UNPLANNABLE=1.\n  "
        + "\n  ".join(stale)
    )

    # The other direction: nothing may be excused that is not pinned. Compared on
    # (path, fingerprint) so one pinned statement appearing at two call sites is
    # still one entry. Without this the permissive bucket could grow in silence.
    distinct_excused = {(path, fp) for path, _msg, fp in known_hits}
    expected_excused = {(path, fp) for path, _frag, fp in _KNOWN_UNPLANNABLE}
    assert distinct_excused == expected_excused, (
        "the set of excused statements does not match _KNOWN_UNPLANNABLE.\n"
        f"  excused but not pinned: {sorted(distinct_excused - expected_excused)}\n"
        f"  pinned but not excused: {sorted(expected_excused - distinct_excused)}"
    )

    total_defects = sum(len(v) for v in defects.values())
    assert not total_defects, (
        f"{total_defects} statement(s) cannot be planned by Postgres.\n"
        "These are DIALECT defects, not fixture gaps — Postgres resolved every name "
        "and then refused the statement.\n"
        "  IndeterminateDatatype / AmbiguousParameter -> a bind Postgres cannot type "
        "from position, or one typed two ways in the same statement. Wrap it: "
        "CAST(:x AS text).\n"
        "  CannotCoerce / DatatypeMismatch -> the expression's type does not match "
        "the column's (json vs jsonb is the usual one).\n"
        "  PostgresSyntaxError -> the SQL is not valid Postgres at all.\n\n"
        + "\n".join(f"-- {name} ({len(items)})\n" + "\n".join(items)
                    for name, items in sorted(defects.items()))
    )

    # Coverage floors. `planned` guards vacuity; `unchecked` guards the subtler
    # failure — a fixture hole MASKS defects behind UndefinedTable, so it must
    # not grow silently.
    assert planned >= _MIN_PLANNED, (
        f"only {planned} statements were planned, floor is {_MIN_PLANNED}. The "
        "fixture stopped resolving names it used to resolve; coverage regressed."
    )
    assert unchecked <= _MAX_UNCHECKED, (
        f"{unchecked} statements went UNCHECKED (ceiling {_MAX_UNCHECKED}) because "
        "Postgres could not resolve a table/column/function. Every one of these is "
        "a statement this gate did NOT prove — and a defect inside one is invisible. "
        "Add the missing DDL, or raise the ceiling with the reason."
    )


def test_the_sweep_detects_each_failure_mode_it_claims_to(prepare) -> None:
    """A gate that passes both before and after the fix is worthless.

    One probe per class this file promises to catch, run through the same
    `prepare` the sweep uses.
    """
    cases = {
        # #1703: variadic "any" gives Postgres nothing to infer from.
        "could not determine data type": (
            "UPDATE catalog_products SET suppression_metadata = "
            "jsonb_build_object('k', :v) WHERE product_key = :pk"
        ),
        # SQLite syntax that no Postgres will accept.
        "syntax error": "INSERT OR IGNORE INTO catalog_products (product_key) VALUES (:pk)",
    }
    for expected, sql in cases.items():
        with pytest.raises(Exception) as caught:
            prepare(sql)
        assert expected in str(caught.value), (
            f"expected {expected!r} for {sql!r}, got "
            f"{type(caught.value).__name__}: {caught.value}"
        )

    # ...and the fixed forms must plan, or the gate is just rejecting everything.
    prepare(
        "UPDATE catalog_products SET suppression_metadata = "
        "jsonb_build_object('k', CAST(:v AS text)) WHERE product_key = :pk"
    )
    prepare(
        "INSERT INTO catalog_products (product_key) VALUES (:pk) ON CONFLICT DO NOTHING"
    )


def test_a_fixture_gap_is_reported_as_unchecked_not_as_a_pass(prepare) -> None:
    """The masking hazard, pinned.

    An unresolvable name must raise — it must NOT quietly plan. This is what
    makes `_MAX_UNCHECKED` meaningful: every unchecked statement is one whose
    defects, if any, this gate cannot see.
    """
    with pytest.raises(Exception) as caught:
        prepare("SELECT * FROM a_table_that_does_not_exist WHERE x = :x")
    assert type(caught.value).__name__ in _FIXTURE_GAP


# ---------------------------------------------------------------------------
# self-tests for the classifier machinery
# ---------------------------------------------------------------------------
# None of the helpers below had any coverage when they were introduced, and the
# column parser shipped with a defect that made 90 of 99 unfaithful tables
# unfaithful for bogus reasons — silently disabling the wrong-column check on
# them. A table-driven test catches that outright, so here it is. These need no
# database; they live in this file because they test THIS file.

@pytest.mark.parametrize("body,expected", [
    ("CREATE TABLE t (a int, b text)", {"a", "b"}),
    # constraints must not read as columns, with OR WITHOUT a space before "("
    ("CREATE TABLE t (a int, UNIQUE(a, b))", {"a"}),
    ("CREATE TABLE t (a int, UNIQUE (a, b))", {"a"}),
    ("CREATE TABLE t (a int, CHECK(a > 0))", {"a"}),
    ("CREATE TABLE t (a int, b int, PRIMARY KEY (a, b))", {"a", "b"}),
    ("CREATE TABLE t (a int, CONSTRAINT c FOREIGN KEY (a) REFERENCES u(id))", {"a"}),
    # a line comment must contribute neither a column nor a comma
    ("CREATE TABLE t (\n  -- the id, and more\n  a int,\n  b int\n)", {"a", "b"}),
    ("CREATE TABLE t (a int, -- note, with comma\n b int)", {"a", "b"}),
    ("CREATE TABLE t (/* x, y */ a int, b int)", {"a", "b"}),
    # a comma inside a string literal must not split a definition
    ("CREATE TABLE t (a text DEFAULT 'x,y', b int)", {"a", "b"}),
    # quoted identifiers, including one containing a space
    ('CREATE TABLE t ("b c" int, d int)', {"b c", "d"}),
    # nested type parens
    ("CREATE TABLE t (a numeric(10, 2), b int)", {"a", "b"}),
])
def test_the_column_parser_reads_real_ddl_shapes(body: str, expected: set) -> None:
    match = _CREATE_TABLE_RE.search(body)
    assert match, body
    assert _columns_declared_in_create(_strip_sql_noise(body), match.end() - 1) == expected


def test_no_declared_column_is_parse_debris() -> None:
    """The assertion that would have caught the original parser bug.

    Every name the parser attributes to a table must LOOK like an identifier. A
    junk token (`unique(a,`, `--`, `y'`) can never appear in information_schema,
    so the table it is attributed to becomes permanently unfaithful and its
    column errors are written off as fixture gaps forever — the check silently
    switches itself off, with no failing test anywhere.
    """
    legal = re.compile(r"^[a-z_][a-z0-9_$ ]*$")
    debris = sorted(
        f"{table}.{column}"
        for table, columns in _columns_declared_by_migrations().items()
        for column in columns
        if not legal.match(column)
    )
    assert not debris, (
        f"{len(debris)} parsed 'column' name(s) are not identifiers — the column "
        "parser is mis-reading migration DDL, which silently exempts their tables "
        "from the wrong-column check:\n  " + "\n  ".join(debris[:40])
    )


@pytest.mark.parametrize("sql,expected", [
    ("SELECT 1 FROM catalog_products", {"catalog_products"}),
    ("SELECT 1 FROM catalog_products cp", {"catalog_products"}),
    ("SELECT 1 FROM public.catalog_products", {"catalog_products"}),
    # the quoted form matched NOTHING before the leading `"?` was added, which
    # emptied `named` and silently exempted the statement
    ('SELECT 1 FROM "catalog_products"', {"catalog_products"}),
    ('SELECT 1 FROM public."catalog_products"', {"catalog_products"}),
    ("UPDATE agent_pdp_view SET x = 1", {"agent_pdp_view"}),
    ("INSERT INTO agent_pdp_view (a) VALUES (1)", {"agent_pdp_view"}),
    ("DELETE FROM agent_pdp_view WHERE x = 1", {"agent_pdp_view"}),
    ("SELECT 1 FROM a JOIN b ON a.i = b.i", {"a", "b"}),
    # `LATERAL` is a keyword, not a table. The subquery alias `s` follows the
    # closing paren with no FROM/JOIN before it, so it is never captured either.
    ("SELECT 1 FROM a JOIN LATERAL (SELECT 1) s ON true", {"a"}),
    # a FROM inside a string literal must not be scanned
    ("SELECT 1 FROM a WHERE x = 'FROM zzz'", {"a"}),
])
def test_table_reference_extraction(sql: str, expected: set) -> None:
    assert _tables_named_in(sql) == expected


def test_a_cte_name_is_still_mistaken_for_a_table() -> None:
    """A KNOWN, UNFIXED escape, pinned so it stays visible rather than folklore.

    `_tables_named_in` cannot tell a CTE from a table, so a statement using one
    carries an unknown name, fails the faithful-subset test, and is exempted from
    the wrong-column check. That direction HIDES defects. Asserted here so that
    the day someone teaches the collector about CTEs, this test fails and the
    limit gets deleted from the docs along with it.
    """
    assert _tables_named_in("WITH c AS (SELECT 1) SELECT * FROM c") == {"c"}


def test_fingerprint_ignores_formatting_but_not_content() -> None:
    a = "SELECT merchant_id   FROM\n  merchants"
    b = "SELECT merchant_id FROM merchants"
    c = "SELECT merchant_id, tax_id FROM merchants"
    assert _sql_fingerprint(a) == _sql_fingerprint(b)
    assert _sql_fingerprint(a) != _sql_fingerprint(c)


def test_a_pin_excuses_only_its_own_statement() -> None:
    """The hole this keying closes: keyed on (path, error) alone, a pin was an
    amnesty for a whole file x error-class, and a brand-new broken statement in a
    pinned file stayed green."""
    pinned_path, pinned_fragment, pinned_fp = sorted(_KNOWN_UNPLANNABLE)[0]
    label = f"{pinned_path}:1"
    message = f"blah {pinned_fragment} blah"

    # A different statement in the same file, failing the same way, is NOT excused.
    assert not _is_known_unplannable(label, message, "SELECT something_else FROM merchants")
    # A different file carrying the pinned statement is NOT excused either.
    assert not _is_known_unplannable("routes/other.py", message, "x")
    # And every pin still carries a fingerprint.
    assert all(len(fp) == 12 for _p, _f, fp in _KNOWN_UNPLANNABLE)
