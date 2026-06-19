"""PR-6: task queue materialization.

Converts an audit's `action_items[]` into tracked merchant_tasks rows.
Called from the merchant audit completion hook (alongside the
executor agent dispatch from PR-4a).

Honest scope:
  - One-shot materialization per audit run (idempotent — re-running
    the same audit_run_id is a no-op via `tasks_for_audit` check).
  - Q-P0-2 / Q-P1-4: cross-audit SUPERSESSION. When a fresh audit
    emits the same canonical action identity as a prior pending task,
    the prior task flips to `status='superseded'` and points at the
    newer task via `superseded_by_task_id`. Operators no longer see
    stale tasks from previous audits in their default queue, and
    the audit-trail link is preserved (queryable via
    `?status_filter=superseded`).
  - Canonical action identity: (merchant_id, lever, normalized_title,
    target_host, product_keys). Pre-fix dedup used only
    `(lever or title).lower()`, which collapsed different editorial
    actions on different target_hosts into a single row. The new
    identity preserves per-host distinctness; see
    `_canonical_action_identity` below.
  - Skips Phase 0 / pivota_integration tasks for cold-start audits
    (mirrors the merchant_view.actions demote — these are pitch
    material in cold-start, not work).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Levers we don't materialize as merchant_tasks (pitch material, not work).
_PITCH_ONLY_LEVERS = frozenset({
    "pivota_integration",  # Phase 0 — onboarding pitch
})

# Levers that represent merchant-scoped (not per-host) actions. For
# these, dedup by lever alone — only ONE such action ever applies per
# audit regardless of how it's titled across products.
#
# Per-host levers (`editorial`, `research`, `partnership`, etc.) use
# the full (lever, normalized_title, target_host, product_key)
# identity so distinct hosts/products materialize as distinct tasks.
_MERCHANT_SCOPED_LEVERS = frozenset({
    "pivota_integration",
    "gsc_integration",
    "schema_indexing",
    "site_audit",
})


def _is_cold_start_audit(integration_state: Optional[Dict[str, Any]]) -> bool:
    """Mirrors services.agent_center_bd_report_service._is_cold_start_audit
    so cold-start audits don't materialize Phase 0 'onboard with us'
    tasks. Inlined here to avoid the cross-service import."""
    if not integration_state:
        return False
    if integration_state.get("fully_integrated"):
        return False
    missing = integration_state.get("missing_pieces") or []
    return "store_platform" in missing and "psp" in missing


def _normalize_title_for_identity(title: str) -> str:
    """Whitespace-collapsed lowercased title for the identity key.
    Punctuation kept because action titles do encode meaningful
    distinctions like "Pitch whowhatwear.com fashion team" vs
    "Pitch forbes.com editorial team" — the host is the differentiator.
    """
    return " ".join(title.split()).lower()


def _canonical_action_identity(
    *,
    title: str,
    lever: Optional[str],
    target_host: Optional[str],
    product_key: Optional[str],
) -> tuple:
    """Q-P1-5 canonical action identity. Same identity → same task;
    different identity → distinct task even if title/lever match.

    Pre-fix `_extract_action_items` used `(lever or title).lower()`
    as the dedup key. That collapsed different editorial actions
    targeting different hosts into one row, because lever='editorial'
    matched across all of them. The Winona prod artifact showed 4
    "Write 1 content brief for failed category visibility queries"
    rows because the OUTER materializer dedup tolerated duplicates
    while the SCOPED-by-key dedup inside extract dropped
    target-host distinctions.

    Two identity regimes:
      1. **Merchant-scoped levers** (gsc_integration, etc.) — ONE
         such action ever applies per audit. Identity = (lever,)
         alone so cross-product duplicates collapse correctly even
         when titles vary slightly ("GSC" vs "GSC again").
      2. **Everything else** — identity = (lever, normalized_title,
         target_host, product_key). Preserves per-host/per-product
         distinctions for editorial / research / partnership / etc.

    Two tasks share identity ↔ they're the same action.
    """
    lever_norm = (lever or "").lower()
    if lever_norm in _MERCHANT_SCOPED_LEVERS:
        # Merchant-scoped: lever alone is the identity.
        return ("merchant_scoped", lever_norm)
    return (
        "per_host",
        lever_norm,
        _normalize_title_for_identity(title),
        (target_host or "").lower(),
        (product_key or "").lower(),
    )


def _nba_text(value: Any) -> str:
    """A per-SKU next_best_action field may be a plain string or a {text/label}
    mapping. Coerce to a clean string."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for k in ("text", "label", "one_liner", "headline"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _nba_body(nba: Dict[str, Any]) -> Optional[str]:
    """Merchant-facing body for a per-SKU task: why it matters + the first move."""
    parts = [_nba_text(nba.get("why_this_first")), _nba_text(nba.get("first_move"))]
    body = "  ".join(p for p in parts if p)
    return body or None


def _nba_severity(sku_report: Dict[str, Any]) -> str:
    """Severity for a per-SKU enrichment task, from the citation score (the
    outcome dimension): not-yet-visible SKUs are the urgent ones."""
    cit = ((sku_report.get("scores") or {}).get("citation") or {}).get("score")
    if not isinstance(cit, (int, float)):
        return "high"
    if cit < 40:
        return "high"
    if cit < 70:
        return "medium"
    return "low"


def _per_sku_action_items(
    audit_report: Dict[str, Any],
    seen_keys: set,
) -> List[Dict[str, Any]]:
    """Bridge for per-SKU audits (audit_mode='per_sku'): their findings live under
    `per_sku_reports` (NOT `per_product`), so the per_product walk yields nothing
    and these audits would materialize ZERO tasks — the action plan would never
    reflect the audits a merchant actually runs. Turn each SKU's already-computed
    `next_best_action` (the per-product 'what to do next') into one task.

    The NBA headline already names the product, so these read as distinct rows.
    Interactive surfaces (where_you_can_win 'create the answer', win-plan pitches)
    are intentionally NOT auto-materialized — they create tasks on click."""
    out: List[Dict[str, Any]] = []
    for r in (audit_report.get("per_sku_reports") or []):
        if not isinstance(r, dict):
            continue
        nba = r.get("next_best_action")
        if not isinstance(nba, dict) or nba.get("is_empty"):
            continue
        title = _nba_text(nba.get("headline"))
        if not title:
            continue
        product_key = r.get("product_key") or r.get("sku_key")
        key = _canonical_action_identity(
            title=title, lever="sku_enrichment",
            target_host=None, product_key=product_key,
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        tracking = [
            t for t in (nba.get("tracking_metrics") or [])
            if isinstance(t, str) and t.strip()
        ]
        cta = nba.get("cta") if isinstance(nba.get("cta"), dict) else {}
        cta_url = cta.get("url") if (
            isinstance(cta.get("url"), str) and cta.get("url", "").startswith("http")
        ) else None
        out.append({
            "title": title,
            "body": _nba_body(nba),
            "severity": _nba_severity(r),
            "lever": "sku_enrichment",
            "evidence": {
                "priority_order": 1,
                "cta_url": cta_url,
                "cta_label": (cta.get("label") if cta_url and isinstance(cta.get("label"), str) else None),
                "target_host": None,
                "product_key": product_key,
                # tracking_metrics ARE the per-SKU success signal — surface them as
                # the outcome/KPI so the task isn't a bare title.
                "expected_outcome": (tracking[0] if tracking else None),
                "kpi_to_track": (tracking[1] if len(tracking) > 1 else None),
            },
        })
    return out


def _extract_action_items(audit_report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Walk per_product → merchant_view.actions (the PR-A redesign
    surface) and return the union of action items across all products.

    Falls back to per_product → action_items (the legacy field) when
    merchant_view is missing. Within-audit dedup uses the canonical
    action identity (see _canonical_action_identity), preserving
    distinct actions that share lever/title but target different
    hosts — pre-fix those collapsed into one row.
    """
    if not isinstance(audit_report, dict):
        return []
    seen_keys: set = set()
    out: List[Dict[str, Any]] = []
    per_product = audit_report.get("per_product") or []
    for product in per_product:
        if not isinstance(product, dict):
            continue
        product_key = product.get("product_key") or product.get("merchant_pdp_url")
        # Page-usability Step 1: the product's display name, to disambiguate
        # per-product task titles. Brand-style actions (e.g. "Index your canonical
        # PDPs") are emitted once PER product, so without this the queue shows N
        # identical titles that read as duplicates (they're distinct per-SKU tasks).
        _prod_meta = product.get("product") if isinstance(product.get("product"), dict) else {}
        product_label = str((_prod_meta or {}).get("title") or "").strip()
        # Prefer merchant_view.actions (PR-A: data-bound, ranked)
        actions = ((product.get("merchant_view") or {}).get("actions") or [])
        if not actions:
            actions = product.get("action_items") or []
        for a in actions:
            if not isinstance(a, dict):
                continue
            title = a.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            title = title.strip()
            # Disambiguate a per-product task whose title doesn't already name the
            # product, so two SKUs' "Index your canonical PDPs" tasks read as the
            # distinct tasks they are. Skip when the title already mentions the
            # product (content-gap actions already do).
            if product_key and product_label and product_label.lower() not in title.lower():
                _short = product_label if len(product_label) <= 48 else f"{product_label[:45].rstrip()}…"
                title = f"{title} — {_short}"
            lever = a.get("lever")
            # PR-codex-review-followup: derive lever from title when
            # absent (matches the same fallback in
            # services/audit_evidence_builder._normalize_action). Both
            # producers MUST share the same derivation rule so the
            # canonical action_plan_items row + the materialized task
            # have the same (lever, title) tuple — without this,
            # _link_task_to_canonical_action below cannot match a
            # task whose action came from _generate_action_items (no
            # explicit lever set), and action_plan_items.
            # materialized_task_id stays NULL forever for those rows.
            if not lever:
                from services.audit_evidence_builder import (
                    _derive_lever_from_title,
                )
                lever = _derive_lever_from_title(title.strip())
            target_host = a.get("target_host")
            # Q-P1-5: canonical identity preserves target_host +
            # product_key distinctions. Pre-fix this dedup key was
            # `(lever or title).lower()`, which collapsed every
            # editorial action with lever='editorial' into one row
            # regardless of which host it targeted.
            key = _canonical_action_identity(
                title=title.strip(),
                lever=lever,
                target_host=target_host,
                product_key=product_key,
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append({
                "title": title.strip(),
                "body": a.get("body") if isinstance(a.get("body"), str) else None,
                "severity": a.get("severity") or "medium",
                "lever": lever,
                "evidence": {
                    "priority_order": a.get("priority_order"),
                    "cta_url": a.get("cta_url"),
                    "cta_label": a.get("cta_label"),
                    "target_host": target_host,
                    "product_key": product_key,
                    # Outcome contract — the action already carries these (set by
                    # _normalize_action); carry them onto the task so the queue can
                    # show "expected outcome / KPI", not just a title.
                    "expected_outcome": a.get("expected_outcome"),
                    "kpi_to_track": a.get("kpi_to_track"),
                },
            })
    # Per-SKU audits carry no `per_product` (their findings are under
    # `per_sku_reports`); bridge them so they materialize tasks too. Only when the
    # legacy walk found nothing, so a report carrying both shapes isn't double-counted.
    if not out:
        out.extend(_per_sku_action_items(audit_report, seen_keys))
    return out


async def materialize_tasks_from_audit(
    *,
    merchant_id: str,
    audit_run_id: str,
    audit_report: Dict[str, Any],
    integration_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convert an audit's action_items into merchant_tasks rows.
    Returns a summary `{materialized, skipped_duplicate, skipped_pitch_only}`.

    Called from the merchant audit completion hook after
    record_audit_run_completed. Best-effort — per-task failures don't
    abort the batch.
    """
    from db.merchant_tasks import (
        find_pending_supersede_candidates,
        mark_task_superseded,
        record_task_created,
        tasks_for_audit,
    )

    if not merchant_id or not audit_run_id:
        return {"materialized": 0, "reason": "missing merchant_id or audit_run_id"}

    cold_start = _is_cold_start_audit(integration_state)
    actions = _extract_action_items(audit_report)
    if not actions:
        return {"materialized": 0, "reason": "no action_items in audit"}

    # Idempotency: if tasks already exist for this audit_run_id, skip
    # — don't double-materialize on reprocessing.
    existing = await tasks_for_audit(parent_audit_run_id=audit_run_id)
    if existing:
        return {
            "materialized": 0,
            "reason": f"audit already has {len(existing)} tasks materialized",
        }

    materialized = 0
    skipped_pitch = 0
    failures = 0
    superseded_total = 0
    # P5.8.3: track which canonical action_plan_items each
    # materialized task corresponds to, so the worker can call
    # link_action_to_task and populate action_plan_items.
    # materialized_task_id. Without this the canonical field
    # stays permanently NULL and the merchant-portal task→action
    # join breaks.
    links_established = 0
    for action in actions:
        if cold_start and action.get("lever") in _PITCH_ONLY_LEVERS:
            skipped_pitch += 1
            continue
        task_id = await record_task_created(
            merchant_id=merchant_id,
            title=action["title"],
            body=action.get("body"),
            severity=action.get("severity") or "medium",
            lever=action.get("lever"),
            parent_audit_run_id=audit_run_id,
            evidence=action.get("evidence"),
        )
        if task_id:
            materialized += 1
            # Q-P0-2 / Q-P1-4: cross-audit supersession. Find prior
            # pending tasks from OTHER audits with the same canonical
            # identity and mark them superseded. The DB query
            # filters on (merchant_id, lever, title); we narrow to
            # full identity match (incl. target_host + product_key)
            # in Python because those fields live in evidence_jsonb
            # and would require a functional index to query directly.
            superseded_count = await _supersede_prior_pending(
                merchant_id=merchant_id,
                new_task_id=task_id,
                action=action,
                audit_run_id=audit_run_id,
            )
            superseded_total += superseded_count
            # Find the canonical action_plan_items row for this
            # (audit_run, lever, title) and link the task back.
            # Match is best-effort: if extract_actions didn't emit
            # a canonical row (e.g., title drift), the link is
            # skipped silently — better to materialize the task
            # than to block on a missing link.
            try:
                ok = await _link_task_to_canonical_action(
                    audit_run_id=audit_run_id,
                    lever=action.get("lever"),
                    title=action.get("title"),
                    task_id=task_id,
                )
                if ok:
                    links_established += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "task_queue: link_action_to_task raised for "
                    "task=%s: %s", task_id, str(exc)[:200],
                )
        else:
            failures += 1

    # Persistent-workspace reconciliation (page-usability Step 1): the action
    # plan is one living cross-audit list. The loop above superseded prior
    # pending tasks the new audit RE-EMITTED; this closes the ones it DROPPED
    # (scope-aware), so the queue reflects current priorities instead of
    # accumulating stale rows. in_progress + standing NULL-parent tasks survive.
    reconciled = 0
    try:
        reconciled = await _reconcile_dropped_pending_tasks(
            merchant_id=merchant_id,
            audit_run_id=audit_run_id,
            covered_product_keys=_covered_product_keys(audit_report),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "task_queue: reconcile dropped-pending failed audit=%s: %s",
            audit_run_id, str(exc)[:200],
        )

    logger.info(
        "task_queue: audit=%s merchant=%s materialized=%d "
        "superseded=%d reconciled=%d links=%d skipped_pitch=%d failures=%d",
        audit_run_id, merchant_id, materialized, superseded_total,
        reconciled, links_established, skipped_pitch, failures,
    )
    return {
        "audit_run_id": audit_run_id,
        "materialized": materialized,
        "superseded_prior_pending": superseded_total,
        "reconciled_dropped_pending": reconciled,
        "links_established": links_established,
        "skipped_pitch_only": skipped_pitch,
        "failures": failures,
        "action_items_total": len(actions),
    }


def _norm_host(value: Any) -> str:
    """Strip scheme/path/query and www, lowercase — robust to bare hosts AND full
    URLs (urlparse doesn't populate hostname for a scheme-less host)."""
    h = str(value or "").strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split("?", 1)[0]
    if h.startswith("www."):
        h = h[4:]
    return h


async def reverify_outreach_records(
    *,
    merchant_id: str,
    run_id: str,
    audit_report: Dict[str, Any],
) -> Dict[str, Any]:
    """Outreach lifecycle Step 2 — close the loop. After a new audit, check each
    PENDING outreach pitch (lever='outreach_pitch', recorded by the mark-sent
    endpoint) against THIS run's set of hosts that INDEPENDENTLY cite THE MERCHANT
    — an endorsement-role host (editorial / creator / community, not the merchant's
    own listing and not a competitor's store) that ALSO cited the merchant's own
    SKU (cites_exact_sku / cites_near_variant). If the pitched host now cites us,
    flip the record to cited (evidence.outreach.status='cited' + cited_run_id /
    verified_at) and mark the task done — the honest proof the outreach worked.
    Best-effort: never raises into the audit worker."""
    from datetime import datetime, timezone

    from db.merchant_tasks import list_tasks_for_merchant, update_task_status
    from services.cited_host_classifier import is_endorsement_role

    try:
        # Oracle: NOT the bare endorsement_hosts roster — that includes independent
        # hosts which grounded a category answer while recommending a COMPETITOR,
        # i.e. a false "your pitch worked". Require the host to actually NAME the
        # merchant's SKU (cites_exact_sku / cites_near_variant) AND be an
        # independent endorsement role. Honest proof = the host now cites the merchant.
        amap = (audit_report or {}).get("authority_map") or {}
        citing_hosts = set()
        for row in amap.get("hosts") or []:
            if not isinstance(row, dict):
                continue
            if not is_endorsement_role(row.get("citation_role")):
                continue
            if not (row.get("cites_exact_sku") or row.get("cites_near_variant")):
                continue
            host = _norm_host(row.get("host"))
            if host:
                citing_hosts.add(host)
        if not citing_hosts:
            return {"checked": 0, "flipped": 0}

        # No lever filter on list_tasks_for_merchant + default limit 50 → raise the
        # limit and filter lever in Python. Step-1 records are created `pending`.
        tasks = await list_tasks_for_merchant(
            merchant_id=merchant_id, status_filter=["pending"], limit=500,
        )
        pending_outreach = [t for t in tasks if t.get("lever") == "outreach_pitch"]

        flipped = 0
        for task in pending_outreach:
            evidence = task.get("evidence_jsonb")
            if not isinstance(evidence, dict):
                continue
            outreach = evidence.get("outreach")
            if not isinstance(outreach, dict):
                continue
            host = _norm_host(outreach.get("host"))
            if not host or host not in citing_hosts:
                continue
            # read-modify-write: update_task_status OVERWRITES evidence_jsonb wholesale.
            outreach["status"] = "cited"
            outreach["cited_run_id"] = run_id
            outreach["verified_at"] = datetime.now(timezone.utc).isoformat()
            evidence["outreach"] = outreach
            task_id = task.get("task_id")
            if task_id and await update_task_status(
                task_id=task_id, status="done", evidence=evidence,
            ):
                flipped += 1

        if flipped:
            logger.info(
                "outreach reverify: merchant=%s run=%s flipped %d/%d pitch(es) to cited",
                merchant_id, run_id, flipped, len(pending_outreach),
            )
        return {"checked": len(pending_outreach), "flipped": flipped}
    except Exception as exc:  # best-effort: a reverify hiccup must not sink the audit
        logger.warning("outreach reverify failed (merchant=%s): %s", merchant_id, exc)
        return {"checked": 0, "flipped": 0, "error": str(exc)}


async def _supersede_prior_pending(
    *,
    merchant_id: str,
    new_task_id: str,
    action: Dict[str, Any],
    audit_run_id: str,
) -> int:
    """Q-P0-2 / Q-P1-4: mark prior pending tasks with the same
    canonical action identity as `superseded` and point them at the
    newly-materialized task.

    Returns the number of rows superseded. Best-effort — accessor
    failures are logged and counted as zero supersessions; the new
    task remains valid regardless.

    Identity match has TWO layers:
      1. SQL prefilter on (merchant_id, lever, title) via
         `find_pending_supersede_candidates`. Cheap, index-backed.
      2. Python full-identity filter on target_host + product_key
         pulled from `evidence_jsonb`. Heavier but precise.

    Tasks in stages other than `pending` are NOT touched:
      - `in_progress`: operator is actively working it; don't
        steal the work mid-flight.
      - `done` / `dismissed` / `failed`: terminal; supersession
        would muddy the audit trail.
      - `superseded`: already pointing at a newer task; would
        create a chain that obscures the latest live task.
    """
    from db.merchant_tasks import (
        find_pending_supersede_candidates,
        mark_task_superseded,
    )

    title = action.get("title") or ""
    lever = action.get("lever")
    new_evidence = action.get("evidence") or {}
    new_target_host = (new_evidence.get("target_host") or "").lower()
    new_product_key = (new_evidence.get("product_key") or "").lower()

    candidates = await find_pending_supersede_candidates(
        merchant_id=merchant_id,
        lever=lever,
        title=title,
        exclude_audit_run_id=audit_run_id,
    )
    if not candidates:
        return 0

    superseded = 0
    for cand in candidates:
        cand_evidence = cand.get("evidence") or {}
        cand_target_host = (cand_evidence.get("target_host") or "").lower()
        cand_product_key = (cand_evidence.get("product_key") or "").lower()
        # Strict equality on the differentiator fields. Pre-fix dedup
        # used `(lever or title).lower()` only — that collapsed every
        # editorial action targeting different hosts. Don't reintroduce
        # that collapse here; only supersede when target_host AND
        # product_key match too.
        if cand_target_host != new_target_host:
            continue
        if cand_product_key != new_product_key:
            continue
        ok = await mark_task_superseded(
            task_id=cand["task_id"],
            superseded_by_task_id=new_task_id,
        )
        if ok:
            superseded += 1
    return superseded


def _covered_product_keys(audit_report: Dict[str, Any]) -> set:
    """The products this audit actually assessed — used to keep reconciliation
    scope-aware (a SKU-scoped audit must not close tasks for SKUs it never
    looked at). Reads both the per-SKU shape (`per_sku_reports`) and the legacy
    brand shape (`per_product`). Lowercased product/sku keys."""
    keys: set = set()
    rpt = audit_report or {}
    sources = list(rpt.get("per_sku_reports") or [])
    brand = rpt.get("brand_report") if isinstance(rpt.get("brand_report"), dict) else rpt
    sources += list((brand or {}).get("per_product") or [])
    for r in sources:
        if isinstance(r, dict):
            pk = r.get("product_key") or r.get("sku_key")
            if pk:
                keys.add(str(pk).lower())
    return keys


async def _reconcile_dropped_pending_tasks(
    *,
    merchant_id: str,
    audit_run_id: str,
    covered_product_keys: set,
) -> int:
    """Close prior-run `pending` tasks the latest audit no longer surfaces, so
    the persistent cross-audit queue reflects current priorities. Scope-aware to
    avoid false-closes:
      - a per-product task (evidence.product_key set) is closed ONLY if this
        audit re-covered that product — an audit of SKU-B must not close SKU-A's
        still-valid tasks.
      - a brand-level task (no product_key) is closed (every audit re-assesses
        the brand).
    in_progress + standing NULL-parent tasks are exempt (the DB fetch excludes
    them). Recoverable: `superseded` is an audit-trail status, not a delete.
    Returns the number of rows closed."""
    from db.merchant_tasks import (
        list_pending_audit_tasks_excluding_run,
        mark_task_superseded,
    )

    stale = await list_pending_audit_tasks_excluding_run(
        merchant_id=merchant_id,
        exclude_audit_run_id=audit_run_id,
    )
    closed = 0
    for task in stale:
        evidence = task.get("evidence") or {}
        product_key = str(evidence.get("product_key") or "").lower()
        if product_key and product_key not in covered_product_keys:
            # this audit didn't look at that product — leave its task alone
            continue
        if await mark_task_superseded(task_id=task["task_id"]):
            closed += 1
    return closed


async def _link_task_to_canonical_action(
    *,
    audit_run_id: str,
    lever: Optional[str],
    title: Optional[str],
    task_id: str,
) -> bool:
    """P5.8.3 helper: find the canonical action_plan_items row that
    corresponds to this task and call link_action_to_task. Returns
    True on link, False when no match (e.g., extract_actions
    dropped the action via dedup or title drift)."""
    if not lever or not title or not task_id or not audit_run_id:
        return False
    try:
        from db.audit_evidence import (
            action_plan_items, link_action_to_task,
        )
        from db.database import database
        row = await database.fetch_one(
            action_plan_items.select()
            .where(
                action_plan_items.c.audit_run_id == audit_run_id,
                action_plan_items.c.lever == lever,
                action_plan_items.c.title == title,
            )
            .limit(1)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "_link_task_to_canonical_action lookup raised for "
            "audit=%s lever=%s: %s",
            audit_run_id, lever, str(exc)[:200],
        )
        return False
    if row is None:
        return False
    action_id = row[0] if hasattr(row, "__getitem__") else None
    try:
        from db.audit_evidence import link_action_to_task
        return await link_action_to_task(
            action_id=str(action_id), task_id=task_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "link_action_to_task call raised for action=%s task=%s: %s",
            action_id, task_id, str(exc)[:200],
        )
        return False


def _norm_fingerprint(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).lower()


def _first_fingerprint(evidence: Dict[str, Any], keys: tuple) -> str:
    if not isinstance(evidence, dict):
        return ""
    for key in keys:
        value = _norm_fingerprint(evidence.get(key))
        if value:
            return value
    return ""


def _executor_topics(evidence: Dict[str, Any]) -> List[str]:
    topics: List[str] = []

    def add(value: Any) -> None:
        norm = _norm_fingerprint(value)
        if norm and norm not in topics:
            topics.append(norm)

    if not isinstance(evidence, dict):
        return topics
    for key in ("topic", "target_topic", "query", "target_query"):
        add(evidence.get(key))
    for key in ("briefs", "failures"):
        for item in evidence.get(key) or []:
            if isinstance(item, dict):
                add(item.get("target_query"))
                add(item.get("query"))
    return topics


def _executor_parent_match_score(
    *,
    title: str,
    evidence: Dict[str, Any],
    candidate: Dict[str, Any],
) -> int:
    candidate_evidence = (
        candidate.get("evidence_jsonb") or candidate.get("evidence") or {}
    )
    score = 0

    executor_host = _first_fingerprint(evidence, ("target_host", "host"))
    candidate_host = _first_fingerprint(candidate_evidence, ("target_host", "host"))
    if executor_host and candidate_host:
        if executor_host != candidate_host:
            return 0
        score += 100

    executor_product = _first_fingerprint(evidence, ("product_key",))
    candidate_product = _first_fingerprint(candidate_evidence, ("product_key",))
    if executor_product and candidate_product:
        if executor_product != candidate_product:
            return 0
        score += 80

    blob_values = [candidate.get("title"), candidate.get("body")]
    if isinstance(candidate_evidence, dict):
        blob_values.extend([
            candidate_evidence.get("topic"),
            candidate_evidence.get("target_topic"),
            candidate_evidence.get("query"),
            candidate_evidence.get("target_query"),
        ])
    candidate_blob = _norm_fingerprint(
        " ".join(str(v) for v in blob_values if v)
    )
    if any(topic in candidate_blob for topic in _executor_topics(evidence)):
        score += 60

    if _normalize_title_for_identity(title) == _normalize_title_for_identity(
        candidate.get("title") or ""
    ):
        score += 20

    return score


async def _resolve_executor_parent_audit_run_id(
    *,
    executor_run_id: str,
    parent_audit_run_id: Optional[str],
) -> Optional[str]:
    if parent_audit_run_id:
        return parent_audit_run_id
    try:
        from db.executor_runs import fetch_executor_run_by_id

        run = await fetch_executor_run_by_id(run_id=executor_run_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "executor parent audit lookup raised run_id=%s: %s",
            executor_run_id, str(exc)[:200],
        )
        return None
    return (run or {}).get("parent_audit_run_id")


async def _find_executor_parent_task_id(
    *,
    merchant_id: str,
    parent_audit_run_id: Optional[str],
    lever: Optional[str],
    title: str,
    evidence: Dict[str, Any],
) -> Optional[str]:
    if not parent_audit_run_id:
        return None
    try:
        from db.merchant_tasks import find_executor_parent_task_candidates

        candidates = await find_executor_parent_task_candidates(
            merchant_id=merchant_id,
            parent_audit_run_id=parent_audit_run_id,
            lever=lever,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "executor parent task lookup raised merchant=%s audit=%s: %s",
            merchant_id, parent_audit_run_id, str(exc)[:200],
        )
        return None

    best_score = 0
    best: List[Dict[str, Any]] = []
    for candidate in candidates:
        score = _executor_parent_match_score(
            title=title,
            evidence=evidence,
            candidate=candidate,
        )
        if score > best_score:
            best_score = score
            best = [candidate]
        elif score == best_score and score > 0:
            best.append(candidate)

    if best_score <= 0 or len(best) != 1:
        return None
    return best[0].get("task_id")


async def materialize_task_from_executor(
    *,
    merchant_id: str,
    executor_run_id: str,
    agent_name: str,
    evidence: Dict[str, Any],
    parent_audit_run_id: Optional[str] = None,
    # P1.1 — explicit overrides from ExecutorResult.task_*. When the
    # agent emitted RESULT_TYPE_HUMAN_TASK_RECOMMENDED, it supplies
    # the task framing directly. Otherwise we fall back to the
    # per-agent _summarize_executor_work mapping (legacy behavior).
    title: Optional[str] = None,
    body: Optional[str] = None,
    severity: Optional[str] = None,
    lever: Optional[str] = None,
) -> Optional[str]:
    """Some executor agents produce work for humans (sitemap diff,
    content brief). Caller invokes this to surface that work as a
    merchant_task linked back to the executor_runs row.

    Returns the new task_id or None when this evidence shape doesn't
    warrant a task (e.g. empty diff).
    """
    from db.merchant_tasks import record_task_created

    if not merchant_id or not executor_run_id or not agent_name:
        return None

    # Prefer explicit values from the agent's ExecutorResult; fall
    # back to the per-agent summarizer for agents that haven't been
    # updated yet.
    if title is None:
        title, body, severity, lever = _summarize_executor_work(
            agent_name, evidence,
        )
    if not title:
        return None  # this agent's run produced no human-actionable work

    resolved_parent_audit_run_id = await _resolve_executor_parent_audit_run_id(
        executor_run_id=executor_run_id,
        parent_audit_run_id=parent_audit_run_id,
    )
    parent_task_id = await _find_executor_parent_task_id(
        merchant_id=merchant_id,
        parent_audit_run_id=resolved_parent_audit_run_id,
        lever=lever,
        title=title,
        evidence=evidence,
    )

    new_task_id = await record_task_created(
        merchant_id=merchant_id,
        title=title,
        body=body or "",
        severity=severity or "medium",
        lever=lever,
        parent_audit_run_id=resolved_parent_audit_run_id,
        source_executor_run_id=executor_run_id,
        parent_task_id=parent_task_id,
        assigned_to_agent=agent_name,
        evidence=evidence,
    )

    # Supersede prior same-identity pending executor tasks so re-audits update
    # in place instead of stacking duplicate rows. The audit-ladder path
    # (materialize_tasks_from_audit) already does this; the executor path did
    # not, so a content_brief run inserted a fresh row every audit cycle. The
    # identity is (merchant_id, lever, title) + target_host/product_key — with
    # per-query titles (content_brief now names its real target_query), a
    # re-audit of the same failing query collapses onto the prior task.
    # Best-effort: a supersession failure never invalidates the new task.
    if new_task_id and resolved_parent_audit_run_id:
        try:
            await _supersede_prior_pending(
                merchant_id=merchant_id,
                new_task_id=new_task_id,
                action={"title": title, "lever": lever, "evidence": evidence},
                audit_run_id=resolved_parent_audit_run_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "task_queue: executor supersede raised for task=%s: %s",
                new_task_id, str(exc)[:200],
            )

    return new_task_id


def _summarize_executor_work(
    agent_name: str,
    evidence: Dict[str, Any],
) -> tuple:
    """Per-agent task-summary mapping. Returns (title, body, severity,
    lever). Returns (None, None, None, None) when this evidence shape
    doesn't warrant a task — caller skips."""
    if not isinstance(evidence, dict):
        return (None, None, None, None)

    if agent_name == "sitemap_freshness_monitor":
        missing = evidence.get("missing_from_sitemap_count") or 0
        orphan = evidence.get("orphan_in_sitemap_count") or 0
        if missing == 0 and orphan == 0:
            return (None, None, None, None)
        sev = (
            "high" if missing >= 20 or orphan >= 50
            else "medium" if missing > 0 else "low"
        )
        host = evidence.get("merchant_host") or "your-domain"
        title = (
            f"Sitemap drift on {host}: "
            f"{missing} catalog products missing, {orphan} orphan URLs"
        )
        sample = evidence.get("missing_from_sitemap_sample") or []
        body = (
            f"Your published sitemap at {evidence.get('sitemap_url')} is "
            f"out of sync with your live catalog. {missing} products are "
            f"in your catalog but missing from the sitemap; {orphan} URLs "
            f"are in the sitemap but no longer in the catalog. "
            f"Sample missing URLs:\n"
            + "\n".join(f"  - {u}" for u in sample[:5])
        )
        return (title, body, sev, "sitemap_freshness")

    if agent_name == "content_brief_generator":
        briefs = evidence.get("briefs") or []
        if not briefs:
            return (None, None, None, None)
        # One task per brief — caller should iterate, but for v1 we
        # roll up into a single task whose body lists all briefs.
        title = f"{len(briefs)} content brief(s) drafted for failed category queries"
        body_parts = ["We drafted briefs for the following queries:"]
        for b in briefs:
            body_parts.append(
                f"  - **{b.get('target_query', '?')}**: "
                f"{b.get('suggested_title', '?')} "
                f"(~{b.get('suggested_word_count') or 1500} words)"
            )
        body = "\n".join(body_parts)
        return (title, body, "medium", "content_brief")

    # gsc_url_submission_loop produces no human task — agent does
    # the work directly. The audit's action_items handle the
    # advisory side. Return None.
    return (None, None, None, None)
