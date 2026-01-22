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

