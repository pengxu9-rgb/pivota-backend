"""Phase 2a: the general evidence store + its read-wirings (UNION into the
agent_pdp_view assembler + the readiness evidence tier). Pure merge helpers are
unit-tested; the DB reads are monkeypatched."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.product_evidence as pe
import services.agent_pdp_view_assembler as asm
import services.merchant_audit_readiness as mar


class _FakeDB:
    def __init__(self, one=None, all_=None):
        self._one = one
        self._all = all_ or []

    async def fetch_one(self, *a, **k):
        return self._one

    async def fetch_all(self, *a, **k):
        return self._all


# --- merge_evidence_profiles (pure) ------------------------------------------

def test_merge_dedupes_by_claim_and_source_case_insensitive() -> None:
    a = {"claims": [{"claim_text": "X", "source_ref": "s1"}], "review_state": "reviewed"}
    b = {"claims": [{"claim_text": "x", "source_ref": "S1"}, {"claim_text": "Y", "source_ref": "s2"}],
         "review_state": "observed"}
    m = pe.merge_evidence_profiles(a, b)
    assert [c["claim_text"] for c in m["claims"]] == ["X", "Y"]  # x/S1 deduped
    assert m["review_state"] == "reviewed"  # first profile wins


def test_merge_none_and_empty_returns_none() -> None:
    assert pe.merge_evidence_profiles(None, {"claims": []}) is None
    assert pe.merge_evidence_profiles() is None


def test_merge_tolerates_json_string_input() -> None:
    s = json.dumps({"claims": [{"claim_text": "Z", "source_ref": "s"}], "review_state": "observed"})
    m = pe.merge_evidence_profiles(s)
    assert m["claims"][0]["claim_text"] == "Z"


def test_merge_disclaimers_dedup_by_code() -> None:
    out = pe.merge_disclaimer_lists(
        [{"code": "fda_dshea", "text": "a"}],
        [{"code": "FDA_DSHEA", "text": "b"}, {"code": "x", "text": "c"}],
    )
    assert [d["code"] for d in out] == ["fda_dshea", "x"]


# --- fetch_product_evidence_for_keys -----------------------------------------

async def test_fetch_product_evidence_merges_rows(monkeypatch) -> None:
    async def _noop():
        return None
    monkeypatch.setattr(pe, "ensure_product_evidence_tables", _noop)
    rows = [
        {"claims": [{"claim_text": "A", "source_ref": "s1", "substantiation_status": "substantiated"}],
         "review_state": "reviewed", "required_disclaimers": [{"code": "d1"}]},
        {"claims": [{"claim_text": "B", "source_ref": "s2"}], "review_state": "observed",
         "required_disclaimers": None},
    ]
    out = await pe.fetch_product_evidence_for_keys(["pk"], db=_FakeDB(all_=rows))
    assert [c["claim_text"] for c in out["evidence_profile"]["claims"]] == ["A", "B"]
    assert out["required_disclaimers"] == [{"code": "d1"}]


async def test_fetch_product_evidence_empty_keys_is_empty() -> None:
    assert await pe.fetch_product_evidence_for_keys([]) == {}


# --- assembler fetch_evidence_for_keys UNION ---------------------------------

async def test_assembler_unions_beauty_and_general(monkeypatch) -> None:
    beauty_row = {
        "evidence_profile": {
            "claims": [{"claim_text": "Brightens", "source_ref": "niacinamide",
                        "substantiation_status": "substantiated"}],
            "review_state": "reviewed",
        },
        "required_disclaimers": None,
    }

    async def _fake_general(keys, *, db=None, geo_code="default"):
        return {"evidence_profile": {
            "claims": [{"claim_text": "Clinically tested", "source_ref": "lab_001",
                        "substantiation_status": "substantiated"}],
            "review_state": "observed",
        }}
    monkeypatch.setattr("db.product_evidence.fetch_product_evidence_for_keys", _fake_general)

    out = await asm.fetch_evidence_for_keys(["pk1"], db=_FakeDB(one=beauty_row))
    texts = [c["claim_text"] for c in out["evidence_profile"]["claims"]]
    assert texts == ["Brightens", "Clinically tested"]  # beauty first
    assert out["evidence_profile"]["review_state"] == "reviewed"


async def test_assembler_general_only_when_no_beauty(monkeypatch) -> None:
    async def _fake_general(keys, *, db=None, geo_code="default"):
        return {"evidence_profile": {"claims": [{"claim_text": "Lab-backed", "source_ref": "lab_2"}],
                                     "review_state": "observed"}}
    monkeypatch.setattr("db.product_evidence.fetch_product_evidence_for_keys", _fake_general)
    out = await asm.fetch_evidence_for_keys(["pk1"], db=_FakeDB(one=None))  # no beauty row
    assert [c["claim_text"] for c in out["evidence_profile"]["claims"]] == ["Lab-backed"]


async def test_assembler_empty_when_no_evidence_anywhere(monkeypatch) -> None:
    async def _none(keys, *, db=None, geo_code="default"):
        return {}
    monkeypatch.setattr("db.product_evidence.fetch_product_evidence_for_keys", _none)
    out = await asm.fetch_evidence_for_keys(["pk1"], db=_FakeDB(one=None))
    assert out == {}


# --- readiness evidence tier --------------------------------------------------

async def test_readiness_includes_evidence_tier(monkeypatch) -> None:
    async def _fake_count(table, merchant_id, *, platform=None, where_extra=None):
        return {"catalog_products": 3, "products_cache": 3,
                "product_quality_snapshot": 3, "product_enrichment": 3}.get(table, 0)
    monkeypatch.setattr(mar, "_table_count", _fake_count)

    async def _ev(mid):
        return 7
    monkeypatch.setattr(mar, "_count_substantiated_evidence", _ev)

    r = await mar.assess_merchant_audit_readiness("m1")
    assert r["evidence"]["products_with_substantiated_claims"] == 7
