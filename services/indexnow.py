"""IndexNow submission — tell participating search engines (Bing, which powers
ChatGPT search; Yandex; and others) that a canonical PDP exists or changed, so it
gets crawled.

This is our live discovery trigger for the canonical PDP surface. Unlike the GSC
Indexing API (services.gsc_integration, gated off pending proof Google honors
product-URL submissions — ADR-006), IndexNow is honored for ordinary content
pages, so it actually gets our schema.org-marked PDPs crawled. Google does NOT
honor IndexNow — that path is a Search Console sitemap submission.

The key is a PUBLIC file hosted at https://{host}/{key}.txt (pivota-agent-ui
public/). IndexNow validates a submission by fetching keyLocation and checking its
body == key, so the key file must be deployed for submissions to be accepted.

Everything here is best-effort and fail-open: a submission failure (or the flag
being off) never affects the caller. Firing happens on a false->true serving-
eligibility transition (see index_pipeline_state_service.recompute_serving_
eligibility), so a page is submitted exactly when it becomes newly citable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable, List, Set

from config.settings import settings

logger = logging.getLogger(__name__)

# Live tasks scheduled via schedule_submit_url; held so they are not GC'd before
# they run (asyncio only keeps weak refs to tasks).
_pending_tasks: Set["asyncio.Task"] = set()


def _key_location() -> str:
    return f"https://{settings.indexnow_host}/{settings.indexnow_key}.txt"


def _clean_same_host(urls: Iterable[str]) -> List[str]:
    host_prefix = f"https://{settings.indexnow_host}/"
    seen: Set[str] = set()
    out: List[str] = []
    for u in urls:
        u = (u or "").strip()
        # IndexNow requires every URL to be on the submitting host.
        if u.startswith(host_prefix) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def submit_urls(urls: Iterable[str]) -> bool:
    """Submit canonical URLs to IndexNow. Returns True iff accepted (HTTP 200/202).
    Never raises; no-ops when disabled or unconfigured."""
    if not settings.indexnow_enabled:
        return False
    if not settings.indexnow_key or not settings.indexnow_host:
        return False

    url_list = _clean_same_host(urls)[:10000]  # IndexNow per-request cap
    if not url_list:
        return False

    payload = {
        "host": settings.indexnow_host,
        "key": settings.indexnow_key,
        "keyLocation": _key_location(),
        "urlList": url_list,
    }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                settings.indexnow_endpoint,
                json=payload,
                headers={"User-Agent": "pivota-indexnow/1.0"},
            )
        ok = resp.status_code in (200, 202)
        (logger.info if ok else logger.warning)(
            {
                "event": "indexnow_submit",
                "count": len(url_list),
                "status": resp.status_code,
                "ok": ok,
            }
        )
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            {
                "event": "indexnow_submit_failed",
                "error": str(exc)[:200],
                "count": len(url_list),
            }
        )
        return False


async def submit_url(url: str) -> bool:
    """Submit a single canonical URL to IndexNow. Never raises."""
    return await submit_urls([url])


def schedule_submit_url(url: str) -> None:
    """Fire a single-URL IndexNow submission WITHOUT blocking the caller — for use
    on hot paths (e.g. eligibility recompute). Best-effort: needs a running event
    loop; silently skips otherwise. The submission itself never raises."""
    if not settings.indexnow_enabled:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop (sync context) — skip; caller stays unaffected
    task = loop.create_task(submit_url(url))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
