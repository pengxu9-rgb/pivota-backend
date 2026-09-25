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
    {"product_key": "k5", "from": "", "to": "beauty/skincare/treat/exfoliant"},   # '' is not NULL
]


class Spy:
    """An async db double. A LIST is not one: `[].execute(...)` raises AttributeError, which the
    best-effort except swallows, so a removed guard would look like a guard that worked."""

    def __init__(self):
        self.calls = []

    async def execute(self, sql, params):
        self.calls.append((sql, params))


def build(**kw):
    base = dict(writer_name="category_refile_test", batch_id="b1", rule="a rule", moves=MOVES)
    base.update(kw)
    return refile.build_refile_audit(**base)


def test_the_batch_counts_what_landed_and_groups_the_transitions():
    audit = build(manifest_sha="deadbeef", skipped=2)
    assert (audit.applied_rows, audit.skipped_rows) == (5, 2)
    assert audit.dry_run_report_hash == "deadbeef"
    assert audit.reasons["moves"] == {
        "beauty/skincare/tone/toner -> beauty/skincare/treat/exfoliant": 2,
        "beauty/makeup -> beauty/skincare/treat/exfoliant": 1,
        "beauty/skincare/treat/serum -> NULL": 1,
        # An empty origin is grouped as NULL, and the detail row below keeps the raw '' it had.
        "NULL -> beauty/skincare/treat/exfoliant": 1,
    }
    assert audit.reasons["no_longer_at_origin"] == 2


def test_every_row_is_named_so_the_batch_can_be_reversed():
    rows = build().reasons["rows"]
    assert [r["product_key"] for r in rows] == ["k1", "k2", "k3", "k4", "k5"]
    assert rows[3] == {"product_key": "k4", "from": "beauty/skincare/treat/serum", "to": None}
    assert rows[4]["from"] == ""   # the raw origin, not the grouped "NULL"


def test_the_payload_is_jsonb_serialisable():
    json.dumps(build().reasons)


def test_the_detail_list_is_capped_but_the_counts_are_not(monkeypatch):
    monkeypatch.setattr(refile, "MAX_DETAIL_ROWS", 2)
    audit = build()
    assert audit.applied_rows == 5
    assert len(audit.reasons["rows"]) == 2
    assert audit.reasons["rows_truncated"] is True


def test_the_batch_states_that_the_origin_stamp_was_left_alone():
    """The claim a reader of writer_audit_log needs: this row explains a path change, and the
    row's own category_label_source still names whoever wrote the ORIGINAL label."""
    assert build().reasons["category_label_source_left_as_origin"] is True


@pytest.mark.asyncio
async def test_an_empty_refile_is_not_an_event():
    spy = Spy()
    assert await refile.record_category_refile(
        writer_name="w", batch_id="b", rule="r", moves=[], db=spy) is None
    assert spy.calls == []


@pytest.mark.asyncio
async def test_a_malformed_moves_list_raises_rather_than_losing_the_provenance_silently():
    """A caller bug must be loud: a repair whose rows are written and whose batch can never be
    reconstructed is worse than a crash that names the bug."""
    spy = Spy()
    with pytest.raises((TypeError, KeyError)):
        await refile.record_category_refile(
            writer_name="w", batch_id="b", rule="r", moves=[{"from": "a", "to": "b"}], db=spy)
    assert spy.calls == []


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
    assert seen["params"]["applied_rows"] == 5
    assert seen["params"]["actor"] == "tester"
    # The stamp column is not in this write at all.
    assert "category_label_source" not in seen["sql"]
    assert json.loads(seen["params"]["reasons"])["rule"] == "a rule"


def test_the_helper_reuses_the_offer_writers_accumulator():
    """One audit rail, one spelling: a second INSERT of its own would drift from migration 132."""
    assert isinstance(build(), WriterAuditAccumulator)


def _refile_script_source() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[2] / "scripts"
            / "standardize_off_taxonomy_category_paths.py").read_text(encoding="utf-8")


def test_the_refile_script_updates_the_path_and_nothing_else():
    """The claim this whole module rests on, checked where it is actually made. The audit row is a
    RECORD of a re-file; it is not permission to also re-stamp the row's origin. Asserted against
    the script's SQL text because the statement is inline in its apply path -- a test that only
    inspected the audit INSERT could never fail, since that INSERT never named the column."""
    source = _refile_script_source()
    update = source[source.index("UPDATE catalog_products"):]
    update = update[:update.index("RETURNING")]
    assert "SET category_path = :target" in update
    for column in ("category_label_source", "category_confidence", "category_label"):
        assert column not in update, column


def test_the_refile_script_records_its_batch():
    """A merged helper with no caller is not a mechanism. Checked on the syntax TREE, not the text:
    `report["audit_written"] = False and bool(await record_category_refile(...))` keeps every
    string a grep would look for while never recording anything."""
    import ast

    tree = ast.parse(_refile_script_source())
    awaited_calls = {
        ast.unparse(node.value.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    }
    assert "record_category_refile" in awaited_calls

    written = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Subscript) and getattr(t.slice, "value", None) == "audit_written"
                for t in node.targets)
    ]
    assert len(written) == 1, "the apply path must record exactly one audit outcome"
    value = written[0].value
    # bool(await record_category_refile(...)) — no BoolOp, no constant, no other callee between.
    assert isinstance(value, ast.Call) and ast.unparse(value.func) == "bool"
    inner = value.args[0]
    assert isinstance(inner, ast.Await)
    assert ast.unparse(inner.value.func) == "record_category_refile"
