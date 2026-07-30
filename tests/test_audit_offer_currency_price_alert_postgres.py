"""Production-dialect gate for the main audit's implausible-price query.

The unit file (tests/test_audit_offer_currency_price_alert.py) drives a fake DB
that never executes SQL, and the SQLite suite cannot gate Postgres behaviour
(standing lesson). This EXECUTES _PRICE_ALERT_SQL on real Postgres with seeded
rows proving the threshold, the USD-only scope, the domain-keyed scope, the
suppressed-rows-visible rule, and the ordering.

Run (never against prod — it INSERTs fixture rows, keyed under 'pa-gate-'):
    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_audit_offer_currency_price_alert_postgres.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "the SQLite suite cannot stand in for it"
    ),
)

_PREFIX = "pa-gate-"


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401  (registers catalog_offers on the MetaData)
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)

    def offer(conn, offer_id, price, currency="USD", domain="oiad.us", suppressed=False):
        conn.execute(text(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
            " currency, list_price, source_system, source_domain, suppressed_at)"
            " VALUES (:o, :sk, :pk, 'pa_gate', :cur, :price, 'external_product_seeds_mirror_v1',"
            " :dom, CASE WHEN CAST(:sup AS boolean) THEN NOW() ELSE NULL END)"),
            {"o": offer_id, "sk": f"{offer_id}::sku", "pk": f"{_PREFIX}pk",
             "cur": currency, "price": price, "dom": domain, "sup": suppressed})

    _cleanup(engine)
    with engine.begin() as conn:
        offer(conn, f"{_PREFIX}oiad-live", 400000.00)
        offer(conn, f"{_PREFIX}suppressed-big", 250000.00, suppressed=True)
        offer(conn, f"{_PREFIX}below-threshold", 999.99)
        offer(conn, f"{_PREFIX}foreign-big", 300000.00, currency="KRW")
        # lower-cased usd with padding must still count as USD (trim+upper)
        offer(conn, f"{_PREFIX}padded-usd", 5000.00, currency=" usd ")
        offer(conn, f"{_PREFIX}domainless-big", 999999.00, domain=None)
    yield engine
    _cleanup(engine)
    engine.dispose()


def _cleanup(engine):
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_offers WHERE offer_id LIKE :p"),
                     {"p": f"{_PREFIX}%"})


def test_alert_sql_executes_with_the_documented_semantics(pg_engine):
    from sqlalchemy import text

    from scripts.audit_offer_currency import _PRICE_ALERT_SQL

    with pg_engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(
            text(_PRICE_ALERT_SQL.replace(":price_alert", ":pa")), {"pa": 1000.0})
            if str(r._mapping["offer_id"]).startswith(_PREFIX)]

    got = [r["offer_id"] for r in rows]
    # ordered by price desc; USD-only; threshold-inclusive scope
    assert got == [f"{_PREFIX}oiad-live", f"{_PREFIX}suppressed-big",
                   f"{_PREFIX}padded-usd"]
    by_id = {r["offer_id"]: r for r in rows}
    # suppressed rows stay VISIBLE, flagged not filtered
    assert by_id[f"{_PREFIX}suppressed-big"]["is_suppressed"] is True
    assert by_id[f"{_PREFIX}oiad-live"]["is_suppressed"] is False
    # KRW magnitude is the sibling check's job; sub-threshold and domain-less
    # rows (the domainless audit's scope) are excluded
    assert f"{_PREFIX}foreign-big" not in got
    assert f"{_PREFIX}below-threshold" not in got
    assert f"{_PREFIX}domainless-big" not in got
