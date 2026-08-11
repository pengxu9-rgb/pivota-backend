"""A failed overlay READ must never erase a published overlay.

THE DEFECT. `_fetch_enrichment_for_canonical` swallowed per-member exceptions and
returned None, which is also what it returns when there is genuinely no overlay.
`assemble_row` then built a row with the overlay columns empty and `UPSERT_SQL`
assigned EVERY column from EXCLUDED, so one transient `get_enrichment` hiccup
during a reconciler pass wrote the empties over live curated copy.

AND IT DID NOT SELF-HEAL, which is what makes it serious rather than annoying.
The write stamps `refreshed_at = NOW()`, and
`jobs/agent_pdp_view_reconciler_cron.py` only re-selects a content_key whose
truth timestamps moved PAST `refreshed_at`. An enrichment write never bumps
those. So the degraded row drops out of the very sweep that would repair it and
stays degraded until unrelated catalog truth happens to change.

WHY IT IS TESTED HERE, ON REAL POSTGRES. The fix is a `CASE WHEN
CAST(:preserve_enrichment AS boolean) THEN agent_pdp_view.col ELSE EXCLUDED.col
END` inside `ON CONFLICT DO UPDATE`. Whether that actually preserves the prior
value is a property of Postgres's UPSERT semantics — the reference to
`agent_pdp_view.col` resolves to the pre-UPDATE row — and no fake can tell you
if it is right. The statement is driven verbatim from the module; nothing is
lifted or retyped here.

🚨 PRIVATE DATABASE, not the shared gate schema: this file needs the REAL
`agent_pdp_view`, which is a db.catalog Table, and building it via
`metadata.create_all` in the database every sibling `test_*_postgres.py` shares
is the blast radius those files warn about. Created and dropped here.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_DB_NAME = f"apv_overlay_preserve_{os.getpid()}"

_CONTENT_KEY = "ck-overlay-preserve"
_BULLETS = ["Niacinamide 5%", "Fragrance free"]
_SCENARIOS = ["Morning routine, before SPF"]
_CURATED = "Curated Pivota copy that must survive a failed overlay read."
_RAW = "Raw storefront description."


def _private_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{_DB_NAME}"))


@pytest.fixture(scope="module")
def db_url():
    import psycopg2
    from sqlalchemy import create_engine

    import db.catalog  # noqa: F401  (registers agent_pdp_view on the shared MetaData)
    from db.database import metadata

    admin = psycopg2.connect(os.environ["DATABASE_URL"])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{_DB_NAME}"')
    admin.close()

    url = _private_url()
    engine = create_engine(url, future=True)
    # The real table, from the real Table definition — never a stub. A stub whose
    # column types drift is exactly the false confidence these gates exist to stop.
    metadata.tables["agent_pdp_view"].create(engine, checkfirst=True)
    # ...but db/catalog.py's Table is BEHIND the migrations: evidence_profile,
    # required_disclaimers, bullet_points and usage_scenarios exist only in
    # db/migrations. Layer every migration that touches agent_pdp_view on top, in
    # version order, so the fixture is the shipped shape rather than the model's
    # stale idea of it. Failures are tolerated: several of these files also touch
    # tables this private database does not have.
    import re as _re
    from sqlalchemy import text as _text

    migrations = sorted(
        (REPO_ROOT / "db" / "migrations").glob("*.sql"),
        key=lambda p: [int(t) if t.isdigit() else t for t in _re.split(r"(\d+)", p.name)],
    )
    for path in migrations:
        body = path.read_text(encoding="utf-8")
        if "agent_pdp_view" not in body:
            continue
        for statement in filter(None, (s.strip() for s in body.split(";"))):
            if "agent_pdp_view" not in statement:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(_text(statement))
            except Exception:  # noqa: BLE001 — costs coverage, cannot cause a false pass
                pass
    engine.dispose()
    try:
        yield url
    finally:
        admin = psycopg2.connect(os.environ["DATABASE_URL"])
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (_DB_NAME,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        admin.close()


def _row(**overrides):
    """A minimal but REAL assemble_row-shaped payload."""
    from services.agent_pdp_view_assembler import BACKFILL_REFRESH_SOURCE

    row = {
        "content_key": _CONTENT_KEY,
        "pivota_signature_id": None, "product_group_id": None,
        "brand": "AuraGlow", "title": "Glow Serum", "description": _RAW,
        "image_url": None, "image_urls": None, "currency": None,
        "price_min": None, "price_max": None, "offer_count": 0, "offers": None,
        "variants": None, "variants_count": 0, "gtin13": None,
        "category_path": None, "taxonomy_tags": None, "breadcrumb": None,
        "pdp_lifecycle_stage": None, "sync_status": None,
        "primary_merchant_id": "m1",
        "material": None, "material_source": None, "material_confidence": None,
        "care": None, "care_source": None, "care_confidence": None,
        "size_guide": None, "size_guide_source": None, "size_guide_confidence": None,
        "rating_value": None, "rating_count": None,
        "bullet_points": None, "usage_scenarios": None,
        "evidence_profile": None, "required_disclaimers": None,
        "refresh_source": BACKFILL_REFRESH_SOURCE,
        "refreshed_by_proposal_id": None,
        "preserve_enrichment": False, "preserve_evidence": False,
    }
    row.update(overrides)
    return row


async def _upsert(database, row):
    from services.agent_pdp_view_assembler import UPSERT_SQL, row_to_upsert_params

    await database.execute(UPSERT_SQL, row_to_upsert_params(row))


async def _read(database):
    row = dict(await database.fetch_one(
        "SELECT description, bullet_points, usage_scenarios, evidence_profile "
        "FROM agent_pdp_view WHERE content_key = :ck", {"ck": _CONTENT_KEY}
    ))
    # asyncpg hands back jsonb as a raw string unless a codec is registered, and
    # `databases` registers none. Decode here so the assertions compare values
    # rather than formatting.
    for column in ("bullet_points", "usage_scenarios", "evidence_profile"):
        if isinstance(row.get(column), str):
            row[column] = json.loads(row[column])
    return row


def _drive(url, coro_factory):
    import databases

    async def run():
        database = databases.Database(
            url if "+asyncpg" in url else url.replace("postgresql://", "postgresql+asyncpg://")
        )
        await database.connect()
        try:
            return await coro_factory(database)
        finally:
            await database.disconnect()

    return asyncio.new_event_loop().run_until_complete(run())


def test_a_failed_overlay_read_preserves_what_is_published(db_url) -> None:
    """The regression. Publish an overlay, then refresh with preserve flags set
    (what a failed fetch produces) and an otherwise-empty row."""
    async def scenario(database):
        await _upsert(database, _row(
            description=_CURATED, bullet_points=_BULLETS,
            usage_scenarios=_SCENARIOS, evidence_profile={"claims": ["a"]},
        ))
        before = await _read(database)
        # Second pass: the overlay fetch FAILED, so the row carries no overlay
        # values and the flags are set.
        await _upsert(database, _row(
            description=_RAW, preserve_enrichment=True, preserve_evidence=True,
        ))
        return before, await _read(database)

    before, after = _drive(db_url, scenario)
    assert before["description"] == _CURATED
    assert after["description"] == _CURATED, "curated copy was overwritten by raw"
    assert after["bullet_points"] == _BULLETS, "bullet_points were erased"
    assert after["usage_scenarios"] == _SCENARIOS, "usage_scenarios were erased"
    assert after["evidence_profile"] == {"claims": ["a"]}, "evidence_profile was erased"


def test_a_genuine_removal_still_propagates(db_url) -> None:
    """The other half, and the reason this is a tri-state rather than a blanket
    never-downgrade rule: a SUCCESSFUL fetch that finds nothing (an employee
    deleted the enrichment) must still clear the served overlay."""
    async def scenario(database):
        await _upsert(database, _row(
            description=_CURATED, bullet_points=_BULLETS,
            usage_scenarios=_SCENARIOS, evidence_profile={"claims": ["a"]},
        ))
        await _upsert(database, _row(
            description=_RAW, preserve_enrichment=False, preserve_evidence=False,
        ))
        return await _read(database)

    after = _drive(db_url, scenario)
    assert after["description"] == _RAW
    assert after["bullet_points"] is None
    assert after["usage_scenarios"] is None
    assert after["evidence_profile"] is None


def test_the_two_flags_are_independent(db_url) -> None:
    """An enrichment failure must not freeze evidence, or vice versa — they are
    separate reads and either can fail alone."""
    async def scenario(database):
        await _upsert(database, _row(
            description=_CURATED, bullet_points=_BULLETS,
            usage_scenarios=_SCENARIOS, evidence_profile={"claims": ["a"]},
        ))
        await _upsert(database, _row(
            description=_RAW, preserve_enrichment=True, preserve_evidence=False,
        ))
        return await _read(database)

    after = _drive(db_url, scenario)
    assert after["description"] == _CURATED, "enrichment should have been preserved"
    assert after["bullet_points"] == _BULLETS
    assert after["evidence_profile"] is None, "evidence should have been cleared"


async def _read_proposal(database):
    return (await database.fetch_one(
        "SELECT refreshed_by_proposal_id FROM agent_pdp_view WHERE content_key = :ck",
        {"ck": _CONTENT_KEY},
    ))["refreshed_by_proposal_id"]


def test_an_anonymous_refresh_does_not_erase_the_proposal_attribution(db_url) -> None:
    """Only the seed writer knows a proposal id. Every other rebuild — the
    reconciler cron, catalog_sync, the backfill, the repair scripts — passes
    None, and a straight `= EXCLUDED.refreshed_by_proposal_id` made each of them
    erase what the seed writer had recorded, on a field the agent PDP route
    serves. COALESCE keeps it."""
    async def scenario(database):
        await _upsert(database, _row(refreshed_by_proposal_id=4242))
        after_write = await _read_proposal(database)
        # An unrelated refresh that knows nothing about proposals.
        await _upsert(database, _row(refreshed_by_proposal_id=None))
        return after_write, await _read_proposal(database)

    after_write, after_anonymous = _drive(db_url, scenario)
    assert after_write == 4242
    assert after_anonymous == 4242, "an anonymous refresh erased the attribution"


def test_a_newer_proposal_still_replaces_the_old_one(db_url) -> None:
    """COALESCE must not freeze the field — a later seed commit owns it."""
    async def scenario(database):
        await _upsert(database, _row(refreshed_by_proposal_id=4242))
        await _upsert(database, _row(refreshed_by_proposal_id=9999))
        return await _read_proposal(database)

    assert _drive(db_url, scenario) == 9999
