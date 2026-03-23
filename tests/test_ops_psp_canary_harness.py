import sys
from argparse import Namespace
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ops_psp_canary_harness import _build_payment_request  # noqa: E402


def _build_args(*, stripe_checkout_canary: bool) -> Namespace:
    return Namespace(
        amount_minor=100,
        currency="USD",
        email="merchant@example.com",
        stripe_checkout_canary=stripe_checkout_canary,
    )


def test_build_payment_request_defaults_to_standard_canary_metadata() -> None:
    request = _build_payment_request(_build_args(stripe_checkout_canary=False), "ord_test_1")

    assert request["amount"] == 100
    assert request["currency"] == "USD"
    assert request["order_id"] == "ord_test_1"
    assert request["customer_email"] == "merchant@example.com"
    assert request["metadata"] == {"source": "ops_psp_canary_harness"}


def test_build_payment_request_can_force_stripe_checkout_mode() -> None:
    request = _build_payment_request(_build_args(stripe_checkout_canary=True), "ord_test_2")

    assert request["metadata"]["source"] == "ops_psp_canary_harness"
    assert request["metadata"]["psp_mode"] == "stripe_checkout"
