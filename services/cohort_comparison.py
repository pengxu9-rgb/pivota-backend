"""PR-2b: cohort comparison helpers.

Pure-function extractors that turn the parent + cohort audit reports
(stored as report_jsonb on merchant_audit_runs and competitor_audit_runs)
into BD-pitch-friendly comparative views:

  - per_brand_query_breakdown: each brand's category queries + cited
    URL + match flag. Side-by-side rendering surfaces "Grüns ran query
    X and got nothing; SmartyPants ran their own query Y and got cited
    on healthline.com."

  - brand_mention_matrix: cross-cuts all 3 audits' competitor_brands
    lists. For each brand Gemini named in any grounded answer, count
    how many times each audit mentioned it. Headline pitch: "Gemini
    named SmartyPants in YOUR category queries 3 times. They named
    Grüns in SmartyPants' queries 0 times. The competitor has
    stronger AI mindshare in your own category."

Honest caveat baked into the docstrings: brand audits use auto-
generated queries based on each brand's product_type, so query texts
don't deterministically overlap. The brand_mention_matrix is the
actual cross-brand signal; per_brand_query_breakdown is per-brand
detail for context.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def extract_per_query_breakdown(
    report_jsonb: Optional[Dict[str, Any]],
    *,
    brand_label: str,
) -> List[Dict[str, Any]]:
    """For one audit, walk per_product → category_visibility.queries
    and emit one row per (product, query). Each row carries the brand
    label so multiple audits can be merged into one rendering.

    Skips the Vertex AI redirector URLs (they hide the actual cited
    host). When `top_cited_url` is a redirector, surface
    `cited_urls_count > 0` as the meaningful signal instead.
    """
    if not isinstance(report_jsonb, dict):
        return []
    out: List[Dict[str, Any]] = []
    for product_report in (report_jsonb.get("per_product") or []):
        if not isinstance(product_report, dict):
            continue
        cv = product_report.get("category_visibility") or {}
        queries = cv.get("queries") or []
        match_details = cv.get("match_details") or []
        # Index match_details by query for joining.
        md_by_query = {
            (md.get("query") or ""): md
            for md in match_details
            if isinstance(md, dict)
        }
        product_title = (product_report.get("product") or {}).get("title", "?")
        for q in queries:
            if not isinstance(q, dict):
                continue
            qtext = q.get("query") or ""
            md = md_by_query.get(qtext, {})
            cited_url = q.get("top_cited_url")
            is_redirector = isinstance(cited_url, str) and (
                "vertexaisearch.cloud.google.com" in cited_url
            )
            out.append({
                "brand": brand_label,
                "product_title": product_title,
                "query": qtext,
                "self_report_yes": q.get("self_report_yes"),
                "cited_urls_count": q.get("cited_urls_count") or 0,
                "matched_in_grounding": md.get("matched", False),
                "in_grounding": md.get("in_grounding", False),
                "title_match": md.get("title_match", False),
                "top_cited_url": (
                    None if is_redirector else cited_url
                ),
                "top_cited_url_was_redirector": is_redirector,
            })
    return out


def extract_brand_mentions(
    report_jsonb: Optional[Dict[str, Any]],
) -> Dict[str, int]:
    """Aggregate `category_visibility.competitor_brands` across all
    products in one audit. Returns `{brand_name_lowercase: total_times_cited}`.

    Lowercase keys for cross-audit join (different audits may have
    different casing for the same brand).
    """
    if not isinstance(report_jsonb, dict):
        return {}
    counter: Dict[str, int] = {}
    for product_report in (report_jsonb.get("per_product") or []):
        if not isinstance(product_report, dict):
            continue
        cv = product_report.get("category_visibility") or {}
        for brand_entry in (cv.get("competitor_brands") or []):
            if not isinstance(brand_entry, dict):
                continue
            name = brand_entry.get("name")
            cited = brand_entry.get("times_cited")
            if not isinstance(name, str) or not name.strip():
                continue
            try:
                cited = int(cited or 0)
            except (TypeError, ValueError):
                cited = 0
            key = name.strip().lower()
            counter[key] = counter.get(key, 0) + cited
    return counter


def build_brand_mention_matrix(
    audits: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Cross-cut N audits to produce a brand × audit citation matrix.

    `audits` is a list of `{label, mentions: {brand_lc: count}}`. The
    return shape:
      {
        "audits": [label, label, ...],
        "matrix": [
          {
            "brand": "SmartyPants",
            "total_mentions": 7,
            "by_audit": {"Grüns": 3, "Nordic Naturals": 4, "SmartyPants": 0},
            "audit_count": 2,
          },
          ...
        ],
      }

    Sorted by total_mentions descending. Audit_count tells the BD
    operator how many DIFFERENT audits cited this brand — a brand
    cited in 3/3 audits has stronger cross-category mindshare than
    one cited in 1/3.
    """
    audit_labels = [a.get("label", "?") for a in audits]
    # Build brand-canonical-name lookup so the rendered name uses the
    # casing from whichever audit cited it most.
    brand_canonical: Dict[str, str] = {}
    for a in audits:
        for brand_lc, count in (a.get("mentions") or {}).items():
            # Prefer the casing from the audit that cited this brand most.
            if brand_lc not in brand_canonical:
                # Find the canonical (cased) form by walking competitor_brands
                brand_canonical[brand_lc] = brand_lc.title()

    rows: List[Dict[str, Any]] = []
    all_brands = set()
    for a in audits:
        all_brands.update((a.get("mentions") or {}).keys())

    for brand_lc in all_brands:
        by_audit: Dict[str, int] = {}
        total = 0
        audit_count = 0
        for a in audits:
            label = a.get("label", "?")
            count = (a.get("mentions") or {}).get(brand_lc, 0)
            by_audit[label] = count
            total += count
            if count > 0:
                audit_count += 1
        rows.append({
            "brand": brand_canonical.get(brand_lc, brand_lc.title()),
            "brand_lower": brand_lc,
            "total_mentions": total,
            "by_audit": by_audit,
            "audit_count": audit_count,
        })

    rows.sort(key=lambda r: (-r["total_mentions"], r["brand_lower"]))
    return {
        "audits": audit_labels,
        "matrix": rows,
    }


def build_cohort_comparison(
    parent_report: Optional[Dict[str, Any]],
    parent_label: str,
    cohort_runs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """End-to-end: take parent_audit_run.report_jsonb plus the cohort
    runs (each with their own report_jsonb) and produce the
    comparison shape consumed by the cohort endpoint + future
    Markdown export.

    `cohort_runs` items are the dicts returned by
    db.competitor_audit_runs.cohort_for_parent_run, each with
    `competitor_brand`, `status`, and (when fetched with full row)
    `report_jsonb`. Rows lacking report_jsonb (status=failed, or
    fetched via the limited-fields accessor) contribute nothing to
    the comparison.

    Returns:
      {
        "summary": {parent, competitors_audited, brands_named_total},
        "per_query_breakdown": [...],   # one row per (audit, product, query)
        "brand_mention_matrix": {...},  # cross-cut of competitor mentions
        "caveat": "audits use auto-generated queries based on each
                   brand's product_type — query texts don't perfectly
                   overlap; brand_mention_matrix is the cross-brand signal."
      }
    """
    parent_per_query = extract_per_query_breakdown(
        parent_report, brand_label=parent_label,
    )
    parent_mentions = extract_brand_mentions(parent_report)

    audit_label_to_mentions = [{
        "label": parent_label,
        "mentions": parent_mentions,
    }]

    all_per_query = list(parent_per_query)
    succeeded_competitors = 0

    for cohort_row in cohort_runs:
        if cohort_row.get("status") != "succeeded":
            continue
        cohort_report = cohort_row.get("report_jsonb")
        if not cohort_report:
            continue
        succeeded_competitors += 1
        label = cohort_row.get("competitor_brand") or "?"
        all_per_query.extend(
            extract_per_query_breakdown(cohort_report, brand_label=label)
        )
        audit_label_to_mentions.append({
            "label": label,
            "mentions": extract_brand_mentions(cohort_report),
        })

    matrix = build_brand_mention_matrix(audit_label_to_mentions)

    # PR-2c: surface the category_used_for_audit info from each
    # cohort run so the comparison renders "all 3 brands audited
    # under <category>" framing when the parent_category override
    # was applied.
    cohort_categories_used: List[Optional[str]] = []
    for cohort_row in cohort_runs:
        if cohort_row.get("status") != "succeeded":
            continue
        report = cohort_row.get("report_jsonb") or {}
        meta = report.get("_cohort_meta") or {}
        cohort_categories_used.append(meta.get("category_used_for_audit"))

    # Determine if we have apples-to-apples comparison: all cohort
    # competitors audited under the same forced category as the
    # parent. When yes, downgrade the caveat to a milder framing.
    same_category_comparison = bool(
        cohort_categories_used
        and all(c is not None for c in cohort_categories_used)
        and len(set(cohort_categories_used)) == 1
    )

    if same_category_comparison:
        category = cohort_categories_used[0]
        caveat = (
            f"All {succeeded_competitors} cohort competitors were audited under "
            f"the parent's category ('{category}') — the brand_mention_matrix "
            f"is a true apples-to-apples comparison: how often Gemini cites each "
            f"brand when answering buyer queries about '{category}'. "
            f"Cohort competitors' individual visibility/attribution scores "
            f"reflect 'are they cited in {parent_label}'s category', NOT 'are "
            f"they cited in their own native category' — that's the right "
            f"framing for cross-brand pitch evidence."
        )
    else:
        caveat = (
            "Audits use auto-generated queries based on each brand's "
            "product_type. Query texts don't deterministically overlap "
            "across brands — brand_mention_matrix is the cross-brand "
            "signal. Per-query rows are per-audit detail. "
            "(Set category_override at audit time to force apples-to-apples.)"
        )

    return {
        "summary": {
            "parent_brand": parent_label,
            "competitors_audited": succeeded_competitors,
            "brands_named_across_audits": len(matrix["matrix"]),
            "queries_total": len(all_per_query),
            "category_override_applied": same_category_comparison,
            "category_used": (
                cohort_categories_used[0] if same_category_comparison else None
            ),
        },
        "per_query_breakdown": all_per_query,
        "brand_mention_matrix": matrix,
        "caveat": caveat,
    }
