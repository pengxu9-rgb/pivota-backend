"""No production module may call `database.connect()` under a bare `wait_for`.

WHY A SOURCE GUARD AND NOT ONLY BEHAVIOUR TESTS. The leak this repo fixed lives
in the IDIOM, not in one function: `databases.PostgresBackend.connect()` assigns
`self._pool` only after its await, so `asyncio.wait_for(database.connect(), ...)`
cancels mid-fill and strands every backend `create_pool` had already opened,
with nothing holding a reference to close them. The idiom had SEVEN copies when
it was fixed, across two modules and two scripts.

Behaviour tests cover the sites they drive; there is a real Postgres test for
`ensure_database_ready`, for `accounts_orders_api._ensure_database_connected`
and for `run_database_reconnect_supervisor`. But the two `main.py` startup
connects and the two scripts are not reachable from any test that could count
server backends, and reverting ANY of the five non-`ensure_database_ready` sites
was measured to leave both the full dialect gate and the full sweep green.

So this file is the cheap half of the pair: it cannot tell you the cleanup
works — that is what the Postgres tests are for — but it does tell you the
moment a call site stops using it. It parses the AST rather than grepping,
because the supervisor's call is split across lines and a grep for the
one-line form missed it during development.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Everything that imports the shared `database` object and connects it.
SEARCH_DIRS = ("routes", "services", "utils", "db", "jobs", "scripts", "core", "adapters")
EXTRA_FILES = ("main.py",)


def _python_files():
    for name in EXTRA_FILES:
        path = REPO_ROOT / name
        if path.is_file():
            yield path
    for directory in SEARCH_DIRS:
        root = REPO_ROOT / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def _is_database_connect(node: ast.AST) -> bool:
    """`<anything>.connect()` where the receiver is named `database`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "database"
    )


def _raw_connect_sites(tree: ast.AST):
    """`asyncio.wait_for(database.connect(), ...)`, however it is formatted."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "wait_for" or not node.args:
            continue
        if _is_database_connect(node.args[0]):
            yield node.lineno


def test_no_module_wraps_database_connect_in_a_bare_wait_for() -> None:
    offenders = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not ours to police
            continue
        for lineno in _raw_connect_sites(tree):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")

    assert not offenders, (
        "these call sites bypass utils.database_readiness.connect_database_with_timeout "
        "and strand every server backend the pool fill had opened when they time out:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse `await connect_database_with_timeout(<timeout>, db=database)`."
    )


def test_the_guard_can_actually_see_the_idiom() -> None:
    """Guard the guard: an AST matcher that matches nothing passes vacuously."""
    both_formattings = ast.parse(
        "import asyncio\n"
        "async def one():\n"
        "    await asyncio.wait_for(database.connect(), timeout=3)\n"
        "async def two():\n"
        "    await asyncio.wait_for(\n"
        "        database.connect(), timeout=connect_timeout_seconds\n"
        "    )\n"
    )
    assert sorted(_raw_connect_sites(both_formattings)) == [3, 5]

    # ...and does not fire on the shape the repo now uses.
    fixed = ast.parse(
        "async def ok():\n"
        "    await connect_database_with_timeout(3, db=database)\n"
        "    await asyncio.wait_for(other.connect(), timeout=3)\n"
    )
    assert list(_raw_connect_sites(fixed)) == []
