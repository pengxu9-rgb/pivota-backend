"""P2-5 (operator review 2026-07-10): unwrap Vertex grounding redirect URIs.

Gemini grounding wraps every cited URL in an opaque, signed
``vertexaisearch.cloud.google.com/grounding-api-redirect/...`` URI. The report
layer already resolves the HOST via the chunk title (scoring/matching is
redirect-aware), but the operator-facing payloads (``failing_prompts``,
``verbatim_grounding_evidence``, custom-prompt evidence, win-plan
``grounds_in`` joins) carried the raw redirector — a 250-char blob the
merchant can't read, and which Google expires after a while, so the evidence
trail goes dead.

The redirector answers a plain, unauthenticated HTTP 302 with the real
article URL in ``Location`` (verified live 2026-07-11). This module resolves
each unique redirect ONCE per process (bounded cache), best-effort with a
short timeout, and rewrites the probe-run dicts in place before any report
builder consumes them — so every downstream surface stores the real,
permanent publisher URL.

Containment: we only ever request URIs whose host is a known Vertex
redirector (a fixed Google host — never a merchant- or model-supplied
arbitrary URL), we do not follow the redirect, and we only accept an
http(s) ``Location``.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from services.audit_facts import _VERTEX_REDIRECTOR_HOSTS
from utils.logger import logger

# Ops kill-switch: the resolver adds (cached, bounded) network calls to the
# report build; disable without a deploy if the redirector ever misbehaves.
_ENABLED = (os.getenv("AUDIT_RESOLVE_GROUNDING_REDIRECTS", "true").strip().lower()
            not in {"0", "false", "no", "off"})
_TIMEOUT_S = float(os.getenv("AUDIT_GROUNDING_REDIRECT_TIMEOUT_S", "5") or 5)
_CONCURRENCY = 8
_CACHE_MAX = 4096

# uri -> resolved url (None = resolution failed; don't retry this process)
_RESOLVE_CACHE: Dict[str, Optional[str]] = {}


def is_vertex_redirect(uri: Any) -> bool:
    if not isinstance(uri, str) or not uri.startswith(("http://", "https://")):
        return False
    try:
        host = (urlsplit(uri).hostname or "").lower()
    except ValueError:
        return False
    return host in _VERTEX_REDIRECTOR_HOSTS


async def _resolve_one(client: Any, uri: str) -> Optional[str]:
    try:
        resp = await client.get(uri, follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = str(resp.headers.get("location") or "").strip()
            if location.startswith(("http://", "https://")):
                return location
    except Exception:  # noqa: BLE001 — best-effort; the raw URI stays
        return None
    return None


async def resolve_redirect_uris(uris: Iterable[str]) -> Dict[str, str]:
    """Resolve unique Vertex redirect URIs to their real destination URLs.
    Returns only the successes; failures are cached as unresolvable for this
    process so a dead redirector can't stall every report build."""
    pending = []
    seen = set()
    for uri in uris:
        if uri in seen or not is_vertex_redirect(uri):
            continue
        seen.add(uri)
        if uri not in _RESOLVE_CACHE:
            pending.append(uri)

    if pending and _ENABLED:
        import httpx

        if len(_RESOLVE_CACHE) > _CACHE_MAX:
            _RESOLVE_CACHE.clear()
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def worker(client: Any, uri: str) -> None:
            async with sem:
                _RESOLVE_CACHE[uri] = await _resolve_one(client, uri)

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                await asyncio.gather(*(worker(client, u) for u in pending))
        except Exception as exc:  # noqa: BLE001 — never sink the report build
            logger.warning("grounding redirect resolution failed: %s", exc)

    return {
        uri: resolved
        for uri in seen
        if (resolved := _RESOLVE_CACHE.get(uri))
    }


async def resolve_grounding_redirects_in_runs(runs: Iterable[Dict[str, Any]]) -> int:
    """Rewrite Vertex redirect URIs to real URLs across probe-run dicts,
    in place: ``grounding_sources[].uri`` and ``grounding_chunks[]`` strings.
    Returns the number of URIs rewritten. Unresolvable URIs are left as-is
    (current behavior — the title still carries the host)."""
    run_list: List[Dict[str, Any]] = [r for r in runs if isinstance(r, dict)]
    uris: List[str] = []
    for run in run_list:
        for source in run.get("grounding_sources") or []:
            if isinstance(source, dict):
                uris.append(source.get("uri") or "")
        for chunk in run.get("grounding_chunks") or []:
            if isinstance(chunk, str):
                uris.append(chunk)

    resolved = await resolve_redirect_uris(uris)
    if not resolved:
        return 0

    rewritten = 0
    for run in run_list:
        for source in run.get("grounding_sources") or []:
            if isinstance(source, dict) and source.get("uri") in resolved:
                source["uri"] = resolved[source["uri"]]
                rewritten += 1
        chunks = run.get("grounding_chunks")
        if isinstance(chunks, list):
            for i, chunk in enumerate(chunks):
                if isinstance(chunk, str) and chunk in resolved:
                    chunks[i] = resolved[chunk]
                    rewritten += 1
    return rewritten
