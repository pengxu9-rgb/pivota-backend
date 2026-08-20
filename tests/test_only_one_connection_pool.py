"""The process must own exactly ONE asyncpg pool.

WHY THIS IS A TEST AND NOT A COMMENT. `db.database.get_db_pool()` existed for
years as a "backward-compatible helper for routes that still expect an asyncpg
pool". It lazily built a SECOND `asyncpg.create_pool(DATABASE_URL)` and, by the
end, had exactly one caller. It carried two hazards that nothing in the codebase
was accounting for:

  * `asyncpg.create_pool` defaults to `min_size=max_size=10`, and that pool
    passed neither — so it opened TEN server connections eagerly, entirely
    outside the `DB_POOL_MAX_SIZE` budget. Every capacity calculation in this
    repo was quietly wrong by ten.
  * its `pool.acquire()` took no deadline, so the unbounded wait that #1781
    removed from the primary pool was still live on this one. The 2026-08-20
    wedge is what an unbounded wait looks like: silent hangs, no errors.

A second pool is easy to add and invisible once added — it needs no migration,
no config, and nothing fails. So the invariant is pinned here rather than left
to review.

WHAT IS ALLOWED. Exactly one site: `utils.database_readiness._new_pool`, which
builds the pool object for the SHARED `database` and must call `create_pool`
directly — PR #1685 fixed a leak by owning that object rather than letting
`databases` construct it, so a timed-out connect can terminate what it opened.

If you need different pool behaviour, change `database_kwargs` in
`db/database.py`. Do not create a private pool.
"""

from __future__ import annotations

import ast
import pathlib
from typing import List, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Every production tree, kept in step with the dialect gate's own sweep so a
# pool cannot hide in `scripts/` — this repo runs ops scripts against the
# production database, and `test_scan_covers_every_production_tree` fails if
# that gate widens and this one does not.
SCANNED_TREES = (
    "db", "routes", "services", "jobs", "utils", "core", "middleware",
    "scripts", "adapters", "orchestrator",
)
SCANNED_FILES = ("main.py",)

# (module path, enclosing function) allowed to call asyncpg.create_pool.
ALLOWED = {("utils/database_readiness.py", "_new_pool")}

# A second `databases.Database(...)` is the OTHER way to grow a pool: it builds
# its own asyncpg pool outside DB_POOL_MAX_SIZE exactly as get_db_pool did. It
# does inherit the bounded checkout (that patch is installed on the asyncpg
# class, not on one pool), so it is the lesser hazard — but it still defeats the
# budget, so it is allowlisted by name rather than ignored.
ALLOWED_DATABASE_CONSTRUCTION = {
    # The one shared instance every route and job uses.
    ("db/database.py", "<module>"),
    # An offline restore tool that connects to a DIFFERENT database (the archive
    # DSN passed on the command line), not a second pool onto the primary.
    ("scripts/recover_seed_data_from_archive.py", "_drive"),
}


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    best, best_line = "<module>", -1
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end and node.lineno > best_line:
                best, best_line = node.name, node.lineno
    return best


def _pool_sites_in_source(rel: str, source: str, suffix: str) -> List[Tuple[str, str, int]]:
    """Real CALLS whose callee ends in `suffix` — not the word in a comment."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: List[Tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(suffix):
            found.append((rel, _enclosing_function(tree, node.lineno), node.lineno))
    return found


def _scanned_sources() -> List[Tuple[str, str]]:
    paths: List[pathlib.Path] = []
    for tree_name in SCANNED_TREES:
        root = REPO_ROOT / tree_name
        if root.is_dir():
            paths.extend(sorted(root.rglob("*.py")))
    for file_name in SCANNED_FILES:
        path = REPO_ROOT / file_name
        if path.is_file():
            paths.append(path)

    sources: List[Tuple[str, str]] = []
    for path in paths:
        if "__pycache__" in path.parts:
            continue
        try:
            sources.append((path.relative_to(REPO_ROOT).as_posix(), path.read_text()))
        except (OSError, UnicodeDecodeError):
            continue
    return sources


def _create_pool_sites() -> List[Tuple[str, str, int]]:
    found: List[Tuple[str, str, int]] = []
    for rel, source in _scanned_sources():
        found.extend(_pool_sites_in_source(rel, source, "create_pool"))
    return found


def _database_construction_sites() -> List[Tuple[str, str, int]]:
    found: List[Tuple[str, str, int]] = []
    for rel, source in _scanned_sources():
        found.extend(_pool_sites_in_source(rel, source, "Database"))
    return found


def test_exactly_one_site_creates_a_connection_pool() -> None:
    sites = _create_pool_sites()
    unexpected = [s for s in sites if (s[0], s[1]) not in ALLOWED]
    assert not unexpected, (
        "a second connection pool is being created.\n\n"
        "A private pool sits outside DB_POOL_MAX_SIZE (so capacity planning is "
        "wrong) and outside the bounded checkout installed in db/database.py "
        "(so it can hang forever — see the 2026-08-20 wedge). Put what you need "
        "in `database_kwargs` instead.\n\n"
        + "\n".join(f"  {p}:{ln} in {fn}()" for p, fn, ln in unexpected)
    )


def test_no_second_databases_instance() -> None:
    """The other route to a private pool: a second `Database(...)`."""
    unexpected = [
        s for s in _database_construction_sites()
        if (s[0], s[1]) not in ALLOWED_DATABASE_CONSTRUCTION
    ]
    assert not unexpected, (
        "a second `databases.Database(...)` is being constructed. It opens its "
        "own asyncpg pool outside DB_POOL_MAX_SIZE. Import the shared "
        "`db.database.database` instead.\n\n"
        + "\n".join(f"  {p}:{ln} in {fn}()" for p, fn, ln in unexpected)
    )


def test_scan_covers_every_production_tree() -> None:
    """Narrowing the scan is how this guard would be silenced without a diff.

    The dialect gate sweeps the same population for a different reason; borrow
    its list so one file stays the definition of "production Python".
    """
    from tests.test_repo_sql_prepare_postgres import _SWEPT_DIRS, _SWEPT_FILES

    missing_dirs = sorted(set(_SWEPT_DIRS) - set(SCANNED_TREES))
    missing_files = sorted(set(_SWEPT_FILES) - set(SCANNED_FILES))
    assert not missing_dirs and not missing_files, (
        "this guard scans less than the dialect gate does, so a pool created in "
        f"{missing_dirs + missing_files} would be invisible here"
    )


def test_the_allowed_site_still_exists() -> None:
    """A stale allowlist hides the thing it was written to permit."""
    sites = {(p, fn) for p, fn, _ in _create_pool_sites()}
    missing = sorted(ALLOWED - sites)
    assert not missing, (
        "ALLOWED names a create_pool site that no longer exists — delete the "
        f"entry so this test keeps meaning something: {missing}"
    )


def test_the_detector_sees_a_real_call() -> None:
    """Guards the failure this repo keeps meeting: a gate that asserts nothing.

    The word `create_pool` appears in many COMMENTS here (utils/database_readiness
    explains the leak at length). A grep-based check would flag those and, once
    someone silenced it, would flag nothing at all. This drives the REAL
    collector so that narrowing it cannot leave this test green.
    """
    source = (
        "async def f():\n"
        "    # asyncpg.create_pool is mentioned here in a comment\n"
        "    p = await asyncpg.create_pool(URL)\n"
        "\n"
        "def g():\n"
        "    return Database(URL)\n"
    )
    assert _pool_sites_in_source("x.py", source, "create_pool") == [("x.py", "f", 3)]
    assert _pool_sites_in_source("x.py", source, "Database") == [("x.py", "g", 6)]


def test_get_db_pool_is_not_reintroduced() -> None:
    """The specific helper that grew the second pool."""
    dbmod = (REPO_ROOT / "db" / "database.py").read_text()
    tree = ast.parse(dbmod)
    names = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "get_db_pool" not in names, (
        "get_db_pool is back. It was removed on 2026-08-20 because it built a "
        "second, unbounded, unbudgeted pool for a single caller."
    )
