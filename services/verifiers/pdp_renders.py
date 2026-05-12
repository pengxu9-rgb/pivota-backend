"""P5.3 verifier: pdp_renders.

Asserts that the merchant's Pivota canonical PDP renders correctly
at agent.pivota.cc/products/{sig_id}. Evidence:
  - status_code (200 expected)
  - has_schema_org_jsonld (the JSON-LD <script> block)
  - has_product_name_in_body (brand or product title appears in
    the rendered HTML)

Status routing:
  - succeeded: 200 + JSON-LD + product name present
  - failed: transient HTTP error (5xx, timeout) — worker retries
  - blocked: persistent 5xx after retries → P5.2 worker upgrades
    failed→exhausted_retries; we explicitly return 'blocked' on
    404 (the PDP doesn't exist — re-trying won't help; a future
    audit will re-mint the sig + re-enqueue)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from services.verification_run_worker import (
    VerifierContext, VerifierResult, register_verifier,
)
from services.verifiers._product_context import (
    get_agent_pivota_base_url, load_product_context,
)

logger = logging.getLogger(__name__)


# Markers we look for in the rendered HTML.
_JSONLD_MARKER = '<script type="application/ld+json"'

# HTTP timeouts. Generous (15s) — agent.pivota.cc Next.js SSR can
# cold-start; verifier wait is not user-facing.
_TIMEOUT_S = 15.0


async def run_pdp_renders(
    context: VerifierContext,
) -> VerifierResult:
    """Verify the canonical PDP renders correctly."""
    product = await load_product_context(
        audit_run_id=context.audit_run_id,
        product_key=context.product_key,
    )
    if product is None:
        return VerifierResult(
            status="blocked",
            error_message=(
                "product context unresolvable: audit run or catalog "
                "row missing"
            ),
            evidence_jsonb={"resolved_product": False},
        )

    sig_id = product.get("pivota_signature_id")
    if not sig_id:
        return VerifierResult(
            status="blocked",
            error_message="product has no pivota_signature_id",
            evidence_jsonb={
                "product_key": product.get("product_key"),
                "resolved_product": True,
                "has_sig_id": False,
            },
        )

    base = get_agent_pivota_base_url()
    pdp_url = f"{base}/products/{sig_id}"
    title = (product.get("title") or "").strip()
    brand = (product.get("brand") or "").strip()

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(pdp_url)
    except httpx.TimeoutException as exc:
        return VerifierResult(
            status="failed",
            error_message=f"timeout: {exc!r}",
            evidence_jsonb={"pdp_url": pdp_url},
        )
    except httpx.RequestError as exc:
        return VerifierResult(
            status="failed",
            error_message=f"http_request: {exc!r}",
            evidence_jsonb={"pdp_url": pdp_url},
        )

    status_code = response.status_code
    body = response.text
    body_len = len(body)
    has_jsonld = _JSONLD_MARKER in body
    # Match either the product title or the brand in the rendered
    # body. Substring match is intentional — exact match would
    # break on Next.js HTML escaping / whitespace + we just need
    # to confirm the PDP didn't render an empty shell.
    has_product_name = False
    if title and title.lower() in body.lower():
        has_product_name = True
    elif brand and brand.lower() in body.lower():
        has_product_name = True

    evidence = {
        "pdp_url": pdp_url,
        "status_code": status_code,
        "body_length": body_len,
        "has_schema_org_jsonld": has_jsonld,
        "has_product_name_in_body": has_product_name,
        "checked_for_title": title,
        "checked_for_brand": brand,
    }

    # 404: the PDP doesn't exist at this sig. Either the sig was
    # never minted, or the canonical_url backing catalog row was
    # deleted. Not retryable — a future audit re-runs the URL
    # derivation chain + may produce a different sig.
    if status_code == 404:
        return VerifierResult(
            status="blocked",
            error_message="pdp_404_not_found",
            evidence_jsonb=evidence,
        )

    # 5xx: upstream transient; retry path.
    if status_code >= 500:
        return VerifierResult(
            status="failed",
            error_message=f"pdp_upstream_5xx_{status_code}",
            evidence_jsonb=evidence,
        )

    # 2xx with body + JSON-LD + product-name match: success.
    if 200 <= status_code < 300:
        if has_jsonld and has_product_name:
            return VerifierResult(
                status="succeeded",
                evidence_jsonb=evidence,
            )
        # Rendered but missing expected markers — failure but
        # retryable (the markers may appear after a re-deploy).
        return VerifierResult(
            status="failed",
            error_message=(
                "pdp_render_missing_markers: "
                f"jsonld={has_jsonld} name={has_product_name}"
            ),
            evidence_jsonb=evidence,
        )

    # Anything else (3xx unexpected): retry.
    return VerifierResult(
        status="failed",
        error_message=f"unexpected_status_{status_code}",
        evidence_jsonb=evidence,
    )


register_verifier("pdp_renders", run_pdp_renders)
