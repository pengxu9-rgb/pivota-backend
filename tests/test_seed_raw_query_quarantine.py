"""Every RAW `external_product_seeds` query must exclude quarantined seeds.

The shared seam (`fetch_external_seed_rows`) covers most consumers, but six raw
queries read the table directly and had to be gated by hand:

    routes/agent_shop_gateway.py   offers.resolve, x3  (UNAUTHENTICATED)
    routes/agent_api.py            direct-id product lookup + TEXT fallback
    routes/accounts_orders_api.py  browse-history price lookup

Review round 2 found all six had **zero** executing coverage: deleting
`{_seed_quarantine_clause()}` from any one of them left 268 tests green. That is
the PR's own thesis turned on itself — it argued per-consumer gates rot
undetected, then shipped six with no rot detector.

METHOD. These tests do not re-type the SQL; a hand-copied query proves nothing
about the one that ships. They locate each f-string SQL literal in the real
module by AST, render it with the module's own `_seed_quarantine_clause`, and
EXECUTE it against SQLite. Delete the clause from any site and the rendered
string changes, so the corresponding test fails.

The predicate is portable by design, which is the only reason this works — see
services/source_quarantine.sql_bare_domain.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sqlite3

import pytest

from services.external_seed_search import build_seed_quarantine_anti_join

REPO = pathlib.Path(__file__).resolve().parents[1]

# (module, count) — the count is deliberate: a NEW raw seed query added without
# a gate changes this number and fails the completeness test below, rather than
# quietly joining the ungated set.
RAW_SEED_QUERY_SITES = {
    "routes/agent_shop_gateway.py": 3,
    "routes/agent_api.py": 2,
    "routes/accounts_orders_api.py": 1,
}


def _render_seed_queries(relpath: str) -> list[tuple[int, str]]:
    """Every f-string SQL literal in `relpath` that reads external_product_seeds.

    Embedded expressions other than the quarantine clause are stubbed with a
    harmless always-true fragment — we are testing the quarantine predicate, not
    the route's own filters.
    """
    tree = ast.parse((REPO / relpath).read_text())
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        literal = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
        if "external_product_seeds" not in literal or "FROM" not in literal:
            continue
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(v.value)
            else:
                src = ast.unparse(v.value)
                if "_seed_quarantine_clause" in src:
                    parts.append(build_seed_quarantine_anti_join())
                else:
                    # The route's own predicates (`' OR '.join(title_clauses)`,
                    # `brand_clause`, …). Neutralised to always-true rather than
                    # empty — several are interpolated as `AND {expr}`, so an
                    # empty stub leaves a dangling AND and the query fails to
                    # parse, which would look like a gate defect.
                    parts.append("1=1")
        out.append((node.lineno, "".join(parts)))
    return out


def _sqlite_with(seed_domains, quarantines=()):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE external_product_seeds ("
        " id TEXT, external_product_id TEXT, attached_product_key TEXT,"
        " attached_variant_id TEXT, price_amount REAL, price_currency TEXT,"
        " seed_data TEXT, domain TEXT, status TEXT, market TEXT, tool TEXT,"
        " utm_template TEXT, partner_type TEXT, disclosure_text TEXT,"
        " destination_url TEXT, canonical_url TEXT, title TEXT, image_url TEXT,"
        " availability TEXT, notes TEXT, created_by_employee_id TEXT,"
        " seller_ref TEXT, seed_kind TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE catalog_source_quarantine (quarantine_id INTEGER, match_type TEXT,"
        " match_value TEXT, state TEXT, expires_at TEXT)"
    )
    # Two rows per domain: one ATTACHED, one not. These six queries disagree —
    # offers.resolve requires `attached_product_key IS NOT NULL`, the direct-id
    # lookup requires `IS NULL` — so a single shape would make half the
    # assertions vacuous. `market`/`tool` are '*' so the routes' own market
    # filters always match whatever value the bind is rendered to.
    for sid, domain in seed_domains:
        conn.execute(
            "INSERT INTO external_product_seeds (id, external_product_id, domain, status,"
            " attached_product_key, attached_variant_id, market, tool,"
            " price_amount, price_currency, seed_data, title, created_at, updated_at)"
            " VALUES (?,?,?,'active',?,?,'*','*',10.0,'USD','{}','serum','2026-01-01','2026-01-01')",
            (sid, sid, domain, sid, sid),
        )
        conn.execute(
            "INSERT INTO external_product_seeds (id, external_product_id, domain, status,"
            " attached_product_key, attached_variant_id, market, tool,"
            " price_amount, price_currency, seed_data, title, created_at, updated_at)"
            " VALUES (?,?,?,'active',NULL,NULL,'*','*',10.0,'USD','{}','serum','2026-01-01','2026-01-01')",
            (sid + "u", sid + "u", domain),
        )
    for q in quarantines:
        conn.execute("INSERT INTO catalog_source_quarantine VALUES (?,?,?,?,?)", q)
    return conn


def _executable(sql: str, ident: str) -> str:
    """Render the route's SQL runnable on SQLite, bound to one seed id.

    Only the engine-specific and parameter bits are touched; the quarantine
    fragment is never rewritten. `ident` is what the route is "looking up", so
    each query can be run once for the quarantined seed and once for the clean
    one — which is how both directions get asserted on the SAME query.
    """
    sql = re.sub(r"=\s*ANY\(:\w+\)", f"= '{ident}'", sql)
    sql = re.sub(r"seed_data\s*#>>\s*'\{[^}]*\}'", "''", sql)
    sql = re.sub(r"seed_data\s*(->>?\s*'[^']*'\s*)+", "''", sql)
    # Numeric params BEFORE the generic substitution — `LIMIT 's1'` raises
    # IntegrityError on SQLite, which would read as a gate failure.
    sql = re.sub(r":(limit|offset|max_rows)\b", "100", sql)
    # LIKE/prefix params must not become a literal id or nothing matches.
    sql = re.sub(r":(\w*(prefix|like|pattern)\w*)\b", "'%'", sql)
    sql = re.sub(r":\w+", f"'{ident}'", sql)
    sql = re.sub(r"\bESCAPE\s+'[^']*'", "", sql)
    return sql


ALL_SITES = [
    (mod, lineno, sql)
    for mod in RAW_SEED_QUERY_SITES
    for lineno, sql in _render_seed_queries(mod)
]


def test_every_known_raw_seed_query_was_found():
    """Guards the fixture itself, and catches a NEW ungated raw query.

    If someone adds a seventh raw read, this count changes and they are forced
    to decide — gate it, or move it to the documented ungated list — rather than
    silently joining the ungated set.
    """
    found = {mod: len(_render_seed_queries(mod)) for mod in RAW_SEED_QUERY_SITES}
    assert found == RAW_SEED_QUERY_SITES, (
        f"raw seed-query sites changed: {found} != {RAW_SEED_QUERY_SITES}. "
        "A new raw read of external_product_seeds must be gated or explicitly "
        "listed as out of scope."
    )


@pytest.mark.parametrize("mod,lineno,sql", ALL_SITES, ids=lambda v: str(v)[:40])
def test_raw_query_carries_the_clause(mod, lineno, sql):
    assert "catalog_source_quarantine" in sql, (
        f"{mod}:{lineno} reads external_product_seeds with no quarantine gate"
    )


def _ids_for(conn, sql, idents):
    """Union of rows returned when the query is bound to each candidate id.

    A union across the attached/unattached pair, because these six queries
    disagree about which shape they accept and we want the row to be reachable
    by whichever one this query is looking for.
    """
    found = set()
    for ident in idents:
        found |= {r[0] for r in conn.execute(_executable(sql, ident)).fetchall()}
    return found


MINTREE = ("s1", "s1u")
CLEAN = ("s2", "s2u")
SEEDS = [("s1", "mintree.us"), ("s2", "beplain.com")]


@pytest.mark.parametrize("mod,lineno,sql", ALL_SITES, ids=lambda v: str(v)[:40])
def test_raw_query_EXECUTES_and_excludes_the_quarantined_seed(mod, lineno, sql):
    """The assertion that actually kills the mutants.

    Runs the real rendered SQL for the quarantined seed (must be absent) and for
    the clean seed (must be present). The second half is what stops a query that
    returns nothing from passing vacuously — which is exactly how a broken
    fixture reads as a working gate.
    """
    conn = _sqlite_with(SEEDS, [(1, "domain", "mintree.us", "active", None)])

    assert not (_ids_for(conn, sql, MINTREE) & set(MINTREE)), (
        f"{mod}:{lineno} served a QUARANTINED seed"
    )
    assert _ids_for(conn, sql, CLEAN) & set(CLEAN), (
        f"{mod}:{lineno} returned nothing for a clean seed — the exclusion above "
        "would have passed vacuously"
    )


@pytest.mark.parametrize("mod,lineno,sql", ALL_SITES, ids=lambda v: str(v)[:40])
def test_raw_query_answers_the_other_way_when_the_quarantine_is_revoked(mod, lineno, sql):
    """A gate that always drops the row is not a gate. Both directions."""
    conn = _sqlite_with(SEEDS, [(1, "domain", "mintree.us", "revoked", None)])
    assert _ids_for(conn, sql, MINTREE) & set(MINTREE), (
        f"{mod}:{lineno} dropped a seed whose quarantine was revoked"
    )
