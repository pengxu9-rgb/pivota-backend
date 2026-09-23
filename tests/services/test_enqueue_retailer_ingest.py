"""Enqueue rows are validated before anything reaches the ledger."""
import pytest

from scripts.enqueue_retailer_ingest import _row_to_job


def test_a_row_becomes_a_job_with_vendors_in_its_options():
    job = _row_to_job({"domain": " k-touch.us ", "brand": "3CE", "vendors": ["3CE"], "priority": 5,
                       "options": {"lip_title_evidence": True, "only_category": "beauty/makeup/lip"}})
    assert job == {"domain": "k-touch.us", "brand": "3CE", "priority": 5,
                   "options": {"lip_title_evidence": True, "only_category": "beauty/makeup/lip", "vendors": ["3CE"]}}


@pytest.mark.parametrize("row", [
    {"domain": "k-touch.us", "brand": "3CE"},                                   # no vendors
    {"domain": "", "brand": "3CE", "vendors": ["3CE"]},                          # no domain
    {"domain": "k-touch.us", "brand": "3CE", "vendors": ["3CE"], "options": {"apply": True}},  # unknown option
])
def test_bad_rows_are_refused(row):
    with pytest.raises(ValueError):
        _row_to_job(row)
