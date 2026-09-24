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
    {"domain": "k-touch.us", "brand": "3CE", "vendors": ["3CE"],
     "options": {"category_path": "beauty/makeup/lip/lipstick"}},                            # leaf fallback
])
def test_bad_rows_are_refused(row):
    with pytest.raises(ValueError):
        _row_to_job(row)


def test_a_brand_official_row_is_accepted():
    job = _row_to_job({"domain": "clinique.com", "brand": "Clinique", "vendors": ["Clinique"],
                       "options": {"source_role": "brand_official", "max_products": 1500}})
    assert job["options"]["source_role"] == "brand_official"


@pytest.mark.parametrize("options", [
    {"source_role": "brand"},                                       # the drain's normalization refuses it
    {"source_role": "brand_official", "retailer_name": "Clinique"},  # a retailer's name on a brand store
])
def test_rows_the_drain_would_refuse_are_refused_at_enqueue(options):
    with pytest.raises(ValueError, match="source_role|retailer_name"):
        _row_to_job({"domain": "clinique.com", "brand": "Clinique", "vendors": ["Clinique"], "options": options})


def test_a_known_retailer_is_refused_as_a_brand_official_store_at_enqueue():
    with pytest.raises(ValueError, match="known retailer"):
        _row_to_job({"domain": "sephora.com", "brand": "Clinique", "vendors": ["Clinique"],
                     "options": {"source_role": "brand_official"}})
