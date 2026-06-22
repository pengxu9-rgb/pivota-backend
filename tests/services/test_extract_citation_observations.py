"""P0.2 — extract_citation_observations (pure, no DB).

Flattens build_authority_map's output into per-(content_key, host, query,
provider) rows, gated so only DEPOSITABLE products emit on their resolved key.
"""

from services.audit_evidence_builder import extract_citation_observations
from services.catalog_identity import (
    DEPOSIT_BASIS_GTIN,
    DEPOSIT_BASIS_UNRESOLVED,
    ResolvedDepositKey,
)


def _report_with_authority():
    return {
        "authority_map": {
            "skus": [
                {
                    "sku_key": "sku1",
                    "product_key": "m1|shopify|sku1",
                    "content_key": "ck_" + "a" * 32,
                    "authority_hosts": [
                        {
                            "host": "reddit.com",
                            "host_type": "reddit",
                            "citation_role": "endorsement",
                            "first_party": False,
                            "is_competitor": False,
                            "evidence_urls": ["https://reddit.com/r/x/1"],
                            "query_observations": [
                                {
                                    "query": "best toner",
                                    "query_class": "category",
                                    "provider": "gemini",
                                },
                                {
                                    "query": "anua toner review",
                                    "query_class": "branded",
                                    "provider": "chatgpt",
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    }


def test_no_map_emits_with_sku_content_key():
    out = extract_citation_observations(_report_with_authority())
    assert len(out) == 2
    assert {o["provider"] for o in out} == {"gemini", "chatgpt"}
    assert all(o["content_key"] == "ck_" + "a" * 32 for o in out)
    assert all(o["cited_host"] == "reddit.com" for o in out)
    assert all(o["evidence_url"] == "https://reddit.com/r/x/1" for o in out)
    assert all(o["content_key_basis"] == "unknown" for o in out)


def test_depositable_map_uses_resolved_key_and_basis():
    report = _report_with_authority()
    resolved_ck = "ck_" + "b" * 32
    out = extract_citation_observations(
        report,
        content_key_map={
            "m1|shopify|sku1": ResolvedDepositKey(resolved_ck, DEPOSIT_BASIS_GTIN, 1.0)
        },
    )
    assert len(out) == 2
    assert all(o["content_key"] == resolved_ck for o in out)
    assert all(o["content_key_basis"] == DEPOSIT_BASIS_GTIN for o in out)


def test_unresolved_product_emits_nothing():
    out = extract_citation_observations(
        _report_with_authority(),
        content_key_map={
            "m1|shopify|sku1": ResolvedDepositKey("ck_x", DEPOSIT_BASIS_UNRESOLVED, 0.0)
        },
    )
    assert out == []


def test_missing_query_or_provider_skipped():
    report = _report_with_authority()
    report["authority_map"]["skus"][0]["authority_hosts"][0][
        "query_observations"
    ] = [
        {"query": "", "provider": "gemini"},
        {"query": "ok", "provider": ""},
        {"query": "good", "provider": "gemini"},
    ]
    out = extract_citation_observations(report)
    assert len(out) == 1
    assert out[0]["query"] == "good"


def test_no_authority_map_returns_empty():
    assert extract_citation_observations({}) == []
