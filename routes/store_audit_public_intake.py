"""Public Store Audit intake and teaser for the marketing funnel.

POST /public/store-audit/intake   — accept one store URL, queue one anonymous
                                    UCP probe for its domain (or reuse fresh
                                    evidence), and answer with a poll state.
GET  /public/store-audit/teaser   — redacted, domain-level poll result the
                                    marketing page renders pre-registration.

Deliberate bounds:
  * UCP probe lane ONLY. The commerce browser probe refuses prospects by
    design and must never be reachable from anonymous input.
  * Everything is domain-keyed; no merchant identity is read, written, or
    returned. Conversion later claims the same route (migration 196).
  * A cold domain gets a route_kind=ucp_discovery placeholder because the real
    MCP endpoint is unknowable here; the receipt path owns the transition to a
    real "ucp" route and the reprobe selector never picks placeholders up.
  * A probe that could not run ("blocked"/"failed") answers "inconclusive",
    never "not agent-ready" — cannot-verify must not buy a negative claim.
  * A negative stays served until it ages past the reprobe TTL; re-running the
    form is not a lever to re-probe someone's store on demand.
  * Default-off behind STORE_AUDIT_PUBLIC_INTAKE_ENABLED; disabled is a 404
    indistinguishable from the route not existing.
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from db.audit_evidence import (
    EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
    ROUTE_KIND_UCP,
    ROUTE_KIND_UCP_DISCOVERY,
    VERIFIER_UCP_PROBE,
    count_recent_intake_verifications,
    enqueue_verification_run,
    fetch_latest_route_evidence_for_domain,
    fetch_latest_verification_for_domain,
    fetch_route_for_domain,
    upsert_execution_route,
)

router = APIRouter(prefix="/public/store-audit", tags=["store-audit-public"])

_INTAKE_IDEMPOTENCY_PREFIX = "public_intake:"
_ACTIVE_RUN_STATUSES = ("pending", "claimed")

# Hosts that can never be a public storefront. The probe worker enforces the
# real SSRF policy at egress; this list only keeps junk out of the queue.
_BLOCKED_HOST_SUFFIXES = (
    ".local", ".localhost", ".internal", ".test", ".invalid", ".example",
    ".corp", ".home.arpa", ".lan",
)
_HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _enabled() -> bool:
    return (
        os.getenv("STORE_AUDIT_PUBLIC_INTAKE_ENABLED", "false")
        .strip().lower() == "true"
    )


def _require_enabled() -> None:
    if not _enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _daily_cap() -> int:
    try:
        value = int(os.getenv("STORE_AUDIT_PUBLIC_INTAKE_DAILY_CAP", "200"))
    except ValueError:
        return 200
    return max(0, value)


def _negative_ttl_hours() -> int:
    # Shares the reprobe lane's freshness horizon so a store that adds UCP
    # later becomes probeable again on the same schedule the lane re-verifies.
    try:
        value = int(os.getenv("STORE_AUDIT_UCP_REPROBE_TTL_HOURS", "168"))
    except ValueError:
        return 168
    return min(720, max(1, value))


def normalize_store_domain(value: str) -> Optional[str]:
    """One lower-cased public host from user input, or None.

    Accepts "yourstore.com", "https://yourstore.com/path", "www.yourstore.com".
    Rejects IPs, credentials, ports, single-label hosts, and non-public TLDs.
    The leading "www." is stripped so the funnel and the probe worker key the
    same domain.
    """
    raw = (value or "").strip()
    if not raw or len(raw) > 512:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    try:
        if parsed.username or parsed.password or parsed.port:
            return None
    except ValueError:
        return None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or len(host) > 253:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) < 2:
        return None
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        return None
    if any(host.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES):
        return None
    return host


class _SlidingWindowLimiter:
    """Best-effort per-instance limiter for an anonymous endpoint.

    In-memory on purpose: the durable bounds are the per-route unique pending
    index (196) and the DB-counted daily cap. This only blunts single-source
    bursts against one instance.
    """

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = {}

    def allow(self, key: str, *, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(key, deque())
        while bucket and current - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(current)
        # Unbounded key growth is the classic in-memory limiter leak.
        if len(self._hits) > 10_000:
            self._hits.clear()
        return True


_intake_limiter = _SlidingWindowLimiter(limit=5, window_seconds=60.0)
_teaser_limiter = _SlidingWindowLimiter(limit=30, window_seconds=60.0)


def _client_key(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    client = request.client
    return (client.host if client else "unknown")[:64]


def _require_rate(limiter: _SlidingWindowLimiter, request: Request) -> None:
    if not limiter.allow(_client_key(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "RATE_LIMITED"},
        )


class PublicIntakeRequest(BaseModel):
    store_url: str = Field(..., min_length=4, max_length=512)


class PublicTeaserResponse(BaseModel):
    domain: str
    state: Literal["unknown", "pending", "ready", "inconclusive"]
    agent_ready: Optional[bool] = None
    evidence_level: Optional[Literal["detected", "tested"]] = None
    checked_at: Optional[datetime] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _teaser_for_domain(domain: str) -> PublicTeaserResponse:
    evidence = await fetch_latest_route_evidence_for_domain(
        normalized_domain=domain,
        evidence_type=EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
        route_kinds=(ROUTE_KIND_UCP,),
    )
    had_positive = evidence is not None
    if evidence is not None:
        expires_at = _as_aware(evidence.get("expires_at"))
        if expires_at is None or expires_at > _now():
            level = evidence.get("evidence_level")
            return PublicTeaserResponse(
                domain=domain,
                state="ready",
                agent_ready=True,
                evidence_level=level if level in ("detected", "tested") else None,
                checked_at=evidence.get("created_at"),
            )

    run = await fetch_latest_verification_for_domain(
        normalized_domain=domain,
        verifier_id=VERIFIER_UCP_PROBE,
        route_kinds=(ROUTE_KIND_UCP, ROUTE_KIND_UCP_DISCOVERY),
    )
    if run is None:
        return PublicTeaserResponse(domain=domain, state="unknown")
    run_status = str(run.get("status") or "")
    if run_status in _ACTIVE_RUN_STATUSES:
        return PublicTeaserResponse(domain=domain, state="pending")
    if run_status == "succeeded":
        if had_positive:
            # The positive expired: stale, re-probeable — never a negative.
            return PublicTeaserResponse(domain=domain, state="unknown")
        return PublicTeaserResponse(
            domain=domain,
            state="ready",
            agent_ready=False,
            checked_at=run.get("completed_at") or run.get("created_at"),
        )
    return PublicTeaserResponse(domain=domain, state="inconclusive")


def _negative_is_fresh(teaser: PublicTeaserResponse) -> bool:
    checked = _as_aware(teaser.checked_at)
    if checked is None:
        return True
    return _now() - checked < timedelta(hours=_negative_ttl_hours())


@router.post(
    "/intake",
    response_model=PublicTeaserResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def public_store_audit_intake(
    payload: PublicIntakeRequest, request: Request,
) -> PublicTeaserResponse:
    """Queue (or reuse) one anonymous UCP probe for a prospect domain."""
    _require_enabled()
    _require_rate(_intake_limiter, request)
    domain = normalize_store_domain(payload.store_url)
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_STORE_URL"},
        )

    teaser = await _teaser_for_domain(domain)
    if teaser.state == "pending":
        return teaser
    if teaser.state == "ready" and (
        teaser.agent_ready or _negative_is_fresh(teaser)
    ):
        return teaser

    today = _now()
    midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
    used = await count_recent_intake_verifications(
        idempotency_prefix=_INTAKE_IDEMPOTENCY_PREFIX, since=midnight,
    )
    if used >= _daily_cap():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "DAILY_CAP_REACHED"},
        )

    # Prefer the real route (its endpoint identity satisfies the strict
    # receipt check); fall back to an existing or fresh discovery placeholder.
    route = await fetch_route_for_domain(
        normalized_domain=domain, route_kinds=(ROUTE_KIND_UCP,),
    )
    if route is None:
        route = await fetch_route_for_domain(
            normalized_domain=domain, route_kinds=(ROUTE_KIND_UCP_DISCOVERY,),
        )
    if route is None:
        route = await upsert_execution_route(
            normalized_domain=domain,
            route_kind=ROUTE_KIND_UCP_DISCOVERY,
            # Synthetic stand-in; the receipt path replaces this identity.
            # merchant_id stays NULL — a prospect is not a merchant.
            endpoint=f"https://{domain}/",
            audit_run_id=str(uuid.uuid4()),
        )
    if route is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "INTAKE_UNAVAILABLE"},
        )

    audit_run_id = str(route.get("last_audit_run_id") or "") or str(uuid.uuid4())
    await enqueue_verification_run(
        audit_run_id=audit_run_id,
        verifier_id=VERIFIER_UCP_PROBE,
        execution_route_id=str(route.get("execution_route_id") or ""),
        max_retries=1,
        idempotency_key=(
            f"{_INTAKE_IDEMPOTENCY_PREFIX}{domain}:{today.strftime('%Y%m%d')}"
        ),
    )
    # A failed enqueue here is overwhelmingly the 196 unique partial index
    # refusing a second active run for the route — someone else just queued
    # the same domain. Either way the honest answer is "pending".
    return PublicTeaserResponse(domain=domain, state="pending")


@router.get("/teaser", response_model=PublicTeaserResponse)
async def public_store_audit_teaser(
    request: Request,
    store_url: str = Query(..., min_length=4, max_length=512),
) -> PublicTeaserResponse:
    """Redacted domain-level poll state for the marketing page."""
    _require_enabled()
    _require_rate(_teaser_limiter, request)
    domain = normalize_store_domain(store_url)
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_STORE_URL"},
        )
    return await _teaser_for_domain(domain)
