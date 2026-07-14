"""W2 tracking series — the basis-segmentation honesty is the point under test,
plus the SKU-coverage axis: per-point panel disclosure + per-SKU mini-series."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.audit_tracking_series import PER_SKU_SERIES_CAP, build_tracking_series

_T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _product(url, vis, att=None, cat=None, title=None):
    """A per_product entry in the shape build_structured_report persists."""
    return {
        "merchant_pdp_url": url,
        "product": {"title": title or url.rsplit("/", 1)[-1]},
        "verdict": {
            "visibility_score": vis,
            "attribution_score": att,
            "category_visibility_score": cat,
        },
    }


def _row(day, vis, att, cat, basis_id="sel_abc", per_product=None, failed=None):
    report = {
        "prompt_basis": {"selected_set_id": basis_id} if basis_id else {},
        "brand_rollup": {"citation_by_provider": {"gemini": {"median": vis}}},
    }
    if per_product is not None:
        # Real reports carry prompt_basis on each per-product entry (audit_delta's
        # _primary_report reads per_product[0]), not at the payload top level.
        report["per_product"] = [
            {**p, "prompt_basis": {"selected_set_id": basis_id} if basis_id else {}}
            for p in per_product
        ]
        report["failed"] = failed or []
    return {
        "run_id": f"run-{day}",
        "requested_at": _T0 + timedelta(days=day),
        "visibility": vis,
        "attribution": att,
        "category_visibility": cat,
        "report_jsonb": report,
    }


def test_same_basis_points_are_comparable():
    series = build_tracking_series([
        _row(0, 60, 45, 55),
        _row(30, 68, 52, 60),
    ])
    pts = series["points"]
    assert pts[0]["comparable_with_prev"] is False        # first point
    assert pts[1]["comparable_with_prev"] is True         # same basis → connect
    assert series["basis_changes"] == []
    assert len(series["segments"]) == 1                   # one continuous line
    assert series["is_baseline_only"] is False
    assert pts[1]["scores"] == {"visibility": 68, "attribution": 52, "category_visibility": 60}
    assert pts[1]["provider_scores"] == {"gemini": 68}


def test_basis_change_breaks_the_line():
    series = build_tracking_series([
        _row(0, 60, 45, 55, basis_id="sel_old"),
        _row(30, 62, 47, 57, basis_id="sel_old"),
        _row(60, 40, 30, 35, basis_id="sel_new"),   # basis refreshed here
        _row(90, 44, 33, 38, basis_id="sel_new"),
    ])
    pts = series["points"]
    assert [p["comparable_with_prev"] for p in pts] == [False, True, False, True]
    assert series["basis_changes"] == [2]                 # the refresh point
    # two segments: the old-basis stretch and the new-basis stretch
    assert [seg["indices"] for seg in series["segments"]] == [[0, 1], [2, 3]]
    assert [seg["basis_id"] for seg in series["segments"]] == ["sel_old", "sel_new"]


def test_single_run_is_baseline_only():
    series = build_tracking_series([_row(0, 60, 45, 55)])
    assert series["is_baseline_only"] is True
    assert series["points"][0]["comparable_with_prev"] is False
    assert series["basis_changes"] == []


def test_missing_basis_is_never_comparable():
    # A run that predates pinning (no basis) can't be asserted comparable to anything.
    series = build_tracking_series([
        _row(0, 60, 45, 55, basis_id=None),
        _row(30, 80, 60, 70, basis_id=None),
    ])
    assert [p["comparable_with_prev"] for p in series["points"]] == [False, False]
    assert series["basis_changes"] == [1]


def test_interleaved_same_basis_points_still_connect():
    # A merchant alternating two URL sets: comparability is a property of the
    # BASIS, not of adjacency. A same-basis re-audit must join its earlier
    # points' segment even when differently-based checks ran in between.
    series = build_tracking_series([
        _row(0, 60, 45, 55, basis_id="sel_a"),
        _row(1, 30, 20, 25, basis_id="sel_b"),   # other URL set interleaved
        _row(2, 32, 22, 27, basis_id="sel_b"),
        _row(3, 68, 52, 60, basis_id="sel_a"),   # back to the first set
    ])
    pts = series["points"]
    assert [p["comparable_with_prev"] for p in pts] == [False, False, True, True]
    # Two per-basis segments; sel_a spans the interleaved run.
    assert [seg["basis_id"] for seg in series["segments"]] == ["sel_a", "sel_b"]
    assert [seg["indices"] for seg in series["segments"]] == [[0, 3], [1, 2]]
    # One break: where the NEW basis first appeared. Returning to sel_a at
    # index 3 continues an existing thread — no break there.
    assert series["basis_changes"] == [1]


def test_empty_history():
    series = build_tracking_series([])
    assert series["points"] == []
    assert series["is_baseline_only"] is True
    assert series["per_sku"] == {}
    assert series["panel_changes"] == []
    assert series["per_sku_truncated"] is False


# ---------------------------------------------------------------------------
# SKU-coverage axis
# ---------------------------------------------------------------------------

_URL_A = "https://shop.example.com/products/serum"
_URL_B = "https://shop.example.com/products/toner"
_URL_C = "https://shop.example.com/products/mask"


def test_point_carries_sku_coverage():
    series = build_tracking_series([
        _row(0, 60, 45, 55, per_product=[
            _product(_URL_A, 70, 50, 60),
            _product(_URL_B, 50, 40, 50),
        ], failed=[{"pdp_url": _URL_C, "error": "probe failed"}]),
    ])
    p = series["points"][0]
    assert p["sku_count"] == 2                    # what the averages are over
    assert p["attempted_sku_count"] == 3          # measured + failed
    assert p["panel_id"] is not None


def test_pre_panel_era_rows_have_unknown_coverage():
    # A report with no per_product (legacy shape) → coverage is unknown, not 0.
    series = build_tracking_series([_row(0, 60, 45, 55)])
    p = series["points"][0]
    assert p["sku_count"] is None
    assert p["attempted_sku_count"] is None
    assert p["panel_id"] is None


def test_panel_id_is_order_independent_and_stable():
    series = build_tracking_series([
        _row(0, 60, 45, 55, per_product=[_product(_URL_A, 70), _product(_URL_B, 50)]),
        _row(30, 62, 47, 57, per_product=[_product(_URL_B, 52), _product(_URL_A, 72)]),
    ])
    pts = series["points"]
    assert pts[0]["panel_id"] == pts[1]["panel_id"]   # same set, different order
    assert series["panel_changes"] == []


def test_panel_change_is_marked():
    series = build_tracking_series([
        _row(0, 60, 45, 55, per_product=[_product(_URL_A, 70), _product(_URL_B, 50)]),
        _row(30, 40, 30, 35, per_product=[_product(_URL_A, 70), _product(_URL_C, 10)]),
    ])
    assert series["panel_changes"] == [1]             # composition shift, not a drop


def test_panel_change_never_asserted_across_unknown():
    # legacy row (no panel) between two known panels → no false "changed" marker
    series = build_tracking_series([
        _row(0, 60, 45, 55, per_product=[_product(_URL_A, 70)]),
        _row(30, 62, 47, 57),                                        # unknown panel
        _row(60, 64, 49, 59, per_product=[_product(_URL_A, 74)]),
    ])
    assert series["panel_changes"] == []


def test_per_sku_series_explodes_run_history():
    series = build_tracking_series([
        _row(0, 60, 45, 55, per_product=[
            _product(_URL_A, 70, 50, 60, title="Serum"),
            _product(_URL_B, 50, 40, 50, title="Toner"),
        ]),
        _row(30, 68, 52, 60, per_product=[
            _product(_URL_A, 76, 54, 64, title="Serum"),
        ]),
    ])
    assert len(series["per_sku"]) == 2
    serum = next(s for s in series["per_sku"].values() if s["title"] == "Serum")
    toner = next(s for s in series["per_sku"].values() if s["title"] == "Toner")
    # SKU points only cover runs that measured the SKU
    assert [p["scores"]["visibility"] for p in serum["points"]] == [70, 76]
    assert [p["run_id"] for p in toner["points"]] == ["run-0"]
    # per-SKU scores are the SKU's own verdict, not the brand average
    assert serum["points"][1]["scores"] == {
        "visibility": 76, "attribution": 54, "category_visibility": 64,
    }
    assert serum["pdp_url"] == _URL_A


def test_per_sku_comparability_follows_the_basis_rule():
    series = build_tracking_series([
        _row(0, 60, 45, 55, basis_id="sel_old", per_product=[_product(_URL_A, 70)]),
        _row(30, 62, 47, 57, basis_id="sel_old", per_product=[_product(_URL_A, 72)]),
        _row(60, 40, 30, 35, basis_id="sel_new", per_product=[_product(_URL_A, 42)]),
    ])
    (sku,) = series["per_sku"].values()
    assert [p["comparable_with_prev"] for p in sku["points"]] == [False, True, False]
    assert sku["basis_changes"] == [2]
    assert [seg["indices"] for seg in sku["segments"]] == [[0, 1], [2]]


def test_per_sku_path_case_is_identity():
    # URL paths are case-sensitive: /Serum and /serum are DIFFERENT products.
    # Only scheme+host case (and a trailing slash) may be normalized away.
    series = build_tracking_series([
        _row(0, 60, 45, 55, per_product=[_product(_URL_A, 70)]),
        _row(30, 62, 47, 57, per_product=[
            _product("https://shop.example.com/products/SERUM", 72),
        ]),
    ])
    assert len(series["per_sku"]) == 2


def test_attempted_count_prefers_aggregate_products_count():
    # aggregate.products_count (what the run TRIED) outranks the failed[] fallback.
    row = _row(0, 60, 45, 55, per_product=[_product(_URL_A, 70)])
    row["report_jsonb"]["aggregate"] = {"products_count": 5}
    series = build_tracking_series([row])
    assert series["points"][0]["attempted_sku_count"] == 5


def test_per_sku_url_normalization_joins_series():
    # Trailing slash / host case must not split one product into two series.
    series = build_tracking_series([
        _row(0, 60, 45, 55, per_product=[_product(_URL_A, 70)]),
        _row(30, 62, 47, 57, per_product=[
            _product("https://SHOP.EXAMPLE.COM/products/serum/", 72),
        ]),
    ])
    assert len(series["per_sku"]) == 1
    (sku,) = series["per_sku"].values()
    assert len(sku["points"]) == 2


def test_per_sku_cap_keeps_most_covered_and_flags_truncation():
    urls = [f"https://shop.example.com/products/sku-{i}" for i in range(PER_SKU_SERIES_CAP + 5)]
    rows = [
        # run 0 measures every SKU once; run 1 re-measures only sku-0 → sku-0
        # has the most points and must survive the cap.
        _row(0, 60, 45, 55, per_product=[_product(u, 50) for u in urls]),
        _row(30, 62, 47, 57, per_product=[_product(urls[0], 52)]),
    ]
    series = build_tracking_series(rows)
    assert series["per_sku_truncated"] is True
    assert len(series["per_sku"]) == PER_SKU_SERIES_CAP
    assert any(len(s["points"]) == 2 for s in series["per_sku"].values())


# ---------------------------------------------------------------------------
# per_sku audit mode (the modern wedge / durable-worker shape)
# ---------------------------------------------------------------------------


def _sku_report(sku_key, title, dims, basis_id="sel_abc"):
    """A per_sku_reports entry in the modern (audit_mode='per_sku') shape."""
    return {
        "sku_key": sku_key,
        "sku_title": title,
        "scores": {d: {"score": s} for d, s in dims.items()},
        "prompt_basis": {"selected_set_id": basis_id} if basis_id else {},
    }


def _per_sku_row(day, vis, att, sku_reports, basis_id="sel_abc"):
    """A per_sku-mode run: report_jsonb carries per_sku_reports, NO per_product."""
    return {
        "run_id": f"run-{day}",
        "requested_at": _T0 + timedelta(days=day),
        "visibility": vis,
        "attribution": att,
        "category_visibility": None,
        "report_jsonb": {
            "audit_mode": "per_sku",
            "per_sku_reports": [
                ({**s, "prompt_basis": {"selected_set_id": basis_id} if basis_id else {}}
                 if isinstance(s, dict) else s)   # junk entries pass through as-is
                for s in sku_reports
            ],
            "brand_rollup": {"citation_by_provider": {"gemini": {"median": vis}}},
        },
    }


def test_per_sku_mode_runs_get_coverage_and_series():
    # per_sku_reports, no per_product. Scores must mirror _per_sku_run_aggregate:
    # visibility = weakest dimension, attribution = citation, category = None.
    series = build_tracking_series([
        _per_sku_row(0, 40, 40, [_sku_report(
            "urlwedge:aaa", "Hair Butter",
            {"citation": 40, "identity": 80, "content_richness": 65, "routability": 90},
        )]),
        _per_sku_row(30, 46, 46, [_sku_report(
            "urlwedge:aaa", "Hair Butter",
            {"citation": 46, "identity": 82, "content_richness": 70, "routability": 90},
        )]),
    ])
    p = series["points"][0]
    assert p["sku_count"] == 1
    assert p["attempted_sku_count"] is None      # per_sku runs record no failed[]
    assert p["panel_id"] is not None
    assert list(series["per_sku"].keys()) == ["urlwedge:aaa"]
    sku = series["per_sku"]["urlwedge:aaa"]
    assert sku["title"] == "Hair Butter"
    assert sku["points"][0]["scores"] == {
        "visibility": 40,                        # min(40, 80, 65, 90) — weakest dim
        "attribution": 40,                       # the citation dimension
        "category_visibility": None,             # no such dimension in per_sku
    }
    assert [pt["comparable_with_prev"] for pt in sku["points"]] == [False, True]
    assert series["panel_changes"] == []


def test_per_sku_mode_never_merges_with_legacy_series():
    # Mode purity: the same product measured by a legacy run (per_product,
    # PDP-URL-keyed) and a per_sku run (sku_key-keyed) must stay TWO series —
    # the two modes write different score semantics into the same fields.
    series = build_tracking_series([
        _row(0, 60, 45, 55, per_product=[_product(_URL_A, 70)]),
        _per_sku_row(30, 46, 46, [_sku_report(
            "urlwedge:aaa", "Serum", {"citation": 46, "identity": 82},
        )]),
    ])
    assert len(series["per_sku"]) == 2


def test_per_sku_mode_tolerates_junk_entries():
    series = build_tracking_series([
        _per_sku_row(0, 40, 40, [
            _sku_report("urlwedge:aaa", "Hair Butter", {"citation": 40}),
            "junk",                                   # not a dict
            {"sku_title": "no key"},                  # no sku_key/content_key
            {"sku_key": "  "},                        # blank key
        ]),
    ])
    assert series["points"][0]["sku_count"] == 1
    assert list(series["per_sku"].keys()) == ["urlwedge:aaa"]


def test_per_sku_mode_junk_scores_never_500():
    # The write path swallows report errors, so a poisoned stored score must
    # degrade to None on read — one bad historical row can't brick the endpoint.
    series = build_tracking_series([
        _per_sku_row(0, 40, 40, [
            _sku_report("urlwedge:aaa", "Hair Butter", {"citation": "n/a"}),
        ]),
        _per_sku_row(30, 46, 46, [
            _sku_report("urlwedge:bbb", "Oil", {"citation": True}),  # bool ≠ score
        ]),
    ])
    a = series["per_sku"]["urlwedge:aaa"]["points"][0]["scores"]
    b = series["per_sku"]["urlwedge:bbb"]["points"][0]["scores"]
    assert a == {"visibility": None, "attribution": None, "category_visibility": None}
    assert b["attribution"] is None


def test_brand_points_unchanged_by_sku_axis():
    # The additive fields must not disturb the existing contract.
    series = build_tracking_series([
        _row(0, 60, 45, 55, per_product=[_product(_URL_A, 70)]),
        _row(30, 68, 52, 60, per_product=[_product(_URL_A, 76)]),
    ])
    pts = series["points"]
    assert pts[1]["scores"] == {"visibility": 68, "attribution": 52, "category_visibility": 60}
    assert pts[1]["provider_scores"] == {"gemini": 68}
    assert [p["comparable_with_prev"] for p in pts] == [False, True]
