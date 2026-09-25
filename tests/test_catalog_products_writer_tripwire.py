"""ADR-011 R2 tripwire — catalog_products has exactly FIVE writers (the doors).

Any new writer of catalog_products outside the five audited chokepoints is a
review-blocking violation (ADR-011 R2: new doors MUST route through the five
chokepoints — or the shared resolve-or-attach primitive — never insert
directly).

The scan is WHITESPACE-NORMALIZED across lines and covers ALL FOUR live insert
idioms (the ADR's audit showed a literal-SQL grep catches only 2 of the 5
doors):

  1. raw SQL          : INSERT INTO catalog_products ...
  2. pg dialect helper: _pg_insert(catalog_products)...
  3. sqlalchemy core  : insert(catalog_products)... / catalog_products.insert(...
  4. sync-door helper : _upsert_by_pk(catalog_products, ...) (call spans lines)

If this test fails because you added a legitimate sixth door: don't. Route the
write through one of the five chokepoints, or — if a new door is genuinely
unavoidable — it must call services.intake_identity.resolve_or_attach_content_
identity pre-insert AND be added to the allowlist in the same reviewed change.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The five audited chokepoints (ADR-011's five-door audit, file:line-verified),
# repo-relative. File-level allowlisting: each file's writes funnel through the
# single reviewed door function.
ALLOWED_WRITER_FILES = {
    "services/catalog_sync_service.py",             # door 1: connected sync
    "scripts/mirror_external_seeds_to_catalog_products.py",  # door 2: seed mirror
    "services/brand_authored_intake.py",            # door 3: store-less brand
    "services/catalog_enrichment_agent/apply.py",   # door 4: retailer crawl/feed
    "services/audit_index_intake.py",               # door 5: audit / URL-wedge
}

# NOT DOORS. Operator scripts that seed a LOCAL fixture database and can never write the
# production catalog. A comment saying "local only" is not the exemption; each entry is exempt
# only while `_local_fixture_guard_holds` proves BOTH halves:
#   (a) the guard refuses the script's OWN full refusal table (`REFUSED_DATABASE_URLS`, one list
#       owned by the script and also parametrized by its tests) and accepts its accept table;
#   (b) the guard is IN THE WRITE PATH: every function that writes catalog_products calls the
#       write-path check as its first statement, and that check calls the guard.
# Same drift rule as the doors: an entry that stops writing must leave the set.
LOCAL_FIXTURE_WRITERS = {
    # seeds one catalog row for the Reap sandbox e2e run
    "scripts/ops/reap_local_e2e.py": {
        "guard": "check_local_database_url",
        "write_path_check": "_bound_database_url_check",
    },
}
#: A refusal table shorter than this is not a table; a gutted one must not pass vacuously.
MIN_REFUSAL_PROBES = 20

# All four live insert idioms, matched against whitespace-collapsed source so
# multi-line calls (the `_upsert_by_pk(\n    catalog_products, ...` shape that
# a literal grep misses) are caught.
WRITER_PATTERNS = [
    # Case-consistent on purpose: SQL in this repo is `INSERT INTO ...` (or a
    # hypothetical all-lowercase `insert into ...`); prose in docstrings says
    # "INSERT into catalog_products" (mixed case) and must not trip the wire.
    re.compile(r"INSERT\s+INTO\s+catalog_products\b"),
    re.compile(r"insert\s+into\s+catalog_products\b"),
    re.compile(r"_pg_insert\(\s*catalog_products\b"),
    re.compile(r"(?<![\w.])insert\(\s*catalog_products\b"),
    re.compile(r"\bcatalog_products\s*\.\s*insert\s*\("),
    re.compile(r"_upsert_by_pk\(\s*catalog_products\b"),
]

# Directories that can't contain a production writer.
EXCLUDED_DIR_PARTS = {
    "tests", ".git", ".github", "__pycache__", ".claude",
    "venv", ".venv", "node_modules",
}


def _python_sources() -> list[Path]:
    out = []
    for path in REPO_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(part in EXCLUDED_DIR_PARTS for part in rel_parts):
            continue
        out.append(path)
    return out


def _writer_hits(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    collapsed = re.sub(r"\s+", " ", source)
    return [p.pattern for p in WRITER_PATTERNS if p.search(collapsed)]


def _load_script(path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_tripwire_fixture_" + re.sub(r"\W", "_", str(path)), path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _guard_refuses_the_full_table(module, guard_name: str) -> bool:
    guard = getattr(module, guard_name, None)
    refused = tuple(getattr(module, "REFUSED_DATABASE_URLS", ()) or ())
    accepted = tuple(getattr(module, "ACCEPTED_DATABASE_URLS", ()) or ())
    if guard is None or len(refused) < MIN_REFUSAL_PROBES or not accepted:
        return False
    for url, _why in refused:
        try:
            guard(url)
        except Exception:  # noqa: BLE001 — any refusal is a refusal
            continue
        return False
    for url in accepted:
        try:
            guard(url)
        except Exception:  # noqa: BLE001
            return False
    return True


def _calls(node, name: str) -> bool:
    return any(
        isinstance(n, ast.Call)
        and ((isinstance(n.func, ast.Name) and n.func.id == name)
             or (isinstance(n.func, ast.Attribute) and n.func.attr == name))
        for n in ast.walk(node)
    )


def _is_direct_call(node, name: str) -> bool:
    """`node` IS a call to `name` — not an expression that merely contains one."""
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == name)
        or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
    )


def _first_statement_calls(fn, name: str) -> bool:
    """The first statement after the docstring is the bare expression `name(...)`.

    NOT "the first statement contains a call to `name`": `(lambda: name())` and
    `False and name()` both contain one and neither runs it."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]  # the docstring
    return bool(body) and isinstance(body[0], ast.Expr) and _is_direct_call(body[0].value, name)


def _guard_is_in_the_write_path(source: str, guard_name: str, check_name: str) -> bool:
    """Every function whose body writes catalog_products opens with `check_name()`, and
    `check_name` itself calls `guard_name`."""
    tree = ast.parse(source)
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    check = next((f for f in functions if f.name == check_name), None)
    if check is None or not _calls(check, guard_name):
        return False
    writers = [
        f for f in functions
        if any(p.search(re.sub(r"\s+", " ", ast.get_source_segment(source, f) or ""))
               for p in WRITER_PATTERNS)
    ]
    # Innermost writers only: an enclosing function "contains" its nested writer's text.
    innermost = [f for f in writers
                 if not any(g is not f and g in writers for g in ast.walk(f))]
    return bool(innermost) and all(_first_statement_calls(f, check_name) for f in innermost)


def _local_fixture_guard_holds(path: Path, spec: dict) -> bool:
    """Both halves of the exemption (see LOCAL_FIXTURE_WRITERS). Any failure is False."""
    try:
        module = _load_script(path)
        source = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return False
    return (
        _guard_refuses_the_full_table(module, spec["guard"])
        and _guard_is_in_the_write_path(source, spec["guard"], spec["write_path_check"])
    )


def _catalog_writer_scan(guard_holds=_local_fixture_guard_holds):
    """(violations, seen_writers). `guard_holds` is injectable so the exemption's
    CONDITIONALITY is itself testable — see `test_the_fixture_exemption_is_conditional`."""
    violations = {}
    seen_writers = set()
    for path in _python_sources():
        hits = _writer_hits(path)
        if not hits:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        seen_writers.add(rel)
        if rel in LOCAL_FIXTURE_WRITERS and guard_holds(path, LOCAL_FIXTURE_WRITERS[rel]):
            continue
        if rel not in ALLOWED_WRITER_FILES:
            violations[rel] = hits
    return violations, seen_writers


def test_catalog_products_writers_are_exactly_the_five_chokepoints():
    violations, seen_writers = _catalog_writer_scan()
    assert not violations, (
        "ADR-011 R2 violation: catalog_products writer(s) outside the five "
        f"chokepoints: {violations}. Route the write through one of the five "
        "intake doors (they run resolve_or_attach_content_identity pre-insert) "
        "instead of inserting directly."
    )
    # Drift guard in the other direction: if a chokepoint stops writing (door
    # refactor/move), the allowlist must shrink in the same reviewed change —
    # a stale allowlist would quietly re-open the door somewhere else.
    missing = (ALLOWED_WRITER_FILES | set(LOCAL_FIXTURE_WRITERS)) - seen_writers
    assert not missing, (
        f"Allowlisted chokepoint(s) no longer write catalog_products: {missing}. "
        "Update ALLOWED_WRITER_FILES (and ADR-011's door table) in this change."
    )


def test_tripwire_idioms_catch_the_known_door_shapes():
    """The four idioms must each be caught by the scan (self-test so a regex
    edit can't silently blind the tripwire)."""
    shapes = [
        "await db.execute('''INSERT INTO\n  catalog_products (product_key)...''')",
        "stmt = _pg_insert(catalog_products).values(**values)",
        "stmt = insert(\n    catalog_products\n).values(x=1)",
        "stmt = catalog_products.insert().values(x=1)",
        "existing = await _upsert_by_pk(\n    catalog_products,\n    'product_key', {...})",
    ]
    for shape in shapes:
        collapsed = re.sub(r"\s+", " ", shape)
        assert any(p.search(collapsed) for p in WRITER_PATTERNS), shape


# ── the local-fixture exemption is conditional, and its probe bites ──────────────────────────


def test_the_fixture_exemption_is_conditional():
    """With the proof failing, the fixture writer IS a violation. Kills an exemption made
    unconditional (`if rel in LOCAL_FIXTURE_WRITERS: continue`)."""
    violations, _ = _catalog_writer_scan(guard_holds=lambda *_: False)
    assert set(violations) == set(LOCAL_FIXTURE_WRITERS)


def test_the_harness_passes_both_halves_of_the_proof():
    for rel, spec in LOCAL_FIXTURE_WRITERS.items():
        assert _local_fixture_guard_holds(REPO_ROOT / rel, spec), rel


_TABLE = (
    "REFUSED_DATABASE_URLS = [('postgresql://10.0.0.%d/x' % i, 'remote') for i in range(25)]\n"
    "ACCEPTED_DATABASE_URLS = ('sqlite:///x.db',)\n"
)
_WRITER = (
    "def _bound_check():\n    guard(None)\n\n"
    "async def seed(db):\n    {first}\n"
    "    await db.execute('INSERT INTO catalog_products (product_key) VALUES (1)')\n"
)
_STRICT = "def guard(u):\n    if str(u).startswith('sqlite:///'):\n        return u\n    raise ValueError(u)\n"
_PERMISSIVE = "def guard(u):\n    return u\n"
_SPEC = {"guard": "guard", "write_path_check": "_bound_check"}


def _fixture(tmp_path, *parts) -> Path:
    path = tmp_path / "fixture_writer.py"
    path.write_text("".join(parts))
    return path


def test_the_probe_accepts_a_strict_guard_in_the_write_path(tmp_path):
    path = _fixture(tmp_path, _TABLE, _STRICT, _WRITER.format(first="_bound_check()"))
    assert _local_fixture_guard_holds(path, _SPEC)


def test_the_probe_rejects_a_guard_that_accepts_a_remote_host(tmp_path):
    path = _fixture(tmp_path, _TABLE, _PERMISSIVE, _WRITER.format(first="_bound_check()"))
    assert not _local_fixture_guard_holds(path, _SPEC)


def test_the_probe_tries_every_entry_of_the_table_not_a_prefix(tmp_path):
    """A guard that refuses all but the LAST table entry must fail the probe — a probe that
    sampled the table would pass it."""
    leaky = (
        "def guard(u):\n"
        "    if str(u).startswith('sqlite:///') or str(u) == 'postgresql://10.0.0.24/x':\n"
        "        return u\n"
        "    raise ValueError(u)\n"
    )
    path = _fixture(tmp_path, _TABLE, leaky, _WRITER.format(first="_bound_check()"))
    assert not _local_fixture_guard_holds(path, _SPEC)


@pytest.mark.parametrize("first", [
    "pass",
    "(lambda: _bound_check())",        # contains the call, never runs it
    "False and _bound_check()",        # contains the call, short-circuits it away
    "[_bound_check for _ in ()]",      # names it, never calls it
])
def test_the_probe_rejects_a_write_that_does_not_open_with_the_check(tmp_path, first):
    path = _fixture(tmp_path, _TABLE, _STRICT, _WRITER.format(first=first))
    assert not _local_fixture_guard_holds(path, _SPEC)


def test_the_probe_rejects_a_write_path_check_that_does_not_call_the_guard(tmp_path):
    writer = _WRITER.replace("guard(None)", "return None").format(first="_bound_check()")
    path = _fixture(tmp_path, _TABLE, _STRICT, writer)
    assert not _local_fixture_guard_holds(path, _SPEC)


def test_the_probe_rejects_a_gutted_refusal_table(tmp_path):
    short = "REFUSED_DATABASE_URLS = [('postgresql://10.0.0.1/x', 'remote')]\n" \
            "ACCEPTED_DATABASE_URLS = ('sqlite:///x.db',)\n"
    path = _fixture(tmp_path, short, _STRICT, _WRITER.format(first="_bound_check()"))
    assert not _local_fixture_guard_holds(path, _SPEC)
