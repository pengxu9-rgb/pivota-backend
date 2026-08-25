"""SitemapFreshnessAgent (PR-4b) — diff merchant's catalog vs their
published sitemap.

When a merchant's catalog has products that aren't in their sitemap,
those products are invisible to grounded LLM search (Gemini can't
crawl pages it doesn't know about). Conversely, sitemap URLs that
aren't in the catalog suggest stale references — pages Google may
crawl but the merchant has discontinued.

This agent computes the diff and surfaces it as evidence. Future
extensions (out of scope for PR-4b):
  - Auto-republish for Pivota-managed sitemaps (agent.pivota.cc/...)
  - Emit a human task with the exact diff for merchant-managed
    sitemaps (PR-6 territory — task queue ships there)

V1 scope: pure diagnostic. Catalog vs sitemap diff + freshness score.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from services import crawl_politeness
from services.executor_agents.base import (
    BaseExecutorAgent,
    ExecutorContext,
    ExecutorResult,
)

logger = logging.getLogger(__name__)


_SITEMAP_FETCH_TIMEOUT_S = 8.0
# Named because the politeness gate must be asked about the SAME agent we then send.
_SITEMAP_UA = "Pivota-SitemapMonitor/1.0"
_SITEMAP_MAX_BYTES = 5_000_000

# Cap evidence sample lists so the executor_runs.evidence_jsonb stays
# reasonably sized. The first 20 missing/orphan URLs are enough for
# the merchant to act on; total counts give the headline.
_EVIDENCE_SAMPLE_CAP = 20


def _normalize_url_for_diff(url: Optional[str]) -> Optional[str]:
    """Canonicalize a URL for set-membership comparison.

    Strips scheme + www. + trailing slash + query params + fragment.
    Lowercases the result. Returns None for unparseable input.

    The diff layer needs (host, path) equivalence — query strings
    (utm_source, ?variant=) and trailing slashes shouldn't make
    "same product" look different.
    """
    if not url or not isinstance(url, str):
        return None
    raw = url.strip()
    if not raw:
        return None
    try:
        # urlparse needs a scheme to populate netloc; default to https.
        if "://" not in raw:
            raw = "https://" + raw
        p = urlparse(raw)
    except (ValueError, TypeError):
        return None
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    path = (p.path or "/").rstrip("/") or "/"
    return f"{host}{path}".lower()


def _parse_sitemap_xml(content: bytes) -> List[str]:
    """Pull all <loc> URLs out of a sitemap XML. Tolerant of
    namespace prefixes and malformed XML — falls back to regex if
    ElementTree fails."""
    try:
        root = ET.fromstring(content)
        urls: List[str] = []
        for loc in root.iter():
            if loc.tag.split("}")[-1].lower() == "loc" and loc.text:
                urls.append(loc.text.strip())
        return urls
    except ET.ParseError:
        return [
            m.decode("utf-8", errors="replace").strip()
            for m in re.findall(rb"<loc>([^<]+)</loc>", content)
        ]


async def _fetch_sitemap_urls_recursive(
    sitemap_url: str,
    *,
    max_child_sitemaps: int = 5,
) -> Tuple[List[str], Optional[str]]:
    """Fetch a sitemap; if it's an index, fetch up to N child sitemaps.
    Returns (flat_url_list, error_or_None)."""
    try:
        async with httpx.AsyncClient(
            timeout=_SITEMAP_FETCH_TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            # Shared politeness gate — these requests leave from the same reserved NAT
            # address as every other crawl lane, so a ban here takes all of them down.
            # `max_wait=0`: batch work, so wait a backoff out rather than drop the domain.
            await crawl_politeness.before_request(
                sitemap_url, user_agent=_SITEMAP_UA, max_wait=0
            )
            r = await client.get(
                sitemap_url,
                headers={"User-Agent": _SITEMAP_UA},
            )
            crawl_politeness.note_response(
                sitemap_url, r.status_code, retry_after=r.headers.get("retry-after")
            )
            if r.status_code != 200:
                return [], f"http_{r.status_code}"
            urls = _parse_sitemap_xml(r.content[:_SITEMAP_MAX_BYTES])

            # Recurse one level into sitemap-index children.
            def _is_child_sitemap(u: str) -> bool:
                try:
                    p = urlparse(u).path or ""
                except Exception:  # noqa: BLE001
                    return False
                return p.lower().endswith(".xml") and "sitemap" in p.lower()

            child_indexes = [u for u in urls if _is_child_sitemap(u)]
            if child_indexes:
                aggregated = [u for u in urls if not _is_child_sitemap(u)]
                for child_url in child_indexes[:max_child_sitemaps]:
                    try:
                        # Child indexes are the same host, so they queue behind the parent
                        # rather than firing as a burst the moment it returns.
                        await crawl_politeness.before_request(
                            child_url, user_agent=_SITEMAP_UA, max_wait=0
                        )
                        cr = await client.get(
                            child_url,
                            headers={"User-Agent": _SITEMAP_UA},
                        )
                        crawl_politeness.note_response(
                            child_url, cr.status_code,
                            retry_after=cr.headers.get("retry-after"),
                        )
                        if cr.status_code == 200:
                            aggregated.extend(
                                _parse_sitemap_xml(cr.content[:_SITEMAP_MAX_BYTES])
                            )
                    except Exception:  # noqa: BLE001
                        continue
                urls = aggregated
            return urls, None
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        return [], f"fetch_error: {exc.__class__.__name__}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("sitemap fetch error for %s: %s", sitemap_url, exc)
        return [], f"unexpected: {exc.__class__.__name__}"


async def _fetch_merchant_catalog_urls(
    merchant_id: str,
) -> List[str]:
    """Pull canonical_url for every active catalog_products row for
    this merchant. Returns raw URLs (normalization happens at the
    diff step)."""
    from db.database import database
    try:
        rows = await database.fetch_all(
            """
            SELECT canonical_url
            FROM catalog_products
            WHERE merchant_id = :merchant_id
              AND canonical_url IS NOT NULL
              AND canonical_url <> ''
            """,
            {"merchant_id": merchant_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sitemap_freshness: catalog query failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
        return []
    return [r["canonical_url"] for r in (rows or []) if r.get("canonical_url")]


def _derive_merchant_host(catalog_urls: List[str]) -> Optional[str]:
    """Pick the most-common host across catalog URLs. The merchant's
    sitemap lives at https://{this_host}/sitemap.xml."""
    if not catalog_urls:
        return None
    counter: Dict[str, int] = {}
    for u in catalog_urls:
        try:
            host = urlparse(u).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host:
                counter[host] = counter.get(host, 0) + 1
        except Exception:  # noqa: BLE001
            continue
    if not counter:
        return None
    return max(counter.items(), key=lambda kv: kv[1])[0]


def _compute_freshness_score(
    catalog_count: int,
    missing_count: int,
    orphan_count: int,
) -> float:
    """0.0-1.0 freshness score. 1.0 = perfect alignment.

    We weight `missing_from_sitemap` (catalog products NOT in sitemap)
    more heavily than `orphan_in_sitemap` (sitemap URLs not in catalog),
    because missing products mean Gemini can't find them — direct
    visibility loss. Orphans are mostly just SEO noise.
    """
    if catalog_count == 0:
        return 1.0  # vacuous truth — no products to compare
    miss_ratio = min(1.0, missing_count / catalog_count)
    orphan_ratio = min(1.0, orphan_count / max(catalog_count, 1))
    score = 1.0 - (0.7 * miss_ratio) - (0.3 * orphan_ratio)
    return max(0.0, round(score, 3))


def _classify_severity(missing_ratio: float, orphan_ratio: float) -> str:
    """Three-tier severity for the human reading the evidence."""
    if missing_ratio >= 0.20 or orphan_ratio >= 0.50:
        return "high"
    if missing_ratio >= 0.05 or orphan_ratio >= 0.20:
        return "medium"
    return "low"


class SitemapFreshnessAgent(BaseExecutorAgent):
    """Diff merchant's catalog against their published sitemap; surface
    drift as evidence."""

    name = "sitemap_freshness_monitor"

    async def should_run(self, context: ExecutorContext) -> bool:
        """Run when:
          - merchant_id is set
          - merchant has at least one catalog_products row with a
            canonical_url (otherwise nothing to compare)
        We do NOT pre-check that the sitemap is reachable — that
        check happens in execute() because a 404'd sitemap IS a
        finding the agent should report."""
        if not context.merchant_id:
            return False
        catalog_urls = await _fetch_merchant_catalog_urls(context.merchant_id)
        return len(catalog_urls) > 0

    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        if not context.merchant_id:
            return ExecutorResult(
                status="skipped", error_message="merchant_id required",
            )
        catalog_urls = await _fetch_merchant_catalog_urls(context.merchant_id)
        if not catalog_urls:
            return ExecutorResult(
                status="skipped",
                evidence={"reason": "no catalog_products with canonical_url"},
            )

        merchant_host = _derive_merchant_host(catalog_urls)
        if not merchant_host:
            return ExecutorResult(
                status="failed",
                error_message="could not derive merchant host from catalog URLs",
                evidence={"catalog_url_count": len(catalog_urls)},
            )

        sitemap_url = f"https://{merchant_host}/sitemap.xml"
        sitemap_urls, fetch_error = await _fetch_sitemap_urls_recursive(sitemap_url)
        if fetch_error:
            # Sitemap unreachable IS a finding — surface it as a failed
            # result with concrete diagnostic. The merchant needs to know
            # their sitemap.xml isn't accessible.
            return ExecutorResult(
                status="failed",
                error_message=f"sitemap_unreachable: {fetch_error}",
                evidence={
                    "merchant_host": merchant_host,
                    "sitemap_url": sitemap_url,
                    "fetch_error": fetch_error,
                    "catalog_url_count": len(catalog_urls),
                },
            )

        # Normalize both sides for set-membership comparison.
        catalog_norm: Set[str] = set()
        catalog_norm_to_raw: Dict[str, str] = {}
        for raw in catalog_urls:
            norm = _normalize_url_for_diff(raw)
            if norm:
                catalog_norm.add(norm)
                catalog_norm_to_raw.setdefault(norm, raw)

        sitemap_norm: Set[str] = set()
        sitemap_norm_to_raw: Dict[str, str] = {}
        for raw in sitemap_urls:
            norm = _normalize_url_for_diff(raw)
            if norm:
                sitemap_norm.add(norm)
                sitemap_norm_to_raw.setdefault(norm, raw)

        missing_from_sitemap = catalog_norm - sitemap_norm
        orphan_in_sitemap = sitemap_norm - catalog_norm

        # Sample lists (raw URLs preserved so the merchant can copy
        # them straight into their CMS / sitemap generator).
        missing_sample = sorted([
            catalog_norm_to_raw[k] for k in missing_from_sitemap
        ])[:_EVIDENCE_SAMPLE_CAP]
        orphan_sample = sorted([
            sitemap_norm_to_raw[k] for k in orphan_in_sitemap
        ])[:_EVIDENCE_SAMPLE_CAP]

        miss_ratio = len(missing_from_sitemap) / max(len(catalog_norm), 1)
        orphan_ratio = len(orphan_in_sitemap) / max(len(catalog_norm), 1)
        score = _compute_freshness_score(
            catalog_count=len(catalog_norm),
            missing_count=len(missing_from_sitemap),
            orphan_count=len(orphan_in_sitemap),
        )
        severity = _classify_severity(miss_ratio, orphan_ratio)

        evidence = {
            "merchant_host": merchant_host,
            "sitemap_url": sitemap_url,
            "catalog_url_count": len(catalog_norm),
            "sitemap_url_count": len(sitemap_norm),
            "missing_from_sitemap_count": len(missing_from_sitemap),
            "orphan_in_sitemap_count": len(orphan_in_sitemap),
            "missing_from_sitemap_sample": missing_sample,
            "orphan_in_sitemap_sample": orphan_sample,
            "freshness_score": score,
            "severity": severity,
        }
        # P1.1: when the sitemap has drift, the fix is merchant work
        # (Pivota can't edit merchant-managed sitemaps directly). Emit
        # human_task_recommended so the dispatcher materializes a
        # merchant_task. When the sitemap is clean (no drift), emit
        # no_op so we don't spam the queue.
        from services.executor_agents.base import (
            RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
            RESULT_TYPE_NO_OP,
        )
        has_drift = (
            len(missing_from_sitemap) > 0 or len(orphan_in_sitemap) > 0
        )
        if not has_drift:
            return ExecutorResult(
                status="succeeded",
                evidence=evidence,
                result_type=RESULT_TYPE_NO_OP,
            )
        # Drift detected — frame the human task
        title_parts = []
        if len(missing_from_sitemap) > 0:
            title_parts.append(
                f"add {len(missing_from_sitemap)} missing PDP"
                f"{'s' if len(missing_from_sitemap) != 1 else ''}"
            )
        if len(orphan_in_sitemap) > 0:
            title_parts.append(
                f"remove {len(orphan_in_sitemap)} orphan URL"
                f"{'s' if len(orphan_in_sitemap) != 1 else ''}"
            )
        task_title = (
            f"Sitemap drift on {merchant_host}: " + " + ".join(title_parts)
        )
        body_lines = [
            f"Sitemap freshness audit found drift between {merchant_host}'s "
            f"sitemap.xml ({len(sitemap_norm)} URLs) and your live "
            f"catalog ({len(catalog_norm)} URLs)."
        ]
        if missing_sample:
            body_lines.append(
                f"\nMissing from sitemap "
                f"(first {len(missing_sample)} of "
                f"{len(missing_from_sitemap)}): "
                + ", ".join(missing_sample[:5])
                + ("..." if len(missing_sample) > 5 else "")
            )
        if orphan_sample:
            body_lines.append(
                f"\nOrphan URLs in sitemap "
                f"(first {len(orphan_sample)} of "
                f"{len(orphan_in_sitemap)}): "
                + ", ".join(orphan_sample[:5])
                + ("..." if len(orphan_sample) > 5 else "")
            )
        return ExecutorResult(
            status="succeeded",
            evidence=evidence,
            result_type=RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
            task_title=task_title,
            task_body="\n".join(body_lines),
            task_severity=severity,
            task_owner="merchant_tech_team",
            task_lever="sitemap_maintenance",
        )
