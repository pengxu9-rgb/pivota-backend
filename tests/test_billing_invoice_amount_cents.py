"""invoices.total_cents from a Stripe invoice: cash received when paid, the billed amount otherwise.

Stripe sends amount_paid=0 (not null) on an unpaid invoice, so preferring it for invoice.payment_failed
zeroed the local total of every failed invoice.
"""

import pytest

from routes.billing_routes import _invoice_amount_cents, _invoice_values

UNPAID = {"id": "in_1", "amount_paid": 0, "amount_due": 4900, "total": 4900}
PAID = {"id": "in_1", "amount_paid": 4900, "amount_due": 4900, "total": 4900}


def test_a_failed_invoice_records_the_amount_due_not_the_zero_paid():
    assert _invoice_amount_cents(UNPAID, status_value="payment_failed") == 4900
    assert _invoice_values(UNPAID, merchant_id="m_1", status_value="payment_failed")["total_cents"] == 4900


def test_a_paid_invoice_records_the_cash_received():
    # A customer credit balance can cover part of it: amount_paid is the cash, not the total.
    partly_credited = {**PAID, "amount_paid": 3000, "amount_due": 3000}
    assert _invoice_amount_cents(partly_credited, status_value="paid") == 3000


@pytest.mark.parametrize("status", ["paid", "payment_failed"])
def test_it_falls_back_to_the_total_when_the_amounts_are_absent(status):
    assert _invoice_amount_cents({"id": "in_1", "total": 1200}, status_value=status) == 1200


def test_a_failed_invoice_never_reads_amount_paid():
    assert _invoice_amount_cents({"id": "in_1", "amount_paid": 700}, status_value="payment_failed") == 0
