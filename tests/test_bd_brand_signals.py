"""Tests for services/bd_brand_signals.py — PR-B of the BD brand-deep
analysis redesign. These cover the pure-parsing sub-extractors with
inline HTML/JSON-LD/sitemap fixtures.

Live integration with sitemap.xml + robots.txt fetches is exercised
end-to-end in the cold-start audit smoke test (not here)."""

from __future__ import annotations

from services.bd_brand_signals import (
    _classify_sitemap_urls,
    _extract_aggregate_rating,
    _extract_open_graph,
    _extract_robots_directives,
    _extract_schema_org_organization,
    _extract_social_handles,
    _flatten_jsonld,
    _parse_jsonld_blocks,
    _parse_sitemap_xml,
    _score_seo_completeness,
)


# ---------------------------------------------------------------------------
# Open Graph extraction
# ---------------------------------------------------------------------------


def test_open_graph_basic_extraction():
    html = """
    <html><head>
      <meta property="og:title" content="Grüns Daily">
      <meta property="og:description" content="Greens for the masses">
      <meta property="og:image" content="https://gruns.co/cover.jpg">
      <meta property="og:type" content="website">
      <meta property="og:site_name" content="Grüns">
      <meta name="twitter:card" content="summary_large_image">
      <meta name="description" content="Plant-based daily nutrition">
    </head></html>
    """
    og = _extract_open_graph(html)
    assert og["og_title"] == "Grüns Daily"
    assert og["og_description"] == "Greens for the masses"
    assert og["og_image"] == "https://gruns.co/cover.jpg"
    assert og["og_type"] == "website"
    assert og["og_site_name"] == "Grüns"
    assert og["twitter_card"] == "summary_large_image"
    assert og["meta_description"] == "Plant-based daily nutrition"


def test_open_graph_handles_attribute_order_reversed():
    """Some sites emit content="..." before property="..." — both orderings
    must work."""
    html = '<meta content="Reverse Order" property="og:title">'
    og = _extract_open_graph(html)
    assert og["og_title"] == "Reverse Order"


def test_open_graph_returns_none_for_missing_fields():
    og = _extract_open_graph("<html></html>")
    assert og["og_title"] is None
    assert og["og_image"] is None
    assert og["twitter_card"] is None


def test_open_graph_handles_empty_html():
    assert _extract_open_graph("") == {}
    assert _extract_open_graph(None) == {}


# ---------------------------------------------------------------------------
# JSON-LD parsing
# ---------------------------------------------------------------------------


def test_jsonld_blocks_extracted_and_flattened():
    html = """
    <script type="application/ld+json">
      {"@type": "Organization", "name": "Acme"}
    </script>
    <script type="application/ld+json">
      [{"@type": "Product", "name": "Widget"}]
    </script>
    <script type="application/ld+json">
      {"@graph": [{"@type": "WebSite"}, {"@type": "Person", "name": "Founder"}]}
    </script>
    """
    blocks = _parse_jsonld_blocks(html)
    assert len(blocks) == 3
    items = _flatten_jsonld(blocks)
    types = [
        i.get("@type") if isinstance(i.get("@type"), str) else i.get("@type")
        for i in items
    ]
    # Organization, Product, the @graph wrapper, WebSite, Person
    assert "Organization" in types
    assert "Product" in types
    assert "WebSite" in types
    assert "Person" in types


def test_jsonld_skips_malformed_blocks():
    html = """
    <script type="application/ld+json">{not valid json</script>
    <script type="application/ld+json">{"@type": "Organization", "name": "OK"}</script>
    """
    blocks = _parse_jsonld_blocks(html)
    assert len(blocks) == 1
    assert blocks[0]["name"] == "OK"


def test_jsonld_extracts_object_from_partial_block():
    """When a JSON-LD block has prose around the object (some Shopify
    apps inject this), extract the first balanced {...}."""
    html = """
    <script type="application/ld+json">
      // Shopify analytics injection
      {"@type": "Organization", "name": "Recovered"}
      window.foo = 1;
    </script>
    """
    blocks = _parse_jsonld_blocks(html)
    assert len(blocks) == 1
    assert blocks[0]["name"] == "Recovered"


# ---------------------------------------------------------------------------
# Schema.org Organization
# ---------------------------------------------------------------------------


def test_schema_org_organization_extraction_full_shape():
    items = [
        {
            "@type": "Organization",
            "name": "Acme Beauty",
            "url": "https://acme.co/",
            "logo": "https://acme.co/logo.png",
            "description": "Clean beauty.",
            "sameAs": [
                "https://instagram.com/acmebeauty",
                "https://twitter.com/acmebeauty",
            ],
            "founder": {"@type": "Person", "name": "Jane Doe"},
            "foundingDate": "2018-03-01",
            "address": {
                "streetAddress": "1 Main St",
                "addressLocality": "Brooklyn",
                "addressRegion": "NY",
                "postalCode": "11211",
                "addressCountry": "US",
            },
            "telephone": "+1-555-1212",
        }
    ]
    org = _extract_schema_org_organization(items)
    assert org["name"] == "Acme Beauty"
    assert org["logo"] == "https://acme.co/logo.png"
    assert org["founders"] == ["Jane Doe"]
    assert org["founding_date"] == "2018-03-01"
    assert "Brooklyn" in org["address"]
    assert org["telephone"] == "+1-555-1212"
    assert "https://instagram.com/acmebeauty" in org["same_as"]


def test_schema_org_organization_handles_logo_as_imageobject():
    items = [{
        "@type": "Organization",
        "name": "X",
        "logo": {"@type": "ImageObject", "url": "https://x.co/l.png"},
    }]
    org = _extract_schema_org_organization(items)
    assert org["logo"] == "https://x.co/l.png"


def test_schema_org_organization_handles_founder_as_list_of_strings():
    items = [{"@type": "Organization", "name": "X", "founder": ["Alice", "Bob"]}]
    org = _extract_schema_org_organization(items)
    assert org["founders"] == ["Alice", "Bob"]


def test_schema_org_organization_matches_brand_or_localbusiness():
    """@type can be Organization, Brand, LocalBusiness, etc."""
    items = [{"@type": "Brand", "name": "BrandOnly"}]
    org = _extract_schema_org_organization(items)
    assert org is not None
    assert org["name"] == "BrandOnly"


def test_schema_org_organization_returns_none_when_absent():
    items = [{"@type": "Product", "name": "Widget"}]
    assert _extract_schema_org_organization(items) is None


def test_schema_org_organization_handles_type_as_list():
    """@type can be a list of strings — match if any matches."""
    items = [{"@type": ["Organization", "Brand"], "name": "Multi"}]
    org = _extract_schema_org_organization(items)
    assert org is not None and org["name"] == "Multi"


# ---------------------------------------------------------------------------
# AggregateRating
# ---------------------------------------------------------------------------


def test_aggregate_rating_top_level():
    items = [{
        "@type": "AggregateRating",
        "ratingValue": "4.7",
        "ratingCount": "1234",
        "reviewCount": "999",
        "bestRating": 5,
        "worstRating": 1,
    }]
    r = _extract_aggregate_rating(items)
    assert r["rating_value"] == 4.7
    assert r["rating_count"] == 1234
    assert r["review_count"] == 999
    assert r["best_rating"] == 5.0


def test_aggregate_rating_nested_inside_organization():
    items = [{
        "@type": "Organization",
        "name": "X",
        "aggregateRating": {
            "@type": "AggregateRating",
            "ratingValue": 4.2,
            "ratingCount": 88,
        },
    }]
    r = _extract_aggregate_rating(items)
    assert r["rating_value"] == 4.2
    assert r["rating_count"] == 88


def test_aggregate_rating_returns_none_when_absent():
    items = [{"@type": "Organization", "name": "X"}]
    assert _extract_aggregate_rating(items) is None


# ---------------------------------------------------------------------------
# Social handles
# ---------------------------------------------------------------------------


def test_social_handles_finds_all_platforms():
    html = """
    <a href="https://www.instagram.com/acmebeauty/">IG</a>
    <a href="https://www.tiktok.com/@acmebeauty">TT</a>
    <a href="https://www.youtube.com/@acmebeauty">YT</a>
    <a href="https://twitter.com/acmebeauty">X</a>
    <a href="https://x.com/acmebeauty">X2</a>
    <a href="https://www.linkedin.com/company/acme-beauty/">LI</a>
    <a href="https://www.facebook.com/acmebeauty">FB</a>
    <a href="https://www.pinterest.com/acmebeauty">P</a>
    """
    handles = _extract_social_handles(html)
    platforms = {h["platform"] for h in handles}
    assert "instagram" in platforms
    assert "tiktok" in platforms
    assert "youtube" in platforms
    assert "twitter" in platforms
    assert "linkedin" in platforms
    assert "facebook" in platforms
    assert "pinterest" in platforms


def test_social_handles_dedupes_across_header_and_footer():
    """Brand often links its IG in both header and footer — surface
    one entry per (platform, handle)."""
    html = """
    <header><a href="https://instagram.com/acme">IG header</a></header>
    <footer><a href="https://www.instagram.com/acme/">IG footer</a></footer>
    """
    handles = _extract_social_handles(html)
    assert len(handles) == 1
    assert handles[0]["platform"] == "instagram"
    assert handles[0]["handle"] == "acme"


def test_social_handles_rejects_blacklisted_paths():
    """Generic Instagram paths like /explore, /p/<id> are not brand
    profiles — must not surface as handles."""
    html = """
    <a href="https://instagram.com/explore">explore page</a>
    <a href="https://instagram.com/p/abc123">post</a>
    <a href="https://www.tiktok.com/@discover">tiktok discover</a>
    """
    handles = _extract_social_handles(html)
    assert handles == []


def test_social_handles_canonical_url_format():
    """TikTok URLs include the @ sign; Instagram doesn't."""
    html = """
    <a href="instagram.com/foo">IG</a>
    <a href="tiktok.com/@bar">TT</a>
    """
    handles = _extract_social_handles(html)
    by_platform = {h["platform"]: h for h in handles}
    assert by_platform["instagram"]["url"] == "https://www.instagram.com/foo"
    assert by_platform["tiktok"]["url"] == "https://www.tiktok.com/@bar"


def test_social_handles_returns_empty_for_no_links():
    assert _extract_social_handles("<html><body>no socials</body></html>") == []


# ---------------------------------------------------------------------------
# Sitemap XML parsing + classification
# ---------------------------------------------------------------------------


def test_sitemap_xml_parsing():
    xml = b"""<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://acme.co/products/p1</loc></url>
      <url><loc>https://acme.co/blog/post1</loc></url>
      <url><loc>https://acme.co/about</loc></url>
    </urlset>
    """
    urls = _parse_sitemap_xml(xml)
    assert len(urls) == 3
    assert "https://acme.co/products/p1" in urls


def test_sitemap_xml_falls_back_to_regex_on_malformed():
    # Missing closing tag — ElementTree raises; regex fallback recovers.
    xml = b"""<urlset>
      <url><loc>https://acme.co/p/1</loc></url>
      <url><loc>https://acme.co/p/2</loc>
    """
    urls = _parse_sitemap_xml(xml)
    assert "https://acme.co/p/1" in urls


def test_classify_sitemap_urls_buckets_correctly():
    urls = [
        "https://acme.co/products/serum",
        "https://acme.co/products/cleanser",
        "https://acme.co/collections/all",
        "https://acme.co/blogs/news/post-1",
        "https://acme.co/press/coverage",
        "https://acme.co/about",
        "https://acme.co/contact",
        "https://acme.co/policies/privacy",
        "https://acme.co/random/page",
    ]
    s = _classify_sitemap_urls(urls)
    assert s["total_urls"] == 9
    assert s["products"]["count"] == 2
    assert s["collections"]["count"] == 1
    assert s["blog"]["count"] == 1
    assert s["press"]["count"] == 1
    assert s["about"]["count"] == 1
    assert s["contact"]["count"] == 1
    assert s["policies"]["count"] == 1
    assert s["other"]["count"] == 1


# ---------------------------------------------------------------------------
# Robots.txt directives
# ---------------------------------------------------------------------------


def test_robots_directives_normal_site():
    txt = """
    User-agent: *
    Disallow: /admin
    Disallow: /checkout
    Sitemap: https://acme.co/sitemap.xml

    User-agent: GPTBot
    Disallow: /
    """
    r = _extract_robots_directives(txt)
    assert r["present"] is True
    assert r["user_agent_groups"] == 2
    assert r["disallow_count"] == 3
    assert r["sitemaps_declared"] == ["https://acme.co/sitemap.xml"]
    assert r["blocks_all_crawlers"] is False


def test_robots_directives_blocks_all_crawlers():
    txt = "User-agent: *\nDisallow: /"
    r = _extract_robots_directives(txt)
    assert r["blocks_all_crawlers"] is True


def test_robots_directives_returns_absent_shape_for_none():
    r = _extract_robots_directives(None)
    assert r["present"] is False
    assert r["sitemaps_declared"] == []


# ---------------------------------------------------------------------------
# SEO completeness scoring
# ---------------------------------------------------------------------------


def test_seo_score_full_signals():
    og = {
        "og_title": "X", "og_description": "Y", "og_image": "Z",
        "twitter_card": "summary", "meta_description": "D",
    }
    robots = {"present": True, "sitemaps_declared": ["s"], "blocks_all_crawlers": False}
    s = _score_seo_completeness(og, jsonld_count=2, robots=robots, sitemap_url_count=10)
    assert s["score"] == 1.0
    assert s["missing_signals"] == []


def test_seo_score_partial_signals_lists_missing():
    og = {"og_title": "X"}  # only one signal
    robots = {"present": False, "sitemaps_declared": [], "blocks_all_crawlers": False}
    s = _score_seo_completeness(og, jsonld_count=0, robots=robots, sitemap_url_count=0)
    assert s["score"] < 1.0
    assert "og_image_present" in s["missing_signals"]
    assert "json_ld_blocks_present" in s["missing_signals"]
    assert "sitemap_present" in s["missing_signals"]


def test_seo_score_blocks_all_crawlers_flagged():
    og = {}
    robots = {"present": True, "sitemaps_declared": [], "blocks_all_crawlers": True}
    s = _score_seo_completeness(og, jsonld_count=0, robots=robots, sitemap_url_count=0)
    assert "not_blocking_all_crawlers" in s["missing_signals"]


# ---------------------------------------------------------------------------
# PR-C: Gemini-backed brand context (retail / founder / press)
# ---------------------------------------------------------------------------

import pytest
from unittest.mock import AsyncMock, patch

from services.bd_brand_signals import (
    _build_founder_prompt,
    _build_press_prompt,
    _build_retail_prompt,
    _parse_gemini_text,
    _unwrap_json,
    infer_brand_context,
)


def test_unwrap_json_strips_fence():
    assert _unwrap_json("```json\n{\"a\": 1}\n```") == {"a": 1}
    assert _unwrap_json("```\n[1, 2]\n```") == [1, 2]


def test_unwrap_json_finds_first_balanced_object():
    s = 'Here is the JSON:\n{"founders": ["Alice"], "year": 2020}\nThanks!'
    assert _unwrap_json(s) == {"founders": ["Alice"], "year": 2020}


def test_unwrap_json_finds_first_balanced_array():
    s = 'Result:\n[{"r": "Sephora"}, {"r": "Ulta"}]\n— end'
    assert _unwrap_json(s) == [{"r": "Sephora"}, {"r": "Ulta"}]


def test_unwrap_json_returns_none_on_garbage():
    assert _unwrap_json("not json at all") is None
    assert _unwrap_json("") is None
    assert _unwrap_json(None) is None


def test_parse_gemini_text_extracts_text_parts():
    payload = {
        "candidates": [{
            "content": {"parts": [{"text": "first chunk"}, {"text": "second"}]},
        }],
    }
    assert _parse_gemini_text(payload) == "first chunk\nsecond"


def test_parse_gemini_text_handles_empty_response():
    assert _parse_gemini_text({"candidates": []}) is None
    assert _parse_gemini_text({}) is None


def test_retail_prompt_mentions_brand_and_domain():
    p = _build_retail_prompt("Grüns", "gruns.co")
    assert "Grüns" in p
    assert "gruns.co" in p
    assert "JSON array" in p


def test_founder_prompt_requests_origin_story():
    p = _build_founder_prompt("Acme", "acme.co")
    assert "Acme" in p
    assert "founders" in p
    assert "founding_year" in p
    assert "origin_story" in p


def test_press_prompt_requests_12_month_window():
    p = _build_press_prompt("Acme", "acme.co")
    assert "12 months" in p
    assert "publication" in p


@pytest.mark.asyncio
async def test_infer_brand_context_no_api_key_returns_unavailable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PIVOTA_GEMINI_API_KEY", raising=False)
    result = await infer_brand_context("Grüns", "gruns.co")
    assert result["available"] is False
    assert result["retail_presence"] is None
    assert result["founder_story"] is None
    assert result["press_coverage"] is None


@pytest.mark.asyncio
async def test_infer_brand_context_empty_brand_returns_unavailable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    result = await infer_brand_context("", "gruns.co")
    assert result["available"] is False


@pytest.mark.asyncio
async def test_infer_brand_context_partial_failure_surfaces_what_succeeded(monkeypatch):
    """If 1 of 3 Gemini calls returns null, the other 2 should still
    surface — graceful partial failure."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    async def fake_retail(*args, **kwargs):
        return [{"retailer": "Amazon", "url": "https://amazon.com/x", "confidence": "high"}]

    async def fake_founder(*args, **kwargs):
        return None  # this one fails

    async def fake_press(*args, **kwargs):
        return [{"publication": "NYMag", "headline": "X", "url": None, "date": None}]

    with patch("services.bd_brand_signals._infer_retail_presence", new=AsyncMock(side_effect=fake_retail)), \
         patch("services.bd_brand_signals._infer_founder_story", new=AsyncMock(side_effect=fake_founder)), \
         patch("services.bd_brand_signals._infer_press_coverage", new=AsyncMock(side_effect=fake_press)):
        result = await infer_brand_context("Grüns", "gruns.co")
    assert result["available"] is True
    assert result["retail_presence"] == [{"retailer": "Amazon", "url": "https://amazon.com/x", "confidence": "high"}]
    assert result["founder_story"] is None
    assert len(result["press_coverage"]) == 1


# ---------------------------------------------------------------------------
# PR-D: Social-media intelligence (TikTok + Instagram)
# ---------------------------------------------------------------------------

from services.bd_brand_signals import (
    _build_competitive_prompt,
    _build_kol_prompt,
    _build_own_presence_prompt,
    _coerce_int,
    _detected_handle,
    infer_social_intelligence,
)


def test_detected_handle_lookup():
    handles = [
        {"platform": "tiktok", "handle": "grunsdaily", "url": "x"},
        {"platform": "instagram", "handle": "grunsig", "url": "y"},
    ]
    assert _detected_handle(handles, "tiktok") == "grunsdaily"
    assert _detected_handle(handles, "instagram") == "grunsig"
    assert _detected_handle(handles, "youtube") is None
    assert _detected_handle([], "tiktok") is None
    assert _detected_handle(None, "tiktok") is None


def test_coerce_int_handles_thousands_suffix():
    assert _coerce_int(12000) == 12000
    assert _coerce_int(12000.5) == 12000
    assert _coerce_int("12000") == 12000
    assert _coerce_int("12,000") == 12000
    assert _coerce_int("12k") == 12000
    assert _coerce_int("1.5M") == 1500000
    assert _coerce_int("not a number") is None
    assert _coerce_int(None) is None


def test_own_presence_prompt_includes_handle_when_known():
    p = _build_own_presence_prompt("Grüns", "tiktok", "grunsdaily")
    assert "Grüns" in p
    assert "@grunsdaily" in p
    assert "TikTok" in p
    assert "view_per_post_estimate" in p


def test_own_presence_prompt_falls_back_to_search_when_no_handle():
    p = _build_own_presence_prompt("Grüns", "instagram", None)
    assert "@" not in p.split("Their Instagram handle")[0]  # no handle clause
    assert "Search for Grüns's official instagram account" in p
    assert "engagement_rate_estimate" in p


def test_kol_prompt_specifies_platform_and_band():
    p = _build_kol_prompt("Grüns", "tiktok")
    assert "TikTok" in p
    assert "10k-1M follower band" in p
    assert "12 months" in p


def test_competitive_prompt_lists_competitors():
    p = _build_competitive_prompt("Grüns", ["Hiya", "First Day", "Olly"])
    assert "Hiya, First Day, Olly" in p
    assert "Grüns" in p


@pytest.mark.asyncio
async def test_infer_social_intelligence_no_api_key_returns_unavailable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PIVOTA_GEMINI_API_KEY", raising=False)
    result = await infer_social_intelligence("Grüns", "gruns.co", [], None)
    assert result["available"] is False
    assert result["own_presence"]["tiktok"] is None
    assert result["competitive_comparison"] is None


@pytest.mark.asyncio
async def test_infer_social_intelligence_skips_competitive_when_no_competitors(monkeypatch):
    """When competitor_brands is None or empty, the 5th call must be
    skipped — only 4 grounded calls fire."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    own_call_count = 0
    kol_call_count = 0
    comp_call_count = 0

    async def fake_own(brand, platform, handle, api_key):
        nonlocal own_call_count
        own_call_count += 1
        return {"platform": platform, "handle": handle, "follower_estimate": 10000,
                "follower_band": "10k-100k", "content_focus": "test",
                "view_per_post_estimate": 5000, "engagement_rate_estimate": None,
                "post_frequency": None, "verified_account": None}

    async def fake_kol(brand, platform, api_key):
        nonlocal kol_call_count
        kol_call_count += 1
        return [{"creator_handle": "creator", "follower_band": "10k-100k",
                 "post_url": None, "view_count_estimate": None, "post_date": None,
                 "content_summary": "x"}]

    async def fake_comp(brand, competitors, api_key):
        nonlocal comp_call_count
        comp_call_count += 1
        return [{"brand": "X"}]

    with patch("services.bd_brand_signals._infer_own_presence", new=AsyncMock(side_effect=fake_own)), \
         patch("services.bd_brand_signals._infer_kol_endorsements", new=AsyncMock(side_effect=fake_kol)), \
         patch("services.bd_brand_signals._infer_competitive_social", new=AsyncMock(side_effect=fake_comp)), \
         patch("services.bd_brand_signals._fetch_homepage_html", new=AsyncMock(return_value=None)):
        result = await infer_social_intelligence("Grüns", "gruns.co", [], None)
    assert result["available"] is True
    assert own_call_count == 2  # tiktok + instagram
    assert kol_call_count == 2  # tiktok + instagram
    assert comp_call_count == 0  # competitive skipped (no competitors)
    assert result["competitive_comparison"] is None


@pytest.mark.asyncio
async def test_infer_social_intelligence_runs_competitive_when_competitors_provided(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    async def fake_own(*a, **k):
        return None

    async def fake_kol(*a, **k):
        return None

    async def fake_comp(brand, competitors, api_key):
        assert competitors == ["Hiya"]
        return [{"brand": "Hiya", "tiktok_followers_estimate": 50000,
                 "instagram_followers_estimate": 180000,
                 "kol_endorsements_count_estimate": 12,
                 "gap_summary": "Hiya leads"}]

    with patch("services.bd_brand_signals._infer_own_presence", new=AsyncMock(side_effect=fake_own)), \
         patch("services.bd_brand_signals._infer_kol_endorsements", new=AsyncMock(side_effect=fake_kol)), \
         patch("services.bd_brand_signals._infer_competitive_social", new=AsyncMock(side_effect=fake_comp)), \
         patch("services.bd_brand_signals._fetch_homepage_html", new=AsyncMock(return_value=None)):
        result = await infer_social_intelligence("Grüns", "gruns.co", [], ["Hiya"])
    assert result["available"] is True
    assert result["competitive_comparison"][0]["brand"] == "Hiya"


@pytest.mark.asyncio
async def test_infer_social_intelligence_partial_failure_surfaces_what_succeeded(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    async def fake_own(brand, platform, handle, api_key):
        # Only TikTok succeeds; Instagram returns None
        if platform == "tiktok":
            return {"platform": "tiktok", "handle": "x", "follower_estimate": 10000,
                    "follower_band": "10k-100k", "content_focus": "test",
                    "view_per_post_estimate": None, "engagement_rate_estimate": None,
                    "post_frequency": None, "verified_account": None}
        return None

    async def fake_kol(*a, **k):
        return None

    with patch("services.bd_brand_signals._infer_own_presence", new=AsyncMock(side_effect=fake_own)), \
         patch("services.bd_brand_signals._infer_kol_endorsements", new=AsyncMock(side_effect=fake_kol)), \
         patch("services.bd_brand_signals._fetch_homepage_html", new=AsyncMock(return_value=None)):
        result = await infer_social_intelligence("Grüns", "gruns.co", [], None)
    assert result["available"] is True
    assert result["own_presence"]["tiktok"] is not None
    assert result["own_presence"]["instagram"] is None


# ---------------------------------------------------------------------------
# PR-F: parser hardening (_coerce_to_array)
# ---------------------------------------------------------------------------

from services.bd_brand_signals import _coerce_to_array


def test_coerce_to_array_passes_through_lists():
    assert _coerce_to_array([{"a": 1}, {"a": 2}]) == [{"a": 1}, {"a": 2}]
    assert _coerce_to_array([]) == []


def test_coerce_to_array_unwraps_single_listvalued_field():
    """Gemini sometimes wraps the array in {"creators": [...]} or
    {"results": [...]} despite the prompt asking for a bare array."""
    assert _coerce_to_array({"creators": [{"creator_handle": "x"}]}) == [{"creator_handle": "x"}]
    assert _coerce_to_array({"results": [1, 2, 3]}) == [1, 2, 3]
    assert _coerce_to_array({"data": []}) == []


def test_coerce_to_array_wraps_single_item_object():
    """Gemini sometimes returns one item as a bare object when the
    prompt asked for an array — wrap it so the caller can iterate."""
    assert _coerce_to_array({"retailer": "Sephora", "confidence": "high"}) == [
        {"retailer": "Sephora", "confidence": "high"},
    ]
    assert _coerce_to_array({"creator_handle": "@x", "follower_band": "10k-100k"}) == [
        {"creator_handle": "@x", "follower_band": "10k-100k"},
    ]


def test_coerce_to_array_returns_none_for_ambiguous_object():
    """Object with multiple list fields is ambiguous — don't guess."""
    assert _coerce_to_array({"a": [1], "b": [2]}) is None


def test_coerce_to_array_returns_none_for_object_without_indicators():
    """Object with no list field and no recognized item-indicator field
    is meaningless garbage."""
    assert _coerce_to_array({"unrelated_key": "value"}) is None


def test_coerce_to_array_passes_through_none():
    assert _coerce_to_array(None) is None


def test_coerce_to_array_returns_none_for_scalar():
    assert _coerce_to_array("a string") is None
    assert _coerce_to_array(42) is None
