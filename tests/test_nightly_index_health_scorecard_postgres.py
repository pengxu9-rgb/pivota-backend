"""Postgres gate for jobs/nightly_index_health_job._SCORECARD_QUERY.

The scorecard decides `domain_extractor_baselines.alert_state`, and 'regression'
blocks EVERY index_pipeline_state row of the domain (extractor_regression). On
2026-09-26 it blocked 551 rows across 9 domains, and none of them was an
extractor regression:

  * flat seeds (catalog_enrichment_agent_v1 lane: image_urls LIST, no snapshot,
    no description) scored 0.0 on description and image because the query only
    read snapshot/description/image_url/images. Their description lives on the
    attached catalog_products row;
  * seeds whose attached product was deliberately SUPPRESSED (its offers are
    suppressed with it) counted as "no price".

The statement is jsonb-heavy Postgres SQL that SQLite cannot execute, so its
semantics are pinned here, on the production dialect, one seeded domain per case.

🚨 THESE GATE FILES SHARE ONE DATABASE. `metadata.create_all(checkfirst=True)` for
the tables db/ owns, `CREATE TABLE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS` for
exactly the columns this file touches, and rows are deleted by this file's own
prefix — never tables. Each case uses its own domain and reads back only that
domain, so rows other gate files leave behind cannot move these numbers.
"""

from __future__ import annotations

import json
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_P = "nihsc-"  # this file's row prefix

# external_product_seeds has no SQLAlchemy model; other gate files create it
# with differing column sets (some with market/destination_url NOT NULL), so
# every insert below supplies those too.
_LIGHTWEIGHT_DDL = """
CREATE TABLE IF NOT EXISTS external_product_seeds (id text);
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS market text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS tool text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS destination_url text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS canonical_url text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS title text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data jsonb;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS attached_product_key text;
ALTER TABLE external_offer_snapshots ADD COLUMN IF NOT EXISTS domain varchar(256);
ALTER TABLE external_offer_snapshots ADD COLUMN IF NOT EXISTS last_checked_at timestamp;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS description text;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS image_url text;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS suppression_reason text;
ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS suppressed_at timestamptz;
ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS suppression_reason text;
ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS suppressed_at timestamptz
"""

_DESC = "A lightweight gel cream that hydrates for 72 hours."  # >= 10 chars
_IMG = "https://cdn.example.test/p.jpg"


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401  (registers catalog_products / catalog_offers)
    import db.external_offers  # noqa: F401  (registers external_offer_snapshots)
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(
        engine,
        tables=[
            metadata.tables["catalog_products"],
            metadata.tables["catalog_offers"],
            metadata.tables["external_offer_snapshots"],
        ],
        checkfirst=True,
    )
    with engine.begin() as conn:
        for stmt in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(stmt))
        _reset(conn)
    yield engine
    with engine.begin() as conn:
        _reset(conn)
    engine.dispose()


def _reset(conn):
    from sqlalchemy import text

    conn.execute(text("DELETE FROM external_offer_snapshots WHERE id LIKE :p"), {"p": _P + "%"})
    conn.execute(text("DELETE FROM external_product_seeds WHERE id LIKE :p"), {"p": _P + "%"})
    conn.execute(text("DELETE FROM catalog_offers WHERE offer_id LIKE :p"), {"p": _P + "%"})
    conn.execute(text("DELETE FROM catalog_products WHERE product_key LIKE :p"), {"p": _P + "%"})


def _product(conn, pk, *, description=None, image_url=None, suppressed=False,
             suppression_reason=None, suppressed_at=None, offer_price=25.0,
             offer_suppression_reason=None):
    """A catalog_products row plus (unless offer_price is None) one offer.

    `suppressed=True` is what prod holds for every suppressed in-window product
    (measured 2026-09-26: suppressed_at and suppression_reason always set
    together, and the offers suppressed with reason 'product_suppressed').
    """
    from sqlalchemy import text

    if suppressed:
        suppression_reason = suppression_reason or "step5_same_merchant_same_url_dup"
        suppressed_at = suppressed_at or "2026-07-10T00:00:00Z"
        offer_suppression_reason = offer_suppression_reason or "product_suppressed"
    conn.execute(
        text(
            "INSERT INTO catalog_products "
            "(product_key, merchant_id, platform, source_product_id, title, "
            " description, image_url, suppression_reason, suppressed_at) "
            "VALUES (:pk, 'external_seed', 'external_seed', :pk, :pk, "
            "        :description, :image_url, :reason, CAST(:sat AS timestamptz))"
        ),
        {"pk": pk, "description": description, "image_url": image_url,
         "reason": suppression_reason, "sat": suppressed_at},
    )
    if offer_price is not None:
        conn.execute(
            text(
                "INSERT INTO catalog_offers "
                "(offer_id, sku_key, product_key, merchant_id, list_price, "
                " suppression_reason, suppressed_at) "
                "VALUES (:oid, :oid, :pk, 'external_seed', :price, :reason, "
                "        CASE WHEN CAST(:reason AS text) IS NULL THEN NULL ELSE NOW() END)"
            ),
            {"oid": pk + "-offer", "pk": pk, "price": offer_price,
             "reason": offer_suppression_reason},
        )


def _seed(conn, sid, domain, *, seed_data, attached=None, title="Seed title",
          snapshots=1, hours_ago=1):
    """An external_product_seeds row and `snapshots` offer snapshots of its URL."""
    from sqlalchemy import text

    url = f"https://{domain}/products/{sid}"
    conn.execute(
        text(
            "INSERT INTO external_product_seeds "
            "(id, market, tool, destination_url, canonical_url, title, seed_data, "
            " status, attached_product_key) "
            "VALUES (:id, 'US', '*', :url, :url, :title, CAST(:sd AS jsonb), 'active', :apk)"
        ),
        {"id": sid, "url": url, "title": title, "sd": json.dumps(seed_data), "apk": attached},
    )
    for i in range(snapshots):
        conn.execute(
            text(
                "INSERT INTO external_offer_snapshots "
                "(id, market, canonical_url, url_hash, domain, last_checked_at) "
                "VALUES (:id, 'US', :url, :hash, :domain, "
                "        NOW() - make_interval(hours => :h))"
            ),
            {"id": f"{sid}-snap{i}", "url": url, "hash": f"{sid}-h{i}",
             "domain": domain, "h": hours_ago},
        )


def _flat(**extra):
    """The catalog_enrichment_agent_v1 seed shape, as stored in prod."""
    data = {
        "brand": "Brand", "title": "Seed title", "in_stock": True, "variants": [],
        "image_urls": [_IMG], "availability": "in_stock", "product_name": "Seed title",
        "validated_at": "2026-09-24T00:00:00Z", "agent_version": "v1",
        "category_path": "beauty/skincare", "merchant_inferred": False,
    }
    data.update(extra)
    return data


def _snapshot_shaped(description=_DESC, image_url=_IMG, **extra):
    data = {
        "snapshot": {"title": "Seed title", "description": description, "image_url": image_url},
        "image_urls": [_IMG],
    }
    data.update(extra)
    return data


def _score(conn, domain):
    from sqlalchemy import text

    from jobs.nightly_index_health_job import _SCORECARD_QUERY

    rows = [dict(r) for r in conn.execute(text(_SCORECARD_QUERY), {"min_sample": 1}).mappings()]
    mine = [r for r in rows if r["domain"] == domain]
    assert len(mine) <= 1
    if not mine:
        return None
    r = mine[0]
    return {k: int(r[k]) for k in ("total", "has_title", "has_description", "has_image", "has_price")}


# ---------------------------------------------------------------------------
# Flat seeds: judged by image_urls and the attached product's description
# ---------------------------------------------------------------------------


def test_flat_seed_with_described_product_counts_as_covered(pg_engine):
    """koolseoul.com: 147/147 seeds flat, product descriptions all present.
    The old query scored description 0.0 and image 0.0 -> 'regression'."""
    d = _P + "flat-covered.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-flat", description=_DESC, image_url=_IMG)
        _seed(conn, _P + "s-flat", d, seed_data=_flat(), attached=_P + "pk-flat")
        assert _score(conn, d) == {
            "total": 1, "has_title": 1, "has_description": 1, "has_image": 1, "has_price": 1,
        }


def test_flat_seed_image_urls_alone_counts_as_image(pg_engine):
    """The seed's own image_urls[0] is enough, whatever the catalog row holds."""
    d = _P + "flat-imgurls.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-imgurls", description=_DESC, image_url=None)
        _seed(conn, _P + "s-imgurls", d, seed_data=_flat(), attached=_P + "pk-imgurls")
        assert _score(conn, d)["has_image"] == 1


def test_flat_seed_falls_back_to_product_image(pg_engine):
    d = _P + "flat-cpimg.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-cpimg", description=_DESC, image_url=_IMG)
        _seed(conn, _P + "s-cpimg", d, seed_data=_flat(image_urls=[]), attached=_P + "pk-cpimg")
        assert _score(conn, d)["has_image"] == 1


def test_genuinely_empty_flat_product_still_counts_as_missing(pg_engine):
    """No description anywhere, a 9-char description, no image anywhere: all
    three are real gaps and must still drag coverage down."""
    d = _P + "flat-empty.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-none", description=None, image_url=None)
        _product(conn, _P + "pk-blank", description="", image_url="")
        _product(conn, _P + "pk-short", description="123456789", image_url=None)
        _seed(conn, _P + "s-none", d, seed_data=_flat(image_urls=[]), attached=_P + "pk-none")
        _seed(conn, _P + "s-blank", d, seed_data=_flat(image_urls=[""]), attached=_P + "pk-blank")
        _seed(conn, _P + "s-short", d, seed_data=_flat(image_urls=[]), attached=_P + "pk-short")
        assert _score(conn, d) == {
            "total": 3, "has_title": 3, "has_description": 0, "has_image": 0, "has_price": 3,
        }


def test_description_exactly_ten_chars_counts(pg_engine):
    d = _P + "flat-ten.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-ten", description="1234567890", image_url=_IMG)
        _seed(conn, _P + "s-ten", d, seed_data=_flat(), attached=_P + "pk-ten")
        assert _score(conn, d)["has_description"] == 1


# ---------------------------------------------------------------------------
# Snapshot-shaped seeds: scored exactly as before, no catalog fallback
# ---------------------------------------------------------------------------


def test_snapshot_seed_with_empty_description_is_not_rescued_by_the_catalog(pg_engine):
    """A crawler that starts writing empty descriptions is the regression this
    scorecard exists to catch. The attached catalog row often still holds the
    OLD copy; falling back to it would hide the regression. Measured on prod
    2026-09-26 by blanking every seed-level description in the query: gated,
    79 currently-ok domains flip to regression/degraded; ungated, only 4 do."""
    d = _P + "snap-empty.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-snap", description=_DESC, image_url=_IMG)
        _seed(conn, _P + "s-snap", d, seed_data=_snapshot_shaped(description="", image_url=""),
              attached=_P + "pk-snap")
        got = _score(conn, d)
        assert got["has_description"] == 0
        # image_urls and the catalog image are flat-seed fallbacks only.
        assert got["has_image"] == 0


def test_snapshot_seed_with_content_counts_as_covered(pg_engine):
    d = _P + "snap-ok.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-snapok", description=None, image_url=None)
        _seed(conn, _P + "s-snapok", d, seed_data=_snapshot_shaped(), attached=_P + "pk-snapok")
        assert _score(conn, d) == {
            "total": 1, "has_title": 1, "has_description": 1, "has_image": 1, "has_price": 1,
        }


def test_flat_description_field_on_the_seed_still_wins(pg_engine):
    """The existing seed fields keep precedence: a flat seed carrying its own
    description is judged by it, exactly as the old query judged it."""
    d = _P + "flat-owndesc.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-own", description=None, image_url=None)
        _seed(conn, _P + "s-own", d, seed_data=_flat(description=_DESC), attached=_P + "pk-own")
        assert _score(conn, d)["has_description"] == 1


# ---------------------------------------------------------------------------
# Suppressed products leave the sample
# ---------------------------------------------------------------------------


def test_suppressed_product_is_excluded_from_the_sample(pg_engine):
    """www.tomfordbeauty.com: 92 of 159 seeds attached to suppressed products,
    whose offers are suppressed with them. Counted, price coverage read 0.42;
    excluded, the sample is the 67 served products and price reads 1.0."""
    d = _P + "supp.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-live", description=_DESC, image_url=_IMG)
        _product(conn, _P + "pk-supp", description=_DESC, image_url=_IMG, suppressed=True)
        _seed(conn, _P + "s-live", d, seed_data=_snapshot_shaped(), attached=_P + "pk-live")
        _seed(conn, _P + "s-supp", d, seed_data=_snapshot_shaped(), attached=_P + "pk-supp")
        assert _score(conn, d) == {
            "total": 1, "has_title": 1, "has_description": 1, "has_image": 1, "has_price": 1,
        }


@pytest.mark.parametrize(
    "reason, suppressed_at",
    [
        ("brand_attribution_key_supersede", None),
        (None, "2026-09-12T00:00:00Z"),
    ],
    ids=["reason_only", "suppressed_at_only"],
)
def test_either_suppression_column_excludes(pg_engine, reason, suppressed_at):
    """Prod holds both columns together today; either one alone still means the
    product was deliberately withdrawn."""
    d = _P + "supp-one-col.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-live1", description=_DESC, image_url=_IMG)
        _product(conn, _P + "pk-half", description=_DESC, image_url=_IMG,
                 suppression_reason=reason, suppressed_at=suppressed_at,
                 offer_suppression_reason="product_suppressed")
        _seed(conn, _P + "s-live1", d, seed_data=_snapshot_shaped(), attached=_P + "pk-live1")
        _seed(conn, _P + "s-half", d, seed_data=_snapshot_shaped(), attached=_P + "pk-half")
        assert _score(conn, d)["total"] == 1


def test_unsuppressed_product_with_suppressed_offers_still_counts_as_unpriced(pg_engine):
    """The exclusion is PRODUCT-level on purpose. A served product whose offers
    were suppressed one by one (orphan_no_sku) is price-pipeline damage the
    scorecard must keep seeing."""
    d = _P + "offer-supp.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-orphan", description=_DESC, image_url=_IMG,
                 offer_suppression_reason="orphan_no_sku")
        _seed(conn, _P + "s-orphan", d, seed_data=_snapshot_shaped(), attached=_P + "pk-orphan")
        assert _score(conn, d) == {
            "total": 1, "has_title": 1, "has_description": 1, "has_image": 1, "has_price": 0,
        }


def test_unattached_seed_stays_in_the_sample(pg_engine):
    """No catalog row to be suppressed: the LEFT JOIN must not drop the seed."""
    d = _P + "unattached.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _seed(conn, _P + "s-unatt", d, seed_data=_snapshot_shaped(), attached=None)
        _seed(conn, _P + "s-dangling", d, seed_data=_flat(), attached=_P + "pk-does-not-exist")
        assert _score(conn, d) == {
            "total": 2, "has_title": 2, "has_description": 1, "has_image": 2, "has_price": 0,
        }


# ---------------------------------------------------------------------------
# Unchanged: one row per (domain, seed), 72h window
# ---------------------------------------------------------------------------


def test_seed_with_many_snapshots_counts_once(pg_engine):
    d = _P + "distinct.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-many", description=_DESC, image_url=_IMG)
        _seed(conn, _P + "s-many", d, seed_data=_flat(), attached=_P + "pk-many", snapshots=3)
        assert _score(conn, d)["total"] == 1


def test_seed_checked_outside_the_window_is_not_sampled(pg_engine):
    d = _P + "stale.test"
    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, _P + "pk-old", description=_DESC, image_url=_IMG)
        _seed(conn, _P + "s-old", d, seed_data=_flat(), attached=_P + "pk-old", hours_ago=73)
        assert _score(conn, d) is None
