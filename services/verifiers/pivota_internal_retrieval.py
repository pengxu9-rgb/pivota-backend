"""P5.3 verifier: pivota_internal_retrieval.

Asserts the backend's canonical PDP resolver endpoint round-trips:
given a sig_id, GET /products/{sig_id} returns the same product
the audit knows about. Catches cases where the catalog row was
deleted / unlinked after the audit ran.

Different from pdp_renders:
  - pdp_renders hits agent.pivota.cc (the rendered HTML PDP)
  - pivota_internal_retrieval hits THIS backend (the JSON data
    feed that the frontend consumes)

This verifier is a smoke test on Pivota's own consistency: if a
sig_id was published to a buyer but the backend can't resolve it
anymore, the buyer would land on a 404 — distinct from the PDP
failing to render at the frontend.

Evidence:
  - sig_id
  - backend_status_code
  - resolved_product_key (round-trip match with audit's product_key)
  - resolved_merchant_id (round-trip match)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from services.verification_run_worker import (
    VerifierContext, VerifierResult, register_verifier,
)
from services.verifiers._product_context import load_product_context

logger = logging.getLogger(__name__)


_TIMEOUT_S = 10.0


def _backend_base_url() -> str:
    """The backend's own base URL. Used so the verifier hits the
    same instance it's running in (or a sibling pod) rather than
    the frontend's agent.pivota.cc. Defaults to localhost for
    test envs; prod sets PIVOTA_BACKEND_INTERNAL_URL."""
    import os
    return (
        os.getenv("PIVOTA_BACKEND_INTERNAL_URL")
        or "http://localhost:8000"
    ).rstrip("/")


async def run_pivota_internal_retrieval(
    context: VerifierContext,
) -> VerifierResult:
    product = await load_product_context(
        audit_run_id=context.audit_run_id,
        product_key=context.product_key,
    )
    if product is None:
        return VerifierResult(
            status="blocked",
            error_message="product context unresolvable",
            evidence_jsonb={"resolved_product": False},
        )

    sig_id = product.get("pivota_signature_id")
    if not sig_id:
        return VerifierResult(
            status="blocked",
            error_message="product has no pivota_signature_id",
            evidence_jsonb={
                "product_key": product.get("product_key"),
                "has_sig_id": False,
            },
        )

    expected_product_key = product.get("product_key")
    expected_merchant_id = product.get("merchant_id")

    base = _backend_base_url()
    resolver_url = f"{base}/products/{sig_id}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(resolver_url)
    except httpx.TimeoutException as exc:
        return VerifierResult(
            status="failed",
            error_message=f"timeout: {exc!r}",
            evidence_jsonb={"resolver_url": resolver_url},
        )
    except httpx.RequestError as exc:
        return VerifierResult(
            status="failed",
            error_message=f"http_request: {exc!r}",
            evidence_jsonb={"resolver_url": resolver_url},
        )

    status_code = response.status_code

    evidence: Dict[str, Any] = {
        "resolver_url": resolver_url,
        "sig_id": sig_id,
        "backend_status_code": status_code,
        "expected_product_key": expected_product_key,
        "expected_merchant_id": expected_merchant_id,
    }

    # 404: the catalog row was deleted (or sig was never persisted).
    # NOT retryable — the consistency gap is real. Mark blocked so
    # ops sees it as a permanent finding.
    if status_code == 404:
        return VerifierResult(
            status="blocked",
            error_message="backend_404_resolver_lost_sig",
            evidence_jsonb=evidence,
        )

    if status_code >= 500:
        return VerifierResult(
            status="failed",
            error_message=f"backend_5xx_{status_code}",
            evidence_jsonb=evidence,
        )

    if not (200 <= status_code < 300):
        return VerifierResult(
            status="failed",
            error_message=f"unexpected_status_{status_code}",
            evidence_jsonb=evidence,
        )

    # 2xx — parse + check round-trip match.
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        return VerifierResult(
            status="failed",
            error_message=f"non_json_response: {exc!r}",
            evidence_jsonb=evidence,
        )

    resolved_product = body.get("product") or {}
    resolved_pk = resolved_product.get("product_key")
    resolved_mid = resolved_product.get("merchant_id")

    evidence["resolved_product_key"] = resolved_pk
    evidence["resolved_merchant_id"] = resolved_mid

    if resolved_pk == expected_product_key and resolved_mid == expected_merchant_id:
        return VerifierResult(
            status="succeeded",
            evidence_jsonb=evidence,
        )

    # Round-trip mismatch — the sig now points at a different
    # product. Not retryable; surface as blocked for ops.
    return VerifierResult(
        status="blocked",
        error_message="sig_collision_or_remap",
        evidence_jsonb=evidence,
    )


register_verifier(
    "pivota_internal_retrieval", run_pivota_internal_retrieval,
)
