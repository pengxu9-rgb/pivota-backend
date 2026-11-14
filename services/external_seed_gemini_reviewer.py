"""Run a blinded external-seed review through the configured Vertex transport."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict
from typing import Sequence

import httpx

from services import vertex_gemini
from services.external_seed_model_review import (
    ReviewReceipt,
    ReviewRow,
    ReviewSnapshot,
    ReviewStage,
    ReviewTarget,
    evidence_packet_matches,
    validated_review_rows,
)
from services.llm_fence import PRODUCT_DATA_FENCE

logger = logging.getLogger(__name__)
_THINKING_LEVEL = {
    "gemini-3.8-flash": "LOW",
    "gemini-3.5-flash-lite": "MINIMAL",
    "gemini-3.5-flash": "MEDIUM",
}
GEMINI_REVIEW_MODELS = frozenset(_THINKING_LEVEL)
_REVIEW_TIMEOUT_SECONDS = 45.0
_MAX_PUBLIC_PAYLOAD_BYTES = 24_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/-]{1,180}\Z")
_RESPONSE_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_PUBLIC_SOURCE_FIELDS = frozenset(
    {
        "variant_id",
        "gtin",
        "shade",
        "price_minor",
        "in_stock",
        "source_url",
        "offer_url",
        "shipping_source_url",
        "shipping_scope",
        "country",
        "currency",
        "currency_proof",
        "observed_at",
        "native_barcode",
        "merchant_sku",
        "gtin_provenance",
    }
)
_STAGE_INSTRUCTION = {
    "primary": "Check the proposed offer independently against the scoped observations.",
    "secondary": "Independently challenge whether the scoped observations support the variant, barcode, market and seller.",
    "tie_break": "Independently review only the supplied disputed targets. Judge from the scoped facts alone.",
}


def _structured_request(
    stage: ReviewStage, public_input: str, row_count: int, model_name: str
) -> dict:
    row_schema = {
        "type": "OBJECT",
        "properties": {
            "variant_id": {"type": "STRING"},
            "scope": {
                "type": "STRING",
                "enum": ["brand_direct", "exact_retailer_offer"],
            },
            "offer_id": {"type": "STRING", "nullable": True},
            "action": {"type": "STRING", "enum": ["employee_review", "hold"]},
            "evidence_refs": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["variant_id", "scope", "offer_id", "action", "evidence_refs"],
    }
    system_prompt = (
        "Review public product and offer evidence for a read-only recommendation. "
        f"{_STAGE_INSTRUCTION[stage]} "
        "Return exactly one result per requested target. Copy variant_id, scope and offer_id "
        "from that target without change. For brand_direct, offer_id must be JSON null; "
        "a public offer URL is evidence and never an offer ID. Use employee_review only when "
        "the seller, exact variant and market are supported by its cited evidence. "
        "Only cite evidence reference IDs assigned to that target. Otherwise use hold. "
        "Keep brand-direct and independent-retailer scopes separate. A matching SKU and "
        "externally supported barcode can be sufficient with explicit provenance; distinguish "
        "that from a native barcode. Different barcodes cannot establish formula or packaging "
        "differences. Observed price and stock apply to the observed seller only. Do not infer "
        "checkout, publish status, or approval. " + PRODUCT_DATA_FENCE.notice
    )
    return {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {"role": "user", "parts": [{"text": PRODUCT_DATA_FENCE.wrap(public_input)}]}
        ],
        "generationConfig": {
            "maxOutputTokens": 4096,
            "thinkingConfig": {"thinkingLevel": _THINKING_LEVEL[model_name]},
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "rows": {
                        "type": "ARRAY",
                        "items": row_schema,
                        "minItems": row_count,
                        "maxItems": row_count,
                    }
                },
                "required": ["rows"],
            },
        },
    }


def _scoped_public_input(
    snapshot: ReviewSnapshot, evidence_packet: dict, targets: tuple[ReviewTarget, ...]
) -> str:
    scoped_refs = dict(snapshot.target_sources)
    requested = []
    selected_refs = set()
    for target in targets:
        refs = sorted(scoped_refs[target])
        if not _IDENTIFIER.fullmatch(target.variant_id) or (
            target.offer_id is not None and not _IDENTIFIER.fullmatch(target.offer_id)
        ):
            raise ValueError("Unsupported public target identifier.")
        if not all(_IDENTIFIER.fullmatch(ref) for ref in refs):
            raise ValueError("Unsupported public evidence reference.")
        requested.append(asdict(target) | {"evidence_refs": refs})
        selected_refs.update(refs)
    sources = {}
    for ref in sorted(selected_refs):
        source = evidence_packet["sources"][ref]
        if not isinstance(source, dict):
            raise ValueError("Each public source must contain structured observations.")
        sources[ref] = {
            key: value
            for key, value in source.items()
            if key in _PUBLIC_SOURCE_FIELDS
            and type(value) in (str, int, bool, type(None))
        }
        if not sources[ref]:
            raise ValueError("Public source has no supported observations.")
    payload = {"targets": requested, "sources": sources}
    serialized = json.dumps(
        PRODUCT_DATA_FENCE.sanitize_value(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(serialized.encode("utf-8")) > _MAX_PUBLIC_PAYLOAD_BYTES:
        raise ValueError("Public evidence exceeds review request limit.")
    return serialized


async def _vertex_post(model_name: str, request: dict) -> dict:
    headers = await vertex_gemini.auth_headers()
    url = vertex_gemini.generate_content_url(model_name)
    async with httpx.AsyncClient(timeout=_REVIEW_TIMEOUT_SECONDS) as client:
        response = await client.post(url, json=request, headers=headers)
        response.raise_for_status()
        return response.json()


def _unique_json_fields(items: list[tuple[str, object]]) -> dict:
    fields = dict(items)
    if len(fields) != len(items):
        raise ValueError("Duplicate fields in review response.")
    return fields


def _parse_receipt(
    payload: dict,
    snapshot: ReviewSnapshot,
    stage: ReviewStage,
    model_name: str,
    targets: tuple[ReviewTarget, ...],
) -> ReviewReceipt:
    response_id = payload["responseId"]
    if (
        payload["modelVersion"] != model_name
        or not isinstance(response_id, str)
        or not _RESPONSE_ID.fullmatch(response_id)
    ):
        raise ValueError(
            "Vertex response identity does not match the configured model."
        )
    (candidate,) = payload["candidates"]
    if candidate["finishReason"] != "STOP":
        raise ValueError("Vertex review was incomplete.")
    parts = candidate["content"]["parts"]
    text = "".join(part["text"] for part in parts if not part.get("thought"))
    data = json.loads(text, object_pairs_hook=_unique_json_fields)
    if (
        not isinstance(data, dict)
        or set(data) != {"rows"}
        or not isinstance(data["rows"], list)
    ):
        raise ValueError("Invalid review response envelope.")
    rows = []
    for row in data["rows"]:
        if (
            not isinstance(row, dict)
            or set(row)
            != {"variant_id", "scope", "offer_id", "action", "evidence_refs"}
            or not isinstance(row["evidence_refs"], list)
        ):
            raise ValueError("Invalid review response row.")
        # A brand-direct target is fully identified by scope and variant. Vertex
        # may spell a required nullable string as an empty string.
        offer_id = (
            None
            if row["scope"] == "brand_direct" and row["offer_id"] == ""
            else row["offer_id"]
        )
        target = ReviewTarget(row["variant_id"], row["scope"], offer_id)
        rows.append(ReviewRow(target, row["action"], tuple(row["evidence_refs"])))
    receipt = ReviewReceipt(
        stage,
        response_id,
        model_name,
        snapshot.seed_id,
        snapshot.seed_digest,
        snapshot.evidence_digest,
        tuple(rows),
    )
    validated_review_rows(snapshot, receipt, stage, model_name, targets)
    return receipt


async def run_gemini_review(
    snapshot: ReviewSnapshot,
    evidence_packet: dict,
    *,
    stage: ReviewStage,
    model_name: str,
    targets: Sequence[ReviewTarget],
) -> ReviewReceipt | None:
    """Return a verified Vertex receipt or fail closed when unavailable or invalid.

    The worker supplies an allowlisted public evidence packet. This transport sends
    only the targeted scopes and referenced public facts. It receives no peer
    responses. The caller schedules a tie-break only for actual disagreements.
    """
    if (
        model_name not in GEMINI_REVIEW_MODELS
        or stage not in _STAGE_INSTRUCTION
        or not vertex_gemini.vertex_enabled()
    ):
        return None
    selected = tuple(targets)
    if not selected or len(set(selected)) != len(selected):
        return None
    expected = set(snapshot.expected_targets)
    if (stage == "tie_break" and not set(selected) <= expected) or (
        stage != "tie_break" and set(selected) != expected
    ):
        return None
    if not evidence_packet_matches(snapshot, evidence_packet) or not isinstance(
        evidence_packet.get("target_sources"), list
    ):
        return None
    try:
        scoped = _scoped_public_input(snapshot, evidence_packet, selected)
        request = _structured_request(stage, scoped, len(selected), model_name)
        payload = await asyncio.wait_for(
            _vertex_post(model_name, request), _REVIEW_TIMEOUT_SECONDS
        )
        return _parse_receipt(payload, snapshot, stage, model_name, selected)
    except (
        TimeoutError,
        httpx.HTTPError,
        vertex_gemini.VertexAuthError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
    ) as error:
        logger.warning(
            "External seed model review failed at %s with %s",
            stage,
            type(error).__name__,
        )
        return None
