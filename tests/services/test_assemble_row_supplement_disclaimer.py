"""assemble_row floor-merges the FDA/DSHEA supplement disclaimer into the SERVED
agent_pdp_view row even when the merchant authored none — so the direct PDP
route serves it at parity with the search path. Pure, no DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.agent_pdp_view_assembler import assemble_row  # noqa: E402
from services.claim_safety import FDA_DSHEA_DISCLAIMER_CODE  # noqa: E402


def _products(*, category_kind=None, title="Magnesium Glycinate 400mg"):
    return [
        {
            "title": title,
            "description": "raw storefront description",
            "product_key": "m1|url_audit|x",
            "category_kind": category_kind,
        }
    ]


def _codes(row):
    return [d["code"] for d in (row["required_disclaimers"] or [])]


def test_supplement_with_no_authored_disclaimers_gets_fda():
    row = assemble_row(
        content_key="ck_x",
        products=_products(category_kind="supplement"),
        skus=[],
        offers=[],
        external_seed=None,
        evidence=None,
    )
    assert row is not None
    assert _codes(row) == [FDA_DSHEA_DISCLAIMER_CODE]


def test_skincare_row_has_no_mandatory_disclaimer():
    row = assemble_row(
        content_key="ck_x",
        products=_products(category_kind="skincare"),
        skus=[],
        offers=[],
        external_seed=None,
        evidence=None,
    )
    assert row is not None
    assert row["required_disclaimers"] is None


def test_supplement_keeps_authored_evidence_disclaimers_and_adds_fda():
    row = assemble_row(
        content_key="ck_x",
        products=_products(category_kind="supplement"),
        skus=[],
        offers=[],
        external_seed=None,
        evidence={"required_disclaimers": [{"code": "prop65", "text": "Prop 65"}]},
    )
    assert row is not None
    assert _codes(row) == ["prop65", FDA_DSHEA_DISCLAIMER_CODE]
