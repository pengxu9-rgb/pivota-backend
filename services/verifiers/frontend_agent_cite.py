"""P5.5 verifier: frontend_agent_cite.

Simulates an external frontend agent (ChatGPT plugin, Claude tool,
Gemini extension) discovering + citing the merchant via Pivota's
canonical PDP. Specifically: hits agent.pivota.cc/products/{sig_id}
with agent-style request headers (Accept: application/ld+json,
User-Agent: agent-style) and asserts the response carries
structured data the agent can ingest.

Distinct from pdp_renders (P5.3):
  - pdp_renders: browser GET, asserts the rendered HTML has
    JSON-LD + product name in body
  - frontend_agent_cite: agent-style request, asserts the response
    serves JSON-LD or structured product data in a shape an LLM
    can directly consume

The two verifiers can pass or fail independently — a PDP can
render correctly for browsers but reject agent-style requests
(or vice versa), and we want to know both.

Evidence:
  - pdp_url
  - status_code
  - content_type (application/ld+json | text/html | other)
  - has_jsonld_structured_data (boolean — JSON-LD with @type=Product)
  - extracted_brand (extracted brand from JSON-LD; cross-check
    against audit's brand)
  - brand_match (boolean — extracted brand matches audit's brand)

Status routing:
  - succeeded: 200 + JSON-LD present + brand_match
  - blocked: 404 (PDP not at sig)
  - failed: 5xx, timeout, JSON-LD missing or unparseable,
    brand mismatch (retryable; PDP regenerates from canonical
    data so brand drift should be transient)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

import httpx

from services.verification_run_worker import (
    VerifierContext, VerifierResult, register_verifier,
)
from services.verifiers._product_context import (
    get_agent_pivota_base_url, load_product_context,
)

logger = logging.getLogger(__name__)


_TIMEOUT_S = 15.0

# Agent-style User-Agent strings we send to simulate discovery.
# Mirrors the UA patterns ChatGPT plugin / Anthropic / Google
# agent crawlers use. Real verification fidelity comes from
# external-agent simulation in a future PR; this verifier
# confirms our endpoint serves the right shape WHEN ASKED in
# an agent-like way.
_AGENT_USER_AGENT = (
    "Mozilla/5.0 (compatible; PivotaAgentCiteCheck/1.0; "
    "+https://pivota.cc/about/verifier)"
)

# Regex to extract <script type="application/ld+json"> blocks.
_JSONLD_BLOCK_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _parse_jsonld_blocks(html: str) -> list:
    """Extract + JSON-parse every <script type="application/ld+json">
    block. Returns a list of parsed objects (parse failures
    silently skipped — agents tolerate this too). Useful for
    inspection (a page may carry both a Product JSON-LD and an
    Organization JSON-LD)."""
    out = []
    for match in _JSONLD_BLOCK_RE.finditer(html or ""):
        try:
            parsed = json.loads(match.group(1).strip())
        except (ValueError, AttributeError):
            continue
        out.append(parsed)
    return out


def _find_product_in_jsonld(parsed_blocks: list) -> Optional[Dict[str, Any]]:
    """Find the JSON-LD block whose @type contains 'Product'.
    JSON-LD allows @type to be either a string or array, so handle
    both."""
    for block in parsed_blocks:
        if not isinstance(block, dict):
            continue
        # Handle @graph (multiple objects in one block) too
        items = (
            block.get("@graph") if isinstance(block.get("@graph"), list)
            else [block]
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("@type")
            if isinstance(t, str) and "Product" in t:
                return item
            if isinstance(t, list) and any(
                isinstance(s, str) and "Product" in s for s in t
            ):
                return item
    return None


def _extract_brand_from_jsonld(item: Dict[str, Any]) -> Optional[str]:
    """JSON-LD's brand field can be a string OR an Organization-
    nested object. Extract the string in either shape."""
    brand = item.get("brand")
    if isinstance(brand, str):
        return brand.strip() or None
    if isinstance(brand, dict):
        name = brand.get("name")
        if isinstance(name, str):
            return name.strip() or None
    return None


async def run_frontend_agent_cite(
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

    base = get_agent_pivota_base_url()
    pdp_url = f"{base}/products/{sig_id}"
    expected_brand = (product.get("brand") or "").strip()

    headers = {
        "User-Agent": _AGENT_USER_AGENT,
        "Accept": (
            "application/ld+json,application/json,"
            "text/html;q=0.9"
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(pdp_url, headers=headers)
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
    content_type = response.headers.get("content-type", "")
    body = response.text

    evidence: Dict[str, Any] = {
        "pdp_url": pdp_url,
        "status_code": status_code,
        "content_type": content_type,
        "expected_brand": expected_brand,
    }

    if status_code == 404:
        return VerifierResult(
            status="blocked",
            error_message="pdp_404_for_agent_request",
            evidence_jsonb=evidence,
        )
    if status_code >= 500:
        return VerifierResult(
            status="failed",
            error_message=f"upstream_5xx_{status_code}",
            evidence_jsonb=evidence,
        )
    if not (200 <= status_code < 300):
        return VerifierResult(
            status="failed",
            error_message=f"unexpected_status_{status_code}",
            evidence_jsonb=evidence,
        )

    # Two paths depending on what the endpoint serves:
    # (a) content-type application/ld+json — body IS the JSON-LD
    # (b) text/html — extract JSON-LD blocks from the HTML
    parsed_blocks: list = []
    if "application/ld+json" in content_type.lower() or "application/json" in content_type.lower():
        try:
            data = json.loads(body)
            parsed_blocks = [data] if isinstance(data, dict) else (
                data if isinstance(data, list) else []
            )
        except ValueError:
            parsed_blocks = []
    else:
        parsed_blocks = _parse_jsonld_blocks(body)

    evidence["jsonld_block_count"] = len(parsed_blocks)

    if not parsed_blocks:
        return VerifierResult(
            status="failed",
            error_message="no_jsonld_blocks_found",
            evidence_jsonb=evidence,
        )

    product_item = _find_product_in_jsonld(parsed_blocks)
    if product_item is None:
        evidence["has_jsonld_structured_data"] = False
        return VerifierResult(
            status="failed",
            error_message="no_product_typed_jsonld",
            evidence_jsonb=evidence,
        )

    evidence["has_jsonld_structured_data"] = True
    extracted_brand = _extract_brand_from_jsonld(product_item)
    evidence["extracted_brand"] = extracted_brand

    brand_match = False
    if extracted_brand and expected_brand:
        # Case-insensitive match; tolerates "Test Brand" vs "test brand"
        brand_match = (
            extracted_brand.lower() == expected_brand.lower()
        )
    evidence["brand_match"] = brand_match

    if brand_match:
        return VerifierResult(
            status="succeeded", evidence_jsonb=evidence,
        )

    # JSON-LD present but brand doesn't match (or brand missing in
    # JSON-LD). Retryable — PDP regenerates from canonical data so
    # brand drift should be transient.
    return VerifierResult(
        status="failed",
        error_message=(
            f"brand_mismatch_or_missing: "
            f"expected={expected_brand!r} got={extracted_brand!r}"
        ),
        evidence_jsonb=evidence,
    )


register_verifier("frontend_agent_cite", run_frontend_agent_cite)
