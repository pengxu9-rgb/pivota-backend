"""The server stamps an allowlisted probe merchant's order itself, so a TEST processor no longer
depends on the caller remembering a URL parameter — but ONLY when that merchant's processors are
actually test-mode.

Production evidence (2026-08-29, merch_c5e24a8d3738d73b, same env both times):
  ORD_9F4C24E73705231D  no allow_test_psp_surfaces  -> "All PSPs blocked: ... configured for test"
  ORD_50C00A24BEADFA78  stamped                     -> paid
A buyer reaching checkout through PDP -> cart -> Checkout never carries the parameter.
"""
from __future__ import annotations

import asyncio

import routes.order_routes as orr


def _run(coro):
    return asyncio.run(coro)


def _probe(monkeypatch, *, enabled=True, merchants="merch_probe"):
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1" if enabled else "0")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", merchants)


def _psps(monkeypatch, rows):
    """Patch the PSP lookup and return a call counter.

    The counter is load-bearing. There is no database in this environment, so an UNPATCHED lookup
    raises and the helper's blanket `except Exception: return False` yields exactly the same answer
    a real refusal does. Asserting the counter is what separates "the guard ran and said no" from
    "the guard never got there" — a review proved 8 tests here passed with the double removed.
    """
    calls = {"n": 0}

    async def fake(*, merchant_id, provider=None, database_override=None):
        calls["n"] += 1
        return rows

    import services.merchant_psp_config_service as svc

    monkeypatch.setattr(svc, "fetch_active_merchant_psps", fake)
    return calls


TEST_ROW = {"provider": "stripe", "environment": "test", "runtime_secret_key": "sk_test_abc"}
LIVE_ROW = {"provider": "stripe", "environment": "live", "runtime_secret_key": "sk_live_abc"}


# --- the delivering behaviour -------------------------------------------------------------

def test_stamps_an_allowlisted_test_mode_merchant_with_no_caller_stamp(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [TEST_ROW])
    metadata: dict = {}
    assert _run(orr._apply_server_granted_test_psp_stamp(metadata, "merch_probe")) is True
    assert metadata["allow_test_psp_surfaces"] is True
    assert metadata["test_psp_surfaces_granted_by"] == "server_allowlist"
    # The whole point: the gate now permits the test processor for this order.
    assert orr._resolve_order_live_readiness_requirement(metadata, "merch_probe") is False


def test_without_the_stamp_the_gate_still_enforces(monkeypatch):
    # Guards the premise: if this were already False, the test above would prove nothing.
    _probe(monkeypatch)
    assert orr._resolve_order_live_readiness_requirement({}, "merch_probe") is True


# --- the new safety guard -----------------------------------------------------------------

def test_refuses_when_the_merchant_has_a_LIVE_processor(monkeypatch):
    # The reason this is safer than the caller-supplied stamp: allowlisting a live merchant by
    # mistake must not route its real buyers to a test processor and mark them paid unpaid.
    _probe(monkeypatch)
    calls = _psps(monkeypatch, [LIVE_ROW])
    metadata: dict = {}
    assert _run(orr._apply_server_granted_test_psp_stamp(metadata, "merch_probe")) is False
    assert calls["n"] >= 1  # refused BY the guard, not by an absent database
    assert "allow_test_psp_surfaces" not in metadata


def test_refuses_when_ANY_processor_is_live(monkeypatch):
    _probe(monkeypatch)
    calls = _psps(monkeypatch, [TEST_ROW, LIVE_ROW])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False
    assert calls["n"] >= 1


def test_refuses_when_the_merchant_has_no_processors(monkeypatch):
    _probe(monkeypatch)
    calls = _psps(monkeypatch, [])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False
    assert calls["n"] >= 1


def test_refuses_when_the_psp_lookup_raises(monkeypatch):
    _probe(monkeypatch)

    calls = {"n": 0}

    async def boom(*, merchant_id, provider=None, database_override=None):
        calls["n"] += 1
        raise RuntimeError("db down")

    import services.merchant_psp_config_service as svc

    monkeypatch.setattr(svc, "fetch_active_merchant_psps", boom)
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False
    # Without this the test could not tell a caught RuntimeError from no lookup at all.
    assert calls["n"] == 1


def test_environment_missing_but_test_key_prefix_counts_as_test(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [{"provider": "stripe", "environment": "", "runtime_secret_key": "sk_test_x"}])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is True


def test_server_provenance_cannot_be_forged_by_the_caller(monkeypatch):
    # order metadata is caller-supplied, so a forged "server_allowlist" would make an incident
    # review unable to tell a server grant from a caller one. On a REFUSED merchant the field is
    # left exactly as the caller sent it and no stamp appears; on a granted one the server owns it.
    _probe(monkeypatch, merchants="merch_other")
    _psps(monkeypatch, [TEST_ROW])
    forged = {"test_psp_surfaces_granted_by": "server_allowlist"}
    assert _run(orr._apply_server_granted_test_psp_stamp(forged, "merch_probe")) is False
    assert "allow_test_psp_surfaces" not in forged
    assert orr._resolve_order_live_readiness_requirement(forged, "merch_probe") is True


# --- rows an adversarial review PROVED were live-in-reality yet passed the first draft ---------
# The first version hand-rolled a stripe-only `sk_live_` prefix check. The repo already had
# `normalize_psp_environment`, which knows every provider's real key shapes and treats a key prefix
# as stronger evidence than the `environment` column. Each row below is graded "live" by that
# canonical normalizer and MUST be refused.

def test_adyen_live_key_labelled_test_is_refused(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [{"provider": "adyen", "environment": "test", "runtime_secret_key": "live_ABCDEF"}])
    assert _run(orr._merchant_active_psp_is_test_mode("merch_probe")) is False
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False


def test_checkout_live_key_labelled_sandbox_is_refused(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [{"provider": "checkout", "environment": "sandbox", "api_key": "pk_live_xyz"}])
    assert _run(orr._merchant_active_psp_is_test_mode("merch_probe")) is False


def test_stripe_live_publishable_key_with_null_secret_is_refused(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [{"provider": "stripe", "environment": "test", "secret_key": None, "api_key": "pk_live_xyz"}])
    assert _run(orr._merchant_active_psp_is_test_mode("merch_probe")) is False


def test_live_secret_hiding_in_provider_config_is_refused(monkeypatch):
    # Non-Stripe credentials commonly live only in provider_config; grading the row on columns
    # alone reads a credential that is not the one which will charge the card.
    _probe(monkeypatch)
    _psps(monkeypatch, [{
        "provider": "stripe",
        "environment": "test",
        "secret_key": None,
        "api_key": None,
        "provider_config": {"secret_key": "sk_live_REAL"},
    }])
    assert _run(orr._merchant_active_psp_is_test_mode("merch_probe")) is False


def test_unknown_credential_shape_is_refused_not_assumed_test(monkeypatch):
    # normalize_psp_environment returns "unknown" when it cannot tell. Unknown must never buy a
    # test-mode bypass; "sandbox" is only meaningful for antom.
    _probe(monkeypatch)
    _psps(monkeypatch, [{"provider": "paypal", "environment": "sandbox", "runtime_secret_key": "whatever"}])
    assert _run(orr._merchant_active_psp_is_test_mode("merch_probe")) is False


def test_adyen_test_key_is_accepted(monkeypatch):
    # Positive counterpart: the guard must still PERMIT a genuinely test-mode non-Stripe processor,
    # or it would be refusing everything and the tests above would prove nothing.
    _probe(monkeypatch)
    _psps(monkeypatch, [{"provider": "adyen", "environment": "test", "runtime_secret_key": "test_ABCDEF"}])
    assert _run(orr._merchant_active_psp_is_test_mode("merch_probe")) is True


def test_a_live_key_is_refused_even_when_environment_claims_test(monkeypatch):
    # The dangerous shape: `environment` is a label someone types, the KEY is what charges a card.
    # A row mislabelled test while holding sk_live_ must never earn a test-mode bypass.
    _probe(monkeypatch)
    _psps(monkeypatch, [{"provider": "stripe", "environment": "test", "runtime_secret_key": "sk_live_x"}])
    assert _run(orr._merchant_active_psp_is_test_mode("merch_probe")) is False
    metadata: dict = {}
    assert _run(orr._apply_server_granted_test_psp_stamp(metadata, "merch_probe")) is False
    assert "allow_test_psp_surfaces" not in metadata


# --- containment: unchanged from before ---------------------------------------------------

def test_master_switch_off_refuses(monkeypatch):
    _probe(monkeypatch, enabled=False)
    calls = _psps(monkeypatch, [TEST_ROW])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False
    assert calls["n"] == 0  # short-circuits before any DB round trip


def test_merchant_not_allowlisted_is_refused(monkeypatch):
    _probe(monkeypatch, merchants="merch_other")
    calls = _psps(monkeypatch, [TEST_ROW])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False
    assert calls["n"] == 0  # no DB round trip for a merchant that can never qualify


def test_explicit_enforce_live_readiness_true_always_wins(monkeypatch):
    # The stricter choice must remain honourable from the caller, or the allowlist becomes a way to
    # force a test processor onto an order someone deliberately pinned to live.
    _probe(monkeypatch)
    _psps(monkeypatch, [TEST_ROW])
    metadata = {"enforce_live_readiness": True}
    assert _run(orr._apply_server_granted_test_psp_stamp(metadata, "merch_probe")) is False
    assert "allow_test_psp_surfaces" not in metadata
    assert orr._resolve_order_live_readiness_requirement(metadata, "merch_probe") is True


def test_non_dict_metadata_is_refused(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [TEST_ROW])
    assert _run(orr._apply_server_granted_test_psp_stamp(None, "merch_probe")) is False


def test_missing_merchant_is_refused(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [TEST_ROW])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "")) is False


def test_a_caller_already_permitted_order_is_left_alone(monkeypatch):
    # The "already permitted" early return. Dropping it made no test fail, yet it is what stops the
    # server re-deciding an order the caller already stamped — and what keeps the DB round trip off
    # the path when there is nothing to add.
    _probe(monkeypatch)
    calls = _psps(monkeypatch, [TEST_ROW])
    metadata = {"allow_test_psp_surfaces": True}
    assert _run(orr._apply_server_granted_test_psp_stamp(metadata, "merch_probe")) is False
    assert calls["n"] == 0
    assert "test_psp_surfaces_granted_by" not in metadata


def test_empty_merchant_never_reaches_the_psp_lookup(monkeypatch):
    # Guards the cheap early exit in _merchant_active_psp_is_test_mode.
    _probe(monkeypatch)
    calls = _psps(monkeypatch, [TEST_ROW])
    assert _run(orr._merchant_active_psp_is_test_mode("")) is False
    assert _run(orr._merchant_active_psp_is_test_mode(None)) is False  # type: ignore[arg-type]
    assert calls["n"] == 0


def test_a_live_secret_in_provider_config_beside_a_test_column_key_is_refused(monkeypatch):
    # The shape the "belt" loop exists for, and the one the earlier provider_config test missed:
    # the ROW's primary credential is a genuine test key, so grading the primary alone passes it —
    # while a LIVE secret sits in provider_config and is what several adapters actually charge on.
    # Every visible credential must clear, not merely the first one found.
    _probe(monkeypatch)
    calls = _psps(
        monkeypatch,
        [{
            "provider": "stripe",
            "environment": "test",
            "runtime_secret_key": "sk_test_looks_fine",
            "provider_config": {"secret_key": "sk_live_REAL"},
        }],
    )
    assert _run(orr._merchant_active_psp_is_test_mode("merch_probe")) is False
    assert calls["n"] >= 1
    metadata: dict = {}
    assert _run(orr._apply_server_granted_test_psp_stamp(metadata, "merch_probe")) is False
    assert "allow_test_psp_surfaces" not in metadata


def test_provider_config_stored_as_a_json_string_is_still_inspected(monkeypatch):
    # provider_config comes back as JSON text on some rows; a dict-only reader would skip it and
    # silently grade the row on the columns alone.
    _probe(monkeypatch)
    _psps(
        monkeypatch,
        [{
            "provider": "stripe",
            "environment": "test",
            "runtime_secret_key": "sk_test_looks_fine",
            "provider_config": '{"secret_key": "sk_live_REAL"}',
        }],
    )
    assert _run(orr._merchant_active_psp_is_test_mode("merch_probe")) is False
