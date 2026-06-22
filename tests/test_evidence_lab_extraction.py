"""Phase 2b lab-report extraction: PDF/JSON parsing, the disease/drug safety
screen, and the LLM-backed candidate extractor (mocked LLM). The candidates are
PROPOSALS — they come back unverified; substantiation happens only when the
merchant confirms one against the stored artifact, so the value here is that the
parse + screen are honest and deterministic."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db.product_evidence as pe
import services.evidence_extraction as ee
import services.llm_synthesis as llm


# --- _safe_json ---------------------------------------------------------------

def test_safe_json_plain() -> None:
    assert ee._safe_json('{"claims": []}') == {"claims": []}


def test_safe_json_code_fenced() -> None:
    fenced = "```json\n{\"claims\": [{\"claim_text\": \"x\"}]}\n```"
    assert ee._safe_json(fenced) == {"claims": [{"claim_text": "x"}]}


def test_safe_json_prose_wrapped() -> None:
    wrapped = 'Here you go: {"claims": []} hope that helps'
    assert ee._safe_json(wrapped) == {"claims": []}


def test_safe_json_garbage_is_none() -> None:
    assert ee._safe_json("not json at all") is None
    assert ee._safe_json(None) is None


# --- _is_claim_safe (disease/drug screen) ------------------------------------

def test_cosmetic_claims_pass() -> None:
    assert ee._is_claim_safe("Reduces transepidermal water loss by 32% after 4 weeks")
    assert ee._is_claim_safe("Prevents moisture loss for up to 24 hours")
    assert ee._is_claim_safe("SPF 30 verified by in-vitro testing")


def test_disease_noun_blocked() -> None:
    assert not ee._is_claim_safe("Clinically shown to treat eczema")
    assert not ee._is_claim_safe("Reduces acne lesions")


def test_drug_verb_blocked() -> None:
    assert not ee._is_claim_safe("Cures dryness permanently")
    assert not ee._is_claim_safe("Helps diagnose skin conditions")


def test_therapeutic_verbs_blocked() -> None:
    # Broadened screen: common therapeutic verbs + condition variants now drop.
    for txt in (
        "Treats high blood pressure",
        "Relieves arthritic pain",
        "Heals wounds overnight",
        "Treats the flu",
        "Antibacterial against infection",
    ):
        assert not ee._is_claim_safe(txt), txt


def test_drug_verb_substring_not_falsely_blocked() -> None:
    # "secure"/"accurate" contain "cure" as a substring but aren't drug claims —
    # the screen is word-boundary aware. Ordinary cosmetic phrasing also survives.
    assert ee._is_claim_safe("Accurate, secure batch testing on every lot")
    assert ee._is_claim_safe("Prevents moisture loss for 24 hours")
    assert ee._is_claim_safe("Reduces the appearance of fine lines")


# --- _parse_candidates --------------------------------------------------------

def _result(obj) -> dict:
    return {"text": json.dumps(obj)}


def test_parse_candidates_basic() -> None:
    out = ee._parse_candidates(
        _result({"claims": [
            {"claim_text": "Hydration up 32%", "source_excerpt": "TEWL -32% (n=30)"},
        ]}),
        max_claims=8,
    )
    assert out == [{"claim_text": "Hydration up 32%", "source_excerpt": "TEWL -32% (n=30)"}]


def test_parse_candidates_dedupes_and_drops_unsafe() -> None:
    out = ee._parse_candidates(
        _result({"claims": [
            {"claim_text": "Hydration up 32%"},
            {"claim_text": "hydration up 32%"},          # dup (case-insensitive)
            {"claim_text": "Treats eczema"},             # unsafe -> dropped
            {"claim_text": ""},                          # empty -> dropped
            "not-a-dict",                                # ignored
        ]}),
        max_claims=8,
    )
    assert [c["claim_text"] for c in out] == ["Hydration up 32%"]
    assert out[0]["source_excerpt"] is None


def test_parse_candidates_caps_max() -> None:
    claims = [{"claim_text": f"Finding {i}"} for i in range(20)]
    out = ee._parse_candidates(_result({"claims": claims}), max_claims=5)
    assert len(out) == 5


def test_parse_candidates_bad_shape_is_empty() -> None:
    assert ee._parse_candidates({"text": "not json"}, max_claims=8) == []
    assert ee._parse_candidates({"text": '{"claims": "nope"}'}, max_claims=8) == []
    assert ee._parse_candidates("not-a-dict", max_claims=8) == []


# --- extract_lab_claims (mocked LLM) -----------------------------------------

async def test_extract_empty_text_returns_empty() -> None:
    assert await ee.extract_lab_claims("   ") == []


async def test_extract_no_provider_raises(monkeypatch) -> None:
    monkeypatch.setattr(ee, "_pick_provider", lambda: None)
    import pytest
    with pytest.raises(ee.EvidenceExtractionError):
        await ee.extract_lab_claims("real report text")


async def test_extract_calls_llm_and_parses(monkeypatch) -> None:
    captured = {}

    async def _fake_synthesize(*, system, user, provider, model, max_tokens):
        captured["provider"] = provider
        captured["user"] = user
        return {"text": json.dumps({"claims": [
            {"claim_text": "SPF 30 verified", "source_excerpt": "SPF 30"},
            {"claim_text": "Treats psoriasis"},  # screened out downstream
        ]})}

    monkeypatch.setattr(ee, "_pick_provider", lambda: "deepseek")
    monkeypatch.setattr(llm, "synthesize", _fake_synthesize)
    monkeypatch.setattr(llm, "default_model_for_provider", lambda p: "deepseek-chat")

    out = await ee.extract_lab_claims("report body", product_title="Sun Serum")
    assert [c["claim_text"] for c in out] == ["SPF 30 verified"]
    assert captured["provider"] == "deepseek"
    assert "Sun Serum" in captured["user"]
    assert "report body" in captured["user"]


async def test_extract_llm_failure_raises(monkeypatch) -> None:
    async def _boom(*, system, user, provider, model, max_tokens):
        raise llm.LLMSynthesisError("upstream down", provider=provider)

    monkeypatch.setattr(ee, "_pick_provider", lambda: "deepseek")
    monkeypatch.setattr(llm, "synthesize", _boom)
    monkeypatch.setattr(llm, "default_model_for_provider", lambda p: "deepseek-chat")

    import pytest
    with pytest.raises(ee.EvidenceExtractionError):
        await ee.extract_lab_claims("report body")


# --- extract_pdf_text (error paths; happy path needs a real PDF writer) ------

def test_extract_pdf_text_empty_raises() -> None:
    import pytest
    with pytest.raises(ee.EvidenceExtractionError):
        ee.extract_pdf_text(b"")


def test_extract_pdf_text_garbage_raises() -> None:
    import pytest
    # Not a PDF (or no extractable text) -> a clean extraction error, never a 500.
    with pytest.raises(ee.EvidenceExtractionError):
        ee.extract_pdf_text(b"%PDF-1.4 this is not really a pdf")


# --- insert_evidence_artifact (fake DB) --------------------------------------

class _CaptureDB:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, values):
        self.calls.append((sql, values))


async def test_insert_artifact_serializes_keys(monkeypatch) -> None:
    async def _noop():
        return None
    monkeypatch.setattr(pe, "ensure_product_evidence_tables", _noop)
    db = _CaptureDB()
    aid = await pe.insert_evidence_artifact(
        artifact_id="art_abc",
        product_key="pk1",
        merchant_id="m1",
        kind="lab_report",
        url_or_blob_ref="report.pdf",
        extracted_claim_keys=["SPF 30 verified"],
        db=db,
    )
    assert aid == "art_abc"
    assert len(db.calls) == 1
    _, values = db.calls[0]
    assert values["aid"] == "art_abc"
    assert values["pk"] == "pk1"
    assert values["kind"] == "lab_report"
    assert values["source"] == "merchant_upload"
    assert json.loads(values["keys"]) == ["SPF 30 verified"]


async def test_insert_artifact_null_keys(monkeypatch) -> None:
    async def _noop():
        return None
    monkeypatch.setattr(pe, "ensure_product_evidence_tables", _noop)
    db = _CaptureDB()
    await pe.insert_evidence_artifact(
        artifact_id="art_x", product_key="pk1", merchant_id=None,
        kind="lab_report", db=db,
    )
    _, values = db.calls[0]
    assert values["keys"] is None
    assert values["ref"] is None
