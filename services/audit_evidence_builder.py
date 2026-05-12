"""P4.3 — derive canonical evidence + findings from the legacy
brand_report shape.

Strategy: don't touch the giant agent_center_bd_report_service.py.
Instead, the audit_run_worker's verifying stage hands the completed
brand_report to this builder, which extracts evidence_items +
readiness_findings rows.

This is a shadow-write — same data, derived view persisted in
canonical form. Phase 6 will retire the legacy JSONB once consumers
migrate to the canonical tables; until then the canonical tables
are a derived index over the source of truth.

Why shadow-write (not dual-write inside build_structured_report):
  - agent_center_bd_report_service.py is 3000+ lines and touched
    by many overlapping PRs (PR-7a/b/c/d/e + PR-8a-d, etc.)
  - Adding writes inline increases merge-conflict surface and
    couples canonical-table writes to every report-shape change
  - A separate builder lets P4 evolve the canonical schema without
    re-opening the giant report builder

What this module extracts (best-effort — missing fields silently
skip):
  - grounding_chunk evidence: per_product[*].evidence_quotes
  - competitor_mention evidence: cross_product_competitors
  - url_match evidence: per_product[*].raw.merchant_store_attribution
  - merchant_visible_via_retailers_only finding: verdict label +
    attribution score mismatch
  - category_visibility_low finding: avg_category_visibility < 40
  - integration_state_incomplete finding: merchant_view tracking
    block signals
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Confidence values per evidence/finding type. Pinned constants make
# it easy to audit confidence calibration in one place rather than
# spreading magic numbers through the extractor.
CONFIDENCE_EVIDENCE_HIGH = 90
CONFIDENCE_EVIDENCE_MEDIUM = 70
CONFIDENCE_EVIDENCE_LOW = 50

CONFIDENCE_FINDING_HIGH = 85
CONFIDENCE_FINDING_MEDIUM = 65


# Allowed phase values (mirrors db.audit_evidence.PHASE_*). Unknown
# values default to None — the action still inserts, just without
# a phase classification.
_VALID_PHASES = frozenset({
    "week_1_to_4", "week_4_to_12", "week_12_to_24",
})


# Lever taxonomy. Mirrors db.audit_evidence.LEVER_* but kept as a
# string set here so the extractor doesn't have to import the
# accessor module just to validate.
_VALID_LEVERS = frozenset({
    "pivota_integration", "content_creation", "pdp_optimization",
    "sitemap_hygiene", "kol_outreach", "editorial_outreach",
})


# =====================================================================
# Evidence extraction
# =====================================================================


def extract_evidence_items(
    brand_report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return a list of evidence-item dicts ready for
    insert_evidence_item. Each dict has:
      {evidence_type, payload, product_key?, confidence?}

    Order matters less than coverage — the writer iterates and
    inserts; sort order within evidence_items table is by
    created_at (set at insert time).
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(brand_report, dict):
        return out

    # Per-product evidence_quotes (PR-7e) → grounding_chunk evidence.
    for product in (brand_report.get("per_product") or []):
        if not isinstance(product, dict):
            continue
        product_key = _product_key_from_report(product)
        quotes = product.get("evidence_quotes") or []
        for q in quotes:
            if not isinstance(q, dict):
                continue
            excerpt = (q.get("excerpt_text") or "").strip()
            host = (q.get("source_host") or "").strip()
            if not excerpt:
                continue
            out.append({
                "evidence_type": "grounding_chunk",
                "payload": {
                    "host": host,
                    "source_title": q.get("source_title"),
                    "excerpt_text": excerpt[:1000],
                    "query": q.get("query"),
                    "attribution_path": q.get("attribution_path"),
                },
                "product_key": product_key,
                "confidence": (
                    CONFIDENCE_EVIDENCE_HIGH if host
                    else CONFIDENCE_EVIDENCE_MEDIUM
                ),
            })

    # Cross-product competitor mentions → competitor_mention evidence.
    competitors = brand_report.get("cross_product_competitors") or []
    for c in competitors:
        if not isinstance(c, dict):
            continue
        host = (c.get("host") or "").strip()
        if not host:
            continue
        out.append({
            "evidence_type": "competitor_mention",
            "payload": {
                "host": host,
                "times_cited": c.get("times_cited"),
            },
            "product_key": None,  # brand-level
            "confidence": CONFIDENCE_EVIDENCE_MEDIUM,
        })

    # Per-product merchant URL matches → url_match evidence.
    for product in (brand_report.get("per_product") or []):
        if not isinstance(product, dict):
            continue
        product_key = _product_key_from_report(product)
        raw = product.get("raw") or {}
        attribution = raw.get("merchant_store_attribution") or {}
        # Each run that hit a URL match becomes a row.
        for run in (attribution.get("raw_runs") or []):
            if not isinstance(run, dict):
                continue
            url_match = run.get("url_match")
            if not isinstance(url_match, dict):
                continue
            if not url_match.get("matched"):
                continue
            out.append({
                "evidence_type": "url_match",
                "payload": {
                    "matched_url": url_match.get("matched_url"),
                    "matched_in": url_match.get("matched_in"),
                    "query": run.get("query"),
                },
                "product_key": product_key,
                "confidence": CONFIDENCE_EVIDENCE_HIGH,
            })

    return out


# =====================================================================
# Finding extraction
# =====================================================================


def extract_findings(
    brand_report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return a list of finding dicts ready for insert_finding.
    Each dict has:
      {finding_type, payload, severity, product_key?,
       confidence?, short_summary?}
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(brand_report, dict):
        return out

    aggregate = brand_report.get("aggregate") or {}
    avg_vis = aggregate.get("avg_visibility")
    avg_attr = aggregate.get("avg_attribution")
    avg_cat = aggregate.get("avg_category_visibility")
    verdict_label = aggregate.get("brand_verdict_label") or ""

    # Paradox finding — "visible via retailers" with weak attribution.
    # The hand-written Grüns report led with this; PR-8a executive
    # summary builder consumes this finding to fire the paradox
    # narrative template.
    visible_via_retailers = (
        "VISIBLE VIA RETAILERS" in str(verdict_label).upper()
    )
    weak_attribution = (
        isinstance(avg_attr, (int, float)) and avg_attr < 30
    )
    if visible_via_retailers and weak_attribution:
        out.append({
            "finding_type": "merchant_visible_via_retailers_only",
            "severity": "high",
            "payload": {
                "verdict_label": verdict_label,
                "avg_visibility": avg_vis,
                "avg_attribution": avg_attr,
                "avg_category_visibility": avg_cat,
            },
            "confidence": CONFIDENCE_FINDING_HIGH,
            "short_summary": (
                f"Brand surfaces via editorial / retailer mentions "
                f"(visibility={avg_vis}) but first-party attribution "
                f"is weak ({avg_attr})."
            ),
        })

    # Low category visibility finding.
    if isinstance(avg_cat, (int, float)) and avg_cat < 40:
        out.append({
            "finding_type": "category_visibility_low",
            "severity": "medium" if avg_cat >= 20 else "high",
            "payload": {
                "avg_category_visibility": avg_cat,
                "products_with_category_data": aggregate.get(
                    "products_succeeded",
                ),
            },
            "confidence": CONFIDENCE_FINDING_HIGH,
            "short_summary": (
                f"Category-open queries surface the brand only "
                f"{int(avg_cat)}% of the time."
            ),
        })

    # First-party PDP indexing gap — Pivota canonical URLs in use.
    # When the audit used the Pivota canonical PDP for any product,
    # that signals the merchant's own URL was unavailable / not
    # indexable. Phase 5 verifier `pdp_in_sitemap` will produce
    # paired verification evidence.
    audited_via_pivota = brand_report.get(
        "audited_via_pivota_canonical",
    ) or []
    # NOTE: that field on the brand_report itself isn't populated
    # today (it's on the audit response, not the report). Check
    # per-product url_source instead.
    pivota_used_count = 0
    for p in (brand_report.get("per_product") or []):
        if not isinstance(p, dict):
            continue
        mv = p.get("merchant_view") or {}
        headline = mv.get("headline") or {}
        if headline.get("audited_via_pivota_canonical"):
            pivota_used_count += 1
    if pivota_used_count > 0:
        out.append({
            "finding_type": "first_party_pdp_indexing_gap",
            "severity": "medium",
            "payload": {
                "products_audited_via_pivota_canonical": (
                    pivota_used_count
                ),
                "total_products": aggregate.get("products_succeeded"),
            },
            "confidence": CONFIDENCE_FINDING_MEDIUM,
            "short_summary": (
                f"{pivota_used_count} of "
                f"{aggregate.get('products_succeeded') or '?'} "
                f"products audited against Pivota canonical PDP "
                f"(merchant's own URL not available / not indexable)."
            ),
        })

    # Integration state incomplete — check the first product's
    # tracking block. Integration state is merchant-level so all
    # per_product reports carry the same value.
    per_product = brand_report.get("per_product") or []
    if per_product and isinstance(per_product[0], dict):
        mv = per_product[0].get("merchant_view") or {}
        tracking = mv.get("tracking") or {}
        integration = tracking.get("integration_state") or {}
        if isinstance(integration, dict):
            phase_0_complete = integration.get("phase_0_complete")
            if phase_0_complete is False:
                out.append({
                    "finding_type": "integration_state_incomplete",
                    "severity": "critical",
                    "payload": dict(integration),
                    "confidence": CONFIDENCE_FINDING_HIGH,
                    "short_summary": (
                        "Pivota integration incomplete — auditing "
                        "results reflect partial pipeline. Complete "
                        "onboarding to unlock the full action loop."
                    ),
                })

    return out


# =====================================================================
# Action extraction
# =====================================================================


def extract_actions(
    brand_report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return a list of action-item dicts ready for insert_action.
    Each dict has the PR-8b shape:
      {lever, title, body, severity, owner, kpi_to_track,
       expected_outcome, phase, product_key?}

    Sources in the brand_report (priority order):
      - per_product[*].merchant_view.actions — the AUTHORITATIVE
        unified merged list (action_items + playbook_actions +
        integration_action; built by build_structured_report and
        enriched by _enrich_action_items_v2)
      - per_product[*].action_items (legacy PR-6 / P1.1 flat list —
        kept for back-compat with older fixtures / replayed audits)
      - per_product[*].action_ladder (legacy PR-8b enriched bucket)
      - per_product[*].implementation_roadmap (PR-8c phase
        containers; nested activities ARE actions)

    De-dup: if the same (lever, title) appears for multiple products
    (e.g. brand-level "complete Pivota integration"), keep only the
    first. Brand-level actions are written with product_key=None.

    Pre-fix history: extract_actions previously ONLY looked at
    per_product[*].action_items. After build_structured_report's
    merchant_view refactor, actions moved to merchant_view.actions
    and the per_product.action_items field became stale. Combined
    with _normalize_action's strict `lever`-required rule (and
    _generate_action_items not setting lever today), 100% of
    actions got dropped — Gate 5 of the deploy validation pipeline
    observed action_plan_items=0 across multiple production audits.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(brand_report, dict):
        return out

    seen: set = set()  # (lever, title) tuples we've already emitted

    def _emit(action: Any, product_key: Optional[str]) -> None:
        normalized = _normalize_action(action, product_key)
        if normalized is None:
            return
        dedupe_key = (normalized["lever"], normalized["title"])
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        out.append(normalized)

    for product in (brand_report.get("per_product") or []):
        if not isinstance(product, dict):
            continue
        product_key = _product_key_from_report(product)

        # PRIMARY source: per_product[*].merchant_view.actions
        # build_structured_report puts the unified merged list here
        # (see agent_center_bd_report_service.py:3840 — "actions":
        # merged_actions in _build_merchant_view's return value).
        merchant_view = product.get("merchant_view") or {}
        if isinstance(merchant_view, dict):
            for action in (merchant_view.get("actions") or []):
                _emit(action, product_key)

        # Legacy back-compat path 1: per_product[*].action_items.
        # Modern builders don't emit at this path but test fixtures
        # and older audits may still have it populated. Kept so the
        # extractor doesn't regress for any existing audit replay.
        for action in (product.get("action_items") or []):
            _emit(action, product_key)

        # Legacy back-compat path 2: per_product[*].action_ladder.
        # Nested under `actions` (or sometimes the top-level value
        # IS the list).
        ladder = product.get("action_ladder") or {}
        if isinstance(ladder, dict):
            ladder_actions = ladder.get("actions") or []
        elif isinstance(ladder, list):
            ladder_actions = ladder
        else:
            ladder_actions = []
        for action in ladder_actions:
            _emit(action, product_key)

        # implementation_roadmap: PR-8c phase containers. Each
        # phase has an `activities` list; each activity is an
        # action with the phase pre-set.
        roadmap = product.get("implementation_roadmap") or {}
        if isinstance(roadmap, dict):
            phases = roadmap.get("phases") or []
        else:
            phases = []
        for phase_block in phases:
            if not isinstance(phase_block, dict):
                continue
            phase_id = phase_block.get("phase_id") or phase_block.get("label")
            for activity in (phase_block.get("activities") or []):
                # activities can be plain strings or action dicts;
                # only dicts are normalizable.
                if not isinstance(activity, dict):
                    continue
                # Inject phase from the roadmap container if the
                # activity itself didn't specify one. Copy first so
                # we don't mutate the brand_report.
                activity_copy = dict(activity)
                if (
                    not activity_copy.get("phase")
                    and isinstance(phase_id, str)
                    and phase_id in _VALID_PHASES
                ):
                    activity_copy["phase"] = phase_id
                _emit(activity_copy, product_key)

    return out


# Fallback lever assigned when an action source produces an item
# without one. The lever column is a categorization tag; missing
# levers should NOT cause the whole action to be dropped (which is
# what the prior strict-required rule did, costing us every
# severity+title-only action that _generate_action_items emits).
_DEFAULT_LEVER = "general_recommendation"


def _derive_lever_from_title(title: str) -> str:
    """Best-effort lever inference from action title keywords. Used
    as a fallback when an action dict has no explicit lever field
    (typical for _generate_action_items output, which is
    severity+title+body+evidence shaped without a categorization
    tag). Keeps action_plan_items.lever meaningful for grouping
    without forcing every caller to remember to set it."""
    if not title:
        return _DEFAULT_LEVER
    title_lower = title.lower()
    # Most specific signals first.
    if (
        "search console" in title_lower
        or "indexing" in title_lower
        or "index your" in title_lower  # matches "Index your canonical PDPs"
        or "url inspection" in title_lower
        or "sitemap" in title_lower
        or "canonical pdp" in title_lower
    ):
        return "indexing_acceleration"
    if (
        "editorial" in title_lower
        or "publisher" in title_lower
        or "outreach" in title_lower
        or "pitch" in title_lower
    ):
        return "editorial_outreach"
    if (
        "content" in title_lower
        or "brief" in title_lower
        or "article" in title_lower
    ):
        return "content_publishing"
    if (
        "competitor" in title_lower
        or "cohort" in title_lower
        or "competitive" in title_lower
    ):
        return "competitive_response"
    if (
        "integration" in title_lower
        or "onboard" in title_lower
        or "pivota" in title_lower
    ):
        return "pivota_integration"
    return _DEFAULT_LEVER


def _normalize_action(
    raw: Any, product_key: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Normalize a raw action dict (from various report shapes) to
    the insert_action contract. Returns None ONLY if the action
    lacks a title — every action must be human-presentable. Missing
    lever is tolerated (derived from title keywords) so the
    severity+title-only shape produced by _generate_action_items
    persists instead of being silently dropped.

    Pre-fix this required (lever AND title); the strict rule meant
    every _generate_action_items output was rejected, since that
    function emits severity+title+body+evidence shaped dicts with
    no `lever` field set. Gate 5 of deploy validation observed
    action_plan_items=0 across multiple production audits because
    of this.
    """
    if not isinstance(raw, dict):
        return None
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    lever = (raw.get("lever") or "").strip() or _derive_lever_from_title(title)

    # Phase validation — only emit phases the canonical taxonomy
    # accepts. PR-8c emits these labels directly so this is mostly
    # a passthrough.
    phase = raw.get("phase")
    if phase and phase not in _VALID_PHASES:
        phase = None

    # owner / kpi / outcome are optional PR-8b fields; pass through
    # what's present, leave others None.
    return {
        "lever": lever,
        "title": title,
        "body": (raw.get("body") or "").strip() or None,
        "severity": raw.get("severity"),
        "owner": raw.get("owner"),
        "kpi_to_track": raw.get("kpi_to_track"),
        "expected_outcome": raw.get("expected_outcome"),
        "phase": phase,
        "product_key": product_key,
    }


# =====================================================================
# Persist — calls the P4.2 accessors
# =====================================================================


async def persist_canonical_evidence(
    *,
    audit_run_id: str,
    brand_report: Dict[str, Any],
    merchant_id: Optional[str] = None,
) -> Dict[str, int]:
    """Extract + persist evidence_items + readiness_findings +
    action_plan_items for one brand_report.

    P5.8.2 idempotency: each insert is keyed by a deterministic
    sha256(audit_run_id|item_type|item_signature). On worker
    crash + reclaim, re-running persist hits the partial unique
    index on (audit_run_id, idempotency_key) → ON CONFLICT skips
    instead of doubling rows. Idempotent-skips are counted in
    `*_deduped` so the summary distinguishes them from genuine
    persistence failures.

    P5.8.1 tenancy: merchant_id is plumbed through every accessor
    call so the canonical tables carry merchant scope at the
    column level.

    Best-effort: persistence failures don't fail the audit
    lifecycle. Returns a count summary for the worker's
    partial_result tracking.
    """
    from db.audit_evidence import (
        compute_canonical_idempotency_key,
        insert_action, insert_evidence_item, insert_finding,
    )

    summary = {
        "evidence_items_inserted": 0,
        "evidence_items_deduped": 0,  # P5.8.2
        "evidence_items_failed": 0,
        "findings_inserted": 0,
        "findings_deduped": 0,
        "findings_failed": 0,
        "actions_inserted": 0,
        "actions_deduped": 0,
        "actions_failed": 0,
    }

    # Pre-flight: count what we'll attempt to insert. Lets us
    # distinguish "ON CONFLICT skipped a row that exists" (deduped)
    # from "INSERT raised an unrelated error" (failed). With the
    # accessor returning None for both cases, we re-query after
    # each loop to attribute the None correctly.

    extracted_evidence = list(extract_evidence_items(brand_report))
    for ev in extracted_evidence:
        signature = _evidence_signature(ev)
        idem_key = compute_canonical_idempotency_key(
            audit_run_id=audit_run_id,
            item_type="evidence",
            item_signature=signature,
        )
        try:
            new_id = await insert_evidence_item(
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                evidence_type=ev["evidence_type"],
                payload=ev["payload"],
                product_key=ev.get("product_key"),
                confidence=ev.get("confidence"),
                idempotency_key=idem_key,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "persist_canonical_evidence: insert_evidence_item "
                "raised for audit=%s: %s",
                audit_run_id, str(exc)[:200],
            )
            new_id = None
        if new_id is None:
            # Attribute via re-lookup: if the row exists with our
            # idempotency_key it's a dedupe-skip, else a real failure.
            existed = await _evidence_exists_by_idem(
                audit_run_id=audit_run_id, idempotency_key=idem_key,
            )
            if existed:
                summary["evidence_items_deduped"] += 1
            else:
                summary["evidence_items_failed"] += 1
        else:
            summary["evidence_items_inserted"] += 1

    for finding in extract_findings(brand_report):
        signature = _finding_signature(finding)
        idem_key = compute_canonical_idempotency_key(
            audit_run_id=audit_run_id,
            item_type="finding",
            item_signature=signature,
        )
        try:
            new_id = await insert_finding(
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                finding_type=finding["finding_type"],
                payload=finding["payload"],
                severity=finding.get("severity"),
                product_key=finding.get("product_key"),
                confidence=finding.get("confidence"),
                short_summary=finding.get("short_summary"),
                idempotency_key=idem_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "persist_canonical_evidence: insert_finding "
                "raised for audit=%s: %s",
                audit_run_id, str(exc)[:200],
            )
            new_id = None
        if new_id is None:
            existed = await _finding_exists_by_idem(
                audit_run_id=audit_run_id, idempotency_key=idem_key,
            )
            if existed:
                summary["findings_deduped"] += 1
            else:
                summary["findings_failed"] += 1
        else:
            summary["findings_inserted"] += 1

    # P4.4: action_plan_items dual-write. Same best-effort pattern.
    for action in extract_actions(brand_report):
        signature = _action_signature(action)
        idem_key = compute_canonical_idempotency_key(
            audit_run_id=audit_run_id,
            item_type="action",
            item_signature=signature,
        )
        try:
            new_id = await insert_action(
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                lever=action["lever"],
                title=action["title"],
                body=action.get("body"),
                severity=action.get("severity"),
                owner=action.get("owner"),
                kpi_to_track=action.get("kpi_to_track"),
                expected_outcome=action.get("expected_outcome"),
                phase=action.get("phase"),
                product_key=action.get("product_key"),
                idempotency_key=idem_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "persist_canonical_evidence: insert_action "
                "raised for audit=%s: %s",
                audit_run_id, str(exc)[:200],
            )
            new_id = None
        if new_id is None:
            existed = await _action_exists_by_idem(
                audit_run_id=audit_run_id, idempotency_key=idem_key,
            )
            if existed:
                summary["actions_deduped"] += 1
            else:
                summary["actions_failed"] += 1
        else:
            summary["actions_inserted"] += 1

    return summary


# =====================================================================
# P5.8.2 — idempotency-signature + exists-by-idem helpers
# =====================================================================


def _evidence_signature(ev: Dict[str, Any]) -> str:
    """Deterministic substring distinguishing one evidence item
    from another for the same audit. Stable across worker re-runs
    because brand_report → extract_evidence_items is deterministic.

    Two evidence items collapse iff they would represent the same
    canonical truth (same type + product + host + excerpt prefix).
    """
    payload = ev.get("payload") or {}
    excerpt = (payload.get("excerpt_text") or "")[:80]
    host = payload.get("host") or ""
    matched_url = payload.get("matched_url") or ""
    return "|".join([
        str(ev.get("evidence_type") or ""),
        str(ev.get("product_key") or ""),
        host, excerpt, matched_url,
    ])


def _finding_signature(finding: Dict[str, Any]) -> str:
    """One finding per (type, product) within an audit. Re-running
    extract_findings on the same brand_report produces the same
    signature → ON CONFLICT skip."""
    return "|".join([
        str(finding.get("finding_type") or ""),
        str(finding.get("product_key") or ""),
    ])


def _action_signature(action: Dict[str, Any]) -> str:
    """One action per (lever, title, product). Title is truncated
    to 80 chars so minor prose churn from Gemini between re-runs
    doesn't produce a fresh signature."""
    return "|".join([
        str(action.get("lever") or ""),
        str(action.get("product_key") or ""),
        (action.get("title") or "")[:80],
    ])


async def _evidence_exists_by_idem(
    *, audit_run_id: str, idempotency_key: str,
) -> bool:
    """Lookup helper after a None return from insert_evidence_item.
    Distinguishes "ON CONFLICT (existing row) → idempotent skip"
    from "INSERT raised an unrelated error → real failure"."""
    from db.audit_evidence import evidence_items
    from db.database import database
    try:
        row = await database.fetch_one(
            evidence_items.select()
            .where(
                evidence_items.c.audit_run_id == audit_run_id,
                evidence_items.c.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        return row is not None
    except Exception:  # noqa: BLE001
        return False


async def _finding_exists_by_idem(
    *, audit_run_id: str, idempotency_key: str,
) -> bool:
    from db.audit_evidence import readiness_findings
    from db.database import database
    try:
        row = await database.fetch_one(
            readiness_findings.select()
            .where(
                readiness_findings.c.audit_run_id == audit_run_id,
                readiness_findings.c.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        return row is not None
    except Exception:  # noqa: BLE001
        return False


async def _action_exists_by_idem(
    *, audit_run_id: str, idempotency_key: str,
) -> bool:
    from db.audit_evidence import action_plan_items
    from db.database import database
    try:
        row = await database.fetch_one(
            action_plan_items.select()
            .where(
                action_plan_items.c.audit_run_id == audit_run_id,
                action_plan_items.c.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        return row is not None
    except Exception:  # noqa: BLE001
        return False


# =====================================================================
# Internal helpers
# =====================================================================


def _product_key_from_report(
    product_report: Dict[str, Any],
) -> Optional[str]:
    """Extract the product_key from a per-product report.
    Different shapes exist across the various PRs that touched
    build_structured_report; check all known locations."""
    # Most reliable: explicit product_key field if it exists
    pk = product_report.get("product_key")
    if isinstance(pk, str) and pk:
        return pk
    # Fall back to (platform, source_product_id) tuple if present
    platform = product_report.get("platform")
    source_id = product_report.get("source_product_id")
    if platform and source_id:
        return f"{platform}::{source_id}"
    # Fall back to merchant_view.headline.product_key
    mv = product_report.get("merchant_view") or {}
    headline = mv.get("headline") or {}
    pk = headline.get("product_key")
    if isinstance(pk, str) and pk:
        return pk
    return None
