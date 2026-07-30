"""All FOUR domain comparators must give the same verdict for the same pair.

Four places compare a row's domain against `catalog_source_quarantine.match_value`:

  1. SQL anti-join            services/source_quarantine.build_quarantine_anti_join_sql
  2. Python matcher           services/source_quarantine.quarantine_matches_source
  3. trust policy             services/catalog_trust_policy._is_quarantined
  4. trust upserter lookup    services/catalog_row_trust_upserter
                              ._SELECT_BY_QUARANTINE_DOMAIN_SQL

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

This file is the invariant. It runs all four over the same matrix and asserts
they never disagree — so the next comparator (or the next normalisation change)
cannot quietly split them again.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from services.catalog_row_trust_upserter import _SELECT_BY_QUARANTINE_DOMAIN_SQL
from services.catalog_trust_policy import _is_quarantined
from services.source_quarantine import (
    MATCH_TYPE_DOMAIN,
    Quarantine,
    build_quarantine_anti_join_sql,
    normalize_domain,
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


def _verdict_upserter_lookup(row_domain, match_value) -> bool:
    """(4) The trust upserter's by-domain lookup — selected == matched.

    Only the WHERE predicate is exercised; the surrounding CTE join is a
    different concern and would need the whole catalog schema.
    """
    where = _SELECT_BY_QUARANTINE_DOMAIN_SQL
    predicate = where[where.rindex("WHERE") + len("WHERE"):].split("ORDER BY")[0]
    # The real query coalesces four sources; here only cp.source_domain exists.
    predicate = predicate.replace(
        "coalesce(cp.source_domain, eps.domain, epm.domain, ms.domain, '')", "r.domain"
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE r (id TEXT, domain TEXT)")
    conn.execute("INSERT INTO r VALUES ('x',?)", (row_domain,))
    rows = conn.execute(f"SELECT id FROM r WHERE {predicate}", {"match_value": match_value}).fetchall()
    return bool(rows)


COMPARATORS = {
    "anti_join": _verdict_anti_join,
    "python_matcher": _verdict_python_matcher,
    "trust_policy": _verdict_trust_policy,
    "upserter_lookup": _verdict_upserter_lookup,
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

    Asserting `normalize_domain(raw) == 'mintree.us'` tests the helper and
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
    """`sql_bare_domain` and `normalize_domain` must not drift apart.

    They are separate implementations of one rule — one in SQL, one in Python —
    which is the only unavoidable duplication here. Compared by execution.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE r (v TEXT)")
    for raw in DOMAIN_FORMS + NON_MATCHING + ["", "   "]:
        conn.execute("DELETE FROM r")
        conn.execute("INSERT INTO r VALUES (?)", (raw,))
        sql_result = conn.execute(f"SELECT {sql_bare_domain('r.v')} FROM r").fetchone()[0]
        py_result = normalize_domain(raw) or None
        assert sql_result == py_result, (
            f"SQL and Python normalisers disagree on {raw!r}: {sql_result!r} vs {py_result!r}"
        )
