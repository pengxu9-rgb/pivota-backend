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
