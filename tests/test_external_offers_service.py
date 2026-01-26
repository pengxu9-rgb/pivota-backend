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
