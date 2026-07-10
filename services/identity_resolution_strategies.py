"""ADR-010 D-2 strategy plugins v1 (Phase A3) — propose, never apply.

Each strategy consumes the Lane-0 working-set classification
(scripts/step5_working_set.py) plus row detail and emits
`identity_resolution_proposals` rows via services.identity_resolution.
Nothing here mutates catalog_products or seeds; the engine applies only
approved proposals, and the Phase-B sweep auto-approves only the
allowlisted mechanical strategies.

Strategy -> proposal mapping (encoding the step-5 as-applied review rules):

  same_url_dup           lane2 groups (every row shares one normalized URL);
                         keeper = pick_canonical (serving-aligned).
                         confidence 0.99 — proven mechanical in step-5.
  campaign_clone         lane3 groups that collapse to ONE base slug or are
                         ALL campaign-marked (the reviewed keep-rule from
                         reports/step5/lane3_proposal_2026-07-10_as_applied);
                         confidence 0.9. Groups failing that rule become
                         label_only `campaign_clone_ambiguous` proposals —
                         visible review candidates, never auto-suppressed
                         (they were the mis-merge traps: size variants,
                         shade PDPs, distinct products behind one title).
  seed_first_party_twin  migration-139 predicate (external_seed row whose
                         content_key has a live NON-audit first-party
                         sibling); keeper = the first-party row. The audit-
                         sibling exclusion is load-bearing: 139's raw
                         predicate would suppress the wrong side there.
  junk_url               rows whose canonical_url is a redirect/tracking
                         artifact (vertexaisearch et al.) WITH a real-URL
                         live sibling to keep.
  multi_seller_observation
                         label_only for cross-merchant groups that are NOT a
                         duplicate store connection — the future resolver /
                         buy-box inputs; labeling protects them from ever
                         being mistaken for a dedup backlog.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from scripts.step5_lane3_campaign_clone_dedup import (
    CAMPAIGN_MARKER_RE,
    slug_evidence,
    url_slug,
)
from services.agent_pdp_view_assembler import pick_canonical
from services.identity_resolution import new_proposal
from services.pdp_matcher.deterministic import normalize_canonical_url

JUNK_URL_RE = re.compile(
    r"^https?://(vertexaisearch\.cloud\.google\.com|www\.google\.com/url|"
    r"[^/]+/grounding-api-redirect)/",
    re.IGNORECASE,
)

_REGION = re.compile(r"-(eu|ca|us|uk|au|intl|global|ukeu|uk-fr)$")
_COPYNUM = re.compile(r"(-copy(-\d+)?|-\d{1,3})$")
# A trailing number preceded by a unit/spec token is PRODUCT IDENTITY
# (spf-45 vs spf-50, 100ml vs 50ml), not a clone counter. The Tier-3 eval
# caught two live mis-merges (Merit "The Uniform"/"Effortless Set" SPF pairs)
# where the bare -\d strip ate the SPF number — never strip these.
_UNIT_NUMBER = re.compile(r"(spf|ml|g|gr|oz|mg|ct|pack|pcs|pk)[-_]?\d{1,4}$",
                          re.IGNORECASE)


def is_campaign_slug(slug: str) -> bool:
    """Campaign-marked, UNLESS the trailing number is a unit/spec (spf-45,
    100ml) — those are product identity, not clone counters."""
    if _UNIT_NUMBER.search(slug):
        return False
    return bool(CAMPAIGN_MARKER_RE.search(slug))


def base_slug(slug: str) -> str:
    prev: Optional[str] = None
    s = slug
    while s != prev:
        prev = s
        s = _REGION.sub("", s)
        if not _UNIT_NUMBER.search(s):
            s = _COPYNUM.sub("", s)
    return s


def _details_for(group: Dict[str, Any],
                 detail_by_key: Dict[str, Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    details = [detail_by_key[r["product_key"]] for r in group["rows"]
               if r["product_key"] in detail_by_key]
    if len(details) != len(group["rows"]) or len(details) < 2:
        return None  # changed between queries — skip, next sweep re-sees it
    return details


def strategy_same_url_dup(
    lane2_groups: List[Dict[str, Any]],
    detail_by_key: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    proposals = []
    for g in lane2_groups:
        details = _details_for(g, detail_by_key)
        if not details:
            continue
        keeper = pick_canonical(details)
        proposals.append(new_proposal(
            kind="suppress_dup",
            strategy="same_url_dup",
            subject_product_keys=[d["product_key"] for d in details],
            keeper_product_key=keeper["product_key"],
            merchant_id=g["merchant_id"],
            content_key=g["content_key"],
            confidence=0.99,
            evidence={
                "normalized_url": normalize_canonical_url(keeper.get("canonical_url")),
                "n_rows": len(details),
            },
        ))
    return proposals


def strategy_campaign_clone(
    lane3_groups: List[Dict[str, Any]],
    detail_by_key: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    proposals = []
    for g in lane3_groups:
        details = _details_for(g, detail_by_key)
        if not details:
            continue
        slugs = [url_slug(d.get("canonical_url")) for d in details]
        bases = {base_slug(s) for s in slugs}
        all_campaign = all(is_campaign_slug(s) for s in slugs)
        collapses = len(bases) == 1
        subject = [d["product_key"] for d in details]
        if collapses or all_campaign:
            keeper = pick_canonical(details)
            proposals.append(new_proposal(
                kind="suppress_dup",
                strategy="campaign_clone",
                subject_product_keys=subject,
                keeper_product_key=keeper["product_key"],
                merchant_id=g["merchant_id"],
                content_key=g["content_key"],
                confidence=0.9,
                evidence={"slugs": sorted(set(slugs))[:8],
                          "rule": "collapse_to_one_base" if collapses else "all_campaign_marked"},
            ))
        else:
            proposals.append(new_proposal(
                kind="label_only",
                strategy="campaign_clone_ambiguous",
                subject_product_keys=subject,
                merchant_id=g["merchant_id"],
                content_key=g["content_key"],
                confidence=0.3,
                evidence={"slugs": sorted(set(slugs))[:8],
                          "why": "distinct bases with clean slugs — possible size/shade/distinct products"},
            ))
    return proposals


def strategy_seed_first_party_twin(
    cross_groups: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Cross-merchant groups mixing external_seed with a NON-audit
    first-party platform: suppress the seed mirror, keep first-party."""
    proposals = []
    for g in cross_groups:
        rows = g["rows"]
        seed_rows = [r for r in rows if r["platform"] == "external_seed"]
        first_party = [r for r in rows
                       if r["platform"] not in ("external_seed", "url_audit")]
        if not seed_rows or not first_party:
            continue  # audit-only siblings are NOT a valid keeper (139 lesson)
        keeper = sorted(first_party, key=lambda r: r["product_key"])[0]
        proposals.append(new_proposal(
            kind="suppress_dup",
            strategy="seed_first_party_twin",
            subject_product_keys=[r["product_key"] for r in seed_rows] + [keeper["product_key"]],
            keeper_product_key=keeper["product_key"],
            content_key=g["content_key"],
            confidence=0.95,
            evidence={"first_party_merchant": keeper["merchant_id"],
                      "precedent": "migration 139 cross_merchant_redundant_external_seed"},
        ))
    return proposals


def strategy_junk_url(
    same_merchant_groups: List[Dict[str, Any]],
    detail_by_key: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Groups containing redirect/tracking-URL rows plus at least one
    real-URL sibling: suppress the junk rows, keep the best real one."""
    proposals = []
    for g in same_merchant_groups:
        details = _details_for(g, detail_by_key)
        if not details:
            continue
        junk = [d for d in details if JUNK_URL_RE.match(str(d.get("canonical_url") or ""))]
        real = [d for d in details if d not in junk]
        if not junk or not real:
            continue
        keeper = pick_canonical(real)
        proposals.append(new_proposal(
            kind="suppress_dup",
            strategy="junk_url",
            subject_product_keys=[d["product_key"] for d in junk] + [keeper["product_key"]],
            keeper_product_key=keeper["product_key"],
            merchant_id=g.get("merchant_id"),
            content_key=g["content_key"],
            confidence=0.97,
            evidence={"junk_urls": [str(d.get("canonical_url"))[:100] for d in junk[:4]]},
        ))
    return proposals


def strategy_multi_seller_observation(
    cross_groups: List[Dict[str, Any]],
    multi_domain_groups: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Label groups that are multi-SELLER observations, never dedup backlog:
    (a) cross-merchant groups that are neither a dup store connection nor a
    seed/first-party twin; (b) same-pseudo-merchant multi-domain groups (the
    external_seed brand+retailer pattern — theordinary+ulta, apple+bestbuy —
    and same-brand regional storefronts, per the lane-4 review verdicts)."""
    proposals = []
    for g in multi_domain_groups or []:
        proposals.append(new_proposal(
            kind="label_only",
            strategy="multi_seller_observation",
            subject_product_keys=[r["product_key"] for r in g["rows"]],
            merchant_id=g.get("merchant_id"),
            content_key=g["content_key"],
            confidence=0.8,
            evidence={"domains": sorted({
                str(r.get("source_domain") or "") for r in g["rows"]})[:6]},
        ))
    for g in cross_groups:
        spid_merchants: Dict[str, set] = defaultdict(set)
        for r in g["rows"]:
            spid = str(r.get("source_product_id") or "").strip()
            if spid:
                spid_merchants[spid].add(r["merchant_id"])
        if any(len(m) > 1 for m in spid_merchants.values()):
            continue  # duplicate store connection — lane-1 territory, not a label
        platforms = {r["platform"] for r in g["rows"]}
        if "external_seed" in platforms and len(platforms) > 1:
            continue  # twin strategy's territory
        proposals.append(new_proposal(
            kind="label_only",
            strategy="multi_seller_observation",
            subject_product_keys=[r["product_key"] for r in g["rows"]],
            content_key=g["content_key"],
            confidence=0.8,
            evidence={"merchants": sorted({r["merchant_id"] for r in g["rows"]})},
        ))
    return proposals


def build_all_proposals(
    report: Dict[str, Any],
    detail_by_key: Dict[str, Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Run every strategy over a working-set report. Returns proposals per
    strategy; the caller upserts them (engine dedupes on proposal_key)."""
    lanes = report.get("lanes", {})
    lane4_same_merchant = (
        lanes.get("lane4_multi_domain", [])
        + lanes.get("lane4_no_url_signal", [])
        + lanes.get("lane4_mixed_url_presence", [])
    )
    cross = (
        lanes.get("lane1_duplicate_store_connection", [])
        + lanes.get("lane4_seed_first_party_twin", [])
        + lanes.get("lane4_review_cross_merchant", [])
    )
    return {
        "same_url_dup": strategy_same_url_dup(
            lanes.get("lane2_same_url", []), detail_by_key),
        "campaign_clone": strategy_campaign_clone(
            lanes.get("lane3_campaign_clones", []), detail_by_key),
        "seed_first_party_twin": strategy_seed_first_party_twin(cross),
        "junk_url": strategy_junk_url(
            lanes.get("lane2_same_url", []) + lanes.get("lane3_campaign_clones", [])
            + lane4_same_merchant, detail_by_key),
        "multi_seller_observation": strategy_multi_seller_observation(
            cross, lanes.get("lane4_multi_domain", [])),
    }
