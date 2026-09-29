"""The enrichment cart-proof writer's SQL on the PRODUCTION dialect.

    DATABASE_URL=postgresql://<user>@localhost:5432/pivota_<name>_dialect_check \\
        .venv/bin/python -m pytest tests/test_enrichment_cart_variant_proof_job_postgres.py

WHAT ONLY POSTGRES CAN SHOW:
  * the selection (jobs/enrichment_cart_variant_proof.SELECT_TARGETS_SQL) over JSONB sku_payload,
    the `substr` variant count, the LEFT JOIN and the cursor, on the real catalog DDL;
  * the upsert against migration 248's real PRIMARY KEY and CHECKs (`ok` must carry its
    evidence), TIMESTAMPTZ ordering in the older-never-overwrites guard, and RETURNING under
    `ON CONFLICT ... DO UPDATE ... WHERE` yielding NO row when the guard refuses.

Every database case of tests/test_enrichment_cart_variant_proof_job.py is imported below, so the
dialect gate (which collects only `*_postgres.py`) runs them on Postgres too. The gate shares ONE
database across files: this module creates the catalog tables from the models (never a migration)
and deletes only its own rows, by the `ext:ecvpjob-` key prefix.

THIS MODULE MUST NOT IMPORT `main` (see tests/test_merchant_purchasability_postgres.py for why).
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
_DBNAME = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
_THROWAWAY = any(marker in _DBNAME for marker in ("dialect_check", "_test", "test_"))

pytestmark = pytest.mark.skipif(
    not (_IS_PG and _THROWAWAY),
    reason=(
        "needs a Postgres DATABASE_URL naming a THROWAWAY database (…dialect_check…, …_test, "
        "test_…) — this is the production-dialect gate; see the module docstring"
    ),
)

if _IS_PG and _THROWAWAY:
    from tests.test_enrichment_cart_variant_proof_job import *  # noqa: F401,F403,E402
    # A star-import skips underscore names; the fixture is resolved by NAME in this module.
    from tests.test_enrichment_cart_variant_proof_job import (  # noqa: F401,E402
        CHECKED,
        RUN_TARTE,
        TARTE_HOST,
        job_db,
        proofs_by_sku,
        sku_of,
    )


async def test_an_ok_row_without_its_evidence_is_refused_by_the_table(job_db):
    """The writer never builds one, and the table refuses one anyway (migration 248's CHECK)."""
    import jobs.enrichment_cart_variant_proof as job

    await insert_rows(job_db, RUN_TARTE)  # noqa: F405
    row = job.ProofRow(product_key=RUN_TARTE["product"]["product_key"],
                       sku_key=sku_of(RUN_TARTE, "::v:63530896818545")["sku_key"], shop_host=TARTE_HOST,
                       handle="amazonian-clay-baked-blush", source=job.SOURCE_PRODUCTS_JS, checked_at=CHECKED,
                       outcome="ok", variant_id="63530896818545", live_variant_count=3, available=True,
                       live_price_minor=None, currency="USD")
    with pytest.raises(Exception, match="ck_enrichment_cart_variant_proofs_ok_has_evidence"):
        await job.upsert_proof(job_db, row, written_at=CHECKED)


async def test_timestamps_are_stored_aware_and_the_guard_compares_instants(job_db):
    """An equal instant spelled in another zone is not "older": the guard compares TIMESTAMPTZ."""
    from datetime import timezone as tz

    import jobs.enrichment_cart_variant_proof as job

    await insert_rows(job_db, RUN_TARTE)  # noqa: F405
    base = job.ProofRow(product_key=RUN_TARTE["product"]["product_key"],
                        sku_key=sku_of(RUN_TARTE, "::v:63530896818545")["sku_key"], shop_host=TARTE_HOST,
                        handle="amazonian-clay-baked-blush", source=job.SOURCE_PRODUCTS_JS, checked_at=CHECKED,
                        outcome=job.VARIANT_GONE, live_variant_count=3)
    assert await job.upsert_proof(job_db, base, written_at=CHECKED)
    plus8 = tz(timedelta(hours=8))
    later_in_sg = job.ProofRow(**{**base.__dict__, "checked_at": (CHECKED + timedelta(minutes=1)).astimezone(plus8),
                                  "outcome": job.REVOKED_404})
    assert await job.upsert_proof(job_db, later_in_sg, written_at=CHECKED)
    earlier_in_sg = job.ProofRow(**{**base.__dict__, "checked_at": CHECKED.astimezone(plus8),
                                    "outcome": job.HANDLE_MISMATCH})
    assert not await job.upsert_proof(job_db, earlier_in_sg, written_at=CHECKED)
    stored = (await proofs_by_sku(job_db))[base.sku_key]
    assert stored["outcome"] == job.REVOKED_404
    assert stored["checked_at"].utcoffset() is not None
    assert stored["checked_at"] == CHECKED + timedelta(minutes=1)
