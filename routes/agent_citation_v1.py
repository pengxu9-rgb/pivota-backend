"""External citation read API (ADR-007 P0).

`GET /agent/v1/citation/{id}` — the offer-free **CitationItem** projection a
frontier agent reads to *cite* Pivota, distinct from the commerce-bearing
`get_pdp` render. It reuses the agent_pdp_v1 index_eligible read gate + id
resolver + `substantiated_claims`, and NEVER emits offers / price /
merchant-private fields. Public-read, rate-limited per `X-Pivota-Agent` (else
client IP), cacheable.

B④-P1 (attribution telemetry): every inbound read is logged best-effort to
`citation_read_log` off the response hot path (`X-Pivota-Agent` else IP + what
was asked for + outcome) — the external half of "who cites us". Flag-gated
`CITATION_READ_TELEMETRY`, default OFF.

Contract: pivota-merchants-portal/docs/external-citation-api-contract.md
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import String, and_, column, literal_column, select, table

from db.citation_read_log import (
    STATUS_DISABLED,
    STATUS_EMPTY,
    STATUS_HIT,
    STATUS_MISS,
    STATUS_SUPPRESSED,
    log_citation_read,
)
from db.database import database
from middleware.rate_limiter import AdvancedRateLimiter
from routes.agent_pdp_v1 import (
    EXT_RESOLVE_SQL,
    _index_eligible_read_enabled,
    _is_external_product_id,
    _query_for_id,
    _row_to_dict,
    aggregate_rating_from_row as _aggregate_rating,
)
from services.agent_pdp_view_assembler import (
    normalize_taxonomy_tags as _normalize_taxonomy_tags,
)
from services.catalog_sync_service import pivota_canonical_pdp_url
from services.canonical_sitemap_candidates import electable_sig_exists
from services.claim_safety import substantiated_claims
from services.pdp_renderability import compile_pg, sig_pdp_will_render_sql

router = APIRouter(prefix="/agent/v1/citation", tags=["agent-citation"])
logger = logging.getLogger(__name__)

# Migration 181. Local Core handle, same lightweight pattern the canonical
# routes use for this table.
_content_canonical_election = table(
    "content_canonical_election",
    column("content_key", String),
    column("canonical_sig_id", String),
)

CITE_AS = "Pivota — agent.pivota.cc"
SUMMARY_MAX = 200

# Per-source token bucket (in-memory; reuse of the existing limiter). Keyed on
# X-Pivota-Agent when supplied, else client IP. Named-partner tiers (P3) layer
# on later; P0 serves everyone the standard tier.
_limiter = AdvancedRateLimiter()


def _caller_identity(request: Request) -> Tuple[Optional[str], Optional[str]]:
    """The caller's self-declared `X-Pivota-Agent` id (free-form UA-style, e.g.
    `openai-chatgpt/1.0`) and client IP. Single source of truth for both the
    rate-limit key and the B④-P1 attribution telemetry."""
    agent = (request.headers.get("X-Pivota-Agent") or "").strip() or None
    client_ip = request.client.host if request.client else None
    return agent, client_ip


def _telemetry_enabled() -> bool:
    """B④-P1 attribution telemetry flag. Default OFF ⇒ no logging task is ever
    spawned, so the off-path is byte-identical to today. Canary on per repo
    rollout convention."""
    return (os.getenv("CITATION_READ_TELEMETRY") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# Strong refs to in-flight fire-and-forget log tasks so the event loop doesn't
# GC them mid-write (asyncio only holds weak refs to bare tasks).
_log_tasks: "set[asyncio.Task[Any]]" = set()


def _spawn_log(**fields: Any) -> None:
    """Fire-and-forget the best-effort telemetry write OFF the response hot
    path. Never raises into the handler; a flag-OFF or loop-less context is a
    silent no-op."""
    if not _telemetry_enabled():
        return
    try:
        task = asyncio.ensure_future(log_citation_read(**fields))
    except RuntimeError:
        # No running loop (e.g. a sync test path) — drop quietly.
        return
    _log_tasks.add(task)
    task.add_done_callback(_log_tasks.discard)


async def _citation_rate_limit(request: Request) -> None:
    agent, client_ip = _caller_identity(request)
    key = agent or client_ip or "anonymous"
    allowed, meta = await _limiter.check_limit(key, tier="standard")
    if not allowed:
        retry = max(1, int(meta.get("reset", 0) - time.time()))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="citation read rate limit exceeded",
            headers={
                "Retry-After": str(retry),
                "X-RateLimit-Limit": str(meta.get("limit", "")),
                "X-RateLimit-Remaining": str(meta.get("remaining", 0)),
            },
        )


def _first_sentence(text: str, *, cap: int = SUMMARY_MAX) -> str:
    """A one-line summary an agent can quote verbatim — first sentence, capped."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    for sep in (". ", "! ", "? "):
        idx = collapsed.find(sep)
        if 0 < idx <= cap:
            return collapsed[: idx + 1].strip()
    return collapsed[:cap].strip()


def project_citation_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project an agent_pdp_view row into the offer-free CitationItem.

    Invariants: buyable=False, offers=None, catalog_track='citation'. Never
    includes merchant id/email, internal scores, take-rate, or raw competitor
    offers.

    The ``attribution`` BLOCK is always present, but its ``canonical_url`` is
    NULLABLE — null for a row with no minted sig, because there is no followable
    PDP for one. ``source``/``cite_as``/``attribution_required`` are
    unconditional, so attribution to the source survives a null URL. The contract
    doc's "``attribution.canonical_url`` is always present" rule
    (pivota-merchants-portal/docs/external-citation-api-contract.md) predates the
    measurement that the URL form it specified was a guaranteed 500, and needs
    that follow-up edit; the doc's own EXAMPLE already shows the sig form this
    now emits.
    """
    content_key = row.get("content_key")
    description = str(row.get("description") or "")
    claims = substantiated_claims(row.get("evidence_profile"))

    # THE CITED URL MUST BE THE **SIG** FORM. It used to be
    # ``PDP_URL_PREFIX + content_key``, which is a dead URL for EVERY row this
    # endpoint serves — not just the unrenderable ones.
    #
    # MEASURED 2026-07-26 against prod: ``agent.pivota.cc/products/{ck_*}``
    # returned a hard **HTTP 500** (2,007 bytes, no product JSON-LD) on every
    # probe — 135 serial requests over **133 distinct content_keys**, 34 of them
    # rows the sitemap feed calls ``renderable=true`` and whose OWN sig-form URL
    # serves a real 54-67 KB PDP. Reproduced INDEPENDENTLY by an adversarial
    # re-measurement the same day that set out to refute it: 103 further distinct
    # ck ids, 103/103 dead, byte-identical 2,007-byte bodies, **zero 3xx** (so the
    # old behaviour was broken, not merely a redirect), ``x-vercel-cache: MISS``
    # on every one (500s are never cached, so no ck URL can warm into a 200), and
    # a fabricated ``sig_0000…`` returning the SAME 500/2,007 — i.e. a ck id is
    # indistinguishable from a nonexistent signature, exactly as the mechanism
    # below predicts. The gateway's sig-exact resolve keys on
    # ``cp.pivota_signature_id = $1`` (PIVOTA-Agent ``src/server.js``,
    # ``resolveCatalogProductRefFromPivotaSignatureInner``); nothing there matches
    # a ``ck_*`` id, so the ISR page route turns the miss into a 500. agent-ui
    # already learned this and fixed its half — ``scripts/sitemap_lib.mjs`` drops
    # any row whose ``sig_id`` fails ``/^sig_.+/`` and notes that "get_pdp_v2
    # rejects a bare content_key with MISSING_MERCHANT_CONTEXT". This endpoint was
    # the half that never got the memo.
    #
    # Why this is the worst place for that bug: the response also carries
    # ``attribution_required: true``, so we tell a frontier agent it MUST
    # attribute, and hand it a URL that 500s. ADR-007's premise is that a
    # citation is followable; this made every citation unfollowable.
    #
    # Built via ``pivota_canonical_pdp_url`` rather than a local prefix so the
    # URL is byte-identical to the one ``routes/agent_pdp_v1._row_as_product``
    # emits as ``pivota_canonical_url``/``url`` for the same row. The two
    # surfaces serve the same rows through the same gate and resolver; a citation
    # that disagreed with the PDP read about its own URL would be a second
    # drift of exactly the kind ``services/pdp_renderability`` exists to prevent.
    #
    # ONE COUPLING THAT COMES WITH THAT HELPER: it derives its host from
    # ``CHECKOUT_UI_BASE_URL`` (services/catalog_sync_service.py), whereas
    # ``CITE_AS`` above hardcodes "agent.pivota.cc". VERIFIED 2026-07-26 that prod
    # resolves the env var to exactly ``https://agent.pivota.cc`` — read back off
    # the live ``/api/agent/pdp/{sig}`` response, which builds its URL with this
    # same helper — so the two agree today. If ``CHECKOUT_UI_BASE_URL`` is ever
    # repointed at a checkout or staging host, ONE response would advertise two
    # different hosts; move ``CITE_AS`` onto the same base at that point.
    #
    # NULL WHEN NO SIG IS MINTED, deliberately, matching
    # ``services/agent_pdp_view_assembler`` (``pdp_url = ... if sig else None``).
    # A row with no sig has no followable PDP at all — the ck fallback was never
    # a weaker URL, it was a broken one — so emitting null is the honest answer
    # and strictly better than a guaranteed 500. ``attribution_required`` STAYS
    # TRUE and ``cite_as`` is unchanged: attribution is to the SOURCE ("Pivota —
    # agent.pivota.cc"), which an agent can still honour by name without a deep
    # link. Dropping the requirement instead would hand away the moat on exactly
    # the rows we cannot yet link to.
    #
    # #1592 fixed the URL's FORM. Whether that now-correct sig URL is FOLLOWABLE
    # is a separate question — and when it is NOT, this row's content_key may
    # still have a sibling that renders, so the citation can be given a URL it
    # can actually follow instead of a dead one.
    #
    # THE LADDER, in order:
    #   1. this row's own sig, when it renders            -> url_source "self"
    #   2. the content_key's ELECTED canonical sig         -> "elected_canonical"
    #   3. this row's own sig, dead, flagged unrenderable  -> "self"
    #   4. no sig at all                                   -> null
    #
    # Step 2 is why 229 of the 245 route-broken content_keys can be given a live
    # URL rather than a flag: measured 2026-07-26, that many have a renderable
    # sibling, and 12/12 sampled siblings served real 200s. It reads the STORED
    # election (migration 181) rather than picking a sibling itself, and the
    # stored winner is intersected with the live electable set by
    # ``electable_sig_exists`` — the SAME validation the sitemap feed applies —
    # so the URL an agent is told to cite is exactly the URL the sitemap
    # advertises. Computing our own preference here would be a fourth opinion on
    # a question that already has three consumers, which is how the sitemap and
    # the PDPs drifted apart in the first place.
    #
    # UNTIL THE ELECTION IS SEEDED THIS IS INERT, by construction and not by
    # accident: ``content_canonical_election`` is empty in prod
    # (``canonical_sig_id`` null on 5,887/5,887 feed rows), so rung 2 never fires
    # and every row lands on 1, 3 or 4 exactly as it did before. Seeding is a
    # separate operator decision — it moves ~350 live sitemap URLs, see
    # docs/runbooks/content_canonical_election_rollout.md — and this ships safely
    # ahead of it rather than waiting on it.
    signature_id = str(row.get("pivota_signature_id") or "").strip()
    own_sig = (
        signature_id
        if signature_id.startswith("sig_") and len(signature_id) > len("sig_")
        else None
    )
    own_renders = bool(row.get("pdp_renderable"))
    elected_sig = str(row.get("elected_canonical_sig") or "").strip() or None

    if own_sig and own_renders:
        canonical_url, url_renderable, url_source = (
            pivota_canonical_pdp_url(own_sig),
            True,
            "self",
        )
    elif elected_sig and elected_sig != own_sig:
        # Electability already implies renderable — that conjunct is inside
        # electable_sig_exists — so this URL is followable by construction.
        canonical_url, url_renderable, url_source = (
            pivota_canonical_pdp_url(elected_sig),
            True,
            "elected_canonical",
        )
    elif own_sig:
        canonical_url, url_renderable, url_source = (
            pivota_canonical_pdp_url(own_sig),
            False,
            "self",
        )
    else:
        canonical_url, url_renderable, url_source = None, None, None

    return {
        "content_key": content_key,
        "title": str(row.get("title") or ""),
        "brand": row.get("brand"),
        "summary": _first_sentence(description),
        "description": description,
        "bullet_points": row.get("bullet_points") or [],
        "usage_scenarios": row.get("usage_scenarios") or [],
        # Normalized: historical view rows carry JSON-encoded strings inside
        # this dict (agents mis-parse "[\"serum\"]" as a string) — see
        # services.agent_pdp_view_assembler.normalize_taxonomy_tags.
        "taxonomy_tags": _normalize_taxonomy_tags(row.get("taxonomy_tags")) or [],
        "image_url": row.get("image_url"),
        # Social proof, never fabricated (migration 186: NULL means "no review
        # data on the source page", not "zero stars"). Same {value, count}
        # normalization as agent_pdp_v1's aggregate_rating; a rating with no
        # positive review count behind it is withheld rather than invented.
        # Offer-free by construction — a rating is content, not commerce.
        "aggregate_rating": _aggregate_rating(row),
        # ── trust / substantiation (the differentiator) ──
        "substantiation": {
            "claims": claims,
            "trust_grade": "substantiated" if claims else "listed",
            # Not carried on the served row — disclosed as unknown rather than
            # implying full coverage (honesty seam). Populated when the
            # per-claim verify coverage reaches agent_pdp_view.
            "verify_coverage": None,
        },
        # ── attribution (REQUIRED for the moat) ──
        # canonical_url is the sig-form PDP, or null when no sig is minted — see
        # the measurement above the return for why the content_key form was a
        # guaranteed 500 and why attribution_required stays true regardless.
        "attribution": {
            "source": "Pivota",
            "canonical_url": canonical_url,
            # CAN THE AGENT ACTUALLY FOLLOW canonical_url? Both of get_pdp_v2's
            # gates, asked about the exact sig the URL above is built from
            # (services.pdp_renderability.sig_pdp_will_render — the same predicate
            # the sitemap feed and the canonical election use, so the three cannot
            # disagree about one sig).
            #
            # THIS IS THE HONESTY SEAM ADR-007 NEEDS. A citation whose URL cannot
            # be followed is not a citation, and until now this endpoint could not
            # tell the difference: measured on the live feed 2026-07-26, 879 of
            # 5,887 rows do not render (779 `serving_eligible` whose content route
            # does not resolve, plus 100 offer-free `no_price` rows admitted by
            # INDEX_ELIGIBLE_READ) and this endpoint answered 200 — with
            # `attribution_required: true` — for every one of them.
            #
            # A SIGNAL, NOT A FILTER, and that asymmetry is the ADR: SLICE 1 exists
            # so an offer-free row stays CITABLE, so we keep serving the content
            # and tell the truth about the link. False means: quote and credit the
            # CONTENT via `cite_as`, but do not emit the URL as a hyperlink.
            #
            # Null (not False) when there is no URL at all to characterise — a
            # sig-less row. False would imply we checked a link we never had.
            "url_renderable": url_renderable,
            # WHICH rung of the ladder produced canonical_url: "self" (this row's
            # own sig), "elected_canonical" (the content_key's elected sibling,
            # substituted because this row's own PDP does not render), or null
            # when there is no URL. Disclosed rather than silently swapped: an
            # agent that cites a URL should be able to tell whether it points at
            # the record it asked for or at that record's group canonical, and a
            # substitution that could not be detected would be its own dishonesty.
            "url_source": url_source,
            "cite_as": CITE_AS,
            "attribution_required": True,
        },
        # ── routing (content, NOT a commerce offer) ──
        "destination_url": None,  # P0: external brand URL not projected yet
        "buyable": False,
        "catalog_track": "citation",
        "offers": None,
        "usage_terms": {"attribution_required": True, "commercial_use": "cite-and-link"},
    }


def _search_row_to_citation(row: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a citable-recall row to the CitationItem projection.

    Recall rows are lighter than a full PDP read (no graded claims / structured
    fields), so the projection's substantiation comes back empty here — the agent
    fetches the single-item endpoint for full substantiation. Reuses
    project_citation_item so search + single-item emit the SAME shape.
    """
    return project_citation_item(
        {
            "content_key": row.get("content_key"),
            # Threaded so search emits the same sig-form attribution URL as the
            # single-item read; selected as
            # COALESCE(apv.pivota_signature_id, p.pivota_signature_id) by
            # services/pivot_query_service._fetch_citable_canonical_rows. Absent
            # ⇒ null canonical_url, never the ck form (a guaranteed 500).
            "pivota_signature_id": row.get("pivota_signature_id"),
            # Computed inline by that same query (it already reads
            # catalog_products), keyed on the identical sig expression — so search
            # never pays the single-item read's extra round trip.
            "pdp_renderable": row.get("pdp_renderable"),
            # Elected canonical for this content_key, so a search hit whose own
            # PDP is dead still cites a followable URL. Selected by the recall
            # query (it already reads catalog_products and can validate
            # electability inline), so search needs no extra round trip.
            "elected_canonical_sig": row.get("elected_canonical_sig"),
            "title": row.get("product_title"),
            "description": row.get("product_description"),
            "brand": row.get("brand"),
            "image_url": row.get("product_image_url"),
            "evidence_profile": None,
            "bullet_points": None,
            "usage_scenarios": None,
            "taxonomy_tags": None,
        }
    )


# NOTE: /search MUST be registered before /{citation_id} or FastAPI matches the
# literal "search" as a citation_id.
@router.get("/search")
async def search_citations(
    request: Request,
    response: Response,
    q: str = Query("", description="free-text query"),
    intent: str = Query("inform", description="inform (cite) | shop (suppressed)"),
    limit: int = Query(20, ge=1, le=50),
    _rl: None = Depends(_citation_rate_limit),
) -> Dict[str, Any]:
    agent, client_ip = _caller_identity(request)
    query = str(q or "").strip()
    norm_intent = str(intent or "inform").strip().lower()
    if not query:
        _spawn_log(endpoint="search", status=STATUS_EMPTY, query=query,
                   intent=norm_intent or "inform", result_count=0,
                   agent=agent, client_ip=client_ip)
        return {"items": [], "count": 0, "query": q, "intent": norm_intent or "inform"}

    # Intent gate (parity with the recall lane): shop / strict_serving_mode
    # SUPPRESSES citation rows — an agent driving a checkout never gets a
    # non-buyable row. Only inform-intent surfaces citations.
    if norm_intent in ("shop", "strict", "strict_serving_mode", "transact"):
        response.headers["Cache-Control"] = "public, max-age=120"
        _spawn_log(endpoint="search", status=STATUS_SUPPRESSED, query=query,
                   intent="shop", result_count=0, agent=agent, client_ip=client_ip)
        return {
            "items": [],
            "count": 0,
            "query": query,
            "intent": "shop",
            "suppressed": "citation rows are inform-intent only",
        }

    # Same gate as the slice-3 recall lane (INDEX_ELIGIBLE_RECALL, default OFF).
    from services.pivot_query_service import (
        _fetch_citable_canonical_rows,
        _index_eligible_recall_enabled,
    )

    if not _index_eligible_recall_enabled():
        _spawn_log(endpoint="search", status=STATUS_DISABLED, query=query,
                   intent="inform", result_count=0, agent=agent, client_ip=client_ip)
        return {"items": [], "count": 0, "query": query, "intent": "inform"}

    rows = await _fetch_citable_canonical_rows(query=query, merchant_id=None, limit=limit)
    items = [_search_row_to_citation(r) for r in rows]
    response.headers["Cache-Control"] = "public, max-age=120"
    response.headers["X-Pivota-Citation-Source"] = "Pivota"
    _spawn_log(endpoint="search", status=(STATUS_HIT if items else STATUS_EMPTY),
               query=query, intent="inform", result_count=len(items),
               agent=agent, client_ip=client_ip)
    return {"items": items, "count": len(items), "query": query, "intent": "inform"}


@router.get("/{citation_id}")
async def get_citation(
    citation_id: str,
    request: Request,
    response: Response,
    _rl: None = Depends(_citation_rate_limit),
) -> Dict[str, Any]:
    requested_id = str(citation_id or "").strip()
    agent, client_ip = _caller_identity(request)
    item = await _resolve_citation_item(requested_id)

    # B④-P1: log every call (hit or miss) off the response hot path. A miss is
    # itself signal — an agent asking for a product we don't (yet) cite.
    _spawn_log(
        endpoint="item",
        status=(STATUS_HIT if item else STATUS_MISS),
        requested_id=requested_id or None,
        content_key=(item.get("content_key") if item else None),
        agent=agent,
        client_ip=client_ip,
    )

    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    # Citation data is not real-time; cacheable + CDN-frontable.
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    response.headers["X-Pivota-Citation-Source"] = "Pivota"
    return item


async def _resolve_citation_item(raw: str) -> Optional[Dict[str, Any]]:
    """Resolve a raw path id to a CitationItem, or None when nothing matches.
    No raises — so the single get_citation telemetry point covers hit + miss
    uniformly. Same fail-closed gate / resolver chain as get_pdp."""
    if not raw:
        return None

    # index_eligible rows are readable only when INDEX_ELIGIBLE_READ is on;
    # flag OFF ⇒ serving-only resolution.
    index_read = _index_eligible_read_enabled()

    # ext_* IDs resolve to a content_key first (reuses agent_pdp_v1's resolver).
    if _is_external_product_id(raw):
        resolved = await database.fetch_one(EXT_RESOLVE_SQL, {"ext_id": raw})
        if not resolved:
            return None
        raw = str(dict(resolved).get("content_key") or "")

    sql = _query_for_id(raw, index_eligible_read=index_read)
    if sql is None:
        return None
    row = await database.fetch_one(sql, {"id": raw})
    if not row:
        return None
    row_dict = _row_to_dict(row)
    row_dict["pdp_renderable"] = await _sig_renderable(
        row_dict.get("pivota_signature_id")
    )
    # Only worth a lookup when the row's own URL is unusable — that is the only
    # case where the elected sibling changes the answer, and it keeps the common
    # path (a renderable row) at one extra query rather than two.
    if not row_dict["pdp_renderable"]:
        row_dict["elected_canonical_sig"] = await _elected_canonical_sig(
            row_dict.get("content_key")
        )
    return project_citation_item(row_dict)


# The group's elected canonical, intersected with the live electable set. Built
# from the SAME helper the sitemap feed validates its own `canonical_sig_id`
# with, so the URL we tell an agent to cite is the URL the sitemap advertises.
#
# `widen=False` deliberately: electability here is a SITEMAP question (which sig
# holds the public URL), not a citation-read question, and the feed's own
# election JOIN is the thing this must agree with. INDEX_ELIGIBLE_READ widens who
# may be READ; it has no bearing on which sibling holds the canonical URL.
_ELECTED_CANONICAL_SIG_SQL = compile_pg(
    select(_content_canonical_election.c.canonical_sig_id)
    .where(
        and_(
            _content_canonical_election.c.content_key == literal_column(":ck"),
            electable_sig_exists(
                _content_canonical_election.c.canonical_sig_id, widen=False
            ),
        )
    )
    .limit(1)
)

# Compiled ONCE at import, like `_ELECTED_CANONICAL_SIG_SQL` above and like
# `services.pivot_query_service._SIG_RENDERABLE_SQL`. The first version built and
# Postgres-compiled this expression inside the request handler — ~0.8 ms of
# SQLAlchemy work on every citation read, for a string that never varies.
# Compiling at import also means a malformed fragment fails the process at
# startup instead of once per request behind a fail-closed except.
_SIG_RENDERABLE_QUERY = f"SELECT ({sig_pdp_will_render_sql(':sig')}) AS pdp_renderable"


async def _elected_canonical_sig(content_key: Any) -> Optional[str]:
    """The content_key's elected canonical sig, or None when there is no usable
    election.

    None covers every degradation identically — no row in
    ``content_canonical_election``, a stored winner that has stopped being
    electable, or a failed query — because the caller's ladder already falls back
    to the row's own (flagged) URL. That is the same "degrade to no election"
    contract every other reader of this table honours; see
    ``electable_sig_exists`` for why naming a stale winner is worse than naming
    none.

    Returns None for all 5,887 feed rows today: the table is unseeded.
    """
    ck = str(content_key or "").strip()
    if not ck:
        return None
    try:
        value = await database.fetch_val(_ELECTED_CANONICAL_SIG_SQL, {"ck": ck})
    except Exception:
        logger.warning(
            "citation_elected_canonical_lookup_failed",
            exc_info=True,
            extra={"content_key": ck},
        )
        return None
    sig = str(value or "").strip()
    return sig if sig.startswith("sig_") and len(sig) > len("sig_") else None


async def _sig_renderable(signature_id: Any) -> bool:
    """Will the PDP for this sig answer 200? Both of get_pdp_v2's gates.

    A SEPARATE ROUND TRIP here, rather than a column on the read above, because
    ``_query_for_id`` returns ``routes/agent_pdp_v1``'s SQL and this endpoint can
    absorb the extra hop: it is a public, offer-free citation read behind
    ``Cache-Control: public, max-age=300, stale-while-revalidate=600``, so the cost
    amortises across a 5-minute window per id, and it is not the agent PDP render
    path.

    HISTORY, because an earlier version of this docstring argued the opposite and
    that argument is now WRONG. It claimed agent_pdp_v1's reads were pinned to
    touch none of ``catalog_products`` / ``catalog_skus`` / ``catalog_offers`` /
    ``product_group_members`` / ``subject_resolve``, that adding the predicate
    there would break that pin, and that the fix "wants the flag MATERIALISED onto
    ``agent_pdp_view`` by the assembler".

    Measured, that was the wrong call twice over, and #1602 reversed it:

      * the predicate costs **0.18-0.57 ms** execution (EXPLAIN ANALYZE on prod,
        all index lookups) — roughly 2-6% of the <10ms p99 target, and NOT the
        five-way join migration 085 removed, so the pin was relaxed to ban
        ``JOIN catalog_products`` specifically rather than the table name;
      * materialising would have been actively worse. ``agent_pdp_view`` has ZERO
        triggers and is refreshed only by the application, and none of the four
        ``serving_eligible`` writers nor any ``external_product_seeds.status``
        writer touches the assembler — nor does the reconciler cron's staleness
        CTE, which keys on ``catalog_products``/``catalog_offers`` timestamps. A
        materialised flag would therefore go stale silently and stay stale.

    So ``/api/agent/pdp`` now carries ``pdp_renderable`` INLINE. Only the second
    pin still holds as written: the emergency
    ``AGENT_PDP_V1_BYPASS_SERVING_ELIGIBILITY`` path stays free of
    ``index_pipeline_state`` — the table it exists to bypass — which is why that
    path emits ``pdp_renderable: null`` rather than a value.

    FAIL-CLOSED on anything unexpected: no sig, a non-sig value, or a DB error ⇒
    False, i.e. "do not follow this link". The alternative direction would claim a
    URL is followable on the strength of a failed check. NOTE the deliberate
    asymmetry with agent_pdp_v1, which answers ``None`` on its bypass path: that is
    a different question ("we could not run the check" vs "we ran nothing because
    the operator overrode the gate the check reads"), and unknown is the honest
    answer only in the second case.
    """
    sig = str(signature_id or "").strip()
    if not sig.startswith("sig_") or len(sig) <= len("sig_"):
        return False
    try:
        value = await database.fetch_val(_SIG_RENDERABLE_QUERY, {"sig": sig})
    except Exception:
        # LOUD on purpose. This guard is fail-closed, so a broken query degrades
        # to `url_renderable: false` on EVERY row rather than to a 500 — which is
        # indistinguishable, from outside, from a catalog where nothing renders.
        # The first version of the embedded fragment WAS invalid SQL, and this
        # except would have turned that into a permanent silent lie. error, not
        # warning, so it surfaces where a 500 would have.
        logger.error(
            "citation_pdp_renderable_check_failed", exc_info=True, extra={"sig": sig}
        )
        return False
    return bool(value)
