"""Per-URL creator/community evidence attribution (HoverAir share-report
follow-up): a cited creator video or Reddit thread that already covers the
merchant is an AMPLIFY target, not a "get cited" pitch — the two must be
distinguishable in the payload.

`evidence_urls_about_merchant` (host rows) and `about_merchant` (reddit
threads) mark content as covering the merchant when ANY of:
  - the answer named the SKU (exact/near parse flags),
  - the grounded query was branded ("is <brand> legit"),
  - the URL slug spells the brand (grounding titles are unreliable — Gemini
    stamps the bare domain — but a permalink slug that names the brand is
    direct evidence).
"""

from __future__ import annotations

from typing import Any, Dict, List

from services.agent_center_bd_report_service import (
    _url_slug_names_brand,
    build_authority_map,
)


def _run(
    query: str,
    uri: str,
    *,
    axis: str = "category",
    correct_sku: bool = False,
    title: str = "youtube.com",
) -> Dict[str, Any]:
    return {
        "query": query,
        "axis_metadata": {"axis": axis},
        "parsed": {"product_visible": correct_sku, "correct_sku": correct_sku},
        "grounding_sources": [{"uri": uri, "title": title}],
        "url_match": {"in_grounding": False},
    }


def _map(raw_runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    probe_runs = [{"provider": "gemini", "probe_run_id": "p", "raw_runs": raw_runs}]
    return build_authority_map(
        [{"sku_key": "sku-1", "product_key": "prod-1"}],
        {"sku-1": probe_runs},
        merchant_host="hoverair.com",
        merchant_brand="HoverAir",
    )


def _host_row(amap: Dict[str, Any], host: str) -> Dict[str, Any]:
    for row in amap["skus"][0]["authority_hosts"]:
        if row["host"] == host:
            return row
    raise AssertionError(f"host {host} not in authority map")


def test_category_query_no_brand_slug_is_not_about_merchant():
    uri = "https://www.youtube.com/watch?v=abc123"
    amap = _map([_run("best camera drone for travel", uri)])
    row = _host_row(amap, "youtube.com")
    assert uri in row["evidence_urls"]
    assert row["evidence_urls_about_merchant"] == []


def test_branded_query_marks_url_about_merchant():
    uri = "https://www.youtube.com/watch?v=abc123"
    amap = _map([_run("is HoverAir legit", uri, axis="trust")])
    row = _host_row(amap, "youtube.com")
    assert row["evidence_urls_about_merchant"] == [uri]


def test_brand_naming_slug_marks_url_about_merchant_even_on_category_query():
    uri = "https://www.youtube.com/watch/hoverair-x1-pro-review-after-6-months"
    amap = _map([_run("best camera drone for travel", uri)])
    row = _host_row(amap, "youtube.com")
    assert row["evidence_urls_about_merchant"] == [uri]


def test_sku_matched_answer_marks_url_about_merchant():
    uri = "https://www.youtube.com/watch?v=abc123"
    amap = _map([_run("best camera drone", uri, correct_sku=True)])
    row = _host_row(amap, "youtube.com")
    assert row["evidence_urls_about_merchant"] == [uri]


def test_reddit_thread_brand_slug_is_about_merchant_without_sku_match():
    """The live-report shape: the r/drones thread is literally titled after
    the product, but the grounded answer skipped the exact SKU name — the
    thread must still be marked as covering the merchant (matched_sku stays
    honest and answer-level)."""
    uri = (
        "https://www.reddit.com/r/drones/comments/1atit33/"
        "hoverair_x1_what_do_you_actually_do_with_it/"
    )
    amap = _map([_run("best camera drone for travel", uri, title="reddit.com")])
    subs = amap["skus"][0]["reddit"]["subreddits"]
    threads = [t for s in subs for t in s["threads"]]
    assert len(threads) == 1
    assert threads[0]["about_merchant"] is True
    assert threads[0]["matched_sku"] is False


def test_reddit_thread_unrelated_slug_not_about_merchant():
    uri = "https://www.reddit.com/r/drones/comments/9zz/best_budget_fpv_setups_2026/"
    amap = _map([_run("best camera drone for travel", uri, title="reddit.com")])
    threads = [t for s in amap["skus"][0]["reddit"]["subreddits"] for t in s["threads"]]
    assert threads[0]["about_merchant"] is False


def test_url_slug_matcher_ignores_short_and_hostname_hits():
    aliases = ("hoverair", "hov")
    # short alias never matches; host part of the URL never matches (path only)
    assert _url_slug_names_brand("https://hov.example.com/watch?v=x", aliases) is False
    assert _url_slug_names_brand("https://hoverair.com/", aliases) is False
    assert _url_slug_names_brand(
        "https://youtube.com/watch/the-hoverair-x1-review", aliases
    ) is True


def test_url_slug_matcher_skips_vertex_redirector_uris():
    """A redirector path is an opaque token — an alias landing inside it can
    only be a false positive (review round 2)."""
    uri = (
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
        "AUZIhoverairYs0m3Tok3n"
    )
    assert _url_slug_names_brand(uri, ("hoverair",)) is False


def test_url_slug_matcher_is_boundary_anchored_not_substring():
    """Compacted-substring matching hit across slug segments — boundary
    matching must not (review round 2)."""
    aliases = ("hoverair",)
    assert _url_slug_names_brand(
        "https://youtube.com/watch/best-hover-airplanes-2026", aliases
    ) is False
    assert _url_slug_names_brand(
        "https://youtube.com/watch/whoverairbnb-tour", aliases
    ) is False


def test_url_slug_matcher_skips_dictionary_word_brands():
    """A 4-char dictionary-word brand ("Glow") survives boundary matching in
    category slugs — the slug arm requires >=5 compact chars; the URL just
    stays in the pitch bucket."""
    assert _url_slug_names_brand(
        "https://youtube.com/watch/best-glow-serums", ("glow",)
    ) is False


def test_missing_axis_does_not_count_as_branded_evidence():
    """run_query_class defaults BRANDED on a missing axis; a degraded run must
    not inflate the amplify bucket (review round 2)."""
    uri = "https://www.youtube.com/watch?v=abc123"
    run = _run("best camera drone for travel", uri)
    del run["axis_metadata"]
    amap = _map([run])
    row = _host_row(amap, "youtube.com")
    assert row["evidence_urls_about_merchant"] == []


def test_new_fields_survive_share_redaction():
    """The whole point of the fields is the (public, allowlisted) share
    payload — assert redaction keeps them."""
    from routes.merchant_audit_routes import _redact_shared_report

    uri = "https://www.youtube.com/watch?v=abc123"
    thread_uri = (
        "https://www.reddit.com/r/drones/comments/1atit33/"
        "hoverair_x1_what_do_you_actually_do_with_it/"
    )
    amap = _map([
        _run("is HoverAir legit", uri, axis="trust"),
        _run("best camera drone", thread_uri, title="reddit.com"),
    ])
    shaped = {
        "status": "succeeded",
        "run_id": "r-1",
        "authority_map": amap,
        "per_sku_reports": [],
    }
    out = _redact_shared_report(shaped)
    row = next(
        r for r in out["authority_map"]["skus"][0]["authority_hosts"]
        if r["host"] == "youtube.com"
    )
    assert row["evidence_urls_about_merchant"] == [uri]
    threads = [
        t
        for sub in out["authority_map"]["skus"][0]["reddit"]["subreddits"]
        for t in sub["threads"]
    ]
    assert any(t.get("about_merchant") is True for t in threads)
