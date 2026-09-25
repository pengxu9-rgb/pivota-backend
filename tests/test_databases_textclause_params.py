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
import re
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


# --------------------------------------------------------------------------
# 5. the sibling crash class: a params dict with a key the SQL never names
#
# Same `_build_query` line, other half: for a str query it calls
# `text(query).bindparams(**values)`, and TextClause.bindparams() raises
# ArgumentError for any name the text does not define. A dict carrying one
# extra key is a guaranteed exception, on every backend, before the driver.
#
# Prod 2026-09-25 (web-01152-yan, 21x): db/agent_identity_issuers.upsert_issuer
# re-used the INSERT's params ({agent_id, issuer, ...}) for the UPDATE branch,
# so every re-registration of an existing issuer 500'd.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_databases_rejects_a_param_the_sql_does_not_reference(tmp_path) -> None:
    from sqlalchemy.exc import ArgumentError

    db = await _sqlite_database(
        tmp_path,
        ddl=["CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"],
        seed=["INSERT INTO t (id, name) VALUES (1, 'a')"],
    )
    try:
        with pytest.raises(ArgumentError, match="bound parameter named 'agent_id'"):
            await db.execute("UPDATE t SET name = :name WHERE id = :id", {"name": "b", "id": 1, "agent_id": "x"})
        await db.execute("UPDATE t SET name = :name WHERE id = :id", {"name": "b", "id": 1})
        assert await db.fetch_val("SELECT name FROM t WHERE id = :id", {"id": 1}) == "b"
    finally:
        await db.disconnect()


class _BuildQueryRecordingDatabase:
    """Runs every call through databases' REAL query builder, then answers from a script.

    The upsert's SQL is Postgres-only (TEXT[], NOW()), so SQLite cannot execute it; but the
    defect lives entirely in `Connection._build_query`, which this does run, unmodified.
    """

    def __init__(self, fetch_one_results: List[Any]) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self._fetch_one_results = list(fetch_one_results)

    def _build(self, method: str, query: str, values: Dict[str, Any] | None) -> None:
        from databases.core import Connection

        Connection._build_query(query, values)  # raises exactly as prod did
        self.calls.append((method, " ".join(query.split()), dict(values or {})))

    async def execute(self, query: str, values: Dict[str, Any] | None = None) -> None:
        self._build("execute", query, values)

    async def fetch_one(self, query: str, values: Dict[str, Any] | None = None) -> Any:
        self._build("fetch_one", query, values)
        return self._fetch_one_results.pop(0)


@pytest.mark.asyncio
async def test_upsert_issuer_update_branch_binds_only_what_the_update_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.agent_identity_issuers as store

    stored = {
        "id": 7, "agent_id": "agent_a", "issuer": "https://idp.example", "jwks_uri": "https://idp.example/v2",
        "audience": "aud", "algs": ["ES256"], "authorized_party": None, "required_scopes": None,
        "status": "active", "last_jwks_ok_at": None, "created_at": None, "updated_at": None,
    }
    fake = _BuildQueryRecordingDatabase(fetch_one_results=[{"id": 7}, stored])
    monkeypatch.setattr(store, "database", fake)
    monkeypatch.setattr(store, "_DDL_READY", True)

    reg = store.IssuerRegistration(
        issuer="https://idp.example", jwks_uri="https://idp.example/v2", audience="aud",
        algs=["ES256"], authorized_party=None, required_scopes=None,
    )
    # Pre-fix: ArgumentError "This text() construct doesn't define a bound parameter named 'agent_id'".
    row = await store.upsert_issuer("agent_a", reg, jwks_ok=True)

    (update,) = [c for c in fake.calls if c[0] == "execute"]
    assert update[1].startswith("UPDATE agent_identity_issuers")
    assert update[2] == {
        "jwks_uri": "https://idp.example/v2", "audience": "aud", "algs": ["ES256"],
        "authorized_party": None, "required_scopes": None, "jwks_ok": True, "id": 7,
    }
    assert row["id"] == 7


# Every trailing receiver name that holds a `databases.Database` in this repo:
# `database`, `db.database`, `self.db`, `read_db`, `write_db`, `seed_db`, ...
def _is_databases_receiver(name: str | None) -> bool:
    return bool(name) and (name == "database" or name.endswith("db"))


# SQLAlchemy's own TextClause bind regex (sqlalchemy/sql/elements.py), verbatim —
# a guard must parse placeholders exactly the way the library does.
_BIND_PARAM_RE = re.compile(r"(?<![:\w\x5c]):(\w+)(?!:)", re.UNICODE)


def _const_sql(node: ast.AST, env: Dict[str, ast.AST]) -> str | None:
    """A str literal, "a" + "b" concatenation, or a name bound once to one. f-strings are NOT
    resolved: their placeholders often live inside the interpolated helpers."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _const_sql(node.left, env), _const_sql(node.right, env)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.Name) and node.id in env:
        return _const_sql(env[node.id], env)
    return None


def _static_dict_keys(node: ast.AST, env: Dict[str, ast.AST], depth: int = 0) -> Set[str] | None:
    """The complete key set of a dict literal / dict(k=...) / `{**name, ...}` with every spread
    resolvable; None when any key is not statically known."""
    if depth > 5:
        return None
    if isinstance(node, ast.Name) and node.id in env:
        return _static_dict_keys(env[node.id], env, depth + 1)
    if isinstance(node, ast.Dict):
        keys: Set[str] = set()
        for key, value in zip(node.keys, node.values):
            if key is None:
                spread = _static_dict_keys(value, env, depth + 1)
                if spread is None:
                    return None
                keys |= spread
            elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
            else:
                return None
        return keys
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
        and not node.args
        and all(kw.arg is not None for kw in node.keywords)
    ):
        return {kw.arg for kw in node.keywords}
    return None


def _single_assignments(nodes: List[ast.AST]) -> Dict[str, ast.AST]:
    """name -> value for names assigned exactly once (and never augmented/unpacked/mutated
    by key) anywhere under `nodes`. Anything re-bound is dropped: its value is not static."""
    env: Dict[str, ast.AST] = {}
    unstable: Set[str] = set()
    for root in nodes:
        for sub in ast.walk(root):
            if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                name = sub.targets[0].id
                if name in env:
                    unstable.add(name)
                env[name] = sub.value
            elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name) and sub.value is not None:
                if sub.target.id in env:
                    unstable.add(sub.target.id)
                env[sub.target.id] = sub.value
            elif isinstance(sub, (ast.Assign, ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr, ast.With, ast.AsyncWith)):
                if isinstance(sub, ast.Assign):
                    targets = sub.targets
                elif isinstance(sub, (ast.With, ast.AsyncWith)):
                    targets = [item.optional_vars for item in sub.items if item.optional_vars is not None]
                else:
                    targets = [sub.target]
                for target in targets:
                    for leaf in ast.walk(target):
                        if isinstance(leaf, ast.Name):
                            unstable.add(leaf.id)
            elif isinstance(sub, ast.Subscript) and isinstance(sub.ctx, ast.Store) and isinstance(sub.value, ast.Name):
                unstable.add(sub.value.id)  # params["k"] = ... adds a key
            elif (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.attr in {"update", "setdefault", "pop"}
            ):
                unstable.add(sub.func.value.id)
    return {k: v for k, v in env.items() if k not in unstable}


def _find_unreferenced_param_calls(tree: ast.AST) -> List[Tuple[int, str, List[str]]]:
    """Flag `<...>database.<method>(SQL, PARAMS)` where both are statically known and PARAMS
    carries a key SQL never references. Unresolvable SQL or keys are skipped, never guessed."""
    out: List[Tuple[int, str, List[str]]] = []
    module_env = _single_assignments([n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))])
    seen: Set[int] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        env = {**module_env, **_single_assignments(fn.body)}
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call) or id(call) in seen:
                continue
            func = call.func
            if not isinstance(func, ast.Attribute) or func.attr not in _DB_METHODS - {"execute_many"}:
                continue
            if not _is_databases_receiver(_receiver_name(func)):
                continue
            kwargs = {kw.arg: kw.value for kw in call.keywords}
            query = call.args[0] if call.args else kwargs.get("query")
            values = call.args[1] if len(call.args) > 1 else kwargs.get("values")
            if query is None or values is None:
                continue
            seen.add(id(call))
            sql = _const_sql(query, env)
            keys = _static_dict_keys(values, env)
            if sql is None or keys is None:
                continue
            unreferenced = keys - set(_BIND_PARAM_RE.findall(sql))
            if unreferenced:
                out.append((call.lineno, func.attr, sorted(unreferenced)))
    return out


def test_no_databases_call_passes_a_param_its_sql_does_not_reference() -> None:
    offenders: List[str] = []
    for path in _iter_repo_py_files():
        if path.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for lineno, method, unreferenced in _find_unreferenced_param_calls(tree):
            rel = path.relative_to(REPO_ROOT)
            offenders.append(f"{rel}:{lineno}  database.{method}(...) passes {unreferenced}")

    assert not offenders, (
        "databases.Database binds a str query via text(query).bindparams(**values), which "
        "raises ArgumentError for any key the SQL does not reference. Pass only the names "
        "the statement uses:\n  " + "\n  ".join(sorted(offenders))
    )


def test_unreferenced_param_guard_detects_the_upsert_issuer_shape() -> None:
    # The exact pre-fix shape of db/agent_identity_issuers.upsert_issuer's UPDATE branch.
    bad = '''
async def upsert_issuer(agent_id, reg, existing):
    params = {"agent_id": agent_id, "issuer": reg.issuer, "jwks_uri": reg.jwks_uri}
    if existing:
        await database.execute(
            """
            UPDATE agent_identity_issuers SET jwks_uri = :jwks_uri WHERE id = :id
            """,
            {**params, "id": dict(existing)["id"]},
        )
'''
    assert _find_unreferenced_param_calls(ast.parse(bad)) == [(5, "execute", ["agent_id", "issuer"])]

    # Receivers other than `database` that hold one; a module-level SQL constant; a CAST.
    other = '''
_Q = "SELECT a FROM t " + "WHERE b = CAST(:b AS text)"
async def f(self):
    await self.db.fetch_one(_Q, dict(b=1, c=2))
    await read_db.fetch_all(_Q, {"b": 1})
'''
    assert _find_unreferenced_param_calls(ast.parse(other)) == [(4, "fetch_one", ["c"])]

    # Not statically knowable -> skipped, never guessed.
    unknown = '''
async def f(where, extra):
    params = {"a": 1}
    params["b"] = 2
    await database.fetch_all(f"SELECT 1 WHERE {where}", {"x": 1})
    await database.fetch_all("SELECT :a, :b", params)
    await database.fetch_all("SELECT :a", {**extra, "a": 1})
'''
    assert _find_unreferenced_param_calls(ast.parse(unknown)) == []

    # A sync SQLAlchemy connection ignores extra params: not this class.
    assert _find_unreferenced_param_calls(ast.parse(
        'def f(conn):\n    conn.execute(text("SELECT :a"), {"a": 1, "b": 2})\n'
    )) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("has_approved_by", [False, True])
async def test_approve_payout_binds_approved_by_only_when_the_column_exists(
    monkeypatch: pytest.MonkeyPatch, has_approved_by: bool
) -> None:
    # Same class, schema-dependent: the UPDATE names :approved_by only when agent_payouts has the
    # column (no migration creates it), but the dict always carried it. The AST guard cannot see
    # this one (f-string SQL), so it is pinned here.
    import services.partner_settlement_service as module

    fake = _BuildQueryRecordingDatabase(fetch_one_results=[{"id": 42}])
    executed: List[int] = []

    async def _columns(_table: str) -> Set[str]:
        return {"status", "approved_by"} if has_approved_by else {"status"}

    async def _execute_payout(payout_id: int) -> None:
        executed.append(payout_id)

    monkeypatch.setattr(module, "database", fake)
    monkeypatch.setattr(module, "_table_columns", _columns)
    monkeypatch.setattr(module, "execute_payout", _execute_payout)

    await module.approve_payout(42, "ops@pivota.cc")

    (update,) = fake.calls
    expected = {"payout_id": 42, "approved_by": "ops@pivota.cc"} if has_approved_by else {"payout_id": 42}
    assert update[2] == expected
    assert executed == [42]
