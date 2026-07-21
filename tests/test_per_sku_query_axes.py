"""Step 1 of the query-axis expansion: the per-SKU audit generator emits diverse
query SHAPES (need/problem-first, constraint, trust) beyond the old superlative
head terms, never the synthetic "shopper question N" junk, and under-fills with
real queries on thin SKUs rather than padding.

ADDITIVE approach: query shapes diversify, but the `axis` tag keeps the COARSE
vocabulary every downstream consumer already handles (category / attribute /
intent / review) — the fine intent taxonomy + per-axis scoring is Step 2. These
tests therefore assert on query SHAPES, not on new axis-tag strings.

See PIVOTA-Agent/docs/ai_readiness_query_axes_build_plan.md.
"""

from services import agent_center_bd_report_service as m

# "sidewalk" joined the coarse vocabulary with the per-SKU AI-visibility
# lanes (#742) + per-intent-axis scoring (#929): every downstream consumer
# maps it (_intent_axis_for → "constraint"). Anything OUTSIDE this set is
# still a leak.
_COARSE_AXES = {"category", "attribute", "intent", "review", "identity", "sidewalk"}


def _collagen_sku_ctx():
    product = {
        "title": "Good Night Collagen",
        "brand": "BB Lab",
        "product_type": "collagen",
        "attributes_raw": {
            "ingredients": "low-molecular marine collagen, magnesium",
            "benefits": "supports sleep and skin elasticity",
            "audience": "women over 40",
            "certifications": "halal",
            "free_from": "sugar",
            "usage": "take before sleep",
        },
    }
    return {
        "product": product,
        "sku": {"title": "2 Box"},
        "sku_title": "2 Box",
        "sku_key": "gnc-2box",
    }


def test_no_synthetic_junk_padding():
    recs = m._build_per_sku_audit_query_records(_collagen_sku_ctx(), 40)
    assert recs
    assert all("shopper question" not in r["query"] for r in recs), (
        "synthetic 'shopper question N' junk padding must be gone"
    )


def test_only_coarse_axis_tags_used():
    # The additive approach must NOT leak fine-grained tag strings into `axis`
    # (those would break the untouched downstream consumers).
    recs = m._build_per_sku_audit_query_records(_collagen_sku_ctx(), 40)
    leaked = {r["axis"] for r in recs} - _COARSE_AXES
    assert not leaked, f"unexpected (non-coarse) axis tags leaked: {leaked}"


def test_diverse_query_shapes_present():
    recs = m._build_per_sku_audit_query_records(_collagen_sku_ctx(), 40)
    queries = " || ".join(r["query"] for r in recs)
    # demoted category_head still present
    assert "best collagen" in queries
    assert "what collagen should I buy" in queries
    # problem/need-first (from the use_case graph class) — the dominant pattern
    assert "what helps with" in queries or " for " in queries
    # trust / validation
    assert "reviews" in queries or "legit" in queries or "actually work" in queries


def test_trust_shapes_tagged_review_axis():
    recs = m._build_per_sku_audit_query_records(_collagen_sku_ctx(), 40)
    # unambiguous brand-trust queries (avoid "reviews", which the category filler
    # pool also uses as "{cat} reviews") must carry the coarse "review" axis.
    trust_like = [r for r in recs if any(t in r["query"] for t in ("legit", "actually work"))]
    assert trust_like, "expected trust/validation queries"
    assert all(r["axis"] == "review" for r in trust_like)


def test_thin_sku_underfills_with_real_queries_not_junk():
    recs = m._build_per_sku_audit_query_records(_collagen_sku_ctx(), 40)
    assert len(recs) <= 40
    assert all("shopper question" not in r["query"] for r in recs)
    qs = [r["query"] for r in recs]
    assert len(qs) == len(set(qs)), "queries must be de-duplicated"


def test_small_target_still_works():
    recs = m._build_per_sku_audit_query_records(_collagen_sku_ctx(), 14)
    assert 1 <= len(recs) <= 14
    assert all("shopper question" not in r["query"] for r in recs)


def test_budget_inversion_specific_is_majority():
    """Win-the-specific-long-tail: the budgeter gives the SPECIFIC stacked sidewalk
    lane the majority of the budget, behind only a thin 2-nav + 2-head + 2-trust
    diagnostic spine. Credit-neutral (total == target). This is the real guard for
    the inversion (un-inverted budgeter would cap sidewalk at _sidewalk_budget≈6)."""
    from collections import Counter
    base = m._query_tuple_records([
        ("where can i buy X", "intent"), ("shop X online", "intent"),
        ("is BB legit", "review"), ("X reviews", "review"), ("does X work", "review"),
        ("best collagen", "category"), ("what collagen should i buy", "category"),
        ("best collagen for sleep", "category"), ("collagen for women", "category"),
        ("vegan collagen", "attribute"), ("marine collagen", "attribute"),
        ("X 2 box", "identity"),
    ])
    sidewalk = m._query_tuple_records([(f"vegan low-mol collagen stack {i}", "sidewalk") for i in range(34)])
    out = m._budgeted_wedge_query_records(base_records=base, sidewalk_records=sidewalk, target=40, title="X")
    axes = Counter(r["axis"] for r in out)
    assert len(out) == 40
    assert axes["sidewalk"] >= 20
    assert axes["sidewalk"] > (len(out) - axes["sidewalk"])
    assert axes["intent"] == 2
    assert axes.get("category", 0) <= 4
