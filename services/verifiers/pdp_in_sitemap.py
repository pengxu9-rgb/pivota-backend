"""P5.3 verifier: pdp_in_sitemap.

Asserts the merchant's Pivota canonical PDP URL is listed in
agent.pivota.cc's sitemap so Google can index it.

Sitemap location: agent.pivota.cc/sitemap-products.xml (rendered
by the pivota-agent-ui repo from the backend's
GET /products list endpoint).

Evidence:
  - sitemap_url
  - sitemap_status_code
  - pdp_url
  - found_in_sitemap (bool)
  - sitemap_url_count (total URLs in the sitemap — sanity check
    that the sitemap is not empty)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

import httpx

from services.verification_run_worker import (
    VerifierContext, VerifierResult, register_verifier,
)
from services.verifiers._product_context import (
    get_agent_pivota_base_url, load_product_context,
)

logger = logging.getLogger(__name__)


_TIMEOUT_S = 15.0

# Sitemap path. Lives on agent.pivota.cc (frontend repo); the
# backend doesn't render this — it just exposes the
# /products list endpoint the frontend consumes.
_SITEMAP_PATH = "/sitemap-products.xml"

# Regex to extract URLs from a sitemap (handles namespaces).
_URL_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


async def run_pdp_in_sitemap(
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

    pdp_url = (product.get("pivota_canonical_url") or "").strip()
    sig_id = product.get("pivota_signature_id")
    if not pdp_url or not sig_id:
        return VerifierResult(
            status="blocked",
            error_message=(
                "product has no pivota_canonical_url or signature"
            ),
            evidence_jsonb={
                "has_canonical_url": bool(pdp_url),
                "has_sig_id": bool(sig_id),
            },
        )

    base = get_agent_pivota_base_url()
    sitemap_url = f"{base}{_SITEMAP_PATH}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(sitemap_url)
    except httpx.TimeoutException as exc:
        return VerifierResult(
            status="failed",
            error_message=f"timeout: {exc!r}",
            evidence_jsonb={"sitemap_url": sitemap_url},
        )
    except httpx.RequestError as exc:
        return VerifierResult(
            status="failed",
            error_message=f"http_request: {exc!r}",
            evidence_jsonb={"sitemap_url": sitemap_url},
        )

    status_code = response.status_code
    body = response.text

    evidence: Dict[str, Any] = {
        "sitemap_url": sitemap_url,
        "sitemap_status_code": status_code,
        "pdp_url": pdp_url,
        "sig_id": sig_id,
    }

    # 404 / 5xx — sitemap not available right now. Retry path
    # rather than blocked: the sitemap is regenerated on deploy +
    # may transiently 404 between regenerations.
    if status_code >= 400:
        return VerifierResult(
            status="failed",
            error_message=f"sitemap_status_{status_code}",
            evidence_jsonb=evidence,
        )

    # Parse the URLs. We don't need full XML parsing — regex over
    # <loc> tags is sufficient for sitemaps (which are
    # well-formed by spec).
    found_urls = _URL_RE.findall(body)
    evidence["sitemap_url_count"] = len(found_urls)

    # Match either the full pivota_canonical_url OR the sig_id
    # suffix (some sitemaps may carry trailing slashes / query
    # params that differ from our stored canonical_url).
    found_in_sitemap = False
    for u in found_urls:
        if pdp_url == u or pdp_url == u.rstrip("/") or u.endswith(sig_id):
            found_in_sitemap = True
            break
    evidence["found_in_sitemap"] = found_in_sitemap

    if found_in_sitemap:
        return VerifierResult(
            status="succeeded", evidence_jsonb=evidence,
        )

    # Not in the sitemap — retryable. The sitemap regenerates
    # periodically; a recently-minted PDP may take one regen
    # cycle to appear. After exhausted_retries, ops can investigate
    # whether the sitemap generator is healthy.
    return VerifierResult(
        status="failed",
        error_message="pdp_not_in_sitemap",
        evidence_jsonb=evidence,
    )


register_verifier("pdp_in_sitemap", run_pdp_in_sitemap)
