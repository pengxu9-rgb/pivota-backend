import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from services import external_seed_gemini_reviewer as reviewer
from services.external_seed_model_review import ReviewTarget, build_review_snapshot
from services.external_seed_review_pilot import REVIEW_MODELS

COCO = ReviewTarget("41341731438667", "brand_direct")
MAUVE = ReviewTarget("41341731635275", "brand_direct")
TARGETS = (COCO, MAUVE)
MODEL, SECOND_MODEL, THIRD_MODEL = REVIEW_MODELS


def make_evidence():
    return {
        "private_snapshot": "internal-outer-field",
        "sources": {
            f"shopify_us:{target.variant_id}": {
                "variant_id": target.variant_id,
                "source_url": "https://example.com/products/balm.js?country=US",
                "shade": shade,
                "native_barcode": barcode,
                "price_minor": 1400,
                "currency": "USD",
                "private_note": "internal-nested-field",
            }
            for target, shade, barcode in (
                (COCO, "Coco Nude", "8800258082894"),
                (MAUVE, "Mauve Whip", "8800258083112"),
            )
        },
        "target_sources": [
            asdict(target) | {"evidence_refs": [f"shopify_us:{target.variant_id}"]}
            for target in TARGETS
        ],
    }


def make_snapshot(evidence):
    return build_review_snapshot(
        seed_id="private-production-seed-id",
        seed_snapshot={"private_fields": "employee-id, seller-ref"},
        evidence_packet=evidence,
        expected_targets=TARGETS,
    )


def provider_response(model_name, targets, *, action="employee_review"):
    rows = [
        asdict(target)
        | {"action": action, "evidence_refs": [f"shopify_us:{target.variant_id}"]}
        for target in targets
    ]
    return {
        "modelVersion": model_name,
        "responseId": "trusted-provider-response-1",
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps({"rows": rows})}]},
            }
        ],
    }


@pytest.fixture(autouse=True)
def enable_vertex(monkeypatch):
    monkeypatch.setattr(reviewer.vertex_gemini, "vertex_enabled", lambda: True)


@pytest.mark.asyncio
async def test_two_base_inputs_blinded_and_transport_emits_structured_trusted_receipt(
    monkeypatch,
):
    evidence = make_evidence()
    snapshot = make_snapshot(evidence)
    post = AsyncMock(
        side_effect=[
            provider_response(MODEL, TARGETS),
            provider_response(SECOND_MODEL, TARGETS),
        ]
    )
    monkeypatch.setattr(reviewer, "_vertex_post", post)
    first = await reviewer.run_gemini_review(
        snapshot, evidence, stage="primary", model_name=MODEL, targets=TARGETS
    )
    second = await reviewer.run_gemini_review(
        snapshot, evidence, stage="secondary", model_name=SECOND_MODEL, targets=TARGETS
    )

    assert (first.model_name, second.model_name) == ("gemini-3.8-flash", "gemini-3.5-flash-lite")
    assert first.run_id == "trusted-provider-response-1"
    assert (
        first.seed_id == snapshot.seed_id
        and first.seed_digest == snapshot.seed_digest
        and first.evidence_digest == snapshot.evidence_digest
    )
    first_request, second_request = [call.args[1] for call in post.await_args_list]
    assert first_request["systemInstruction"] != second_request["systemInstruction"]
    assert first_request["contents"] == second_request["contents"]
    assert first_request["generationConfig"]["responseMimeType"] == "application/json"
    assert (
        first_request["generationConfig"]["responseSchema"]["properties"]["rows"][
            "minItems"
        ]
        == 2
    )
    for request, level in ((first_request, "LOW"), (second_request, "MINIMAL")):
        assert request["generationConfig"]["thinkingConfig"] == {"thinkingLevel": level}
        assert "temperature" not in request["generationConfig"]
    sent = json.dumps(first_request)
    assert "internal-outer-field" not in sent and "internal-nested-field" not in sent
    assert snapshot.seed_id not in sent and snapshot.evidence_digest not in sent
    assert "8800258082894" in sent


@pytest.mark.asyncio
async def test_tie_break_sees_only_scoped_variant_and_no_previous_result(monkeypatch):
    evidence = make_evidence()
    snapshot = make_snapshot(evidence)
    post = AsyncMock(return_value=provider_response(THIRD_MODEL, (COCO,)))
    monkeypatch.setattr(reviewer, "_vertex_post", post)
    result = await reviewer.run_gemini_review(
        snapshot, evidence, stage="tie_break", model_name=THIRD_MODEL, targets=(COCO,)
    )

    assert result.model_name == "gemini-3.5-flash"
    assert result.rows[0].target == COCO
    assert len(result.rows) == 1
    request = post.await_args.args[1]
    assert request["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}
    sent = request["contents"][0]["parts"][0]["text"]
    assert COCO.variant_id in sent
    assert MAUVE.variant_id not in sent and "Mauve Whip" not in sent


@pytest.mark.asyncio
async def test_nullable_offer_id_accepts_vertex_empty_string_for_brand_only(
    monkeypatch,
):
    evidence = make_evidence()
    payload = provider_response(MODEL, TARGETS)
    part = payload["candidates"][0]["content"]["parts"][0]
    data = json.loads(part["text"])
    for row in data["rows"]:
        row["offer_id"] = ""
    part["text"] = json.dumps(data)
    monkeypatch.setattr(reviewer, "_vertex_post", AsyncMock(return_value=payload))
    result = await reviewer.run_gemini_review(
        make_snapshot(evidence),
        evidence,
        stage="primary",
        model_name=MODEL,
        targets=TARGETS,
    )
    assert result is not None
    assert all(row.target.offer_id is None for row in result.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "model",
        "response_id",
        "finish",
        "duplicate",
        "unscoped",
        "offer_url",
        "extra_action",
        "missing",
        "malformed",
    ],
)
async def test_invalid_provider_output_returns_no_receipt(monkeypatch, change):
    evidence = make_evidence()
    payload = provider_response(MODEL, TARGETS)
    candidate = payload["candidates"][0]
    part = candidate["content"]["parts"][0]
    data = json.loads(part["text"])
    if change == "model":
        payload["modelVersion"] = SECOND_MODEL
    elif change == "response_id":
        payload.pop("responseId")
    elif change == "finish":
        candidate["finishReason"] = "MAX_TOKENS"
    elif change == "duplicate":
        data["rows"][1] = data["rows"][0]
    elif change == "unscoped":
        data["rows"][0]["evidence_refs"] = [f"shopify_us:{MAUVE.variant_id}"]
    elif change == "offer_url":
        data["rows"][0]["offer_id"] = "https://example.com/product"
    elif change == "extra_action":
        data["rows"][0]["action"] = "publish"
    elif change == "missing":
        data["rows"].pop()
    part["text"] = "broken {" if change == "malformed" else json.dumps(data)
    monkeypatch.setattr(reviewer, "_vertex_post", AsyncMock(return_value=payload))
    assert (
        await reviewer.run_gemini_review(
            make_snapshot(evidence),
            evidence,
            stage="primary",
            model_name=MODEL,
            targets=TARGETS,
        )
        is None
    )


@pytest.mark.asyncio
async def test_unconfigured_model_vertex_disabled_and_mismatched_evidence_skip_transport(
    monkeypatch,
):
    evidence = make_evidence()
    snapshot = make_snapshot(evidence)
    changed = deepcopy(evidence)
    changed["sources"][f"shopify_us:{COCO.variant_id}"]["price_minor"] = 1300
    post = AsyncMock()
    monkeypatch.setattr(reviewer, "_vertex_post", post)

    assert (
        await reviewer.run_gemini_review(
            snapshot,
            evidence,
            stage="primary",
            model_name="unsafe/model",
            targets=TARGETS,
        )
        is None
    )
    assert (
        await reviewer.run_gemini_review(
            snapshot, changed, stage="primary", model_name=MODEL, targets=TARGETS
        )
        is None
    )
    assert (
        await reviewer.run_gemini_review(
            snapshot, evidence, stage="primary", model_name=MODEL, targets=(COCO,)
        )
        is None
    )
    monkeypatch.setattr(reviewer.vertex_gemini, "vertex_enabled", lambda: False)
    assert (
        await reviewer.run_gemini_review(
            snapshot, evidence, stage="primary", model_name=MODEL, targets=TARGETS
        )
        is None
    )
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_returns_no_receipt_and_cancels_call(monkeypatch):
    evidence = make_evidence()
    cancelled = asyncio.Event()

    async def slow_transport(*_args):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    monkeypatch.setattr(reviewer, "_vertex_post", slow_transport)
    monkeypatch.setattr(reviewer, "_REVIEW_TIMEOUT_SECONDS", 0.01)
    assert (
        await reviewer.run_gemini_review(
            make_snapshot(evidence),
            evidence,
            stage="primary",
            model_name=MODEL,
            targets=TARGETS,
        )
        is None
    )
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_unscoped_packet_never_reaches_external_model(monkeypatch):
    evidence = make_evidence()
    evidence.pop("target_sources")
    post = AsyncMock()
    monkeypatch.setattr(reviewer, "_vertex_post", post)
    result = await reviewer.run_gemini_review(
        make_snapshot(evidence),
        evidence,
        stage="tie_break",
        model_name=MODEL,
        targets=(COCO,),
    )
    assert result is None
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_reuses_vertex_url_and_auth_in_actual_http_path(monkeypatch):
    evidence = make_evidence()
    calls = []
    auth = AsyncMock(return_value={"Authorization": "Bearer fake-credential"})
    monkeypatch.setattr(reviewer.vertex_gemini, "auth_headers", auth)
    monkeypatch.setattr(
        reviewer.vertex_gemini,
        "generate_content_url",
        lambda model: f"https://vertex.example/{model}:generateContent",
    )

    async def fake_http(request):
        calls.append(request)
        return httpx.Response(200, json=provider_response(MODEL, TARGETS))

    original = httpx.AsyncClient
    monkeypatch.setattr(
        reviewer.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(fake_http), **kwargs),
    )
    result = await reviewer.run_gemini_review(
        make_snapshot(evidence),
        evidence,
        stage="primary",
        model_name=MODEL,
        targets=TARGETS,
    )
    assert result is not None and len(calls) == 1
    assert (
        calls[0].method == "POST"
        and calls[0].headers["Authorization"] == "Bearer fake-credential"
    )
    assert "fake-credential" not in calls[0].content.decode()
    auth.assert_awaited_once_with()
