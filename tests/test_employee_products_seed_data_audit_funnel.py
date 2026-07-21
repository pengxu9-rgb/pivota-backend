"""Tests for the _seed_data_payload audit funnel.

Background: routes/employee_products.py has 9 different seed_data
write paths (CSV catalog import, CSV v1 import, storefront-seed
candidate, bulk update, manual edit, etc.). PR #412 audited only
one of those (storefront-seed candidate). The remaining 8 paths
were still vulnerable to dirty content from external-seeds-backfill /
seed-correction codex skill cycles.

Funnel fix: hook services.seed_content_audit.audit_seed_data into
_seed_data_payload itself. All 9 write paths funnel through that
function before the SQL bind, so auditing there closes every
backfill-write entry point at once.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.employee_products import _seed_data_payload  # noqa: E402


def test_payload_decodes_html_entities_in_description() -> None:
    """The trigger from the user's PDP — HTML entities in description.
    The funnel must decode them before persistence. Reproducible from
    routes/employee_products.py via ANY of the 9 write paths."""
    seed_data = {
        "title": "Gloss Bomb Stix",
        "description": "It&rsquo;s about to get juicy",
        "brand": "Fenty Beauty",
    }
    out = _seed_data_payload(seed_data)
    assert out["description"] == "It’s about to get juicy"
    assert "&rsquo;" not in out["description"]


def test_payload_strips_shade_name_prefix_in_ingredients() -> None:
    """Fenty pattern: shade names + colon precede actual INCI list.
    The funnel must strip the prefix before persistence."""
    seed_data = {
        "title": "Lip Color",
        "pdp_ingredients_raw": "TWO'LIP KISS, SORTA $ELFISH: BIS-DIGLYCERYL POLYACYLADIPATE-2, AQUA",
    }
    out = _seed_data_payload(seed_data)
    assert out["pdp_ingredients_raw"].startswith("BIS-DIGLYCERYL POLYACYLADIPATE-2")
    assert "TWO'LIP KISS" not in out["pdp_ingredients_raw"]


def test_payload_stamps_review_summary_with_audit_metadata() -> None:
    """Every write must end up with a review_summary stamp so the audit
    pipeline (and downstream observability) can tell which rows have
    been processed by which auditor version."""
    seed_data = {"description": "It&rsquo;s nice"}
    out = _seed_data_payload(seed_data)
    assert "review_summary" in out
    assert out["review_summary"]["auditor"] == "seed_content_audit_v1"
    assert out["review_summary"]["review_status"] == "auto_corrected"
    assert "decoded_html_entities_in_description" in out["review_summary"]["fixes_applied"]


def test_payload_clean_input_still_gets_audit_stamp() -> None:
    """Already-clean content should pass through unchanged content-wise
    but still get an audit stamp (`no_issues_found`). Otherwise we
    can't distinguish "not yet audited" from "audited and clean"."""
    seed_data = {
        "title": "Clean Product",
        "description": "A clean description.",
        "pdp_ingredients_raw": "AQUA, GLYCERIN, NIACINAMIDE",
    }
    out = _seed_data_payload(seed_data)
    assert out["description"] == "A clean description."
    assert out["pdp_ingredients_raw"] == "AQUA, GLYCERIN, NIACINAMIDE"
    assert out["review_summary"]["review_status"] == "no_issues_found"
    assert out["review_summary"]["fixes_applied"] == []


def test_payload_handles_non_dict_input_safely() -> None:
    """Defensive: legacy callers might pass an already-stringified JSON
    blob or None. The funnel must NOT crash and must NOT try to audit
    non-dict input."""
    out = _seed_data_payload("already-stringified-json")
    assert out == "already-stringified-json"


def test_payload_recurses_into_snapshot() -> None:
    """seed_data.snapshot.* fields are mirrored from shopify scrapes
    and must also be cleaned. The auditor's recursion catches them;
    the funnel inherits that behavior."""
    seed_data = {
        "snapshot": {
            "description": "Has &rsquo;",
            "pdp_ingredients_raw": "SHADE1, SHADE2: AQUA, GLYCERIN",
        },
    }
    out = _seed_data_payload(seed_data)
    assert out["snapshot"]["description"] == "Has ’"
    assert out["snapshot"]["pdp_ingredients_raw"] == "AQUA, GLYCERIN"
