"""The Reap operator audit tables (migrations 253, 255, 257) each heal in their own try block.

Production never runs db/migrations; `ensure_required_schema_light` is the only thing that builds
these tables, and each of its branches is a chain of best-effort try blocks in which a raising
statement abandons everything after it IN THAT BLOCK. The 253 and 255 CREATE TABLEs used to sit
inside the mig-224 block, between the enrollments and the purchases DDL, so a failing enrollment
index skipped both audit tables and a failing audit DDL skipped the purchases table. The 257
table is created last by `ensure_continuation_schema`, behind statements that can fail on their
own. Each now has its own try, after the mig-224 block, in both dialects.

Pinned two ways: structurally from the AST (the placement), and by making the neighbouring DDL
fail on a real SQLite database and checking the tables still appear (the behaviour). The Postgres
twin is tests/test_schema_guard_reap_audit_tables_postgres.py.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from db.database import IS_POSTGRES

SOURCE = (Path(__file__).resolve().parents[1] / "db" / "schema_guard.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

AUDIT_TABLES = {
    253: "reap_checkout_manual_resolution_audit",
    255: "reap_unopened_attempt_retirements",
    257: "reap_checkout_dispatch_resolution_audit",
}
MIG_224 = "CREATE TABLE IF NOT EXISTS reap_agentic_purchases"


def _heal_function() -> ast.AsyncFunctionDef:
    return next(n for n in ast.walk(TREE)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "ensure_required_schema_light")


def _branches():
    """{'postgres': If-node body, 'sqlite': If-node body} of the heal function."""
    out = {}
    for node in ast.walk(_heal_function()):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
            if node.test.id == "IS_POSTGRES" and "postgres" not in out:
                out["postgres"] = node
            elif node.test.id == "IS_SQLITE" and "sqlite" not in out:
                out["sqlite"] = node
    assert set(out) == {"postgres", "sqlite"}
    return out


def _innermost_tries(branch: ast.If, predicate):
    """For every node under `branch.body` matching `predicate`, the innermost enclosing Try."""
    found = []

    def walk(node, stack):
        if predicate(node):
            found.append(stack[-1] if stack else None)
        for child in ast.iter_child_nodes(node):
            walk(child, stack + [node] if isinstance(node, ast.Try) and child in node.body else stack)

    for statement in branch.body:
        walk(statement, [])
    return found


def _mentions(text: str):
    return lambda node: isinstance(node, ast.Constant) and isinstance(node.value, str) and text in node.value


def _imports_resolution_audit(node) -> bool:
    return (isinstance(node, ast.ImportFrom) and node.module == "db.reap_continuation"
            and any(alias.name == "_RESOLUTION_AUDIT" for alias in node.names))


def _locator(number: int):
    if number == 257:
        return _imports_resolution_audit
    return _mentions(f"CREATE TABLE IF NOT EXISTS {AUDIT_TABLES[number]}")


def _create_tables_in(node: ast.AST):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "CREATE TABLE IF NOT EXISTS" in n.value]


@pytest.mark.parametrize("dialect", ["postgres", "sqlite"])
@pytest.mark.parametrize("number", sorted(AUDIT_TABLES))
def test_each_audit_table_has_its_own_try_after_the_mig_224_block(dialect, number):
    branch = _branches()[dialect]
    [mig224] = _innermost_tries(branch, _mentions(MIG_224))
    [own] = _innermost_tries(branch, _locator(number))
    assert own is not None and own is not mig224
    assert own.lineno > mig224.end_lineno, "after the mig-224 block, not before or inside it"
    # Its try holds nothing else that can raise before it: one statement's DDL, nothing more.
    assert not any(isinstance(n, ast.Try) for n in ast.walk(own) if n is not own)
    creates = _create_tables_in(own)
    if number == 257:
        assert creates == []  # the DDL is the module's constant, imported, not respelled
        calls = [n for n in ast.walk(own) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "ensure_continuation_schema"]
        assert calls == []
    else:
        assert len(creates) == 1 and AUDIT_TABLES[number] in creates[0]
    # A failure is logged and swallowed: startup must go on.
    [handler] = own.handlers
    assert not any(isinstance(n, ast.Raise) for n in ast.walk(handler))
    assert f"(mig {number})" in ast.get_source_segment(SOURCE, handler)


@pytest.mark.parametrize("dialect", ["postgres", "sqlite"])
def test_the_mig_224_block_no_longer_carries_the_audit_tables(dialect):
    [mig224] = _innermost_tries(_branches()[dialect], _mentions(MIG_224))
    creates = " ".join(_create_tables_in(mig224))
    for table in AUDIT_TABLES.values():
        assert table not in creates
    assert "reap_agentic_enrollments" in creates and "reap_agentic_purchases" in creates


def test_the_257_heal_uses_the_modules_ddl_with_the_modules_sqlite_spelling():
    from db import reap_continuation

    assert "CREATE TABLE IF NOT EXISTS reap_checkout_dispatch_resolution_audit" in reap_continuation._RESOLUTION_AUDIT
    module = Path(reap_continuation.__file__).read_text(encoding="utf-8")
    assert "_RESOLUTION_AUDIT.replace('TIMESTAMPTZ', 'TIMESTAMP')" in module
    assert SOURCE.count("await database.execute(_RESOLUTION_AUDIT.replace('TIMESTAMPTZ', 'TIMESTAMP'))") == 1
    assert SOURCE.count("await database.execute(_RESOLUTION_AUDIT)") == 1


# ── behaviour, on a real SQLite database ──────────────────────────────────────────────────────

sqlite_only = pytest.mark.skipif(IS_POSTGRES, reason="SQLite arm; see the _postgres twin")


@pytest.fixture
async def heal(monkeypatch):
    """Run the real self-heal with chosen statements failing; restore the schema afterwards."""
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    if not database.is_connected:
        await database.connect()
    real = database.execute

    async def run(*failing: str, drop=()):
        for table in drop:
            await real(f"DROP TABLE IF EXISTS {table}")

        async def execute(query, *args, **kwargs):
            if any(marker in str(query) for marker in failing):
                raise RuntimeError("synthetic DDL failure")
            return await real(query, *args, **kwargs)

        monkeypatch.setattr(database, "execute", execute)
        try:
            await ensure_required_schema_light()
        finally:
            monkeypatch.setattr(database, "execute", real)
        rows = await database.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
        return {r["name"] for r in rows}

    yield run
    await ensure_required_schema_light()


@sqlite_only
async def test_a_failing_enrollment_index_no_longer_skips_the_audit_tables(heal):
    tables = await heal("uq_reap_agentic_enrollments_one_active", drop=AUDIT_TABLES.values())
    assert set(AUDIT_TABLES.values()) <= tables


@sqlite_only
@pytest.mark.parametrize("number", [253, 255])
async def test_a_failing_audit_table_no_longer_skips_the_purchases_table(heal, number):
    tables = await heal(f"CREATE TABLE IF NOT EXISTS {AUDIT_TABLES[number]}",
                        drop=["reap_agentic_purchases", AUDIT_TABLES[number]])
    assert "reap_agentic_purchases" in tables and AUDIT_TABLES[number] not in tables
    others = set(AUDIT_TABLES.values()) - {AUDIT_TABLES[number]}
    assert others <= tables


@sqlite_only
async def test_a_failing_continuation_heal_no_longer_skips_the_257_table(heal):
    # The journal's append-only trigger is created before the 257 table inside
    # ensure_continuation_schema; failing it used to leave the audit table missing.
    tables = await heal("reap_dispatch_events_no_update",
                        drop=["reap_checkout_dispatch_resolution_audit"])
    assert "reap_checkout_dispatch_resolution_audit" in tables
