"""All SEVEN domain comparators must give the same verdict for the same pair.

Seven places compare a row's domain against `catalog_source_quarantine.match_value`:

  1. SQL anti-join            services/source_quarantine.build_quarantine_anti_join_sql
  2. Python matcher           services/source_quarantine.quarantine_matches_source
  3. trust policy             services/catalog_trust_policy._is_quarantined
  4. trust upserter lookup    services/catalog_row_trust_upserter
                              ._SELECT_BY_QUARANTINE_DOMAIN_SQL
  5. CLI product preview      scripts/manage_source_quarantine._product_match_clause
  6. CLI seed preview         scripts/manage_source_quarantine._seed_domain_match
  7. Node twin                PIVOTA-Agent/src/services/catalogTrustPolicy.isQuarantined
                              (cross-repo — parity checked by execution in the PR,
                              not importable from here)

The PR that fixed 3 and 4 originally called it "four comparators". 5 and 6 are
the operator's ONLY preview of a destructive action, and leaving them on a bare
`lower()` while the WRITE canonicalised made the dry-run under-report blast
radius by 100%: `--dry-run-proposed --match-value www.mintree.us` reported 0
impact, then `create` blocked 120 products.

#1637 normalised 1 and 2 (lowercase + `www.` strip, both sides). 3 and 4 still
did a bare `lower()`, so they disagreed — and `create_quarantine` accepts
whatever an operator types.

THE FAILURE THAT WAS LIVE-CAPABLE. Quarantine `www.mintree.us`. A row with
`source_domain = 'mintree.us'` is EXCLUDED by the normalised anti-join, so it
loses the canonical pick and its APV row is not rebuilt — while (3) returns
False, so `serving_decision` stays `public`, `serving_eligible` stays true and
the URL stays in the sitemap. The sitemap then advertises a PDP with no view row.

Prod was safe only because no `www.`-prefixed seed sat on a quarantined
storefront. A data coincidence, not an invariant, and silent on both sides.

This file is the invariant ON THE NORMALISATION AXIS ONLY, and that scope is
deliberate. The four comparators legitimately read DIFFERENT domain-source
chains:

    anti-join (assembler / reconciler cron)  cp.source_domain alone
    trust policy                             product -> external_seed -> merchant_store
    trust upserter lookup                    coalesce(cp, eps, epm, ms, '')

so feeding them one `row_domain` cannot prove they agree in general — only that
they spell the same storefront the same way. An earlier version of this
docstring claimed more than that, and a mutant deleting external_seed /
merchant_store from the trust policy's chain passed 156 tests.
`test_each_comparator_reads_its_own_documented_chain` closes that.

The chain axis itself — where a NULL `cp.source_domain` with a matching
`eps.domain` is blocked by two comparators and not the third — is a separate,
real divergence, tracked separately. Do not "fix" it by widening this file's
claim.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

import pytest

from services.catalog_row_trust_upserter import _SELECT_BY_QUARANTINE_DOMAIN_SQL
from services.catalog_trust_policy import _is_quarantined
from services.source_quarantine import (
    MATCH_TYPE_DOMAIN,
    Quarantine,
    build_quarantine_anti_join_sql,
    bare_domain,
    quarantine_matches_source,
    sql_bare_domain,
)

# Every combination of the forms an operator or an ingest lane can produce.
DOMAIN_FORMS = ["mintree.us", "www.mintree.us", "MINTREE.US", "WWW.MinTree.US", "  mintree.us  "]
NON_MATCHING = ["beplain.com", "notmintree.us", "shop.mintree.us", "mintree.us.evil.com"]


def _quarantine(match_value: str) -> Quarantine:
    return Quarantine(
        quarantine_id=1,
        match_type=MATCH_TYPE_DOMAIN,
        match_value=match_value,
        state="active",
        reason="currency mismatch",
        expires_at=None,
        created_by="audit_offer_currency",
        created_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        revoked_at=None,
        revoked_by=None,
        metadata=None,
    )


# --- the four comparators, each reduced to "does this pair match?" -----------

def _verdict_anti_join(row_domain, match_value) -> bool:
    """(1) The SQL anti-join. Blocked == the row does NOT survive."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE r (id TEXT, domain TEXT, merchant_id TEXT, platform TEXT,"
                 " source_system TEXT, source_ref TEXT)")
    conn.execute("CREATE TABLE catalog_source_quarantine (quarantine_id INTEGER,"
                 " match_type TEXT, match_value TEXT, state TEXT, expires_at TEXT)")
    conn.execute("INSERT INTO r VALUES ('x',?,NULL,NULL,NULL,NULL)", (row_domain,))
    conn.execute("INSERT INTO catalog_source_quarantine VALUES (1,'domain',?,'active',NULL)",
                 (match_value,))
    frag = build_quarantine_anti_join_sql(
        "r.domain", "r.merchant_id", "r.platform", "r.source_system", "r.source_ref"
    )
    survived = conn.execute(f"SELECT id FROM r WHERE 1=1 {frag}").fetchall()
    return not survived


def _verdict_python_matcher(row_domain, match_value) -> bool:
    """(2) quarantine_matches_source."""
    return quarantine_matches_source(
        _quarantine(match_value),
        domain=row_domain,
        merchant_id=None,
        platform=None,
        source_system=None,
        source_ref=None,
    )


def _verdict_trust_policy(row_domain, match_value) -> bool:
    """(3) catalog_trust_policy._is_quarantined."""
    return _is_quarantined(
        product={"source_domain": row_domain, "merchant_id": None, "platform": None},
        external_seed=None,
        merchant_store=None,
        active_quarantines=[{
            "match_type": "domain",
            "match_value": match_value,
            "state": "active",
            "expires_at": None,
        }],
        now=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )


def _replace_balanced(sql: str, start_token: str, replacement: str) -> str:
    """Replace the balanced-paren expression starting at `start_token`.

    A regex cannot do this safely — a non-greedy one stops at the first `))`,
    which is INSIDE the chain, and truncates the predicate mid-CASE. An exact
    literal is worse: it silently stopped matching the moment the chain gained
    `nullif(...)` per leg, and this harness then tested a different predicate
    from the one that ships. Counting parens is the only version that cannot
    drift quietly.
    """
    i = sql.index(start_token)
    depth, j = 0, i + len("coalesce")
    while True:
        if sql[j] == "(":
            depth += 1
        elif sql[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return sql[:i] + replacement + sql[j + 1:]


def _verdict_upserter_lookup(row_domain, match_value) -> bool:
    """(4) The trust upserter's by-domain lookup — selected == matched.

    Only the WHERE predicate is exercised; the surrounding CTE join is a
    different concern and would need the whole catalog schema.
    """
    where = _SELECT_BY_QUARANTINE_DOMAIN_SQL
    predicate = where[where.rindex("WHERE") + len("WHERE"):].split("ORDER BY")[0]
    # The real query coalesces four sources; this harness has only one column.
    # Matched by REGEX, not by an exact literal: an exact-string replace silently
    # stopped matching the moment the chain gained `nullif(...)` per leg, and the
    # predicate then ran with `cp.source_domain` unresolved — 48 tests failed
    # loudly, but a subtler edit could have left it merely wrong.
    # ALL occurrences: sql_bare_domain expands its argument three times (both
    # CASE branches plus the ELSE), so replacing only the first leaves the rest
    # referencing columns this harness does not have.
    while "coalesce(nullif(cp.source_domain" in predicate:
        predicate = _replace_balanced(predicate, "coalesce(nullif(cp.source_domain", "r.domain")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE r (id TEXT, domain TEXT)")
    conn.execute("INSERT INTO r VALUES ('x',?)", (row_domain,))
    rows = conn.execute(f"SELECT id FROM r WHERE {predicate}", {"match_value": match_value}).fetchall()
    return bool(rows)


def _cli_clause_verdict(clause: str, column: str, row_domain, match_value) -> bool:
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE t ({column} TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (row_domain,))
    sql = f"SELECT 1 FROM t WHERE {clause}"
    return bool(conn.execute(sql, {"match_value": match_value}).fetchall())


def _verdict_cli_product_preview(row_domain, match_value) -> bool:
    """(5) The operator's dry-run over catalog_products."""
    from scripts.manage_source_quarantine import _product_match_clause

    return _cli_clause_verdict(
        _product_match_clause("domain").replace("p.source_domain", "t.source_domain"),
        "source_domain", row_domain, match_value,
    )


def _verdict_cli_seed_preview(row_domain, match_value) -> bool:
    """(6) The operator's dry-run over external_product_seeds."""
    from scripts.manage_source_quarantine import _seed_domain_match

    return _cli_clause_verdict(
        _seed_domain_match().replace("e.domain", "t.domain"),
        "domain", row_domain, match_value,
    )


COMPARATORS = {
    "anti_join": _verdict_anti_join,
    "python_matcher": _verdict_python_matcher,
    "trust_policy": _verdict_trust_policy,
    "upserter_lookup": _verdict_upserter_lookup,
    "cli_product_preview": _verdict_cli_product_preview,
    "cli_seed_preview": _verdict_cli_seed_preview,
}


# --- the invariant ----------------------------------------------------------

@pytest.mark.parametrize("row_domain", DOMAIN_FORMS)
@pytest.mark.parametrize("match_value", DOMAIN_FORMS)
def test_all_four_comparators_agree_that_it_MATCHES(row_domain, match_value):
    """Every form of the same storefront must match every other form.

    This is the cell that was broken: row `mintree.us` + quarantine
    `www.mintree.us` matched on (1) and (2) and missed on (3) and (4).
    """
    verdicts = {name: fn(row_domain, match_value) for name, fn in COMPARATORS.items()}
    assert all(verdicts.values()), (
        f"comparators disagree for row={row_domain!r} match_value={match_value!r}: {verdicts}"
    )


@pytest.mark.parametrize("row_domain", NON_MATCHING)
@pytest.mark.parametrize("match_value", DOMAIN_FORMS)
def test_all_four_comparators_agree_that_it_does_NOT_match(row_domain, match_value):
    """Over-blocking splits them just as badly as under-blocking.

    `shop.mintree.us` and `mintree.us.evil.com` are lookalikes, not the store;
    a comparator that treats the match as a suffix would silently delete a real
    merchant's products on some doors and not others.
    """
    verdicts = {name: fn(row_domain, match_value) for name, fn in COMPARATORS.items()}
    assert not any(verdicts.values()), (
        f"comparators over-block for row={row_domain!r} match_value={match_value!r}: {verdicts}"
    )


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_match_value_matches_nothing_on_any_comparator(blank):
    """A blank quarantine must not select the whole corpus on ANY door.

    `match_value` is `TEXT NOT NULL` with no non-empty CHECK and these rows come
    from direct SQL ops, so a blank is reachable. On the upserter lookup — which
    feeds a trust WRITE — that would be a mass mis-classification.
    """
    for name, fn in COMPARATORS.items():
        assert fn("mintree.us", blank) is False, f"{name} matched a blank match_value"
        assert fn("", blank) is False, f"{name} matched blank against blank"


class _CaptureDb:
    """Records the values `create_quarantine` actually writes."""

    def __init__(self):
        self.written = None

    async def fetch_one(self, query, values=None):
        self.written = dict(values or {})
        return {
            "quarantine_id": 1,
            "match_type": self.written["match_type"],
            "match_value": self.written["match_value"],
            "state": "active",
            "reason": self.written.get("reason"),
            "expires_at": None,
            "created_by": self.written["created_by"],
            "created_at": None,
            "revoked_at": None,
            "revoked_by": None,
            "metadata": None,
        }

    async def fetch_all(self, query, values=None):
        return []

    async def execute(self, query, values=None):
        return None


@pytest.mark.parametrize("raw", DOMAIN_FORMS)
def test_create_quarantine_STORES_the_canonical_domain(raw):
    """Drives the real writer, not the normaliser.

    Asserting `bare_domain(raw) == 'mintree.us'` tests the helper and
    leaves the WRITER free to skip it — a mutant disabling the write-time
    canonicalisation survived exactly that. What matters is the value that
    lands in the row.
    """
    import asyncio

    from services.source_quarantine import create_quarantine

    db = _CaptureDb()
    q = asyncio.run(
        create_quarantine(
            match_type="domain", match_value=raw, reason=None, created_by="t", db=db
        )
    )
    assert db.written["match_value"] == "mintree.us", (
        f"create_quarantine stored {db.written['match_value']!r} for input {raw!r} — "
        "an operator's spelling reached the table verbatim"
    )
    assert q.match_value == "mintree.us"


def test_create_quarantine_does_not_lowercase_non_domain_match_values():
    """Only `domain` is a hostname. `merchant_platform` ids are case-sensitive.

    Canonicalising them would silently stop a real quarantine from matching.
    """
    import asyncio

    from services.source_quarantine import create_quarantine

    db = _CaptureDb()
    asyncio.run(
        create_quarantine(
            match_type="merchant_platform",
            match_value="Merch_ABC:shopify",
            reason=None,
            created_by="t",
            db=db,
        )
    )
    assert db.written["match_value"] == "Merch_ABC:shopify"


def test_create_quarantine_rejects_a_domain_that_normalises_to_nothing():
    """`www.` alone, or whitespace, is not a storefront."""
    import asyncio

    from services.source_quarantine import create_quarantine

    for raw in ["   ", "www.", "WWW."]:
        with pytest.raises(ValueError):
            asyncio.run(
                create_quarantine(
                    match_type="domain", match_value=raw, reason=None,
                    created_by="t", db=_CaptureDb(),
                )
            )


def test_the_sql_helper_and_the_python_helper_are_the_same_rule():
    """`sql_bare_domain` and `bare_domain` must not drift apart.

    They are separate implementations of one rule — one in SQL, one in Python —
    which is the only unavoidable duplication here. Compared by execution.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE r (v TEXT)")
    for raw in DOMAIN_FORMS + NON_MATCHING + ["", "   "]:
        conn.execute("DELETE FROM r")
        conn.execute("INSERT INTO r VALUES (?)", (raw,))
        sql_result = conn.execute(f"SELECT {sql_bare_domain('r.v')} FROM r").fetchone()[0]
        py_result = bare_domain(raw) or None
        assert sql_result == py_result, (
            f"SQL and Python normalisers disagree on {raw!r}: {sql_result!r} vs {py_result!r}"
        )


# --- the chain axis ---------------------------------------------------------
#
# Each comparator reads its own documented set of domain sources. That is by
# design, but it must be PINNED: a mutant deleting external_seed/merchant_store
# from the trust policy's chain passed 156 tests, and it is a live under-block —
# every NULL-`source_domain` mirror row would stay `public` under an active
# quarantine, silently.

@pytest.mark.parametrize(
    "source_field",
    ["product", "external_seed", "merchant_store"],
)
def test_trust_policy_reads_its_whole_documented_chain(source_field):
    """product.source_domain -> external_seed.domain -> merchant_store.domain.

    `cp.source_domain` is nullable (migration 133) and the domain-less mirror
    cohort is large and current, so the fallback legs are not decorative — they
    are how most of that cohort is matched at all.
    """
    args = {
        "product": {"source_domain": None, "merchant_id": None, "platform": None},
        "external_seed": None,
        "merchant_store": None,
    }
    if source_field == "product":
        args["product"] = {"source_domain": "mintree.us", "merchant_id": None, "platform": None}
    elif source_field == "external_seed":
        args["external_seed"] = {"domain": "mintree.us"}
    else:
        args["merchant_store"] = {"domain": "mintree.us"}

    assert _is_quarantined(
        **args,
        active_quarantines=[{
            "match_type": "domain", "match_value": "mintree.us",
            "state": "active", "expires_at": None,
        }],
        now=datetime(2026, 7, 30, tzinfo=timezone.utc),
    ) is True, f"the {source_field} leg of the domain chain is not read"


def test_trust_policy_chain_precedence_is_product_first():
    """A product's own domain wins over a seed's — otherwise a shared seed
    could drag an unrelated product into a quarantine."""
    assert _is_quarantined(
        product={"source_domain": "beplain.com", "merchant_id": None, "platform": None},
        external_seed={"domain": "mintree.us"},
        merchant_store=None,
        active_quarantines=[{
            "match_type": "domain", "match_value": "mintree.us",
            "state": "active", "expires_at": None,
        }],
        now=datetime(2026, 7, 30, tzinfo=timezone.utc),
    ) is False


def test_upserter_lookup_reads_all_four_domain_sources():
    """coalesce(cp.source_domain, eps.domain, epm.domain, ms.domain, '')."""
    sql = _SELECT_BY_QUARANTINE_DOMAIN_SQL
    for col in ("cp.source_domain", "eps.domain", "epm.domain", "ms.domain"):
        assert col in sql, f"{col} dropped from the upserter's domain chain"


def test_create_quarantine_rejects_a_url_instead_of_silently_storing_it():
    """`bare_domain` is a strip/lower/www rule, not a URL parser.

    `https://www.mintree.us/products/x` would be stored verbatim and then match
    nothing on any of the seven comparators — a quarantine that reports success
    and blocks nobody, which is the precise failure this workstream exists to
    end. Reject loudly instead of accepting a value that cannot work.
    """
    import asyncio

    from services.source_quarantine import create_quarantine

    for raw in [
        "https://www.mintree.us/products/x",
        "http://mintree.us",
        "mintree.us:443",
        "mintree.us/products",
        "min tree.us",
    ]:
        with pytest.raises(ValueError) as exc:
            asyncio.run(
                create_quarantine(
                    match_type="domain", match_value=raw, reason=None,
                    created_by="t", db=_CaptureDb(),
                )
            )
        assert "bare hostname" in str(exc.value), (
            f"rejection message for {raw!r} does not tell the operator what to pass"
        )


def test_create_quarantine_still_accepts_a_plain_hostname():
    """The guard must not reject legitimate values — including the 15 forms
    already live in prod (hyphens, multi-label TLDs, myshopify subdomains)."""
    import asyncio

    from services.source_quarantine import create_quarantine

    for raw in [
        "mintree.us", "reddane.co.za", "wholesale.publicgoods.com",
        "dearbarber.co.uk", "jwx893-fz.myshopify.com", "biologique-recherche.com",
    ]:
        db = _CaptureDb()
        asyncio.run(
            create_quarantine(
                match_type="domain", match_value=raw, reason=None, created_by="t", db=db
            )
        )
        assert db.written["match_value"] == raw


# ---------------------------------------------------------------------------
# THE CHAIN AXIS (#1643) — which COLUMNS each comparator reads
# ---------------------------------------------------------------------------
#
# #1641 made all seven spell a domain the same way. It did NOT make them read
# the same columns: the assembler and the reconciler cron used
# `cp.source_domain` ALONE while the trust layer used the full chain. On a row
# with a NULL source_domain — 4,007 of 14,124 in prod — a quarantined storefront
# kept its canonical pick while trust marked it blocked.
#
# These tests parametrise over (cp, eps, epm, ms) TUPLES rather than a single
# row_domain, which is what the previous version of this file could not see.

CHAIN_SHAPES = [
    # (label,          cp,             eps,            epm,            ms)
    ("cp only",        "mintree.us",   None,           None,           None),
    ("eps only",       None,           "mintree.us",   None,           None),
    ("epm only",       None,           None,           "mintree.us",   None),
    ("ms only",        None,           None,           None,           "mintree.us"),
    ("cp empty + eps", "",             "mintree.us",   None,           None),
    ("cp empty + ms",  "",             None,           None,           "mintree.us"),
]


def _catalog_chain_verdict(cp, eps, epm, ms, match_value="mintree.us") -> bool:
    """Run the REAL assembler anti-join over a catalog_products-shaped row."""
    from services.agent_pdp_view_assembler import _SOURCE_QUARANTINE_ANTI_JOIN

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE catalog_products (product_key TEXT, merchant_id TEXT, platform TEXT,"
        " source_product_id TEXT, source_system TEXT, source_domain TEXT, source_ref TEXT)"
    )
    conn.execute(
        "CREATE TABLE external_product_seeds (id TEXT, external_product_id TEXT, domain TEXT,"
        " attached_product_key TEXT, status TEXT, updated_at TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE merchant_stores (store_id TEXT, merchant_id TEXT, platform TEXT, domain TEXT,"
        " status TEXT, is_primary INT, last_sync TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE catalog_source_quarantine (quarantine_id INTEGER, match_type TEXT,"
        " match_value TEXT, state TEXT, expires_at TEXT)"
    )
    # The minted leg prefers an identity-carrying seed, so it references this
    # table even when empty. Kept as a stub here; the ordering it drives is
    # asserted on real Postgres in test_quarantine_domain_chain_postgres, where
    # multi-row groups make the tie-break observable at all.
    conn.execute("CREATE TABLE pdp_identity_listing (product_id TEXT, source_listing_ref TEXT)")
    # source_system decides WHICH seed leg applies, so pick it from the fixture.
    system = "catalog_enrichment_agent_v1" if epm else "external_product_seeds_mirror_v1"
    conn.execute(
        "INSERT INTO catalog_products VALUES ('pk','m1','shopify','ext_1',?,?,NULL)",
        (system, cp),
    )
    if eps:
        conn.execute(
            "INSERT INTO external_product_seeds VALUES ('s1','ext_1',?,NULL,'active','2026-01-01','2026-01-01')",
            (eps,),
        )
    if epm:
        conn.execute(
            "INSERT INTO external_product_seeds VALUES ('s2','ext_2',?,'pk','active','2026-01-01','2026-01-01')",
            (epm,),
        )
    if ms:
        conn.execute("INSERT INTO merchant_stores VALUES ('st1','m1','shopify',?,'active',1,'2026-01-01','2026-01-01')", (ms,))
    conn.execute(
        "INSERT INTO catalog_source_quarantine VALUES (1,'domain',?,'active',NULL)", (match_value,)
    )
    sql = f"SELECT product_key FROM catalog_products cp WHERE 1=1 {_SOURCE_QUARANTINE_ANTI_JOIN}"
    return not conn.execute(sql).fetchall()


@pytest.mark.parametrize("label,cp,eps,epm,ms", CHAIN_SHAPES, ids=[s[0] for s in CHAIN_SHAPES])
def test_the_assembler_anti_join_reads_the_WHOLE_domain_chain(label, cp, eps, epm, ms):
    """Every source the trust layer reads, the anti-join must read too.

    Each shape here was a live divergence before #1643: the trust layer blocked
    and the anti-join did not, so a quarantined storefront kept the canonical
    pick and shadowed the real merchant's PDP.
    """
    assert _catalog_chain_verdict(cp, eps, epm, ms) is True, (
        f"[{label}] the assembler's anti-join is blind to this domain source"
    )


@pytest.mark.parametrize("label,cp,eps,epm,ms", CHAIN_SHAPES, ids=[s[0] for s in CHAIN_SHAPES])
def test_the_assembler_anti_join_answers_the_other_way(label, cp, eps, epm, ms):
    """A predicate that always blocks is not a gate."""
    assert _catalog_chain_verdict(cp, eps, epm, ms, match_value="unrelated.com") is False, (
        f"[{label}] row blocked by an unrelated quarantine"
    )


def test_cp_source_domain_wins_over_the_fallback_legs():
    """A product's own domain must outrank a seed's.

    Otherwise a shared seed drags an unrelated product into a quarantine — the
    same precedence the trust policy uses.
    """
    assert _catalog_chain_verdict("beplain.com", "mintree.us", None, None) is False


def test_the_assembler_and_reconciler_use_THE_SAME_expression():
    """Two call sites, one rule. They diverged for months; pin them together."""
    from jobs.agent_pdp_view_reconciler_cron import _truth_cte
    from services.agent_pdp_view_assembler import _SOURCE_QUARANTINE_ANTI_JOIN
    from services.source_quarantine import CATALOG_PRODUCT_DOMAIN_SQL

    assert CATALOG_PRODUCT_DOMAIN_SQL in _SOURCE_QUARANTINE_ANTI_JOIN
    assert CATALOG_PRODUCT_DOMAIN_SQL in _truth_cte()
