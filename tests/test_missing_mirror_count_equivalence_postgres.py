"""The cheap missing-mirror chain must agree with the report chain, row for row.

THE FILENAME IS LOAD-BEARING (postgres-dialect-gate glob). Both chains are
Postgres-only — DISTINCT ON, window functions, `#>>` — so sqlite cannot run them.

scripts/mirror_external_seeds_to_catalog_products defines the mirrorable set
twice: COMMON_CTES builds the full report (every seed column + a dozen seed_data
extractions), MISSING_MIRROR_CTES answers "is anything missing?" without touching
seed_data. The materialization job now trusts the cheap one to decide whether to
do any work at all, so a disagreement is silent lost work: the job would report
"no missing mirrors" forever while seeds pile up unmirrored.

Every case below is one way the two chains could disagree, driven through the
REAL module constants (never lifted SQL), each asserted on the exact
external_product_id SET rather than just the count — a count match can hide two
compensating errors. `test_fixture_exercises_every_branch` fails if the fixture
stops discriminating, so a future edit cannot quietly turn these into
vacuous 0 == 0 comparisons.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_SCHEMA = f"mirror_equiv_test_{os.getpid()}"

# Seed rows: (id, external_product_id, market, title, status,
#             attached_product_key, updated_at, created_at)
_SEEDS = [
    # -- plain mirrorable winner, no catalog row: MISSING
    ("s-solo", "epid-solo", "US", "Solo Product", "active", None, "2026-08-01", "2026-07-01"),
    # -- already mirrored under the legacy singleton identity: NOT missing
    ("s-mirrored", "epid-mirrored", "US", "Mirrored", "active", None, "2026-08-01", "2026-07-01"),
    # -- already mirrored under an ADR-009 observed seller (merch_obs_*), which the
    #    identity join must see because it keys on platform+source_product_id with
    #    no merchant literal: NOT missing
    ("s-obs", "epid-obs", "US", "Observed", "active", None, "2026-08-01", "2026-07-01"),
    # -- inactive: excluded before ranking
    ("s-inactive", "epid-inactive", "US", "Inactive", "archived", None, "2026-08-01", "2026-07-01"),
    # -- blank/NULL external_product_id: excluded before ranking
    ("s-blank-epid", "   ", "US", "Blank Epid", "active", None, "2026-08-01", "2026-07-01"),
    ("s-null-epid", None, "US", "Null Epid", "active", None, "2026-08-01", "2026-07-01"),
    # -- winner has a blank title => whole group skipped, even though the LOSER
    #    of the group has a good title. Discriminates "filter the winner" from
    #    "filter then pick a winner".
    ("s-tw-win", "epid-titlewin", "US", "   ", "active", None, "2026-08-05", "2026-07-01"),
    ("s-tw-lose", "epid-titlewin", "GB", "Has A Title", "active", None, "2026-08-09", "2026-07-01"),
    # -- mirror image: the US winner has a title, the fresher GB loser is blank.
    #    MISSING. Together with the pair above this pins the market-first tiebreak.
    ("s-tg-win", "epid-titlegood", "US", "Good Title", "active", None, "2026-08-05", "2026-07-01"),
    ("s-tg-lose", "epid-titlegood", "GB", "  ", "active", None, "2026-08-09", "2026-07-01"),
    # -- no US row: freshest updated_at wins, and it has the blank title
    ("s-fresh-a", "epid-fresh", "GB", "   ", "active", None, "2026-08-20", "2026-07-01"),
    ("s-fresh-b", "epid-fresh", "DE", "Older But Titled", "active", None, "2026-08-02", "2026-07-01"),
    # -- updated_at NULL sorts last, so the titled row wins: MISSING
    ("s-nullupd-a", "epid-nullupd", "GB", "  ", "active", None, None, "2026-07-01"),
    ("s-nullupd-b", "epid-nullupd", "DE", "Titled Winner", "active", None, "2026-08-02", "2026-07-01"),
    # -- identical market+updated_at+created_at: `id ASC` breaks the tie, and the
    #    lower id has the blank title => skipped
    ("s-tie-a", "epid-tie", "GB", "   ", "active", None, "2026-08-02", "2026-07-01"),
    ("s-tie-b", "epid-tie", "GB", "Loser By Id", "active", None, "2026-08-02", "2026-07-01"),
    # -- over-length external_product_id (>128): skipped. Group-invariant filter.
    ("s-long", "e" * 129, "US", "Too Long", "active", None, "2026-08-01", "2026-07-01"),
    # -- product_key would exceed 255 (prefix is 36 chars): skipped
    ("s-longkey", "k" * 220, "US", "Long Key", "active", None, "2026-08-01", "2026-07-01"),
    # -- attached_product_key resolves to a live catalog row => group is present
    #    even though no platform+source_product_id row matches
    ("s-attached", "epid-attached", "US", "Attached", "active", "prod::agent::slug-1", "2026-08-01", "2026-07-01"),
    # -- attached_product_key points at a row that no longer exists => self-heal,
    #    the group is mirrorable again: MISSING
    ("s-dangling", "epid-dangling", "US", "Dangling", "active", "prod::gone::nope", "2026-08-01", "2026-07-01"),
    # -- GROUP-level attachment: the ranked winner is unattached, but a LOSER in
    #    the same group is attached to a live row. Both chains must treat the whole
    #    group as present (the 39-COSRX-shadow regression).
    ("s-mixed-win", "epid-mixed", "US", "Mixed Winner", "active", None, "2026-08-05", "2026-07-01"),
    ("s-mixed-att", "epid-mixed", "GB", "Mixed Attached", "active", "prod::agent::slug-2", "2026-08-09", "2026-07-01"),
    # -- an INACTIVE seed carries the attachment; the active_all status predicate
    #    means it must NOT count as present => MISSING. Pins that the cheap chain
    #    carried `lower(coalesce(status,''))='active'` onto its inlined NOT EXISTS.
    ("s-inact-att", "epid-inactatt", "GB", "Inactive Attached", "archived", "prod::agent::slug-3", "2026-08-09", "2026-07-01"),
    ("s-inact-win", "epid-inactatt", "US", "Active Winner", "active", None, "2026-08-05", "2026-07-01"),
]

# (product_key, merchant_id, platform, source_product_id)
_CATALOG = [
    ("prod::external_seed::external_seed::epid-mirrored", "external_seed", "external_seed", "epid-mirrored"),
    ("prod::external_seed::external_seed::epid-obs", "merch_obs_acme", "external_seed", "epid-obs"),
    # Path-C agent rows: source_product_id is a title slug, so ONLY the
    # attached_product_key back-link can see them.
    ("prod::agent::slug-1", "merch_obs_acme", "shopify", "some-title-slug-1"),
    ("prod::agent::slug-2", "merch_obs_acme", "shopify", "some-title-slug-2"),
    ("prod::agent::slug-3", "merch_obs_acme", "shopify", "some-title-slug-3"),
]

# Winners that survive every filter and have no mirror. Written out literally so
# the test states the expected answer rather than asking the code to define it.
_EXPECTED_MISSING = {
    "epid-solo",
    "epid-titlegood",
    "epid-nullupd",
    "epid-dangling",
    "epid-inactatt",
}


@pytest.fixture(scope="module")
def engine():
    from sqlalchemy import create_engine, text

    eng = create_engine(
        os.environ["DATABASE_URL"],
        future=True,
        connect_args={"options": f"-csearch_path={_SCHEMA}"},
    )
    raw = create_engine(os.environ["DATABASE_URL"], future=True)
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
    with eng.begin() as c:
        c.execute(
            text(
                "CREATE TABLE external_product_seeds ("
                " id text PRIMARY KEY, external_product_id text, market text,"
                " title text, status text, attached_product_key text,"
                " seed_data jsonb NOT NULL DEFAULT '{}'::jsonb,"
                " image_url text, domain text, tool text,"
                " updated_at timestamptz, created_at timestamptz)"
            )
        )
        c.execute(
            text(
                "CREATE TABLE catalog_products ("
                " product_key varchar(255) PRIMARY KEY, merchant_id varchar(64),"
                " platform varchar(64), source_product_id varchar(128),"
                " pivota_signature_id text, source_system varchar(64),"
                " image_url text, description text)"
            )
        )
        for s in _SEEDS:
            c.execute(
                text(
                    "INSERT INTO external_product_seeds (id, external_product_id,"
                    " market, title, status, attached_product_key, seed_data,"
                    " updated_at, created_at) VALUES (:id, :epid, :market, :title,"
                    " :status, :apk, :sd, :upd, :crt)"
                ),
                {
                    "id": s[0], "epid": s[1], "market": s[2], "title": s[3],
                    "status": s[4], "apk": s[5],
                    # non-trivial seed_data so the report chain does real work
                    "sd": '{"snapshot": {"description": "a description that is at'
                          ' least fifty characters long for the report", '
                          '"brand": "Acme", "product_type": "Serum"}}',
                    "upd": s[6], "crt": s[7],
                },
            )
        for p in _CATALOG:
            c.execute(
                text(
                    "INSERT INTO catalog_products (product_key, merchant_id,"
                    " platform, source_product_id) VALUES (:pk, :mid, :plat, :spid)"
                ),
                {"pk": p[0], "mid": p[1], "plat": p[2], "spid": p[3]},
            )
    yield eng
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))


def _missing_set(engine, cte_chain: str, projection: str) -> set:
    from sqlalchemy import text

    with engine.connect() as c:
        rows = c.execute(text(cte_chain + projection)).fetchall()
    return {r[0] for r in rows}


def _report_missing(engine) -> set:
    from scripts.mirror_external_seeds_to_catalog_products import COMMON_CTES

    return _missing_set(engine, COMMON_CTES, " SELECT external_product_id FROM missing")


def _cheap_missing(engine) -> set:
    from scripts.mirror_external_seeds_to_catalog_products import MISSING_MIRROR_CTES

    return _missing_set(
        engine, MISSING_MIRROR_CTES, " SELECT external_product_id FROM missing"
    )


def test_cheap_chain_matches_report_chain_exactly(engine) -> None:
    """The whole point: same set, not merely the same count."""
    report = _report_missing(engine)
    cheap = _cheap_missing(engine)

    assert cheap == report, (
        f"chains disagree — only in report: {sorted(report - cheap)}, "
        f"only in cheap: {sorted(cheap - report)}"
    )


def test_missing_set_is_the_expected_one(engine) -> None:
    """Pins the ANSWER independently, so both chains drifting together still fails.

    Without this, a shared-fragment edit that broke both identically would leave
    test_cheap_chain_matches_report_chain_exactly passing.
    """
    assert _report_missing(engine) == _EXPECTED_MISSING
    assert _cheap_missing(engine) == _EXPECTED_MISSING


def test_fixture_exercises_every_branch(engine) -> None:
    """A comparison over an empty or trivial set proves nothing. Fail loudly if
    the fixture ever stops discriminating."""
    from sqlalchemy import text

    missing = _cheap_missing(engine)
    assert missing, "fixture produced NO missing rows — the equality tests are vacuous"

    with engine.connect() as c:
        total_groups = c.execute(
            text(
                "SELECT count(DISTINCT external_product_id) FROM"
                " external_product_seeds WHERE external_product_id IS NOT NULL"
            )
        ).scalar()
        dupe_groups = c.execute(
            text(
                "SELECT count(*) FROM (SELECT external_product_id FROM"
                " external_product_seeds WHERE lower(coalesce(status,'')) = 'active'"
                " GROUP BY external_product_id HAVING count(*) > 1) d"
            )
        ).scalar()

    # Rows must be EXCLUDED as well as included, or the filters are untested.
    assert len(missing) < total_groups, "every group came back missing — filters untested"
    assert dupe_groups >= 5, "need multi-row groups to exercise winner selection"


@pytest.mark.asyncio
async def test_count_helper_returns_the_chain_count(engine) -> None:
    """count_missing_catalog_mirrors() must return the cheap chain's count off the
    real `database` handle — not just be correct as raw SQL."""
    import scripts.mirror_external_seeds_to_catalog_products as mirror

    captured = {}

    class _FakeDB:
        async def fetch_val(self, sql, values=None):
            captured["sql"] = sql
            from sqlalchemy import text

            with engine.connect() as c:
                return c.execute(text(sql)).scalar()

    original = mirror.database
    mirror.database = _FakeDB()
    try:
        count = await mirror.count_missing_catalog_mirrors()
    finally:
        mirror.database = original

    assert count == len(_EXPECTED_MISSING)
    # It must go through the cheap chain, not the report chain.
    assert "DISTINCT ON" in captured["sql"]
    assert "seed_data" not in captured["sql"]


def test_cheap_chain_never_reads_seed_data(engine) -> None:
    """The entire saving is not detoasting seed_data (~207 MB of TOAST on
    production against a ~25 MB heap). A future edit that reaches into
    seed_data here silently reintroduces the ~125s report cost."""
    from scripts.mirror_external_seeds_to_catalog_products import (
        COMMON_CTES,
        MISSING_MIRROR_CTES,
    )

    assert "seed_data" not in MISSING_MIRROR_CTES
    assert "SELECT *" not in MISSING_MIRROR_CTES
    assert "eps.*" not in MISSING_MIRROR_CTES
    # Contrast: the report chain legitimately does all three.
    assert "seed_data" in COMMON_CTES
    assert "eps.*" in COMMON_CTES


def test_both_chains_share_the_ranking_and_filter_fragments() -> None:
    """The two chains are kept in sync by construction, not by copy-paste."""
    from scripts.mirror_external_seeds_to_catalog_products import (
        COMMON_CTES,
        MISSING_MIRROR_CTES,
        _CANDIDATE_FILTERS,
        _WINNER_ORDER_BY,
    )

    for chain_name, chain in (
        ("COMMON_CTES", COMMON_CTES),
        ("MISSING_MIRROR_CTES", MISSING_MIRROR_CTES),
    ):
        assert _WINNER_ORDER_BY in chain, f"{chain_name} no longer shares the ranking"
        assert _CANDIDATE_FILTERS in chain, f"{chain_name} no longer shares the filters"
