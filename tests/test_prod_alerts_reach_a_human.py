"""The alerting worked on 2026-08-28. Nobody heard it.

The backend wedged on database pool exhaustion at 10:46:56Z and served 503/504 for 5h40m. The
uptime check detected it correctly — `check_passed` for api.pivota.cc went to 0.00 by 12:32 and
stayed there — and `prod: host is down` fired as designed. The single notification channel pointed
at an address nobody reads, chosen by a silent default: `ALERT_EMAIL` fell back to
`gcloud config get-value account`, i.e. whoever last ran the script.

Detection was never the gap. DELIVERY was. These tests pin the three lines that decide whether a
firing alert reaches a person, because all three are invisible on a dashboard that cheerfully
reports "5 policies, all enabled".

Same family as tests/test_setup_scheduler_is_safe_to_rerun.py: assertions against the shell source,
because there is nothing importable here.
"""

from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "infra" / "gcp" / "setup_monitoring.sh"


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _uncommented(text: str) -> str:
    """Drop comment lines.

    Load-bearing: this file's rationale mentions `gcloud config get-value account` and the old
    threshold in prose. A whole-file grep would match the explanation and pass while the code
    underneath had regressed.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_prod_refuses_to_infer_the_alert_destination(source: str) -> None:
    """The exact regression: prod must not guess where pages go."""
    body = _uncommented(source)
    assert 'if [ "$ENV" = prod ]; then' in body
    assert "ALERT_EMAIL is required for prod" in body
    # The convenience default must survive for staging, which pages no one — deleting it
    # outright would be a different (and worse) change than the one this guards.
    assert 'config get-value account' in body


def test_the_inferred_default_is_not_reachable_from_prod(source: str) -> None:
    """A fallback that prod can still reach is the bug wearing a guard.

    Asserts ordering, not mere presence: the `exit 2` for prod must come BEFORE the
    `gcloud config get-value account` assignment, or prod falls through to it anyway.
    """
    body = _uncommented(source)
    guard = body.index("ALERT_EMAIL is required for prod")
    fallback = body.index('ALERT_EMAIL="$("$GCLOUD" config get-value account')
    assert guard < fallback, "prod guard must precede the inferred fallback"


def test_an_unverified_channel_is_surfaced(source: str) -> None:
    """Cloud Monitoring delivers nothing to an unverified email channel.

    The live channel on 2026-08-28 reported no verificationStatus at all, so every policy was
    firing into the void with no signal anywhere that this was happening.
    """
    body = _uncommented(source)
    assert "verificationStatus" in body
    assert '[ "$VERIFIED" != VERIFIED ]' in body


def test_the_lb_5xx_threshold_is_reachable_at_real_traffic(source: str) -> None:
    """0.2 req/s asks for 12 5xx per second on an API serving ~0.03 req/s.

    A total outage returning 5xx to every caller still sat an order of magnitude under it, which
    is why this policy had never fired once. The assertion is on the *magnitude* rather than an
    exact number so the threshold stays tunable — what must not come back is a value that no
    achievable failure can reach.
    """
    body = _uncommented(source)
    assert "COMPARISON_GT 0.2 300s" not in body, "the unreachable threshold is back"
    marker = "prod: load balancer 5xx"
    idx = body.rindex(marker)
    tail = body[idx : idx + 800]
    threshold = float(tail.split("COMPARISON_GT")[1].split()[0])
    assert threshold <= 0.05, (
        f"threshold {threshold}/s is unreachable at ~0.03 req/s baseline traffic"
    )


def test_pool_exhaustion_has_its_own_alert(source: str) -> None:
    """num_backends cannot catch a client-side leak.

    From the server a leaked connection is indistinguishable from an idle one, so the Cloud SQL
    policy read 9% throughout an outage caused entirely by connection exhaustion. This alert
    watches the application's own symptom instead.
    """
    body = _uncommented(source)
    assert "logging metrics create" in body
    assert "PoolCheckoutTimeout" in body
    assert "prod: database pool exhausted" in body
    assert "logging.googleapis.com/user/pool_checkout_timeout" in body


def test_the_log_metric_is_idempotent(source: str) -> None:
    """setup_monitoring.sh advertises itself as safe to re-run.

    `gcloud logging metrics create` fails on an existing metric, so without the describe guard a
    second run would abort under `set -e` before reaching the policies below it.
    """
    body = _uncommented(source)
    assert "logging metrics describe" in body
    create = body.index("logging metrics create")
    describe = body.index("logging metrics describe")
    assert describe < create, "existence check must precede creation"
