"""W2 payoff — the honest visibility-tracking series.

Turns a merchant's audit history into a time-series a chart can render: brand-level
visibility / attribution / category scores over time, plus per-provider lines.

The load-bearing bit is HONESTY: a trend line only means something if the points are
COMPARABLE — measured on the same pinned prompt set (W2). Two points on different
prompt sets aren't a "drop", they're different questions. So each point carries its
`basis_id` and a `comparable_with_prev` flag; the chart draws a continuous line only
across same-basis points and breaks (annotates "measurement basis refreshed") where
a new basis appears. Without this the chart would re-introduce exactly the run-to-run
noise W2 removed.

Comparability is a property of the BASIS, not of adjacency: a check is comparable
to every earlier check on the same pinned prompt set, even when a differently-based
check ran in between (a merchant alternating two URL sets, say). Segments therefore
group ALL points sharing a basis — not just consecutive runs — and the chart
connects each basis's points across interleaved others.

SKU-coverage honesty (the second axis): every brand-level point is an AVERAGE over
the SKUs that run measured (report_jsonb.per_product). A point averaging 3 SKUs and
a point averaging 30 are different measurements even on the same prompt basis — so
each point also carries `sku_count` / `attempted_sku_count` and a `panel_id` (the
identity of the measured SKU set), and `panel_changes` marks where the measured set
differs from the previous check ("tracked products changed" — distinct from a prompt
refresh). The same per_product data, exploded per SKU, yields `per_sku`: one honest
mini-series per product so the chart can offer a per-SKU lens without a new query —
retroactive for every historical run, same trick as the per-provider lines.

Reuses audit_delta's basis extraction so this series and the pairwise "since your
last audit" delta can never disagree about basis identity.

Pure: callers fetch rows (oldest-first) via db.score_history_for_merchant and pass
them in; no I/O here.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Optional, Tuple

SCORE_KEYS = ("visibility", "attribution", "category_visibility")

# Per-SKU series included inline only up to this many SKUs (panels are pinned
# top-N / caller-chosen sets, small in practice). Beyond the cap the largest
# series are kept and `per_sku_truncated` is set — never a silent drop.
PER_SKU_SERIES_CAP = 50

# per_product verdict field → series score key. Per-SKU reports carry all three
# scores the brand average is built from (services/agent_center_bd_report_service
# _aggregate_brand_scores), so the SKU lens can render the same lines.
_VERDICT_SCORE_FIELDS = (
    ("visibility", "visibility_score"),
    ("attribution", "attribution_score"),
    ("category_visibility", "category_visibility_score"),
)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _basis_id(report: Mapping[str, Any]) -> Optional[str]:
    """The run's pinned-basis identity, via audit_delta (single source of truth)."""
    from services import audit_delta as ad

    return ad._prompt_set_id(report, ad._primary_report(report))


def _sku_key(pdp_url: Any) -> Optional[str]:
    """Stable per-SKU identity for the series: a short hash of the (lightly
    normalized) merchant PDP URL. The URL is what the re-audit panel itself is
    keyed on (jobs/scheduled_audit_job re-audits the prior report's
    title+merchant_pdp_url pairs), so it's stable across cron re-audits;
    content_key isn't guaranteed present in historical report shapes."""
    if not isinstance(pdp_url, str):
        return None
    url = pdp_url.strip().rstrip("/")
    if not url:
        return None
    # Scheme/host are case-insensitive; paths are not. Lowercase only up to
    # the path so "HTTPS://Shop.X.com/A" and "https://shop.x.com/A" agree.
    head, sep, tail = url.partition("://")
    if sep:
        host, slash, path = tail.partition("/")
        url = head.lower() + sep + host.lower() + slash + path
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _per_product_entries(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """(sku_key, title, pdp_url, scores) for each measured product in the run.
    Tolerates the shapes the pipeline has emitted across versions — entries
    without a usable PDP URL are skipped (no identity → can't be a series)."""
    per_product = report.get("per_product")
    if not isinstance(per_product, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in per_product:
        if not isinstance(entry, dict):
            continue
        key = _sku_key(entry.get("merchant_pdp_url"))
        if not key:
            continue
        product = entry.get("product") if isinstance(entry.get("product"), dict) else {}
        verdict = entry.get("verdict") if isinstance(entry.get("verdict"), dict) else {}
        out.append({
            "sku_key": key,
            "title": product.get("title"),
            "pdp_url": entry.get("merchant_pdp_url"),
            "scores": {sk: verdict.get(vf) for sk, vf in _VERDICT_SCORE_FIELDS},
        })
    return out


def _per_sku_mode_entries(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The per_sku-audit-mode counterpart of _per_product_entries: modern runs
    (the durable worker / URL wedge, audit_mode='per_sku') persist
    report_jsonb.per_sku_reports — a DIFFERENT shape with 4-dimension scores —
    and no per_product at all, so without this fallback their coverage reads
    unknown and the per-SKU lens is empty for exactly the freshest runs.

    Score mapping mirrors _per_sku_run_aggregate (agent_center_bd_report_service)
    EXACTLY, so a SKU's series composes with the run-level columns those runs
    persist: visibility = _overall_score (the SKU's weakest dimension — the
    headline AI-readiness number), attribution = the citation-dimension score,
    category_visibility = None (no such dimension in per_sku).

    Keyed by the run's own sku_key (urlwedge:* / catalog sku key) rather than a
    PDP-URL hash — mode purity by construction: a per_sku series can never merge
    with a legacy per_product series for the same product, which matters because
    the two modes write different score semantics (see _per_sku_prior_runs)."""
    holder: Mapping[str, Any] = report
    brand = report.get("brand_report")
    if isinstance(brand, Mapping):  # tolerate the wrapped shape, like audit_delta
        holder = brand
    per_sku = holder.get("per_sku_reports")
    if not isinstance(per_sku, list):
        return []
    from services.agent_center_bd_report_service import _overall_score

    out: List[Dict[str, Any]] = []
    for entry in per_sku:
        if not isinstance(entry, dict):
            continue
        key = entry.get("sku_key") or entry.get("content_key")
        if not isinstance(key, str) or not key.strip():
            continue
        citation = (entry.get("scores") or {}).get("citation")
        citation_score = citation.get("score") if isinstance(citation, dict) else None
        out.append({
            "sku_key": key.strip(),
            "title": entry.get("sku_title"),
            "pdp_url": None,  # per_sku entries don't carry the PDP URL top-level
            "scores": {
                "visibility": _overall_score(entry),
                "attribution": citation_score,
                "category_visibility": None,
            },
        })
    return out


def _panel_id(sku_keys: List[str]) -> Optional[str]:
    """Identity of the measured SKU set — order-independent, so two runs over
    the same products always agree regardless of audit order."""
    if not sku_keys:
        return None
    return hashlib.sha1("|".join(sorted(set(sku_keys))).encode("utf-8")).hexdigest()[:12]


def _attempted_count(report: Mapping[str, Any], measured: int) -> Optional[int]:
    """How many products the run TRIED to measure (measured + failed). Prefers
    the run's own aggregate counter; falls back to the failed[] list. None when
    the report predates both shapes — never guessed."""
    aggregate = report.get("aggregate")
    if isinstance(aggregate, dict):
        count = aggregate.get("products_count")
        # bool is an int subclass — a corrupt True would surface as "1 attempted"
        if isinstance(count, int) and not isinstance(count, bool) and count >= measured:
            return count
    failed = report.get("failed")
    if isinstance(failed, list):
        return measured + len(failed)
    return None


def _segment_by_basis(points: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[int]]:
    """Group points into per-basis segments across the WHOLE series, setting each
    point's `comparable_with_prev` (shares its basis with an EARLIER point — not
    necessarily the adjacent one). Consecutive-only grouping rendered an
    interleaved same-basis re-audit as a break even though basis pinning worked —
    the honesty rule is "same prompt set", not "same prompt set AND adjacent".
    Points with no basis (pre-pinning runs) can't be asserted comparable to
    anything, so each stays a singleton segment.

    Returns (segments, basis_changes); basis_changes marks where a NEW basis
    first appears — not every alternation back to an already-seen basis (those
    points continue their existing segment). Shared by the brand-level series
    and each per-SKU series so both lenses break lines by the same rule."""
    segments: List[Dict[str, Any]] = []
    seg_index_by_basis: Dict[str, int] = {}
    for i, p in enumerate(points):
        basis_id = p["basis_id"]
        si = seg_index_by_basis.get(basis_id) if basis_id else None
        if si is None:
            if basis_id:
                seg_index_by_basis[basis_id] = len(segments)
            segments.append({"basis_id": basis_id, "indices": [i]})
        else:
            segments[si]["indices"].append(i)
            p["comparable_with_prev"] = True
    basis_changes = [i for i, p in enumerate(points) if i > 0 and not p["comparable_with_prev"]]
    return segments, basis_changes


def build_tracking_series(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rows are OLDEST-first, each {run_id, requested_at, visibility, attribution,
    category_visibility, report_jsonb}. Returns the chart payload:

      points[]                — one per run, with scores, basis_id, provider_scores,
                                comparable_with_prev (shares its basis with an
                                EARLIER point — not necessarily the adjacent one —
                                → the line may connect it into that basis's segment),
                                plus SKU coverage: sku_count (products measured →
                                what the averages are over), attempted_sku_count
                                (measured + failed), panel_id (identity of the
                                measured SKU set).
      basis_changes[]         — point indices where a NEW basis first appears
                                (draw a break: a fresh comparison thread starts).
      panel_changes[]         — point indices where the measured SKU set differs
                                from the ADJACENT previous check (annotate
                                "tracked products changed" — a composition shift,
                                not a score movement). Adjacency-based on purpose:
                                unlike basis comparability, the reader's question
                                is "did this check cover the same products as the
                                last one". Never asserted across unknown panels.
      segments[]              — per-basis point groups ({basis_id, indices});
                                indices are ascending but need not be consecutive.
      per_sku                 — {sku_key: {title, pdp_url, points[], segments[],
                                basis_changes[]}} — one honest mini-series per
                                measured product, exploded from the same rows.
                                A SKU's points only cover runs that measured it;
                                its comparability follows the same basis rule.
      per_sku_truncated       — True when more than PER_SKU_SERIES_CAP SKUs exist
                                and only the most-covered ones are included.
      is_baseline_only        — <=1 point (nothing to trend yet).
    """
    from db.merchant_audit_runs import _provider_scores_from_report

    points: List[Dict[str, Any]] = []
    sku_series: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        report = r.get("report_jsonb") or {}
        # A run is one mode or the other: legacy runs persist per_product,
        # per_sku-mode runs persist per_sku_reports. Legacy first (its shape is
        # the one _extract_products_from_prior_report pins panels on).
        entries = _per_product_entries(report) or _per_sku_mode_entries(report)
        basis_id = _basis_id(report)
        point = {
            "run_id": r.get("run_id"),
            "date": _iso(r.get("requested_at")),
            "scores": {k: r.get(k) for k in SCORE_KEYS},
            "basis_id": basis_id,
            "provider_scores": _provider_scores_from_report(report),
            "comparable_with_prev": False,  # set below once its basis group is known
            "sku_count": len(entries) if entries else None,
            "attempted_sku_count": _attempted_count(report, len(entries)) if entries else None,
            "panel_id": _panel_id([e["sku_key"] for e in entries]),
        }
        points.append(point)
        for e in entries:
            series = sku_series.get(e["sku_key"])
            if series is None:
                series = sku_series[e["sku_key"]] = {
                    "title": e["title"],
                    "pdp_url": e["pdp_url"],
                    "points": [],
                }
            elif e["title"]:
                series["title"] = e["title"]  # freshest title wins
            series["points"].append({
                "run_id": point["run_id"],
                "date": point["date"],
                "scores": e["scores"],
                "basis_id": basis_id,
                "comparable_with_prev": False,
            })

    segments, basis_changes = _segment_by_basis(points)

    # Composition-shift markers: the measured SKU set changed vs the previous
    # check. Only asserted when BOTH sides' panels are known — a pre-panel-era
    # row (no per_product) is unknown, not "changed".
    panel_changes = [
        i for i in range(1, len(points))
        if points[i]["panel_id"] and points[i - 1]["panel_id"]
        and points[i]["panel_id"] != points[i - 1]["panel_id"]
    ]

    per_sku_truncated = len(sku_series) > PER_SKU_SERIES_CAP
    if per_sku_truncated:
        keep = sorted(
            sku_series.items(),
            # str() the title tiebreak: a corrupt non-string title in one stored
            # report must not make the whole endpoint 500 on a sort TypeError
            key=lambda kv: (-len(kv[1]["points"]), str(kv[1]["title"] or ""), kv[0]),
        )[:PER_SKU_SERIES_CAP]
        sku_series = dict(keep)
    per_sku: Dict[str, Dict[str, Any]] = {}
    for key, series in sku_series.items():
        sku_segments, sku_basis_changes = _segment_by_basis(series["points"])
        per_sku[key] = {**series, "segments": sku_segments, "basis_changes": sku_basis_changes}

    return {
        "points": points,
        "basis_changes": basis_changes,
        "panel_changes": panel_changes,
        "segments": segments,
        "per_sku": per_sku,
        "per_sku_truncated": per_sku_truncated,
        "is_baseline_only": len(points) <= 1,
    }
