"""Q-P1-3: cross-product competitor rollup regression tests.

Pre-fix the rollup function walked only `attribution.competitor_hosts`,
which is the buyer-intent probe. Category-probe peer brands
(`category_visibility.retailer_hosts`) were dropped entirely. The
Winona prod artifact's "verywellfit.com" and "shape.com" peers
never surfaced in the brand-level pitch because of this.

After the fix the rollup walks BOTH probes and tags each host with
a confidence tier:

  - verified_competitor: appears in both probes
  - grounded_competitor: buyer-intent only
  - possible_peer_host: category-probe only

`times_cited` sums across both probes for ranking. Each entry also
carries `buyer_intent_cited` + `category_cited` for downstream
introspection, and `source` ∈ {buyer_intent, category_only, both}.
"""

from __future__ import annotations

from typing import Any, Dict, List

from services.agent_center_bd_report_service import _aggregate_brand_competitors


def _product(
    *,
    competitor_hosts: List[Dict[str, Any]] | None = None,
    retailer_hosts: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "attribution": {"competitor_hosts": competitor_hosts or []},
        "category_visibility": {"retailer_hosts": retailer_hosts or []},
    }


# =========================================================================
# Confidence tiers
# =========================================================================


def test_verified_competitor_when_host_in_both_probes():
    """Same host appears in both attribution.competitor_hosts AND
    category_visibility.retailer_hosts → verified_competitor."""
    products = [
        _product(
            competitor_hosts=[{"host": "sephora.com", "times_cited": 3}],
            retailer_hosts=[{"host": "sephora.com", "times_cited": 4}],
        ),
    ]
    out = _aggregate_brand_competitors(products)
    assert len(out) == 1
    entry = out[0]
    assert entry["host"] == "sephora.com"
    assert entry["confidence"] == "verified_competitor"
    assert entry["source"] == "both"
    assert entry["buyer_intent_cited"] == 3
    assert entry["category_cited"] == 4
    assert entry["times_cited"] == 7


def test_grounded_competitor_when_host_only_in_buyer_intent():
    """attribution.competitor_hosts only → grounded_competitor."""
    products = [
        _product(competitor_hosts=[{"host": "ulta.com", "times_cited": 5}]),
    ]
    out = _aggregate_brand_competitors(products)
    assert len(out) == 1
    entry = out[0]
    assert entry["host"] == "ulta.com"
    assert entry["confidence"] == "grounded_competitor"
    assert entry["source"] == "buyer_intent"
    assert entry["buyer_intent_cited"] == 5
    assert entry["category_cited"] == 0


def test_possible_peer_host_when_host_only_in_category_probe():
    """category_visibility.retailer_hosts only → possible_peer_host.
    The Winona regression case — verywellfit.com / shape.com peers
    were dropped by the old logic."""
    products = [
        _product(retailer_hosts=[{"host": "verywellfit.com", "times_cited": 2}]),
    ]
    out = _aggregate_brand_competitors(products)
    assert len(out) == 1
    entry = out[0]
    assert entry["host"] == "verywellfit.com"
    assert entry["confidence"] == "possible_peer_host"
    assert entry["source"] == "category_only"
    assert entry["buyer_intent_cited"] == 0
    assert entry["category_cited"] == 2


# =========================================================================
# Aggregation across products
# =========================================================================


def test_sums_buyer_intent_across_products():
    products = [
        _product(competitor_hosts=[{"host": "amazon.com", "times_cited": 2}]),
        _product(competitor_hosts=[{"host": "amazon.com", "times_cited": 3}]),
    ]
    out = _aggregate_brand_competitors(products)
    assert out[0]["host"] == "amazon.com"
    assert out[0]["buyer_intent_cited"] == 5


def test_sums_category_across_products():
    products = [
        _product(retailer_hosts=[{"host": "shape.com", "times_cited": 1}]),
        _product(retailer_hosts=[{"host": "shape.com", "times_cited": 2}]),
    ]
    out = _aggregate_brand_competitors(products)
    assert out[0]["host"] == "shape.com"
    assert out[0]["category_cited"] == 3


def test_combines_probes_across_products():
    """Host shows up in buyer-intent on product 1 and in category on
    product 2. Should still register as verified_competitor when
    rolled up at the brand level."""
    products = [
        _product(competitor_hosts=[{"host": "nordstrom.com", "times_cited": 4}]),
        _product(retailer_hosts=[{"host": "nordstrom.com", "times_cited": 6}]),
    ]
    out = _aggregate_brand_competitors(products)
    assert out[0]["host"] == "nordstrom.com"
    assert out[0]["confidence"] == "verified_competitor"
    assert out[0]["buyer_intent_cited"] == 4
    assert out[0]["category_cited"] == 6


# =========================================================================
# Ranking + capping
# =========================================================================


def test_ranking_by_combined_times_cited_desc():
    products = [
        _product(
            competitor_hosts=[
                {"host": "low.com", "times_cited": 1},
                {"host": "high.com", "times_cited": 10},
                {"host": "mid.com", "times_cited": 5},
            ],
        ),
    ]
    out = _aggregate_brand_competitors(products)
    assert [e["host"] for e in out] == ["high.com", "mid.com", "low.com"]


def test_tie_break_alphabetical():
    """Equal times_cited → tiebreak by host alphabetically for
    deterministic ordering."""
    products = [
        _product(
            competitor_hosts=[
                {"host": "b.com", "times_cited": 3},
                {"host": "a.com", "times_cited": 3},
            ],
        ),
    ]
    out = _aggregate_brand_competitors(products)
    assert [e["host"] for e in out] == ["a.com", "b.com"]


def test_caps_at_15_entries():
    products = [
        _product(
            competitor_hosts=[
                {"host": f"h{i}.com", "times_cited": 30 - i}
                for i in range(20)
            ],
        ),
    ]
    out = _aggregate_brand_competitors(products)
    assert len(out) == 15
    # Top entries (h0 = 30 cites … h14 = 16 cites) survive the cap.
    assert out[0]["host"] == "h0.com"
    assert out[-1]["host"] == "h14.com"


# =========================================================================
# Edge cases
# =========================================================================


def test_empty_products_returns_empty():
    assert _aggregate_brand_competitors([]) == []


def test_skips_entries_with_missing_host():
    products = [
        _product(
            competitor_hosts=[
                {"host": "", "times_cited": 5},
                {"times_cited": 3},
                {"host": "ok.com", "times_cited": 2},
            ],
        ),
    ]
    out = _aggregate_brand_competitors(products)
    assert len(out) == 1
    assert out[0]["host"] == "ok.com"


def test_skips_entries_with_zero_count():
    products = [
        _product(competitor_hosts=[
            {"host": "zero.com", "times_cited": 0},
            {"host": "real.com", "times_cited": 1},
        ]),
    ]
    out = _aggregate_brand_competitors(products)
    assert [e["host"] for e in out] == ["real.com"]


def test_hosts_normalized_to_lowercase():
    """Case-mismatched host strings collapse to one bucket so
    AMAZON.COM and amazon.com don't show up as two separate rows."""
    products = [
        _product(competitor_hosts=[{"host": "AMAZON.com", "times_cited": 3}]),
        _product(retailer_hosts=[{"host": "amazon.COM", "times_cited": 2}]),
    ]
    out = _aggregate_brand_competitors(products)
    assert len(out) == 1
    assert out[0]["host"] == "amazon.com"
    assert out[0]["times_cited"] == 5


def test_back_compat_fields_present():
    """Output entries still carry `host` and `times_cited` so
    existing consumers (markdown renderer, downstream evidence
    builder) keep working without a coordinated migration."""
    products = [
        _product(competitor_hosts=[{"host": "x.com", "times_cited": 1}]),
    ]
    out = _aggregate_brand_competitors(products)
    assert "host" in out[0]
    assert "times_cited" in out[0]


# =========================================================================
# Realistic Winona-shape regression
# =========================================================================


def test_winona_shape_surfaces_category_peers():
    """The Winona prod artifact's per-product reports had:
      - attribution.competitor_hosts: [] (zero buyer-intent capture)
      - category_visibility.retailer_hosts: verywellfit.com, shape.com

    Pre-fix the brand rollup returned []. Post-fix the rollup surfaces
    both peers tagged as possible_peer_host so the merchant report can
    say "peer category hosts include..." instead of pretending nobody
    was captured."""
    products = [
        _product(
            competitor_hosts=[],
            retailer_hosts=[
                {"host": "verywellfit.com", "times_cited": 3},
                {"host": "shape.com", "times_cited": 2},
            ],
        ),
        _product(
            competitor_hosts=[],
            retailer_hosts=[
                {"host": "verywellfit.com", "times_cited": 1},
            ],
        ),
    ]
    out = _aggregate_brand_competitors(products)
    assert len(out) == 2
    by_host = {e["host"]: e for e in out}
    assert by_host["verywellfit.com"]["confidence"] == "possible_peer_host"
    assert by_host["verywellfit.com"]["times_cited"] == 4
    assert by_host["shape.com"]["confidence"] == "possible_peer_host"
    assert by_host["shape.com"]["times_cited"] == 2


# =========================================================================
# N5 PR-7: exclude merchant's own brand-derived hosts
# =========================================================================


def test_excludes_merchant_brand_host_from_rollup():
    """The Beauty of Joseon external_seed case: the audit probed
    `agent.pivota.cc/products/sig_...` so `merchant_host` was
    `agent.pivota.cc`. The category probe found `beautyofjoseon.com`
    (the brand's real D2C site) and surfaced it as a "competitor".
    Post-PR-7 the brand-derived host gets excluded."""
    products = [
        _product(
            retailer_hosts=[
                {"host": "beautyofjoseon.com", "times_cited": 1},
                {"host": "ulta.com", "times_cited": 2},
            ],
        ),
    ]
    out = _aggregate_brand_competitors(products, merchant_brand="Beauty of Joseon")
    hosts = [e["host"] for e in out]
    assert "beautyofjoseon.com" not in hosts, (
        "Merchant's own brand-derived host must be filtered out"
    )
    assert "ulta.com" in hosts


def test_excludes_explicit_merchant_domain_from_rollup():
    """When the brand report request supplies an explicit
    `merchant_domain`, that host is excluded regardless of what
    `merchant_brand` derives to. Stronger signal than the
    brand-name heuristic."""
    products = [
        _product(
            competitor_hosts=[
                {"host": "winona.com", "times_cited": 5},
                {"host": "sephora.com", "times_cited": 3},
            ],
        ),
    ]
    out = _aggregate_brand_competitors(
        products,
        merchant_brand="Winona",
        merchant_domain="winona.com",
    )
    hosts = [e["host"] for e in out]
    assert "winona.com" not in hosts
    assert "sephora.com" in hosts


def test_excludes_www_prefixed_merchant_host():
    """The brand probe may surface `www.beautyofjoseon.com` instead
    of `beautyofjoseon.com`. Both forms must be excluded — the
    rollup normalizes by stripping the www. prefix."""
    products = [
        _product(
            retailer_hosts=[
                {"host": "www.beautyofjoseon.com", "times_cited": 2},
                {"host": "ulta.com", "times_cited": 1},
            ],
        ),
    ]
    out = _aggregate_brand_competitors(products, merchant_brand="Beauty of Joseon")
    hosts = [e["host"] for e in out]
    assert "www.beautyofjoseon.com" not in hosts
    assert "ulta.com" in hosts


def test_brand_with_punctuation_normalizes_correctly():
    """Brand names with apostrophes / spaces / hyphens normalize
    to their concatenated alnum form. "L'Oreal" → "loreal.com"."""
    products = [
        _product(retailer_hosts=[
            {"host": "loreal.com", "times_cited": 3},
            {"host": "ulta.com", "times_cited": 1},
        ]),
    ]
    out = _aggregate_brand_competitors(products, merchant_brand="L'Oreal")
    hosts = [e["host"] for e in out]
    assert "loreal.com" not in hosts
    assert "ulta.com" in hosts


def test_no_brand_or_domain_no_filtering():
    """Back-compat — calling without merchant_brand/merchant_domain
    preserves the old behavior (no filtering). Internal merchants
    that already work correctly aren't affected by PR-7."""
    products = [
        _product(competitor_hosts=[{"host": "anyhost.com", "times_cited": 1}]),
    ]
    out = _aggregate_brand_competitors(products)
    assert [e["host"] for e in out] == ["anyhost.com"]


def test_empty_brand_string_no_filtering():
    """Empty / whitespace-only merchant_brand is treated as None —
    no candidates derived, no filtering applied."""
    products = [
        _product(competitor_hosts=[{"host": "winona.com", "times_cited": 1}]),
    ]
    out = _aggregate_brand_competitors(products, merchant_brand="   ")
    assert [e["host"] for e in out] == ["winona.com"]


def test_brand_with_no_alnum_chars_no_filtering():
    """Pathological case: brand is all punctuation. Yields no
    candidate hosts, no filtering applied (don't accidentally
    filter every host by matching an empty string)."""
    products = [
        _product(competitor_hosts=[{"host": "real.com", "times_cited": 1}]),
    ]
    out = _aggregate_brand_competitors(products, merchant_brand="!!!")
    assert [e["host"] for e in out] == ["real.com"]
