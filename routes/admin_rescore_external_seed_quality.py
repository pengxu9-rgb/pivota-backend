"""Admin route: run `backfill_external_seed_quality_rescore` IN-CLUSTER, in chunks.

WHY THIS EXISTS. The rescore is a prod backfill that must reach the database, and
the only two venues both failed (measured 2026-07-28):

  * over the Railway PUBLIC PROXY from a laptop — the script ran but managed only
    ~18 of 500 rows in ~8 minutes before the pool could not reconnect
    (`wrote=16 eligible=6 fail=2; PROMOTED to public: 6`). At that rate the 3,860
    rows still to rescore need ~215 abort/resume cycles and ~28h wall clock.
  * `railway ssh` — unavailable on this project: the bare form answers "Your
    application is not running or in a unexpected state" while `/health` reports
    `db_ok: true`, and both the deployment id and the service-instance id are
    rejected with "Deployment instance not found".

This route is the third venue: the work runs inside the `web` container, against
the INTERNAL database, where the per-row cost is ~1ms of RTT instead of ~140ms.

🚨 IT DELIBERATELY DOES NOT CALL `scripts.backfill_external_seed_quality_rescore.run()`.
That function owns the process's connection lifecycle: it calls
`database.connect()` / `disconnect()`, and its `_reset_connection()` recovery path
calls `pool.terminate()` and sets `backend._pool = None` on the SHARED `databases`
pool. That is correct for a one-shot CLI and catastrophic inside the web process —
it would tear the app's pool out from under every in-flight request. So this route
reuses the script's SQL and its pure helpers (the parts that cannot drift) and
re-implements only the loop.

WHAT IS DELIBERATELY DROPPED, AND WHY THAT IS SAFE HERE. The script wraps each
product in `asyncio.wait_for(..., timeout=45)`. That timeout exists because a
cross-region proxy socket can park forever; but the cancellation it performs is
itself what leaves the connection half-acquired and poisons every later query —
which is the entire reason `_reset_connection()` had to be written. On the
internal network the condition that motivated the timeout does not arise, so this
loop omits BOTH the cancellation and the recovery it necessitates. Bounded work
per request is what keeps the endpoint honest instead.

The `limit` cap is the real safety property: one call does bounded work and
returns a summary, and the caller drives it. Resume is free because
`_rescored_ids()` skips products already on the current rules version.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from db.database import database
from utils.auth import require_admin_or_key

logger = logging.getLogger("admin_rescore_external_seed_quality")

router = APIRouter(prefix="/admin/rescore", tags=["Admin Rescore"])

# Hard ceiling per call. The point of this endpoint is BOUNDED work inside a
# request that also has to keep serving traffic; an unbounded run belongs in a
# job, not here. 500 internal-network rows is seconds of awaits, not minutes.
MAX_LIMIT = 500
DEFAULT_LIMIT = 200


class RescoreRequest(BaseModel):
    mode: Literal["dry_run", "apply"] = "dry_run"
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    source_prefix: Optional[str] = None
    # Off by default, exactly as the CLI has it: a rescore can then only ever
    # promote a row, never demote one that is already serving.
    include_eligible: bool = False
    trust_flush_every: int = Field(default=200, ge=1, le=MAX_LIMIT)
    skip_trust: bool = False


class RescoreResponse(BaseModel):
    mode: str
    batch: int
    already_rescored: int
    candidates: int
    selected: int
    with_inci: int
    with_sections: int
    wrote: int
    eligible: int
    promoted_to_public: int
    no_write: int
    failed: int
    sample: List[Dict[str, Any]] = []
    notes: List[str] = []


async def _run_chunk(req: RescoreRequest) -> Dict[str, Any]:
    # Imported lazily and BY REFERENCE so the SQL and the row-shaping helpers stay
    # single-sourced with the CLI. A second copy of FETCH is exactly how the two
    # would drift into scoring different populations.
    from scripts.backfill_external_seed_quality_rescore import (
        FETCH,
        _coerce_sections,
        _flush_trust,
        _rescored_ids,
        demotion_preview,
        filter_todo,
    )
    from services.external_seed_servability import (
        build_servable_quality_payload,
        make_external_seed_servable,
    )

    notes: List[str] = []

    rows = [
        dict(r)
        for r in await database.fetch_all(
            FETCH,
            {
                "source_prefix": req.source_prefix,
                "include_eligible": 1 if req.include_eligible else 0,
            },
        )
    ]
    done = await _rescored_ids()
    # filter_todo, NOT an inline comparison. _rescored_ids returns identity
    # TRIPLES; the previous inline check compared a bare source_product_id
    # against them, which is always-False membership — the route re-scored the
    # same first `limit` rows on every call, appending a fresh snapshot each
    # time, and could never advance past its first chunk.
    todo = filter_todo(rows, done)
    selected = todo[: req.limit]

    # Reported over the FULL candidate set, matching the CLI's own preflight line,
    # so the two are directly comparable across venues.
    with_inci = sum(1 for r in rows if (r.get("raw_inci") or "").strip())
    with_sections = sum(1 for r in rows if _coerce_sections(r.get("pdp_details_sections")))

    result: Dict[str, Any] = {
        "mode": req.mode,
        "batch": len(rows),
        "already_rescored": len(done),
        "candidates": len(todo),
        "selected": len(selected),
        "with_inci": with_inci,
        "with_sections": with_sections,
        "wrote": 0,
        "would_demote_skipped": 0,
        "eligible": 0,
        "promoted_to_public": 0,
        "no_write": 0,
        "failed": 0,
        "sample": [],
        "notes": notes,
    }

    if req.mode == "dry_run":
        result["sample"] = [
            {
                "source_product_id": r["source_product_id"],
                "inci": bool((r.get("raw_inci") or "").strip()),
                "sections": bool(_coerce_sections(r.get("pdp_details_sections"))),
                "category_kind": r.get("category_kind"),
                "title": (r.get("title") or "")[:60],
            }
            for r in selected[:5]
        ]
        notes.append("dry_run — no writes; pass mode=apply to write")
        return result

    wrote = eligible = silent = failed = would_demote = 0
    promoted: List[str] = []
    trust_wrote = 0

    for r in selected:
        epid = r["source_product_id"]
        try:
            qp = build_servable_quality_payload(
                title=r["title"],
                description=r["description"],
                price=r["price_amount"],
                image_url=r["image_url"],
                brand=r["brand"],
                product_type=r["product_type"],
                category=r["category_kind"],
                raw_inci=r["raw_inci"],
                pdp_details_sections=_coerce_sections(r.get("pdp_details_sections")),
            )
            # Same promote-only guard as the CLI, from the same module — this
            # route is the venue the prod runbook names for include_eligible
            # runs, i.e. exactly the case the guard exists for. It previously
            # had NO guard: a currently-public row scoring below the bar was
            # demoted and IndexNow fired on the way down.
            preview_score = demotion_preview(r, qp)
            if preview_score is not None:
                would_demote += 1
                logger.warning(
                    "rescore SKIP would-demote %s preview=%.1f", epid, preview_score
                )
                continue
            summary = await make_external_seed_servable(
                # seed_id comes from the row — never rebuilt from a tool constant.
                product_key=r["product_key"],
                seed_id=r["seed_id"],
                source_product_id=epid,
                quality_payload=qp,
                reason="rescore_ingredient_aware",
            )
        except Exception as exc:  # noqa: BLE001 — one bad row must not end the chunk
            failed += 1
            logger.warning("rescore FAIL %s: %s", epid, exc)
            continue

        # `make_external_seed_servable` swallows its own errors, so a returned
        # summary is NOT proof of a write. `quality` is the authoritative signal,
        # and `serving_eligible is None` means the identity lookup failed and the
        # snapshot landed under the FALLBACK merchant — unfindable by the
        # eligibility classifier, yet `_rescored_ids()` would mark the product done
        # forever. Both conditions are required, exactly as in the CLI.
        persisted = bool((summary or {}).get("quality"))
        resolved = (summary or {}).get("serving_eligible") is not None
        if persisted and resolved:
            wrote += 1
            if (summary or {}).get("serving_eligible") is True:
                eligible += 1
                if not req.skip_trust:
                    promoted.append(r["product_key"])
        else:
            silent += 1
            logger.warning(
                "rescore NO-WRITE %s persisted=%s resolved=%s", epid, persisted, resolved
            )

        if len(promoted) >= req.trust_flush_every:
            trust_wrote += await _flush_trust(promoted)
            promoted = []

    # Flush the remainder. Load-bearing: serving_eligible is NOT what public
    # readers gate on — catalog_row_trust.serving_decision is, and only the trust
    # upserter writes it. Without this a rescored row sits eligible-but-blocked
    # until the phase-2d drift cron happens to catch it.
    trust_wrote += await _flush_trust(promoted)

    result.update(
        wrote=wrote,
        eligible=eligible,
        would_demote_skipped=would_demote,
        promoted_to_public=trust_wrote,
        no_write=silent,
        failed=failed,
    )
    if silent:
        notes.append(
            f"{silent} row(s) persisted nothing — re-runnable, they are not marked done"
        )
    return result


@router.post("/external-seed-quality", response_model=RescoreResponse)
async def rescore_external_seed_quality(
    request: RescoreRequest,
    current_user: dict = Depends(require_admin_or_key),
):
    del current_user
    return RescoreResponse(**await _run_chunk(request))
