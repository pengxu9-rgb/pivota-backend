"""The view-heal must cover every field the serving classifier reads off the view.

THE DEFECT THIS CLOSES. `index_pipeline_state_service` blocks on the VIEW, not the
catalog row: `no_image` reads `agent_pdp_view.image_url` and `short_description`
reads `agent_pdp_view.description`. `promote_brand_official_canonicals` heals a
stale view before scoring — but it only ever compared DESCRIPTION.

Measured in prod 2026-09-05: re-ingesting maccosmetics.com with images (PR #2069)
lifted the quality score — `low_quality` fell by 239 — and every one of those rows
immediately re-blocked on `no_image`, because their `agent_pdp_view.image_url` was
still the null written by the first (imageless) ingest and nothing in the heal
looked at it. The row was fixed; the copy the gate reads was not.

The test drives the REAL query constant against Postgres rather than asserting a
substring of it, so it fails if the predicate stops selecting an image-stale row —
including if someone rewrites the SQL in a different shape.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)"
)

_LONG = "d" * 80


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    yield engine
    engine.dispose()


def _reset(conn):
    from sqlalchemy import text

    for t in ("agent_pdp_view", "catalog_products"):
        conn.execute(text(f"DELETE FROM {t}"))


def _product(conn, *, pk, ck, description, image_url):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform,"
            " source_product_id, title, description, image_url, content_key,"
            " source_system, sync_status)"
            " VALUES (:pk,'m','external_seed',:pk,'T',:d,:img,:ck,"
            "         'catalog_enrichment_agent_v1','live')"
        ),
        {"pk": pk, "d": description, "img": image_url, "ck": ck},
    )


def _view(conn, *, ck, description, image_url):
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO agent_pdp_view (content_key, title, description, image_url)"
            " VALUES (:ck,'T',:d,:img)"
        ),
        {"ck": ck, "d": description, "img": image_url},
    )


def _stale(engine):
    """Run the runner's OWN query constant — never a copy."""
    from sqlalchemy import text

    import scripts.promote_brand_official_canonicals as promote

    with engine.begin() as conn:
        rows = conn.execute(text(promote._SELECT_STALE_APV)).fetchall()
    return {r[0] for r in rows}


def test_a_row_whose_image_arrived_after_the_view_is_healed(pg_engine):
    """The MAC case: description was always fine, the image arrived on a later
    ingest, and the view still holds the null the classifier blocks on."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_img", ck="ck_img", description=_LONG, image_url="https://cdn/x.jpg")
        _view(conn, ck="ck_img", description=_LONG, image_url=None)

    assert "ck_img" in _stale(pg_engine)


def test_a_view_that_already_agrees_is_left_alone(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_ok", ck="ck_ok", description=_LONG, image_url="https://cdn/x.jpg")
        _view(conn, ck="ck_ok", description=_LONG, image_url="https://cdn/x.jpg")

    assert _stale(pg_engine) == set()


def test_the_description_case_still_heals(pg_engine):
    """The behaviour that existed before the image clause must survive it."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_desc", ck="ck_desc", description=_LONG, image_url="https://cdn/x.jpg")
        _view(conn, ck="ck_desc", description="short", image_url="https://cdn/x.jpg")

    assert "ck_desc" in _stale(pg_engine)


def test_a_row_with_no_image_of_its_own_is_not_called_stale(pg_engine):
    """Heal what the ROW can supply. A row with no image cannot fix the view, and
    flagging it would loop the heal forever on a cohort it can never repair."""
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_none", ck="ck_none", description=_LONG, image_url=None)
        _view(conn, ck="ck_none", description=_LONG, image_url=None)

    assert _stale(pg_engine) == set()


def test_a_missing_view_row_is_stale(pg_engine):
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, pk="pk_new", ck="ck_new", description=_LONG, image_url="https://cdn/x.jpg")

    assert "ck_new" in _stale(pg_engine)
