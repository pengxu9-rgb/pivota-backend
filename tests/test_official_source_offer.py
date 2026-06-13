"""official_source trust signal on OfferNode (decision-grade `trust` dimension)."""
from services.pivot_query_service import _registrable_host, _is_official_brand_source


def test_registrable_host_strips_scheme_www_path():
    assert _registrable_host("https://www.cosrx.com/products/x") == "cosrx.com"
    assert _registrable_host("roundlab.com") == "roundlab.com"
    assert _registrable_host("https://shop.brand.co.uk/p") == "co.uk"  # last two labels
    assert _registrable_host(None) is None
    assert _registrable_host("") is None


def test_official_when_source_domain_matches_canonical_host():
    assert _is_official_brand_source("cosrx.com", "https://www.cosrx.com/products/snail-96") is True
    assert _is_official_brand_source("www.skin1004.com", "https://skin1004.com/products/centella") is True


def test_not_official_for_retailer_mirror_or_missing():
    assert _is_official_brand_source("sephora.com", "https://www.cosrx.com/products/x") is False
    assert _is_official_brand_source(None, "https://cosrx.com/x") is False
    assert _is_official_brand_source("cosrx.com", None) is False
