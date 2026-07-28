"""
Regression tests for a latent crash class: passing a SQLAlchemy ``text(...)``
ClauseElement together with a params dict to a ``databases.Database`` call.

``databases`` 0.7.0 ``Connection._build_query`` does::

    if isinstance(query, str):
        query = text(query)
        return query.bindparams(**values) if values is not None else query
    elif values:
        return query.values(**values)          # <-- ClauseElement branch

``TextClause`` has no ``.values()``, so ``database.fetch_val(text(SQL), params)``
raises ``AttributeError`` unconditionally -- on every backend, before any driver
is touched. Passing the *same* SQL as a plain ``str`` with the same ``:name``
placeholders takes the first branch and works.

Why this survived review and CI: the repo's usual idiom is to monkeypatch the
module-level ``database`` with a fake object that duck-types
``fetch_val(query, values)``. A fake never runs ``_build_query``, so it accepts
a ``TextClause`` happily. Likewise a *sync* ``sqlalchemy.Connection.execute``
handles ``text(...) + params`` correctly. Only a real ``databases.Database``
reproduces it -- which is what these tests use.

SQLite is sufficient: ``_build_query`` raises before dialect/driver work, so
these tests reproduce the bug without a Postgres service and therefore actually
run in CI.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest
from databases import Database
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
async def _sqlite_database(tmp_path, ddl: List[str], seed: List[str]) -> Database:
    """Build a real async databases.Database over a seeded on-disk SQLite file."""
    db_path = os.path.join(str(tmp_path), "t.db")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
        for stmt in seed:
            conn.execute(text(stmt))
    engine.dispose()

    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.connect()
    return db


# --------------------------------------------------------------------------
# 1. the raw contract -- pins the library behaviour the two fixes depend on
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_databases_rejects_textclause_with_params_but_accepts_str(tmp_path) -> None:
    db = await _sqlite_database(
        tmp_path,
        ddl=["CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"],
        seed=["INSERT INTO t (id, name) VALUES (1, 'a')"],
    )
    try:
        # The trap: ClauseElement + params.
        with pytest.raises(AttributeError, match="no attribute 'values'"):
            await db.fetch_val(text("SELECT name FROM t WHERE id = :id"), {"id": 1})

        # The idiom the repo should use everywhere: plain str + params.
        assert await db.fetch_val("SELECT name FROM t WHERE id = :id", {"id": 1}) == "a"
    finally:
        await db.disconnect()


# --------------------------------------------------------------------------
# 2. site 1 -- routes/order_routes.py _log_shopify_receipt_suppressed_once
#
# The failing fetch_val sits inside `try: ... except Exception: pass`, so the
# AttributeError is swallowed: `existing` is never bound, the `if existing:
# return` short-circuit is skipped, and control falls through to
# log_order_event(). Symptom is NOT a 500 -- it is that the "once" in the
# function name is void and every call writes a duplicate order_event.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shopify_receipt_suppressed_event_is_logged_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import routes.order_routes as module

    db = await _sqlite_database(
        tmp_path,
        ddl=[
            "CREATE TABLE order_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " order_id TEXT,"
            " event_type TEXT)"
        ],
        seed=[],
    )

    calls: List[Dict[str, Any]] = []

    async def _fake_log_order_event(**kwargs: Any) -> None:
        # Mirror the real side effect so the dedupe SELECT has something to find.
        calls.append(kwargs)
        await db.execute(
            "INSERT INTO order_events (order_id, event_type) VALUES (:oid, :et)",
            {"oid": kwargs["order_id"], "et": kwargs["event_type"]},
        )

    monkeypatch.setattr(module, "database", db)
    monkeypatch.setattr(module, "log_order_event", _fake_log_order_event)

    try:
        for _ in range(2):
            await module._log_shopify_receipt_suppressed_once(
                order_id="ord_1",
                merchant_id="m_1",
                total_amount=10.0,
                currency="USD",
                metadata={},
            )

        # Pre-fix this is 2: the swallowed AttributeError defeats the dedupe.
        assert len(calls) == 1, f"expected exactly one event, got {len(calls)}"
        rows = await db.fetch_all(
            "SELECT id FROM order_events WHERE order_id = :oid AND event_type = :et",
            {"oid": "ord_1", "et": "shopify_receipt_suppressed"},
        )
        assert len(rows) == 1
    finally:
        await db.disconnect()


# --------------------------------------------------------------------------
# 3. site 2 -- services/ugc_capabilities_service.py
#    get_product_group_member_product_ids
#
# The failing fetch_all sits inside `try: ... except Exception: return set()`,
# so the AttributeError is swallowed into an empty result. Symptom is NOT a 500
# -- it is a silent false negative: group members never contribute to UGC
# purchase matching, so a buyer who bought a sibling variant in the product
# group is wrongly judged ineligible.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_product_group_member_ids_are_returned(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.ugc_capabilities_service as module

    db = await _sqlite_database(
        tmp_path,
        ddl=[
            "CREATE TABLE product_group_members ("
            " product_group_id TEXT,"
            " platform_product_id TEXT)"
        ],
        seed=[
            "INSERT INTO product_group_members VALUES ('grp_1', 'prod_a')",
            "INSERT INTO product_group_members VALUES ('grp_1', 'prod_b')",
            "INSERT INTO product_group_members VALUES ('grp_1', '')",
            "INSERT INTO product_group_members VALUES ('grp_1', NULL)",
            "INSERT INTO product_group_members VALUES ('grp_2', 'prod_z')",
        ],
    )

    monkeypatch.setattr(module, "database", db)

    try:
        got = await module.get_product_group_member_product_ids("grp_1")
        # Pre-fix this is set(): the swallowed AttributeError becomes "no members".
        assert got == {"prod_a", "prod_b"}, f"expected both members, got {got!r}"
    finally:
        await db.disconnect()


# --------------------------------------------------------------------------
# 4. repo-wide AST guard -- catches the whole class, not just these two sites
# --------------------------------------------------------------------------
_SKIP_DIRS = {".venv", ".claude", ".git", "node_modules", "__pycache__", "venv", "build", "dist"}
_DB_METHODS = {"execute", "execute_many", "fetch_all", "fetch_one", "fetch_val", "iterate"}
_PARAM_KWARGS = {"values", "parameters"}


def _receiver_name(func: ast.Attribute) -> str | None:
    """Trailing name of the call receiver: `database`, `db.database` -> 'database'."""
    value = func.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _is_text_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "text":
        return True
    return isinstance(func, ast.Attribute) and func.attr == "text"


def _iter_repo_py_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _find_textclause_param_calls(tree: ast.AST) -> List[Tuple[int, str]]:
    """
    Flag `<...>database.<method>(text(...), <params>)`.

    Deliberately scoped to receivers named `database` (covers `database`,
    `db.database`, `self.database`). A *sync* `sqlalchemy.Connection.execute`
    -- typically bound to `conn`/`connection` in this repo -- handles
    `text(...) + params` correctly and must not be flagged.
    """
    out: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _DB_METHODS:
            continue
        if _receiver_name(func) != "database":
            continue
        if not node.args or not _is_text_call(node.args[0]):
            continue
        has_params = len(node.args) >= 2 or any(kw.arg in _PARAM_KWARGS for kw in node.keywords)
        if has_params:
            out.append((node.lineno, func.attr))
    return out


def test_no_textclause_passed_with_params_to_databases_calls() -> None:
    offenders: List[str] = []
    for path in _iter_repo_py_files():
        if path.name == Path(__file__).name:
            continue  # this file demonstrates the anti-pattern on purpose
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for lineno, method in _find_textclause_param_calls(tree):
            rel = path.relative_to(REPO_ROOT)
            offenders.append(f"{rel}:{lineno}  database.{method}(text(...), params)")

    assert not offenders, (
        "databases.Database rejects a text() ClauseElement when params are also "
        "passed (TextClause has no .values()); this raises AttributeError at "
        "runtime. Drop the text() wrapper and pass the SQL as a plain string "
        "with the same :name placeholders:\n  " + "\n  ".join(sorted(offenders))
    )


def test_ast_guard_actually_detects_the_anti_pattern() -> None:
    """The guard must fail on bad code and pass on the fixed form."""
    bad = "database.fetch_val(text('SELECT 1 WHERE x = :x'), {'x': 1})"
    assert _find_textclause_param_calls(ast.parse(bad)) == [(1, "fetch_val")]

    bad_kw = "db.database.fetch_all(text('SELECT :x'), values={'x': 1})"
    assert _find_textclause_param_calls(ast.parse(bad_kw)) == [(1, "fetch_all")]

    # Fixed form: plain string + params.
    good = "database.fetch_val('SELECT 1 WHERE x = :x', {'x': 1})"
    assert _find_textclause_param_calls(ast.parse(good)) == []

    # text() with no params is fine -- _build_query returns it untouched.
    no_params = "database.execute(text('ALTER TABLE t ADD COLUMN c INT'))"
    assert _find_textclause_param_calls(ast.parse(no_params)) == []

    # Sync SQLAlchemy Connection handles text() + params correctly.
    sync_conn = "conn.execute(text('SELECT :x'), {'x': 1})"
    assert _find_textclause_param_calls(ast.parse(sync_conn)) == []
