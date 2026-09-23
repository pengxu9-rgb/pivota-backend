"""A category RE-FILE records itself as a batch, and never as a new label source.

Two backfills (the non-face-leaf repair #2248/#2253 and the acid-pad repair #2254) rewrote
category_path on rows that already had one and left NO trace: the row's own
`category_label_source` names the ORIGIN of the label by migration 069's contract, and
`enrichment_agent_v1` also decides pdp_scope, so it cannot be re-stamped. The batch is the
provenance record.
"""
import json

import pytest

from services import category_refile_audit as refile
from services.catalog_offer_writer_guard import WriterAuditAccumulator

MOVES = [
    {"product_key": "k1", "from": "beauty/skincare/tone/toner", "to": "beauty/skincare/treat/exfoliant"},
    {"product_key": "k2", "from": "beauty/skincare/tone/toner", "to": "beauty/skincare/treat/exfoliant"},
    {"product_key": "k3", "from": "beauty/makeup", "to": "beauty/skincare/treat/exfoliant"},
    {"product_key": "k4", "from": "beauty/skincare/treat/serum", "to": None},
]


def build(**kw):
    base = dict(writer_name="category_refile_test", batch_id="b1", rule="a rule", moves=MOVES)
    base.update(kw)
    return refile.build_refile_audit(**base)


def test_the_batch_counts_what_landed_and_groups_the_transitions():
    audit = build(manifest_sha="deadbeef", skipped=2)
    assert (audit.applied_rows, audit.skipped_rows) == (4, 2)
    assert audit.dry_run_report_hash == "deadbeef"
    assert audit.reasons["moves"] == {
        "beauty/skincare/tone/toner -> beauty/skincare/treat/exfoliant": 2,
        "beauty/makeup -> beauty/skincare/treat/exfoliant": 1,
        "beauty/skincare/treat/serum -> NULL": 1,
    }
    assert audit.reasons["not_at_target"] == 2


def test_every_row_is_named_so_the_batch_can_be_reversed():
    rows = build().reasons["rows"]
    assert [r["product_key"] for r in rows] == ["k1", "k2", "k3", "k4"]
    assert rows[3] == {"product_key": "k4", "from": "beauty/skincare/treat/serum", "to": None}


def test_the_payload_is_jsonb_serialisable():
    json.dumps(build().reasons)


def test_the_detail_list_is_capped_but_the_counts_are_not(monkeypatch):
    monkeypatch.setattr(refile, "MAX_DETAIL_ROWS", 2)
    audit = build()
    assert audit.applied_rows == 4
    assert len(audit.reasons["rows"]) == 2
    assert audit.reasons["rows_truncated"] is True


def test_the_batch_states_that_the_origin_stamp_was_left_alone():
    """The claim a reader of writer_audit_log needs: this row explains a path change, and the
    row's own category_label_source still names whoever wrote the ORIGINAL label."""
    assert build().reasons["category_label_source_left_as_origin"] is True


@pytest.mark.asyncio
async def test_an_empty_refile_is_not_an_event():
    calls = []
    assert await refile.record_category_refile(
        writer_name="w", batch_id="b", rule="r", moves=[], db=calls) is None
    assert calls == []


@pytest.mark.asyncio
async def test_a_failed_audit_row_never_raises_over_rows_that_are_already_written():
    class Boom:
        async def execute(self, *a, **kw):
            raise RuntimeError("audit table gone")

    assert await refile.record_category_refile(
        writer_name="w", batch_id="b", rule="r", moves=MOVES, db=Boom()) is None


@pytest.mark.asyncio
async def test_a_landed_batch_writes_one_row_through_the_shared_writer():
    seen = {}

    class Fake:
        async def execute(self, sql, params):
            seen["sql"], seen["params"] = sql, params

    got = await refile.record_category_refile(
        writer_name="category_refile_test", batch_id="b7", rule="a rule",
        moves=MOVES, manifest_sha="sha7", actor="tester", db=Fake())
    assert got == "b7"
    assert "INSERT INTO writer_audit_log" in seen["sql"]
    assert seen["params"]["batch_id"] == "b7"
    assert seen["params"]["applied_rows"] == 4
    assert seen["params"]["actor"] == "tester"
    # The stamp column is not in this write at all.
    assert "category_label_source" not in seen["sql"]
    assert json.loads(seen["params"]["reasons"])["rule"] == "a rule"


def test_the_helper_reuses_the_offer_writers_accumulator():
    """One audit rail, one spelling: a second INSERT of its own would drift from migration 132."""
    assert isinstance(build(), WriterAuditAccumulator)
