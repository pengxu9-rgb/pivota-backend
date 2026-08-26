from services.external_offers_service import _extract_from_html


def test_extract_from_html_collects_multiple_images_from_meta_and_jsonld() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://example.com/p/1" />
        <meta property="og:image" content="https://example.com/og_1.jpg" />
        <meta property="og:image" content="https://example.com/og_2.jpg" />
        <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Example",
            "image": [
              "https://example.com/j1.jpg",
              "https://example.com/j2.jpg"
            ],
            "offers": {
              "@type": "Offer",
              "price": "12.34",
              "priceCurrency": "USD",
              "availability": "https://schema.org/InStock"
            }
          }
        </script>
      </head>
      <body></body>
    </html>
    """

    out = _extract_from_html("https://example.com/p/1", html)
    image_urls = out.get("image_urls") or []
    assert out.get("image_url") == "https://example.com/j1.jpg"
    assert "https://example.com/j1.jpg" in image_urls
    assert "https://example.com/j2.jpg" in image_urls
    assert "https://example.com/og_1.jpg" in image_urls
    assert "https://example.com/og_2.jpg" in image_urls


def test_extract_from_html_normalizes_relative_jsonld_image_url() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://example.com/products/mascara" />
        <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Mascara",
            "image": "/-/media/products/mascara.jpg?rev=1",
            "offers": {
              "@type": "Offer",
              "price": "13.99",
              "priceCurrency": "USD"
            }
          }
        </script>
      </head>
      <body></body>
    </html>
    """

    out = _extract_from_html("https://example.com/products/mascara", html)
    expected = "https://example.com/-/media/products/mascara.jpg?rev=1"
    assert out.get("image_url") == expected
    assert (out.get("image_urls") or [])[0] == expected


def test_extract_from_html_collects_images_from_data_product_skus_value() -> None:
    import html as html_lib
    import json

    payload = [
        {
            "id": "sku_1",
            "price": "12.34",
            "price_currency": "USD",
            "inventory_status": "in_stock",
            "images": [
                {"src": "https://example.com/a.jpg"},
                {"src": "https://example.com/b.jpg"},
            ],
        }
    ]
    attr = html_lib.escape(json.dumps(payload))

    html = f"""
    <html>
      <head><title>Example</title></head>
      <body>
        <div data-product-skus-value="{attr}"></div>
      </body>
    </html>
    """

    out = _extract_from_html("https://example.com/p/1", html)
    image_urls = out.get("image_urls") or []
    assert out.get("image_url") == "https://example.com/a.jpg"
    assert "https://example.com/a.jpg" in image_urls
    assert "https://example.com/b.jpg" in image_urls


def test_extract_from_html_collects_images_from_img_tags_and_srcset() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://example.com/p/1" />
        <link rel="preload" as="image" href="https://example.com/preload_1.webp" />
      </head>
      <body>
        <img src="/assets/p1.jpg" />
        <img data-src="//cdn.example.com/p2.png" />
        <img
          srcset="https://example.com/small.jpg 400w, https://example.com/large.jpg 1200w"
        />
        <img src="https://example.com/logo.svg" />
      </body>
    </html>
    """

    out = _extract_from_html("https://example.com/p/1", html)
    image_urls = out.get("image_urls") or []
    assert "https://example.com/preload_1.webp" in image_urls
    assert "https://example.com/assets/p1.jpg" in image_urls
    assert "https://cdn.example.com/p2.png" in image_urls
    assert "https://example.com/large.jpg" in image_urls
    assert all("logo.svg" not in u for u in image_urls)


def test_extract_from_html_dedupes_shopify_size_variants() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://shop.example.com/products/1" />
      </head>
      <body>
        <img src="https://cdn.shopify.com/s/files/1/1/products/img_360x360.jpg?v=1" />
        <img
          srcset="https://cdn.shopify.com/s/files/1/1/products/img_360x360.jpg?v=1 360w, https://cdn.shopify.com/s/files/1/1/products/img_2048x2048.jpg?v=1 2048w"
        />
        <img
          src="https://cdn.shopify.com/s/files/1/1/products/img2.jpg?v=2&width=200"
          srcset="https://cdn.shopify.com/s/files/1/1/products/img2.jpg?v=2&width=200 200w, https://cdn.shopify.com/s/files/1/1/products/img2.jpg?v=2&width=1200 1200w"
        />
      </body>
    </html>
    """

    out = _extract_from_html("https://shop.example.com/products/1", html)
    image_urls = out.get("image_urls") or []
    assert out.get("image_url") == "https://cdn.shopify.com/s/files/1/1/products/img_2048x2048.jpg?v=1"
    assert image_urls == [
        "https://cdn.shopify.com/s/files/1/1/products/img_2048x2048.jpg?v=1",
        "https://cdn.shopify.com/s/files/1/1/products/img2.jpg?v=2&width=1200",
    ]


def test_extract_from_html_separates_variant_label_images_and_filters_gallery() -> None:
    import html as html_lib
    import json

    payload = [
        {
            "id": "sku_1",
            "price": "12.34",
            "price_currency": "USD",
            "inventory_status": "in_stock",
            "images": [
                {"src": "https://cdn.shopify.com/s/files/1/1/products/p_50x50.png?v=1"},
                {"src": "https://cdn.shopify.com/s/files/1/1/products/p_1000x1000.png?v=1"},
            ],
        }
    ]
    attr = html_lib.escape(json.dumps(payload))

    html = f"""
    <html>
      <head><link rel="canonical" href="https://shop.example.com/products/1" /></head>
      <body>
        <div data-product-skus-value="{attr}"></div>
      </body>
    </html>
    """

    out = _extract_from_html("https://shop.example.com/products/1", html)
    variants = out.get("variants") or []
    assert any(v.get("variant_id") == "sku_1" and v.get("image_url") == "https://cdn.shopify.com/s/files/1/1/products/p_1000x1000.png?v=1" for v in variants)
    assert any(v.get("variant_id") == "sku_1" and v.get("label_image_url") == "https://cdn.shopify.com/s/files/1/1/products/p_50x50.png?v=1" for v in variants)

    image_urls = out.get("image_urls") or []
    assert "https://cdn.shopify.com/s/files/1/1/products/p_1000x1000.png?v=1" in image_urls
    assert all("p_50x50.png" not in u for u in image_urls)


def test_extract_from_html_prefers_size_titles_from_data_attrs_when_jsonld_titles_are_generic() -> None:
    html = """
    <html>
      <head>
        <title>Figue Érotique Eau de Parfum</title>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "Figue Érotique Eau de Parfum",
          "description": "Explore desire. An amber fruity fragrance that captures the arc of the fig.",
          "offers": [
            {"@type":"Offer","sku":"T2JZ01","price":"255.00","priceCurrency":"USD","availability":"https://schema.org/InStock","itemOffered":{"@type":"Product","name":"Figue Érotique Eau de Parfum"}},
            {"@type":"Offer","sku":"T2KB01","price":"405.00","priceCurrency":"USD","availability":"https://schema.org/InStock","itemOffered":{"@type":"Product","name":"Figue Érotique Eau de Parfum"}},
            {"@type":"Offer","sku":"T2KD01","price":"1350.00","priceCurrency":"USD","availability":"https://schema.org/InStock","itemOffered":{"@type":"Product","name":"Figue Érotique Eau de Parfum"}}
          ]
        }
        </script>
      </head>
      <body>
        <div data-product-skus-value="[{&quot;id&quot;:&quot;T2JZ01&quot;,&quot;size&quot;:&quot;30 ml&quot;,&quot;price&quot;:255,&quot;price_currency&quot;:&quot;USD&quot;,&quot;inventory_status&quot;:&quot;in_stock&quot;},{&quot;id&quot;:&quot;T2KB01&quot;,&quot;size&quot;:&quot;50 ml&quot;,&quot;price&quot;:405,&quot;price_currency&quot;:&quot;USD&quot;,&quot;inventory_status&quot;:&quot;in_stock&quot;},{&quot;id&quot;:&quot;T2KD01&quot;,&quot;size&quot;:&quot;250 ml&quot;,&quot;price&quot;:1350,&quot;price_currency&quot;:&quot;USD&quot;,&quot;inventory_status&quot;:&quot;in_stock&quot;}]"></div>
      </body>
    </html>
    """

    out = _extract_from_html("https://www.tomfordbeauty.com/product/figue-erotique-eau-de-parfum", html)
    variants = out.get("variants") or []
    assert any(v.get("variant_id") == "T2JZ01" and v.get("title") == "30 ml" for v in variants)
    assert any(v.get("variant_id") == "T2KB01" and v.get("title") == "50 ml" for v in variants)
    assert any(v.get("variant_id") == "T2KD01" and v.get("title") == "250 ml" for v in variants)

    assert "Explore desire" in (out.get("description") or "")


def test_extract_from_html_prefers_expanded_product_details_over_short_jsonld_description() -> None:
    html = """
    <html>
      <head>
        <title>The Acne Set</title>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "The Acne Set",
          "description": "A 3-step regimen with Salicylic Acid 2% Solution for clearer skin",
          "offers": {
            "@type":"Offer",
            "price":"16.70",
            "priceCurrency":"USD",
            "availability":"https://schema.org/InStock"
          }
        }
        </script>
      </head>
      <body>
        <input
          type="hidden"
          id="overview-about-text"
          value="%3Cp%3E%3Cstrong%3EThe%20Acne%20Set%3C/strong%3E%20offers%20a%20targeted%20skincare%20regimen%20featuring%20%3Cstrong%3ESalicylic%20Acid%202%25%20Solution%3C/strong%3E%20for%20treating%20acne.%3C/p%3E%0A%3Cp%3EThis%20set%20includes...%3C/p%3E%0A%3Cul%3E%3Cli%3E%3Cstrong%3EGlucoside%20Foaming%20Cleanser%3C/strong%3E%20removes%20dirt%20and%20impurities.%3C/li%3E%3Cli%3E%3Cstrong%3ESalicylic%20Acid%202%25%20Solution%3C/strong%3E%20helps%20clear%20pores.%3C/li%3E%3C/ul%3E"
        />
      </body>
    </html>
    """

    out = _extract_from_html("https://theordinary.com/en-us/the-acne-set-100631.html", html)

    assert out.get("description") == (
        "The Acne Set offers a targeted skincare regimen featuring Salicylic Acid 2% Solution for treating acne.\n\n"
        "This set includes...\n\n"
        "Glucoside Foaming Cleanser removes dirt and impurities.\n"
        "Salicylic Acid 2% Solution helps clear pores."
    )


# --- a dead destination must not be able to masquerade as a cached one -------------------------

def test_resolve_external_offer_defaults_to_returning_the_cache_on_a_dead_url(monkeypatch) -> None:
    """The existing contract, pinned before it is opted out of.

    Serving deliberately prefers a cached snapshot over nothing — one transient outage must not
    blank the catalogue. Every pre-existing caller relies on that, so `raise_on_unavailable`
    defaults to False and this behaviour is unchanged.
    """
    import asyncio

    from services import external_offers_service as eos

    cached = {"id": "eo_1", "market": "US", "canonical_url": "https://brand.com/products/x"}

    async def fake_get_snapshot_row(_market, _hash):
        return cached

    async def fake_fetch_html(url, *, max_wait=None, observed=None):
        raise eos.ExternalOfferUnavailable(status_code=404, url=url)

    monkeypatch.setattr(eos, "_get_snapshot_row", fake_get_snapshot_row)
    monkeypatch.setattr(eos, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(eos, "_row_to_snapshot", lambda row: row)
    monkeypatch.setattr(eos, "_is_stale", lambda _ts: True)

    got = asyncio.run(
        eos.resolve_external_offer(market="US", url="https://brand.com/products/x")
    )
    assert got is cached


def test_resolve_external_offer_can_be_asked_to_surface_a_dead_url(monkeypatch) -> None:
    """THE DEFECT THIS OPT-IN REMOVES.

    `_refresh_external_seed_by_id` got the cached snapshot back, wrote it, and set
    `updated_at = NOW()` — so fetching a 404 made the seed look FRESHER to the staleness gate.
    A caller asking about the LINK rather than the CONTENT must hear the 404.
    """
    import asyncio

    import pytest as _pytest

    from services import external_offers_service as eos

    cached = {"id": "eo_1", "market": "US"}

    async def fake_get_snapshot_row(_market, _hash):
        return cached

    async def fake_fetch_html(url, *, max_wait=None, observed=None):
        raise eos.ExternalOfferUnavailable(status_code=404, url=url, final_url=url)

    monkeypatch.setattr(eos, "_get_snapshot_row", fake_get_snapshot_row)
    monkeypatch.setattr(eos, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(eos, "_row_to_snapshot", lambda row: row)
    monkeypatch.setattr(eos, "_is_stale", lambda _ts: True)

    with _pytest.raises(eos.ExternalOfferUnavailable) as caught:
        asyncio.run(
            eos.resolve_external_offer(
                market="US", url="https://brand.com/products/x", raise_on_unavailable=True
            )
        )
    assert caught.value.status_code == 404


def test_a_transport_failure_still_falls_back_to_the_cache_even_when_asked_to_raise(
    monkeypatch,
) -> None:
    """`raise_on_unavailable` is about the ORIGIN'S ANSWER, not about our own network.

    A timeout says nothing about the product, so it keeps the old fallback — otherwise a flaky
    link would break serving for every caller that opted in.
    """
    import asyncio

    from services import external_offers_service as eos

    cached = {"id": "eo_1", "market": "US"}

    async def fake_get_snapshot_row(_market, _hash):
        return cached

    async def fake_fetch_html(url, *, max_wait=None, observed=None):
        raise TimeoutError("connect timeout")

    monkeypatch.setattr(eos, "_get_snapshot_row", fake_get_snapshot_row)
    monkeypatch.setattr(eos, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(eos, "_row_to_snapshot", lambda row: row)
    monkeypatch.setattr(eos, "_is_stale", lambda _ts: True)

    got = asyncio.run(
        eos.resolve_external_offer(
            market="US", url="https://brand.com/products/x", raise_on_unavailable=True
        )
    )
    assert got is cached
