"""P0.2 — extract_evidence_items content_key stamping (pure, no DB).

Stamps the canonical entity key onto evidence only when the product maps to a
DEPOSITABLE (resolved) content_key. Unmapped / unresolved keys leave it unset,
so nothing accretes on the deliberately non-unique content_key (mig 083).
"""

from services.audit_evidence_builder import extract_evidence_items
from services.catalog_identity import (
    DEPOSIT_BASIS_GTIN,
    DEPOSIT_BASIS_UNRESOLVED,
    ResolvedDepositKey,
)


def _brand_report_with_one_quote():
    return {
        "per_product": [
            {
                "product_key": "m1|shopify|sku1",
                "evidence_quotes": [
                    {
                        "excerpt_text": "great toner",
                        "source_host": "reddit.com",
                        "query": "best toner",
                    }
                ],
            }
        ]
    }


def test_no_map_leaves_content_key_unset():
    out = extract_evidence_items(_brand_report_with_one_quote())
    assert out, "expected at least one evidence item"
    assert all(ev.get("content_key") is None for ev in out)


def test_depositable_map_stamps_content_key():
    report = _brand_report_with_one_quote()
    pk = extract_evidence_items(report)[0]["product_key"]
    ck = "ck_" + "a" * 32
    out = extract_evidence_items(
        report,
        content_key_map={pk: ResolvedDepositKey(ck, DEPOSIT_BASIS_GTIN, 1.0)},
    )
    assert out[0]["content_key"] == ck


def test_unresolved_map_does_not_stamp():
    report = _brand_report_with_one_quote()
    pk = extract_evidence_items(report)[0]["product_key"]
    ck = "ck_" + "b" * 32
    out = extract_evidence_items(
        report,
        content_key_map={pk: ResolvedDepositKey(ck, DEPOSIT_BASIS_UNRESOLVED, 0.0)},
    )
    assert out[0].get("content_key") is None
