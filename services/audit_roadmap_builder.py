"""Implementation roadmap generator (PR-8c).

Aggregates recommendation-engine-v2 action items (PR-8b) into
time-windowed phases with rolled-up activities + expected outcomes.
The polished Grüns report's "Implementation Roadmap" section
(5-phase 12-week breakdown) is the target shape — system-generated
audits today don't have any equivalent.

**Why phased rollup matters.**

A merchant reading 5-15 individual recommendations doesn't get a
sense of sequencing or what to expect at each milestone. A phased
roadmap converts the same recommendations into a project-plan
shape: "Weeks 1-4 we do X, Y, Z; expected outcome by week 4 is A.
Weeks 4-12 we do P, Q, R; expected outcome by week 12 is B."

**Input.** The full enriched action_items list from
`build_structured_report` (each carrying severity / title / phase /
owner / kpi_to_track / expected_outcome from PR-8b).

**Output.**

```python
{
  "phases": [
    {
      "phase_id": "week_1_to_4",
      "label": "Phase 1: Foundation",
      "weeks": "1-4",
      "activity_count": 3,
      "owners": ["pivota_ops", "merchant_brand_team"],
      "activities": [
        {"title": "Index your canonical PDPs...", "owner": "pivota_ops"},
        ...
      ],
      "expected_outcome": "Indexing accelerated; first editorial pitch in flight; ...",
    },
    ...
  ],
  "total_weeks": 24,
  "total_activities": 12,
}
```

Empty when no action items exist OR all items lack the `phase`
field (defensive — older audits without PR-8b enrichment).
"""

from __future__ import annotations

from typing import Any, Dict, List


# Canonical phase order — matches the buckets PR-8b assigns to
# action items. Renderer iterates in this order.
PHASE_ORDER = ["week_1_to_4", "week_4_to_12", "week_12_to_24"]


# Phase metadata for renderer-friendly labels + week ranges. Kept
# here so the labels don't need to be reconstructed by every renderer.
PHASE_METADATA: Dict[str, Dict[str, Any]] = {
    "week_1_to_4": {
        "phase_id": "week_1_to_4",
        "label": "Phase 1: Foundation",
        "weeks": "1-4",
        "weeks_low": 1,
        "weeks_high": 4,
        "default_outcome_template": (
            "Foundation laid: indexing acceleration started, first "
            "editorial pitches in flight, multi-platform infrastructure "
            "operational."
        ),
    },
    "week_4_to_12": {
        "phase_id": "week_4_to_12",
        "label": "Phase 2: Editorial Reinforcement & First Lift",
        "weeks": "4-12",
        "weeks_low": 4,
        "weeks_high": 12,
        "default_outcome_template": (
            "First citation lift visible; editorial publishers "
            "coverage cycles complete; gap-closing actions iterating."
        ),
    },
    "week_12_to_24": {
        "phase_id": "week_12_to_24",
        "label": "Phase 3: Sustained Capture & Trend Establishment",
        "weeks": "12-24",
        "weeks_low": 12,
        "weeks_high": 24,
        "default_outcome_template": (
            "Trend established; defensible citation share in target "
            "subcategories; ongoing monitoring + drift detection."
        ),
    },
}


def _bucket_actions_by_phase(
    action_items: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Group action items by their `phase` field. Items without a
    phase (older audits pre-PR-8b) are dropped — better to render an
    empty roadmap than mis-attribute actions."""
    buckets: Dict[str, List[Dict[str, Any]]] = {p: [] for p in PHASE_ORDER}
    for item in action_items or []:
        phase = item.get("phase")
        if phase in buckets:
            buckets[phase].append(item)
    return buckets


def _summarize_phase_outcome(
    *,
    phase_id: str,
    actions: List[Dict[str, Any]],
) -> str:
    """Compose the phase's expected_outcome by combining individual
    action expected_outcomes when available, falling back to the
    phase's default template otherwise.

    Strategy: pick the top 2 expected_outcomes from the phase's
    actions (filter to non-null), join with '; ', truncate to a
    reasonable length. If no action carries an expected_outcome,
    use the phase's default template.
    """
    outcomes = [
        (a.get("expected_outcome") or "").strip()
        for a in actions
        if a.get("expected_outcome")
    ]
    # De-dup while preserving order
    seen = set()
    unique_outcomes: List[str] = []
    for o in outcomes:
        if o and o not in seen:
            seen.add(o)
            unique_outcomes.append(o)
    if not unique_outcomes:
        return PHASE_METADATA[phase_id]["default_outcome_template"]
    composed = "; ".join(unique_outcomes[:2])
    # Soft cap to keep roadmap readable
    if len(composed) > 320:
        composed = composed[:317].rstrip() + "..."
    return composed


def _phase_owners(actions: List[Dict[str, Any]]) -> List[str]:
    """Unique owner list (preserves first-seen order) for the phase
    so the renderer can show "Owners: pivota_ops, merchant_brand_team"
    headers without reprocessing."""
    seen = set()
    owners: List[str] = []
    for a in actions:
        owner = a.get("owner")
        if owner and owner not in seen:
            seen.add(owner)
            owners.append(owner)
    return owners


def _phase_activity_summary(
    actions: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Per-activity rollup for the phase. Keeps title + owner only —
    the full action carrying body / kpi / etc. lives in
    `report.action_items`, the roadmap section is a navigation /
    sequencing layer above that."""
    return [
        {
            "title": (a.get("title") or "").strip(),
            "owner": (a.get("owner") or "joint"),
        }
        for a in actions
    ]


def build_implementation_roadmap(
    action_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Top-level entry point. Build the roadmap from PR-8b enriched
    action_items.

    Empty roadmap returned when no actions OR no actions carry
    a recognized phase — defensive against older audits or failure
    modes in PR-8b. Empty roadmap renders as a 'no roadmap available'
    note rather than crashing.
    """
    if not action_items:
        return {
            "phases": [],
            "total_weeks": 0,
            "total_activities": 0,
        }
    buckets = _bucket_actions_by_phase(action_items)
    phases: List[Dict[str, Any]] = []
    total_activities = 0
    for phase_id in PHASE_ORDER:
        actions_in_phase = buckets[phase_id]
        if not actions_in_phase:
            # Skip empty phases — don't render placeholder rows for
            # buckets where we have nothing to do
            continue
        meta = PHASE_METADATA[phase_id]
        phases.append({
            "phase_id": meta["phase_id"],
            "label": meta["label"],
            "weeks": meta["weeks"],
            "weeks_low": meta["weeks_low"],
            "weeks_high": meta["weeks_high"],
            "activity_count": len(actions_in_phase),
            "owners": _phase_owners(actions_in_phase),
            "activities": _phase_activity_summary(actions_in_phase),
            "expected_outcome": _summarize_phase_outcome(
                phase_id=phase_id, actions=actions_in_phase,
            ),
        })
        total_activities += len(actions_in_phase)
    total_weeks = (
        max((p["weeks_high"] for p in phases), default=0)
    )
    return {
        "phases": phases,
        "total_weeks": total_weeks,
        "total_activities": total_activities,
    }
