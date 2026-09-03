"""scripts/backfill_stale_pdp_overlays.py repairs the rows a pre-fix rollback left stale.

a9019b1c9 (PR #2030) fixed `rollback_module` so a rollback re-materializes
`merchant_product_overlay`. It did NOT repair rows already left `active` in production
pointing at a version that is now `superseded` — the public PDP is still serving content
an operator revoked. This file drives the backfill script that repairs them.

HOW THE STALE STATE IS BUILT HERE
---------------------------------
Everything up to the bug is driven through the SHIPPED writers over the sqlite test DB
from tests/conftest.py: `create_module_draft` -> `publish_module_version` (v1) ->
`publish_module_version` (v2). The rollback itself cannot be, because the shipped
`rollback_module` no longer has the bug. So `_pre_fix_rollback` below reproduces the OLD
writer's exact effect — the four statements `rollback_module` ran at
`git show a9019b1c9^:services/pdp_governance_service.py` (supersede the live published
row, insert the restored one, write the `module_rolled_back` audit row) and nothing else,
leaving the overlay untouched. It is a reproduction of the pre-fix writer, not an
invention: no column is faked and no DDL is added — the tables come from
`metadata.create_all` on the shipped models plus `ensure_pdp_governance_tables()`.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import database, engine, metadata  # noqa: E402
from db.merchant_product_overlay import merchant_product_overlay  # noqa: E402
from db.pdp_governance import pdp_module_versions  # noqa: E402
from scripts import backfill_stale_pdp_overlays as backfill  # noqa: E402
from services import pdp_governance_service as svc  # noqa: E402

MODULE_KEY = "copy"
FIELD_KEY = "pdp_description_raw"
V1_COPY = "V1 copy: the approved description the operator wants back."
V2_COPY = "V2 copy: the description the operator publishes and then revokes."


@pytest.fixture(autouse=True)
async def _db(monkeypatch):
    metadata.create_all(engine, tables=[merchant_product_overlay], checkfirst=True)
    if not database.is_connected:
        await database.connect()
    await svc.ensure_pdp_governance_tables()
    # The hybrid publish path is flag-gated OFF by default and the flag is read into a
    # module constant at import, so the env var alone would not flip it.
    monkeypatch.setattr(svc, "SKU_OPT_OVERLAY_V1_ENABLED", True)
    yield


async def _subject() -> Dict[str, Any]:
    product_key = f"merch_bf_{uuid.uuid4().hex[:8]}|shopify|{uuid.uuid4().hex[:8]}"
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


async def _pre_fix_rollback(
    *, pdp_id: str, target: Dict[str, Any], write_audit: bool = True
) -> Dict[str, Any]:
    """Reproduce, statement for statement, what `rollback_module` did BEFORE a9019b1c9.

    Source: `git show a9019b1c9^:services/pdp_governance_service.py`, `rollback_module`
    — supersede the live published row, insert the restored one, write the
    `module_rolled_back` audit row, return. It never touched merchant_product_overlay,
    which is the bug this backfill repairs.

    `write_audit=False` drops only the audit row, to build the `unknown`-cause state: a
    write that left no trail for the classifier to read.
    """
    now = svc._now()
    await database.execute(
        pdp_module_versions.update()
        .where(
            (pdp_module_versions.c.pdp_id == pdp_id)
            & (pdp_module_versions.c.module_key == MODULE_KEY)
            & (pdp_module_versions.c.stage == "published")
            & (pdp_module_versions.c.superseded_at.is_(None))
        )
        .values(status="superseded", superseded_at=now)
    )
    rollback_row = {
        "id": f"pdpmod_{uuid.uuid4().hex}",
        "pdp_id": pdp_id,
        "module_key": MODULE_KEY,
        "stage": "published",
        "version": await svc._next_module_version(pdp_id, MODULE_KEY),
        "status": "published",
        "payload": svc._json_dict(target.get("payload")),
        "source_refs": svc._json_list(target.get("source_refs")),
        "review_actor_type": svc.REVIEW_ACTOR_HUMAN,
        "review_actor_id": "emp_senior_1",
        "review_model": None,
        "review_decision": "pass",
        "review_confidence": 1.0,
        "review_rubric": {"rollback_from_version_id": target["id"]},
        "risk_level": target.get("risk_level") or "low",
        "requires_human": bool(target.get("requires_human")),
        "generated_by": target.get("generated_by"),
        "generation_ref": target.get("generation_ref"),
        "created_by_employee_id": "emp_senior_1",
        "created_at": now,
        "published_at": now,
        "superseded_at": None,
    }
    await database.execute(pdp_module_versions.insert().values(**rollback_row))
    if write_audit:
        await svc._audit(
            pdp_id=pdp_id,
            module_key=MODULE_KEY,
            action="module_rolled_back",
            actor_type=svc.REVIEW_ACTOR_HUMAN,
            actor_id="emp_senior_1",
            details={"target_version_id": target["id"], "published_version_id": rollback_row["id"]},
        )
    return rollback_row


async def _overlay_rows(product_key: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        merchant_product_overlay.select().where(
            merchant_product_overlay.c.product_key == product_key
        )
    )
    return sorted((dict(row) for row in rows), key=lambda r: r["overlay_id"])


def _active(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in rows if row["approval_status"] == "active"]


async def _served(product_key: str) -> Dict[str, Any]:
    merchant_id, _, source_product_id = svc.parse_product_key(product_key)
    return await svc.active_overlay_fields_for_product(
        merchant_id=merchant_id,
        source_product_id=source_product_id,
        module_key=MODULE_KEY,
    )


def _args(*extra: str, pdp_id: Optional[str] = None):
    argv = list(extra)
    if pdp_id:
        # Scope to this test's own pdp: the sqlite test DB is shared across test files,
        # so an unscoped scan would see a sibling file's rows too.
        argv += ["--pdp-id", pdp_id]
    return backfill.parse_args(argv)


async def _stale_state() -> Dict[str, Any]:
    """publish v1 -> publish v2 -> PRE-FIX rollback to v1. Overlay left on v2."""
    subject = await _subject()
    pdp_id = subject["pdp_id"]
    product_key = subject["representative_product_key"]
    v1 = await _publish(pdp_id, V1_COPY)
    v2 = await _publish(pdp_id, V2_COPY)
    rollback_row = await _pre_fix_rollback(pdp_id=pdp_id, target=v1)

    rows = await _overlay_rows(product_key)
    active = _active(rows)
    assert len(active) == 1
    assert active[0]["value_jsonb"] == V2_COPY
    assert active[0]["source_version_id"] == v2["id"]
    return {
        "pdp_id": pdp_id,
        "product_key": product_key,
        "v1": v1,
        "v2": v2,
        "rollback_row": rollback_row,
        "stale_overlay_id": active[0]["overlay_id"],
    }


async def test_dry_run_finds_the_rolled_back_row_classifies_it_and_writes_nothing(tmp_path, capsys):
    state = await _stale_state()
    before = await _overlay_rows(state["product_key"])

    exit_code = await backfill._run_cli(
        _args("--report-dir", str(tmp_path), pdp_id=state["pdp_id"])
    )
    assert exit_code == 0

    reports = list(tmp_path.glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())

    assert report["apply"] is False
    assert report["counts"]["stale_rows_found"] == 1
    row = report["rows"][0]
    assert row["overlay_id"] == state["stale_overlay_id"]
    assert row["field_key"] == FIELD_KEY
    assert row["stale_source_version_id"] == state["v2"]["id"]
    assert row["current_published_version_id"] == state["rollback_row"]["id"]
    assert row["cause"] == backfill.CAUSE_ROLLED_BACK
    assert row["cause_evidence"]["action"] == "module_rolled_back"

    group = report["groups"][0]
    assert group["decision"] == backfill.DECISION_REPAIR
    assert group["outcome"] == "dry_run"

    # Wrote nothing.
    assert await _overlay_rows(state["product_key"]) == before
    assert await _served(state["product_key"]) == {FIELD_KEY: V2_COPY}

    out = capsys.readouterr().out
    assert "DRY RUN (writes nothing)" in out
    assert backfill.FLAG_NAME in out


async def test_an_unscoped_scan_sees_the_stale_row_and_leaves_a_healthy_one_alone():
    """The --pdp-id scoping used elsewhere in this file is for determinism on a shared
    test DB, not the thing under test: the stale query must find the row on its own.

    And it must find ONLY stale rows. The healthy pdp below has an `active` overlay row
    whose source version is the CURRENT published one — the normal state of every row in
    the table. Selecting it would make the backfill re-materialize the whole table."""
    state = await _stale_state()
    healthy = await _subject()
    await _publish(healthy["pdp_id"], V1_COPY)
    healthy_active = _active(await _overlay_rows(healthy["representative_product_key"]))
    assert len(healthy_active) == 1

    report = await backfill.run(_args())
    found = {row["overlay_id"] for row in report["rows"]}
    assert state["stale_overlay_id"] in found
    assert healthy_active[0]["overlay_id"] not in found


async def test_apply_repairs_through_the_shipped_materialize_and_is_idempotent():
    state = await _stale_state()

    report = await backfill.run(_args("--apply", pdp_id=state["pdp_id"]))
    assert report["counts"]["applied"] == 1
    assert report["counts"]["refused_by_guardrails"] == 0
    assert report["counts"]["errored"] == 0
    assert report["groups"][0]["outcome"] == "repaired"

    rows = await _overlay_rows(state["product_key"])
    by_version = {row["source_version_id"]: row for row in rows}
    # v2's row — the one the public PDP was still serving — is superseded.
    assert by_version[state["v2"]["id"]]["approval_status"] == "superseded"
    assert by_version[state["v1"]["id"]]["approval_status"] == "superseded"
    # The restored content is active, on a row the SHIPPED flattener wrote for the
    # current published (rollback) version.
    active = _active(rows)
    assert len(active) == 1
    assert active[0]["source_version_id"] == state["rollback_row"]["id"]
    assert active[0]["value_jsonb"] == V1_COPY
    # Provenance comes from the current published version's own reviewer (a senior
    # employee), exactly as the fixed rollback_module writes it.
    assert active[0]["provenance"] == "ops_approved"

    # What the public PDP merge hook would serve.
    assert await _served(state["product_key"]) == {FIELD_KEY: V1_COPY}

    # Idempotent: a second --apply finds nothing to do.
    after_first = await _overlay_rows(state["product_key"])
    second = await backfill.run(_args("--apply", pdp_id=state["pdp_id"]))
    assert second["counts"]["stale_rows_found"] == 0
    assert second["counts"]["applied"] == 0
    assert await _overlay_rows(state["product_key"]) == after_first


async def test_a_publish_whose_materialization_failed_is_classified_and_repaired(monkeypatch):
    """Publish's overlay write is best-effort (`except Exception` in
    publish_module_version), so a failure there publishes the version and leaves the
    overlay on the PREVIOUS one — the same stale query, a different cause."""
    subject = await _subject()
    pdp_id = subject["pdp_id"]
    product_key = subject["representative_product_key"]
    v1 = await _publish(pdp_id, V1_COPY)

    async def _boom(**_kwargs):
        raise RuntimeError("overlay write failed")

    real = svc.materialize_overlay_from_module
    monkeypatch.setattr(svc, "materialize_overlay_from_module", _boom)
    v2 = await _publish(pdp_id, V2_COPY)
    monkeypatch.setattr(svc, "materialize_overlay_from_module", real)

    active = _active(await _overlay_rows(product_key))
    assert len(active) == 1
    assert active[0]["source_version_id"] == v1["id"]
    assert await _served(product_key) == {FIELD_KEY: V1_COPY}

    report = await backfill.run(_args(pdp_id=pdp_id))
    assert report["counts"]["stale_rows_found"] == 1
    row = report["rows"][0]
    assert row["cause"] == backfill.CAUSE_PUBLISH_FAILED
    assert row["cause_evidence"]["action"] == "module_published"
    assert row["cause_evidence"]["published_version_id"] == v2["id"]
    assert report["groups"][0]["decision"] == backfill.DECISION_REPAIR

    applied = await backfill.run(_args("--apply", pdp_id=pdp_id))
    assert applied["counts"]["applied"] == 1
    active = _active(await _overlay_rows(product_key))
    assert len(active) == 1
    assert active[0]["source_version_id"] == v2["id"]
    assert active[0]["value_jsonb"] == V2_COPY
    assert await _served(product_key) == {FIELD_KEY: V2_COPY}


async def test_no_current_published_version_is_reported_and_never_written():
    """With no live published version, the correct end state may be NO active overlay row
    (a withdrawal) rather than a re-materialization. The script must say so, not guess."""
    subject = await _subject()
    pdp_id = subject["pdp_id"]
    product_key = subject["representative_product_key"]
    await _publish(pdp_id, V1_COPY)
    v2 = await _publish(pdp_id, V2_COPY)
    # Withdraw the module: supersede the live published row without publishing another.
    await database.execute(
        pdp_module_versions.update()
        .where(
            (pdp_module_versions.c.pdp_id == pdp_id)
            & (pdp_module_versions.c.module_key == MODULE_KEY)
            & (pdp_module_versions.c.stage == "published")
            & (pdp_module_versions.c.superseded_at.is_(None))
        )
        .values(status="superseded", superseded_at=svc._now())
    )
    before = await _overlay_rows(product_key)

    report = await backfill.run(_args("--apply", "--include-unknown", pdp_id=pdp_id))
    assert report["counts"]["stale_rows_found"] == 1
    assert report["rows"][0]["stale_source_version_id"] == v2["id"]
    group = report["groups"][0]
    assert group["decision"] == backfill.DECISION_SKIP_NO_CURRENT
    assert group["current_published_version_id"] is None
    assert group["outcome"] == "skipped"
    assert report["counts"]["applied"] == 0
    # --apply AND --include-unknown together still wrote nothing here.
    assert await _overlay_rows(product_key) == before


async def test_a_repair_that_fails_part_way_through_a_module_leaves_the_overlay_untouched(
    monkeypatch,
):
    """One transaction per (pdp_id, module_key). `materialize_overlay_from_module` makes
    each FIELD's supersede+insert atomic on its own; the repair's transaction is what
    stops a module's fields from ending up split across two source versions when a later
    field fails — the state no write path can produce and no report describes.

    Fail-injected through the shipped `_OVERLAY_FIELD_MAP`, which the module is designed
    to grow ("Add modules to _OVERLAY_FIELD_MAP to widen coverage"): a second field whose
    extractor raises, after the first field's inner transaction has already committed.
    """
    state = await _stale_state()
    before = await _overlay_rows(state["product_key"])

    def _boom(_payload):
        raise RuntimeError("injected second-field failure")

    monkeypatch.setitem(
        svc._OVERLAY_FIELD_MAP,
        MODULE_KEY,
        [(FIELD_KEY, svc._overlay_copy_description), ("pdp_second_field", _boom)],
    )
    report = await backfill.run(_args("--apply", pdp_id=state["pdp_id"]))
    monkeypatch.undo()

    assert report["counts"]["errored"] == 1
    assert report["counts"]["applied"] == 0
    assert "injected second-field failure" in report["groups"][0]["error"]
    # The first field's supersede+insert was rolled back with the failure.
    assert await _overlay_rows(state["product_key"]) == before
    assert await _served(state["product_key"]) == {FIELD_KEY: V2_COPY}


async def test_a_guardrail_refusal_is_reported_not_silently_skipped(tmp_path, monkeypatch, capsys):
    """The repair re-runs the APPLY guardrails, the way publish and (since a9019b1c9)
    rollback do. A payload the config no longer allows is refused — and the refusal is
    reported, leaves the stale row untouched, and makes the run exit non-zero so a Cloud
    Run execution goes red instead of reporting a quiet success."""
    state = await _stale_state()
    before = await _overlay_rows(state["product_key"])

    # Tighten the field-size ceiling below the payload being re-materialized.
    # current_config() reads the env fresh on every call.
    monkeypatch.setenv("MERCHANT_WRITE_MAX_FIELD_CHARS", str(len(V1_COPY) - 1))
    exit_code = await backfill._run_cli(
        _args("--apply", "--report-dir", str(tmp_path), pdp_id=state["pdp_id"])
    )
    assert exit_code == 1

    report = json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
    group = report["groups"][0]
    assert group["outcome"] == "refused_by_guardrails"
    assert group["error"]
    assert report["counts"]["refused_by_guardrails"] == 1
    assert report["counts"]["applied"] == 0
    assert await _overlay_rows(state["product_key"]) == before
    assert await _served(state["product_key"]) == {FIELD_KEY: V2_COPY}
    assert "REFUSED BY GUARDRAILS" in capsys.readouterr().out


async def test_an_unknown_cause_is_repaired_only_with_include_unknown():
    """No audit row for the write that superseded the overlay's source version, so
    neither story is supported by evidence. `unknown` must not be repaired by default."""
    subject = await _subject()
    pdp_id = subject["pdp_id"]
    product_key = subject["representative_product_key"]
    v1 = await _publish(pdp_id, V1_COPY)
    await _publish(pdp_id, V2_COPY)
    rollback_row = await _pre_fix_rollback(pdp_id=pdp_id, target=v1, write_audit=False)
    before = await _overlay_rows(product_key)

    report = await backfill.run(_args("--apply", pdp_id=pdp_id))
    assert report["rows"][0]["cause"] == backfill.CAUSE_UNKNOWN
    assert report["groups"][0]["decision"] == backfill.DECISION_SKIP_UNKNOWN
    assert report["counts"]["applied"] == 0
    assert await _overlay_rows(product_key) == before

    opted_in = await backfill.run(_args("--apply", "--include-unknown", pdp_id=pdp_id))
    assert opted_in["groups"][0]["decision"] == backfill.DECISION_REPAIR
    assert opted_in["counts"]["applied"] == 1
    active = _active(await _overlay_rows(product_key))
    assert len(active) == 1
    assert active[0]["source_version_id"] == rollback_row["id"]
    assert await _served(product_key) == {FIELD_KEY: V1_COPY}
