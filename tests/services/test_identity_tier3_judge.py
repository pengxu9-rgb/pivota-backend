"""ADR-010 D-2 Phase C — judge prompt calibration, parsing, eval gate (no LLM)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")

from services.identity_tier3_judge import (  # noqa: E402
    CONFIDENCE_FLOOR,
    JUDGE_VERSION,
    build_judge_prompt,
    eval_gate,
    judge_group,
    parse_judge_response,
    validate_verdict,
)


class TestPromptCalibration:
    """The prompt must encode the step-5 lane-4 verdict semantics, which
    deliberately differ from pdp_matcher/llm_match ('sizes still count as
    the same PDP' is WRONG here)."""

    def test_sizes_are_keep_separate(self):
        prompt = build_judge_prompt([{"title": "x", "url": "u"}])
        assert "different SIZES" in prompt
        assert "distinct sellable SKUs" in prompt

    def test_multi_seller_is_keep_separate(self):
        prompt = build_judge_prompt([{"title": "x", "url": "u"}])
        assert "DIFFERENT seller" in prompt
        assert "multi-seller" in prompt

    def test_asymmetric_error_cost_stated(self):
        prompt = build_judge_prompt([{"title": "x", "url": "u"}])
        assert "far worse" in prompt
        assert 'never a low-conviction "collapse"' in prompt

    def test_rows_rendered(self):
        prompt = build_judge_prompt([
            {"title": "Gel Mask", "brand": "Biodance",
             "url": "https://biodance.com/products/0627_cm_a"},
        ])
        assert "Gel Mask" in prompt and "0627_cm_a" in prompt


class TestParsing:
    def _payload(self, text: str) -> dict:
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    def test_plain_json(self):
        v = parse_judge_response(self._payload(
            '{"verdict": "collapse", "confidence": 0.95, "reasoning": "clones"}'))
        assert v["verdict"] == "collapse" and v["judge_version"] == JUDGE_VERSION

    def test_fenced_json(self):
        v = parse_judge_response(self._payload(
            '```json\n{"verdict": "keep_separate", "confidence": 0.8, "reasoning": "sizes"}\n```'))
        assert v["verdict"] == "keep_separate"

    def test_garbage_returns_none(self):
        assert parse_judge_response(self._payload("no json here")) is None

    def test_invalid_verdict_rejected(self):
        assert validate_verdict({"verdict": "obliterate", "confidence": 0.9}) is None

    def test_out_of_range_confidence_rejected(self):
        assert validate_verdict({"verdict": "collapse", "confidence": 1.7}) is None


class TestOfflineBehavior:
    def test_no_api_key_returns_none_never_guesses(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("PIVOTA_GEMINI_API_KEY", raising=False)
        assert judge_group([{"title": "x"}]) is None


class TestEvalGate:
    def _r(self, label: str, verdict, conf: float, gid: str = "g") -> dict:
        return {"group_id": gid, "label": label, "verdict": verdict,
                "confidence": conf}

    def test_confident_mis_merge_fails_the_gate(self):
        gate = eval_gate([
            self._r("keep_separate", "collapse", 0.9, "bad"),
            self._r("collapse", "collapse", 0.9),
        ])
        assert gate["gate_passed"] is False
        assert gate["mis_merges"] == 1
        assert gate["mis_merge_group_ids"] == ["bad"]

    def test_low_confidence_collapse_on_keep_does_not_fail(self):
        # Below the floor the caller ignores the verdict — not a mis-merge.
        gate = eval_gate([self._r("keep_separate", "collapse", 0.5)])
        assert gate["gate_passed"] is True
        assert gate["unsure_or_failed"] == 1

    def test_clean_run_passes_with_coverage(self):
        gate = eval_gate([
            self._r("collapse", "collapse", 0.95),
            self._r("collapse", "unsure", 0.4),
            self._r("keep_separate", "keep_separate", 0.9),
        ])
        assert gate["gate_passed"] is True
        assert gate["collapse_coverage"] == 0.5
        assert gate["keep_coverage"] == 1.0

    def test_failed_judgments_count_as_unsure(self):
        gate = eval_gate([self._r("collapse", None, 0.0)])
        assert gate["gate_passed"] is True
        assert gate["unsure_or_failed"] == 1
        assert gate["confidence_floor"] == CONFIDENCE_FLOOR
