"""A PDP governance rollback must move the overlay the public PDP actually serves.

Publishing a governance module flattens its approved payload into
`merchant_product_overlay` (services/pdp_governance_service.materialize_overlay_from_module),
and the public PDP merge hook serves whatever row is `active` for
(product_key, module_key, field_key). Before this file's fix, `rollback_module`
rewrote `pdp_module_versions` only: the rolled-back version's overlay row stayed
`active`, so publish v1 -> publish v2 -> rollback-to-v1 left the live PDP serving
v2 while the version history said v1. That is the regression pinned here.

Everything is driven through the SHIPPED service functions (`create_module_draft`,
`publish_module_version`, `rollback_module`) over the sqlite test DB from conftest.
The overlay table is built by `metadata.create_all` from db/merchant_product_overlay.py
-- the MODEL, not migration 143 -- so the columns these tests depend on are asserted
against the migration explicitly (see the first test).

NOT covered here: the actual serving read, which is not in this repo. It is
PIVOTA-Agent `src/services/catalogPdpContentFields.js`
`readMerchantProductOverlayByProductRefs`. Its selection predicate is reproduced by
`active_overlay_fields_for_product`, which these tests call for the "what does the
PDP serve" assertion.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import database, engine, metadata  # noqa: E402
from db.merchant_product_overlay import merchant_product_overlay  # noqa: E402
from services import pdp_governance_service as svc  # noqa: E402
from services.merchant_write_guardrails import GuardrailViolation  # noqa: E402

MODULE_KEY = "copy"
FIELD_KEY = "pdp_description_raw"
V1_COPY = "V1 copy: the approved description the operator wants back."
V2_COPY = "V2 copy: the description the operator publishes and then revokes."


@pytest.fixture(autouse=True)
async def _db(monkeypatch):
    # Built from the MODEL (db/merchant_product_overlay.py). checkfirst=True so a
    # pivota_test.db an earlier test file already created is reused as-is.
    metadata.create_all(engine, tables=[merchant_product_overlay], checkfirst=True)
    if not database.is_connected:
        await database.connect()
    await svc.ensure_pdp_governance_tables()
    # The hybrid publish path is flag-gated and OFF by default. The flag is read into
    # a module constant at import, so the env var alone would not flip it.
    monkeypatch.setattr(svc, "SKU_OPT_OVERLAY_V1_ENABLED", True)
    yield


async def _subject() -> Dict[str, Any]:
    """A PDP subject on its own product_key, so rows never collide with a sibling
    test in the shared sqlite file."""
    product_key = f"merch_rb_{uuid.uuid4().hex[:8]}|shopify|{uuid.uuid4().hex[:8]}"
    subject = await svc.resolve_pdp_subject(product_key=product_key)
    assert subject["representative_product_key"] == product_key
    return subject


async def _publish(pdp_id: str, text: str) -> Dict[str, Any]:
    draft = await svc.create_module_draft(
        pdp_id=pdp_id,
        module_key=MODULE_KEY,
        payload={FIELD_KEY: text},
        actor_type=svc.REVIEW_ACTOR_HUMAN,
        actor_id="emp_senior_1",
        actor_role="senior_employee",
    )
    return await svc.publish_module_version(
        pdp_id=pdp_id,
        module_key=MODULE_KEY,
        version_id=draft["id"],
        actor_type=svc.REVIEW_ACTOR_HUMAN,
        actor_id="emp_senior_1",
    )


async def _overlay_rows(product_key: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        merchant_product_overlay.select().where(
            merchant_product_overlay.c.product_key == product_key
        )
    )
    return [dict(row) for row in rows]


def _active(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in rows if row["approval_status"] == "active"]


async def _served(product_key: str) -> Dict[str, Any]:
    merchant_id, _, source_product_id = svc.parse_product_key(product_key)
    return await svc.active_overlay_fields_for_product(
        merchant_id=merchant_id,
        source_product_id=source_product_id,
        module_key=MODULE_KEY,
    )


def test_overlay_columns_the_rollback_depends_on_are_declared_and_match_migration_143():
    """`create_all` builds merchant_product_overlay from the MODEL on the test DB, so a
    model/migration drift on these columns would make every assertion below green and
    wrong in production. Assert both declarations name the same columns."""
    model_columns = {column.name for column in merchant_product_overlay.columns}
    required = {
        "product_key",
        "module_key",
        "field_key",
        "value_jsonb",
        "provenance",
        "source_version_id",
        "approval_status",
        "approved_by",
        "approved_at",
    }
    assert required <= model_columns

    migration = (
        Path(__file__).resolve().parents[1]
        / "db"
        / "migrations"
        / "143_merchant_product_overlay.sql"
    ).read_text(encoding="utf-8")
    create_table = re.search(
        r"CREATE TABLE IF NOT EXISTS merchant_product_overlay\s*\((.*?)\n\);",
        migration,
        re.S,
    )
    assert create_table, "migration 143 no longer declares the overlay table"
    declared = {
        match.group(1)
        for match in re.finditer(r"^\s{4}(\w+)\s+\w", create_table.group(1), re.M)
    }
    assert required <= declared

    # And the table actually built on the test DB carries them.
    from sqlalchemy import inspect as sa_inspect

    built = {col["name"] for col in sa_inspect(engine).get_columns("merchant_product_overlay")}
    assert required <= built

    # The status vocabulary this file asserts on is the migration's, not one invented
    # here: 'active' is the default and the merge hook's filter.
    assert re.search(r"approval_status\s+TEXT\s+NOT NULL\s+DEFAULT\s+'active'", migration)
    assert re.search(r"WHERE\s+approval_status\s*=\s*'active'", migration)


async def test_rollback_supersedes_the_rolled_back_overlay_row_and_reactivates_the_restored_one():
    """THE REGRESSION. Fails on origin/main: the v2 overlay row stays active, so the
    public PDP keeps serving the content the rollback just revoked."""
    subject = await _subject()
    pdp_id = subject["pdp_id"]
    product_key = subject["representative_product_key"]

    v1 = await _publish(pdp_id, V1_COPY)
    v2 = await _publish(pdp_id, V2_COPY)

    rollback = await svc.rollback_module(
        pdp_id=pdp_id,
        module_key=MODULE_KEY,
        target_version_id=v1["id"],
        actor_type=svc.REVIEW_ACTOR_HUMAN,
        actor_id="emp_senior_1",
        actor_role="senior_employee",
    )

    rows = await _overlay_rows(product_key)
    active = _active(rows)

    # 1. The rows: exactly one active, and it is NOT the rolled-back version's.
    assert len(active) == 1, f"expected one active overlay row, got {active}"
    assert active[0]["value_jsonb"] == V1_COPY
    assert active[0]["source_version_id"] == rollback["id"]
    superseded_sources = {
        row["source_version_id"] for row in rows if row["approval_status"] == "superseded"
    }
    assert v2["id"] in superseded_sources, "the rolled-back version's overlay row is still live"
    assert v1["id"] in superseded_sources

    # 2. Provenance: the rollback actor, from the existing vocabulary, with a timestamp.
    assert active[0]["provenance"] == svc._OVERLAY_PROVENANCE_BY_ACTOR[svc.REVIEW_ACTOR_HUMAN]
    assert active[0]["approved_by"] == "emp_senior_1"
    assert active[0]["approved_at"] is not None

    # 3. What the public PDP merge hook serves.
    assert await _served(product_key) == {FIELD_KEY: V1_COPY}


async def test_publish_without_rollback_supersedes_v1_and_serves_v2():
    """The positive counterpart: the fix must not make a plain re-publish stop working
    -- v2 stays live and v1 is superseded, with no rollback involved."""
    subject = await _subject()
    pdp_id = subject["pdp_id"]
    product_key = subject["representative_product_key"]

    v1 = await _publish(pdp_id, V1_COPY)
    v2 = await _publish(pdp_id, V2_COPY)

    rows = await _overlay_rows(product_key)
    active = _active(rows)
    assert len(active) == 1
    assert active[0]["value_jsonb"] == V2_COPY
    assert active[0]["source_version_id"] == v2["id"]
    assert [row["approval_status"] for row in rows if row["source_version_id"] == v1["id"]] == [
        "superseded"
    ]
    assert await _served(product_key) == {FIELD_KEY: V2_COPY}


class _FailOnOverlayInsert:
    """Fault injector: the real `databases.Database`, except that the overlay INSERT
    raises -- i.e. the failure lands AFTER the supersede UPDATE and BEFORE the restored
    row exists. Every other attribute IS the real object's (`__getattr__` delegates), so
    this invents no fields and no return shapes."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.blocked = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        if "INSERT INTO merchant_product_overlay" in str(query):
            self.blocked += 1
            raise RuntimeError("injected overlay insert failure")
        return await self._real.execute(query, *args, **kwargs)


async def test_rollback_that_fails_between_deactivate_and_restore_leaves_everything_unchanged(
    monkeypatch,
):
    """Atomicity. A failure after the deactivate and before the restore must leave the
    overlay exactly as it was -- and must not leave the version history rolled back
    while the PDP still serves the revoked copy."""
    subject = await _subject()
    pdp_id = subject["pdp_id"]
    product_key = subject["representative_product_key"]

    v1 = await _publish(pdp_id, V1_COPY)
    v2 = await _publish(pdp_id, V2_COPY)
    before = sorted(
        (row["source_version_id"], row["approval_status"], row["value_jsonb"])
        for row in await _overlay_rows(product_key)
    )

    injector = _FailOnOverlayInsert(svc.database)
    monkeypatch.setattr(svc, "database", injector)
    with pytest.raises(RuntimeError, match="injected overlay insert failure"):
        await svc.rollback_module(
            pdp_id=pdp_id,
            module_key=MODULE_KEY,
            target_version_id=v1["id"],
            actor_type=svc.REVIEW_ACTOR_HUMAN,
            actor_id="emp_senior_1",
            actor_role="senior_employee",
        )
    monkeypatch.undo()
    assert injector.blocked >= 1, "the injector never saw the overlay insert"

    after = sorted(
        (row["source_version_id"], row["approval_status"], row["value_jsonb"])
        for row in await _overlay_rows(product_key)
    )
    assert after == before, "the supersede was not rolled back with the failed insert"
    assert await _served(product_key) == {FIELD_KEY: V2_COPY}

    # The version history must not have moved either: a committed version rollback with
    # an un-moved overlay is the exact divergence this transaction exists to prevent.
    published = await svc._current_published_version(pdp_id, MODULE_KEY)
    assert published is not None
    assert published["id"] == v2["id"]


async def test_rollback_re_checks_apply_guardrails_against_the_config_in_force_now(
    monkeypatch,
):
    """A rollback re-applies a payload to merchant-visible state, so it re-runs the
    APPLY guardrails against the config in force at rollback time -- the same rule
    publish_module_version follows, and the blueprint's rule for apply. A payload that
    is no longer allowed does not become live again just because it was approved once,
    and the refusal leaves the overlay untouched."""
    subject = await _subject()
    pdp_id = subject["pdp_id"]
    product_key = subject["representative_product_key"]

    v1 = await _publish(pdp_id, V1_COPY)
    await _publish(pdp_id, V2_COPY)
    before = sorted(
        (row["source_version_id"], row["approval_status"]) for row in await _overlay_rows(product_key)
    )

    # Tighten the field-size ceiling below the restored payload. current_config() reads
    # the env fresh on every call, which is what "config in force at apply time" means.
    monkeypatch.setenv("MERCHANT_WRITE_MAX_FIELD_CHARS", str(len(V1_COPY) - 1))
    with pytest.raises(GuardrailViolation):
        await svc.rollback_module(
            pdp_id=pdp_id,
            module_key=MODULE_KEY,
            target_version_id=v1["id"],
            actor_type=svc.REVIEW_ACTOR_HUMAN,
            actor_id="emp_senior_1",
            actor_role="senior_employee",
        )

    after = sorted(
        (row["source_version_id"], row["approval_status"]) for row in await _overlay_rows(product_key)
    )
    assert after == before
    assert await _served(product_key) == {FIELD_KEY: V2_COPY}
