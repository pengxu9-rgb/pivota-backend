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

import re
from pathlib import Path

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


def test_catalog_products_writers_are_exactly_the_five_chokepoints():
    violations = {}
    seen_writers = set()
    for path in _python_sources():
        hits = _writer_hits(path)
        if not hits:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        seen_writers.add(rel)
        if rel not in ALLOWED_WRITER_FILES:
            violations[rel] = hits
    assert not violations, (
        "ADR-011 R2 violation: catalog_products writer(s) outside the five "
        f"chokepoints: {violations}. Route the write through one of the five "
        "intake doors (they run resolve_or_attach_content_identity pre-insert) "
        "instead of inserting directly."
    )
    # Drift guard in the other direction: if a chokepoint stops writing (door
    # refactor/move), the allowlist must shrink in the same reviewed change —
    # a stale allowlist would quietly re-open the door somewhere else.
    missing = ALLOWED_WRITER_FILES - seen_writers
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
