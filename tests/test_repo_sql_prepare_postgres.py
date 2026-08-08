"""Every SQL literal this repo hands to `database.*` must be PLANNABLE by Postgres.

THE WIDENING. `tests/test_ops_script_sql_prepare_postgres.py` proves the idea on
four ops scripts by DRIVING them. This file takes the other half: a static sweep
over every `database.<method>("...")` call site in the repo, asking Postgres to
PREPARE each one. The two are complementary and neither subsumes the other —

    driven   sees `.format()`/f-string SQL, but only where a test drives it;
    static   sees every literal call site, but only literal ones.

1,479 of this repo's ~3,200 `database.*` calls build their SQL dynamically and
are invisible to a static sweep. That is a real limit, stated here rather than
left to be inferred from a green run.

WHY IT PAYS. The first sweep found NINE statements that cannot be planned, all
in code shipped since May 2026 and all fixed in the commit that adds this file:
three in the `catalog_sync_service` tombstone prune (#1703's own defect, in a
live sync path), one in `pdp_matcher`, two `products_cache` backfills, one in
`subject_resolve`, one in `commerce_attribution_service`, and a `jsonb`-into-
`json` COALESCE in `routes/buyer_api.py` that sits inside `except Exception:
pass` — a silent no-op since #281 rather than a visible 500. That is the #1588
story again: the repo's tests run on SQLite, and SQLite cannot see any of it.

THE FIXTURE IS THE WHOLE BALLGAME. PREPARE resolves names against a real schema,
so what this gate can see is bounded by what the schema has. Each layer bought
real coverage, of 1,954 collected statements:

    metadata.create_all only .................  751 planned
    + db/migrations/*.sql .................... 1,518 planned
    + main.py's startup DDL .................. 1,790 planned
    + create_all isolated from `public` ...... 1,826 planned,  128 unchecked

The 3 `stale_catalog_*` stubs matter for the same reason — they are TEMP tables
the sync prune creates at runtime, and without them three genuinely-broken
statements in `services/catalog_sync_service.py` fail with `UndefinedTable` and
get written off as a fixture gap. A fixture hole does not just lose coverage, it
MASKS defects, which is why the unchecked count is pinned below rather than
merely reported.

🚨 THIS GATE DOES NOT SHARE THE PUBLIC SCHEMA. It builds a private schema and
drops it. That is not a style preference: applying 220 migrations to the database
every other `test_*_postgres.py` file shares is exactly the blast radius the
warning in tests/test_canonical_feed_tombstoned_flag_postgres.py describes, one
order of magnitude up. The sibling gates keep `metadata.create_all` + DELETE on
`public`; this one never touches it. Precedent for the private-schema shape:
tests/test_pdp_scope_backfill_postgres.py.
"""

from __future__ import annotations

import ast
import asyncio
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

_SCHEMA = f"sql_prepare_gate_{os.getpid()}"

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
_MIN_COLLECTED = 1900
_MIN_PLANNED = 1790
_MAX_UNCHECKED = 150


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------
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
        try:
            cursor.execute(path.read_text(encoding="utf-8"))
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
    """Yield `prepare(sql)` against a private schema holding the real schema.

    Build order is load-bearing: `metadata.create_all` FIRST, then migrations.
    Many migrations ALTER tables that `create_all` owns, so the other order
    loses 52 of them and a third of the coverage.
    """
    import asyncpg
    import importlib
    import pkgutil

    from sqlalchemy import create_engine, text

    import db  # noqa: F401  (parent package for the loop below)
    for mod in pkgutil.iter_modules([str(REPO_ROOT / "db")]):
        if mod.name != "migrations":
            try:
                importlib.import_module(f"db.{mod.name}")
            except Exception:  # noqa: BLE001 — a module that will not import owns no tables
                pass
    from db.database import metadata

    admin = create_engine(DATABASE_URL, future=True)
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))

    # `public` stays on the path for extensions (pg_trgm et al), but the private
    # schema is FIRST, so every table this gate resolves is one it built itself.
    # create_all runs with the private schema ALONE on the path. With `public`
    # visible, `checkfirst=True` finds the sibling gates' `catalog_products`
    # there, skips creating ours, and a later migration's
    # `CREATE TABLE IF NOT EXISTS` then builds a PARTIAL one — a real bug this
    # gate hit: `catalog_products` ended up without `suppression_metadata`, so
    # every statement touching that column silently became a fixture gap. The
    # shared-database hazard from the other direction.
    creator = create_engine(
        DATABASE_URL, future=True,
        connect_args={"options": f"-csearch_path={_SCHEMA}"},
    )
    metadata.create_all(creator, checkfirst=True)
    creator.dispose()

    import psycopg2

    raw = psycopg2.connect(DATABASE_URL, options=f"-csearch_path={_SCHEMA},public")
    raw.autocommit = True
    try:
        applied, failed = _apply_migrations(raw)
        for ddl in list(_startup_ddl()) + list(_TEMP_TABLE_DDL):
            cursor = raw.cursor()
            try:
                cursor.execute(ddl)
            except Exception:  # noqa: BLE001 — costs coverage, cannot cause a false pass
                pass
            finally:
                cursor.close()
    finally:
        raw.close()
    print(f"\n[sql-prepare-gate] schema={_SCHEMA} migrations applied={applied} failed={failed}")

    loop = asyncio.new_event_loop()
    pg = loop.run_until_complete(
        asyncpg.connect(
            DATABASE_URL.replace("+asyncpg", ""),
            server_settings={"search_path": f"{_SCHEMA},public"},
        )
    )

    def _prepare(sql: str) -> None:
        loop.run_until_complete(pg.prepare(_to_positional(sql)))

    try:
        yield _prepare
    finally:
        loop.run_until_complete(pg.close())
        loop.close()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
        admin.dispose()


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
    """The module, then every function body, each as its own name scope."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
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
                if _receiver_name(func) != "database" or not node.args:
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
            classes[name] += 1
            if name not in _FIXTURE_GAP:
                body = " ".join(sql.split())[:200]
                defects[name].append(f"  {label}\n    !! {name}: {exc}\n    {body}")

    planned = classes["planned"]
    unchecked = sum(count for name, count in classes.items() if name in _FIXTURE_GAP)

    with capsys.disabled():
        print(f"\n[sql-prepare-gate] {len(statements)} collected | {planned} planned | "
              f"{unchecked} unchecked (fixture gaps)")

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
