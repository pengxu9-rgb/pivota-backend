"""PR-1b: scheduled re-audit job tests.

Covers the pure-logic surface of jobs/scheduled_audit_job:
  - is_audit_due: schedule + interval window logic
  - _extract_products_from_prior_report: tolerates report shape variants

The DB-touching path (_list_due_merchants, _re_audit_merchant) is
exercised end-to-end on staging — not unit-tested here because mocking
sqlalchemy + databases interactions through the full audit pipeline
adds more brittleness than confidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jobs.scheduled_audit_job import (
    _extract_products_from_prior_report,
    is_audit_due,
)


# ---------------------------------------------------------------------------
# is_audit_due
# ---------------------------------------------------------------------------


def test_is_audit_due_when_never_audited_and_opted_in():
    """First-ever audit fires immediately on the next cron tick."""
    assert is_audit_due(None, "weekly") is True
    assert is_audit_due(None, "monthly") is True


def test_is_audit_due_false_for_opt_out():
    """Opted-out merchants never auto-audit."""
    assert is_audit_due(None, "none") is False
    assert is_audit_due(
        datetime(2026, 1, 1, tzinfo=timezone.utc), "none",
    ) is False


def test_is_audit_due_false_within_weekly_window():
    """Audited 5 days ago, weekly schedule = not yet due."""
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=5)
    assert is_audit_due(last, "weekly", now=now) is False


def test_is_audit_due_true_at_weekly_boundary():
    """Audited exactly 7 days ago = due (boundary inclusive)."""
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=7)
    assert is_audit_due(last, "weekly", now=now) is True


def test_is_audit_due_true_past_weekly_boundary():
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=10)
    assert is_audit_due(last, "weekly", now=now) is True


def test_is_audit_due_false_within_monthly_window():
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=20)
    assert is_audit_due(last, "monthly", now=now) is False


def test_is_audit_due_true_past_monthly_boundary():
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=31)
    assert is_audit_due(last, "monthly", now=now) is True


def test_is_audit_due_unknown_schedule_returns_false():
    """Defensive: schedule values outside the constraint check return
    False (the cron should NEVER pick up a malformed schedule)."""
    assert is_audit_due(None, "daily") is False
    assert is_audit_due(None, "yearly") is False
    assert is_audit_due(None, "") is False


# ---------------------------------------------------------------------------
# _extract_products_from_prior_report
# ---------------------------------------------------------------------------


def test_extract_products_from_canonical_per_product_shape():
    """Canonical shape: report_jsonb.per_product[*] each with `product`
    + `merchant_pdp_url`."""
    report = {
        "per_product": [
            {
                "product": {"title": "X", "vendor": "Acme", "product_type": "skincare"},
                "merchant_pdp_url": "https://acme.co/p/x",
            },
            {
                "product": {"title": "Y", "vendor": "Acme", "product_type": "skincare"},
                "merchant_pdp_url": "https://acme.co/p/y",
            },
        ],
    }
    out = _extract_products_from_prior_report(report)
    assert len(out) == 2
    assert out[0]["title"] == "X"
    assert out[0]["pdp_url"] == "https://acme.co/p/x"
    assert out[0]["vendor"] == "Acme"
    assert out[0]["product_type"] == "skincare"


def test_extract_products_skips_missing_title_or_url():
    """Defensive: skip entries that lack either field — partial data
    would produce nonsense audit results."""
    report = {
        "per_product": [
            {"product": {"title": "X"}},  # no pdp_url
            {"merchant_pdp_url": "https://acme.co/p/y"},  # no title
            {
                "product": {"title": "Z"},
                "merchant_pdp_url": "https://acme.co/p/z",
            },
        ],
    }
    out = _extract_products_from_prior_report(report)
    assert len(out) == 1
    assert out[0]["title"] == "Z"


def test_extract_products_returns_empty_for_missing_per_product():
    assert _extract_products_from_prior_report({}) == []
    assert _extract_products_from_prior_report({"per_product": None}) == []
    assert _extract_products_from_prior_report({"per_product": "not a list"}) == []


def test_extract_products_returns_empty_for_garbage():
    assert _extract_products_from_prior_report(None) == []
    assert _extract_products_from_prior_report("string") == []
    assert _extract_products_from_prior_report(42) == []


def test_extract_products_handles_optional_vendor_and_type():
    """vendor and product_type are nullable in the source — output
    preserves None, doesn't fabricate."""
    report = {
        "per_product": [{
            "product": {"title": "X"},
            "merchant_pdp_url": "https://acme.co/p/x",
        }],
    }
    out = _extract_products_from_prior_report(report)
    assert out[0]["vendor"] is None
    assert out[0]["product_type"] is None
