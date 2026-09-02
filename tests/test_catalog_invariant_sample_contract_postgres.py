"""Postgres gate for the invariant runner's sample contract.

THE CONTRACT. `run_catalog_invariant_checks` reads every check's samples as
`row["subject_key"]`, so every `sample_sql` in services/catalog_invariant_checks
must project exactly one column, aliased `subject_key`. On the production
dialect a `databases` Record raises KeyError for a column the row does not
carry, and that KeyError lands in the runner's per-check handler: the check is
logged as "failed" with a traceback, its samples are lost and (until the same
fix) its verdict dropped out of `violated_count`.

WHY A POSTGRES GATE. Two checks shipped without the alias
(`dead_quality_component` projected `name`, `serving_eligible_not_renderable`
projected `product_key`) and nothing caught them for weeks:

  * the SQLite unit file (tests/test_catalog_invariant_checks.py) fakes rows
    that are always keyed `subject_key`;
  * each check's own Postgres gate read its samples by POSITION (`r[0]`), which
    is exactly the read that cannot see a wrong column NAME.

This executes every registered sample_sql against the migration-built schema
the PREPARE sweep maintains and asserts the column the ROW carries. It needs
no data: a statement's result columns are described even when it returns zero
rows. Named `*_postgres.py`, so the dialect gate discovers it on its own.
"""

from __future__ import annotations

import os

import pytest

# The PREPARE sweep already builds a private database holding the real schema —
# create_all, then main.py's startup DDL, then db/migrations/*.sql — and drops
# it afterwards. Reuse it rather than hand-roll DDL for the seven tables these
# statements touch outside `metadata`: a hand-copied fixture drifts silently.
from test_repo_sql_prepare_postgres import _gate_db_url, prepare  # noqa: F401

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)


# `pdp_identity_listing` is written by the gateway, not this repo: no model, no
# migration, no startup DDL declares it, so the PREPARE sweep counts every
# statement that joins it as a fixture gap. The one check that joins it
# (cross_domain_content_key_fragmented_identity) would fail UndefinedTable here
# and prove nothing about its projection. Same lightweight shape the other gate
# files use for it (test_agent_pdp_view_reconciler_postgres_integration.py):
# only the columns services/identity_join_sql.py reads. Additive, ADD COLUMN IF
# NOT EXISTS, so it cannot narrow a shape another fixture built first.
_FOREIGN_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS pdp_identity_listing (product_id text)",
    "ALTER TABLE pdp_identity_listing ADD COLUMN IF NOT EXISTS merchant_id text",
    "ALTER TABLE pdp_identity_listing ADD COLUMN IF NOT EXISTS source_listing_ref text",
    "ALTER TABLE pdp_identity_listing ADD COLUMN IF NOT EXISTS sellable_item_group_id text",
)


@pytest.fixture(scope="module")
def gate_db_url(prepare):
    """The PREPARE sweep's private database, plus the foreign table above."""
    import psycopg2

    url = _gate_db_url()
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for ddl in _FOREIGN_TABLE_DDL:
                cur.execute(ddl)
    finally:
        conn.close()
    return url


def _sql_checks():
    from services.catalog_invariant_checks import _CHECKS

    return [c for c in _CHECKS if c.get("sample_sql") is not None]


def test_the_contract_has_subjects():
    """A gate over zero checks is green for free. Pin that the SQL-driven
    checks are still registered as SQL (a `runner` check carries no sample_sql
    and is legitimately outside this contract)."""
    assert len(_sql_checks()) >= 10


@pytest.mark.parametrize("check", _sql_checks(), ids=lambda c: c["name"])
def test_sample_sql_projects_exactly_subject_key(gate_db_url, check):
    import psycopg2

    from services.catalog_invariant_checks import SAMPLE_KEY_COLUMN

    conn = psycopg2.connect(gate_db_url)
    try:
        conn.set_client_encoding("UTF8")
        with conn.cursor() as cur:
            cur.execute(check["sample_sql"])
            columns = [d.name for d in cur.description]
    finally:
        conn.close()

    assert columns == [SAMPLE_KEY_COLUMN], (
        f"{check['name']}: sample_sql projects {columns}; the runner reads "
        f"row[{SAMPLE_KEY_COLUMN!r}] and the positional fallback assumes ONE "
        f"column — alias it `... AS {SAMPLE_KEY_COLUMN}`"
    )
